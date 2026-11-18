"""Carga idempotente de la capa plata en Postgres (esquemas `raw`, `staging`, `marts`).

Cada tabla se identifica por (sha256 del PDF, página, tabla) y lleva una huella de su
contenido y estado. Una carga solo reescribe las tablas cuya huella cambió, así que
repetirla no modifica nada. `staging` contiene únicamente las tablas que el verificador
aceptó (`verificada` o `corregida`); si una tabla deja de estarlo, sale de `staging`.
Toda la carga ocurre en una transacción: o entra completa o no entra.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict

from prediccion_ganadera.adaptadores.almacen.plata import AlmacenPlata, TablaPlata

ESQUEMA_SQL = Path(__file__).with_name("esquema_postgres.sql")
ESTADOS_VALIDADOS = frozenset({"verificada", "corregida"})
_CLAVE = "sha256 = %s AND pagina = %s AND tabla = %s"


class ResumenCarga(BaseModel):
    model_config = ConfigDict(frozen=True)

    tablas_nuevas: int
    tablas_actualizadas: int
    tablas_sin_cambios: int
    tablas_en_staging: int
    celdas: int  # celdas escritas en raw durante esta carga


def crear_esquemas(conexion: psycopg.Connection) -> None:
    """Crea esquemas, tablas y vistas si no existen; se puede ejecutar varias veces."""
    with conexion.transaction():
        conexion.execute(ESQUEMA_SQL.read_text(encoding="utf-8"))


def huella_tabla(tabla: TablaPlata) -> str:
    """SHA-256 del contenido, el estado y los metadatos que se cargan de una tabla."""
    contenido = {
        "estado": tabla.estado,
        "fecha": tabla.fecha.isoformat() if tabla.fecha else None,
        "especial": tabla.especial,
        "ajustes": tabla.tabla.ajustes,
        "filas": [[f.indice, list(f.celdas)] for f in tabla.tabla.filas],
    }
    serializado = json.dumps(contenido, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(serializado.encode("utf-8")).hexdigest()


def cargar_plata(
    plata: AlmacenPlata, conexion: psycopg.Connection, *, ahora: datetime
) -> ResumenCarga:
    crear_esquemas(conexion)
    nuevas = actualizadas = sin_cambios = celdas = 0
    with conexion.transaction():
        existentes = {
            (s, p, t): h
            for s, p, t, h in conexion.execute(
                "SELECT sha256, pagina, tabla, huella FROM raw.tablas"
            ).fetchall()
        }
        for tabla in plata.tablas():
            clave = (tabla.clave.sha256, tabla.clave.pagina, tabla.clave.tabla)
            huella = huella_tabla(tabla)
            anterior = existentes.get(clave)
            if anterior == huella:
                sin_cambios += 1
                continue
            if anterior is None:
                nuevas += 1
            else:
                actualizadas += 1
            celdas += _escribir_raw(conexion, tabla, huella, ahora)
            _sincronizar_staging(conexion, tabla, clave)
        (en_staging,) = conexion.execute("SELECT count(*) FROM staging.tablas").fetchone()
    return ResumenCarga(
        tablas_nuevas=nuevas,
        tablas_actualizadas=actualizadas,
        tablas_sin_cambios=sin_cambios,
        tablas_en_staging=en_staging,
        celdas=celdas,
    )


def _escribir_raw(
    conexion: psycopg.Connection, tabla: TablaPlata, huella: str, ahora: datetime
) -> int:
    clave = (tabla.clave.sha256, tabla.clave.pagina, tabla.clave.tabla)
    conexion.execute(
        """
        INSERT INTO raw.tablas (sha256, pagina, tabla, fecha, especial, estado_verificacion,
                                ajustes, filas, columnas, huella, extraido_en, cargado_en)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (sha256, pagina, tabla) DO UPDATE SET
            fecha = EXCLUDED.fecha,
            especial = EXCLUDED.especial,
            estado_verificacion = EXCLUDED.estado_verificacion,
            ajustes = EXCLUDED.ajustes,
            filas = EXCLUDED.filas,
            columnas = EXCLUDED.columnas,
            huella = EXCLUDED.huella,
            extraido_en = EXCLUDED.extraido_en,
            cargado_en = EXCLUDED.cargado_en
        """,
        (
            *clave,
            tabla.fecha,
            tabla.especial,
            tabla.estado,
            Jsonb(tabla.tabla.ajustes),
            len(tabla.tabla.filas),
            tabla.tabla.columnas,
            huella,
            tabla.extraido_en,
            ahora,
        ),
    )
    conexion.execute(f"DELETE FROM raw.celdas WHERE {_CLAVE}", clave)
    escritas = 0
    with conexion.cursor().copy(
        "COPY raw.celdas (sha256, pagina, tabla, fila, columna, valor) FROM STDIN"
    ) as copia:
        for fila in tabla.tabla.filas:
            for columna, valor in enumerate(fila.celdas):
                copia.write_row((*clave, fila.indice, columna, valor))
                escritas += 1
    return escritas


def _sincronizar_staging(
    conexion: psycopg.Connection, tabla: TablaPlata, clave: tuple[str, int, int]
) -> None:
    conexion.execute(f"DELETE FROM staging.tablas WHERE {_CLAVE}", clave)
    if tabla.estado not in ESTADOS_VALIDADOS:
        return
    conexion.execute(
        f"""
        INSERT INTO staging.tablas (sha256, pagina, tabla, fecha, especial, estado_verificacion,
                                    filas, columnas, huella, cargado_en)
        SELECT sha256, pagina, tabla, fecha, especial, estado_verificacion,
               filas, columnas, huella, cargado_en
        FROM raw.tablas WHERE {_CLAVE}
        """,
        clave,
    )
    conexion.execute(
        f"""
        INSERT INTO staging.celdas (sha256, pagina, tabla, fila, columna, valor)
        SELECT sha256, pagina, tabla, fila, columna, valor FROM raw.celdas WHERE {_CLAVE}
        """,
        clave,
    )
