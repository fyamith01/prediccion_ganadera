"""Puerto de IA: lo único que los agentes saben del modelo que los atiende.

Cada agente recibe un `ProveedorIA` según `config/agentes.yaml`; puede ser un harness
headless (Claude Code, Antigravity, Cursor) o un modelo local.
"""

from __future__ import annotations

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


class ProveedorIA(Protocol):
    def completar(self, prompt: str) -> RespuestaIA: ...
