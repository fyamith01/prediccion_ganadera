"""Puerto de reportes: cómo se dibuja un reporte (PDF hoy; otro formato mañana)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from prediccion_ganadera.herramientas.cifras_reporte import CifrasReporte, Narrativa


class Renderizador(Protocol):
    extension: str  # p. ej. ".pdf"

    def renderizar(
        self,
        cifras: CifrasReporte,
        narrativa: Narrativa,
        destino: Path,
        *,
        generado_en: datetime,
    ) -> None:
        """Escribe el reporte en `destino`."""
        ...
