"""Las muestras versionadas en `evaluacion/muestras/` son válidas y apuntan a su PDF (T9b)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from prediccion_ganadera.evaluacion.extraccion import cargar_muestra
from prediccion_ganadera.herramientas.comprobaciones import numeros

RAIZ = Path(__file__).parent.parent
MUESTRAS = sorted((RAIZ / "evaluacion" / "muestras").glob("*.yaml"))


def test_hay_al_menos_una_muestra():
    assert MUESTRAS


@pytest.mark.parametrize("ruta", MUESTRAS, ids=lambda r: r.name)
def test_muestra_valida_y_ligada_a_su_pdf(ruta):
    muestra = cargar_muestra(ruta)

    assert muestra.ruta, "las muestras versionadas traen la ruta del PDF en el repositorio"
    pdf = RAIZ / muestra.ruta
    assert hashlib.sha256(pdf.read_bytes()).hexdigest() == muestra.sha256
    for pagina in muestra.paginas:
        assert all(numeros(fila) for fila in pagina.filas), pagina.pagina


def test_muestra_de_la_subasta_del_24_de_julio_sigue_la_regla_acordada():
    """Página 1 completa + 10 lotes (cada 8.º lote en orden de página, desde el primero)."""
    muestra = cargar_muestra(RAIZ / "evaluacion" / "muestras" / "subasta_2026-07-24.yaml")

    por_pagina = {p.pagina: p for p in muestra.paginas}
    assert por_pagina[1].completa
    assert len(por_pagina[1].filas) == 9 + 3  # 9 categorías, 2 totales de bloque, gran total
    lotes = [f for p in muestra.paginas if p.pagina > 1 for f in p.filas]
    assert [numeros(f)[0] for f in lotes] == [8, 24, 33, 41, 49, 57, 74, 82, 90, 120]
    assert not any(p.completa for p in muestra.paginas if p.pagina > 1)
    assert len(muestra.precios) == 9
