"""El diccionario de datos documenta exactamente las columnas que existen."""

import re
from pathlib import Path

import psycopg
import pytest
import yaml

from prediccion_ganadera.adaptadores.almacen.bronce import RegistroManifiesto
from prediccion_ganadera.adaptadores.almacen.plata import ESQUEMA
from prediccion_ganadera.adaptadores.almacen.postgres import crear_esquemas
from prediccion_ganadera.adaptadores.almacen.revision import ESQUEMA_REVISION

RAIZ = Path(__file__).resolve().parents[1]
DICCIONARIO = RAIZ / "docs" / "diccionario_datos.md"
COMPOSE = RAIZ / "infra" / "docker-compose.yml"
LAGO = {"bronce", "plata"}


def _secciones() -> dict[str, list[str]]:
    """`### <relación>` → columnas en la primera celda (entre comillas invertidas) de su tabla."""
    secciones: dict[str, list[str]] = {}
    actual = None
    for linea in DICCIONARIO.read_text(encoding="utf-8").splitlines():
        if encabezado := re.match(r"^### `([\w.]+)`", linea):
            actual = encabezado.group(1)
            secciones[actual] = []
        elif actual and (columna := re.match(r"^\| `(\w+)` \|", linea)):
            secciones[actual].append(columna.group(1))
    return secciones


def test_documenta_los_campos_del_lago():
    secciones = _secciones()

    assert secciones["bronce.manifiesto"] == list(RegistroManifiesto.model_fields)
    assert secciones["plata.tablas_extraidas"] == ESQUEMA.names
    assert secciones["plata.cola_revision"] == ESQUEMA_REVISION.names


def test_documenta_las_columnas_de_postgres(postgres_dsn: str):
    with psycopg.connect(postgres_dsn) as conexion:
        crear_esquemas(conexion)
        filas = conexion.execute(
            "SELECT table_schema || '.' || table_name, column_name FROM information_schema.columns "
            "WHERE table_schema IN ('raw', 'staging', 'marts', 'operacion') "
            "ORDER BY table_schema, table_name, ordinal_position"
        ).fetchall()
    reales: dict[str, list[str]] = {}
    for relacion, columna in filas:
        reales.setdefault(relacion, []).append(columna)

    documentadas = {k: v for k, v in _secciones().items() if k.split(".")[0] not in LAGO}
    assert documentadas == reales


@pytest.fixture
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_compose_usa_la_misma_version_de_postgres_que_las_pruebas(compose: dict):
    assert compose["services"]["postgres"]["image"].startswith("postgres:16")


def test_compose_solo_publica_el_puerto_en_localhost_y_exige_la_contrasena(compose: dict):
    servicio = compose["services"]["postgres"]
    assert all(p.startswith("127.0.0.1:") for p in servicio["ports"])
    assert ":?" in servicio["environment"]["POSTGRES_PASSWORD"]
    assert "healthcheck" in servicio
