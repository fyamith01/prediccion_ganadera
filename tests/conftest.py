"""Fixtures compartidas.

`cifras_sinteticas` arma cifras de reporte fijas para probar el PDF y el reportero.

`postgres_dsn` entrega una base de datos Postgres vacía por prueba. Usa, en este orden:
la variable `PREDICCION_PG_DSN_PRUEBAS` (un servidor que ya tengas) o un Postgres 16
embebido de `pixeltable-pgserver` (binarios dentro del wheel, socket Unix, sin red ni
contenedores). Si ninguno está disponible, las pruebas que lo piden se omiten.
"""

from __future__ import annotations

import os
import random
import uuid
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import pytest

from prediccion_ganadera.adaptadores.almacen.oro import PrediccionOro
from prediccion_ganadera.herramientas.cifras_reporte import (
    CifrasReporte,
    FilaHorizonte,
    FilaRecomendacion,
)
from prediccion_ganadera.modelado.variables import PrecioCategoria, ResultadoPrecios

INICIO = date(2025, 1, 3)  # viernes
BASES = {"VACA HORRA": 7800, "MACHO LEVANTE": 10500, "HEMBRA LEVANTE": 9800}


@pytest.fixture(autouse=True)
def _sin_fuente_del_entorno(monkeypatch: pytest.MonkeyPatch) -> None:
    """Las pruebas definen su fuente en el YAML: ignora la URL real y los ajustes TLS del
    `.env` exportado."""
    for variable in (
        "PREDICCION_FUENTE_URL",
        "PREDICCION_FUENTE_DOMINIOS",
        "PREDICCION_SSL_NO_ESTRICTO",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
    ):
        monkeypatch.delenv(variable, raising=False)


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


def _cifras(**cambios) -> CifrasReporte:
    base = CifrasReporte(
        version="20261118T120000Z",
        fuente="plata",
        incluye_no_verificadas=False,
        fecha_desde=date(2025, 1, 3),
        fecha_hasta=date(2026, 11, 13),
        fechas_historico=94,
        precios=846,
        descartes=2,
        tablas_omitidas=1,
        mae=312.4,
        mae_ingenuo=401.9,
        mape=0.034,
        mejora=0.2227,
        pliegues=5,
        confianza="media",
        motivos_confianza=["la mejora frente a la línea base es menor al 30 %"],
        horizontes=[
            FilaHorizonte(horizonte=1, mae=280.0, mae_ingenuo=350.0, mejora=0.2),
            FilaHorizonte(horizonte=2, mae=330.5, mae_ingenuo=420.0, mejora=0.213),
        ],
        recomendaciones=[
            FilaRecomendacion(
                categoria="MACHO LEVANTE",
                mejor_fecha=date(2026, 11, 27),
                horizonte=2,
                ultimo_precio=10450.0,
                precio_predicho=10812.6,
                variacion_esperada=0.0347,
                error_esperado=330.5,
                distinguible=True,
            )
        ],
        predicciones=[
            PrediccionOro(
                fecha=date(2026, 11, 20),
                categoria="MACHO LEVANTE",
                horizonte=1,
                ultimo_precio=10450.0,
                precio_predicho=10600.2,
                variacion_esperada=0.0144,
                error_esperado=280.0,
            ),
            PrediccionOro(
                fecha=date(2026, 11, 27),
                categoria="MACHO LEVANTE",
                horizonte=2,
                ultimo_precio=10450.0,
                precio_predicho=10812.6,
                variacion_esperada=0.0347,
                error_esperado=330.5,
            ),
        ],
    )
    return base.model_copy(update=cambios)


@pytest.fixture
def cifras_sinteticas():
    """Fábrica de `CifrasReporte` sintéticas (sin entrenar): `cifras_sinteticas(campo=valor)`."""
    return _cifras


def _precios(semanas: int) -> ResultadoPrecios:
    """Tres categorías que suben y bajan 10 % semana a semana, una subasta por viernes."""
    azar = random.Random(3)
    precios = [
        PrecioCategoria(
            fecha=INICIO + timedelta(weeks=i),
            especial=False,
            sha256="a" * 64,
            seccion="hembra",
            categoria=categoria,
            unidad="kg",
            animales=40,
            peso_cabeza=300,
            precio_minimo=precio,
            precio_maximo=precio,
            precio_promedio=precio,
            valor_animal=precio * 300,
        )
        for i in range(semanas)
        for categoria, base in BASES.items()
        for precio in [round(base * (1.1 if i % 2 else 1.0) * azar.uniform(0.995, 1.005))]
    ]
    return ResultadoPrecios(
        precios=precios, descartes=[], tablas_resumen=semanas, tablas_omitidas=0
    )


@pytest.fixture(scope="session")
def precios_sinteticos():
    """Fábrica de `ResultadoPrecios` sintéticos: `precios_sinteticos(semanas)`."""
    return _precios
