"""Cifras del reporte periódico, calculadas en código.

El agente reportero no calcula nada: recibe estas cifras ya formateadas (los «hechos»)
y solo redacta la narrativa. La barrera contra cifras inventadas compara los números
del texto redactado con los números de los hechos: cualquier número que no aparezca
tal cual en ellos se rechaza. Los formatos siguen la convención colombiana (punto de
miles, coma decimal) y las fechas van como dd/mm/aaaa.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict

from prediccion_ganadera.adaptadores.almacen.oro import PrediccionOro
from prediccion_ganadera.modelado.modelador import ResumenModelado

_NUMERO = re.compile(r"\d+(?:[.,]\d+)*")


def formato_numero(valor: float, *, decimales: int = 0) -> str:
    texto = f"{valor:,.{decimales}f}"  # 1,234,567.89
    return texto.replace(",", "_").replace(".", ",").replace("_", ".")


def formato_porcentaje(fraccion: float, *, signo: bool = False) -> str:
    texto = formato_numero(fraccion * 100, decimales=1)
    if signo and fraccion >= 0:
        texto = f"+{texto}"
    return f"{texto} %"


def formato_fecha(fecha: date) -> str:
    return fecha.strftime("%d/%m/%Y")


def numeros_de(texto: str) -> set[str]:
    """Los números escritos en un texto, tal como aparecen (sin el signo)."""
    return set(_NUMERO.findall(texto))


def numeros_no_permitidos(texto: str, permitidos: Iterable[str]) -> list[str]:
    """Números del texto que no están en `permitidos`, en orden de aparición."""
    conjunto = set(permitidos)
    vistos: list[str] = []
    for numero in _NUMERO.findall(texto):
        if numero not in conjunto and numero not in vistos:
            vistos.append(numero)
    return vistos


class _Modelo(BaseModel):
    model_config = ConfigDict(frozen=True)


class FilaHorizonte(_Modelo):
    horizonte: int
    mae: float
    mae_ingenuo: float
    mejora: float


class FilaRecomendacion(_Modelo):
    categoria: str
    mejor_fecha: date
    horizonte: int
    ultimo_precio: float
    precio_predicho: float
    variacion_esperada: float
    error_esperado: float
    distinguible: bool


class Narrativa(_Modelo):
    resumen: str
    puntos: list[str]
    origen: Literal["ia", "determinista"]
    motivo: str | None = None  # por qué no se usó la IA, si aplica


class CifrasReporte(_Modelo):
    version: str
    fuente: str
    incluye_no_verificadas: bool
    fecha_desde: date
    fecha_hasta: date
    fechas_historico: int
    precios: int
    descartes: int
    tablas_omitidas: int
    mae: float
    mae_ingenuo: float
    mape: float
    mejora: float
    pliegues: int
    confianza: str
    motivos_confianza: list[str]
    horizontes: list[FilaHorizonte]
    recomendaciones: list[FilaRecomendacion]
    predicciones: list[PrediccionOro]

    @property
    def fechas_futuras(self) -> list[date]:
        return sorted({p.fecha for p in self.predicciones})

    def hechos(self) -> str:
        """Las cifras como texto: lo único que el modelo puede citar."""
        lineas = [
            f"Versión del modelo: {self.version}",
            f"Histórico: {self.fechas_historico} subastas entre {formato_fecha(self.fecha_desde)}"
            f" y {formato_fecha(self.fecha_hasta)} ({self.precios} precios por categoría,"
            f" fuente {self.fuente})",
            f"Filas descartadas por inconsistentes: {self.descartes}",
            f"Tablas de resumen omitidas (sin validar o sin fecha): {self.tablas_omitidas}",
            f"Error medio de validación (MAE): {formato_numero(self.mae)} $/kg; línea base"
            f" (último precio): {formato_numero(self.mae_ingenuo)} $/kg; mejora frente a la"
            f" línea base: {formato_porcentaje(self.mejora, signo=True)}",
            f"Error porcentual medio (MAPE): {formato_porcentaje(self.mape)}; pliegues de"
            f" validación temporal: {self.pliegues}",
            f"Confianza: {self.confianza}",
        ]
        if self.incluye_no_verificadas:
            lineas.append("Advertencia: incluye tablas sin verificar visualmente")
        lineas += [f"Motivo de la confianza: {m}" for m in self.motivos_confianza]
        lineas += [
            f"Horizonte {h.horizonte} subasta(s): MAE {formato_numero(h.mae)} $/kg, línea base"
            f" {formato_numero(h.mae_ingenuo)} $/kg, mejora"
            f" {formato_porcentaje(h.mejora, signo=True)}"
            for h in self.horizontes
        ]
        lineas += [
            f"Recomendación {r.categoria}: mejor fecha {formato_fecha(r.mejor_fecha)}, precio"
            f" esperado {formato_numero(r.precio_predicho)} $/kg, último precio"
            f" {formato_numero(r.ultimo_precio)} $/kg, variación"
            f" {formato_porcentaje(r.variacion_esperada, signo=True)}, error esperado ±"
            f"{formato_numero(r.error_esperado)} $/kg, "
            + (
                "la diferencia entre fechas supera el error"
                if r.distinguible
                else "la diferencia entre fechas NO supera el error"
            )
            for r in self.recomendaciones
        ]
        return "\n".join(lineas)

    def numeros_permitidos(self) -> set[str]:
        return numeros_de(self.hechos())


def calcular_cifras(
    resumen: ResumenModelado, predicciones: Iterable[PrediccionOro]
) -> CifrasReporte:
    metricas = resumen.metricas
    return CifrasReporte(
        version=resumen.version,
        fuente=resumen.fuente,
        incluye_no_verificadas=resumen.incluye_no_verificadas,
        fecha_desde=resumen.fecha_desde,
        fecha_hasta=resumen.fecha_hasta,
        fechas_historico=resumen.fechas_historico,
        precios=resumen.precios,
        descartes=sum(resumen.descartes.values()),
        tablas_omitidas=resumen.tablas_omitidas,
        mae=metricas.mae,
        mae_ingenuo=metricas.mae_ingenuo,
        mape=metricas.mape,
        mejora=metricas.mejora,
        pliegues=metricas.pliegues,
        confianza=resumen.confianza.nivel,
        motivos_confianza=list(resumen.confianza.motivos),
        horizontes=[
            FilaHorizonte(horizonte=h, mae=m.mae, mae_ingenuo=m.mae_ingenuo, mejora=m.mejora)
            for h, m in sorted(metricas.por_horizonte.items())
        ],
        recomendaciones=[
            FilaRecomendacion.model_validate(r.model_dump()) for r in resumen.recomendaciones
        ],
        predicciones=sorted(predicciones, key=lambda p: (p.fecha, p.categoria)),
    )


def narrativa_determinista(cifras: CifrasReporte, *, motivo: str | None = None) -> Narrativa:
    """Narrativa armada solo con plantillas sobre los hechos (sin IA)."""
    resumen = (
        f"El modelo {cifras.version} se entrenó con {cifras.fechas_historico} subastas entre"
        f" {formato_fecha(cifras.fecha_desde)} y {formato_fecha(cifras.fecha_hasta)}. En la"
        f" validación temporal su error medio fue de {formato_numero(cifras.mae)} $/kg frente a"
        f" {formato_numero(cifras.mae_ingenuo)} $/kg de la línea base (último precio), una"
        f" mejora de {formato_porcentaje(cifras.mejora, signo=True)}. Confianza:"
        f" {cifras.confianza}."
    )
    puntos = []
    for r in cifras.recomendaciones[:3]:
        punto = (
            f"{r.categoria}: mejor fecha {formato_fecha(r.mejor_fecha)}, precio esperado"
            f" {formato_numero(r.precio_predicho)} $/kg"
            f" ({formato_porcentaje(r.variacion_esperada, signo=True)} frente al último precio,"
            f" error esperado ±{formato_numero(r.error_esperado)} $/kg)."
        )
        if not r.distinguible:
            punto += " La diferencia entre fechas no supera el error: cualquier fecha es similar."
        puntos.append(punto)
    if not cifras.recomendaciones:
        puntos.append("En esta corrida no hay recomendaciones de fecha ni de categoría.")
    puntos += [f"Confianza {cifras.confianza}: {m}." for m in cifras.motivos_confianza]
    return Narrativa(resumen=resumen, puntos=puntos, origen="determinista", motivo=motivo)
