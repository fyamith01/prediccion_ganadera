"""Adaptador para harnesses de agentes en modo headless (`claude -p`, `agy -p`, `agent -p`).

El comando viene de la configuración y se ejecuta sin shell, con el prompt como un
único argumento. La salida se interpreta como el sobre JSON de `--output-format json`
(o como texto plano si el harness se configuró sin JSON). Antigravity niega acciones
no aprobadas con código de salida 0, por eso también se revisa `status`.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from prediccion_ganadera.configuracion import BackendCliHarness
from prediccion_ganadera.puertos.ia import ErrorProveedorIA, RespuestaIA

# Valores de `status` que indican éxito. Supuesto a verificar con una instalación real de
# `agy`: la documentación oficial no enumera los valores posibles.
_ESTADOS_OK = {"success", "ok", "completed"}
_MAX_DETALLE = 500


class ProveedorCliHarness:
    def __init__(
        self,
        backend: BackendCliHarness,
        ejecutar: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self._backend = backend
        self._ejecutar = ejecutar

    def completar(self, prompt: str, imagenes: Sequence[Path] = ()) -> RespuestaIA:
        # Los harness no reciben archivos por argumento: leen la imagen con su propia
        # herramienta de lectura a partir de la ruta absoluta.
        if imagenes:
            rutas = "\n".join(f"- {Path(imagen).resolve()}" for imagen in imagenes)
            prompt = (
                f"{prompt}\n\nImágenes adjuntas (ábrelas con tu herramienta de lectura de "
                f"archivos):\n{rutas}"
            )
        argv = self._backend.argv(prompt)
        try:
            proceso = self._ejecutar(
                argv,
                capture_output=True,
                text=True,
                timeout=self._backend.timeout_s,
                check=False,
            )
        except FileNotFoundError:
            raise ErrorProveedorIA(f"no se encontró el ejecutable '{argv[0]}'") from None
        except subprocess.TimeoutExpired:
            raise ErrorProveedorIA(
                f"'{argv[0]}' no respondió en {self._backend.timeout_s} s"
            ) from None
        if proceso.returncode != 0:
            detalle = (proceso.stderr or proceso.stdout).strip()[-_MAX_DETALLE:]
            raise ErrorProveedorIA(
                f"'{argv[0]}' terminó con código {proceso.returncode}: {detalle}"
            )
        return interpretar_salida(proceso.stdout)


def _entero(valor: Any) -> int | None:
    return valor if isinstance(valor, int) else None


def interpretar_salida(salida: str) -> RespuestaIA:
    """Convierte la salida estándar del harness en una `RespuestaIA`."""
    texto = salida.strip()
    if not texto:
        raise ErrorProveedorIA("el harness no devolvió salida")
    try:
        sobre = json.loads(texto)
    except json.JSONDecodeError:
        return RespuestaIA(texto=texto)
    if not isinstance(sobre, dict):
        return RespuestaIA(texto=texto)
    if sobre.get("is_error") is True:
        raise ErrorProveedorIA(f"el harness reportó un error: {sobre.get('result', '')}")
    estado = sobre.get("status")
    if estado is not None and str(estado).lower() not in _ESTADOS_OK:
        raise ErrorProveedorIA(f"el harness respondió con estado '{estado}'")
    respuesta = sobre.get("result", sobre.get("response"))
    if not isinstance(respuesta, str):
        raise ErrorProveedorIA("el sobre JSON no trae 'result' ni 'response'")
    uso = sobre.get("usage") if isinstance(sobre.get("usage"), dict) else {}
    costo = sobre.get("total_cost_usd")
    return RespuestaIA(
        texto=respuesta.strip(),
        tokens_entrada=_entero(uso.get("input_tokens")),
        tokens_salida=_entero(uso.get("output_tokens")),
        costo_usd=float(costo) if isinstance(costo, int | float) else None,
    )
