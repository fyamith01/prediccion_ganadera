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
import pyarrow.compute as pc
from deltalake import DeltaTable, write_deltalake
from pydantic import BaseModel, ConfigDict, Field

from prediccion_ganadera.herramientas.extraer_tablas_pdf import FilaExtraida, TablaExtraida

ESTADO_PENDIENTE = "pendiente"
# pendiente: recién extraída; verificada: el verificador la aceptó sin cambios;
# corregida: aceptada después de rectificarla; en_revision: requiere una persona.
ESTADOS = frozenset({ESTADO_PENDIENTE, "verificada", "corregida", "en_revision"})
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


class ClaveTabla(BaseModel):
    """Identifica una tabla en plata; el SHA-256 se valida porque entra en predicados SQL."""

    model_config = ConfigDict(frozen=True)

    sha256: str = Field(pattern=r"^[0-9a-f]+$")
    pagina: int = Field(ge=1)
    tabla: int = Field(ge=1)

    def predicado(self) -> str:
        return f"sha256 = '{self.sha256}' AND pagina = {self.pagina} AND tabla = {self.tabla}"


class TablaPlata(BaseModel):
    model_config = ConfigDict(frozen=True)

    clave: ClaveTabla
    fecha: date | None
    especial: bool
    estado: str
    tabla: TablaExtraida


def _validar_estado(estado: str) -> None:
    if estado not in ESTADOS:
        raise ValueError(f"estado de verificación desconocido: {estado!r}")


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

    def claves_por_estado(self, estado: str) -> list[ClaveTabla]:
        _validar_estado(estado)
        datos = self.leer(["sha256", "pagina", "tabla", "estado_verificacion"])
        datos = datos.filter(pc.equal(datos["estado_verificacion"], estado))
        claves = {(f["sha256"], f["pagina"], f["tabla"]) for f in datos.to_pylist()}
        return [ClaveTabla(sha256=s, pagina=p, tabla=t) for s, p, t in sorted(claves)]

    def leer_tabla(self, clave: ClaveTabla) -> TablaPlata:
        """La tabla tal como está en plata; `KeyError` si no existe.

        `columnas` se reconstruye como la columna más alta guardada + 1: las filas vacías
        que el extractor descartó no se guardan.
        """
        if not self._existe():
            raise KeyError(clave)
        filas = (
            DeltaTable(self.ruta)
            .to_pyarrow_table(
                filters=[
                    ("sha256", "=", clave.sha256),
                    ("pagina", "=", clave.pagina),
                    ("tabla", "=", clave.tabla),
                ]
            )
            .to_pylist()
        )
        if not filas:
            raise KeyError(clave)
        celdas: dict[int, dict[int, str | None]] = {}
        for fila in filas:
            celdas.setdefault(fila["fila"], {})[fila["columna"]] = fila["valor"]
        columnas = max(f["columna"] for f in filas) + 1
        primera = filas[0]
        return TablaPlata(
            clave=clave,
            fecha=primera["fecha"],
            especial=primera["especial"],
            estado=primera["estado_verificacion"],
            tabla=TablaExtraida(
                pagina=clave.pagina,
                indice=clave.tabla,
                columnas=columnas,
                filas=tuple(
                    FilaExtraida(indice=i, celdas=tuple(v for _, v in sorted(celdas[i].items())))
                    for i in sorted(celdas)
                ),
                ajustes=json.loads(primera["ajustes"]),
            ),
        )

    def marcar_estado(self, clave: ClaveTabla, estado: str) -> None:
        _validar_estado(estado)
        DeltaTable(self.ruta).update(
            predicate=clave.predicado(), updates={"estado_verificacion": f"'{estado}'"}
        )

    def reemplazar_tabla(
        self, clave: ClaveTabla, tabla: TablaExtraida, estado: str, ahora: datetime
    ) -> None:
        """Sustituye las celdas de una tabla en un solo commit Delta (overwrite con predicado)."""
        _validar_estado(estado)
        if (tabla.pagina, tabla.indice) != (clave.pagina, clave.tabla):
            raise ValueError(
                f"la tabla (página {tabla.pagina}, índice {tabla.indice}) no coincide con la clave"
            )
        actual = self.leer_tabla(clave)
        write_deltalake(
            self.ruta,
            pa.Table.from_pylist(
                self._filas(clave.sha256, actual.fecha, actual.especial, [tabla], estado, ahora),
                schema=ESQUEMA,
            ),
            mode="overwrite",
            predicate=clave.predicado(),
            partition_by=PARTICIONES,
        )

    @staticmethod
    def _filas(
        sha256: str,
        fecha: date | None,
        especial: bool,
        tablas: Iterable[TablaExtraida],
        estado: str,
        ahora: datetime,
    ) -> list[dict]:
        return [
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
                "estado_verificacion": estado,
                "extraido_en": ahora,
            }
            for tabla in tablas
            for fila in tabla.filas
            for columna, valor in enumerate(fila.celdas)
        ]

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
        filas = self._filas(sha256, fecha, especial, tablas, ESTADO_PENDIENTE, ahora)
        if not filas:
            return 0
        write_deltalake(
            self.ruta,
            pa.Table.from_pylist(filas, schema=ESQUEMA),
            mode="append",
            partition_by=PARTICIONES,
        )
        return len(filas)
