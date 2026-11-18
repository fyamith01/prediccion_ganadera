"""Agente recolector: corrobora qué subastas están publicadas y trae sus PDFs a bronce.

Las herramientas deterministas hacen el trabajo pesado (listar, fechar, reportar huecos,
descargar). El modelo solo interviene donde el código no puede decidir: un enlace cuyo
texto y nombre de archivo traen fechas distintas. Guardarraíles:

- el modelo elige UNA de las fechas candidatas o declina; nunca propone una fecha nueva;
- cualquier respuesta inválida, error del backend o presupuesto agotado manda el enlace
  a revisión manual en vez de aceptarlo en silencio;
- los enlaces en revisión no se descargan: se reevalúan en la siguiente corrida.
"""

from __future__ import annotations

import json
import re
from bisect import bisect_left
from datetime import date, datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from prediccion_ganadera.adaptadores.almacen.bronce import AlmacenBronce
from prediccion_ganadera.herramientas.calendario import ReporteHuecos, reportar_huecos
from prediccion_ganadera.herramientas.descarga import ResultadoDescarga, descargar_nuevos
from prediccion_ganadera.herramientas.enlaces import EnlaceSubasta, EstadoFecha, listar_enlaces
from prediccion_ganadera.puertos.ia import ErrorProveedorIA, ProveedorIA, UsoIA

_OBJETO_JSON = re.compile(r"\{.*\}", re.DOTALL)

PROMPT_CONFLICTO = """Eres el agente recolector de un sistema que descarga resúmenes de subastas \
ganaderas. Un enlace trae fechas contradictorias y debes elegir la fecha real de la subasta.

Enlace: {url}
Texto del enlace: "{texto}"
Fechas candidatas: {candidatas}
Subastas publicadas más cercanas: {vecinas}
Regla del negocio: hay una subasta por semana, normalmente el viernes; el archivo suele subirse \
en el mes de la subasta o en el siguiente (la ruta /AAAA/MM/ indica el mes de subida).

Responde SOLO con JSON: {{"fecha": "AAAA-MM-DD" o null, "razon": "<una frase>"}}.
La fecha debe ser exactamente una de las candidatas; usa null si la evidencia no alcanza."""


class FuenteRecoleccion(Protocol):
    def obtener_html(self, url: str) -> str: ...

    def descargar(self, url: str) -> bytes: ...


class _Modelo(BaseModel):
    model_config = ConfigDict(frozen=True)


class DecisionFecha(_Modelo):
    url: str
    fecha: date
    razon: str


class EnlaceEnRevision(_Modelo):
    url: str
    texto: str
    candidatas: tuple[date, ...]
    motivo: str


class ResultadoRecoleccion(_Modelo):
    enlaces: int
    decisiones: list[DecisionFecha]
    revision: list[EnlaceEnRevision]
    huecos: ReporteHuecos
    descarga: ResultadoDescarga | None
    uso: UsoIA


class AgenteRecolector:
    def __init__(
        self,
        *,
        ia: ProveedorIA | None,
        fuente: FuenteRecoleccion,
        almacen: AlmacenBronce,
        url_pagina: str,
        dominios_permitidos: list[str],
        presupuesto_tokens: int = 200_000,
    ) -> None:
        self._ia = ia
        self._fuente = fuente
        self._almacen = almacen
        self._url_pagina = url_pagina
        self._dominios = dominios_permitidos
        self._presupuesto = presupuesto_tokens

    def ejecutar(
        self,
        *,
        html: str | None = None,
        descargar: bool = True,
        desde: date | None = None,
        hasta: date | None = None,
        ahora: datetime | None = None,
    ) -> ResultadoRecoleccion:
        if html is None:
            html = self._fuente.obtener_html(self._url_pagina)
        enlaces = listar_enlaces(html, self._url_pagina, self._dominios)
        publicadas = sorted(e.fecha for e in enlaces if e.fecha is not None)
        uso = UsoIA()
        decisiones: list[DecisionFecha] = []
        revision: list[EnlaceEnRevision] = []
        for enlace in enlaces:
            if enlace.estado is EstadoFecha.RESUELTA:
                continue
            decision = self._decidir(enlace, publicadas, uso)
            if isinstance(decision, DecisionFecha):
                decisiones.append(decision)
            else:
                revision.append(
                    EnlaceEnRevision(
                        url=enlace.url,
                        texto=enlace.texto,
                        candidatas=enlace.candidatas,
                        motivo=decision,
                    )
                )

        fechas = publicadas + [d.fecha for d in decisiones]
        huecos = reportar_huecos(
            fechas,
            desde=desde or min(fechas, default=hasta or date.today()),
            hasta=hasta or max(fechas, default=desde or date.today()),
        )
        descarga = None
        if descargar:
            en_revision = {r.url for r in revision}
            descarga = descargar_nuevos(
                [e for e in enlaces if e.url not in en_revision],
                self._fuente,
                self._almacen,
                fechas_decididas={d.url: d.fecha for d in decisiones},
                ahora=ahora,
            )
        return ResultadoRecoleccion(
            enlaces=len(enlaces),
            decisiones=decisiones,
            revision=revision,
            huecos=huecos,
            descarga=descarga,
            uso=uso,
        )

    def _decidir(
        self, enlace: EnlaceSubasta, publicadas: list[date], uso: UsoIA
    ) -> DecisionFecha | str:
        """Una `DecisionFecha` o el motivo por el que el enlace va a revisión."""
        if enlace.estado is EstadoFecha.SIN_FECHA:
            return "ni el texto ni el nombre del archivo traen una fecha válida"
        if self._ia is None:
            return "conflicto de fechas y la consulta a la IA está desactivada"
        if uso.tokens >= self._presupuesto:
            return f"conflicto de fechas y el presupuesto de {self._presupuesto} tokens se agotó"
        prompt = PROMPT_CONFLICTO.format(
            url=enlace.url,
            texto=enlace.texto,
            candidatas=", ".join(f.isoformat() for f in enlace.candidatas),
            vecinas=", ".join(f.isoformat() for f in _vecinas(publicadas, enlace.candidatas[0])),
        )
        try:
            respuesta = self._ia.completar(prompt)
        except ErrorProveedorIA as error:
            return f"el backend de IA falló: {error}"
        finally:
            uso.llamadas_ia += 1
        uso.registrar(respuesta)
        return _validar_decision(enlace, respuesta.texto)


def _vecinas(publicadas: list[date], referencia: date, cantidad: int = 2) -> list[date]:
    posicion = bisect_left(publicadas, referencia)
    return publicadas[max(0, posicion - cantidad) : posicion + cantidad]


def _validar_decision(enlace: EnlaceSubasta, texto: str) -> DecisionFecha | str:
    coincidencia = _OBJETO_JSON.search(texto)
    try:
        datos = json.loads(coincidencia.group(0)) if coincidencia else None
    except json.JSONDecodeError:
        datos = None
    if not isinstance(datos, dict):
        return f"respuesta ilegible de la IA: {texto[:200]!r}"
    razon = str(datos.get("razon", "")).strip()
    if datos.get("fecha") is None:
        return f"la IA declinó decidir: {razon}"
    try:
        fecha = date.fromisoformat(str(datos["fecha"]))
    except ValueError:
        return f"la IA devolvió una fecha inválida: {datos['fecha']!r}"
    if fecha not in enlace.candidatas:
        return f"la IA propuso {fecha}, que no está entre las candidatas"
    return DecisionFecha(url=enlace.url, fecha=fecha, razon=razon)
