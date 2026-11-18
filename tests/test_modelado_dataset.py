import math
import random
from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from prediccion_ganadera.modelado.dataset import (
    COLUMNAS_VARIABLES,
    DatosInsuficientes,
    construir_dataset,
    evaluar_confianza,
    particiones_temporales,
    serie_precios,
    validar,
)
from prediccion_ganadera.modelado.variables import PrecioCategoria

INICIO = date(2025, 1, 3)  # viernes
BASES = {"VACA HORRA": 7800, "MACHO LEVANTE": 10500, "HEMBRA LEVANTE": 9800}


def _precio(fecha, categoria, promedio, *, unidad="kg", especial=False, animales=40):
    peso = 1 if unidad == "cabeza" else 300
    return PrecioCategoria(
        fecha=fecha,
        especial=especial,
        sha256="a" * 64,
        seccion="hembra",
        categoria=categoria,
        unidad=unidad,
        animales=animales,
        peso_cabeza=peso,
        precio_minimo=promedio,
        precio_maximo=promedio,
        precio_promedio=promedio,
        valor_animal=promedio * peso,
    )


def _sinteticos(semanas: int, *, semilla: int = 7) -> list[PrecioCategoria]:
    """Precios semanales con estacionalidad anual y ruido, reproducibles."""
    azar = random.Random(semilla)
    precios = []
    for i in range(semanas):
        fecha = INICIO + timedelta(weeks=i)
        for categoria, base in BASES.items():
            ciclo = 1 + 0.08 * math.sin(2 * math.pi * i / 52)
            promedio = round(base * ciclo * azar.uniform(0.98, 1.02))
            precios.append(_precio(fecha, categoria, promedio))
    return precios


def test_serie_deja_fuera_precios_por_cabeza_y_subastas_especiales():
    f = INICIO
    precios = [
        _precio(f, "VACA HORRA", 7800),
        _precio(f, "MULETO", 3_000_000, unidad="cabeza"),
        _precio(f + timedelta(days=1), "VACA HORRA", 9000, especial=True),
    ]

    serie = serie_precios(precios)
    con_especiales = serie_precios(precios, incluir_especiales=True)

    assert serie[["fecha", "categoria", "precio"]].values.tolist() == [
        [pd.Timestamp(f), "VACA HORRA", 7800]
    ]
    assert len(con_especiales) == 2


def test_variables_usan_solo_subastas_anteriores_al_horizonte():
    serie = serie_precios(_sinteticos(20))
    dataset = construir_dataset(serie, horizontes=(1, 3))

    fila = dataset[
        (dataset.fecha == pd.Timestamp(INICIO + timedelta(weeks=10)))
        & (dataset.categoria == "VACA HORRA")
        & (dataset.horizonte == 3)
    ].iloc[0]
    precios_vaca = serie[serie.categoria == "VACA HORRA"].set_index("fecha").precio

    referencia = pd.Timestamp(INICIO + timedelta(weeks=7))
    assert fila.fecha_referencia == referencia
    assert fila.ultimo_precio == precios_vaca[referencia]
    assert fila.media_4 == pytest.approx(precios_vaca[:referencia].iloc[-4:].mean())
    assert fila.semanas_desde_ultimo == pytest.approx(3)
    assert fila.precio == precios_vaca[pd.Timestamp(INICIO + timedelta(weeks=10))]
    assert fila.objetivo == pytest.approx(fila.precio / fila.ultimo_precio - 1)


def test_cambiar_precios_futuros_no_cambia_las_variables():
    serie = serie_precios(_sinteticos(20))
    alterada = serie.copy()
    corte = pd.Timestamp(INICIO + timedelta(weeks=7))
    alterada.loc[alterada.fecha > corte, "precio"] *= 3
    alterada.loc[alterada.fecha > corte, "animales"] = 999

    def variables(s):
        d = construir_dataset(s, horizontes=(1, 2, 3))
        return d[d.fecha_referencia <= corte].sort_values(["fecha", "categoria", "horizonte"])

    a, b = variables(serie), variables(alterada)
    pd.testing.assert_frame_equal(
        a[COLUMNAS_VARIABLES].reset_index(drop=True), b[COLUMNAS_VARIABLES].reset_index(drop=True)
    )


def test_categoria_ausente_usa_su_ultimo_precio_conocido():
    f = [INICIO + timedelta(weeks=i) for i in range(6)]
    precios = [_precio(d, "MACHO LEVANTE", 10000 + i) for i, d in enumerate(f)]
    precios += [_precio(f[0], "VACA HORRA", 7000), _precio(f[4], "VACA HORRA", 7400)]

    dataset = construir_dataset(serie_precios(precios), horizontes=(1,))
    fila = dataset[(dataset.categoria == "VACA HORRA") & (dataset.fecha == pd.Timestamp(f[4]))]

    assert fila.ultimo_precio.item() == 7000
    assert fila.semanas_desde_ultimo.item() == pytest.approx(4)


def test_pliegues_crecen_en_el_tiempo_sin_mezclar_fechas():
    dataset = construir_dataset(serie_precios(_sinteticos(60)), horizontes=(1, 2))

    pliegues = particiones_temporales(dataset, n_pliegues=4, fechas_prueba=5)

    assert len(pliegues) == 4
    anterior_entrenamiento = 0
    for entrenamiento, prueba in pliegues:
        fechas_e = dataset.loc[entrenamiento, "fecha"]
        fechas_p = dataset.loc[prueba, "fecha"]
        assert fechas_e.max() < fechas_p.min()
        assert set(fechas_e).isdisjoint(set(fechas_p))
        assert fechas_p.nunique() == 5
        # todas las categorías y horizontes de una fecha de prueba quedan del mismo lado
        assert len(prueba) == len(dataset[dataset.fecha.isin(set(fechas_p))])
        assert len(entrenamiento) > anterior_entrenamiento
        anterior_entrenamiento = len(entrenamiento)
    assert dataset.loc[pliegues[-1][1], "fecha"].max() == dataset.fecha.max()


def test_pocas_fechas_lanzan_datos_insuficientes():
    dataset = construir_dataset(serie_precios(_sinteticos(8)), horizontes=(1,))

    with pytest.raises(DatosInsuficientes, match="fechas"):
        particiones_temporales(dataset, n_pliegues=3, fechas_prueba=4, minimo_entrenamiento=10)


class _Constante:
    def __init__(self, valor: float) -> None:
        self.valor = valor

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        return np.full(len(x), self.valor)


def test_validar_compara_contra_la_linea_base_ingenua():
    dataset = construir_dataset(serie_precios(_sinteticos(60)), horizontes=(1, 4))
    oraculo = dataset.set_index(["fecha", "categoria", "horizonte"]).objetivo

    class _Oraculo:
        def predict(self, x: pd.DataFrame) -> np.ndarray:
            return oraculo.loc[list(zip(x.fecha, x.categoria, x.horizonte, strict=True))].values

    ingenuo = validar(dataset, lambda x, y: _Constante(0.0), n_pliegues=3, fechas_prueba=4)
    perfecto = validar(dataset, lambda x, y: _Oraculo(), n_pliegues=3, fechas_prueba=4)

    assert ingenuo.mae == pytest.approx(ingenuo.mae_ingenuo)
    assert ingenuo.mejora == pytest.approx(0.0)
    assert perfecto.mae == pytest.approx(0.0)
    assert perfecto.mejora == pytest.approx(1.0)
    assert sorted(perfecto.por_horizonte) == [1, 4]
    assert perfecto.pliegues == 3
    assert perfecto.fechas_evaluadas == 12


def test_confianza_honesta_con_historico_corto_o_sin_mejora():
    dataset = construir_dataset(serie_precios(_sinteticos(60)), horizontes=(1,))
    ingenuo = validar(dataset, lambda x, y: _Constante(0.0), n_pliegues=3, fechas_prueba=4)

    sin_mejora = evaluar_confianza(ingenuo, fechas_historico=60)
    corto = evaluar_confianza(ingenuo.model_copy(update={"mejora": 0.3}), fechas_historico=20)
    medio = evaluar_confianza(ingenuo.model_copy(update={"mejora": 0.3}), fechas_historico=40)
    alto = evaluar_confianza(ingenuo.model_copy(update={"mejora": 0.3}), fechas_historico=80)

    assert sin_mejora.nivel == "baja"
    assert any("línea base" in m for m in sin_mejora.motivos)
    assert corto.nivel == "baja"
    assert any("20 fechas" in m for m in corto.motivos)
    assert medio.nivel == "media"
    assert alto.nivel == "alta"
