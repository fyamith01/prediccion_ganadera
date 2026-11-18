"""Capa bronce: PDFs inmutables nombrados por SHA-256 y un manifiesto JSONL.

El manifiesto es de solo-anexar y tiene un registro por URL; si dos URLs publican el
mismo archivo comparten el PDF en disco. Escribir de nuevo una URL registrada no
cambia nada (idempotencia para corridas repetidas).
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

FIRMA_PDF = b"%PDF-"


class ErrorContenido(ValueError):
    """El contenido descargado no es un PDF (p. ej. una página de error)."""


class RegistroManifiesto(BaseModel):
    model_config = ConfigDict(frozen=True)

    url: str
    sha256: str
    ruta: str
    bytes: int
    fecha: date | None
    especial: bool
    descargado_en: datetime


class AlmacenBronce:
    def __init__(self, raiz: Path) -> None:
        self.raiz = Path(raiz)
        self.manifiesto = self.raiz / "manifiesto.jsonl"

    def registros(self) -> list[RegistroManifiesto]:
        if not self.manifiesto.exists():
            return []
        lineas = self.manifiesto.read_text(encoding="utf-8").splitlines()
        return [RegistroManifiesto.model_validate_json(linea) for linea in lineas if linea]

    def ruta_pdf(self, sha256: str) -> Path:
        return self.raiz / "pdfs" / f"{sha256}.pdf"

    def urls_registradas(self) -> set[str]:
        return {r.url for r in self.registros()}

    def guardar(
        self, url: str, contenido: bytes, *, fecha: date | None, especial: bool, ahora: datetime
    ) -> RegistroManifiesto:
        existente = next((r for r in self.registros() if r.url == url), None)
        if existente is not None:
            return existente
        if not contenido.startswith(FIRMA_PDF):
            raise ErrorContenido(f"{url} no es un PDF (firma {contenido[:8]!r})")
        sha = hashlib.sha256(contenido).hexdigest()
        ruta = self.ruta_pdf(sha).relative_to(self.raiz)
        self._escribir_si_falta(self.raiz / ruta, contenido)
        registro = RegistroManifiesto(
            url=url,
            sha256=sha,
            ruta=ruta.as_posix(),
            bytes=len(contenido),
            fecha=fecha,
            especial=especial,
            descargado_en=ahora,
        )
        with self.manifiesto.open("a", encoding="utf-8") as archivo:
            archivo.write(registro.model_dump_json() + "\n")
        return registro

    @staticmethod
    def _escribir_si_falta(destino: Path, contenido: bytes) -> None:
        if destino.exists():
            return
        destino.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporal = tempfile.mkstemp(dir=destino.parent, suffix=".parcial")
        try:
            with os.fdopen(descriptor, "wb") as archivo:
                archivo.write(contenido)
            os.replace(temporal, destino)
        except BaseException:
            Path(temporal).unlink(missing_ok=True)
            raise
