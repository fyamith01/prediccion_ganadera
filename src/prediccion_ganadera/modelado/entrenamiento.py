"""Entrenamiento XGBoost y ranking de próximas fechas × categorías.

Flujo: precios → serie por kilo → conjunto multi-horizonte → validación temporal (que
mide el error esperado por horizonte y la confianza) → modelo final con todo el
histórico → predicción de los próximos viernes de subasta. «Mejor día para vender» es,
por categoría, la fecha con mayor precio predicho; «qué ganado» ordena las categorías
por la variación esperada frente a su último precio. Si la diferencia entre fechas no
supera el error de validación, la recomendación lo dice (`distinguible = False`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xgboost as xgb
from pydantic import BaseModel, ConfigDict, Field

from prediccion_ganadera.modelado.dataset import (
    COLUMNAS_VARIABLES,
    HORIZONTES,
    Confianza,
    MetricasValidacion,
    construir_dataset,
    evaluar_confianza,
    filas_prediccion,
    serie_precios,
    validar,
)
from prediccion_ganadera.modelado.variables import PrecioCategoria

# Árboles poco profundos y muchos pasos pequeños: el histórico es corto (U2).
PARAMETROS_XGB: dict[str, Any] = {
    "num_boost_round": 300,
    "max_depth": 3,
    "eta": 0.05,
    "subsample": 0.9,
    "colsample_bytree": 0.9,
    "tree_method": "hist",
    "objective": "reg:squarederror",
    "seed": 0,
    "nthread": 1,  # resultados reproducibles
}


class OpcionesEntrenamiento(BaseModel):
    model_config = ConfigDict(frozen=True)

    horizontes: tuple[int, ...] = HORIZONTES
    n_pliegues: int = 5
    fechas_prueba: int = 4
    minimo_entrenamiento: int = 12
    incluir_especiales: bool = False
    parametros: dict[str, Any] = Field(default_factory=dict)  # sobre PARAMETROS_XGB


def matriz_xgb(x: pd.DataFrame, categorias: list[str], y: pd.Series | None = None) -> xgb.DMatrix:
    """Variables del modelo con las categorías fijadas, para que sus códigos coincidan
    entre entrenamiento y predicción (y entre clientes, en el modo federado)."""
    matriz = x[COLUMNAS_VARIABLES].copy()
    matriz["categoria"] = pd.Categorical(matriz.categoria.astype(str), categories=categorias)
    return xgb.DMatrix(matriz, label=y, enable_categorical=True)


class ModeloXGB:
    """Booster XGBoost (API nativa) que fija las categorías conocidas al entrenar y al
    predecir, para que los códigos de categoría coincidan entre ambos momentos."""

    def __init__(self, categorias: list[str], parametros: dict[str, Any]) -> None:
        self.categorias = categorias
        self.parametros = parametros
        self.booster: xgb.Booster | None = None

    def _matriz(self, x: pd.DataFrame, y: pd.Series | None = None) -> xgb.DMatrix:
        return matriz_xgb(x, self.categorias, y)

    def entrenar(self, x: pd.DataFrame, y: pd.Series) -> ModeloXGB:
        parametros = dict(self.parametros)
        rondas = parametros.pop("num_boost_round")
        self.booster = xgb.train(parametros, self._matriz(x, y), num_boost_round=rondas)
        return self

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        if self.booster is None:
            raise RuntimeError("el modelo no está entrenado")
        return self.booster.predict(self._matriz(x))

    def guardar(self, ruta: Path) -> None:
        if self.booster is None:
            raise RuntimeError("el modelo no está entrenado")
        self.booster.save_model(ruta)

    @classmethod
    def cargar(cls, ruta: Path, categorias: list[str], parametros: dict[str, Any]) -> ModeloXGB:
        modelo = cls(categorias, parametros)
        modelo.booster = xgb.Booster(model_file=str(ruta))
        return modelo


@dataclass(frozen=True)
class ResultadoEntrenamiento:
    modelo: ModeloXGB
    opciones: OpcionesEntrenamiento
    metricas: MetricasValidacion
    confianza: Confianza
    ranking: pd.DataFrame
    categorias: list[str]
    fechas_historico: int
    fecha_desde: date
    fecha_hasta: date
    filas_entrenamiento: int
    huella_datos: str  # SHA-256 de la serie usada, para rastrear con qué datos se entrenó
    entrenado_en: datetime


class RecomendacionCategoria(BaseModel):
    model_config = ConfigDict(frozen=True)

    categoria: str
    mejor_fecha: date
    horizonte: int
    ultimo_precio: float
    precio_predicho: float
    variacion_esperada: float
    error_esperado: float
    distinguible: bool  # la diferencia entre fechas supera el error esperado


def entrenar_modelo(
    precios: Iterable[PrecioCategoria], opciones: OpcionesEntrenamiento, *, ahora: datetime
) -> ResultadoEntrenamiento:
    """Valida, entrena y predice; `DatosInsuficientes` si el histórico no alcanza."""
    serie = serie_precios(precios, incluir_especiales=opciones.incluir_especiales)
    dataset = construir_dataset(serie, horizontes=opciones.horizontes)
    categorias = sorted(serie.categoria.unique())
    parametros = {**PARAMETROS_XGB, **opciones.parametros}

    def entrenar(x: pd.DataFrame, y: pd.Series) -> ModeloXGB:
        return ModeloXGB(categorias, parametros).entrenar(x, y)

    metricas = validar(
        dataset,
        entrenar,
        n_pliegues=opciones.n_pliegues,
        fechas_prueba=opciones.fechas_prueba,
        minimo_entrenamiento=opciones.minimo_entrenamiento,
    )
    fechas_historico = int(serie.fecha.nunique())
    modelo = entrenar(dataset, dataset.objetivo)
    futuras = filas_prediccion(serie, horizontes=opciones.horizontes)
    variacion = modelo.predict(futuras).astype(float)
    ranking = pd.DataFrame(
        {
            "fecha": futuras.fecha,
            "fecha_referencia": futuras.fecha_referencia,
            "categoria": futuras.categoria.astype(str),
            "horizonte": futuras.horizonte.astype(int),
            "ultimo_precio": futuras.ultimo_precio,
            "precio_predicho": futuras.ultimo_precio * (1 + variacion),
            "variacion_esperada": variacion,
            "error_esperado": [metricas.por_horizonte[int(h)].mae for h in futuras.horizonte],
        }
    )
    huella = hashlib.sha256(serie.to_csv(index=False).encode("utf-8")).hexdigest()
    return ResultadoEntrenamiento(
        modelo=modelo,
        opciones=opciones,
        metricas=metricas,
        confianza=evaluar_confianza(metricas, fechas_historico=fechas_historico),
        ranking=ranking,
        categorias=categorias,
        fechas_historico=fechas_historico,
        fecha_desde=serie.fecha.min().date(),
        fecha_hasta=serie.fecha.max().date(),
        filas_entrenamiento=len(dataset),
        huella_datos=huella,
        entrenado_en=ahora,
    )


def recomendaciones(ranking: pd.DataFrame) -> list[RecomendacionCategoria]:
    """La mejor fecha por categoría, de mayor a menor variación esperada."""
    resultado = []
    for categoria, grupo in ranking.groupby("categoria", sort=True):
        mejor = grupo.loc[grupo.precio_predicho.idxmax()]
        amplitud = grupo.precio_predicho.max() - grupo.precio_predicho.min()
        resultado.append(
            RecomendacionCategoria(
                categoria=str(categoria),
                mejor_fecha=pd.Timestamp(mejor.fecha).date(),
                horizonte=int(mejor.horizonte),
                ultimo_precio=float(mejor.ultimo_precio),
                precio_predicho=float(mejor.precio_predicho),
                variacion_esperada=float(mejor.variacion_esperada),
                error_esperado=float(mejor.error_esperado),
                distinguible=bool(amplitud > grupo.error_esperado.mean()),
            )
        )
    return sorted(resultado, key=lambda r: r.variacion_esperada, reverse=True)
