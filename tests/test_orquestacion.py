"""Grafo de la corrida: orden de pasos, paradas, presupuesto y reanudación con checkpointer."""

from collections import Counter
from pathlib import Path

import pytest

from prediccion_ganadera.orquestacion.grafo import (
    ORDEN_PASOS,
    ErrorPaso,
    Orquestador,
    ResultadoPaso,
    abrir_checkpointer,
)


class Pasos:
    """Pasos falsos que registran sus llamadas y el estado que reciben."""

    def __init__(self, **especiales):
        self.llamadas: Counter[str] = Counter()
        self.recibido: dict[str, dict] = {}
        self.especiales = especiales

    def __getitem__(self, nombre: str):
        def paso(estado):
            self.llamadas[nombre] += 1
            self.recibido[nombre] = dict(estado)
            especial = self.especiales.get(nombre)
            if especial is not None:
                return especial(estado, self.llamadas[nombre])
            return ResultadoPaso(resumen={"paso": nombre}, tokens=10, costo_usd=0.5)

        return paso

    def como_dict(self):
        return {nombre: self[nombre] for nombre in ORDEN_PASOS}


@pytest.fixture
def checkpointer(tmp_path: Path):
    with abrir_checkpointer(tmp_path / "corridas" / "checkpoints.sqlite") as guardado:
        yield guardado


def test_ejecuta_los_pasos_en_orden_y_acumula_el_uso(checkpointer):
    pasos = Pasos()
    orquestador = Orquestador(pasos.como_dict(), checkpointer, presupuesto_tokens=1000)

    estado = orquestador.iniciar("corrida-1")

    assert ORDEN_PASOS == ("recolectar", "extraer", "verificar", "cargar")
    assert list(estado["pasos"]) == list(ORDEN_PASOS)
    assert all(pasos.llamadas[n] == 1 for n in ORDEN_PASOS)
    assert estado["estado"] == "completada"
    assert estado["tokens"] == 40
    assert estado["costo_usd"] == pytest.approx(2.0)
    assert estado["pasos"]["extraer"]["resumen"] == {"paso": "extraer"}
    assert estado["pasos"]["extraer"]["duracion_s"] >= 0
    assert orquestador.pendientes("corrida-1") == ()


def test_cada_paso_recibe_el_presupuesto_restante(checkpointer):
    pasos = Pasos()
    Orquestador(pasos.como_dict(), checkpointer, presupuesto_tokens=1000).iniciar("c")

    assert pasos.recibido["recolectar"]["presupuesto_restante"] == 1000
    assert pasos.recibido["verificar"]["presupuesto_restante"] == 980


def test_un_error_esperado_detiene_la_corrida_sin_ejecutar_los_siguientes(checkpointer):
    def falla(_estado, _n):
        raise ErrorPaso("no se pudo leer la fuente")

    pasos = Pasos(recolectar=falla)
    estado = Orquestador(pasos.como_dict(), checkpointer, presupuesto_tokens=10).iniciar("c")

    assert estado["estado"] == "fallida"
    assert estado["error"] == "recolectar: no se pudo leer la fuente"
    assert pasos.llamadas["extraer"] == 0
    assert "recolectar" not in estado["pasos"]


def test_un_paso_puede_omitirse_con_un_motivo(checkpointer):
    pasos = Pasos(cargar=lambda _e, _n: ResultadoPaso(omitido="sin conexión a Postgres"))
    estado = Orquestador(pasos.como_dict(), checkpointer, presupuesto_tokens=10).iniciar("c")

    assert estado["estado"] == "completada"
    assert estado["pasos"]["cargar"]["omitido"] == "sin conexión a Postgres"


def test_reanuda_tras_una_caida_sin_repetir_los_pasos_terminados(tmp_path: Path):
    def cae_la_primera_vez(_estado, intento):
        if intento == 1:
            raise RuntimeError("se cayó el proceso")
        return ResultadoPaso(resumen={"intento": intento})

    pasos = Pasos(verificar=cae_la_primera_vez)
    ruta = tmp_path / "checkpoints.sqlite"
    with abrir_checkpointer(ruta) as guardado, pytest.raises(RuntimeError):
        Orquestador(pasos.como_dict(), guardado, presupuesto_tokens=100).iniciar("c")
    # Un proceso nuevo abre el mismo archivo y continúa donde quedó.
    with abrir_checkpointer(ruta) as guardado:
        orquestador = Orquestador(pasos.como_dict(), guardado, presupuesto_tokens=100)
        assert orquestador.pendientes("c") == ("verificar",)
        estado = orquestador.reanudar("c")

    assert estado["estado"] == "completada"
    assert pasos.llamadas == Counter(recolectar=1, extraer=1, verificar=2, cargar=1)
    assert estado["pasos"]["verificar"]["resumen"] == {"intento": 2}
    assert estado["tokens"] == 30


def test_no_reanuda_una_corrida_terminada_ni_inexistente(checkpointer):
    orquestador = Orquestador(Pasos().como_dict(), checkpointer, presupuesto_tokens=10)
    orquestador.iniciar("hecha")

    with pytest.raises(ErrorPaso, match="no hay pasos pendientes"):
        orquestador.reanudar("hecha")
    with pytest.raises(ErrorPaso, match="no existe"):
        orquestador.reanudar("otra")


def test_no_inicia_dos_veces_la_misma_corrida(checkpointer):
    orquestador = Orquestador(Pasos().como_dict(), checkpointer, presupuesto_tokens=10)
    orquestador.iniciar("c")

    with pytest.raises(ErrorPaso, match="ya existe"):
        orquestador.iniciar("c")


def test_exige_los_cuatro_pasos(checkpointer):
    pasos = Pasos().como_dict()
    del pasos["cargar"]

    with pytest.raises(ValueError, match="cargar"):
        Orquestador(pasos, checkpointer, presupuesto_tokens=10)
