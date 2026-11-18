import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

from prediccion_ganadera.adaptadores.almacen.bronce import AlmacenBronce
from prediccion_ganadera.adaptadores.almacen.plata import AlmacenPlata
from prediccion_ganadera.cli import main
from prediccion_ganadera.herramientas.bronce_a_plata import extraer_bronce_a_plata

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PDF_MUESTRA = FIXTURES / "pdfs" / "Resumen-de-subasta-de-ganado-24-07-2026.pdf"
AHORA = datetime(2026, 11, 18, tzinfo=UTC)

# Responde como `claude -p --output-format json` con un sobre grabado.
HARNESS_FALSO = """
import pathlib, sys
print(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
"""


def _preparar(tmp_path: Path, sobre: str = "verificador_acepta.json") -> Path:
    datos = tmp_path / "data"
    bronce = AlmacenBronce(datos / "bronce")
    bronce.guardar(
        "https://www.subastas.example/wp-content/uploads/2026/07/muestra.pdf",
        PDF_MUESTRA.read_bytes(),
        fecha=date(2026, 7, 24),
        especial=False,
        ahora=AHORA,
    )
    extraer_bronce_a_plata(bronce, AlmacenPlata(datos / "plata"), ahora=AHORA)
    harness = tmp_path / "harness_falso.py"
    harness.write_text(HARNESS_FALSO, encoding="utf-8")
    config = tmp_path / "agentes.yaml"
    config.write_text(
        f"""
version: 1
backends:
  falso:
    tipo: cli_harness
    comando: ["{sys.executable}", "{harness}", "{{prompt}}", "{FIXTURES / "ia" / sobre}"]
agentes:
  verificador: {{backend: falso}}
limites: {{max_iteraciones_rectificacion: 2, confianza_minima_verificacion: 0.9}}
rutas: {{datos: "{datos}", reportes: "{tmp_path / "reportes"}"}}
""",
        encoding="utf-8",
    )
    return config


def test_verificar_sin_ia(tmp_path: Path, capsys):
    config = _preparar(tmp_path)

    codigo = main(["verificar", "--config", str(config), "--sin-ia"])

    assert codigo == 0
    salida = capsys.readouterr().out
    assert "4 tabla(s) revisadas" in salida
    assert "pendiente: 4" in salida
    assert "IA: 0 llamadas" in salida


def test_verificar_con_el_backend_configurado_en_json(tmp_path: Path, capsys):
    config = _preparar(tmp_path)

    codigo = main(["verificar", "--config", str(config), "--limite", "2", "--json"])

    assert codigo == 0
    resultado = json.loads(capsys.readouterr().out)
    assert [t["estado"] for t in resultado["tablas"]] == ["corregida", "verificada"]
    assert resultado["uso"]["llamadas_ia"] == 2
    assert (tmp_path / "data" / "paginas").is_dir()


def test_verificar_lista_las_tablas_en_revision(tmp_path: Path, capsys):
    config = _preparar(tmp_path, "verificador_corrige_titulo.json")

    codigo = main(["verificar", "--config", str(config), "--limite", "1"])

    assert codigo == 0
    salida = capsys.readouterr().out
    assert "en_revision: 1" in salida
    assert "Cola de revisión (1):" in salida


def test_verificar_con_un_agente_sin_configurar(tmp_path: Path, capsys):
    config = _preparar(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8").replace("verificador:", "otro:"), encoding="utf-8"
    )

    assert main(["verificar", "--config", str(config)]) == 2
    assert "verificador" in capsys.readouterr().err
