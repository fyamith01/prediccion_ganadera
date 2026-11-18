"""Métricas de trayectoria de los agentes a partir del registro de corridas.

Evaluar un sistema multiagente no es solo mirar el resultado final: también importa el
camino. Cada corrida deja en el registro (`data/corridas/corridas.jsonl` y, con DSN, la
tabla `operacion.runs`) el resumen de cada paso; de ahí se calcula:

- por corrida: estado, duración total y por paso, tokens y costo, pasos omitidos y si
  fue reanudada tras una caída;
- del verificador: cuántas tablas terminó en cada estado, cuántas llamadas al modelo de
  visión y cuántas rectificaciones hizo por tabla, qué tipo de rectificación usó y qué
  fracción resolvió sin intervención humana (las que no fueron a la cola de revisión);
- del recolector: enlaces, cobertura del calendario de subastas, fechas decididas por la
  IA, enlaces en revisión y descargas.

Solo lee: no recalcula nada de los agentes ni vuelve a consultar ningún modelo.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict

from prediccion_ganadera.puertos.corridas import RegistroCorrida

ESTADOS_TERMINALES = ("completada", "fallida", "interrumpida")
RESUELTAS = ("verificada", "corregida")


def _razon(a: float, b: float) -> float | None:
    return a / b if b else None


class MetricasVerificador(BaseModel):
    model_config = ConfigDict(frozen=True)

    tablas: int = 0
    por_estado: dict[str, int] = {}
    llamadas_vision: int = 0
    rectificaciones: int = 0
    acciones_por_tipo: dict[str, int] = {}
    max_rectificaciones_tabla: int = 0
    pendientes_por_presupuesto: int = 0

    @property
    def con_decision(self) -> int:
        """Tablas con decisión final: resueltas o enviadas a revisión humana."""
        return sum(self.por_estado.get(e, 0) for e in (*RESUELTAS, "en_revision"))

    @property
    def tasa_automatica(self) -> float | None:
        return _razon(sum(self.por_estado.get(e, 0) for e in RESUELTAS), self.con_decision)

    @property
    def tasa_revision(self) -> float | None:
        return _razon(self.por_estado.get("en_revision", 0), self.con_decision)

    @property
    def vision_por_tabla(self) -> float | None:
        return _razon(self.llamadas_vision, self.tablas)

    @property
    def rectificaciones_por_tabla(self) -> float | None:
        return _razon(self.rectificaciones, self.tablas)


class MetricasRecolector(BaseModel):
    model_config = ConfigDict(frozen=True)

    enlaces: int
    fechas_esperadas: int
    fechas_faltantes: int
    decisiones_ia: int  # fechas en conflicto que el modelo resolvió
    en_revision: int  # enlaces que quedaron para revisión manual
    descargados: int
    errores_descarga: int

    @property
    def cobertura(self) -> float | None:
        return _razon(self.fechas_esperadas - self.fechas_faltantes, self.fechas_esperadas)


class MetricasCorrida(BaseModel):
    model_config = ConfigDict(frozen=True)

    id_corrida: str
    estado: str
    duracion_s: float | None  # solo si la corrida terminó
    tokens: int
    costo_usd: float
    duracion_pasos: dict[str, float]
    omitidos: dict[str, str]
    reanudada: bool | None  # None si la fuente no guarda el historial
    error: str | None
    recolector: MetricasRecolector | None
    verificador: MetricasVerificador | None


class MetricasTrayectorias(BaseModel):
    model_config = ConfigDict(frozen=True)

    corridas: int
    por_estado: dict[str, int]
    reanudadas: int | None
    tokens_totales: int
    costo_total_usd: float
    duracion_media_pasos: dict[str, float]
    omisiones: dict[str, int]  # «paso: motivo» -> veces
    errores: dict[str, str]  # id de corrida -> error
    verificador: MetricasVerificador  # suma de todas las corridas
    recolector_ultima: MetricasRecolector | None  # de la corrida más reciente que recolectó
    detalle: list[MetricasCorrida]

    @property
    def tasa_exito(self) -> float | None:
        """Completadas entre las terminadas (las que siguen en curso no cuentan)."""
        terminadas = sum(self.por_estado.get(e, 0) for e in ESTADOS_TERMINALES)
        return _razon(self.por_estado.get("completada", 0), terminadas)


def _verificador(resumen: Mapping[str, Any]) -> MetricasVerificador:
    tablas = resumen.get("tablas", [])
    acciones = [a.split(":")[0].strip() for t in tablas for a in t.get("acciones", [])]
    return MetricasVerificador(
        tablas=len(tablas),
        por_estado=dict(Counter(t["estado"] for t in tablas)),
        llamadas_vision=sum(t.get("verificaciones", 0) for t in tablas),
        rectificaciones=len(acciones),
        acciones_por_tipo=dict(sorted(Counter(acciones).items())),
        max_rectificaciones_tabla=max((len(t.get("acciones", [])) for t in tablas), default=0),
        pendientes_por_presupuesto=sum(
            t["estado"] == "pendiente" and "presupuesto" in t.get("motivo", "") for t in tablas
        ),
    )


def _sumar(partes: Sequence[MetricasVerificador]) -> MetricasVerificador:
    estados: Counter[str] = Counter()
    acciones: Counter[str] = Counter()
    for parte in partes:
        estados.update(parte.por_estado)
        acciones.update(parte.acciones_por_tipo)
    return MetricasVerificador(
        tablas=sum(p.tablas for p in partes),
        por_estado=dict(estados),
        llamadas_vision=sum(p.llamadas_vision for p in partes),
        rectificaciones=sum(p.rectificaciones for p in partes),
        acciones_por_tipo=dict(sorted(acciones.items())),
        max_rectificaciones_tabla=max((p.max_rectificaciones_tabla for p in partes), default=0),
        pendientes_por_presupuesto=sum(p.pendientes_por_presupuesto for p in partes),
    )


def _recolector(resumen: Mapping[str, Any]) -> MetricasRecolector:
    huecos = resumen.get("huecos") or {}
    descarga = resumen.get("descarga") or {}
    return MetricasRecolector(
        enlaces=resumen.get("enlaces", 0),
        fechas_esperadas=len(huecos.get("esperadas", [])),
        fechas_faltantes=len(huecos.get("faltantes", [])),
        decisiones_ia=len(resumen.get("decisiones", [])),
        en_revision=len(resumen.get("revision", [])),
        descargados=len(descarga.get("nuevos", [])),
        errores_descarga=len(descarga.get("errores", [])),
    )


def _paso_ejecutado(pasos: Mapping[str, Any], nombre: str) -> Mapping[str, Any] | None:
    paso = pasos.get(nombre)
    return paso["resumen"] if paso and not paso.get("omitido") else None


def _corrida(registro: RegistroCorrida, reanudada: bool | None) -> MetricasCorrida:
    terminada = registro.terminada_en
    recoleccion = _paso_ejecutado(registro.pasos, "recolectar")
    verificacion = _paso_ejecutado(registro.pasos, "verificar")
    return MetricasCorrida(
        id_corrida=registro.id_corrida,
        estado=registro.estado,
        duracion_s=(terminada - registro.iniciada_en).total_seconds() if terminada else None,
        tokens=registro.tokens,
        costo_usd=registro.costo_usd,
        duracion_pasos={n: p.get("duracion_s", 0.0) for n, p in registro.pasos.items()},
        omitidos={n: p["omitido"] for n, p in registro.pasos.items() if p.get("omitido")},
        reanudada=reanudada,
        error=registro.error,
        recolector=_recolector(recoleccion) if recoleccion is not None else None,
        verificador=_verificador(verificacion) if verificacion is not None else None,
    )


def evaluar_trayectorias(
    historial: Sequence[RegistroCorrida], *, con_historial: bool
) -> MetricasTrayectorias:
    """`historial`: versiones en orden de escritura. Con `con_historial=False` (p. ej.
    `operacion.runs`, que guarda solo la última versión) no se afirma nada sobre
    reanudaciones."""
    ultimas: dict[str, RegistroCorrida] = {}
    inicios: Counter[str] = Counter()
    for registro in historial:
        ultimas[registro.id_corrida] = registro
        inicios[registro.id_corrida] += registro.estado == "en_curso"

    detalle = [_corrida(r, (inicios[i] > 1) if con_historial else None) for i, r in ultimas.items()]
    duraciones: dict[str, list[float]] = {}
    omisiones: Counter[str] = Counter()
    for corrida in detalle:
        for nombre, duracion in corrida.duracion_pasos.items():
            duraciones.setdefault(nombre, []).append(duracion)
        omisiones.update(f"{n}: {m}" for n, m in corrida.omitidos.items())
    con_recoleccion = [c.recolector for c in detalle if c.recolector is not None]
    return MetricasTrayectorias(
        corridas=len(detalle),
        por_estado=dict(Counter(c.estado for c in detalle)),
        reanudadas=sum(bool(c.reanudada) for c in detalle) if con_historial else None,
        tokens_totales=sum(c.tokens for c in detalle),
        costo_total_usd=sum(c.costo_usd for c in detalle),
        duracion_media_pasos={n: sum(d) / len(d) for n, d in duraciones.items()},
        omisiones=dict(omisiones),
        errores={c.id_corrida: c.error for c in detalle if c.error and c.estado != "completada"},
        verificador=_sumar([c.verificador for c in detalle if c.verificador is not None]),
        recolector_ultima=con_recoleccion[-1] if con_recoleccion else None,
        detalle=detalle,
    )
