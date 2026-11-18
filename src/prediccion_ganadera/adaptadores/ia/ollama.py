"""Adaptador para un modelo local servido por Ollama (`POST /api/generate`).

Las imágenes viajan en base64 dentro de la petición, así que sirve para el verificador
visual con un modelo de visión (p. ej. `qwen2.5vl`). Sin `stream`: una sola respuesta
JSON con el texto y los conteos de tokens. Un modelo local no tiene costo en US$.

Todos los agentes piden JSON, así que se envía `format: "json"`, y `options.num_ctx` fija
la ventana de contexto. Si Ollama corta la respuesta por esa ventana
(`done_reason: "length"`), se avisa con el valor que hay que subir.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from prediccion_ganadera.configuracion import BackendOllama
from prediccion_ganadera.puertos.ia import ErrorProveedorIA, RespuestaIA

_MAX_DETALLE = 300


class ProveedorOllama:
    def __init__(
        self, backend: BackendOllama, transporte: httpx.BaseTransport | None = None
    ) -> None:
        self._backend = backend
        self._transporte = transporte

    def completar(self, prompt: str, imagenes: Sequence[Path] = ()) -> RespuestaIA:
        cuerpo: dict[str, Any] = {"model": self._backend.modelo, "prompt": prompt}
        if imagenes:
            cuerpo["images"] = [_base64(imagen) for imagen in imagenes]
        cuerpo["stream"] = False
        cuerpo["format"] = "json"
        cuerpo["options"] = {"num_ctx": self._backend.num_ctx}
        url = self._backend.url.rstrip("/")
        try:
            with httpx.Client(
                base_url=url, timeout=self._backend.timeout_s, transport=self._transporte
            ) as cliente:
                respuesta = cliente.post("/api/generate", json=cuerpo)
        except httpx.TimeoutException:
            raise ErrorProveedorIA(f"Ollama no respondió en {self._backend.timeout_s} s") from None
        except httpx.TransportError as error:
            raise ErrorProveedorIA(f"no se pudo conectar con Ollama en {url}: {error}") from None
        return _interpretar(respuesta, num_ctx=self._backend.num_ctx)


def _base64(imagen: Path) -> str:
    try:
        return base64.b64encode(Path(imagen).read_bytes()).decode("ascii")
    except OSError as error:
        raise ErrorProveedorIA(f"no se pudo leer la imagen {imagen}: {error}") from None


def _entero(valor: Any) -> int | None:
    return valor if isinstance(valor, int) else None


def _interpretar(respuesta: httpx.Response, *, num_ctx: int) -> RespuestaIA:
    try:
        datos = respuesta.json()
    except ValueError:
        datos = None
    if respuesta.status_code != 200:
        detalle = datos.get("error") if isinstance(datos, dict) else respuesta.text
        raise ErrorProveedorIA(
            f"Ollama respondió {respuesta.status_code}: {str(detalle)[:_MAX_DETALLE]}"
        )
    if not isinstance(datos, dict):
        raise ErrorProveedorIA(f"respuesta ilegible de Ollama: {respuesta.text[:_MAX_DETALLE]!r}")
    texto = datos.get("response")
    if not isinstance(texto, str):
        raise ErrorProveedorIA("la respuesta de Ollama no trae 'response'")
    if datos.get("done_reason") == "length":
        raise ErrorProveedorIA(
            f"respuesta de Ollama cortada: llenó la ventana de contexto de {num_ctx} tokens; "
            "sube `num_ctx` del backend en config/agentes.yaml"
        )
    return RespuestaIA(
        texto=texto.strip(),
        tokens_entrada=_entero(datos.get("prompt_eval_count")),
        tokens_salida=_entero(datos.get("eval_count")),
    )
