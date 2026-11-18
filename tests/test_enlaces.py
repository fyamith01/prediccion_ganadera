from datetime import date
from pathlib import Path

import pytest

from prediccion_ganadera.herramientas.enlaces import (
    EstadoFecha,
    fecha_de_nombre,
    fecha_de_texto,
    listar_enlaces,
)

RAIZ = Path(__file__).resolve().parent
HTML_FUENTE = RAIZ / "fixtures" / "html" / "resumen_de_subastas.html"
PAGINA = "https://www.subastas.example/resumen-de-subastas/"
DOMINIOS = ["www.subastas.example", "subastas.example"]
UPLOADS = "https://www.subastas.example/wp-content/uploads/"


@pytest.mark.parametrize(
    ("nombre", "esperada"),
    [
        ("10-01-25.pdf", date(2025, 1, 10)),
        ("resumen-de-subasta-2-1-2025.pdf", date(2025, 1, 2)),
        ("Resumen-Subasta-Ganado-13-11-2026.pdf", date(2026, 11, 13)),
        ("Subasta-de-Ganado-14-02-2025.pdf", date(2025, 2, 14)),
        ("19-19-25.pdf", None),
        ("informe-mensual.pdf", None),
    ],
)
def test_fecha_de_nombre(nombre, esperada):
    assert fecha_de_nombre(nombre) == esperada


@pytest.mark.parametrize(
    ("texto", "esperada"),
    [
        ("Subasta 09-01-2026 ", date(2026, 1, 9)),
        ("Subasta Esp. 17-10-2026", date(2026, 10, 17)),
        ("Subasta 31-02-2026", None),
        ("", None),
    ],
)
def test_fecha_de_texto(texto, esperada):
    assert fecha_de_texto(texto) == esperada


def _html(*anclas: str) -> str:
    return "<html><body><ul>" + "".join(f"<li>{a}</li>" for a in anclas) + "</ul></body></html>"


def test_listar_enlaces_filtra_dominio_formato_y_duplicados():
    html = _html(
        '<a href="/wp-content/uploads/2026/01/09-01-26.pdf"><span>Subasta 09-01-2026</span></a>',
        '<a href="https://www.subastas.example/wp-content/uploads/2026/01/09-01-26.pdf">otra</a>',
        '<a href="https://malicioso.example/2026/01/16-01-26.pdf">Subasta 16-01-2026</a>',
        '<a href="https://www.subastas.example/contacto/">Contacto</a>',
        '<a href="javascript:alert(1)">x.pdf</a>',
        '<a href="https://subastas.example/doc/16-01-26.PDF?v=2">Subasta  16-01-2026</a>',
    )

    enlaces = listar_enlaces(html, PAGINA, DOMINIOS)

    assert [e.url for e in enlaces] == [
        "https://www.subastas.example/wp-content/uploads/2026/01/09-01-26.pdf",
        "https://subastas.example/doc/16-01-26.PDF?v=2",
    ]
    assert enlaces[0].texto == "Subasta 09-01-2026"
    assert enlaces[1].texto == "Subasta 16-01-2026"


def test_resolucion_de_fecha_por_texto_y_nombre():
    html = _html(
        f'<a href="{UPLOADS}2025/03/28-02-25.pdf">Subasta 28-02-2025</a>',
        f'<a href="{UPLOADS}2025/09/19-19-25.pdf">Subasta 19-09-2025</a>',
        f'<a href="{UPLOADS}2025/01/resumen-de-subasta-2-1-2025.pdf">Subasta 24-01-2025</a>',
        f'<a href="{UPLOADS}2025/05/informe.pdf">Informe</a>',
        f'<a href="{UPLOADS}2025/05/23-05-25.pdf">Descargar</a>',
    )

    acorde, mes_invalido, conflicto, sin_fecha, solo_nombre = listar_enlaces(html, PAGINA, DOMINIOS)

    assert (acorde.estado, acorde.fecha) == (EstadoFecha.RESUELTA, date(2025, 2, 28))
    assert (mes_invalido.estado, mes_invalido.fecha) == (EstadoFecha.RESUELTA, date(2025, 9, 19))
    assert conflicto.estado is EstadoFecha.CONFLICTO
    assert conflicto.fecha is None
    assert conflicto.candidatas == (date(2025, 1, 24), date(2025, 1, 2))
    assert (sin_fecha.estado, sin_fecha.fecha) == (EstadoFecha.SIN_FECHA, None)
    assert (solo_nombre.estado, solo_nombre.fecha) == (EstadoFecha.RESUELTA, date(2025, 5, 23))


def test_marca_subastas_especiales():
    html = _html(
        f'<a href="{UPLOADS}2026/10/R-17-10-2026.pdf">Subasta Esp. 17-10-2026</a>',
        f'<a href="{UPLOADS}2026/10/R-16-10-2026.pdf">Subasta 16-10-2026</a>',
    )

    especial, normal = listar_enlaces(html, PAGINA, DOMINIOS)

    assert especial.especial is True
    assert normal.especial is False


def test_fixture_real_de_la_fuente():
    enlaces = listar_enlaces(HTML_FUENTE.read_text(encoding="utf-8"), PAGINA, DOMINIOS)

    assert len(enlaces) == 95
    assert all(e.url.startswith(UPLOADS) for e in enlaces)
    por_estado = {estado: [e for e in enlaces if e.estado is estado] for estado in EstadoFecha}
    assert len(por_estado[EstadoFecha.RESUELTA]) == 94
    assert [e.url.rsplit("/", 1)[1] for e in por_estado[EstadoFecha.CONFLICTO]] == [
        "resumen-de-subasta-2-1-2025.pdf"
    ]
    assert por_estado[EstadoFecha.SIN_FECHA] == []
    assert [e.fecha for e in enlaces if e.especial] == [date(2026, 10, 17)]
    fechas = [e.fecha for e in por_estado[EstadoFecha.RESUELTA]]
    assert min(fechas) == date(2025, 1, 10)
    assert max(fechas) == date(2026, 11, 13)
