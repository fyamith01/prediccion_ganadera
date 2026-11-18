"""Cola de revisión manual: tablas que el verificador no pudo dejar correctas.

Tabla Delta `cola_revision` junto a plata, con una entrada por tabla (la última gana).
Nada entra a la capa oro desde aquí sin que una persona lo resuelva.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pyarrow as pa
from deltalake import DeltaTable, write_deltalake
from pydantic import BaseModel, ConfigDict

from prediccion_ganadera.adaptadores.almacen.plata import ClaveTabla

ESQUEMA_REVISION = pa.schema(
    [
        pa.field("sha256", pa.string(), nullable=False),
        pa.field("pagina", pa.int32(), nullable=False),
        pa.field("tabla", pa.int32(), nullable=False),
        pa.field("motivo", pa.string(), nullable=False),
        pa.field("hallazgos", pa.string(), nullable=False),  # lista JSON
        pa.field("intentos", pa.int32(), nullable=False),
        pa.field("creado_en", pa.timestamp("us", tz="UTC"), nullable=False),
    ]
)


class EntradaRevision(BaseModel):
    model_config = ConfigDict(frozen=True)

    clave: ClaveTabla
    motivo: str
    hallazgos: list[str]
    intentos: int
    creado_en: datetime


class ColaRevision:
    def __init__(self, raiz: Path) -> None:
        self.ruta = Path(raiz) / "cola_revision"

    def _existe(self) -> bool:
        return DeltaTable.is_deltatable(str(self.ruta))

    def entradas(self) -> list[EntradaRevision]:
        if not self._existe():
            return []
        return [
            EntradaRevision(
                clave=ClaveTabla(sha256=f["sha256"], pagina=f["pagina"], tabla=f["tabla"]),
                motivo=f["motivo"],
                hallazgos=json.loads(f["hallazgos"]),
                intentos=f["intentos"],
                creado_en=f["creado_en"],
            )
            for f in DeltaTable(self.ruta).to_pyarrow_table().to_pylist()
        ]

    def agregar(self, entrada: EntradaRevision) -> None:
        """Inserta o sustituye la entrada de la tabla en un solo commit."""
        datos = pa.Table.from_pylist(
            [
                {
                    **entrada.clave.model_dump(),
                    "motivo": entrada.motivo,
                    "hallazgos": json.dumps(entrada.hallazgos, ensure_ascii=False),
                    "intentos": entrada.intentos,
                    "creado_en": entrada.creado_en,
                }
            ],
            schema=ESQUEMA_REVISION,
        )
        if self._existe():
            write_deltalake(self.ruta, datos, mode="overwrite", predicate=entrada.clave.predicado())
        else:
            write_deltalake(self.ruta, datos, mode="append")

    def quitar(self, clave: ClaveTabla) -> None:
        if self._existe():
            DeltaTable(self.ruta).delete(clave.predicado())
