"""Herramienta determinista: compara las fechas publicadas con el calendario esperado.

La fuente realiza una subasta por semana (los viernes en los datos de 2025-2026).
El reporte no interpreta los huecos (festivos, subastas aplazadas o especiales):
eso lo hace el agente recolector con el reporte como evidencia.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import date, timedelta

from pydantic import BaseModel, ConfigDict

VIERNES = 4


class ReporteHuecos(BaseModel):
    model_config = ConfigDict(frozen=True)

    desde: date
    hasta: date
    esperadas: tuple[date, ...]
    faltantes: tuple[date, ...]
    fuera_de_calendario: tuple[date, ...]
    duplicadas: tuple[date, ...]

    @property
    def cobertura(self) -> float:
        """Fracción de subastas esperadas que tienen resumen publicado."""
        if not self.esperadas:
            return 1.0
        return 1 - len(self.faltantes) / len(self.esperadas)


def _fechas_esperadas(desde: date, hasta: date, dia_semana: int) -> tuple[date, ...]:
    primera = desde + timedelta(days=(dia_semana - desde.weekday()) % 7)
    semanas = range(0, (hasta - primera).days + 1, 7) if primera <= hasta else range(0)
    return tuple(primera + timedelta(days=dias) for dias in semanas)


def reportar_huecos(
    fechas: Iterable[date], desde: date, hasta: date, dia_semana: int = VIERNES
) -> ReporteHuecos:
    """Subastas faltantes, fuera de calendario y duplicadas en `[desde, hasta]`."""
    if desde > hasta:
        raise ValueError(f"rango inválido: desde={desde} es posterior a hasta={hasta}")
    conteo = Counter(f for f in fechas if desde <= f <= hasta)
    esperadas = _fechas_esperadas(desde, hasta, dia_semana)
    return ReporteHuecos(
        desde=desde,
        hasta=hasta,
        esperadas=esperadas,
        faltantes=tuple(f for f in esperadas if f not in conteo),
        fuera_de_calendario=tuple(sorted(f for f in conteo if f.weekday() != dia_semana)),
        duplicadas=tuple(sorted(f for f, veces in conteo.items() if veces > 1)),
    )
