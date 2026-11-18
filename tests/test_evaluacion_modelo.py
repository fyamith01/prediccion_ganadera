"""Métricas del modelo por versión y seguimiento de sus predicciones (T9d)."""

from __future__ import annotations

import shutil
from datetime import UTC, datetime

import pytest

from prediccion_ganadera.adaptadores.almacen.oro import AlmacenOro
from prediccion_ganadera.evaluacion.modelo import evaluar_modelos
from prediccion_ganadera.modelado.modelador import ejecutar_modelado
from prediccion_ganadera.modelado.variables import ResultadoPrecios

AHORA = datetime(2026, 11, 18, 12, tzinfo=UTC)


def _hasta(precios: ResultadoPrecios, semanas: int) -> ResultadoPrecios:
    fechas = precios.fechas[:semanas]
    return precios.model_copy(update={"precios": [p for p in precios.precios if p.fecha in fechas]})


def _entrenar(precios: ResultadoPrecios, datos, ahora: datetime = AHORA) -> str:
    resumen = ejecutar_modelado(
        precios, datos=datos, fuente="plata", incluir_no_verificadas=False, ahora=ahora
    )
    return resumen.version


@pytest.fixture(scope="module")
def dos_versiones(tmp_path_factory, precios_sinteticos):
    datos = tmp_path_factory.mktemp("datos")
    completo = precios_sinteticos(44)
    primera = _entrenar(_hasta(completo, 34), datos, AHORA)
    segunda = _entrenar(_hasta(completo, 38), datos, AHORA.replace(hour=13))
    return datos, completo, primera, segunda


def test_metricas_por_version_y_cambio_frente_a_la_anterior(dos_versiones):
    datos, _, primera, segunda = dos_versiones

    evaluacion = evaluar_modelos(datos, observados=None)

    assert [v.version for v in evaluacion.versiones] == [primera, segunda]
    v1, v2 = evaluacion.versiones
    assert v1.cambio_mae is None
    assert v2.cambio_mae == pytest.approx(v2.mae - v1.mae)
    assert v2.cambio_mejora == pytest.approx(v2.mejora - v1.mejora)
    assert v1.fechas_historico == 34 and v2.fechas_historico == 38
    assert v1.confianza in ("alta", "media", "baja")
    assert set(v1.mae_por_horizonte) == set(v1.mejora_por_horizonte)
    assert v1.seguimiento is None  # sin precios observados no hay seguimiento


def test_seguimiento_compara_predicciones_con_precios_observados(dos_versiones):
    datos, completo, primera, _ = dos_versiones

    evaluacion = evaluar_modelos(datos, observados=completo.precios)

    seguimiento = evaluacion.versiones[0].seguimiento
    predicciones = AlmacenOro(datos / "oro").leer_predicciones(primera)
    reales = {(p.fecha, p.categoria): p.precio_promedio for p in completo.precios}
    pares = [(p, reales[(p.fecha, p.categoria)]) for p in predicciones]
    assert seguimiento.predicciones == len(predicciones)
    assert seguimiento.comparadas == len(pares) > 0
    assert seguimiento.mae == pytest.approx(
        sum(abs(p.precio_predicho - r) for p, r in pares) / len(pares)
    )
    assert seguimiento.mae_ingenuo == pytest.approx(
        sum(abs(p.ultimo_precio - r) for p, r in pares) / len(pares)
    )
    dentro = sum(abs(p.precio_predicho - r) <= p.error_esperado for p, r in pares)
    assert seguimiento.dentro_del_error == pytest.approx(dentro / len(pares))
    # en la serie sintética el patrón alterno es aprendible: el modelo le gana al ingenuo
    assert seguimiento.mejora > 0


def test_seguimiento_sin_fechas_observadas_aun(dos_versiones):
    datos, completo, _, _ = dos_versiones

    evaluacion = evaluar_modelos(datos, observados=_hasta(completo, 38).precios)

    segunda = evaluacion.versiones[1].seguimiento
    assert segunda.comparadas == 0
    assert segunda.mae is None
    assert segunda.mejora is None


def test_registro_vacio(tmp_path):
    evaluacion = evaluar_modelos(tmp_path, observados=None)

    assert evaluacion.versiones == []
    assert evaluacion.sin_metadatos == []


def test_version_sin_carpeta_se_informa(tmp_path, precios_sinteticos):
    version = _entrenar(precios_sinteticos(34), tmp_path)
    shutil.rmtree(tmp_path / "modelos" / version)

    evaluacion = evaluar_modelos(tmp_path, observados=None)

    assert evaluacion.versiones == []
    assert evaluacion.sin_metadatos == [version]
