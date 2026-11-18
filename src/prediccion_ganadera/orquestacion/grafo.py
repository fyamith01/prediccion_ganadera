"""Grafo de una corrida: recolectar → extraer → verificar → cargar.

Cada nodo envuelve un paso inyectado (el grafo no conoce agentes, almacenes ni backends de
IA). El estado tipado viaja entre nodos y se guarda en un checkpointer SQLite después de
cada paso, de modo que una corrida caída se reanuda en el paso que falló sin repetir los
anteriores.

Condiciones de parada:
- `ErrorPaso` (fallo esperado, p. ej. la fuente no responde) termina la corrida como
  `fallida` sin ejecutar los pasos siguientes.
- Cualquier otra excepción se propaga: el proceso cae y la corrida queda pendiente para
  `reanudar`.
- El presupuesto de tokens es de la corrida: cada paso recibe `presupuesto_restante`.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

ORDEN_PASOS = ("recolectar", "extraer", "verificar", "cargar")

log = logging.getLogger(__name__)


class ErrorPaso(RuntimeError):
    """Fallo esperado de un paso o de la corrida; se reporta, no se reintenta solo."""


class ResultadoPaso(BaseModel):
    """Lo que un paso devuelve al grafo. `resumen` debe ser serializable a JSON."""

    resumen: dict[str, Any] = Field(default_factory=dict)
    tokens: int = Field(default=0, ge=0)
    costo_usd: float = Field(default=0.0, ge=0)
    omitido: str | None = None  # motivo si el paso no se ejecutó (p. ej. sin DSN)


class EstadoCorrida(TypedDict, total=False):
    id_corrida: str
    presupuesto_tokens: int
    presupuesto_restante: int
    tokens: int
    costo_usd: float
    pasos: dict[str, dict[str, Any]]  # nombre -> resumen, uso y duración
    estado: str  # en_curso | completada | fallida
    error: str | None


Paso = Callable[[EstadoCorrida], ResultadoPaso]


@contextmanager
def abrir_checkpointer(ruta: Path) -> Iterator[SqliteSaver]:
    """Checkpointer SQLite en `ruta` (se crea el directorio si falta)."""
    ruta.parent.mkdir(parents=True, exist_ok=True)
    conexion = sqlite3.connect(ruta, check_same_thread=False)
    try:
        yield SqliteSaver(conexion)
    finally:
        conexion.close()


def _nodo(nombre: str, paso: Paso, ultimo: bool) -> Callable[[EstadoCorrida], dict]:
    def nodo(estado: EstadoCorrida) -> dict:
        inicio = time.perf_counter()
        log.info("corrida=%s paso=%s inicio", estado["id_corrida"], nombre)
        try:
            resultado = paso(estado)
        except ErrorPaso as error:
            log.warning("corrida=%s paso=%s error=%s", estado["id_corrida"], nombre, error)
            return {"estado": "fallida", "error": f"{nombre}: {error}"}
        duracion = time.perf_counter() - inicio
        tokens = estado["tokens"] + resultado.tokens
        registro = {
            "resumen": resultado.resumen,
            "omitido": resultado.omitido,
            "tokens": resultado.tokens,
            "costo_usd": resultado.costo_usd,
            "duracion_s": round(duracion, 3),
        }
        log.info(
            "corrida=%s paso=%s fin duracion_s=%.3f tokens=%d omitido=%s",
            estado["id_corrida"],
            nombre,
            duracion,
            resultado.tokens,
            resultado.omitido,
        )
        actualizacion: dict[str, Any] = {
            "pasos": {**estado["pasos"], nombre: registro},
            "tokens": tokens,
            "costo_usd": estado["costo_usd"] + resultado.costo_usd,
            "presupuesto_restante": max(0, estado["presupuesto_tokens"] - tokens),
        }
        if ultimo:
            actualizacion["estado"] = "completada"
        return actualizacion

    return nodo


def construir_grafo(pasos: Mapping[str, Paso], checkpointer: SqliteSaver):
    faltantes = [nombre for nombre in ORDEN_PASOS if nombre not in pasos]
    if faltantes:
        raise ValueError(f"faltan pasos de la corrida: {', '.join(faltantes)}")
    grafo = StateGraph(EstadoCorrida)
    for i, nombre in enumerate(ORDEN_PASOS):
        grafo.add_node(nombre, _nodo(nombre, pasos[nombre], i == len(ORDEN_PASOS) - 1))
    grafo.add_edge(START, ORDEN_PASOS[0])
    for actual, siguiente in zip(ORDEN_PASOS, ORDEN_PASOS[1:], strict=False):
        grafo.add_conditional_edges(
            actual,
            lambda estado, siguiente=siguiente: (
                END if estado.get("estado") == "fallida" else siguiente
            ),
            [siguiente, END],
        )
    grafo.add_edge(ORDEN_PASOS[-1], END)
    return grafo.compile(checkpointer=checkpointer)


class Orquestador:
    """Inicia y reanuda corridas identificadas por `id_corrida` (el hilo del checkpointer)."""

    def __init__(
        self, pasos: Mapping[str, Paso], checkpointer: SqliteSaver, *, presupuesto_tokens: int
    ) -> None:
        self._grafo = construir_grafo(pasos, checkpointer)
        self._presupuesto = presupuesto_tokens

    @staticmethod
    def _config(id_corrida: str) -> dict:
        return {"configurable": {"thread_id": id_corrida}}

    def existe(self, id_corrida: str) -> bool:
        return bool(self._grafo.get_state(self._config(id_corrida)).values)

    def estado(self, id_corrida: str) -> EstadoCorrida:
        return EstadoCorrida(**self._grafo.get_state(self._config(id_corrida)).values)

    def pendientes(self, id_corrida: str) -> tuple[str, ...]:
        return tuple(self._grafo.get_state(self._config(id_corrida)).next)

    def iniciar(self, id_corrida: str) -> EstadoCorrida:
        if self.existe(id_corrida):
            raise ErrorPaso(f"la corrida {id_corrida} ya existe; usa reanudar")
        inicial = EstadoCorrida(
            id_corrida=id_corrida,
            presupuesto_tokens=self._presupuesto,
            presupuesto_restante=self._presupuesto,
            tokens=0,
            costo_usd=0.0,
            pasos={},
            estado="en_curso",
            error=None,
        )
        return self._grafo.invoke(inicial, self._config(id_corrida))

    def reanudar(self, id_corrida: str) -> EstadoCorrida:
        if not self.existe(id_corrida):
            raise ErrorPaso(f"la corrida {id_corrida} no existe")
        if not self.pendientes(id_corrida):
            raise ErrorPaso(f"la corrida {id_corrida}: no hay pasos pendientes")
        return self._grafo.invoke(None, self._config(id_corrida))
