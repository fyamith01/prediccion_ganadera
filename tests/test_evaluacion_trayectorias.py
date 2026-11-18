"""Métricas de trayectoria a partir del registro de corridas (T9c)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from prediccion_ganadera.adaptadores.almacen.corridas import (
    RegistroCorrida,
    RegistroJsonl,
    RegistroPostgres,
)
from prediccion_ganadera.evaluacion.trayectorias import evaluar_trayectorias

T0 = datetime(2026, 11, 18, 8, 0, tzinfo=UTC)


def _tabla(estado: str, verificaciones: int, acciones: list[str], motivo: str = "") -> dict:
    return {
        "clave": {"sha256": "ab" * 32, "pagina": 1, "tabla": 1},
        "estado": estado,
        "verificaciones": verificaciones,
        "acciones": acciones,
        "motivo": motivo,
        "hallazgos": [],
    }


def _pasos(*, verificar: list[dict] | None = None, omitir_cargar: bool = False) -> dict:
    def paso(resumen: dict | None = None, tokens: int = 0, duracion: float = 1.0, omitido=None):
        return {
            "resumen": resumen or {},
            "omitido": omitido,
            "tokens": tokens,
            "costo_usd": tokens / 1000,
            "duracion_s": duracion,
        }

    pasos = {
        "recolectar": paso(
            {
                "enlaces": 95,
                "decisiones": [{"url": "u", "fecha": "2025-01-24", "razon": "r"}],
                "revision": [],
                "huecos": {
                    "desde": "2025-01-10",
                    "hasta": "2025-01-31",
                    "esperadas": ["2025-01-10", "2025-01-17", "2025-01-24", "2025-01-31"],
                    "faltantes": ["2025-01-17"],
                    "fuera_de_calendario": [],
                    "duplicadas": [],
                },
                "descarga": {"nuevos": [{}, {}], "omitidos": [], "errores": [["u", "404"]]},
            },
            tokens=100,
            duracion=2.0,
        ),
        "extraer": paso(duracion=3.0),
    }
    if verificar is not None:
        pasos["verificar"] = paso({"tablas": verificar}, tokens=500, duracion=10.0)
    pasos["cargar"] = paso(omitido="sin conexión a Postgres") if omitir_cargar else paso()
    return pasos


def _registro(id_corrida: str, estado: str, minutos: int = 5, **cambios) -> RegistroCorrida:
    terminada = T0 + timedelta(minutes=minutos) if estado in ("completada", "fallida") else None
    base = {
        "id_corrida": id_corrida,
        "estado": estado,
        "iniciada_en": T0,
        "actualizada_en": T0 + timedelta(minutes=minutos),
        "terminada_en": terminada,
        "tokens": 0,
        "costo_usd": 0.0,
        "error": None,
        "pasos": {},
    }
    return RegistroCorrida.model_validate({**base, **cambios})


TABLAS = [
    _tabla("verificada", 1, []),
    _tabla("corregida", 2, ["anexar_renglones: 1", "corregir_celdas: 2"]),
    _tabla("en_revision", 3, ["reextraer: {}", "anexar_renglones: 1", "reextraer: {}"]),
    _tabla("pendiente", 0, [], "presupuesto de 100 tokens agotado"),
]


def test_metricas_de_una_corrida_completa():
    historial = [
        _registro("a", "en_curso"),
        _registro("a", "completada", tokens=600, costo_usd=0.6, pasos=_pasos(verificar=TABLAS)),
    ]

    metricas = evaluar_trayectorias(historial, con_historial=True)

    assert metricas.corridas == 1
    assert metricas.por_estado == {"completada": 1}
    assert metricas.tasa_exito == 1.0
    assert metricas.tokens_totales == 600
    corrida = metricas.detalle[0]
    assert corrida.duracion_s == 300
    assert corrida.duracion_pasos == {
        "recolectar": 2.0, "extraer": 3.0, "verificar": 10.0, "cargar": 1.0,
    }  # fmt: skip
    assert corrida.reanudada is False

    verificador = metricas.verificador
    assert verificador.tablas == 4
    assert verificador.por_estado == {
        "verificada": 1, "corregida": 1, "en_revision": 1, "pendiente": 1,
    }  # fmt: skip
    assert verificador.llamadas_vision == 6
    assert verificador.rectificaciones == 5
    assert verificador.acciones_por_tipo == {
        "anexar_renglones": 2, "corregir_celdas": 1, "reextraer": 2,
    }  # fmt: skip
    assert verificador.max_rectificaciones_tabla == 3
    assert verificador.pendientes_por_presupuesto == 1
    # de las 3 tablas con decisión final, 2 se resolvieron sin intervención humana
    assert verificador.tasa_automatica == pytest.approx(2 / 3)
    assert verificador.tasa_revision == pytest.approx(1 / 3)

    recolector = metricas.recolector_ultima
    assert recolector.enlaces == 95
    assert recolector.cobertura == pytest.approx(3 / 4)
    assert recolector.decisiones_ia == 1
    assert recolector.descargados == 2
    assert recolector.errores_descarga == 1


def test_reanudadas_fallidas_y_omisiones():
    historial = [
        _registro("a", "en_curso"),
        _registro("a", "interrumpida", error="boom"),
        _registro("a", "en_curso"),
        _registro("a", "completada", pasos=_pasos(omitir_cargar=True)),
        _registro("b", "en_curso"),
        _registro("b", "fallida", error="recolectar: 406", pasos={}),
        _registro("c", "en_curso"),  # sigue en curso: no cuenta para la tasa de éxito
    ]

    metricas = evaluar_trayectorias(historial, con_historial=True)

    assert metricas.corridas == 3
    assert metricas.por_estado == {"completada": 1, "fallida": 1, "en_curso": 1}
    assert metricas.tasa_exito == pytest.approx(1 / 2)
    assert metricas.reanudadas == 1
    assert metricas.omisiones == {"cargar: sin conexión a Postgres": 1}
    assert metricas.errores == {"b": "recolectar: 406"}
    assert metricas.verificador.tablas == 0
    assert metricas.verificador.tasa_automatica is None


def test_sin_historial_no_se_afirman_reanudaciones():
    metricas = evaluar_trayectorias([_registro("a", "completada")], con_historial=False)

    assert metricas.reanudadas is None
    assert metricas.detalle[0].reanudada is None


def test_registro_vacio():
    metricas = evaluar_trayectorias([], con_historial=True)

    assert metricas.corridas == 0
    assert metricas.tasa_exito is None
    assert metricas.recolector_ultima is None


def test_historial_jsonl_conserva_todas_las_versiones(tmp_path):
    registro = RegistroJsonl(tmp_path / "corridas.jsonl")
    registro.guardar(_registro("a", "en_curso"))
    registro.guardar(_registro("a", "completada"))

    assert [r.estado for r in registro.historial()] == ["en_curso", "completada"]
    assert list(registro.ultimas()) == ["a"]
    assert RegistroJsonl(tmp_path / "no_existe.jsonl").historial() == []


def test_registro_postgres_lee_la_ultima_version(postgres_dsn):
    registro = RegistroPostgres(postgres_dsn)
    registro.guardar(_registro("a", "en_curso"))
    registro.guardar(_registro("a", "completada", pasos=_pasos(verificar=TABLAS)))
    registro.guardar(_registro("b", "fallida", error="x"))

    leidas = registro.leer()

    assert [(r.id_corrida, r.estado) for r in leidas] == [("a", "completada"), ("b", "fallida")]
    assert evaluar_trayectorias(leidas, con_historial=False).verificador.tablas == 4


def test_registro_postgres_sin_tabla_devuelve_vacio(postgres_dsn):
    assert RegistroPostgres(postgres_dsn).leer() == []
    with psycopg.connect(postgres_dsn) as conexion:  # leer no crea esquemas
        existe = conexion.execute("SELECT to_regclass('operacion.runs')").fetchone()[0]
    assert existe is None
