"""`prediccion-ganadera reportar` y el paso `reportar` del orquestador, sin red ni IA real.

El backend de IA es un harness falso local que imprime un sobre SINTÉTICO con la forma de
`claude -p --output-format json`.
"""

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from prediccion_ganadera.cli import main
from prediccion_ganadera.configuracion import cargar_configuracion
from prediccion_ganadera.modelado.modelador import ejecutar_modelado
from prediccion_ganadera.orquestacion.pasos import OpcionesCorrida, crear_pasos

AHORA = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)

# Narrativa sin números: siempre pasa la barrera, sean cuales sean las cifras.
HARNESS_FALSO = """
import json, sys
narrativa = {"resumen": "Resumen redactado por el harness falso.", "puntos": ["Sin cifras."]}
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": json.dumps(narrativa), "total_cost_usd": 0.02,
                  "usage": {"input_tokens": 900, "output_tokens": 40}}))
"""


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("PREDICCION_PG_DSN", raising=False)
    harness = tmp_path / "harness_falso.py"
    harness.write_text(HARNESS_FALSO, encoding="utf-8")
    ruta = tmp_path / "agentes.yaml"
    ruta.write_text(
        f"""
version: 1
backends:
  falso:
    tipo: cli_harness
    comando: ["{sys.executable}", "{harness}", "{{prompt}}"]
agentes:
  recolector: {{backend: falso}}
  verificador: {{backend: falso}}
  reportero: {{backend: falso}}
rutas: {{datos: "{tmp_path / "data"}", reportes: "{tmp_path / "reportes"}"}}
fuente:
  url: https://www.subastas.example/resumen-de-subastas/
  dominios_permitidos: [www.subastas.example]
""",
        encoding="utf-8",
    )
    return ruta


@pytest.fixture
def entrenado(config: Path, precios_sinteticos):
    return ejecutar_modelado(
        precios_sinteticos(40),
        datos=config.parent / "data",
        fuente="plata",
        incluir_no_verificadas=False,
        ahora=AHORA,
    )


def test_reportar_con_ia_publica_pdf_y_json(config: Path, entrenado, capsys):
    codigo = main(["reportar", "--config", str(config), "--json"])

    assert codigo == 0
    resumen = json.loads(capsys.readouterr().out)
    pdf = Path(resumen["pdf"])
    assert resumen["version"] == entrenado.version
    assert resumen["narrativa"] == "ia"
    assert resumen["uso"]["tokens"] == 940
    assert resumen["uso"]["costo_usd"] == pytest.approx(0.02)
    assert pdf.is_file() and pdf.suffix == ".pdf"
    assert pdf.parent.parent == config.parent / "reportes"
    assert Path(resumen["registro_json"]).is_file()


def test_reportar_sin_ia_usa_la_narrativa_determinista(config: Path, entrenado, capsys):
    codigo = main(["reportar", "--config", str(config), "--sin-ia", "--version", entrenado.version])

    assert codigo == 0
    salida = capsys.readouterr().out
    assert f"Reporte del modelo {entrenado.version}" in salida
    assert "narrativa determinista (la consulta a la IA está desactivada)" in salida
    assert ".pdf" in salida


def test_sin_modelo_o_con_version_inexistente_sale_con_1(config: Path, capsys):
    assert main(["reportar", "--config", str(config), "--sin-ia"]) == 1
    assert "no hay un modelo entrenado" in capsys.readouterr().err
    assert main(["reportar", "--config", str(config), "--sin-ia", "--version", "x"]) == 1
    assert "no existe" in capsys.readouterr().err
    assert not (config.parent / "reportes").exists()


def test_reportero_sin_backend_configurado_sale_con_2(config: Path, entrenado, capsys):
    texto = config.read_text(encoding="utf-8").replace("  reportero: {backend: falso}\n", "")
    config.write_text(texto, encoding="utf-8")

    assert main(["reportar", "--config", str(config)]) == 2
    assert "reportero" in capsys.readouterr().err


def _estado(pasos: dict) -> dict:
    return {"id_corrida": "c", "presupuesto_restante": 1000, "pasos": pasos}


def test_paso_reportar_se_omite_sin_modelo_nuevo(config: Path):
    pasos = crear_pasos(cargar_configuracion(config), OpcionesCorrida(sin_ia=True))
    omitido = {"resumen": {}, "omitido": "sin datos suficientes: ..."}

    assert pasos["reportar"](_estado({"modelar": omitido})).omitido == (
        "no hay un modelo nuevo en esta corrida"
    )


def test_paso_reportar_publica_el_reporte_del_modelo_de_la_corrida(config: Path, entrenado):
    pasos = crear_pasos(cargar_configuracion(config), OpcionesCorrida())
    modelar = {"resumen": entrenado.model_dump(mode="json"), "omitido": None}

    resultado = pasos["reportar"](_estado({"modelar": modelar}))

    assert resultado.omitido is None
    assert resultado.resumen["version"] == entrenado.version
    assert resultado.resumen["narrativa"] == "ia"
    assert Path(resultado.resumen["pdf"]).is_file()
    assert (resultado.tokens, resultado.costo_usd) == (940, pytest.approx(0.02))
