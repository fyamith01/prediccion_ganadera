from pathlib import Path

import pytest

from prediccion_ganadera.herramientas.extraer_tablas_pdf import ErrorExtraccion
from prediccion_ganadera.herramientas.paginas import lineas_de_pagina, renderizar_pagina

PDF_MUESTRA = (
    Path(__file__).parent / "fixtures" / "pdfs" / "Resumen-de-subasta-de-ganado-24-07-2026.pdf"
)


def test_renderiza_la_pagina_como_png(tmp_path: Path):
    ruta = renderizar_pagina(PDF_MUESTRA, 1, tmp_path, escala=1.0)

    assert ruta == tmp_path / "p1.png"
    contenido = ruta.read_bytes()
    assert contenido.startswith(b"\x89PNG")
    ancho = int.from_bytes(contenido[16:20], "big")
    assert 500 < ancho < 1000  # carta u oficio a 72 ppp


def test_la_escala_aumenta_la_resolucion(tmp_path: Path):
    normal = renderizar_pagina(PDF_MUESTRA, 1, tmp_path / "a", escala=1.0)
    doble = renderizar_pagina(PDF_MUESTRA, 1, tmp_path / "b", escala=2.0)

    ancho = int.from_bytes(normal.read_bytes()[16:20], "big")
    assert int.from_bytes(doble.read_bytes()[16:20], "big") == pytest.approx(2 * ancho, abs=2)


def test_no_vuelve_a_renderizar_una_pagina_existente(tmp_path: Path):
    ruta = renderizar_pagina(PDF_MUESTRA, 2, tmp_path)
    ruta.write_bytes(b"marcador")

    assert renderizar_pagina(PDF_MUESTRA, 2, tmp_path).read_bytes() == b"marcador"


@pytest.mark.parametrize("pagina", [0, 5])
def test_pagina_inexistente_es_un_error(tmp_path: Path, pagina: int):
    with pytest.raises(ErrorExtraccion, match="página"):
        renderizar_pagina(PDF_MUESTRA, pagina, tmp_path)


def test_lineas_de_pagina_en_orden_de_lectura():
    lineas = lineas_de_pagina(PDF_MUESTRA, 1)

    assert "BUFALA HORRA 12 192 10,550 10,550 10,550 2,022,083" in lineas
    assert lineas.index("TOTAL: 209") < lineas.index("TOTAL: 160") < lineas.index("GRAN TOTAL 389")


def test_lineas_de_un_pdf_ilegible(tmp_path: Path):
    roto = tmp_path / "roto.pdf"
    roto.write_bytes(b"%PDF-1.7\n roto")

    with pytest.raises(ErrorExtraccion):
        lineas_de_pagina(roto, 1)
