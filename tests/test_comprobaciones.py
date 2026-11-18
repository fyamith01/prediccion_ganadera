from pathlib import Path

from prediccion_ganadera.herramientas.comprobaciones import comprobar_pagina, numeros
from prediccion_ganadera.herramientas.extraer_tablas_pdf import (
    AJUSTES_POR_DEFECTO,
    FilaExtraida,
    TablaExtraida,
    extraer_tablas,
)
from prediccion_ganadera.herramientas.paginas import lineas_de_pagina

PDF_MUESTRA = (
    Path(__file__).parent / "fixtures" / "pdfs" / "Resumen-de-subasta-de-ganado-24-07-2026.pdf"
)

LINEAS = [
    "RESUMEN GENERAL DE LA FERIA",
    "Feria: 1369 Fecha: 24/07/2026",
    "Edad Animales Peso Precio",
    "VACA HORRA 55 422 6,500 9,000",
    "VACA PARIDA 19 430 6,000 15,500",
    "TOTAL: 74",
    "GRAN TOTAL 74",
]


def _tabla(*filas: tuple[str | None, ...], indice: int = 1) -> TablaExtraida:
    return TablaExtraida(
        pagina=1,
        indice=indice,
        columnas=max(len(f) for f in filas),
        filas=tuple(FilaExtraida(indice=i * 2, celdas=f) for i, f in enumerate(filas)),
        ajustes=AJUSTES_POR_DEFECTO,
    )


COMPLETA = (
    ("RESUMEN", "GENERAL D", "E LA FERIA", None),
    ("Edad", "Animales", "Peso", "Precio"),
    ("VACA HO", "RRA", "55", "422 6,500 9,000"),
    ("VACA PA", "RIDA 19", "430", "6,000 15,500"),
    ("TO", "TAL:", "74", ""),
    ("GRAN", "TOTAL", "74", ""),
)


def test_numeros_reconoce_enteros_con_separador_de_miles():
    assert numeros("8 1 ML 1,755 10:26 24/07/2026 2.2.5 9,000 0") == (8, 1, 1755, 9000, 0)


def test_tabla_fiel_a_la_pagina_no_tiene_hallazgos():
    assert comprobar_pagina([_tabla(*COMPLETA)], LINEAS) == {1: []}


def test_fila_de_datos_que_falta_en_la_tabla():
    tabla = _tabla(*COMPLETA[:3], *COMPLETA[4:])

    hallazgos = comprobar_pagina([tabla], LINEAS)[1]

    assert [h.tipo for h in hallazgos] == ["fila_faltante", "total_incorrecto"]
    assert "VACA PARIDA 19 430 6,000 15,500" in hallazgos[0].detalle


def test_fila_con_valores_distintos_a_la_pagina():
    tabla = _tabla(*COMPLETA[:3], ("VACA PA", "RIDA 19", "480", "6,000 15,500"), *COMPLETA[4:])

    tipos = sorted(h.tipo for h in comprobar_pagina([tabla], LINEAS)[1])

    assert tipos == ["fila_distinta", "fila_faltante"]


def test_fila_de_total_que_falta_en_la_tabla():
    hallazgos = comprobar_pagina([_tabla(*COMPLETA[:-1])], LINEAS)[1]

    assert [h.tipo for h in hallazgos] == ["total_faltante"]
    assert "GRAN TOTAL 74" in hallazgos[0].detalle


def test_total_que_no_cuadra_con_las_filas_del_bloque():
    lineas = [*LINEAS[:5], "TOTAL: 75", "GRAN TOTAL 74"]
    tabla = _tabla(*COMPLETA[:4], ("TO", "TAL:", "75", ""), COMPLETA[5])

    hallazgos = comprobar_pagina([tabla], lineas)[1]

    assert [h.tipo for h in hallazgos] == ["total_incorrecto"]
    assert "75" in hallazgos[0].detalle and "74" in hallazgos[0].detalle


def test_las_filas_faltantes_se_reportan_en_cada_tabla_de_la_pagina():
    arriba = _tabla(*COMPLETA[:3], indice=1)
    abajo = _tabla(*COMPLETA[4:], indice=2)

    resultado = comprobar_pagina([arriba, abajo], LINEAS)

    assert [h.tipo for h in resultado[1]] == ["fila_faltante"]
    assert [h.tipo for h in resultado[2]] == ["fila_faltante", "total_incorrecto"]


def test_pdf_de_muestra():
    tablas = extraer_tablas(PDF_MUESTRA)
    por_pagina = {
        p: comprobar_pagina([t for t in tablas if t.pagina == p], lineas_de_pagina(PDF_MUESTRA, p))
        for p in (1, 2, 3)
    }

    # La tabla de la página 1 deja fuera la línea "GRAN TOTAL 389"; los totales 209 y 160
    # cuadran con sus bloques. Las páginas de lotes coinciden con el texto.
    assert [(h.tipo, "389" in h.detalle) for h in por_pagina[1][1]] == [("total_faltante", True)]
    assert por_pagina[2] == {1: []}
    assert por_pagina[3] == {1: []}
