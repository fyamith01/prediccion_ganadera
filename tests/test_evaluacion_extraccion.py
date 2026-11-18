"""Exactitud de extracción contra una muestra revisada a mano (T9a)."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from prediccion_ganadera.adaptadores.almacen.plata import AlmacenPlata
from prediccion_ganadera.evaluacion.extraccion import (
    ErrorMuestra,
    MuestraRevisada,
    cargar_muestra,
    combinar,
    evaluar_extraccion,
    tablas_de_plata,
    tablas_del_extractor,
)
from prediccion_ganadera.herramientas.extraer_tablas_pdf import FilaExtraida, TablaExtraida

PDF = Path(__file__).parent / "fixtures" / "pdfs" / "Resumen-de-subasta-de-ganado-24-07-2026.pdf"
SHA = hashlib.sha256(PDF.read_bytes()).hexdigest()


def _tabla(pagina: int, filas: list[list[str]], indice: int = 1) -> TablaExtraida:
    return TablaExtraida(
        pagina=pagina,
        indice=indice,
        columnas=max(len(f) for f in filas),
        filas=tuple(FilaExtraida(indice=i, celdas=tuple(f)) for i, f in enumerate(filas)),
        ajustes={},
    )


def _muestra(**cambios) -> MuestraRevisada:
    base = {
        "pdf": "subasta.pdf",
        "sha256": "ab" * 32,
        "fecha": date(2026, 7, 24),
        "estado": "borrador",
        "paginas": [
            {
                "pagina": 1,
                "completa": True,
                "filas": [
                    "VACA HORRA 55 422 6,500 9,000 7,805 3,291,581",
                    "VACA PARIDA 19 430 6,000 15,500 9,104 3,914,720",
                    "TOTAL: 74",
                ],
            }
        ],
        "precios": [
            {
                "categoria": "VACA HORRA",
                "animales": 55,
                "peso_cabeza": 422,
                "precio_minimo": 6500,
                "precio_maximo": 9000,
                "precio_promedio": 7805,
                "valor_animal": 3291581,
            },
            {
                "categoria": "VACA PARIDA",
                "animales": 19,
                "peso_cabeza": 430,
                "precio_minimo": 6000,
                "precio_maximo": 15500,
                "precio_promedio": 9104,
                "valor_animal": 3914720,
            },
        ],
    }
    return MuestraRevisada.model_validate({**base, **cambios})


RESUMEN_PERFECTO = [
    ["resumen", "RESUMEN", "GENERAL D", "E LA FERIA"],
    ["GANAD", "O HEMBRA DE PRIMER", "A"],
    ["VACA HO", "RRA", "55", "422", "6,500", "9,000", "7,805", "3,291,581"],
    ["VACA PA", "RIDA", "19", "430", "6,000", "15,500", "9,104", "3,914,720"],
    ["TO", "TAL:", "74"],
]


def test_extraccion_perfecta_da_todo_uno():
    evaluacion = evaluar_extraccion(_muestra(), [_tabla(1, RESUMEN_PERFECTO)], fuente="extractor")

    assert evaluacion.filas.esperadas == 3
    assert evaluacion.filas.encontradas == 3
    assert evaluacion.filas.recall == 1.0
    assert evaluacion.filas.precision == 1.0
    assert evaluacion.filas.exactitud_valores == 1.0
    assert evaluacion.precios.exactitud_campos == 1.0
    assert evaluacion.precios.faltantes == []
    assert evaluacion.revisada is False  # un borrador nunca se presenta como revisado


def test_fila_faltante_baja_el_recall_y_se_detalla():
    tablas = [_tabla(1, RESUMEN_PERFECTO[:-1])]  # sin el renglón de total

    evaluacion = evaluar_extraccion(_muestra(), tablas, fuente="extractor")

    assert evaluacion.filas.encontradas == 2
    assert evaluacion.filas.recall == pytest.approx(2 / 3)
    assert evaluacion.filas.precision == 1.0
    assert any("TOTAL: 74" in d for d in evaluacion.detalles)


def test_cifra_equivocada_cuenta_en_filas_valores_y_precios():
    filas = [list(f) for f in RESUMEN_PERFECTO]
    filas[2][6] = "7,850"  # promedio mal leído (7,805 en el PDF)

    evaluacion = evaluar_extraccion(_muestra(), [_tabla(1, filas)], fuente="extractor")

    assert evaluacion.filas.encontradas == 2
    assert evaluacion.filas.precision == pytest.approx(2 / 3)
    # 6 + 6 + 1 cifras esperadas; solo una está mal
    assert evaluacion.filas.valores_esperados == 13
    assert evaluacion.filas.valores_correctos == 12
    # 2 categorías × 6 campos; un campo mal
    assert evaluacion.precios.campos_esperados == 12
    assert evaluacion.precios.campos_correctos == 11
    assert any("VACA HORRA" in d and "precio_promedio" in d for d in evaluacion.detalles)


def test_categoria_descartada_por_el_parser_es_faltante():
    filas = [list(f) for f in RESUMEN_PERFECTO]
    filas[3] = ["VACA PA", "RIDA", "19", "430", "6,000"]  # fila incompleta: se descarta

    evaluacion = evaluar_extraccion(_muestra(), [_tabla(1, filas)], fuente="extractor")

    assert evaluacion.precios.faltantes == ["VACA PARIDA"]
    assert evaluacion.precios.campos_correctos == 6


def test_precision_solo_en_paginas_completas():
    muestra = _muestra(
        paginas=[
            {"pagina": 2, "completa": False, "filas": ["8 1 ML 250 250 10:26 X 9,000 81 9,450 0 0"]}
        ],
        precios=[],
    )
    tablas = [
        _tabla(2, [["8 1 ML", "250", "250", "10:26 X", "9,000", "81", "9,450", "0", "0"],
                   ["9 2 HL", "285", "143", "06:29 Y", "8,000", "81", "7,450", "0", "0"]]),
    ]  # fmt: skip

    evaluacion = evaluar_extraccion(muestra, tablas, fuente="extractor")

    assert evaluacion.filas.recall == 1.0
    assert evaluacion.filas.precision is None  # la otra fila no está en la muestra
    assert evaluacion.precios.exactitud_campos is None  # la muestra no trae precios


def test_muestra_revisada_exige_revisor_y_fecha():
    with pytest.raises(ValueError, match="revisado_por"):
        _muestra(estado="revisada")

    revisada = _muestra(estado="revisada", revisado_por="Fabián", revisado_en=date(2026, 11, 18))
    assert revisada.estado == "revisada"


def test_cargar_muestra_desde_yaml(tmp_path):
    ruta = tmp_path / "m.yaml"
    ruta.write_text(
        f"pdf: x.pdf\nsha256: {'cd' * 32}\nfecha: 2026-07-24\nestado: borrador\n"
        "paginas:\n  - pagina: 1\n    completa: true\n    filas: ['A 1 2 3']\n",
        encoding="utf-8",
    )
    assert cargar_muestra(ruta).paginas[0].filas == ["A 1 2 3"]

    ruta.write_text("pdf: x.pdf\n", encoding="utf-8")
    with pytest.raises(ErrorMuestra, match=r"m\.yaml"):
        cargar_muestra(ruta)


def test_tablas_del_extractor_comprueba_el_hash(tmp_path):
    tablas = tablas_del_extractor(PDF, SHA)
    assert [t.pagina for t in tablas] == [1, 2, 3, 4]

    with pytest.raises(ErrorMuestra, match="SHA-256"):
        tablas_del_extractor(PDF, "ef" * 32)


def test_pdf_real_contra_muestra_construida_en_la_prueba():
    """El extractor deja fuera el «GRAN TOTAL» de la página 1: la métrica lo debe ver."""
    muestra = _muestra(
        sha256=SHA,
        paginas=[
            {
                "pagina": 1,
                "completa": True,
                "filas": [
                    "BUFALA HORRA 12 192 10,550 10,550 10,550 2,022,083",
                    "TOTAL: 209",
                    "GRAN TOTAL 389",
                ],
            }
        ],
        precios=[],
    )
    evaluacion = evaluar_extraccion(muestra, tablas_del_extractor(PDF, SHA), fuente="extractor")

    assert evaluacion.filas.encontradas == 2
    assert any("GRAN TOTAL 389" in d for d in evaluacion.detalles)


def test_tablas_de_plata_filtra_por_sha(tmp_path):
    plata = AlmacenPlata(tmp_path / "plata")
    tablas = tablas_del_extractor(PDF, SHA)
    plata.guardar(
        sha256=SHA, fecha=date(2026, 7, 24), especial=False, tablas=tablas,
        ahora=datetime(2026, 11, 18, tzinfo=UTC),
    )  # fmt: skip

    assert [t.pagina for t in tablas_de_plata(plata, SHA)] == [1, 2, 3, 4]
    with pytest.raises(ErrorMuestra, match="plata"):
        tablas_de_plata(plata, "ef" * 32)


def test_combinar_suma_varias_muestras():
    buena = evaluar_extraccion(_muestra(), [_tabla(1, RESUMEN_PERFECTO)], fuente="extractor")
    mala = evaluar_extraccion(_muestra(), [_tabla(1, RESUMEN_PERFECTO[:-1])], fuente="extractor")

    total = combinar([buena, mala])

    assert total.filas.esperadas == 6
    assert total.filas.encontradas == 5
    assert total.muestras == 2
    assert total.revisadas == 0
