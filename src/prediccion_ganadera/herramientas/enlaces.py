"""Herramienta determinista: lista los PDFs de subastas y resuelve su fecha.

La página de la fuente publica cada resumen como un enlace con un texto del tipo
`Subasta DD-MM-YYYY` y un nombre de archivo con formatos variables (`10-01-25.pdf`,
`resumen-de-subasta-2-1-2025.pdf`, ...). El texto es la fuente principal de la fecha
y el nombre del archivo la confirma. Si ambos se contradicen el enlace queda en
`conflicto` con sus fechas candidatas: lo decide el agente recolector, no esta
herramienta.
"""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit

from pydantic import BaseModel, ConfigDict

_FECHA_TEXTO = re.compile(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})")
_FECHA_NOMBRE = re.compile(r"(?<!\d)(\d{1,2})-(\d{1,2})-(\d{4}|\d{2})(?!\d)")
_ESPECIAL = re.compile(r"\besp(?:\.|ecial\b)", re.IGNORECASE)


class EstadoFecha(StrEnum):
    RESUELTA = "resuelta"
    CONFLICTO = "conflicto"
    SIN_FECHA = "sin_fecha"


class EnlaceSubasta(BaseModel):
    """Un PDF publicado por la fuente, con la fecha resuelta o sus candidatas."""

    model_config = ConfigDict(frozen=True)

    url: str
    texto: str
    fecha_texto: date | None
    fecha_archivo: date | None
    especial: bool

    @property
    def candidatas(self) -> tuple[date, ...]:
        """Fechas distintas encontradas, primero la del texto."""
        fechas = (self.fecha_texto, self.fecha_archivo)
        return tuple(dict.fromkeys(f for f in fechas if f is not None))

    @property
    def estado(self) -> EstadoFecha:
        cantidad = len(self.candidatas)
        if cantidad == 0:
            return EstadoFecha.SIN_FECHA
        return EstadoFecha.RESUELTA if cantidad == 1 else EstadoFecha.CONFLICTO

    @property
    def fecha(self) -> date | None:
        """Fecha de la subasta si no hay ambigüedad."""
        return self.candidatas[0] if self.estado is EstadoFecha.RESUELTA else None


def _fecha_valida(dia: str, mes: str, anio: str) -> date | None:
    anio_completo = int(anio) + 2000 if len(anio) == 2 else int(anio)
    try:
        return date(anio_completo, int(mes), int(dia))
    except ValueError:
        return None


def fecha_de_texto(texto: str) -> date | None:
    """Fecha `DD-MM-YYYY` del texto del enlace; `None` si falta o no existe."""
    coincidencia = _FECHA_TEXTO.search(texto)
    return _fecha_valida(*coincidencia.groups()) if coincidencia else None


def fecha_de_nombre(nombre: str) -> date | None:
    """Última fecha `D-M-YY` o `D-M-YYYY` del nombre de archivo; `None` si no es válida."""
    coincidencias = _FECHA_NOMBRE.findall(PurePosixPath(nombre).stem)
    return _fecha_valida(*coincidencias[-1]) if coincidencias else None


class _AnclasPdf(HTMLParser):
    """Recoge `(href, texto)` de cada `<a>`; el texto incluye el de sus hijos."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anclas: list[tuple[str, str]] = []
        self._href: str | None = None
        self._texto: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self._href = dict(attrs).get("href") or ""
            self._texto = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._texto.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.anclas.append((self._href, " ".join("".join(self._texto).split())))
            self._href = None


def _es_pdf_permitido(url: str, dominios_permitidos: set[str]) -> bool:
    partes = urlsplit(url)
    return (
        partes.scheme in {"http", "https"}
        and (partes.hostname or "") in dominios_permitidos
        and partes.path.lower().endswith(".pdf")
    )


def listar_enlaces(
    html: str, url_pagina: str, dominios_permitidos: list[str]
) -> list[EnlaceSubasta]:
    """Enlaces a PDF dentro de los dominios permitidos, sin duplicados y en orden de aparición."""
    lector = _AnclasPdf()
    lector.feed(html)
    lector.close()
    permitidos = {d.lower() for d in dominios_permitidos}
    enlaces: dict[str, EnlaceSubasta] = {}
    for href, texto in lector.anclas:
        url = urljoin(url_pagina, href.strip())
        if url in enlaces or not _es_pdf_permitido(url, permitidos):
            continue
        nombre = unquote(PurePosixPath(urlsplit(url).path).name)
        enlaces[url] = EnlaceSubasta(
            url=url,
            texto=texto,
            fecha_texto=fecha_de_texto(texto),
            fecha_archivo=fecha_de_nombre(nombre),
            especial=bool(_ESPECIAL.search(texto)),
        )
    return list(enlaces.values())
