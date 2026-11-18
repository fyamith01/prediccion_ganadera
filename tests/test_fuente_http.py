import ssl

import httpx
import pytest

from prediccion_ganadera.adaptadores.fuente import http
from prediccion_ganadera.adaptadores.fuente.http import ErrorFuente, FuenteHttp, contexto_ssl

PERMITIDOS = ["www.subastas.example"]
URL = "https://www.subastas.example/wp-content/uploads/2026/01/09-01-26.pdf"


def _fuente(manejador, pausas=None, **kwargs) -> tuple[FuenteHttp, list[httpx.Request]]:
    solicitudes: list[httpx.Request] = []

    def registrar(request: httpx.Request) -> httpx.Response:
        solicitudes.append(request)
        return manejador(request, len(solicitudes))

    cliente = httpx.Client(transport=httpx.MockTransport(registrar))
    dormir = pausas.append if pausas is not None else lambda _s: None
    opciones = {"pausa_entre_solicitudes_s": 0, "espera_base_s": 1.0} | kwargs
    return FuenteHttp(PERMITIDOS, cliente=cliente, dormir=dormir, **opciones), solicitudes


def test_envia_cabeceras_de_navegador_y_devuelve_bytes():
    fuente, solicitudes = _fuente(lambda _r, _n: httpx.Response(200, content=b"%PDF-1.7"))

    assert fuente.descargar(URL) == b"%PDF-1.7"
    cabeceras = solicitudes[0].headers
    assert "Mozilla/5.0" in cabeceras["user-agent"]
    assert cabeceras["accept-language"].startswith("es")
    assert "text/html" in cabeceras["accept"]


def test_obtener_html_decodifica_texto():
    fuente, _ = _fuente(
        lambda _r, _n: httpx.Response(
            200, content=b"<p>Subasta</p>", headers={"content-type": "text/html"}
        )
    )

    assert fuente.obtener_html(URL) == "<p>Subasta</p>"


def test_reintenta_errores_transitorios_con_espera_exponencial():
    def manejador(request, intento):
        if intento == 1:
            raise httpx.ConnectError("caída", request=request)
        if intento == 2:
            return httpx.Response(503)
        return httpx.Response(200, content=b"ok")

    pausas: list[float] = []
    fuente, solicitudes = _fuente(manejador, pausas)

    assert fuente.descargar(URL) == b"ok"
    assert len(solicitudes) == 3
    assert pausas == [1.0, 2.0]


def test_agota_reintentos():
    fuente, solicitudes = _fuente(lambda _r, _n: httpx.Response(429), reintentos=2)

    with pytest.raises(ErrorFuente, match="429"):
        fuente.descargar(URL)
    assert len(solicitudes) == 3


def test_error_definitivo_no_se_reintenta():
    fuente, solicitudes = _fuente(lambda _r, _n: httpx.Response(406))

    with pytest.raises(ErrorFuente, match="406"):
        fuente.descargar(URL)
    assert len(solicitudes) == 1


def test_rechaza_dominios_no_permitidos_sin_hacer_solicitudes():
    fuente, solicitudes = _fuente(lambda _r, _n: httpx.Response(200))

    with pytest.raises(ErrorFuente, match="no permitido"):
        fuente.descargar("https://otro.example/archivo.pdf")
    with pytest.raises(ErrorFuente, match="no permitido"):
        fuente.descargar("file:///etc/passwd")
    assert solicitudes == []


def test_sigue_redirecciones_solo_dentro_de_los_dominios_permitidos():
    def manejador(request, _n):
        if request.url.path == "/viejo.pdf":
            return httpx.Response(301, headers={"location": "/nuevo.pdf"})
        if request.url.path == "/nuevo.pdf":
            return httpx.Response(302, headers={"location": "https://otro.example/x.pdf"})
        return httpx.Response(200)

    fuente, solicitudes = _fuente(manejador)

    with pytest.raises(ErrorFuente, match="no permitido"):
        fuente.descargar("https://www.subastas.example/viejo.pdf")
    assert [r.url.path for r in solicitudes] == ["/viejo.pdf", "/nuevo.pdf"]


def test_pausa_entre_solicitudes():
    pausas: list[float] = []
    fuente, _ = _fuente(lambda _r, _n: httpx.Response(200), pausas, pausa_entre_solicitudes_s=1.5)

    fuente.descargar(URL)
    fuente.descargar(URL)

    assert pausas == [1.5]


def test_contexto_ssl_no_estricto_quita_solo_el_modo_estricto():
    contexto = contexto_ssl(estricto=False)

    assert not contexto.verify_flags & ssl.VERIFY_X509_STRICT
    assert contexto.verify_mode == ssl.CERT_REQUIRED
    assert contexto.check_hostname


def test_contexto_ssl_estricto_conserva_la_bandera_por_defecto():
    por_defecto = httpx.create_ssl_context().verify_flags & ssl.VERIFY_X509_STRICT

    assert contexto_ssl(estricto=True).verify_flags & ssl.VERIFY_X509_STRICT == por_defecto


@pytest.mark.parametrize("estricto", [True, False])
def test_fuente_crea_su_cliente_con_el_contexto_ssl(monkeypatch, estricto):
    pedidos: list[bool] = []
    contexto = ssl.create_default_context()

    def falso(estricto: bool) -> ssl.SSLContext:
        pedidos.append(estricto)
        return contexto

    monkeypatch.setattr(http, "contexto_ssl", falso)

    FuenteHttp(PERMITIDOS, x509_estricto=estricto)

    assert pedidos == [estricto]


def test_cliente_inyectado_no_crea_contexto_ssl(monkeypatch):
    monkeypatch.setattr(http, "contexto_ssl", lambda estricto: pytest.fail("no debe llamarse"))

    FuenteHttp(PERMITIDOS, cliente=httpx.Client(), x509_estricto=False)
