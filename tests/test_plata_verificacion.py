from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from deltalake import DeltaTable

from prediccion_ganadera.adaptadores.almacen.plata import AlmacenPlata, ClaveTabla
from prediccion_ganadera.adaptadores.almacen.revision import ColaRevision, EntradaRevision
from prediccion_ganadera.herramientas.extraer_tablas_pdf import (
    AJUSTES_POR_DEFECTO,
    FilaExtraida,
    TablaExtraida,
)

AHORA = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
DESPUES = datetime(2026, 11, 18, 13, 0, tzinfo=UTC)


def _tabla(pagina: int, indice: int, *filas: tuple[str | None, ...], ajustes=None):
    return TablaExtraida(
        pagina=pagina,
        indice=indice,
        columnas=max(len(f) for f in filas),
        filas=tuple(FilaExtraida(indice=i * 2, celdas=f) for i, f in enumerate(filas)),
        ajustes=ajustes or AJUSTES_POR_DEFECTO,
    )


@pytest.fixture
def plata(tmp_path: Path) -> AlmacenPlata:
    plata = AlmacenPlata(tmp_path / "plata")
    plata.guardar(
        sha256="aa",
        fecha=date(2026, 7, 24),
        especial=False,
        tablas=[_tabla(1, 1, ("Lote", "Precio"), ("7", None)), _tabla(2, 1, ("x", "1", "2"))],
        ahora=AHORA,
    )
    plata.guardar(
        sha256="bb", fecha=None, especial=True, tablas=[_tabla(1, 1, ("y",))], ahora=AHORA
    )
    return plata


def test_lista_las_tablas_por_estado(plata: AlmacenPlata):
    assert plata.claves_por_estado("pendiente") == [
        ClaveTabla(sha256="aa", pagina=1, tabla=1),
        ClaveTabla(sha256="aa", pagina=2, tabla=1),
        ClaveTabla(sha256="bb", pagina=1, tabla=1),
    ]
    assert plata.claves_por_estado("verificada") == []


def test_lee_una_tabla_tal_como_se_guardo(plata: AlmacenPlata):
    leida = plata.leer_tabla(ClaveTabla(sha256="aa", pagina=1, tabla=1))

    assert leida.fecha == date(2026, 7, 24)
    assert leida.especial is False
    assert leida.estado == "pendiente"
    assert leida.tabla == _tabla(1, 1, ("Lote", "Precio"), ("7", None))


def test_leer_una_tabla_inexistente_es_un_error(plata: AlmacenPlata):
    with pytest.raises(KeyError):
        plata.leer_tabla(ClaveTabla(sha256="aa", pagina=9, tabla=1))


def test_marca_el_estado_de_una_sola_tabla(plata: AlmacenPlata):
    plata.marcar_estado(ClaveTabla(sha256="aa", pagina=2, tabla=1), "verificada")

    assert plata.claves_por_estado("verificada") == [ClaveTabla(sha256="aa", pagina=2, tabla=1)]
    assert len(plata.claves_por_estado("pendiente")) == 2


def test_estado_desconocido_es_un_error(plata: AlmacenPlata):
    with pytest.raises(ValueError, match="estado"):
        plata.marcar_estado(ClaveTabla(sha256="aa", pagina=2, tabla=1), "aprobada")


def test_reemplaza_una_tabla_en_un_solo_commit_sin_tocar_las_demas(plata: AlmacenPlata):
    clave = ClaveTabla(sha256="aa", pagina=1, tabla=1)
    nueva = _tabla(1, 1, ("Lote", "Precio", "Peso"), ajustes={"vertical_strategy": "lines"})
    version = DeltaTable(plata.ruta).version()

    plata.reemplazar_tabla(clave, nueva, estado="corregida", ahora=DESPUES)

    assert DeltaTable(plata.ruta).version() == version + 1
    leida = plata.leer_tabla(clave)
    assert leida.tabla == nueva
    assert (leida.estado, leida.fecha, leida.especial) == ("corregida", date(2026, 7, 24), False)
    assert plata.leer_tabla(ClaveTabla(sha256="aa", pagina=2, tabla=1)).estado == "pendiente"
    assert plata.leer().num_rows == 3 + 3 + 1


def test_reemplazar_exige_que_la_tabla_coincida_con_la_clave(plata: AlmacenPlata):
    with pytest.raises(ValueError, match="clave"):
        plata.reemplazar_tabla(
            ClaveTabla(sha256="aa", pagina=1, tabla=1), _tabla(2, 1, ("z",)), "corregida", AHORA
        )


def test_la_clave_rechaza_sha_que_no_es_hexadecimal():
    with pytest.raises(ValueError):
        ClaveTabla(sha256="aa' OR '1'='1", pagina=1, tabla=1)


def _entrada(sha: str, motivo: str, intentos: int = 3) -> EntradaRevision:
    return EntradaRevision(
        clave=ClaveTabla(sha256=sha, pagina=1, tabla=1),
        motivo=motivo,
        hallazgos=["total_faltante: GRAN TOTAL 389"],
        intentos=intentos,
        creado_en=AHORA,
    )


def test_cola_de_revision_vacia(tmp_path: Path):
    assert ColaRevision(tmp_path / "plata").entradas() == []


def test_cola_de_revision_guarda_una_entrada_por_tabla(tmp_path: Path):
    cola = ColaRevision(tmp_path / "plata")

    cola.agregar(_entrada("aa", "primera"))
    cola.agregar(_entrada("bb", "otra"))
    cola.agregar(_entrada("aa", "segunda", intentos=4))

    entradas = sorted(cola.entradas(), key=lambda e: e.clave.sha256)
    assert [(e.clave.sha256, e.motivo, e.intentos) for e in entradas] == [
        ("aa", "segunda", 4),
        ("bb", "otra", 3),
    ]
    assert entradas[0].hallazgos == ["total_faltante: GRAN TOTAL 389"]


def test_cola_de_revision_quita_una_tabla_resuelta(tmp_path: Path):
    cola = ColaRevision(tmp_path / "plata")
    cola.agregar(_entrada("aa", "primera"))
    cola.agregar(_entrada("bb", "otra"))

    cola.quitar(ClaveTabla(sha256="aa", pagina=1, tabla=1))
    cola.quitar(ClaveTabla(sha256="cc", pagina=1, tabla=1))  # no estaba: no falla

    assert [e.clave.sha256 for e in cola.entradas()] == ["bb"]
