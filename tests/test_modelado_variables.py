from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest

from prediccion_ganadera.adaptadores.almacen.plata import AlmacenPlata, ClaveTabla
from prediccion_ganadera.adaptadores.almacen.postgres import cargar_plata, tablas_staging
from prediccion_ganadera.herramientas.extraer_tablas_pdf import (
    AJUSTES_POR_DEFECTO,
    FilaExtraida,
    TablaExtraida,
    extraer_tablas,
)
from prediccion_ganadera.modelado.variables import (
    es_resumen_general,
    precios_de_resumen,
    precios_desde_tablas,
)

PDF = Path(__file__).parent / "fixtures" / "pdfs" / "Resumen-de-subasta-de-ganado-24-07-2026.pdf"
FECHA = date(2026, 7, 24)
AHORA = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64


@pytest.fixture(scope="module")
def tablas_pdf() -> list[TablaExtraida]:
    return extraer_tablas(PDF)


def _tabla(*filas: tuple[str | None, ...], pagina: int = 1, indice: int = 1) -> TablaExtraida:
    return TablaExtraida(
        pagina=pagina,
        indice=indice,
        columnas=max(len(f) for f in filas),
        filas=tuple(FilaExtraida(indice=i, celdas=f) for i, f in enumerate(filas)),
        ajustes=AJUSTES_POR_DEFECTO,
    )


ENCABEZADO = ("resumen", "_subasta_tipo", "", "RESUMEN", "GENERAL D", "E LA FERIA", "", "")
HEMBRAS = ("GANAD", "O HEMBRA DE PRIMER", "A", "", "", "", "", "")
MACHOS = ("GANAD", "O MACHO DE PRIMERA", "", "", "", "", "", "")


def test_detecta_solo_la_tabla_de_resumen_general(tablas_pdf):
    assert [es_resumen_general(t) for t in tablas_pdf] == [True, False, False, False]


# Resumen cuyo título quedó por encima de la tabla: solo trae el encabezado de la feria.
SIN_TITULO = _tabla(
    ("Feria: 0000 Fecha: 01/01/2026", "", "", "", "", "", "", ""),
    ("Coordinador: XXXX", "", "", "", "", "", "", ""),
    HEMBRAS,
    ("CATEGORIA", "", "ANIMALES", "PESO", "MINIMO", "MAXIMO", "PROMEDIO", "VALOR"),
    ("VACA HO", "RRA", "10", "400", "7,000", "8,000", "7,500", "3,000,000"),
    ("NOVILLA DE", "VIENTRE", "5", "300", "8,000", "9,000", "8,500", "2,550,000"),
)


def test_reconoce_el_resumen_por_sus_secciones_aunque_falte_el_titulo(tmp_path):
    plata = AlmacenPlata(tmp_path / "plata")
    plata.guardar(sha256=SHA_A, fecha=FECHA, especial=False, tablas=[SIN_TITULO], ahora=AHORA)

    resultado = precios_desde_tablas(plata.tablas(), estados={"pendiente"})

    assert es_resumen_general(SIN_TITULO)
    assert [p.categoria for p in resultado.precios] == ["NOVILLA DE VIENTRE", "VACA HORRA"]
    assert resultado.tablas_resumen == 1


def test_ganado_a_mitad_de_una_fila_de_lotes_no_es_resumen():
    lotes = _tabla(
        ("LOTE", "VENDEDOR", "DESCRIPCION", "", "", "", "", ""),
        ("12", "XXXX", "GANADO HEMBRA DE PRIMERA", "", "", "", "", ""),
        ("13", "XXXX", "LOTE DE GANADO MACHO", "", "", "", "", ""),
    )

    assert not es_resumen_general(lotes)


def test_lee_las_categorias_del_resumen_del_pdf_de_prueba(tablas_pdf):
    resultado = precios_de_resumen(tablas_pdf[0], fecha=FECHA, especial=False, sha256=SHA_A)

    assert resultado.descartes == []
    por_categoria = {p.categoria: p for p in resultado.precios}
    assert list(por_categoria) == [
        "BUFALA HORRA",
        "HEMBRA LEVANTE",
        "NOVILLA DE VIENTRE",
        "VACA HORRA",
        "VACA PARIDA",
        "MULETO",
        "MACHO LEVANTE",
        "MACHO",
        "MACHO DE CEBA",
    ]
    vaca = por_categoria["VACA HORRA"]
    assert (vaca.fecha, vaca.seccion, vaca.unidad) == (FECHA, "hembra", "kg")
    assert (vaca.animales, vaca.peso_cabeza) == (55, 422)
    assert (vaca.precio_minimo, vaca.precio_maximo, vaca.precio_promedio) == (6500, 9000, 7805)
    assert vaca.valor_animal == 3_291_581
    assert por_categoria["MACHO DE CEBA"].seccion == "macho"
    # Los machos enteros y muletos se rematan por cabeza (peso 1, precio = valor del animal).
    assert por_categoria["MULETO"].unidad == "cabeza"
    assert por_categoria["MACHO"].unidad == "cabeza"


def test_descarta_filas_inconsistentes_sin_inventar_valores():
    tabla = _tabla(
        ENCABEZADO,
        HEMBRAS,
        ("VACA HO", "RRA", "55", "422", "6,500", "9,000", "7,805", "3,291,581"),
        # promedio fuera del rango mínimo-máximo
        ("VACA PA", "RIDA", "19", "430", "6,000", "5,500", "9,104", "3,914,720"),
        # valor por animal que no cuadra con peso × precio (un número partido, por ejemplo)
        ("NOVILLA", "DE VIENTRE", "29", "313", "8,000", "9,850", "8,840", "2,77"),
        # fila incompleta: faltan columnas numéricas
        ("HEMBRA", "LEVANTE", "94", "206", "7,450"),
        ("TO", "TAL:", "209", "", "", "", "", ""),
    )

    resultado = precios_de_resumen(tabla, fecha=FECHA, especial=False, sha256=SHA_A)

    assert [p.categoria for p in resultado.precios] == ["VACA HORRA"]
    assert [(d.categoria, d.motivo) for d in resultado.descartes] == [
        ("VACA PARIDA", "precio_fuera_de_rango"),
        ("NOVILLA DE VIENTRE", "valor_inconsistente"),
        ("HEMBRA LEVANTE", "fila_incompleta"),
    ]


def test_categoria_desconocida_conserva_su_nombre_compacto():
    tabla = _tabla(
        ENCABEZADO,
        MACHOS,
        ("TORO", "REPRO", "DUCTOR", "2", "500", "9,000", "9,000", "9,000", "4,500,000"),
    )

    (precio,) = precios_de_resumen(tabla, fecha=FECHA, especial=True, sha256=SHA_A).precios

    assert (precio.categoria, precio.seccion, precio.especial) == ("TOROREPRODUCTOR", "macho", True)


def _plata(tmp_path: Path, tablas_pdf: list[TablaExtraida]) -> AlmacenPlata:
    plata = AlmacenPlata(tmp_path / "plata")
    plata.guardar(sha256=SHA_A, fecha=FECHA, especial=False, tablas=tablas_pdf, ahora=AHORA)
    otra = _tabla(
        ENCABEZADO, HEMBRAS, ("VACA HO", "RRA", "10", "400", "7,000", "8,000", "7,500", "3,000,000")
    )
    plata.guardar(sha256=SHA_B, fecha=date(2026, 7, 31), especial=False, tablas=[otra], ahora=AHORA)
    return plata


def test_desde_plata_usa_solo_tablas_validadas_por_defecto(tmp_path, tablas_pdf):
    plata = _plata(tmp_path, tablas_pdf)
    plata.marcar_estado(ClaveTabla(sha256=SHA_A, pagina=1, tabla=1), "corregida")

    validadas = precios_desde_tablas(plata.tablas())
    todas = precios_desde_tablas(plata.tablas(), estados={"pendiente", "verificada", "corregida"})

    assert validadas.fechas == [FECHA]
    assert len(validadas.precios) == 9
    assert validadas.tablas_resumen == 1
    assert validadas.tablas_omitidas == 1  # la del 31-07 sigue pendiente
    assert todas.fechas == [FECHA, date(2026, 7, 31)]


def test_misma_fecha_en_dos_pdfs_no_duplica_categorias(tmp_path, tablas_pdf):
    plata = AlmacenPlata(tmp_path / "plata")
    plata.guardar(sha256=SHA_A, fecha=FECHA, especial=False, tablas=tablas_pdf[:1], ahora=AHORA)
    plata.guardar(sha256=SHA_B, fecha=FECHA, especial=False, tablas=tablas_pdf[:1], ahora=AHORA)

    resultado = precios_desde_tablas(plata.tablas(), estados={"pendiente"})

    assert len(resultado.precios) == 9
    assert {d.motivo for d in resultado.descartes} == {"duplicada"}
    assert all(d.sha256 == SHA_B for d in resultado.descartes)


def test_desde_staging_lee_solo_lo_validado(tmp_path, tablas_pdf, postgres_dsn):
    plata = _plata(tmp_path, tablas_pdf)
    plata.marcar_estado(ClaveTabla(sha256=SHA_A, pagina=1, tabla=1), "verificada")
    with psycopg.connect(postgres_dsn) as conexion:
        cargar_plata(plata, conexion, ahora=AHORA)
        tablas = tablas_staging(conexion)

    assert [(t.clave.sha256, t.clave.pagina, t.estado) for t in tablas] == [
        (SHA_A, 1, "verificada")
    ]
    desde_staging = precios_desde_tablas(tablas)
    plata_validada = precios_desde_tablas(plata.tablas())
    assert desde_staging.precios == plata_validada.precios
