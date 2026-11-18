"""Fixtures compartidas.

`postgres_dsn` entrega una base de datos Postgres vacía por prueba. Usa, en este orden:
la variable `PREDICCION_PG_DSN_PRUEBAS` (un servidor que ya tengas) o un Postgres 16
embebido de `pixeltable-pgserver` (binarios dentro del wheel, socket Unix, sin red ni
contenedores). Si ninguno está disponible, las pruebas que lo piden se omiten.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def _servidor_postgres(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    dsn = os.environ.get("PREDICCION_PG_DSN_PRUEBAS")
    if dsn:
        yield dsn
        return
    pgserver = pytest.importorskip("pixeltable_pgserver")
    # Ruta corta: el socket Unix de Postgres no admite rutas largas.
    servidor = pgserver.get_server(Path(tmp_path_factory.mktemp("pg")), cleanup_mode="stop")
    try:
        yield servidor.get_uri()
    finally:
        servidor.cleanup()


@pytest.fixture
def postgres_dsn(_servidor_postgres: str) -> Iterator[str]:
    psycopg = pytest.importorskip("psycopg")
    from psycopg.conninfo import make_conninfo

    nombre = f"prueba_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(_servidor_postgres, autocommit=True) as conexion:
        conexion.execute(f'CREATE DATABASE "{nombre}"')
    try:
        yield make_conninfo(_servidor_postgres, dbname=nombre)
    finally:
        with psycopg.connect(_servidor_postgres, autocommit=True) as conexion:
            conexion.execute(f'DROP DATABASE IF EXISTS "{nombre}" WITH (FORCE)')
