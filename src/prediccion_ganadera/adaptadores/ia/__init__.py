"""Adaptadores de IA y la fábrica que elige uno por agente según la configuración."""

from __future__ import annotations

from prediccion_ganadera.adaptadores.ia.cli_harness import ProveedorCliHarness
from prediccion_ganadera.configuracion import BackendCliHarness, Configuracion
from prediccion_ganadera.puertos.ia import ErrorProveedorIA, ProveedorIA


def crear_proveedor(config: Configuracion, agente: str) -> ProveedorIA:
    """Proveedor de IA del agente; `ErrorConfiguracion` si el agente no existe."""
    backend = config.backend_de(agente)
    if isinstance(backend, BackendCliHarness):
        return ProveedorCliHarness(backend)
    # El adaptador de Ollama (visión local) llega con el verificador visual (T4).
    raise ErrorProveedorIA(
        f"el backend '{backend.tipo}' del agente '{agente}' aún no está disponible"
    )
