"""Agente reportero: redacta la narrativa del reporte sobre cifras calculadas en código.

El modelo recibe los «hechos» (cifras ya formateadas por `herramientas.cifras_reporte`) y
devuelve un resumen y unas viñetas en JSON. Guardarraíles:

- todo número de la narrativa debe aparecer tal cual en los hechos; si no, se rechaza y
  se pide una vez más indicando qué números sobran;
- una respuesta ilegible también se reintenta una vez;
- si el backend falla, el presupuesto de tokens se agota o los intentos se acaban, el
  reporte usa la narrativa determinista (plantillas) y dice por qué: nunca se publica
  un texto con cifras que el código no calculó.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, ConfigDict

from prediccion_ganadera.herramientas.cifras_reporte import (
    CifrasReporte,
    Narrativa,
    narrativa_determinista,
    numeros_no_permitidos,
)
from prediccion_ganadera.puertos.ia import ErrorProveedorIA, ProveedorIA, UsoIA

_OBJETO_JSON = re.compile(r"\{.*\}", re.DOTALL)
MAX_PUNTOS = 5

PROMPT_NARRATIVA = """Eres el agente reportero de un sistema que predice precios de subastas \
ganaderas en Colombia. Escribe en español, para un ganadero, la narrativa del reporte de \
esta corrida del modelo.

Hechos (las únicas cifras que puedes usar):
{hechos}

Reglas:
- No calcules, redondees ni conviertas cifras: copia cada número tal cual aparece en los \
hechos (punto de miles, coma decimal, fechas dd/mm/aaaa).
- No escribas ningún número que no esté en los hechos (ni años, ni conteos, ni porcentajes \
nuevos); si necesitas una cantidad que no está, descríbela con palabras.
- Di cuál es la confianza del modelo y, si la diferencia entre fechas no supera el error, \
dilo: en ese caso ninguna fecha es claramente mejor.
- No des consejos financieros más allá de lo que dicen los hechos.

Responde SOLO con JSON: {{"resumen": "<un párrafo>", "puntos": ["<viñeta>", ...]}} con \
máximo {max_puntos} viñetas."""

CORRECCION = """

Tu respuesta anterior fue rechazada: {motivo}. Escríbela de nuevo cumpliendo las reglas."""


class ResultadoReporte(BaseModel):
    model_config = ConfigDict(frozen=True)

    narrativa: Narrativa
    rechazos: list[str]  # por qué se rechazó cada respuesta de la IA
    uso: UsoIA


class AgenteReportero:
    def __init__(
        self,
        *,
        ia: ProveedorIA | None,
        presupuesto_tokens: int = 200_000,
        max_intentos: int = 2,
    ) -> None:
        self._ia = ia
        self._presupuesto = presupuesto_tokens
        self._max_intentos = max_intentos

    def redactar(self, cifras: CifrasReporte) -> ResultadoReporte:
        uso = UsoIA()
        rechazos: list[str] = []

        def respaldo(motivo: str) -> ResultadoReporte:
            return ResultadoReporte(
                narrativa=narrativa_determinista(cifras, motivo=motivo), rechazos=rechazos, uso=uso
            )

        if self._ia is None:
            return respaldo("la consulta a la IA está desactivada")
        base = PROMPT_NARRATIVA.format(hechos=cifras.hechos(), max_puntos=MAX_PUNTOS)
        prompt = base
        permitidos = cifras.numeros_permitidos()
        for _ in range(self._max_intentos):
            if uso.tokens >= self._presupuesto:
                return respaldo(f"el presupuesto de {self._presupuesto} tokens se agotó")
            try:
                respuesta = self._ia.completar(prompt)
            except ErrorProveedorIA as error:
                return respaldo(f"el backend de IA falló: {error}")
            finally:
                uso.llamadas_ia += 1
            uso.registrar(respuesta)
            resultado = _validar(respuesta.texto, permitidos)
            if isinstance(resultado, Narrativa):
                return ResultadoReporte(narrativa=resultado, rechazos=rechazos, uso=uso)
            rechazos.append(resultado)
            prompt = base + CORRECCION.format(motivo=resultado)
        return respaldo(f"tras {self._max_intentos} intentos: {rechazos[-1]}")


def _validar(texto: str, permitidos: set[str]) -> Narrativa | str:
    """La narrativa de la IA, o el motivo del rechazo."""
    coincidencia = _OBJETO_JSON.search(texto)
    try:
        datos = json.loads(coincidencia.group(0)) if coincidencia else None
    except json.JSONDecodeError:
        datos = None
    if not isinstance(datos, dict):
        return f"respuesta ilegible de la IA: {texto[:200]!r}"
    resumen, puntos = datos.get("resumen"), datos.get("puntos", [])
    if not isinstance(resumen, str) or not resumen.strip():
        return "respuesta ilegible de la IA: falta el resumen"
    if not isinstance(puntos, list) or not all(isinstance(p, str) for p in puntos):
        return "respuesta ilegible de la IA: «puntos» debe ser una lista de textos"
    puntos = [p.strip() for p in puntos if p.strip()][:MAX_PUNTOS]
    sobrantes = numeros_no_permitidos(" ".join([resumen, *puntos]), permitidos)
    if sobrantes:
        return f"la IA usó números que no están en las cifras: {', '.join(sobrantes)}"
    return Narrativa(resumen=resumen.strip(), puntos=puntos, origen="ia")
