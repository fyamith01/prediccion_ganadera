"""Herramienta determinista: descarga a bronce los PDFs que aún no están registrados.

Un fallo en un PDF no detiene la corrida: queda en `errores` para el reporte del
agente y se reintenta en la siguiente ejecución.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Protocol

from prediccion_ganadera.adaptadores.almacen.bronce import (
    AlmacenBronce,
    ErrorContenido,
    RegistroManifiesto,
)
from prediccion_ganadera.adaptadores.fuente.http import ErrorFuente
from prediccion_ganadera.herramientas.enlaces import EnlaceSubasta


class FuentePdfs(Protocol):
    def descargar(self, url: str) -> bytes: ...


@dataclass
class ResultadoDescarga:
    nuevos: list[RegistroManifiesto] = field(default_factory=list)
    omitidos: list[str] = field(default_factory=list)
    errores: list[tuple[str, str]] = field(default_factory=list)


def descargar_nuevos(
    enlaces: Iterable[EnlaceSubasta],
    fuente: FuentePdfs,
    almacen: AlmacenBronce,
    *,
    fechas_decididas: Mapping[str, date] | None = None,
    ahora: datetime | None = None,
) -> ResultadoDescarga:
    """Descarga cada enlace no registrado; `fechas_decididas` resuelve los conflictos."""
    decididas = fechas_decididas or {}
    registradas = almacen.urls_registradas()
    resultado = ResultadoDescarga()
    for enlace in enlaces:
        if enlace.url in registradas:
            resultado.omitidos.append(enlace.url)
            continue
        try:
            contenido = fuente.descargar(enlace.url)
            registro = almacen.guardar(
                enlace.url,
                contenido,
                fecha=decididas.get(enlace.url, enlace.fecha),
                especial=enlace.especial,
                ahora=ahora or datetime.now(UTC),
            )
        except (ErrorFuente, ErrorContenido) as error:
            resultado.errores.append((enlace.url, str(error)))
            continue
        registradas.add(enlace.url)
        resultado.nuevos.append(registro)
    return resultado
