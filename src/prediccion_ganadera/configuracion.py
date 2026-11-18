"""Carga y validación de `config/agentes.yaml`.

Define qué backend de IA usa cada agente, los límites de los bucles y las rutas
de trabajo. Todo error de configuración se reporta como `ErrorConfiguracion`
antes de lanzar una corrida.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

MARCADOR_PROMPT = "{prompt}"


class ErrorConfiguracion(ValueError):
    """La configuración no existe, no es YAML válido o no cumple el esquema."""


class _Estricto(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BackendCliHarness(_Estricto):
    """Harness de agentes en modo headless (claude -p, agy -p, agent -p)."""

    tipo: Literal["cli_harness"]
    comando: list[str] = Field(min_length=1)
    timeout_s: int = Field(default=300, gt=0)

    @field_validator("comando")
    @classmethod
    def _exige_marcador(cls, comando: list[str]) -> list[str]:
        if sum(arg.count(MARCADOR_PROMPT) for arg in comando) != 1:
            raise ValueError(
                f"el comando debe contener exactamente un argumento '{MARCADOR_PROMPT}'"
            )
        return comando

    def argv(self, prompt: str) -> list[str]:
        """Lista de argumentos para `subprocess` sin shell: el prompt nunca se interpreta."""
        return [arg.replace(MARCADOR_PROMPT, prompt) for arg in self.comando]


class BackendOllama(_Estricto):
    """Modelo local servido por Ollama."""

    tipo: Literal["ollama"]
    modelo: str = Field(min_length=1)
    url: str = "http://localhost:11434"
    timeout_s: int = Field(default=600, gt=0)


Backend = Annotated[BackendCliHarness | BackendOllama, Field(discriminator="tipo")]


class AsignacionAgente(_Estricto):
    backend: str


class Limites(_Estricto):
    max_iteraciones_rectificacion: int = Field(default=3, ge=1)
    presupuesto_tokens_corrida: int = Field(default=200_000, gt=0)
    confianza_minima_verificacion: float = Field(default=0.8, ge=0, le=1)


class Rutas(_Estricto):
    datos: Path = Path("data")
    reportes: Path = Path("reportes")


class Fuente(_Estricto):
    url: str
    dominios_permitidos: list[str] = Field(min_length=1)


class Configuracion(_Estricto):
    version: Literal[1]
    backends: dict[str, Backend]
    agentes: dict[str, AsignacionAgente]
    limites: Limites = Limites()
    rutas: Rutas = Rutas()
    fuente: Fuente | None = None

    @model_validator(mode="after")
    def _agentes_apuntan_a_backends_existentes(self) -> Configuracion:
        for agente, asignacion in self.agentes.items():
            if asignacion.backend not in self.backends:
                disponibles = ", ".join(sorted(self.backends)) or "ninguno"
                raise ValueError(
                    f"el agente '{agente}' usa el backend '{asignacion.backend}', "
                    f"que no está definido (disponibles: {disponibles})"
                )
        return self

    def backend_de(self, agente: str) -> BackendCliHarness | BackendOllama:
        try:
            return self.backends[self.agentes[agente].backend]
        except KeyError:
            raise ErrorConfiguracion(f"el agente '{agente}' no está configurado") from None


def cargar_configuracion(ruta: str | Path) -> Configuracion:
    ruta = Path(ruta)
    try:
        datos = yaml.safe_load(ruta.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ErrorConfiguracion(f"no existe el archivo de configuración: {ruta}") from None
    except yaml.YAMLError as error:
        raise ErrorConfiguracion(f"YAML inválido en {ruta}: {error}") from None
    try:
        return Configuracion.model_validate(datos)
    except ValidationError as error:
        raise ErrorConfiguracion(f"configuración inválida en {ruta}:\n{error}") from None
