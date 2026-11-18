"""Herramienta determinista: extrae a plata los PDFs de bronce que aún no están allí."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from prediccion_ganadera.adaptadores.almacen.bronce import AlmacenBronce
from prediccion_ganadera.adaptadores.almacen.plata import AlmacenPlata
from prediccion_ganadera.herramientas.extraer_tablas_pdf import ErrorExtraccion, extraer_tablas


class ResumenExtraccion(BaseModel):
    procesados: list[str] = Field(default_factory=list)  # SHA-256 escritos en plata
    omitidos: int = 0  # ya estaban en plata
    sin_tablas: list[str] = Field(default_factory=list)  # se reintentan en la próxima corrida
    errores: dict[str, str] = Field(default_factory=dict)  # SHA-256 -> motivo
    tablas: int = 0
    celdas: int = 0


def extraer_bronce_a_plata(
    bronce: AlmacenBronce, plata: AlmacenPlata, *, ahora: datetime
) -> ResumenExtraccion:
    """Recorre el manifiesto; un PDF ilegible se reporta y no detiene el lote."""
    resumen = ResumenExtraccion()
    ya_en_plata = plata.sha_registrados()
    vistos: set[str] = set()
    for registro in bronce.registros():
        # Varias URLs pueden publicar el mismo archivo: se extrae una vez por hash.
        if registro.sha256 in vistos:
            continue
        vistos.add(registro.sha256)
        if registro.sha256 in ya_en_plata:
            resumen.omitidos += 1
            continue
        try:
            tablas = extraer_tablas(bronce.raiz / registro.ruta)
        except ErrorExtraccion as error:
            resumen.errores[registro.sha256] = str(error)
            continue
        if not tablas:
            resumen.sin_tablas.append(registro.sha256)
            continue
        resumen.celdas += plata.guardar(
            sha256=registro.sha256,
            fecha=registro.fecha,
            especial=registro.especial,
            tablas=tablas,
            ahora=ahora,
        )
        resumen.tablas += len(tablas)
        resumen.procesados.append(registro.sha256)
    return resumen
