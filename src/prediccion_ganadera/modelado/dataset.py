"""Conjunto de entrenamiento multi-horizonte y validación temporal.

Cada fila responde: «conociendo las subastas hasta la fecha de referencia, ¿cuál será el
precio promedio por kilo de la categoría c dentro de h subastas?». Las variables salen
solo de subastas con fecha ≤ referencia (la de la fila objetivo menos h posiciones en el
calendario de subastas observadas), así que nada del futuro entra al modelo.

El objetivo es la variación relativa frente al último precio conocido de la categoría,
no el precio: los árboles no extrapolan niveles, y así la línea base ingenua («el precio
no cambia») es predecir 0. La validación usa ventanas crecientes por fecha: todas las
categorías y horizontes de una fecha caen del mismo lado y nunca se barajan fechas.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Literal, Protocol

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict

from prediccion_ganadera.modelado.variables import PrecioCategoria

HORIZONTES = (1, 2, 3, 4)
COLUMNAS_VARIABLES = [
    "categoria",
    "horizonte",
    "ultimo_precio",
    "media_4",
    "tendencia",
    "animales_ultimo",
    "peso_ultimo",
    "oferta_referencia",
    "semanas_desde_ultimo",
    "mes",
    "semana",
]
# Umbrales de confianza (heurísticos, documentados en el README): medio año y un año de
# subastas semanales, y una mejora mínima sobre la línea base.
FECHAS_MINIMAS = 26
FECHAS_ANIO = 52
MEJORA_MINIMA = 0.10
PLIEGUES_MINIMOS = 3


class DatosInsuficientes(ValueError):
    """No hay suficientes fechas para validar el modelo sin mezclar el tiempo."""


class Predictor(Protocol):
    def predict(self, x: pd.DataFrame) -> np.ndarray: ...


Entrenador = Callable[[pd.DataFrame, pd.Series], Predictor]


def serie_precios(
    precios: Iterable[PrecioCategoria], *, incluir_especiales: bool = False
) -> pd.DataFrame:
    """Precios por kilo (fecha, categoría); los rematados por cabeza quedan fuera."""
    filas = [
        {
            "fecha": pd.Timestamp(p.fecha),
            "categoria": p.categoria,
            "precio": p.precio_promedio,
            "animales": p.animales,
            "peso_cabeza": p.peso_cabeza,
        }
        for p in precios
        if p.unidad == "kg" and (incluir_especiales or not p.especial)
    ]
    columnas = ["fecha", "categoria", "precio", "animales", "peso_cabeza"]
    serie = pd.DataFrame(filas, columns=columnas)
    return serie.sort_values(["fecha", "categoria"]).reset_index(drop=True)


def _variables(
    historia: pd.DataFrame,
    *,
    categoria: str,
    referencia: pd.Timestamp,
    fecha: pd.Timestamp,
    horizonte: int,
    oferta: int,
) -> dict:
    """Variables de una fila a partir de la historia de la categoría hasta `referencia`."""
    ultimo = historia.iloc[-1]
    media_4 = historia.precio.iloc[-4:].mean()
    return {
        "fecha": fecha,
        "fecha_referencia": referencia,
        "categoria": categoria,
        "horizonte": horizonte,
        "ultimo_precio": float(ultimo.precio),
        "media_4": float(media_4),
        "tendencia": float(ultimo.precio / media_4 - 1),
        "animales_ultimo": int(ultimo.animales),
        "peso_ultimo": int(ultimo.peso_cabeza),
        "oferta_referencia": oferta,
        "semanas_desde_ultimo": (fecha - ultimo.fecha).days / 7,
        "mes": fecha.month,
        "semana": int(fecha.isocalendar().week),
    }


def _ordenar(filas: list[dict], serie: pd.DataFrame) -> pd.DataFrame:
    tabla = pd.DataFrame(filas)
    if tabla.empty:
        return tabla
    tabla["categoria"] = pd.Categorical(
        tabla.categoria, categories=sorted(serie.categoria.unique())
    )
    return tabla.sort_values(["fecha", "categoria", "horizonte"]).reset_index(drop=True)


def construir_dataset(
    serie: pd.DataFrame, *, horizontes: Sequence[int] = HORIZONTES
) -> pd.DataFrame:
    fechas = sorted(serie.fecha.unique())
    posicion = {f: i for i, f in enumerate(fechas)}
    oferta = serie.groupby("fecha").animales.sum()
    filas = []
    for categoria, grupo in serie.groupby("categoria", sort=True):
        grupo = grupo.sort_values("fecha").reset_index(drop=True)
        for objetivo in grupo.itertuples():
            for h in horizontes:
                indice = posicion[objetivo.fecha] - h
                if indice < 0:
                    continue
                referencia = fechas[indice]
                historia = grupo[grupo.fecha <= referencia]
                if historia.empty:
                    continue
                fila = _variables(
                    historia,
                    categoria=categoria,
                    referencia=referencia,
                    fecha=objetivo.fecha,
                    horizonte=h,
                    oferta=int(oferta[referencia]),
                )
                fila["precio"] = float(objetivo.precio)
                fila["objetivo"] = float(objetivo.precio / fila["ultimo_precio"] - 1)
                filas.append(fila)
    return _ordenar(filas, serie)


def filas_prediccion(
    serie: pd.DataFrame, *, horizontes: Sequence[int] = HORIZONTES
) -> pd.DataFrame:
    """Variables para los próximos viernes de subasta, con la última subasta como
    referencia; el horizonte h es el h-ésimo viernes después de ella."""
    referencia = serie.fecha.max()
    primer_viernes = referencia + pd.Timedelta(days=(4 - referencia.dayofweek - 1) % 7 + 1)
    oferta = int(serie[serie.fecha == referencia].animales.sum())
    filas = [
        _variables(
            grupo.sort_values("fecha"),
            categoria=categoria,
            referencia=referencia,
            fecha=primer_viernes + pd.Timedelta(weeks=h - 1),
            horizonte=h,
            oferta=oferta,
        )
        for categoria, grupo in serie.groupby("categoria", sort=True)
        for h in horizontes
    ]
    return _ordenar(filas, serie)


def particiones_temporales(
    dataset: pd.DataFrame,
    *,
    n_pliegues: int = 5,
    fechas_prueba: int = 4,
    minimo_entrenamiento: int = 12,
) -> list[tuple[pd.Index, pd.Index]]:
    """Ventanas crecientes: cada pliegue prueba `fechas_prueba` fechas objetivo seguidas y
    entrena con todas las filas de fechas anteriores. El último pliegue termina en la
    fecha más reciente."""
    fechas = sorted(dataset.fecha.unique()) if not dataset.empty else []
    necesarias = minimo_entrenamiento + n_pliegues * fechas_prueba
    if len(fechas) < necesarias:
        raise DatosInsuficientes(
            f"se necesitan al menos {necesarias} fechas con objetivo para {n_pliegues} "
            f"pliegues de {fechas_prueba} fechas; hay {len(fechas)}"
        )
    pliegues = []
    for k in range(n_pliegues, 0, -1):
        inicio = len(fechas) - k * fechas_prueba
        prueba = set(fechas[inicio : inicio + fechas_prueba])
        pliegues.append(
            (
                dataset.index[dataset.fecha < fechas[inicio]],
                dataset.index[dataset.fecha.isin(prueba)],
            )
        )
    return pliegues


class MetricasHorizonte(BaseModel):
    model_config = ConfigDict(frozen=True)

    mae: float  # pesos por kilo
    mae_ingenuo: float
    mejora: float  # 1 - mae / mae_ingenuo


class MetricasValidacion(BaseModel):
    model_config = ConfigDict(frozen=True)

    mae: float
    mae_ingenuo: float
    mape: float  # fracción, no porcentaje
    mape_ingenuo: float
    mejora: float
    por_horizonte: dict[int, MetricasHorizonte]
    pliegues: int
    fechas_evaluadas: int
    filas_evaluadas: int


def _mejora(mae: float, mae_ingenuo: float) -> float:
    return 0.0 if mae_ingenuo == 0 else 1 - mae / mae_ingenuo


def entradas(dataset: pd.DataFrame) -> pd.DataFrame:
    """Lo que ve un predictor: todo menos el precio y el objetivo de la fila."""
    return dataset.drop(columns=["precio", "objetivo"])


def validar(
    dataset: pd.DataFrame,
    entrenar: Entrenador,
    *,
    n_pliegues: int = 5,
    fechas_prueba: int = 4,
    minimo_entrenamiento: int = 12,
) -> MetricasValidacion:
    partes = []
    for entrenamiento, prueba in particiones_temporales(
        dataset,
        n_pliegues=n_pliegues,
        fechas_prueba=fechas_prueba,
        minimo_entrenamiento=minimo_entrenamiento,
    ):
        modelo = entrenar(
            entradas(dataset.loc[entrenamiento]), dataset.loc[entrenamiento, "objetivo"]
        )
        bloque = dataset.loc[prueba, ["fecha", "horizonte", "ultimo_precio", "precio"]].copy()
        variacion = np.asarray(modelo.predict(entradas(dataset.loc[prueba])), dtype=float)
        bloque["predicho"] = bloque.ultimo_precio * (1 + variacion)
        partes.append(bloque)
    evaluado = pd.concat(partes)
    error = (evaluado.predicho - evaluado.precio).abs()
    error_ingenuo = (evaluado.ultimo_precio - evaluado.precio).abs()
    por_horizonte = {}
    for h, indices in evaluado.groupby("horizonte").groups.items():
        mae_h, ingenuo_h = float(error[indices].mean()), float(error_ingenuo[indices].mean())
        por_horizonte[int(h)] = MetricasHorizonte(
            mae=mae_h, mae_ingenuo=ingenuo_h, mejora=_mejora(mae_h, ingenuo_h)
        )
    mae, mae_ingenuo = float(error.mean()), float(error_ingenuo.mean())
    return MetricasValidacion(
        mae=mae,
        mae_ingenuo=mae_ingenuo,
        mape=float((error / evaluado.precio).mean()),
        mape_ingenuo=float((error_ingenuo / evaluado.precio).mean()),
        mejora=_mejora(mae, mae_ingenuo),
        por_horizonte=por_horizonte,
        pliegues=len(partes),
        fechas_evaluadas=int(evaluado.fecha.nunique()),
        filas_evaluadas=len(evaluado),
    )


class Confianza(BaseModel):
    model_config = ConfigDict(frozen=True)

    nivel: Literal["alta", "media", "baja"]
    motivos: list[str]


def evaluar_confianza(metricas: MetricasValidacion, *, fechas_historico: int) -> Confianza:
    """Nivel de confianza honesto (U2): el histórico es corto y semanal desde 2025."""
    bajas, medias = [], []
    if fechas_historico < FECHAS_MINIMAS:
        bajas.append(
            f"histórico corto: {fechas_historico} fechas (< {FECHAS_MINIMAS}, medio año de "
            "subastas semanales)"
        )
    if metricas.pliegues < PLIEGUES_MINIMOS:
        bajas.append(f"solo {metricas.pliegues} pliegues de validación")
    if metricas.mejora <= 0:
        bajas.append("el modelo no supera a la línea base ingenua (último precio conocido)")
    if fechas_historico < FECHAS_ANIO:
        medias.append(
            f"menos de un año de histórico ({fechas_historico} fechas): la estacionalidad "
            "anual no se puede aprender"
        )
    if 0 < metricas.mejora < MEJORA_MINIMA:
        medias.append(f"mejora sobre la línea base menor al {MEJORA_MINIMA:.0%}")
    contexto = (
        f"validado en {metricas.pliegues} pliegues y {metricas.fechas_evaluadas} fechas; "
        f"mejora sobre la línea base: {metricas.mejora:.1%}"
    )
    if bajas:
        return Confianza(nivel="baja", motivos=[*bajas, *medias, contexto])
    if medias:
        return Confianza(nivel="media", motivos=[*medias, contexto])
    return Confianza(nivel="alta", motivos=[contexto])
