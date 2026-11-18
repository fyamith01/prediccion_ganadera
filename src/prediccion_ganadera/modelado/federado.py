"""Aprendizaje federado simulado: XGBoost federado con Flower frente al modelo central.

Cada cliente guarda sus filas del conjunto multi-horizonte y nunca las comparte: en cada
ronda recibe el modelo global, le agrega árboles entrenados solo con sus datos y devuelve
únicamente esos árboles nuevos. El servidor los une con la estrategia de *bagging* de
Flower (`FedXgbBagging.aggregate_fit`): el modelo global suma los árboles de todos los
clientes, así que la tasa de aprendizaje de cada cliente es la central dividida entre el
número de clientes y la predicción base es 0 (el objetivo ya es la variación frente al
último precio).

La simulación corre en el mismo proceso con el cliente y la estrategia de Flower, sin
Ray ni red (`flwr.simulation` necesita Ray). Ambos modelos se miden con la misma
validación temporal (`dataset.validar`), así que la comparación es justa.

Todos los datos vienen de una sola casa de subastas: cualquier reparto es simulado. Un
reparto se declara no viable, con el motivo y sin métricas, si deja un solo cliente o
algún cliente con muy pocas fechas (ver `docs/aprendizaje_federado.md`).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Literal, cast

import pandas as pd
import xgboost as xgb
from flwr.client import Client
from flwr.common import Code, FitIns, FitRes, Parameters, Status
from flwr.server.client_proxy import ClientProxy
from flwr.server.strategy import FedXgbBagging
from pydantic import BaseModel, ConfigDict

from prediccion_ganadera.modelado.dataset import (
    MetricasValidacion,
    construir_dataset,
    serie_precios,
    validar,
)
from prediccion_ganadera.modelado.entrenamiento import (
    PARAMETROS_XGB,
    ModeloXGB,
    OpcionesEntrenamiento,
    matriz_xgb,
)
from prediccion_ganadera.modelado.variables import PrecioCategoria

Reparto = Literal["anio", "seccion", "tipo_subasta"]
# Rondas federadas por defecto. Flower agrega un solo árbol por respuesta de cliente
# (`aggregate` copia `num_parallel_tree` árboles), así que cada ronda agrega un árbol por
# cliente. Como el bagging vuelve a leer el modelo global completo en cada agregación, el
# costo crece con el cuadrado de las rondas (300 rondas: ~4 min por reparto, 60: ~5 s):
# se usan menos rondas con una tasa de aprendizaje proporcionalmente mayor.
RONDAS_FEDERADAS = 60
REPARTOS: tuple[Reparto, ...] = ("anio", "seccion", "tipo_subasta")


def asignar_clientes(
    dataset: pd.DataFrame, reparto: Reparto, *, precios: Iterable[PrecioCategoria]
) -> pd.Series:
    """Cliente dueño de cada fila: el año de la fecha objetivo, la sección (hembra/macho)
    de la categoría o el tipo de subasta (ordinaria/especial) de la fecha objetivo."""
    if reparto == "anio":
        etiquetas = dataset.fecha.dt.year.astype(str)
    elif reparto == "seccion":
        secciones = {p.categoria: p.seccion or "sin_seccion" for p in precios}
        etiquetas = dataset.categoria.astype(str).map(secciones).fillna("sin_seccion")
    elif reparto == "tipo_subasta":
        especiales = {pd.Timestamp(p.fecha) for p in precios if p.especial}
        etiquetas = dataset.fecha.isin(especiales).map({True: "especial", False: "ordinaria"})
    else:
        raise ValueError(f"reparto desconocido: {reparto!r}")
    return pd.Series(etiquetas.to_numpy(), index=dataset.index, name="cliente", dtype=str)


class DiagnosticoReparto(BaseModel):
    model_config = ConfigDict(frozen=True)

    viable: bool
    motivos: list[str]
    fechas_por_cliente: dict[str, int]  # fechas objetivo distintas por cliente
    filas_por_cliente: dict[str, int]


def diagnosticar_reparto(
    dataset: pd.DataFrame, clientes: pd.Series, *, minimo_fechas: int
) -> DiagnosticoReparto:
    fechas = dataset.groupby(clientes, sort=True).fecha.nunique()
    filas = clientes.value_counts().sort_index()
    motivos = []
    if len(fechas) < 2:
        motivos.append(
            "un solo cliente: no hay nada que federar (el resultado sería el modelo central)"
        )
    for cliente, n in fechas.items():
        if n < minimo_fechas:
            motivos.append(
                f"el cliente «{cliente}» tiene {n} fecha(s) objetivo (< {minimo_fechas}): "
                "no alcanza para entrenar árboles con sentido ni para validarlo en el tiempo"
            )
    return DiagnosticoReparto(
        viable=not motivos,
        motivos=motivos,
        fechas_por_cliente={str(k): int(v) for k, v in fechas.items()},
        filas_por_cliente={str(k): int(v) for k, v in filas.items()},
    )


class ClienteXGB(Client):
    """Cliente Flower: agrega un árbol al modelo global con sus filas y devuelve solo ese
    árbol (ni filas ni gradientes salen del cliente)."""

    def __init__(
        self,
        x: pd.DataFrame,
        y: pd.Series,
        categorias: list[str],
        parametros: dict[str, Any],
    ) -> None:
        self.matriz = matriz_xgb(x, categorias, y)
        self.parametros = parametros

    def fit(self, ins: FitIns) -> FitRes:
        previo = None
        if ins.parameters.tensors:
            previo = xgb.Booster(params=self.parametros)
            previo.load_model(bytearray(ins.parameters.tensors[0]))
        modelo = xgb.train(self.parametros, self.matriz, num_boost_round=1, xgb_model=previo)
        total = modelo.num_boosted_rounds()
        nuevos = modelo[total - 1 : total]
        return FitRes(
            status=Status(code=Code.OK, message=""),
            parameters=Parameters(tensors=[bytes(nuevos.save_raw("json"))], tensor_type=""),
            num_examples=self.matriz.num_row(),
            metrics={},
        )


def parametros_cliente(
    parametros: dict[str, Any], *, n_clientes: int, rondas: int
) -> dict[str, Any]:
    """Parámetros XGBoost de cada cliente. El aprendizaje total (`eta` × rondas) del modelo
    central se reparte entre las rondas federadas, y el bagging suma los árboles de todos
    los clientes, así que cada uno aprende `eta × rondas_centrales / rondas / n_clientes`."""
    locales = {**parametros, "base_score": 0.0}
    centrales = int(locales.pop("num_boost_round", rondas))
    locales["eta"] = locales.get("eta", 0.3) * centrales / rondas / n_clientes
    return locales


def entrenar_federado(
    x: pd.DataFrame,
    y: pd.Series,
    clientes: pd.Series,
    categorias: list[str],
    parametros: dict[str, Any],
    *,
    rondas: int,
) -> ModeloXGB:
    """Rondas de Flower en el mismo proceso: cada cliente entrena con sus filas y la
    estrategia `FedXgbBagging` agrega sus árboles al modelo global."""
    etiquetas = clientes.loc[x.index]
    nombres = sorted(etiquetas.unique())
    locales = parametros_cliente(parametros, n_clientes=len(nombres), rondas=rondas)
    participantes = [
        ClienteXGB(x[etiquetas == n], y[etiquetas == n], categorias, locales) for n in nombres
    ]
    estrategia = FedXgbBagging()
    global_ = Parameters(tensors=[], tensor_type="")
    for ronda in range(1, rondas + 1):
        respuestas = [
            (cast(ClientProxy, None), c.fit(FitIns(global_, {"ronda": ronda})))
            for c in participantes
        ]
        agregado, _ = estrategia.aggregate_fit(ronda, respuestas, [])
        if agregado is None:
            raise RuntimeError(f"la ronda {ronda} no produjo un modelo global")
        global_ = agregado
    modelo = ModeloXGB(categorias, locales)
    modelo.booster = xgb.Booster(params=locales)
    modelo.booster.load_model(bytearray(global_.tensors[0]))
    return modelo


class ComparacionFederada(BaseModel):
    model_config = ConfigDict(frozen=True)

    reparto: Reparto
    viable: bool
    motivos: list[str]
    fechas_por_cliente: dict[str, int]
    filas_por_cliente: dict[str, int]
    rondas: int
    central: MetricasValidacion | None
    federado: MetricasValidacion | None
    clientes_por_pliegue: list[int]  # clientes con filas de entrenamiento en cada pliegue
    diferencia_mae: float | None  # federado − central, pesos por kilo (> 0: peor)


def comparar_federado(
    precios: Iterable[PrecioCategoria],
    reparto: Reparto,
    opciones: OpcionesEntrenamiento,
    *,
    rondas: int = RONDAS_FEDERADAS,
) -> ComparacionFederada:
    """Modelo central vs federado con los mismos pliegues; `DatosInsuficientes` si el
    histórico no alcanza para validar."""
    precios = list(precios)
    especiales = opciones.incluir_especiales or reparto == "tipo_subasta"
    serie = serie_precios(precios, incluir_especiales=especiales)
    dataset = construir_dataset(serie, horizontes=opciones.horizontes)
    categorias = sorted(serie.categoria.unique())
    parametros = {**PARAMETROS_XGB, **opciones.parametros}
    clientes = asignar_clientes(dataset, reparto, precios=precios)
    diagnostico = diagnosticar_reparto(
        dataset, clientes, minimo_fechas=opciones.minimo_entrenamiento
    )
    base = {
        "reparto": reparto,
        "motivos": diagnostico.motivos,
        "fechas_por_cliente": diagnostico.fechas_por_cliente,
        "filas_por_cliente": diagnostico.filas_por_cliente,
        "rondas": rondas,
    }
    if not diagnostico.viable:
        return ComparacionFederada(
            **base,
            viable=False,
            central=None,
            federado=None,
            clientes_por_pliegue=[],
            diferencia_mae=None,
        )
    pliegues = {
        "n_pliegues": opciones.n_pliegues,
        "fechas_prueba": opciones.fechas_prueba,
        "minimo_entrenamiento": opciones.minimo_entrenamiento,
    }
    central = validar(
        dataset,
        lambda x, y: ModeloXGB(categorias, parametros).entrenar(x, y),
        **pliegues,
    )
    clientes_por_pliegue: list[int] = []

    def entrenar(x: pd.DataFrame, y: pd.Series) -> ModeloXGB:
        clientes_por_pliegue.append(int(clientes.loc[x.index].nunique()))
        return entrenar_federado(x, y, clientes, categorias, parametros, rondas=rondas)

    federado = validar(dataset, entrenar, **pliegues)
    return ComparacionFederada(
        **base,
        viable=True,
        central=central,
        federado=federado,
        clientes_por_pliegue=clientes_por_pliegue,
        diferencia_mae=federado.mae - central.mae,
    )
