"""Registro de corridas: JSONL local, tabla `operacion.runs` y su uso desde el orquestador."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg
import pytest

from prediccion_ganadera.adaptadores.almacen.corridas import (
    RegistroCorrida,
    RegistroJsonl,
    RegistroPostgres,
)
from prediccion_ganadera.orquestacion.grafo import (
    ORDEN_PASOS,
    ErrorPaso,
    Orquestador,
    ResultadoPaso,
    abrir_checkpointer,
)

T0 = datetime(2026, 11, 18, 8, 0, tzinfo=UTC)


def _registro(estado: str = "en_curso", **cambios) -> RegistroCorrida:
    datos = {
        "id_corrida": "20261118-080000",
        "estado": estado,
        "iniciada_en": T0,
        "actualizada_en": T0,
        "terminada_en": None,
        "tokens": 0,
        "costo_usd": 0.0,
        "error": None,
        "pasos": {},
    }
    return RegistroCorrida(**(datos | cambios))


def test_jsonl_agrega_versiones_y_devuelve_la_ultima_por_corrida(tmp_path: Path):
    registro = RegistroJsonl(tmp_path / "corridas" / "corridas.jsonl")
    registro.guardar(_registro())
    final = _registro(
        "completada",
        terminada_en=T0 + timedelta(minutes=3),
        tokens=120,
        pasos={"extraer": {"resumen": {"tablas": 4}}},
    )
    registro.guardar(final)
    registro.guardar(_registro(id_corrida="otra"))

    lineas = (tmp_path / "corridas" / "corridas.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lineas) == 3
    assert registro.ultimas() == {"20261118-080000": final, "otra": _registro(id_corrida="otra")}


def test_jsonl_vacio_no_tiene_corridas(tmp_path: Path):
    assert RegistroJsonl(tmp_path / "corridas.jsonl").ultimas() == {}


def test_postgres_hace_upsert_por_id_de_corrida(postgres_dsn: str):
    registro = RegistroPostgres(postgres_dsn)
    registro.guardar(_registro())
    registro.guardar(
        _registro(
            "fallida",
            actualizada_en=T0 + timedelta(seconds=5),
            terminada_en=T0 + timedelta(seconds=5),
            tokens=7,
            costo_usd=0.25,
            error="recolectar: sin red",
            pasos={"recolectar": {"tokens": 7}},
        )
    )

    with psycopg.connect(postgres_dsn) as conexion:
        filas = conexion.execute(
            "SELECT id_corrida, estado, iniciada_en, terminada_en, tokens, costo_usd, error, pasos "
            "FROM operacion.runs"
        ).fetchall()
    assert filas == [
        (
            "20261118-080000",
            "fallida",
            T0,
            T0 + timedelta(seconds=5),
            7,
            0.25,
            "recolectar: sin red",
            {"recolectar": {"tokens": 7}},
        )
    ]


def test_el_registro_rechaza_un_estado_desconocido():
    with pytest.raises(ValueError):
        _registro("rara")


class Memoria:
    def __init__(self, falla: bool = False):
        self.guardados: list[RegistroCorrida] = []
        self.falla = falla

    def guardar(self, registro: RegistroCorrida) -> None:
        if self.falla:
            raise OSError("disco lleno")
        self.guardados.append(registro)


def _pasos(**especiales):
    def crear(nombre):
        return especiales.get(nombre, lambda _e: ResultadoPaso(resumen={"paso": nombre}, tokens=5))

    return {nombre: crear(nombre) for nombre in ORDEN_PASOS}


class Reloj:
    def __init__(self):
        self.ahora = T0

    def __call__(self) -> datetime:
        self.ahora += timedelta(seconds=1)
        return self.ahora


def test_el_orquestador_registra_inicio_y_fin(tmp_path: Path):
    memoria = Memoria()
    with abrir_checkpointer(tmp_path / "c.sqlite") as guardado:
        Orquestador(
            _pasos(), guardado, presupuesto_tokens=100, registros=[memoria], reloj=Reloj()
        ).iniciar("c1")

    inicio, fin = memoria.guardados
    assert (inicio.estado, inicio.terminada_en, inicio.pasos) == ("en_curso", None, {})
    assert fin.estado == "completada"
    assert fin.iniciada_en == inicio.iniciada_en
    assert fin.terminada_en == fin.actualizada_en > inicio.iniciada_en
    assert fin.tokens == 20
    assert list(fin.pasos) == list(ORDEN_PASOS)


def test_un_error_esperado_queda_registrado_como_fallida(tmp_path: Path):
    def falla(_estado):
        raise ErrorPaso("sin red")

    memoria = Memoria()
    with abrir_checkpointer(tmp_path / "c.sqlite") as guardado:
        Orquestador(
            _pasos(recolectar=falla), guardado, presupuesto_tokens=100, registros=[memoria]
        ).iniciar("c1")

    assert memoria.guardados[-1].estado == "fallida"
    assert memoria.guardados[-1].error == "recolectar: sin red"


def test_una_caida_queda_interrumpida_y_al_reanudar_conserva_el_inicio(tmp_path: Path):
    intentos = []

    def cae_una_vez(_estado):
        intentos.append(1)
        if len(intentos) == 1:
            raise RuntimeError("se cayó")
        return ResultadoPaso()

    memoria = Memoria()
    reloj = Reloj()
    pasos = _pasos(verificar=cae_una_vez)
    with abrir_checkpointer(tmp_path / "c.sqlite") as guardado:
        orquestador = Orquestador(
            pasos, guardado, presupuesto_tokens=100, registros=[memoria], reloj=reloj
        )
        with pytest.raises(RuntimeError):
            orquestador.iniciar("c1")
        caida = memoria.guardados[-1]
        orquestador.reanudar("c1")

    assert caida.estado == "interrumpida"
    assert caida.terminada_en is None
    assert "se cayó" in caida.error
    assert list(caida.pasos) == ["recolectar", "extraer"]
    reanudada, fin = memoria.guardados[-2:]
    assert reanudada.estado == "en_curso"
    assert fin.estado == "completada"
    assert fin.error is None
    assert fin.iniciada_en == caida.iniciada_en


def test_un_registro_que_falla_no_detiene_la_corrida(tmp_path: Path, caplog):
    with abrir_checkpointer(tmp_path / "c.sqlite") as guardado:
        estado = Orquestador(
            _pasos(), guardado, presupuesto_tokens=100, registros=[Memoria(falla=True)]
        ).iniciar("c1")

    assert estado["estado"] == "completada"
    assert "disco lleno" in caplog.text
