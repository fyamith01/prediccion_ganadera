"""Comprobaciones deterministas de una tabla extraída contra el texto de su página.

Se hacen antes de consultar al modelo de visión: lo que el código puede probar no se
le pregunta a la IA. Trabajan con los números de cada renglón porque el detector de
tablas parte palabras entre celdas (`VACA HO` / `RRA`) pero no los números:

- filas de datos (renglones con 3 o más números) que están en la página y no en las
  tablas, o que están en una tabla con números que la página no tiene;
- renglones de totales (`TOTAL`, `GRAN TOTAL`) que la página tiene y las tablas no;
- totales de bloque (`TOTAL: N`) que no igualan la suma de la primera columna numérica
  de las filas de datos que los preceden.

Las filas faltantes de una página se atribuyen a todas sus tablas: no hay forma
determinista de saber a cuál pertenecían.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict

from prediccion_ganadera.herramientas.extraer_tablas_pdf import TablaExtraida

_NUMERO = re.compile(r"\d{1,3}(?:,\d{3})+|\d+")
_MIN_NUMEROS_FILA_DATOS = 3

TipoHallazgo = Literal["fila_faltante", "fila_distinta", "total_faltante", "total_incorrecto"]


class Hallazgo(BaseModel):
    model_config = ConfigDict(frozen=True)

    tipo: TipoHallazgo
    detalle: str
    renglon: str | None = None  # el renglón de la página, en los hallazgos de faltantes


def numeros(texto: str) -> tuple[int, ...]:
    """Enteros del texto (`1,755` -> 1755); ignora horas, fechas y versiones."""
    return tuple(int(token.replace(",", "")) for token in texto.split() if _NUMERO.fullmatch(token))


def _es_total(texto: str) -> bool:
    return "TOTAL" in texto.replace(" ", "").upper()


def _es_datos(texto: str) -> bool:
    return not _es_total(texto) and len(numeros(texto)) >= _MIN_NUMEROS_FILA_DATOS


def _texto_fila(celdas: Iterable[str | None]) -> str:
    return " ".join(c for c in celdas if c)


def _diferencia(a: Iterable[tuple[int, ...]], b: Iterable[tuple[int, ...]]) -> Counter:
    return Counter(a) - Counter(b)


def _totales_de_bloque(tabla: TablaExtraida) -> list[Hallazgo]:
    hallazgos: list[Hallazgo] = []
    suma = 0
    for fila in tabla.filas:
        texto = _texto_fila(fila.celdas)
        pegado = "".join(c or "" for c in fila.celdas).replace(" ", "").upper()
        if _es_datos(texto):
            suma += numeros(texto)[0]
        elif pegado.startswith("TOTAL") and len(valor := numeros(texto)) == 1:
            if valor[0] != suma:
                hallazgos.append(
                    Hallazgo(
                        tipo="total_incorrecto",
                        detalle=f"fila {fila.indice}: el total dice {valor[0]} "
                        f"y las filas del bloque suman {suma}",
                    )
                )
            suma = 0
    return hallazgos


def comprobar_pagina(
    tablas: Sequence[TablaExtraida], lineas: Sequence[str]
) -> dict[int, list[Hallazgo]]:
    """Hallazgos por índice de tabla; una lista vacía significa que la tabla cuadra."""
    datos_pagina = [linea for linea in lineas if _es_datos(linea)]
    totales_pagina = [linea for linea in lineas if _es_total(linea)]
    claves_datos = [numeros(linea) for linea in datos_pagina]
    filas = {t.indice: [(f.indice, _texto_fila(f.celdas)) for f in t.filas] for t in tablas}
    textos = [texto for filas_tabla in filas.values() for _, texto in filas_tabla]

    faltantes = _diferencia(claves_datos, (numeros(t) for t in textos if _es_datos(t)))
    totales_faltantes = _diferencia(
        (numeros(linea) for linea in totales_pagina),
        (numeros(t) for t in textos if _es_total(t)),
    )
    texto_de = {numeros(linea): linea for linea in datos_pagina + totales_pagina}
    comunes = [
        Hallazgo(
            tipo="fila_faltante",
            detalle=f"la página tiene «{texto_de[clave]}»",
            renglon=texto_de[clave],
        )
        for clave in faltantes.elements()
    ]
    comunes_totales = [
        Hallazgo(
            tipo="total_faltante",
            detalle=f"la página tiene «{texto_de[clave]}»",
            renglon=texto_de[clave],
        )
        for clave in totales_faltantes.elements()
    ]

    resultado: dict[int, list[Hallazgo]] = {}
    for tabla in tablas:
        datos_tabla = [(i, texto) for i, texto in filas[tabla.indice] if _es_datos(texto)]
        sobrantes = _diferencia((numeros(t) for _, t in datos_tabla), claves_datos)
        distintas = []
        for indice, texto in datos_tabla:
            clave = numeros(texto)
            if sobrantes[clave] > 0:
                sobrantes[clave] -= 1
                distintas.append(
                    Hallazgo(
                        tipo="fila_distinta",
                        detalle=f"fila {indice}: «{texto}» no coincide con ningún renglón",
                    )
                )
        resultado[tabla.indice] = comunes + distintas + comunes_totales + _totales_de_bloque(tabla)
    return resultado
