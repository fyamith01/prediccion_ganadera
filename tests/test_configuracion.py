from pathlib import Path

import pytest

from prediccion_ganadera.configuracion import (
    BackendCliHarness,
    BackendOllama,
    ErrorConfiguracion,
    cargar_configuracion,
)

RAIZ = Path(__file__).resolve().parents[1]

CONFIG_MINIMA = """
version: 1
backends:
  claude:
    tipo: cli_harness
    comando: ["claude", "-p", "{prompt}", "--output-format", "json"]
  local:
    tipo: ollama
    modelo: qwen2.5vl:3b
agentes:
  verificador: {backend: claude}
  reportero: {backend: local}
fuente:
  url: https://www.subastas.example/resumen-de-subastas/
  dominios_permitidos: [www.subastas.example]
"""


def escribir(tmp_path: Path, contenido: str) -> Path:
    ruta = tmp_path / "agentes.yaml"
    ruta.write_text(contenido, encoding="utf-8")
    return ruta


def test_carga_backends_tipados_y_valores_por_defecto(tmp_path):
    config = cargar_configuracion(escribir(tmp_path, CONFIG_MINIMA))

    assert isinstance(config.backends["claude"], BackendCliHarness)
    assert isinstance(config.backends["local"], BackendOllama)
    assert config.backends["local"].url == "http://localhost:11434"
    assert config.limites.max_iteraciones_rectificacion == 3
    assert config.rutas.datos == Path("data")
    assert config.rutas.reportes == Path("reportes")


def test_backend_de_un_agente_se_resuelve_por_nombre(tmp_path):
    config = cargar_configuracion(escribir(tmp_path, CONFIG_MINIMA))

    assert config.backend_de("verificador") is config.backends["claude"]


def test_agente_desconocido_falla_con_mensaje_claro(tmp_path):
    config = cargar_configuracion(escribir(tmp_path, CONFIG_MINIMA))

    with pytest.raises(ErrorConfiguracion, match="recolector"):
        config.backend_de("recolector")


def test_agente_que_apunta_a_backend_inexistente_es_invalido(tmp_path):
    contenido = CONFIG_MINIMA.replace("{backend: local}", "{backend: gpt}")

    with pytest.raises(ErrorConfiguracion, match="gpt"):
        cargar_configuracion(escribir(tmp_path, contenido))


def test_comando_cli_sin_marcador_de_prompt_es_invalido(tmp_path):
    contenido = CONFIG_MINIMA.replace('"{prompt}", ', "")

    with pytest.raises(ErrorConfiguracion, match="prompt"):
        cargar_configuracion(escribir(tmp_path, contenido))


def test_tipo_de_backend_desconocido_es_invalido(tmp_path):
    contenido = CONFIG_MINIMA.replace("tipo: ollama", "tipo: magia")

    with pytest.raises(ErrorConfiguracion):
        cargar_configuracion(escribir(tmp_path, contenido))


def test_campos_desconocidos_se_rechazan(tmp_path):
    contenido = CONFIG_MINIMA + "\nextra: 1\n"

    with pytest.raises(ErrorConfiguracion, match="extra"):
        cargar_configuracion(escribir(tmp_path, contenido))


def test_archivo_inexistente_falla_con_ruta(tmp_path):
    with pytest.raises(ErrorConfiguracion, match="no_existe.yaml"):
        cargar_configuracion(tmp_path / "no_existe.yaml")


def test_construye_argv_sin_shell_con_el_prompt_como_un_solo_argumento(tmp_path):
    config = cargar_configuracion(escribir(tmp_path, CONFIG_MINIMA))
    prompt = 'compara "tabla"; rm -rf /'

    argv = config.backends["claude"].argv(prompt)

    assert argv == ["claude", "-p", prompt, "--output-format", "json"]


def test_la_configuracion_del_repositorio_es_valida():
    config = cargar_configuracion(RAIZ / "config" / "agentes.yaml")

    for agente in ("recolector", "verificador", "rectificador", "reportero"):
        assert config.backend_de(agente) is not None
    assert config.backend_de("verificador") is config.backends["claude"]
