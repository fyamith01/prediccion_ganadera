"""Puerto de IA: lo único que los agentes saben del modelo que los atiende.

Cada agente recibe un `ProveedorIA` según `config/agentes.yaml`; puede ser un harness
headless (Claude Code, Antigravity, Cursor) o un modelo local.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class ErrorProveedorIA(RuntimeError):
    """El backend no respondió, respondió con error o con una salida ilegible."""


class RespuestaIA(BaseModel):
    model_config = ConfigDict(frozen=True)

    texto: str
    tokens_entrada: int | None = None
    tokens_salida: int | None = None
    costo_usd: float | None = None


class UsoIA(BaseModel):
    """Consumo acumulado de un agente en una corrida (para presupuesto y observabilidad)."""

    llamadas_ia: int = 0
    tokens: int = 0
    costo_usd: float = 0.0

    def registrar(self, respuesta: RespuestaIA) -> None:
        self.tokens += (respuesta.tokens_entrada or 0) + (respuesta.tokens_salida or 0)
        self.costo_usd += respuesta.costo_usd or 0.0


class ProveedorIA(Protocol):
    def completar(self, prompt: str, imagenes: Sequence[Path] = ()) -> RespuestaIA:
        """Respuesta del modelo; `imagenes` son archivos locales (p. ej. páginas en PNG)."""
        ...
