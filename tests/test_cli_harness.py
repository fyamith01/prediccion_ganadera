import json
import subprocess
import sys

import pytest

from prediccion_ganadera.adaptadores.ia import crear_proveedor
from prediccion_ganadera.adaptadores.ia.cli_harness import ProveedorCliHarness, interpretar_salida
from prediccion_ganadera.adaptadores.ia.ollama import ProveedorOllama
from prediccion_ganadera.configuracion import (
    BackendCliHarness,
    BackendOllama,
    ErrorConfiguracion,
    cargar_configuracion,
)
from prediccion_ganadera.puertos.ia import ErrorProveedorIA, RespuestaIA

# Harness falso: imprime un sobre JSON como `claude -p --output-format json`.
HARNESS_FALSO = """
import json, sys
prompt = sys.argv[1]
if prompt == "falla":
    print("credenciales inválidas", file=sys.stderr)
    sys.exit(3)
print(json.dumps({"type": "result", "is_error": False, "result": prompt.upper(),
                  "usage": {"input_tokens": 7, "output_tokens": 2}, "total_cost_usd": 0.01}))
"""


def _backend(comando: list[str], timeout_s: int = 30) -> BackendCliHarness:
    return BackendCliHarness(tipo="cli_harness", comando=comando, timeout_s=timeout_s)


def test_ejecuta_el_harness_sin_shell_y_devuelve_la_respuesta():
    proveedor = ProveedorCliHarness(_backend([sys.executable, "-c", HARNESS_FALSO, "{prompt}"]))

    respuesta = proveedor.completar("hola; rm -rf / $(whoami)")

    assert respuesta == RespuestaIA(
        texto="HOLA; RM -RF / $(WHOAMI)", tokens_entrada=7, tokens_salida=2, costo_usd=0.01
    )


def test_codigo_de_salida_distinto_de_cero_es_un_error():
    proveedor = ProveedorCliHarness(_backend([sys.executable, "-c", HARNESS_FALSO, "{prompt}"]))

    with pytest.raises(ErrorProveedorIA, match="credenciales inválidas"):
        proveedor.completar("falla")


def test_ejecutable_inexistente_es_un_error():
    proveedor = ProveedorCliHarness(_backend(["no-existe-este-harness-123", "{prompt}"]))

    with pytest.raises(ErrorProveedorIA, match="no se encontró"):
        proveedor.completar("hola")


def test_respeta_el_timeout_del_backend():
    llamadas = []

    def ejecutar(argv, **kwargs):
        llamadas.append(kwargs)
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    proveedor = ProveedorCliHarness(_backend(["claude", "-p", "{prompt}"], timeout_s=5), ejecutar)

    with pytest.raises(ErrorProveedorIA, match="5 s"):
        proveedor.completar("hola")
    assert llamadas[0]["timeout"] == 5
    assert llamadas[0].get("shell", False) is False


@pytest.mark.parametrize(
    ("salida", "texto"),
    [
        # Claude Code y Cursor: sobre {type, is_error, result}
        ({"type": "result", "subtype": "success", "is_error": False, "result": "ok"}, "ok"),
        # Antigravity: sobre {status, response, usage}
        ({"status": "success", "response": "ok", "usage": {}}, "ok"),
    ],
)
def test_interpreta_sobres_json_de_los_harness(salida, texto):
    assert interpretar_salida(json.dumps(salida)).texto == texto


def test_salida_de_texto_plano():
    assert interpretar_salida("  respuesta libre\n").texto == "respuesta libre"


@pytest.mark.parametrize(
    "salida",
    [
        "",
        json.dumps({"type": "result", "is_error": True, "result": "límite de uso"}),
        # Antigravity niega acciones sin aprobar con código 0: hay que leer `status`
        json.dumps({"status": "denied", "response": ""}),
        json.dumps({"type": "result", "is_error": False}),
    ],
)
def test_salidas_invalidas_son_errores(salida):
    with pytest.raises(ErrorProveedorIA):
        interpretar_salida(salida)


def test_crear_proveedor_por_agente(tmp_path):
    ruta = tmp_path / "agentes.yaml"
    ruta.write_text(
        """
version: 1
backends:
  claude: {tipo: cli_harness, comando: ["claude", "-p", "{prompt}"]}
  local: {tipo: ollama, modelo: qwen2.5vl:3b}
agentes:
  recolector: {backend: claude}
  verificador: {backend: local}
fuente: {url: "https://www.subastas.example/", dominios_permitidos: [www.subastas.example]}
""",
        encoding="utf-8",
    )
    config = cargar_configuracion(ruta)

    assert isinstance(crear_proveedor(config, "recolector"), ProveedorCliHarness)
    assert isinstance(config.backends["local"], BackendOllama)
    assert isinstance(crear_proveedor(config, "verificador"), ProveedorOllama)
    with pytest.raises(ErrorConfiguracion, match="no está configurado"):
        crear_proveedor(config, "inexistente")
