"""Precios por categoría de ganado a partir del «RESUMEN GENERAL DE LA FERIA».

Cada subasta trae en su primera página una tabla con una fila por categoría: animales,
peso por cabeza, precio mínimo, máximo y promedio (pesos por kilo) y valor por animal.
El extractor parte palabras y encabezados entre celdas (`'VACA HO', 'RRA'`), así que
cada fila se une en un texto y se interpreta así: los últimos seis números son los
valores y las letras anteriores, sin espacios, identifican la categoría.

Ninguna cifra se completa ni se adivina: una fila que no cuadra (promedio fuera del
rango, valor por animal distinto de peso × precio, columnas faltantes) se descarta y
queda registrada con su motivo.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict

from prediccion_ganadera.adaptadores.almacen.plata import TablaPlata
from prediccion_ganadera.herramientas.extraer_tablas_pdf import TablaExtraida

ESTADOS_VALIDADOS = frozenset({"verificada", "corregida"})
TOLERANCIA_VALOR = 0.05  # el valor por animal se publica redondeado sobre pesos promedio

# Nombre legible de las categorías conocidas; las demás conservan su clave compacta.
CATEGORIAS = {
    "BUFALAHORRA": "BUFALA HORRA",
    "HEMBRALEVANTE": "HEMBRA LEVANTE",
    "NOVILLADEVIENTRE": "NOVILLA DE VIENTRE",
    "VACAHORRA": "VACA HORRA",
    "VACAPARIDA": "VACA PARIDA",
    "MULETO": "MULETO",
    "MACHOLEVANTE": "MACHO LEVANTE",
    "MACHO": "MACHO",
    "MACHODECEBA": "MACHO DE CEBA",
}

_NUMERO = re.compile(r"^[\d.,]*\d[\d.,]*$")
_NO_LETRA = re.compile(r"[^A-ZÁÉÍÓÚÑ]")

Motivo = Literal["fila_incompleta", "precio_fuera_de_rango", "valor_inconsistente", "duplicada"]


class PrecioCategoria(BaseModel):
    model_config = ConfigDict(frozen=True)

    fecha: date
    especial: bool
    sha256: str
    seccion: Literal["hembra", "macho"] | None
    categoria: str
    unidad: Literal["kg", "cabeza"]  # en qué se expresan los precios
    animales: int
    peso_cabeza: int
    precio_minimo: int
    precio_maximo: int
    precio_promedio: int
    valor_animal: int


class Descarte(BaseModel):
    model_config = ConfigDict(frozen=True)

    fecha: date | None
    sha256: str
    categoria: str
    texto: str
    motivo: Motivo


class ResultadoResumen(BaseModel):
    model_config = ConfigDict(frozen=True)

    precios: list[PrecioCategoria]
    descartes: list[Descarte]


class ResultadoPrecios(BaseModel):
    model_config = ConfigDict(frozen=True)

    precios: list[PrecioCategoria]
    descartes: list[Descarte]
    tablas_resumen: int  # tablas de resumen usadas
    tablas_omitidas: int  # tablas de resumen sin fecha o con un estado no admitido

    @property
    def fechas(self) -> list[date]:
        return sorted({p.fecha for p in self.precios})


def _texto(celdas: Iterable[str | None]) -> str:
    return " ".join(c.strip() for c in celdas if c and c.strip())


def _compacto(texto: str) -> str:
    return re.sub(r"\s", "", texto).upper()


def es_resumen_general(tabla: TablaExtraida) -> bool:
    return any("RESUMENGENERAL" in _compacto(_texto(f.celdas)) for f in tabla.filas[:3])


def _entero(token: str) -> int:
    return int(token.replace(",", "").replace(".", ""))


def _interpretar(texto: str, seccion: str | None, comun: dict) -> PrecioCategoria | Descarte | None:
    tokens = texto.split()
    inicio = len(tokens)
    while inicio > 0 and _NUMERO.match(tokens[inicio - 1]):
        inicio -= 1
    clave = _NO_LETRA.sub("", " ".join(tokens[:inicio]).upper())
    numeros = [_entero(t) for t in tokens[inicio:]]
    if not clave or not numeros:  # encabezados de columna o renglones sin cifras
        return None
    categoria = CATEGORIAS.get(clave, clave)

    def descarte(motivo: Motivo) -> Descarte:
        return Descarte(
            fecha=comun["fecha"], sha256=comun["sha256"], categoria=categoria,
            texto=texto, motivo=motivo,
        )  # fmt: skip

    if len(numeros) != 6:
        return descarte("fila_incompleta")
    animales, peso, minimo, maximo, promedio, valor = numeros
    if not minimo <= promedio <= maximo:
        return descarte("precio_fuera_de_rango")
    unidad = "cabeza" if peso <= 1 else "kg"
    esperado = promedio if unidad == "cabeza" else peso * promedio
    if animales < 1 or abs(valor - esperado) > TOLERANCIA_VALOR * esperado:
        return descarte("valor_inconsistente")
    return PrecioCategoria(
        **comun,
        seccion=seccion,
        categoria=categoria,
        unidad=unidad,
        animales=animales,
        peso_cabeza=peso,
        precio_minimo=minimo,
        precio_maximo=maximo,
        precio_promedio=promedio,
        valor_animal=valor,
    )


def precios_de_resumen(
    tabla: TablaExtraida, *, fecha: date, especial: bool, sha256: str
) -> ResultadoResumen:
    """Interpreta una tabla de resumen general; las filas antes de la primera sección
    («GANADO HEMBRA/MACHO ...») son el encabezado del documento y se ignoran."""
    comun = {"fecha": fecha, "especial": especial, "sha256": sha256}
    precios: list[PrecioCategoria] = []
    descartes: list[Descarte] = []
    seccion: str | None = None
    en_seccion = False
    for fila in tabla.filas:
        texto = _texto(fila.celdas)
        compacto = _compacto(texto)
        if compacto.startswith("GANADO"):
            en_seccion = True
            seccion = "hembra" if "HEMBRA" in compacto else "macho" if "MACHO" in compacto else None
            continue
        if not en_seccion or "TOTAL" in compacto:
            continue
        resultado = _interpretar(texto, seccion, comun)
        if isinstance(resultado, PrecioCategoria):
            precios.append(resultado)
        elif isinstance(resultado, Descarte):
            descartes.append(resultado)
    return ResultadoResumen(precios=precios, descartes=descartes)


def precios_desde_tablas(
    tablas: Iterable[TablaPlata], *, estados: Iterable[str] = ESTADOS_VALIDADOS
) -> ResultadoPrecios:
    """Precios de todas las tablas de resumen con fecha y un estado admitido.

    Si dos PDFs traen la misma fecha y categoría (una subasta publicada dos veces), se
    conserva la primera en orden de clave y la otra queda como descarte `duplicada`.
    """
    admitidos = set(estados)
    precios: dict[tuple[date, str], PrecioCategoria] = {}
    descartes: list[Descarte] = []
    usadas = omitidas = 0
    for tabla in tablas:
        if not es_resumen_general(tabla.tabla):
            continue
        if tabla.fecha is None or tabla.estado not in admitidos:
            omitidas += 1
            continue
        usadas += 1
        resultado = precios_de_resumen(
            tabla.tabla, fecha=tabla.fecha, especial=tabla.especial, sha256=tabla.clave.sha256
        )
        descartes.extend(resultado.descartes)
        for precio in resultado.precios:
            clave = (precio.fecha, precio.categoria)
            if clave in precios:
                descartes.append(
                    Descarte(
                        fecha=precio.fecha,
                        sha256=precio.sha256,
                        categoria=precio.categoria,
                        texto="misma fecha y categoría en otro PDF",
                        motivo="duplicada",
                    )
                )
                continue
            precios[clave] = precio
    return ResultadoPrecios(
        precios=sorted(precios.values(), key=lambda p: (p.fecha, p.categoria)),
        descartes=descartes,
        tablas_resumen=usadas,
        tablas_omitidas=omitidas,
    )
