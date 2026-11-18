"""Puerto del registro de corridas (observabilidad del orquestador)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

EstadoRegistro = Literal["en_curso", "completada", "fallida", "interrumpida"]


class RegistroCorrida(BaseModel):
    model_config = ConfigDict(frozen=True)

    id_corrida: str
    estado: EstadoRegistro
    iniciada_en: datetime
    actualizada_en: datetime
    terminada_en: datetime | None  # solo para completada o fallida
    tokens: int
    costo_usd: float
    error: str | None
    pasos: dict[str, dict[str, Any]]  # nombre -> resumen, uso y duración


class RegistroCorridas(Protocol):
    def guardar(self, registro: RegistroCorrida) -> None: ...
