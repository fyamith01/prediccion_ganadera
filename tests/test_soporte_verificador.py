"""Piezas pequeñas que el verificador necesita de módulos existentes."""

import sys
from datetime import UTC, date, datetime
from pathlib import Path

from prediccion_ganadera.adaptadores.almacen.bronce import AlmacenBronce
from prediccion_ganadera.adaptadores.almacen.plata import AlmacenPlata
from prediccion_ganadera.adaptadores.ia.cli_harness import ProveedorCliHarness
from prediccion_ganadera.configuracion import BackendCliHarness, Limites
from prediccion_ganadera.herramientas.comprobaciones import comprobar_pagina
from prediccion_ganadera.herramientas.extraer_tablas_pdf import (
    AJUSTES_ALTERNATIVOS,
    AJUSTES_POR_DEFECTO,
    extraer_tablas,
)

PDF_MUESTRA = (
    Path(__file__).parent / "fixtures" / "pdfs" / "Resumen-de-subasta-de-ganado-24-07-2026.pdf"
)
AHORA = datetime(2026, 11, 18, tzinfo=UTC)

ECO = "import sys; print(sys.argv[1])"


def test_extrae_solo_las_paginas_pedidas():
    tablas = extraer_tablas(PDF_MUESTRA, paginas={2, 4})

    assert [t.pagina for t in tablas] == [2, 4]


def test_hay_ajustes_alternativos_distintos_del_por_defecto():
    assert AJUSTES_ALTERNATIVOS
    assert all(a != AJUSTES_POR_DEFECTO for a in AJUSTES_ALTERNATIVOS)


def test_los_hallazgos_faltantes_traen_el_renglon_de_la_pagina():
    tablas = extraer_tablas(PDF_MUESTRA, paginas={1})

    hallazgos = comprobar_pagina(tablas, ["GRAN TOTAL 389"])[1]

    (hallazgo,) = [h for h in hallazgos if h.tipo == "total_faltante"]

    assert hallazgo.renglon == "GRAN TOTAL 389"


def test_bronce_da_la_ruta_del_pdf_por_sha(tmp_path: Path):
    bronce = AlmacenBronce(tmp_path)
    registro = bronce.guardar(
        "https://x/a.pdf", PDF_MUESTRA.read_bytes(), fecha=None, especial=False, ahora=AHORA
    )

    assert bronce.ruta_pdf(registro.sha256) == tmp_path / registro.ruta
    assert bronce.ruta_pdf(registro.sha256).exists()


def test_plata_lee_todas_las_tablas_de_una_pagina(tmp_path: Path):
    plata = AlmacenPlata(tmp_path)
    plata.guardar(
        sha256="ab",
        fecha=date(2026, 7, 24),
        especial=False,
        tablas=extraer_tablas(PDF_MUESTRA, paginas={1, 2}),
        ahora=AHORA,
    )

    assert [t.clave.tabla for t in plata.tablas_de_pagina("ab", 2)] == [1]
    assert plata.tablas_de_pagina("ab", 3) == []


def test_cli_harness_pasa_las_imagenes_como_rutas_absolutas_en_el_prompt(tmp_path: Path):
    imagen = tmp_path / "p1.png"
    proveedor = ProveedorCliHarness(
        BackendCliHarness(tipo="cli_harness", comando=[sys.executable, "-c", ECO, "{prompt}"])
    )

    texto = proveedor.completar("compara", imagenes=[imagen]).texto

    assert texto.startswith("compara")
    assert str(imagen.resolve()) in texto


def test_limites_traen_la_confianza_minima_del_verificador():
    assert Limites().confianza_minima_verificacion == 0.8
