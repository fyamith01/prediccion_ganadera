from pathlib import Path

from prediccion_ganadera import __version__
from prediccion_ganadera.cli import main

RAIZ = Path(__file__).resolve().parents[1]


def test_version(capsys):
    assert main(["--version"]) == 0
    assert __version__ in capsys.readouterr().out


def test_validar_config_valida_resume_agentes_y_backends(capsys):
    codigo = main(["validar-config", "--config", str(RAIZ / "config" / "agentes.yaml")])

    salida = capsys.readouterr().out
    assert codigo == 0
    assert "verificador -> claude (cli_harness)" in salida


def test_validar_config_invalida_devuelve_codigo_2(tmp_path, capsys):
    ruta = tmp_path / "mala.yaml"
    ruta.write_text("version: 1\nbackends: {}\nagentes: {x: {backend: y}}\n", encoding="utf-8")

    codigo = main(["validar-config", "--config", str(ruta)])

    assert codigo == 2
    assert "y" in capsys.readouterr().err
