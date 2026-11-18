from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest

from prediccion_ganadera.adaptadores.almacen.plata import AlmacenPlata, ClaveTabla
from prediccion_ganadera.adaptadores.almacen.postgres import (
    ResumenCarga,
    cargar_plata,
    crear_esquemas,
    huella_tabla,
)
from prediccion_ganadera.herramientas.extraer_tablas_pdf import (
    AJUSTES_POR_DEFECTO,
    FilaExtraida,
    TablaExtraida,
)

AHORA = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
DESPUES = datetime(2026, 11, 18, 13, 0, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64


def _tabla(pagina: int, indice: int, *filas: tuple[str | None, ...]) -> TablaExtraida:
    return TablaExtraida(
        pagina=pagina,
        indice=indice,
        columnas=max(len(f) for f in filas),
        filas=tuple(FilaExtraida(indice=i, celdas=f) for i, f in enumerate(filas)),
        ajustes=AJUSTES_POR_DEFECTO,
    )


@pytest.fixture
def plata(tmp_path: Path) -> AlmacenPlata:
    plata = AlmacenPlata(tmp_path / "plata")
    plata.guardar(
        sha256=SHA_A,
        fecha=date(2026, 7, 24),
        especial=False,
        tablas=[
            _tabla(1, 1, ("Lote", "Precio"), ("7", None)),
            _tabla(2, 1, ("x", "1", "2")),
        ],
        ahora=AHORA,
    )
    plata.guardar(
        sha256=SHA_B, fecha=None, especial=True, tablas=[_tabla(1, 1, ("y",))], ahora=AHORA
    )
    plata.marcar_estado(ClaveTabla(sha256=SHA_A, pagina=1, tabla=1), "verificada")
    return plata


def _filas(dsn: str, sql: str) -> list[tuple]:
    with psycopg.connect(dsn) as conexion:
        return conexion.execute(sql).fetchall()


def test_crea_los_esquemas_de_forma_idempotente(postgres_dsn: str):
    with psycopg.connect(postgres_dsn) as conexion:
        crear_esquemas(conexion)
        crear_esquemas(conexion)

    esquemas = {
        f[0]
        for f in _filas(
            postgres_dsn,
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name IN ('raw', 'staging', 'marts')",
        )
    }
    relaciones = set(
        _filas(
            postgres_dsn,
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema IN ('raw', 'staging', 'marts')",
        )
    )
    assert esquemas == {"raw", "staging", "marts"}
    assert relaciones == {
        ("raw", "tablas"),
        ("raw", "celdas"),
        ("staging", "tablas"),
        ("staging", "celdas"),
        ("marts", "cobertura_subastas"),
    }


def test_raw_recibe_todas_las_tablas_y_staging_solo_las_validadas(
    plata: AlmacenPlata, postgres_dsn: str
):
    with psycopg.connect(postgres_dsn) as conexion:
        resumen = cargar_plata(plata, conexion, ahora=AHORA)

    assert resumen == ResumenCarga(
        tablas_nuevas=3, tablas_actualizadas=0, tablas_sin_cambios=0, tablas_en_staging=1, celdas=8
    )
    assert _filas(
        postgres_dsn,
        "SELECT sha256, pagina, tabla, fecha, especial, estado_verificacion, filas, columnas "
        "FROM raw.tablas ORDER BY sha256, pagina",
    ) == [
        (SHA_A, 1, 1, date(2026, 7, 24), False, "verificada", 2, 2),
        (SHA_A, 2, 1, date(2026, 7, 24), False, "pendiente", 1, 3),
        (SHA_B, 1, 1, None, True, "pendiente", 1, 1),
    ]
    assert _filas(
        postgres_dsn,
        f"SELECT fila, columna, valor FROM raw.celdas WHERE sha256 = '{SHA_A}' AND pagina = 1 "
        "ORDER BY fila, columna",
    ) == [(0, 0, "Lote"), (0, 1, "Precio"), (1, 0, "7"), (1, 1, None)]
    assert _filas(postgres_dsn, "SELECT sha256, pagina, tabla FROM staging.tablas") == [
        (SHA_A, 1, 1)
    ]
    assert _filas(postgres_dsn, "SELECT count(*) FROM staging.celdas") == [(4,)]


def test_una_segunda_carga_sin_cambios_no_reescribe_nada(plata: AlmacenPlata, postgres_dsn: str):
    with psycopg.connect(postgres_dsn) as conexion:
        cargar_plata(plata, conexion, ahora=AHORA)
    with psycopg.connect(postgres_dsn) as conexion:
        resumen = cargar_plata(plata, conexion, ahora=DESPUES)

    assert resumen == ResumenCarga(
        tablas_nuevas=0, tablas_actualizadas=0, tablas_sin_cambios=3, tablas_en_staging=1, celdas=0
    )
    assert _filas(postgres_dsn, "SELECT DISTINCT cargado_en FROM raw.tablas") == [(AHORA,)]
    assert _filas(postgres_dsn, "SELECT count(*) FROM raw.celdas") == [(8,)]


def test_una_tabla_rectificada_reemplaza_sus_celdas_y_entra_a_staging(
    plata: AlmacenPlata, postgres_dsn: str
):
    with psycopg.connect(postgres_dsn) as conexion:
        cargar_plata(plata, conexion, ahora=AHORA)
    clave = ClaveTabla(sha256=SHA_A, pagina=2, tabla=1)
    plata.reemplazar_tabla(clave, _tabla(2, 1, ("x", "10")), "corregida", DESPUES)

    with psycopg.connect(postgres_dsn) as conexion:
        resumen = cargar_plata(plata, conexion, ahora=DESPUES)

    assert resumen == ResumenCarga(
        tablas_nuevas=0, tablas_actualizadas=1, tablas_sin_cambios=2, tablas_en_staging=2, celdas=2
    )
    consulta = (
        "SELECT fila, columna, valor FROM {}.celdas "
        f"WHERE sha256 = '{SHA_A}' AND pagina = 2 ORDER BY fila, columna"
    )
    assert _filas(postgres_dsn, consulta.format("raw")) == [(0, 0, "x"), (0, 1, "10")]
    assert _filas(postgres_dsn, consulta.format("staging")) == [(0, 0, "x"), (0, 1, "10")]
    assert _filas(
        postgres_dsn,
        f"SELECT estado_verificacion, cargado_en FROM raw.tablas WHERE sha256 = '{SHA_A}' "
        "AND pagina = 2",
    ) == [("corregida", DESPUES)]


def test_una_tabla_que_deja_de_estar_validada_sale_de_staging(
    plata: AlmacenPlata, postgres_dsn: str
):
    with psycopg.connect(postgres_dsn) as conexion:
        cargar_plata(plata, conexion, ahora=AHORA)
    plata.marcar_estado(ClaveTabla(sha256=SHA_A, pagina=1, tabla=1), "en_revision")

    with psycopg.connect(postgres_dsn) as conexion:
        resumen = cargar_plata(plata, conexion, ahora=DESPUES)

    assert resumen.tablas_actualizadas == 1
    assert resumen.tablas_en_staging == 0
    assert _filas(postgres_dsn, "SELECT count(*) FROM staging.tablas") == [(0,)]
    assert _filas(postgres_dsn, "SELECT count(*) FROM staging.celdas") == [(0,)]


def test_el_mart_resume_la_cobertura_por_subasta(plata: AlmacenPlata, postgres_dsn: str):
    with psycopg.connect(postgres_dsn) as conexion:
        cargar_plata(plata, conexion, ahora=AHORA)

    assert _filas(
        postgres_dsn,
        "SELECT sha256, fecha, especial, tablas, tablas_validadas, tablas_pendientes, "
        "tablas_en_revision, filas_validadas FROM marts.cobertura_subastas ORDER BY sha256",
    ) == [
        (SHA_A, date(2026, 7, 24), False, 2, 1, 1, 0, 2),
        (SHA_B, None, True, 1, 0, 1, 0, 0),
    ]


def test_cargar_una_plata_vacia_crea_los_esquemas_y_no_escribe(tmp_path: Path, postgres_dsn: str):
    with psycopg.connect(postgres_dsn) as conexion:
        resumen = cargar_plata(AlmacenPlata(tmp_path / "vacia"), conexion, ahora=AHORA)

    assert resumen == ResumenCarga(
        tablas_nuevas=0, tablas_actualizadas=0, tablas_sin_cambios=0, tablas_en_staging=0, celdas=0
    )
    assert _filas(postgres_dsn, "SELECT count(*) FROM raw.tablas") == [(0,)]


def test_la_huella_cambia_con_el_contenido_o_el_estado(plata: AlmacenPlata):
    tabla = plata.leer_tabla(ClaveTabla(sha256=SHA_A, pagina=2, tabla=1))
    misma = plata.leer_tabla(ClaveTabla(sha256=SHA_A, pagina=2, tabla=1))
    otro_estado = tabla.model_copy(update={"estado": "verificada"})
    otra_celda = tabla.model_copy(update={"tabla": _tabla(2, 1, ("x", "1", "3"))})

    assert huella_tabla(tabla) == huella_tabla(misma)
    assert len(huella_tabla(tabla)) == 64
    assert huella_tabla(otro_estado) != huella_tabla(tabla)
    assert huella_tabla(otra_celda) != huella_tabla(tabla)
