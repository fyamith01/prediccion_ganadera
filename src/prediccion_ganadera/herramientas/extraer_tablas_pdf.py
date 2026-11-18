"""Herramienta determinista: extrae las tablas crudas de un PDF de subasta.

No interpreta encabezados ni tipos: los PDFs parten encabezados entre celdas
(`GENERAL D` / `E LA FERIA`), así que el texto se conserva tal cual para que el
verificador visual (T4) lo compare con la página y el rectificador lo corrija. Los
ajustes usados viajan con cada tabla para poder reintentar con otros.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pdfplumber
from pdfplumber.utils.exceptions import PdfminerException
from pydantic import BaseModel, ConfigDict

# Las tablas del resumen no tienen rejilla completa: columnas y filas se infieren
# por la alineación del texto.
AJUSTES_POR_DEFECTO: dict[str, Any] = {
    "vertical_strategy": "text",
    "horizontal_strategy": "text",
    "snap_tolerance": 3,
    "join_tolerance": 3,
}


class ErrorExtraccion(RuntimeError):
    """El PDF no se pudo abrir o leer."""


class FilaExtraida(BaseModel):
    model_config = ConfigDict(frozen=True)

    indice: int  # posición original en la tabla de pdfplumber (las vacías se omiten)
    celdas: tuple[str | None, ...]


class TablaExtraida(BaseModel):
    model_config = ConfigDict(frozen=True)

    pagina: int
    indice: int  # orden de la tabla dentro de la página, desde 1
    columnas: int
    filas: tuple[FilaExtraida, ...]
    ajustes: dict[str, Any]


def _fila_vacia(celdas: list[str | None]) -> bool:
    return not any(c and c.strip() for c in celdas)


def extraer_tablas(ruta: Path, ajustes: Mapping[str, Any] | None = None) -> list[TablaExtraida]:
    """Devuelve las tablas no vacías del PDF, en orden de página."""
    usados = dict(AJUSTES_POR_DEFECTO if ajustes is None else ajustes)
    tablas: list[TablaExtraida] = []
    try:
        with pdfplumber.open(ruta) as pdf:
            for num_pagina, pagina in enumerate(pdf.pages, start=1):
                crudas = pagina.extract_tables(table_settings=usados)
                for num_tabla, cruda in enumerate(crudas, start=1):
                    filas = tuple(
                        FilaExtraida(indice=i, celdas=tuple(fila))
                        for i, fila in enumerate(cruda)
                        if not _fila_vacia(fila)
                    )
                    if not filas:
                        continue
                    tablas.append(
                        TablaExtraida(
                            pagina=num_pagina,
                            indice=num_tabla,
                            columnas=max(len(fila) for fila in cruda),
                            filas=filas,
                            ajustes=usados,
                        )
                    )
    except PdfminerException as error:
        raise ErrorExtraccion(f"no se pudo leer {Path(ruta).name}: {error}") from error
    return tablas
