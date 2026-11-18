"""Agente verificador visual con bucle de rectificación.

Para cada tabla pendiente de plata:

1. Comprobaciones deterministas contra el texto de la página (filas, totales). Si
   fallan, rectifica sin IA: re-extrae la página con ajustes alternativos y se queda
   con la variante que deje menos hallazgos; si ninguna mejora y la tabla es la última
   de la página, anexa los renglones faltantes copiados literalmente de la página.
2. Con las comprobaciones en limpio, el modelo de visión compara la imagen de la página
   con las celdas y devuelve un veredicto estructurado. Sus correcciones de celdas solo
   se aplican si el valor corregido aparece en el texto de la página: el modelo no
   puede introducir cifras que el PDF no tenga.
3. Como máximo `max_rectificaciones` rectificaciones por tabla. Lo que no queda
   aceptado va a la cola de revisión (`en_revision`); nunca se acepta en silencio.
   Si se agota el presupuesto de tokens la tabla sigue `pendiente` para otra corrida.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from prediccion_ganadera.adaptadores.almacen.bronce import AlmacenBronce
from prediccion_ganadera.adaptadores.almacen.plata import AlmacenPlata, ClaveTabla, TablaPlata
from prediccion_ganadera.adaptadores.almacen.revision import ColaRevision, EntradaRevision
from prediccion_ganadera.herramientas.comprobaciones import Hallazgo, comprobar_pagina
from prediccion_ganadera.herramientas.extraer_tablas_pdf import (
    AJUSTES_ALTERNATIVOS,
    ErrorExtraccion,
    FilaExtraida,
    TablaExtraida,
    extraer_tablas,
)
from prediccion_ganadera.herramientas.paginas import lineas_de_pagina, renderizar_pagina
from prediccion_ganadera.puertos.ia import ErrorProveedorIA, ProveedorIA, UsoIA

_OBJETO_JSON = re.compile(r"\{.*\}", re.DOTALL)

PROMPT_VERIFICACION = """Eres el verificador visual de un sistema que extrae tablas de \
resúmenes de subastas ganaderas. Compara la imagen adjunta (página {pagina} del PDF) con la \
tabla que se extrajo de esa página.

Cada fila empieza con su número (fN) y cada celda con su columna entre corchetes, desde 0:
{filas}

Las comprobaciones automáticas contra el texto de la página no encontraron diferencias.

Revisa que cada celda tenga exactamente el texto que se ve en esa posición de la imagen y \
que no falten filas. Una palabra partida entre dos celdas es un error: corrígela dejando la \
palabra completa en una celda y la otra vacía (""). Copia solo texto visible en la imagen; \
nunca inventes ni calcules valores.

Responde SOLO con JSON:
{{"coincide": true|false, "confianza": <0 a 1>, "encabezado_ok": true|false, \
"filas_faltantes": <entero>, "celdas_erroneas": [{{"fila": <N de fN>, "columna": <entero>, \
"valor_correcto": "<texto>"}}], "comentario": "<una frase>"}}"""


class CeldaErronea(BaseModel):
    fila: int
    columna: int = Field(ge=0)
    valor_correcto: str | None


class VeredictoVisual(BaseModel):
    coincide: bool
    confianza: float = Field(ge=0, le=1)
    encabezado_ok: bool = True
    filas_faltantes: int = Field(default=0, ge=0)
    celdas_erroneas: list[CeldaErronea] = Field(default_factory=list)
    comentario: str = ""


class TrayectoriaTabla(BaseModel):
    """Lo que hizo el agente con una tabla: base para evaluar trayectorias (T9)."""

    model_config = ConfigDict(frozen=True)

    clave: ClaveTabla
    estado: str
    verificaciones: int  # llamadas al modelo de visión
    acciones: list[str]
    motivo: str
    hallazgos: list[str]


class ResultadoVerificacion(BaseModel):
    tablas: list[TrayectoriaTabla]
    uso: UsoIA

    def cuenta(self) -> dict[str, int]:
        return dict(Counter(t.estado for t in self.tablas))


class _Estado:
    """Estado de trabajo de una tabla dentro del bucle."""

    def __init__(self, actual: TablaPlata) -> None:
        self.tabla = actual.tabla
        self.acciones: list[str] = []
        self.verificaciones = 0
        self.hallazgos: list[Hallazgo] = []

    def rectificar(self, tabla: TablaExtraida, accion: str) -> None:
        self.tabla = tabla
        self.acciones.append(accion)


class AgenteVerificador:
    def __init__(
        self,
        *,
        ia: ProveedorIA | None,
        bronce: AlmacenBronce,
        plata: AlmacenPlata,
        cola: ColaRevision,
        dir_paginas: Path,
        max_rectificaciones: int = 3,
        confianza_minima: float = 0.8,
        presupuesto_tokens: int = 200_000,
        ajustes_alternativos: tuple[dict, ...] = AJUSTES_ALTERNATIVOS,
    ) -> None:
        self._ia = ia
        self._bronce = bronce
        self._plata = plata
        self._cola = cola
        self._dir_paginas = Path(dir_paginas)
        self._max = max_rectificaciones
        self._confianza = confianza_minima
        self._presupuesto = presupuesto_tokens
        self._alternativas = ajustes_alternativos
        self._lineas: dict[tuple[str, int], list[str]] = {}

    def ejecutar(self, *, ahora: datetime, limite: int | None = None) -> ResultadoVerificacion:
        uso = UsoIA()
        claves = self._plata.claves_por_estado("pendiente")[:limite]
        trayectorias = [self._verificar(clave, uso, ahora) for clave in claves]
        return ResultadoVerificacion(tablas=trayectorias, uso=uso)

    # -- bucle por tabla -------------------------------------------------------------

    def _verificar(self, clave: ClaveTabla, uso: UsoIA, ahora: datetime) -> TrayectoriaTabla:
        pagina = self._plata.tablas_de_pagina(clave.sha256, clave.pagina)
        actual = next(t for t in pagina if t.clave == clave)
        otras = [t.tabla for t in pagina if t.clave != clave]
        es_ultima = clave.tabla == max(t.clave.tabla for t in pagina)
        estado = _Estado(actual)
        try:
            ruta_pdf = self._bronce.ruta_pdf(clave.sha256)
            lineas = self._lineas_de(ruta_pdf, clave)
            final, motivo = self._bucle(clave, estado, ruta_pdf, lineas, otras, es_ultima, uso)
        except ErrorExtraccion as error:
            final, motivo = "en_revision", f"no se pudo leer el PDF: {error}"
        return self._cerrar(clave, actual, estado, final, motivo, ahora)

    def _bucle(
        self,
        clave: ClaveTabla,
        estado: _Estado,
        ruta_pdf: Path,
        lineas: list[str],
        otras: list[TablaExtraida],
        es_ultima: bool,
        uso: UsoIA,
    ) -> tuple[str, str]:
        """Devuelve (estado final, motivo)."""
        while True:
            estado.hallazgos = comprobar_pagina([estado.tabla, *otras], lineas)[clave.tabla]
            agotado = len(estado.acciones) >= self._max
            if estado.hallazgos:
                if agotado:
                    return "en_revision", self._motivo_limite(estado)
                rectificada = self._rectificar_determinista(
                    estado, ruta_pdf, lineas, otras, es_ultima
                )
                if rectificada is None:
                    return "en_revision", "las comprobaciones fallan y ninguna re-extracción mejora"
                estado.rectificar(*rectificada)
                continue

            if self._ia is None:
                return "pendiente", "cuadra con el texto de la página; sin verificación visual"
            if uso.tokens >= self._presupuesto:
                return "pendiente", f"presupuesto de {self._presupuesto} tokens agotado"

            veredicto = self._consultar(clave, estado, ruta_pdf, uso)
            if isinstance(veredicto, str):
                return "en_revision", veredicto
            if self._acepta(veredicto):
                return ("corregida" if estado.acciones else "verificada"), veredicto.comentario
            if agotado:
                return "en_revision", self._motivo_limite(estado, veredicto)
            corregida, aplicadas, rechazadas = _aplicar_correcciones(
                estado.tabla, veredicto.celdas_erroneas, lineas
            )
            if corregida is None:
                return "en_revision", _motivo_sin_correcciones(
                    veredicto, self._confianza, rechazadas
                )
            estado.rectificar(corregida, f"corregir_celdas: {aplicadas}")

    def _acepta(self, veredicto: VeredictoVisual) -> bool:
        return (
            veredicto.coincide
            and not veredicto.celdas_erroneas
            and veredicto.filas_faltantes == 0
            and veredicto.confianza >= self._confianza
        )

    def _motivo_limite(self, estado: _Estado, veredicto: VeredictoVisual | None = None) -> str:
        detalle = (
            veredicto.comentario if veredicto else "; ".join(h.detalle for h in estado.hallazgos)
        )
        return f"sigue con diferencias después de {self._max} rectificaciones: {detalle}"

    # -- rectificación determinista -----------------------------------------------------

    def _rectificar_determinista(
        self,
        estado: _Estado,
        ruta_pdf: Path,
        lineas: list[str],
        otras: list[TablaExtraida],
        es_ultima: bool,
    ) -> tuple[TablaExtraida, str] | None:
        actual = estado.tabla
        mejor: tuple[int, TablaExtraida] | None = None
        for ajustes in self._alternativas:
            if ajustes == actual.ajustes:
                continue
            candidatas = extraer_tablas(ruta_pdf, ajustes, paginas={actual.pagina})
            candidata = next((t for t in candidatas if t.indice == actual.indice), None)
            if candidata is None:
                continue
            n = len(comprobar_pagina([candidata, *otras], lineas)[actual.indice])
            if n < len(estado.hallazgos) and (mejor is None or n < mejor[0]):
                mejor = (n, candidata)
        if mejor is not None:
            return mejor[1], f"reextraer: {json.dumps(mejor[1].ajustes, sort_keys=True)}"

        faltantes = [
            h.renglon
            for h in estado.hallazgos
            if h.renglon is not None and h.tipo in ("fila_faltante", "total_faltante")
        ]
        if not (faltantes and es_ultima):
            return None
        siguiente = max(f.indice for f in actual.filas) + 1
        anexas = tuple(
            FilaExtraida(indice=siguiente + i, celdas=(renglon,))
            for i, renglon in enumerate(sorted(faltantes, key=lineas.index))
        )
        tabla = actual.model_copy(update={"filas": actual.filas + anexas})
        return tabla, f"anexar_renglones: {len(anexas)}"

    # -- consulta al modelo -------------------------------------------------------------

    def _consultar(
        self, clave: ClaveTabla, estado: _Estado, ruta_pdf: Path, uso: UsoIA
    ) -> VeredictoVisual | str:
        """Veredicto del modelo, o el motivo por el que la tabla va a revisión."""
        assert self._ia is not None
        imagen = renderizar_pagina(ruta_pdf, clave.pagina, self._dir_paginas / clave.sha256)
        prompt = PROMPT_VERIFICACION.format(
            pagina=clave.pagina,
            filas=_filas_para_prompt(estado.tabla),
        )
        estado.verificaciones += 1
        uso.llamadas_ia += 1
        try:
            respuesta = self._ia.completar(prompt, imagenes=[imagen])
        except ErrorProveedorIA as error:
            return f"el backend de IA falló: {error}"
        uso.registrar(respuesta)
        return _leer_veredicto(respuesta.texto)

    # -- persistencia -------------------------------------------------------------------

    def _lineas_de(self, ruta_pdf: Path, clave: ClaveTabla) -> list[str]:
        llave = (clave.sha256, clave.pagina)
        if llave not in self._lineas:
            self._lineas[llave] = lineas_de_pagina(ruta_pdf, clave.pagina)
        return self._lineas[llave]

    def _cerrar(
        self,
        clave: ClaveTabla,
        actual: TablaPlata,
        estado: _Estado,
        final: str,
        motivo: str,
        ahora: datetime,
    ) -> TrayectoriaTabla:
        if estado.tabla != actual.tabla:
            self._plata.reemplazar_tabla(clave, estado.tabla, final, ahora)
        elif final != actual.estado:
            self._plata.marcar_estado(clave, final)
        hallazgos = [f"{h.tipo}: {h.detalle}" for h in estado.hallazgos]
        if final == "en_revision":
            self._cola.agregar(
                EntradaRevision(
                    clave=clave,
                    motivo=motivo,
                    hallazgos=hallazgos,
                    intentos=len(estado.acciones),
                    creado_en=ahora,
                )
            )
        elif final in ("verificada", "corregida"):
            self._cola.quitar(clave)
        return TrayectoriaTabla(
            clave=clave,
            estado=final,
            verificaciones=estado.verificaciones,
            acciones=estado.acciones,
            motivo=motivo,
            hallazgos=hallazgos,
        )


def _filas_para_prompt(tabla: TablaExtraida) -> str:
    return "\n".join(
        f"f{fila.indice}: "
        + " | ".join(f"[{columna}] {valor or ''}" for columna, valor in enumerate(fila.celdas))
        for fila in tabla.filas
    )


def _leer_veredicto(texto: str) -> VeredictoVisual | str:
    coincidencia = _OBJETO_JSON.search(texto)
    try:
        return VeredictoVisual.model_validate_json(coincidencia.group(0) if coincidencia else "")
    except ValidationError:
        return f"respuesta ilegible del verificador: {texto[:200]!r}"


def _normalizar(texto: str) -> str:
    return " ".join(texto.split())


def _aplicar_correcciones(
    tabla: TablaExtraida, correcciones: list[CeldaErronea], lineas: list[str]
) -> tuple[TablaExtraida | None, int, list[str]]:
    """Tabla corregida (None si ninguna corrección vale), cuántas se aplicaron y las rechazadas."""
    texto_pagina = _normalizar(" ".join(lineas))
    filas = {f.indice: list(f.celdas) for f in tabla.filas}
    rechazadas: list[str] = []
    aplicadas = 0
    for celda in correcciones:
        valor = _normalizar(celda.valor_correcto or "")
        if celda.fila not in filas or celda.columna >= len(filas[celda.fila]):
            rechazadas.append(f"f{celda.fila}[{celda.columna}] no existe")
        elif valor and valor not in texto_pagina:
            rechazadas.append(f"f{celda.fila}[{celda.columna}] = {valor!r} no está en la página")
        else:
            filas[celda.fila][celda.columna] = valor
            aplicadas += 1
    if not aplicadas:
        return None, 0, rechazadas
    nuevas = tuple(FilaExtraida(indice=i, celdas=tuple(c)) for i, c in filas.items())
    return tabla.model_copy(update={"filas": nuevas}), aplicadas, rechazadas


def _motivo_sin_correcciones(
    veredicto: VeredictoVisual, confianza_minima: float, rechazadas: list[str]
) -> str:
    if veredicto.coincide and veredicto.confianza < confianza_minima:
        return (
            f"el verificador dice que coincide con confianza {veredicto.confianza:.2f}, "
            f"menor que la mínima {confianza_minima:.2f}"
        )
    partes = [
        f"el verificador reporta diferencias sin correcciones aplicables: {veredicto.comentario}"
    ]
    if veredicto.filas_faltantes:
        partes.append(f"filas faltantes según la imagen: {veredicto.filas_faltantes}")
    if rechazadas:
        partes.append("rechazadas: " + "; ".join(rechazadas))
    return " | ".join(partes)
