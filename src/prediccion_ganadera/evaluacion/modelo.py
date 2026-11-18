"""Métricas del modelo predictivo: por versión y frente a lo que pasó después.

Dos miradas complementarias:

- validación (al entrenar): el error de la validación temporal guardado en
  `data/modelos/<version>/metadatos.json` (MAE y MAPE contra el ingenuo «el precio será
  el último conocido», por horizonte) y el nivel de confianza; se compara cada versión
  con la anterior para ver si reentrenar mejoró o empeoró;
- seguimiento (después de entrenar): las predicciones guardadas en `oro.predicciones`
  se cruzan con los precios que se observaron luego en las subastas. Es el error real
  fuera de muestra; solo existe para las fechas que ya pasaron y tienen datos validados.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, computed_field

from prediccion_ganadera.adaptadores.almacen.modelos import RegistroModelos
from prediccion_ganadera.adaptadores.almacen.oro import AlmacenOro
from prediccion_ganadera.modelado.variables import PrecioCategoria


class SeguimientoPredicciones(BaseModel):
    model_config = ConfigDict(frozen=True)

    predicciones: int  # filas de la versión en oro
    comparadas: int  # con un precio observado para esa fecha y categoría
    mae: float | None  # pesos por kilo
    mae_ingenuo: float | None
    dentro_del_error: float | None  # fracción con |error| <= error esperado

    @computed_field
    @property
    def mejora(self) -> float | None:
        if self.mae is None or not self.mae_ingenuo:
            return None
        return 1 - self.mae / self.mae_ingenuo


class MetricasVersion(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str
    creado_en: str
    fecha_desde: date
    fecha_hasta: date
    fechas_historico: int
    mae: float
    mae_ingenuo: float
    mape: float
    mape_ingenuo: float
    mejora: float
    mae_por_horizonte: dict[int, float]
    mejora_por_horizonte: dict[int, float]
    pliegues: int
    confianza: str
    motivos_confianza: list[str]
    cambio_mae: float | None  # frente a la versión anterior (negativo = mejor)
    cambio_mejora: float | None
    seguimiento: SeguimientoPredicciones | None


class EvaluacionModelos(BaseModel):
    model_config = ConfigDict(frozen=True)

    versiones: list[MetricasVersion]
    sin_metadatos: list[str]  # versiones del índice cuya carpeta ya no existe


def _seguimiento(
    oro: AlmacenOro, version: str, reales: dict[tuple[date, str], int]
) -> SeguimientoPredicciones:
    predicciones = oro.leer_predicciones(version)
    pares = [
        (p, reales[(p.fecha, p.categoria)])
        for p in predicciones
        if (p.fecha, p.categoria) in reales
    ]
    if not pares:
        return SeguimientoPredicciones(
            predicciones=len(predicciones),
            comparadas=0,
            mae=None,
            mae_ingenuo=None,
            dentro_del_error=None,
        )
    n = len(pares)
    return SeguimientoPredicciones(
        predicciones=len(predicciones),
        comparadas=n,
        mae=sum(abs(p.precio_predicho - real) for p, real in pares) / n,
        mae_ingenuo=sum(abs(p.ultimo_precio - real) for p, real in pares) / n,
        dentro_del_error=sum(abs(p.precio_predicho - real) <= p.error_esperado for p, real in pares)
        / n,
    )


def evaluar_modelos(
    datos: Path, *, observados: Iterable[PrecioCategoria] | None
) -> EvaluacionModelos:
    """`observados`: precios validados para el seguimiento (None = sin seguimiento)."""
    registro = RegistroModelos(datos / "modelos")
    oro = AlmacenOro(datos / "oro")
    reales = (
        {(p.fecha, p.categoria): p.precio_promedio for p in observados if p.unidad == "kg"}
        if observados is not None
        else None
    )
    versiones: list[MetricasVersion] = []
    sin_metadatos: list[str] = []
    anterior: MetricasVersion | None = None
    for entrada in registro.listar():
        ruta = registro.raiz / entrada["version"] / "metadatos.json"
        if not ruta.is_file():
            sin_metadatos.append(entrada["version"])
            continue
        meta = json.loads(ruta.read_text(encoding="utf-8"))
        metricas = meta["metricas"]
        horizontes = metricas["por_horizonte"]
        actual = MetricasVersion(
            version=meta["version"],
            creado_en=meta["creado_en"],
            fecha_desde=meta["fechas"]["desde"],
            fecha_hasta=meta["fechas"]["hasta"],
            fechas_historico=meta["fechas"]["cantidad"],
            mae=metricas["mae"],
            mae_ingenuo=metricas["mae_ingenuo"],
            mape=metricas["mape"],
            mape_ingenuo=metricas["mape_ingenuo"],
            mejora=metricas["mejora"],
            mae_por_horizonte={int(h): m["mae"] for h, m in horizontes.items()},
            mejora_por_horizonte={int(h): m["mejora"] for h, m in horizontes.items()},
            pliegues=metricas["pliegues"],
            confianza=meta["confianza"]["nivel"],
            motivos_confianza=meta["confianza"]["motivos"],
            cambio_mae=metricas["mae"] - anterior.mae if anterior else None,
            cambio_mejora=metricas["mejora"] - anterior.mejora if anterior else None,
            seguimiento=_seguimiento(oro, meta["version"], reales) if reales is not None else None,
        )
        versiones.append(actual)
        anterior = actual
    return EvaluacionModelos(versiones=versiones, sin_metadatos=sin_metadatos)
