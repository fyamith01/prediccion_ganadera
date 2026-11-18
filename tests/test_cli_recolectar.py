import json
from pathlib import Path

from prediccion_ganadera.cli import main

FIXTURE = (
    Path(__file__).resolve().parent / "fixtures" / "html" / "resumen_de_subastas.html"
)


def _config(tmp_path: Path) -> Path:
    ruta = tmp_path / "agentes.yaml"
    ruta.write_text(
        f"""
version: 1
backends:
  claude: {{tipo: cli_harness, comando: ["claude", "-p", "{{prompt}}"]}}
agentes:
  recolector: {{backend: claude}}
rutas: {{datos: "{tmp_path / "data"}", reportes: "{tmp_path / "reportes"}"}}
fuente:
  url: https://www.subastas.example/resumen-de-subastas/
  dominios_permitidos: [www.subastas.example]
""",
        encoding="utf-8",
    )
    return ruta


def test_recolectar_offline_sin_ia_ni_descargas(tmp_path: Path, capsys):
    codigo = main(
        [
            "recolectar",
            "--config",
            str(_config(tmp_path)),
            "--html",
            str(FIXTURE),
            "--solo-listar",
            "--sin-ia",
            "--json",
        ]
    )

    assert codigo == 0
    reporte = json.loads(capsys.readouterr().out)
    assert reporte["enlaces"] == 95
    assert [r["url"].rsplit("/", 1)[1] for r in reporte["revision"]] == [
        "resumen-de-subasta-2-1-2025.pdf"
    ]
    assert reporte["huecos"]["fuera_de_calendario"] == ["2026-10-17", "2026-10-31"]
    assert reporte["descarga"] is None
    assert not (tmp_path / "data").exists()


def test_recolectar_resumen_legible(tmp_path: Path, capsys):
    codigo = main(
        ["recolectar", "--config", str(_config(tmp_path)), "--html", str(FIXTURE)]
        + ["--solo-listar", "--sin-ia", "--desde", "2026-01-01", "--hasta", "2026-01-31"]
    )

    salida = capsys.readouterr().out
    assert codigo == 0
    assert "Enlaces: 95" in salida
    assert "Faltantes (1): 2026-01-02" in salida
    assert "Revisión manual (1)" in salida


def test_recolectar_sin_fuente_configurada(tmp_path: Path, capsys):
    ruta = tmp_path / "agentes.yaml"
    ruta.write_text(
        'version: 1\nbackends: {c: {tipo: cli_harness, comando: ["c", "{prompt}"]}}\n'
        "agentes: {recolector: {backend: c}}\n",
        encoding="utf-8",
    )

    assert main(["recolectar", "--config", str(ruta), "--sin-ia"]) == 2
    assert "fuente" in capsys.readouterr().err
