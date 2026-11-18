"""Capa oro: predicciones del modelo como tabla Delta Lake.

Una fila por versión del modelo × fecha futura × categoría. Se escribe una vez por
versión, así que repetir la escritura no duplica filas; el historial de versiones
permite comparar luego lo predicho con lo que pasó en la subasta.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
from deltalake import DeltaTable, write_deltalake
from pydantic import BaseModel, ConfigDict

from prediccion_ganadera.modelado.entrenamiento import ResultadoEntrenamiento

ESQUEMA_PREDICCIONES = pa.schema(
    [
        pa.field("version", pa.string(), nullable=False),
        pa.field("generado_en", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("fecha_referencia", pa.date32(), nullable=False),
        pa.field("fecha", pa.date32(), nullable=False),
        pa.field("categoria", pa.string(), nullable=False),
        pa.field("horizonte", pa.int32(), nullable=False),
        pa.field("ultimo_precio", pa.float64(), nullable=False),
        pa.field("precio_predicho", pa.float64(), nullable=False),
        pa.field("variacion_esperada", pa.float64(), nullable=False),
        pa.field("error_esperado", pa.float64(), nullable=False),
        pa.field("confianza", pa.string(), nullable=False),
    ]
)


class PrediccionOro(BaseModel):
    """Una fila de `oro.predicciones` (precios en pesos por kilo)."""

    model_config = ConfigDict(frozen=True)

    fecha: date
    categoria: str
    horizonte: int
    ultimo_precio: float
    precio_predicho: float
    variacion_esperada: float
    error_esperado: float


class AlmacenOro:
    def __init__(self, raiz: Path) -> None:
        self.ruta_predicciones = Path(raiz) / "predicciones"

    def _versiones(self) -> set[str]:
        if not DeltaTable.is_deltatable(str(self.ruta_predicciones)):
            return set()
        tabla = DeltaTable(self.ruta_predicciones).to_pyarrow_table(columns=["version"])
        return set(pc.unique(tabla["version"]).to_pylist())

    def leer_predicciones(self, version: str) -> list[PrediccionOro]:
        """Las predicciones de una versión, por fecha y categoría (vacío si no hay)."""
        if version not in self._versiones():
            return []
        tabla = DeltaTable(self.ruta_predicciones).to_pyarrow_table(
            columns=["version", *PrediccionOro.model_fields]
        )
        filas = tabla.filter(pc.equal(tabla["version"], version)).drop_columns("version")
        return sorted(
            (PrediccionOro.model_validate(f) for f in filas.to_pylist()),
            key=lambda p: (p.fecha, p.categoria),
        )

    def guardar_predicciones(
        self, version: str, resultado: ResultadoEntrenamiento, *, ahora: datetime
    ) -> int:
        """Escribe el ranking de la versión; devuelve las filas escritas (0 si ya estaba)."""
        if version in self._versiones():
            return 0
        filas = [
            {
                "version": version,
                "generado_en": ahora,
                "fecha_referencia": fila.fecha_referencia.date(),
                "fecha": fila.fecha.date(),
                "categoria": fila.categoria,
                "horizonte": int(fila.horizonte),
                "ultimo_precio": float(fila.ultimo_precio),
                "precio_predicho": float(fila.precio_predicho),
                "variacion_esperada": float(fila.variacion_esperada),
                "error_esperado": float(fila.error_esperado),
                "confianza": resultado.confianza.nivel,
            }
            for fila in resultado.ranking.itertuples()
        ]
        if not filas:
            return 0
        write_deltalake(
            self.ruta_predicciones,
            pa.Table.from_pylist(filas, schema=ESQUEMA_PREDICCIONES),
            mode="append",
        )
        return len(filas)
