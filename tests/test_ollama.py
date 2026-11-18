import base64
import json
from pathlib import Path

import httpx
import pytest

from prediccion_ganadera.adaptadores.ia.ollama import ProveedorOllama
from prediccion_ganadera.configuracion import BackendOllama
from prediccion_ganadera.puertos.ia import ErrorProveedorIA, RespuestaIA

BACKEND = BackendOllama(
    tipo="ollama", modelo="qwen2.5vl:3b", url="http://ollama:11434", timeout_s=7
)


def _proveedor(manejador) -> ProveedorOllama:
    return ProveedorOllama(BACKEND, transporte=httpx.MockTransport(manejador))


def test_envia_prompt_e_imagenes_en_base64_y_lee_la_respuesta(tmp_path: Path):
    imagen = tmp_path / "p1.png"
    imagen.write_bytes(b"\x89PNG falso")
    pedidos: list[httpx.Request] = []

    def manejador(pedido: httpx.Request) -> httpx.Response:
        pedidos.append(pedido)
        return httpx.Response(
            200,
            json={
                "response": ' {"coincide": true} ',
                "done": True,
                "prompt_eval_count": 900,
                "eval_count": 40,
            },
        )

    respuesta = _proveedor(manejador).completar("compara", imagenes=[imagen])

    assert respuesta == RespuestaIA(
        texto='{"coincide": true}', tokens_entrada=900, tokens_salida=40, costo_usd=None
    )
    (pedido,) = pedidos
    assert str(pedido.url) == "http://ollama:11434/api/generate"
    cuerpo = json.loads(pedido.content)
    assert cuerpo == {
        "model": "qwen2.5vl:3b",
        "prompt": "compara",
        "images": [base64.b64encode(b"\x89PNG falso").decode()],
        "stream": False,
        "format": "json",
        "options": {"num_ctx": 16384},
    }


def test_sin_imagenes_no_envia_el_campo():
    cuerpos = []

    def manejador(pedido: httpx.Request) -> httpx.Response:
        cuerpos.append(json.loads(pedido.content))
        return httpx.Response(200, json={"response": "ok", "done": True})

    assert _proveedor(manejador).completar("hola").texto == "ok"
    assert "images" not in cuerpos[0]


def test_respuesta_cortada_por_el_contexto_avisa_como_subirlo():
    respuesta = httpx.Response(
        200, json={"response": "```json\n{", "done": True, "done_reason": "length"}
    )

    with pytest.raises(ErrorProveedorIA, match=r"cortada.*16384.*num_ctx.*config/agentes\.yaml"):
        _proveedor(lambda pedido: respuesta).completar("hola")


@pytest.mark.parametrize("fin", [{"done_reason": "stop"}, {}])
def test_respuesta_terminada_normalmente_o_sin_done_reason_no_es_cortada(fin):
    respuesta = httpx.Response(200, json={"response": "{}", "done": True, **fin})

    assert _proveedor(lambda pedido: respuesta).completar("hola").texto == "{}"


@pytest.mark.parametrize(
    ("respuesta", "mensaje"),
    [
        (httpx.Response(404, json={"error": "model 'qwen2.5vl:3b' not found"}), "not found"),
        (httpx.Response(500, text="fallo interno"), "500"),
        (httpx.Response(200, json={"done": True}), "response"),
        (httpx.Response(200, text="no es json"), "ilegible"),
    ],
)
def test_respuestas_de_error(respuesta, mensaje):
    with pytest.raises(ErrorProveedorIA, match=mensaje):
        _proveedor(lambda pedido: respuesta).completar("hola")


def test_servidor_inalcanzable():
    def manejador(pedido):
        raise httpx.ConnectError("conexión rechazada", request=pedido)

    with pytest.raises(ErrorProveedorIA, match="http://ollama:11434"):
        _proveedor(manejador).completar("hola")


def test_timeout_del_backend():
    def manejador(pedido):
        assert pedido.extensions["timeout"]["read"] == 7
        raise httpx.ReadTimeout("lento", request=pedido)

    with pytest.raises(ErrorProveedorIA, match="7 s"):
        _proveedor(manejador).completar("hola")


def test_imagen_inexistente(tmp_path: Path):
    with pytest.raises(ErrorProveedorIA, match="imagen"):
        _proveedor(lambda p: httpx.Response(200, json={})).completar(
            "hola", imagenes=[tmp_path / "no.png"]
        )
