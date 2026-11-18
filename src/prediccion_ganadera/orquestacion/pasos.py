"""Pasos reales de la corrida: conectan el grafo con los agentes, herramientas y almacenes.

Cada paso se construye a partir de la configuración y lee del estado del grafo el
presupuesto de tokens que le queda a la corrida. Los fallos esperados se traducen a
`ErrorPaso` (la corrida termina como `fallida`); los demás se propagan para reanudar.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg

from prediccion_ganadera.adaptadores.almacen.bronce import AlmacenBronce
from prediccion_ganadera.adaptadores.almacen.plata import AlmacenPlata
from prediccion_ganadera.adaptadores.almacen.postgres import cargar_plata
from prediccion_ganadera.adaptadores.almacen.revision import ColaRevision
from prediccion_ganadera.adaptadores.fuente.http import ErrorFuente, FuenteHttp
from prediccion_ganadera.adaptadores.ia import crear_proveedor
from prediccion_ganadera.agentes.recolector import AgenteRecolector
from prediccion_ganadera.agentes.verificador import AgenteVerificador
from prediccion_ganadera.configuracion import Configuracion, ErrorConfiguracion
from prediccion_ganadera.herramientas.bronce_a_plata import extraer_bronce_a_plata
from prediccion_ganadera.modelado.dataset import DatosInsuficientes
from prediccion_ganadera.modelado.modelador import ejecutar_modelado, leer_precios
from prediccion_ganadera.orquestacion.grafo import ErrorPaso, EstadoCorrida, Paso, ResultadoPaso

SIN_DSN = "sin conexión a Postgres: define PREDICCION_PG_DSN o usa --dsn"


@dataclass(frozen=True)
class OpcionesCorrida:
    sin_ia: bool = False
    html: str | None = None  # copia local de la página de subastas
    descargar: bool = True
    desde: date | None = None
    hasta: date | None = None
    dsn: str | None = None


def _ahora() -> datetime:
    return datetime.now(UTC)


def crear_pasos(config: Configuracion, opciones: OpcionesCorrida) -> dict[str, Paso]:
    """Valida la configuración y los backends antes de la corrida (lanza ErrorConfiguracion
    o ErrorProveedorIA) y devuelve los pasos del grafo."""
    if config.fuente is None:
        raise ErrorConfiguracion("falta la sección 'fuente' en la configuración")
    fuente_cfg = config.fuente
    ia_recolector = None if opciones.sin_ia else crear_proveedor(config, "recolector")
    ia_verificador = None if opciones.sin_ia else crear_proveedor(config, "verificador")
    datos: Path = config.rutas.datos
    bronce = AlmacenBronce(datos / "bronce")
    plata = AlmacenPlata(datos / "plata")

    def recolectar(estado: EstadoCorrida) -> ResultadoPaso:
        agente = AgenteRecolector(
            ia=ia_recolector,
            fuente=FuenteHttp(fuente_cfg.dominios_permitidos),
            almacen=bronce,
            url_pagina=fuente_cfg.url,
            dominios_permitidos=fuente_cfg.dominios_permitidos,
            presupuesto_tokens=estado["presupuesto_restante"],
        )
        try:
            resultado = agente.ejecutar(
                html=opciones.html,
                descargar=opciones.descargar,
                desde=opciones.desde,
                hasta=opciones.hasta,
            )
        except ErrorFuente as error:
            raise ErrorPaso(f"no se pudo leer la fuente: {error}") from error
        return ResultadoPaso(
            resumen=resultado.model_dump(mode="json"),
            tokens=resultado.uso.tokens,
            costo_usd=resultado.uso.costo_usd,
        )

    def extraer(_estado: EstadoCorrida) -> ResultadoPaso:
        resumen = extraer_bronce_a_plata(bronce, plata, ahora=_ahora())
        return ResultadoPaso(resumen=resumen.model_dump(mode="json"))

    def verificar(estado: EstadoCorrida) -> ResultadoPaso:
        agente = AgenteVerificador(
            ia=ia_verificador,
            bronce=bronce,
            plata=plata,
            cola=ColaRevision(datos / "plata"),
            dir_paginas=datos / "paginas",
            max_rectificaciones=config.limites.max_iteraciones_rectificacion,
            confianza_minima=config.limites.confianza_minima_verificacion,
            presupuesto_tokens=estado["presupuesto_restante"],
        )
        resultado = agente.ejecutar(ahora=_ahora())
        return ResultadoPaso(
            resumen={**resultado.model_dump(mode="json"), "cuenta": resultado.cuenta()},
            tokens=resultado.uso.tokens,
            costo_usd=resultado.uso.costo_usd,
        )

    def cargar(_estado: EstadoCorrida) -> ResultadoPaso:
        if not opciones.dsn:
            return ResultadoPaso(omitido=SIN_DSN)
        try:
            with psycopg.connect(opciones.dsn) as conexion:
                resumen = cargar_plata(plata, conexion, ahora=_ahora())
        except psycopg.Error as error:
            # libpq no incluye la contraseña en sus mensajes; el DSN nunca se imprime.
            raise ErrorPaso(f"no se pudo cargar en Postgres: {error}") from error
        return ResultadoPaso(resumen=resumen.model_dump(mode="json"))

    def modelar(_estado: EstadoCorrida) -> ResultadoPaso:
        # Lee plata (solo tablas validadas): no depende de que Postgres esté disponible.
        precios = leer_precios("plata", datos=datos)
        try:
            resumen = ejecutar_modelado(
                precios, datos=datos, fuente="plata", incluir_no_verificadas=False, ahora=_ahora()
            )
        except DatosInsuficientes as error:
            return ResultadoPaso(omitido=f"sin datos suficientes: {error}")
        return ResultadoPaso(resumen=resumen.model_dump(mode="json"))

    return {
        "recolectar": recolectar,
        "extraer": extraer,
        "verificar": verificar,
        "cargar": cargar,
        "modelar": modelar,
    }
