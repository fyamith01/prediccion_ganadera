"""Capa plata: tablas extraídas como tabla Delta Lake en formato largo.

Una fila por celda (PDF, página, tabla, fila, columna). Las tablas de un mismo PDF
tienen anchos distintos y encabezados partidos, así que un esquema ancho y fijo no
las contiene; en formato largo el esquema es estable y el verificador (T4) corrige
celdas puntuales. Cada PDF se escribe en un solo commit Delta y una vez por SHA-256.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

import pyarrow as pa
from deltalake import DeltaTable, write_deltalake

from prediccion_ganadera.herramientas.extraer_tablas_pdf import TablaExtraida

ESTADO_PENDIENTE = "pendiente"
PARTICIONES = ["anio", "mes"]

ESQUEMA = pa.schema(
    [
        pa.field("sha256", pa.string(), nullable=False),
        pa.field("fecha", pa.date32()),
        pa.field("anio", pa.int32()),
        pa.field("mes", pa.int32()),
        pa.field("especial", pa.bool_(), nullable=False),
        pa.field("pagina", pa.int32(), nullable=False),
        pa.field("tabla", pa.int32(), nullable=False),
        pa.field("fila", pa.int32(), nullable=False),
        pa.field("columna", pa.int32(), nullable=False),
        pa.field("valor", pa.string()),
        pa.field("ajustes", pa.string(), nullable=False),
        pa.field("estado_verificacion", pa.string(), nullable=False),
        pa.field("extraido_en", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)


class AlmacenPlata:
    def __init__(self, raiz: Path) -> None:
        self.ruta = Path(raiz) / "tablas_extraidas"

    def _existe(self) -> bool:
        return DeltaTable.is_deltatable(str(self.ruta))

    def leer(self, columnas: list[str] | None = None) -> pa.Table:
        if not self._existe():
            return ESQUEMA.empty_table().select(columnas or ESQUEMA.names)
        return DeltaTable(self.ruta).to_pyarrow_table(columns=columnas)

    def sha_registrados(self) -> set[str]:
        return set(self.leer(["sha256"]).column("sha256").to_pylist())

    def guardar(
        self,
        *,
        sha256: str,
        fecha: date | None,
        especial: bool,
        tablas: Iterable[TablaExtraida],
        ahora: datetime,
    ) -> int:
        """Escribe las celdas del PDF; devuelve cuántas, o 0 si el PDF ya estaba."""
        if sha256 in self.sha_registrados():
            return 0
        filas = [
            {
                "sha256": sha256,
                "fecha": fecha,
                "anio": fecha.year if fecha else None,
                "mes": fecha.month if fecha else None,
                "especial": especial,
                "pagina": tabla.pagina,
                "tabla": tabla.indice,
                "fila": fila.indice,
                "columna": columna,
                "valor": valor,
                "ajustes": json.dumps(tabla.ajustes, sort_keys=True),
                "estado_verificacion": ESTADO_PENDIENTE,
                "extraido_en": ahora,
            }
            for tabla in tablas
            for fila in tabla.filas
            for columna, valor in enumerate(fila.celdas)
        ]
        if not filas:
            return 0
        write_deltalake(
            self.ruta,
            pa.Table.from_pylist(filas, schema=ESQUEMA),
            mode="append",
            partition_by=PARTICIONES,
        )
        return len(filas)
