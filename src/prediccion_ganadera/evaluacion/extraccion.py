"""Exactitud de extracción contra una muestra revisada a mano.

Una muestra (`evaluacion/muestras/*.yaml`) describe lo que una persona leyó en un PDF:
los renglones con cifras de algunas páginas, tal como se imprimen, y los precios del
«RESUMEN GENERAL DE LA FERIA». Se compara con las tablas que produjo el extractor (o con
las que quedaron en plata después del verificador) y se mide:

- filas: un renglón esperado está «encontrado» si alguna fila extraída de esa página
  tiene exactamente los mismos números en el mismo orden (el extractor parte palabras
  entre celdas pero no los números, por eso se comparan números y no textos);
  `recall` = encontradas / esperadas; `precision` = filas extraídas con cifras que están
  en la muestra / filas extraídas con cifras, solo en páginas transcritas completas;
- valores: para cada renglón esperado, la fila extraída de la misma página con más
  números en común; `exactitud_valores` = números acertados / números esperados;
- precios: lo que el modelo predictivo consume. Se interpretan las tablas con el mismo
  código del modelado (`precios_de_resumen`) y se compara campo por campo.

Una muestra en `borrador` todavía no la revisó una persona: sus métricas se reportan
marcadas como borrador y nunca se presentan como revisadas.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, computed_field, model_validator

from prediccion_ganadera.adaptadores.almacen.plata import AlmacenPlata
from prediccion_ganadera.herramientas.comprobaciones import numeros
from prediccion_ganadera.herramientas.extraer_tablas_pdf import TablaExtraida, extraer_tablas
from prediccion_ganadera.modelado.variables import (
    CATEGORIAS,
    es_resumen_general,
    precios_de_resumen,
)

CAMPOS_PRECIO = (
    "animales",
    "peso_cabeza",
    "precio_minimo",
    "precio_maximo",
    "precio_promedio",
    "valor_animal",
)
_NO_LETRA = re.compile(r"[^A-ZÁÉÍÓÚÑ]")
_MIN_NUMEROS_FILA = 3

Fuente = Literal["extractor", "plata"]


class ErrorMuestra(ValueError):
    """La muestra no se puede leer o no corresponde al PDF o a los datos disponibles."""


class _Estricto(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PaginaMuestra(_Estricto):
    pagina: int = Field(ge=1)
    completa: bool  # True: están todos los renglones con cifras de la página
    filas: list[str] = Field(min_length=1)  # renglones tal como se imprimen


class PrecioEsperado(_Estricto):
    categoria: str  # como se imprime, p. ej. «VACA HORRA»
    animales: int
    peso_cabeza: int
    precio_minimo: int
    precio_maximo: int
    precio_promedio: int
    valor_animal: int


class MuestraRevisada(_Estricto):
    pdf: str  # nombre del archivo original (informativo)
    ruta: str | None = None  # ruta al PDF relativa a la raíz del repositorio, si no está en bronce
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fecha: date
    estado: Literal["borrador", "revisada"]
    revisado_por: str | None = None
    revisado_en: date | None = None
    notas: str = ""
    paginas: list[PaginaMuestra] = Field(min_length=1)
    precios: list[PrecioEsperado] = Field(default_factory=list)

    @model_validator(mode="after")
    def _revision_completa(self) -> MuestraRevisada:
        if self.estado == "revisada" and not (self.revisado_por and self.revisado_en):
            raise ValueError("una muestra revisada necesita revisado_por y revisado_en")
        return self


class MetricasFilas(BaseModel):
    model_config = ConfigDict(frozen=True)

    esperadas: int
    encontradas: int
    extraidas_en_completas: int  # filas extraídas con cifras, en páginas completas
    correctas_en_completas: int
    valores_esperados: int
    valores_correctos: int

    @computed_field
    @property
    def recall(self) -> float | None:
        return _razon(self.encontradas, self.esperadas)

    @computed_field
    @property
    def precision(self) -> float | None:
        return _razon(self.correctas_en_completas, self.extraidas_en_completas)

    @computed_field
    @property
    def exactitud_valores(self) -> float | None:
        return _razon(self.valores_correctos, self.valores_esperados)


class MetricasPrecios(BaseModel):
    model_config = ConfigDict(frozen=True)

    categorias_esperadas: int
    faltantes: list[str]  # esperadas que el parser no produjo (o descartó)
    sobrantes: list[str]  # producidas que la muestra no tiene
    campos_esperados: int
    campos_correctos: int

    @computed_field
    @property
    def exactitud_campos(self) -> float | None:
        return _razon(self.campos_correctos, self.campos_esperados)


class EvaluacionExtraccion(BaseModel):
    model_config = ConfigDict(frozen=True)

    pdf: str
    sha256: str
    estado: Literal["borrador", "revisada"]
    fuente: Fuente
    filas: MetricasFilas
    precios: MetricasPrecios
    detalles: list[str]  # cada diferencia encontrada, legible

    @computed_field
    @property
    def revisada(self) -> bool:
        return self.estado == "revisada"


class TotalExtraccion(BaseModel):
    """Suma de varias evaluaciones (las razones se recalculan sobre los conteos)."""

    model_config = ConfigDict(frozen=True)

    muestras: int
    revisadas: int
    filas: MetricasFilas
    precios: MetricasPrecios


def _razon(a: int, b: int) -> float | None:
    return a / b if b else None


def _texto_fila(celdas: Iterable[str | None]) -> str:
    return " ".join(c for c in celdas if c)


def _clave_categoria(texto: str) -> str:
    clave = _NO_LETRA.sub("", texto.upper())
    return CATEGORIAS.get(clave, clave)


def _tiene_cifras(texto: str) -> bool:
    """Renglones que interesan: datos (3+ números) o totales con algún número."""
    cantidad = len(numeros(texto))
    es_total = "TOTAL" in texto.replace(" ", "").upper()
    return cantidad >= _MIN_NUMEROS_FILA or (es_total and cantidad >= 1)


def cargar_muestra(ruta: Path) -> MuestraRevisada:
    try:
        datos = yaml.safe_load(ruta.read_text(encoding="utf-8"))
        return MuestraRevisada.model_validate(datos)
    except (OSError, yaml.YAMLError, ValidationError) as error:
        raise ErrorMuestra(f"{ruta}: {error}") from error


def tablas_del_extractor(ruta_pdf: Path, sha256: str) -> list[TablaExtraida]:
    """Extrae el PDF con los ajustes por defecto, tras comprobar que es el de la muestra."""
    real = hashlib.sha256(ruta_pdf.read_bytes()).hexdigest()
    if real != sha256:
        raise ErrorMuestra(f"{ruta_pdf}: el SHA-256 es {real}, la muestra espera {sha256}")
    return extraer_tablas(ruta_pdf)


def tablas_de_plata(plata: AlmacenPlata, sha256: str) -> list[TablaExtraida]:
    """Las tablas de ese PDF tal como quedaron en plata (después del verificador)."""
    tablas = [t.tabla for t in plata.tablas() if t.clave.sha256 == sha256]
    if not tablas:
        raise ErrorMuestra(f"el PDF {sha256} no tiene tablas en plata")
    return tablas


def _evaluar_filas(
    muestra: MuestraRevisada, tablas: Sequence[TablaExtraida], detalles: list[str]
) -> MetricasFilas:
    esperadas = encontradas = extraidas_completas = correctas_completas = 0
    valores_esperados = valores_correctos = 0
    for pagina in muestra.paginas:
        extraidas = [
            numeros(texto)
            for tabla in tablas
            if tabla.pagina == pagina.pagina
            for fila in tabla.filas
            if _tiene_cifras(texto := _texto_fila(fila.celdas))
        ]
        claves = [numeros(fila) for fila in pagina.filas]
        comunes = Counter(claves) & Counter(extraidas)
        esperadas += len(claves)
        encontradas += comunes.total()
        faltan = Counter(claves) - Counter(extraidas)
        for fila in pagina.filas:
            if faltan[numeros(fila)] > 0:
                faltan[numeros(fila)] -= 1
                detalles.append(f"página {pagina.pagina}: falta o difiere «{fila}»")
        if pagina.completa:
            extraidas_completas += len(extraidas)
            correctas_completas += comunes.total()

        disponibles = [Counter(n) for n in extraidas]
        for clave in claves:
            valores_esperados += len(clave)
            esperado = Counter(clave)
            mejor = max(
                range(len(disponibles)),
                key=lambda i: (esperado & disponibles[i]).total(),
                default=None,
            )
            if mejor is None or not (acierto := (esperado & disponibles[mejor]).total()):
                continue
            valores_correctos += acierto
            disponibles.pop(mejor)
    return MetricasFilas(
        esperadas=esperadas,
        encontradas=encontradas,
        extraidas_en_completas=extraidas_completas,
        correctas_en_completas=correctas_completas,
        valores_esperados=valores_esperados,
        valores_correctos=valores_correctos,
    )


def _evaluar_precios(
    muestra: MuestraRevisada, tablas: Sequence[TablaExtraida], detalles: list[str]
) -> MetricasPrecios:
    producidos = {}
    for tabla in tablas:
        if not es_resumen_general(tabla):
            continue
        resultado = precios_de_resumen(
            tabla, fecha=muestra.fecha, especial=False, sha256=muestra.sha256
        )
        for precio in resultado.precios:
            producidos.setdefault(precio.categoria, precio)
        for descarte in resultado.descartes:
            detalles.append(f"precios: «{descarte.texto}» descartada ({descarte.motivo})")

    esperados = {_clave_categoria(p.categoria): p for p in muestra.precios}
    faltantes = [c for c in esperados if c not in producidos]
    sobrantes = [c for c in producidos if c not in esperados] if esperados else []
    for categoria in faltantes:
        detalles.append(f"precios: falta la categoría {categoria}")
    correctos = 0
    for categoria, esperado in esperados.items():
        if categoria not in producidos:
            continue
        for campo in CAMPOS_PRECIO:
            obtenido = getattr(producidos[categoria], campo)
            if obtenido == getattr(esperado, campo):
                correctos += 1
            else:
                detalles.append(
                    f"precios: {categoria} {campo} = {obtenido}, "
                    f"la muestra dice {getattr(esperado, campo)}"
                )
    return MetricasPrecios(
        categorias_esperadas=len(esperados),
        faltantes=faltantes,
        sobrantes=sobrantes,
        campos_esperados=len(esperados) * len(CAMPOS_PRECIO),
        campos_correctos=correctos,
    )


def evaluar_extraccion(
    muestra: MuestraRevisada, tablas: Sequence[TablaExtraida], *, fuente: Fuente
) -> EvaluacionExtraccion:
    detalles: list[str] = []
    filas = _evaluar_filas(muestra, tablas, detalles)
    precios = _evaluar_precios(muestra, tablas, detalles)
    return EvaluacionExtraccion(
        pdf=muestra.pdf,
        sha256=muestra.sha256,
        estado=muestra.estado,
        fuente=fuente,
        filas=filas,
        precios=precios,
        detalles=detalles,
    )


def combinar(evaluaciones: Sequence[EvaluacionExtraccion]) -> TotalExtraccion:
    def suma(campo: str, de: str) -> int:
        return sum(getattr(getattr(e, de), campo) for e in evaluaciones)

    return TotalExtraccion(
        muestras=len(evaluaciones),
        revisadas=sum(e.revisada for e in evaluaciones),
        filas=MetricasFilas(**{c: suma(c, "filas") for c in MetricasFilas.model_fields}),
        precios=MetricasPrecios(
            categorias_esperadas=suma("categorias_esperadas", "precios"),
            faltantes=[c for e in evaluaciones for c in e.precios.faltantes],
            sobrantes=[c for e in evaluaciones for c in e.precios.sobrantes],
            campos_esperados=suma("campos_esperados", "precios"),
            campos_correctos=suma("campos_correctos", "precios"),
        ),
    )
