"""Modelador: lee los precios, entrena, registra el modelo y publica las predicciones.

Lo usan el comando `entrenar` y el paso `modelar` del orquestador. Por defecto solo
entran tablas validadas (`verificada`/`corregida`); con `incluir_no_verificadas` también
las `pendiente` (nunca las `en_revision`, que se sabe que están mal) y la confianza lo
advierte. Si no hay histórico suficiente lanza `DatosInsuficientes` con el porqué.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Literal

import psycopg
from pydantic import BaseModel, ConfigDict

from prediccion_ganadera.adaptadores.almacen.modelos import RegistroModelos
from prediccion_ganadera.adaptadores.almacen.oro import AlmacenOro
from prediccion_ganadera.adaptadores.almacen.plata import AlmacenPlata
from prediccion_ganadera.adaptadores.almacen.postgres import tablas_staging
from prediccion_ganadera.modelado.dataset import Confianza, DatosInsuficientes, MetricasValidacion
from prediccion_ganadera.modelado.entrenamiento import (
    OpcionesEntrenamiento,
    RecomendacionCategoria,
    entrenar_modelo,
    recomendaciones,
)
from prediccion_ganadera.modelado.variables import (
    ESTADOS_VALIDADOS,
    ResultadoPrecios,
    precios_desde_tablas,
)

Fuente = Literal["plata", "staging"]
AVISO_NO_VERIFICADAS = (
    "incluye tablas sin verificar visualmente: las cifras pueden tener errores de extracción"
)


class ResumenModelado(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: str
    fuente: Fuente
    incluye_no_verificadas: bool
    fechas_historico: int
    fecha_desde: date
    fecha_hasta: date
    precios: int
    descartes: dict[str, int]  # filas descartadas por motivo
    tablas_resumen: int
    tablas_omitidas: int
    metricas: MetricasValidacion
    confianza: Confianza
    recomendaciones: list[RecomendacionCategoria]
    predicciones_oro: int


def leer_precios(
    fuente: Fuente,
    *,
    datos: Path,
    dsn: str | None = None,
    incluir_no_verificadas: bool = False,
) -> ResultadoPrecios:
    """Precios desde plata (Delta) o staging (Postgres; requiere `dsn`)."""
    estados = set(ESTADOS_VALIDADOS) | ({"pendiente"} if incluir_no_verificadas else set())
    if fuente == "staging":
        if not dsn:
            raise ValueError("la fuente staging requiere una conexión a Postgres")
        with psycopg.connect(dsn) as conexion:
            tablas = tablas_staging(conexion)
    else:
        tablas = AlmacenPlata(datos / "plata").tablas()
    return precios_desde_tablas(tablas, estados=estados)


def ejecutar_modelado(
    precios: ResultadoPrecios,
    *,
    datos: Path,
    fuente: Fuente,
    incluir_no_verificadas: bool,
    ahora: datetime,
    opciones: OpcionesEntrenamiento | None = None,
) -> ResumenModelado:
    try:
        resultado = entrenar_modelo(
            precios.precios, opciones or OpcionesEntrenamiento(), ahora=ahora
        )
    except DatosInsuficientes as error:
        detalle = [str(error), f"{len(precios.fechas)} subasta(s) con precios"]
        if precios.tablas_omitidas:
            detalle.append(
                f"{precios.tablas_omitidas} tabla(s) de resumen sin validar o sin fecha "
                "(verifícalas con IA o usa --incluir-no-verificadas)"
            )
        raise DatosInsuficientes("; ".join(detalle)) from error
    if incluir_no_verificadas:
        motivos = [*resultado.confianza.motivos, AVISO_NO_VERIFICADAS]
        resultado = replace(
            resultado, confianza=resultado.confianza.model_copy(update={"motivos": motivos})
        )
    version = RegistroModelos(datos / "modelos").guardar(resultado, ahora=ahora)
    escritas = AlmacenOro(datos / "oro").guardar_predicciones(version, resultado, ahora=ahora)
    resumen = ResumenModelado(
        version=version,
        fuente=fuente,
        incluye_no_verificadas=incluir_no_verificadas,
        fechas_historico=resultado.fechas_historico,
        fecha_desde=resultado.fecha_desde,
        fecha_hasta=resultado.fecha_hasta,
        precios=len(precios.precios),
        descartes=dict(Counter(d.motivo for d in precios.descartes)),
        tablas_resumen=precios.tablas_resumen,
        tablas_omitidas=precios.tablas_omitidas,
        metricas=resultado.metricas,
        confianza=resultado.confianza,
        recomendaciones=recomendaciones(resultado.ranking),
        predicciones_oro=escritas,
    )
    RegistroModelos(datos / "modelos").guardar_resumen(version, resumen.model_dump(mode="json"))
    return resumen


def cargar_resumen(datos: Path, version: str | None = None) -> ResumenModelado:
    """El resumen guardado junto al modelo (la versión más reciente si no se indica);
    `KeyError` si no hay modelo o si la versión no tiene resumen."""
    return ResumenModelado.model_validate(RegistroModelos(datos / "modelos").leer_resumen(version))
