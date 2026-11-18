import json
from datetime import UTC, date, datetime
from pathlib import Path

from deltalake import DeltaTable

from prediccion_ganadera.adaptadores.almacen.bronce import AlmacenBronce
from prediccion_ganadera.adaptadores.almacen.plata import AlmacenPlata
from prediccion_ganadera.cli import main
from prediccion_ganadera.herramientas.bronce_a_plata import extraer_bronce_a_plata
from prediccion_ganadera.herramientas.extraer_tablas_pdf import (
    AJUSTES_POR_DEFECTO,
    FilaExtraida,
    TablaExtraida,
)

AHORA = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
UPLOADS = "https://www.subastas.example/wp-content/uploads/"
PDF_MUESTRA = (
    Path(__file__).parent / "fixtures" / "pdfs" / "Resumen-de-subasta-de-ganado-24-07-2026.pdf"
)


def _tabla() -> TablaExtraida:
    return TablaExtraida(
        pagina=2,
        indice=1,
        columnas=2,
        filas=(
            FilaExtraida(indice=0, celdas=("Lote", "Precio")),
            FilaExtraida(indice=3, celdas=("7", None)),
        ),
        ajustes=AJUSTES_POR_DEFECTO,
    )


def _bronce_con_muestra(tmp_path: Path) -> AlmacenBronce:
    bronce = AlmacenBronce(tmp_path / "bronce")
    bronce.guardar(
        UPLOADS + "2026/07/muestra.pdf",
        PDF_MUESTRA.read_bytes(),
        fecha=date(2026, 7, 24),
        especial=False,
        ahora=AHORA,
    )
    return bronce


def test_guarda_una_fila_por_celda_particionada_y_pendiente(tmp_path: Path):
    plata = AlmacenPlata(tmp_path / "plata")

    celdas = plata.guardar(
        sha256="abc", fecha=date(2026, 7, 24), especial=False, tablas=[_tabla()], ahora=AHORA
    )

    assert celdas == 4
    filas = sorted(plata.leer().to_pylist(), key=lambda f: (f["fila"], f["columna"]))
    assert [(f["fila"], f["columna"], f["valor"]) for f in filas] == [
        (0, 0, "Lote"),
        (0, 1, "Precio"),
        (3, 0, "7"),
        (3, 1, None),
    ]
    primera = filas[0]
    assert (primera["sha256"], primera["pagina"], primera["tabla"]) == ("abc", 2, 1)
    assert (primera["anio"], primera["mes"], primera["fecha"]) == (2026, 7, date(2026, 7, 24))
    assert primera["estado_verificacion"] == "pendiente"
    assert json.loads(primera["ajustes"]) == AJUSTES_POR_DEFECTO
    assert DeltaTable(plata.ruta).metadata().partition_columns == ["anio", "mes"]


def test_guardar_es_idempotente_por_sha(tmp_path: Path):
    plata = AlmacenPlata(tmp_path / "plata")
    plata.guardar(sha256="abc", fecha=None, especial=False, tablas=[_tabla()], ahora=AHORA)
    version = DeltaTable(plata.ruta).version()

    repetido = plata.guardar(
        sha256="abc", fecha=None, especial=False, tablas=[_tabla()], ahora=AHORA
    )

    assert repetido == 0
    assert DeltaTable(plata.ruta).version() == version
    assert plata.sha_registrados() == {"abc"}


def test_plata_vacia_no_tiene_registros(tmp_path: Path):
    plata = AlmacenPlata(tmp_path / "plata")

    assert plata.sha_registrados() == set()
    assert plata.leer().num_rows == 0


def test_extrae_los_pdfs_del_manifiesto_una_sola_vez(tmp_path: Path):
    bronce = _bronce_con_muestra(tmp_path)
    plata = AlmacenPlata(tmp_path / "plata")

    primero = extraer_bronce_a_plata(bronce, plata, ahora=AHORA)
    segundo = extraer_bronce_a_plata(bronce, plata, ahora=AHORA)

    assert len(primero.procesados) == 1
    assert primero.tablas == 4
    assert primero.celdas == plata.leer().num_rows > 0
    assert primero.errores == {}
    assert segundo.procesados == []
    assert segundo.omitidos == 1


def test_dos_urls_con_el_mismo_pdf_se_extraen_una_vez(tmp_path: Path):
    bronce = _bronce_con_muestra(tmp_path)
    bronce.guardar(
        UPLOADS + "2026/08/copia.pdf",
        PDF_MUESTRA.read_bytes(),
        fecha=date(2026, 7, 24),
        especial=False,
        ahora=AHORA,
    )

    resumen = extraer_bronce_a_plata(bronce, AlmacenPlata(tmp_path / "plata"), ahora=AHORA)

    assert len(resumen.procesados) == 1


def test_un_pdf_ilegible_no_detiene_el_lote(tmp_path: Path):
    bronce = _bronce_con_muestra(tmp_path)
    roto = bronce.guardar(
        UPLOADS + "roto.pdf", b"%PDF-1.7\n roto", fecha=None, especial=False, ahora=AHORA
    )
    plata = AlmacenPlata(tmp_path / "plata")

    resumen = extraer_bronce_a_plata(bronce, plata, ahora=AHORA)

    assert len(resumen.procesados) == 1
    assert list(resumen.errores) == [roto.sha256]
    assert roto.sha256 not in plata.sha_registrados()


def test_cli_extraer_de_bronce_a_plata(tmp_path: Path, capsys):
    _bronce_con_muestra(tmp_path / "data")
    config = tmp_path / "agentes.yaml"
    config.write_text(
        f"""
version: 1
backends:
  claude: {{tipo: cli_harness, comando: ["claude", "-p", "{{prompt}}"]}}
agentes: {{}}
rutas: {{datos: "{tmp_path / "data"}", reportes: "{tmp_path / "reportes"}"}}
""",
        encoding="utf-8",
    )

    codigo = main(["extraer", "--config", str(config)])

    assert codigo == 0
    salida = capsys.readouterr().out
    assert "1 PDF(s) extraídos" in salida
    assert DeltaTable.is_deltatable(str(tmp_path / "data" / "plata" / "tablas_extraidas"))
