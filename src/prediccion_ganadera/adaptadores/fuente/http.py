"""Adaptador HTTP de la fuente de PDFs.

Guardarraíles: solo hace GET a los dominios permitidos (también tras una redirección),
espacia las solicitudes y reintenta con espera exponencial los errores transitorios.
La fuente responde 406 a clientes sin cabeceras de navegador, por eso se envían.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from urllib.parse import urljoin, urlsplit

import httpx

CABECERAS_NAVEGADOR = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/130.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
    "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
}
_TRANSITORIOS = {429, 500, 502, 503, 504}
_MAX_REDIRECCIONES = 5


class ErrorFuente(RuntimeError):
    """La fuente no entregó el recurso o la URL viola los guardarraíles."""


class FuenteHttp:
    def __init__(
        self,
        dominios_permitidos: Iterable[str],
        *,
        cliente: httpx.Client | None = None,
        reintentos: int = 3,
        espera_base_s: float = 2.0,
        pausa_entre_solicitudes_s: float = 1.0,
        timeout_s: float = 30.0,
        dormir: Callable[[float], None] = time.sleep,
    ) -> None:
        self._permitidos = {d.lower() for d in dominios_permitidos}
        self._cliente = cliente or httpx.Client(timeout=timeout_s)
        self._reintentos = reintentos
        self._espera_base_s = espera_base_s
        self._pausa_s = pausa_entre_solicitudes_s
        self._dormir = dormir
        self._hubo_solicitud = False

    def obtener_html(self, url: str) -> str:
        return self._get(url).text

    def descargar(self, url: str) -> bytes:
        return self._get(url).content

    def _verificar(self, url: str) -> None:
        partes = urlsplit(url)
        if partes.scheme not in {"http", "https"}:
            raise ErrorFuente(f"esquema no permitido: {url}")
        if (partes.hostname or "").lower() not in self._permitidos:
            raise ErrorFuente(f"dominio no permitido: {url}")

    def _get(self, url: str) -> httpx.Response:
        for _ in range(_MAX_REDIRECCIONES + 1):
            self._verificar(url)
            respuesta = self._get_con_reintentos(url)
            if not respuesta.is_redirect:
                return respuesta
            url = urljoin(url, respuesta.headers.get("location", ""))
        raise ErrorFuente(f"demasiadas redirecciones: {url}")

    def _get_con_reintentos(self, url: str) -> httpx.Response:
        ultimo_error = ""
        for intento in range(self._reintentos + 1):
            if intento:
                self._dormir(self._espera_base_s * 2 ** (intento - 1))
            elif self._hubo_solicitud and self._pausa_s > 0:
                self._dormir(self._pausa_s)
            self._hubo_solicitud = True
            try:
                respuesta = self._cliente.get(url, headers=CABECERAS_NAVEGADOR)
            except httpx.TransportError as error:
                ultimo_error = f"{type(error).__name__}: {error}"
                continue
            if respuesta.status_code in _TRANSITORIOS:
                ultimo_error = f"HTTP {respuesta.status_code}"
                continue
            if respuesta.is_success or respuesta.is_redirect:
                return respuesta
            raise ErrorFuente(f"HTTP {respuesta.status_code} en {url}")
        raise ErrorFuente(f"{ultimo_error} en {url} tras {self._reintentos} reintentos")
