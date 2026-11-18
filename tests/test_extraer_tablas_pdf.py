import subprocess
import sys
from pathlib import Path

import pytest

from prediccion_ganadera.herramientas.extraer_tablas_pdf import (
    AJUSTES_POR_DEFECTO,
    ErrorExtraccion,
    extraer_tablas,
)

PDF_MUESTRA = (
    Path(__file__).parent / "fixtures" / "pdfs" / "Resumen-de-subasta-de-ganado-24-07-2026.pdf"
)


def test_importar_el_modulo_no_ejecuta_nada(tmp_path: Path):
    # Proceso aparte: recargar el módulo aquí cambiaría las clases que usan otras pruebas.
    proceso = subprocess.run(
        [sys.executable, "-c", "import prediccion_ganadera.herramientas.extraer_tablas_pdf"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    assert proceso.stdout == ""
    assert list(tmp_path.iterdir()) == []


def test_extrae_una_tabla_por_pagina_del_pdf_de_muestra():
    tablas = extraer_tablas(PDF_MUESTRA)

    assert [(t.pagina, t.indice) for t in tablas] == [(1, 1), (2, 1), (3, 1), (4, 1)]
    assert [t.columnas for t in tablas] == [8, 11, 11, 12]
    assert all(t.ajustes == AJUSTES_POR_DEFECTO for t in tablas)


def test_conserva_el_texto_crudo_y_descarta_filas_vacias():
    primera = extraer_tablas(PDF_MUESTRA)[0]

    textos = [celda for fila in primera.filas for celda in fila.celdas]
    assert "RESUMEN" in textos
    assert "Feria: 1369 F" in textos
    assert all(any(c for c in fila.celdas) for fila in primera.filas)
    indices = [fila.indice for fila in primera.filas]
    assert indices == sorted(indices)
    assert indices[:2] == [0, 2]  # la fila 1 (vacía) se descarta sin renumerar


def test_acepta_ajustes_propios_y_los_registra():
    ajustes = {**AJUSTES_POR_DEFECTO, "snap_tolerance": 5}

    tablas = extraer_tablas(PDF_MUESTRA, ajustes=ajustes)

    assert tablas
    assert all(t.ajustes == ajustes for t in tablas)


def test_un_archivo_que_no_es_pdf_lanza_error_de_extraccion(tmp_path: Path):
    falso = tmp_path / "falso.pdf"
    falso.write_bytes(b"%PDF-1.7\n esto no es un pdf valido")

    with pytest.raises(ErrorExtraccion, match="falso.pdf"):
        extraer_tablas(falso)
