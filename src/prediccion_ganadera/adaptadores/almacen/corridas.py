"""Registro de corridas para observabilidad y evaluación de trayectorias.

Cada corrida se guarda al iniciar, al reanudar y al terminar (o caer). Dos destinos:
- `RegistroJsonl`: archivo local de solo-agregar; siempre disponible, sin servidor.
- `RegistroPostgres`: tabla `operacion.runs`, una fila por corrida (upsert por id).
"""

from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from prediccion_ganadera.adaptadores.almacen.postgres import crear_esquemas
from prediccion_ganadera.puertos.corridas import RegistroCorrida, RegistroCorridas

__all__ = ["RegistroCorrida", "RegistroCorridas", "RegistroJsonl", "RegistroPostgres"]


class RegistroJsonl:
    def __init__(self, ruta: Path) -> None:
        self.ruta = ruta

    def guardar(self, registro: RegistroCorrida) -> None:
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        with self.ruta.open("a", encoding="utf-8") as archivo:
            archivo.write(registro.model_dump_json() + "\n")

    def historial(self) -> list[RegistroCorrida]:
        """Todas las versiones guardadas, en orden de escritura (para evaluar trayectorias)."""
        if not self.ruta.exists():
            return []
        return [
            RegistroCorrida.model_validate_json(linea)
            for linea in self.ruta.read_text(encoding="utf-8").splitlines()
            if linea.strip()
        ]

    def ultimas(self) -> dict[str, RegistroCorrida]:
        """Versión más reciente de cada corrida, en orden de primera aparición."""
        ultimas: dict[str, RegistroCorrida] = {}
        for registro in self.historial():
            ultimas[registro.id_corrida] = registro
        return ultimas


_UPSERT = """
INSERT INTO operacion.runs (
    id_corrida, estado, iniciada_en, actualizada_en, terminada_en, tokens, costo_usd, error, pasos
) VALUES (
    %(id_corrida)s, %(estado)s, %(iniciada_en)s, %(actualizada_en)s, %(terminada_en)s,
    %(tokens)s, %(costo_usd)s, %(error)s, %(pasos)s
)
ON CONFLICT (id_corrida) DO UPDATE SET
    estado = EXCLUDED.estado,
    actualizada_en = EXCLUDED.actualizada_en,
    terminada_en = EXCLUDED.terminada_en,
    tokens = EXCLUDED.tokens,
    costo_usd = EXCLUDED.costo_usd,
    error = EXCLUDED.error,
    pasos = EXCLUDED.pasos
"""

_LEER = """
SELECT id_corrida, estado, iniciada_en, actualizada_en, terminada_en, tokens, costo_usd,
       error, pasos
FROM operacion.runs
ORDER BY iniciada_en, id_corrida
"""


class RegistroPostgres:
    """Conexión corta por escritura: el registro no retiene conexiones durante la corrida."""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._esquemas_creados = False

    def guardar(self, registro: RegistroCorrida) -> None:
        with psycopg.connect(self._dsn) as conexion:
            if not self._esquemas_creados:
                crear_esquemas(conexion)
                self._esquemas_creados = True
            parametros = registro.model_dump()
            parametros["pasos"] = Jsonb(registro.model_dump(mode="json")["pasos"])
            conexion.execute(_UPSERT, parametros)

    def leer(self) -> list[RegistroCorrida]:
        """Última versión de cada corrida (la tabla no guarda el historial), por inicio.

        No crea esquemas: si la tabla todavía no existe, no hay corridas que leer.
        """
        with psycopg.connect(self._dsn) as conexion:
            if conexion.execute("SELECT to_regclass('operacion.runs')").fetchone()[0] is None:
                return []
            cursor = conexion.cursor(row_factory=dict_row)
            filas = cursor.execute(_LEER).fetchall()
        return [RegistroCorrida.model_validate(fila) for fila in filas]
