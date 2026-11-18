from datetime import date
from pathlib import Path

import pytest

from prediccion_ganadera.herramientas.calendario import VIERNES, reportar_huecos
from prediccion_ganadera.herramientas.enlaces import listar_enlaces

HTML_FUENTE = (
    Path(__file__).resolve().parent / "fixtures" / "html" / "resumen_de_subastas.html"
)


def test_reporta_faltantes_fuera_de_calendario_y_duplicadas():
    fechas = [
        date(2026, 1, 9),
        date(2026, 1, 9),
        date(2026, 1, 23),
        date(2026, 1, 24),  # sábado
        date(2025, 12, 26),  # fuera del rango consultado
    ]

    reporte = reportar_huecos(fechas, desde=date(2026, 1, 5), hasta=date(2026, 1, 31))

    assert reporte.esperadas == (
        date(2026, 1, 9),
        date(2026, 1, 16),
        date(2026, 1, 23),
        date(2026, 1, 30),
    )
    assert reporte.faltantes == (date(2026, 1, 16), date(2026, 1, 30))
    assert reporte.fuera_de_calendario == (date(2026, 1, 24),)
    assert reporte.duplicadas == (date(2026, 1, 9),)
    assert reporte.cobertura == pytest.approx(0.5)


def test_rango_sin_subastas_esperadas():
    reporte = reportar_huecos([], desde=date(2026, 1, 10), hasta=date(2026, 1, 15))

    assert reporte.esperadas == ()
    assert reporte.faltantes == ()
    assert reporte.cobertura == 1.0


def test_rango_invertido_es_un_error():
    with pytest.raises(ValueError, match="desde"):
        reportar_huecos([], desde=date(2026, 2, 1), hasta=date(2026, 1, 1))


def test_dia_de_subasta_configurable():
    reporte = reportar_huecos(
        [date(2026, 1, 6)], desde=date(2026, 1, 5), hasta=date(2026, 1, 14), dia_semana=1
    )

    assert VIERNES == 4
    assert reporte.esperadas == (date(2026, 1, 6), date(2026, 1, 13))
    assert reporte.faltantes == (date(2026, 1, 13),)


def test_huecos_del_fixture_real():
    enlaces = listar_enlaces(
        HTML_FUENTE.read_text(encoding="utf-8"),
        "https://www.subastas.example/resumen-de-subastas/",
        ["www.subastas.example"],
    )
    fechas = [e.fecha for e in enlaces if e.fecha is not None]

    reporte = reportar_huecos(fechas, desde=date(2025, 1, 10), hasta=date(2026, 11, 13))

    assert reporte.faltantes == (
        date(2025, 1, 24),  # el enlace en conflicto (texto 24-01, archivo 2-1)
        date(2025, 4, 18),  # Viernes Santo
        date(2026, 1, 2),
        date(2026, 4, 3),  # Viernes Santo
        date(2026, 10, 30),
    )
    assert reporte.fuera_de_calendario == (date(2026, 10, 17), date(2026, 10, 31))
    assert reporte.duplicadas == ()
