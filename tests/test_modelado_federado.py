import json
import random
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest
import xgboost as xgb
from flwr.common import FitIns, Parameters
from flwr.server.strategy.fedxgb_bagging import aggregate

from prediccion_ganadera.modelado.dataset import construir_dataset, entradas, serie_precios
from prediccion_ganadera.modelado.entrenamiento import PARAMETROS_XGB, OpcionesEntrenamiento
from prediccion_ganadera.modelado.federado import (
    ClienteXGB,
    asignar_clientes,
    comparar_federado,
    diagnosticar_reparto,
    entrenar_federado,
    parametros_cliente,
)
from prediccion_ganadera.modelado.variables import PrecioCategoria

INICIO = date(2025, 1, 3)  # viernes
SECCIONES = {"VACA HORRA": "hembra", "HEMBRA LEVANTE": "hembra", "MACHO LEVANTE": "macho"}
BASES = {"VACA HORRA": 7800, "MACHO LEVANTE": 10500, "HEMBRA LEVANTE": 9800}
RAPIDO = {"num_boost_round": 40}


def _precio(fecha: date, categoria: str, promedio: int, *, especial=False) -> PrecioCategoria:
    return PrecioCategoria(
        fecha=fecha,
        especial=especial,
        sha256="a" * 64,
        seccion=SECCIONES[categoria],
        categoria=categoria,
        unidad="kg",
        animales=40,
        peso_cabeza=300,
        precio_minimo=promedio,
        precio_maximo=promedio,
        precio_promedio=promedio,
        valor_animal=promedio * 300,
    )


def _alternantes(
    semanas: int, *, especiales: frozenset[int] = frozenset()
) -> list[PrecioCategoria]:
    """Sube y baja 10 % semana a semana (aprendible desde la tendencia)."""
    azar = random.Random(3)
    return [
        _precio(
            INICIO + timedelta(weeks=i),
            categoria,
            round(base * (1.1 if i % 2 else 1.0) * azar.uniform(0.995, 1.005)),
            especial=i in especiales,
        )
        for i in range(semanas)
        for categoria, base in BASES.items()
    ]


def _dataset(precios, **kwargs):
    return construir_dataset(serie_precios(precios, **kwargs))


def test_reparto_por_anio_usa_el_anio_de_la_fecha_objetivo():
    dataset = _dataset(_alternantes(70))

    clientes = asignar_clientes(dataset, "anio", precios=_alternantes(70))

    assert clientes.index.equals(dataset.index)
    assert set(clientes) == {"2025", "2026"}
    assert (clientes == dataset.fecha.dt.year.astype(str)).all()


def test_reparto_por_seccion_agrupa_las_categorias_de_hembras_y_machos():
    precios = _alternantes(30)
    dataset = _dataset(precios)

    clientes = asignar_clientes(dataset, "seccion", precios=precios)

    por_categoria = dict(zip(dataset.categoria.astype(str), clientes, strict=True))
    assert por_categoria == SECCIONES


def test_reparto_por_tipo_de_subasta_con_una_sola_especial_no_es_viable():
    precios = _alternantes(40, especiales=frozenset({20}))
    dataset = _dataset(precios, incluir_especiales=True)
    clientes = asignar_clientes(dataset, "tipo_subasta", precios=precios)

    diagnostico = diagnosticar_reparto(dataset, clientes, minimo_fechas=12)

    assert diagnostico.fechas_por_cliente == {"especial": 1, "ordinaria": 38}
    assert not diagnostico.viable
    assert any("especial" in m and "1 fecha" in m for m in diagnostico.motivos)


def test_un_solo_cliente_no_es_un_reparto_federado():
    dataset = _dataset(_alternantes(30))
    clientes = pd.Series("2025", index=dataset.index)

    diagnostico = diagnosticar_reparto(dataset, clientes, minimo_fechas=12)

    assert not diagnostico.viable
    assert any("un solo cliente" in m for m in diagnostico.motivos)


def test_cliente_entrena_solo_con_sus_filas_y_devuelve_solo_los_arboles_nuevos():
    dataset = _dataset(_alternantes(30))
    parte = dataset[dataset.categoria == "VACA HORRA"]
    parametros = {**PARAMETROS_XGB, "base_score": 0.0}
    parametros.pop("num_boost_round")
    cliente = ClienteXGB(entradas(parte), parte.objetivo, sorted(BASES), parametros)

    primera = cliente.fit(FitIns(Parameters(tensors=[], tensor_type=""), {}))
    segunda = cliente.fit(FitIns(primera.parameters, {}))

    assert primera.num_examples == len(parte)
    for respuesta in (primera, segunda):
        (modelo,) = respuesta.parameters.tensors
        arboles = json.loads(modelo)["learner"]["gradient_booster"]["model"]["trees"]
        assert len(arboles) == 1


def test_el_bagging_de_flower_solo_toma_un_arbol_por_respuesta_de_cliente():
    """Por eso cada cliente agrega un solo árbol por ronda: con más, Flower descartaría
    los demás sin avisar (`aggregate` copia `num_parallel_tree` árboles)."""
    dataset = _dataset(_alternantes(20))
    matriz = xgb.DMatrix(dataset[["ultimo_precio", "tendencia"]], label=dataset.objetivo)
    primero = xgb.train({"nthread": 1}, matriz, num_boost_round=1)
    tres = xgb.train({"nthread": 1}, matriz, num_boost_round=3)

    unido = aggregate(bytes(primero.save_raw("json")), bytes(tres.save_raw("json")))

    arboles = json.loads(unido)["learner"]["gradient_booster"]["model"]["trees"]
    assert len(arboles) == 2


def test_parametros_de_cliente_reparten_el_aprendizaje_entre_rondas_y_clientes():
    central = {**PARAMETROS_XGB, "eta": 0.05, "num_boost_round": 300}

    locales = parametros_cliente(central, n_clientes=2, rondas=60)

    assert locales["eta"] == pytest.approx(0.05 * 300 / 60 / 2)
    assert locales["base_score"] == 0.0
    assert "num_boost_round" not in locales


def test_entrenamiento_federado_agrega_los_arboles_de_todos_los_clientes():
    dataset = _dataset(_alternantes(40))
    clientes = asignar_clientes(dataset, "seccion", precios=_alternantes(40))

    def entrenar():
        return entrenar_federado(
            entradas(dataset),
            dataset.objetivo,
            clientes,
            sorted(BASES),
            PARAMETROS_XGB,
            rondas=4,
        )

    modelo = entrenar()

    assert modelo.booster.num_boosted_rounds() == 4 * 2  # rondas × clientes (bagging)
    np.testing.assert_array_equal(
        modelo.predict(entradas(dataset)), entrenar().predict(entradas(dataset))
    )  # reproducible


def test_compara_federado_y_centralizado_con_los_mismos_pliegues():
    precios = _alternantes(70)

    comparacion = comparar_federado(
        precios, "anio", OpcionesEntrenamiento(parametros=RAPIDO), rondas=20
    )

    assert comparacion.viable
    assert comparacion.rondas == 20
    assert comparacion.fechas_por_cliente.keys() == {"2025", "2026"}
    central, federado = comparacion.central, comparacion.federado
    assert central is not None and federado is not None
    assert (federado.pliegues, federado.fechas_evaluadas, federado.filas_evaluadas) == (
        central.pliegues,
        central.fechas_evaluadas,
        central.filas_evaluadas,
    )
    assert federado.mejora > 0  # aprende el patrón, no solo copia el último precio
    assert len(comparacion.clientes_por_pliegue) == central.pliegues
    assert max(comparacion.clientes_por_pliegue) == 2
    assert comparacion.diferencia_mae == pytest.approx(federado.mae - central.mae)


def test_reparto_no_viable_se_documenta_sin_forzar_metricas():
    precios = _alternantes(40, especiales=frozenset({20}))

    comparacion = comparar_federado(
        precios, "tipo_subasta", OpcionesEntrenamiento(parametros=RAPIDO)
    )

    assert not comparacion.viable
    assert comparacion.central is None and comparacion.federado is None
    assert comparacion.diferencia_mae is None
    assert comparacion.motivos
