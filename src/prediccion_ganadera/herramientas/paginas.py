"""Herramientas deterministas sobre páginas de un PDF: imagen y líneas de texto.

La imagen (pypdfium2) es lo que ve el verificador visual. Las líneas salen de
`pdfplumber.extract_text_lines`, que agrupa los caracteres por renglón sin pasar por
el detector de tablas: sirven de referencia independiente para comprobar la tabla.
El texto de pypdfium2 no sirve para esto porque llega en orden de columnas.
"""

from __future__ import annotations

from pathlib import Path

import pdfplumber
import pypdfium2
from pdfplumber.utils.exceptions import PdfminerException

from prediccion_ganadera.herramientas.extraer_tablas_pdf import ErrorExtraccion


def renderizar_pagina(ruta_pdf: Path, pagina: int, destino: Path, escala: float = 2.0) -> Path:
    """PNG de la página (desde 1) en `destino/p<pagina>.png`; si ya existe, no la rehace."""
    salida = Path(destino) / f"p{pagina}.png"
    if salida.exists():
        return salida
    try:
        documento = pypdfium2.PdfDocument(ruta_pdf)
    except pypdfium2.PdfiumError as error:
        raise ErrorExtraccion(f"no se pudo abrir {Path(ruta_pdf).name}: {error}") from error
    try:
        if not 1 <= pagina <= len(documento):
            raise ErrorExtraccion(
                f"{Path(ruta_pdf).name} no tiene la página {pagina} (tiene {len(documento)})"
            )
        imagen = documento[pagina - 1].render(scale=escala).to_pil()
    finally:
        documento.close()
    salida.parent.mkdir(parents=True, exist_ok=True)
    temporal = salida.with_suffix(".parcial")
    imagen.save(temporal, format="PNG")
    temporal.replace(salida)
    return salida


def lineas_de_pagina(ruta_pdf: Path, pagina: int) -> list[str]:
    """Renglones de texto de la página (desde 1), de arriba abajo."""
    try:
        with pdfplumber.open(ruta_pdf) as pdf:
            if not 1 <= pagina <= len(pdf.pages):
                raise ErrorExtraccion(f"{Path(ruta_pdf).name} no tiene la página {pagina}")
            return [linea["text"] for linea in pdf.pages[pagina - 1].extract_text_lines()]
    except (PdfminerException, FileNotFoundError) as error:
        raise ErrorExtraccion(f"no se pudo leer {Path(ruta_pdf).name}: {error}") from error
