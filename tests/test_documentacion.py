"""La documentación y la licencia no se desincronizan del código (T10)."""

from __future__ import annotations

import tomllib
from pathlib import Path

RAIZ = Path(__file__).parent.parent


def test_licencia_mit_declarada_en_pyproject():
    licencia = (RAIZ / "LICENSE").read_text(encoding="utf-8")
    assert licencia.startswith("MIT License")
    assert "Fabián Yamith Tovar" in licencia
    assert "Todos los derechos reservados" not in licencia

    proyecto = tomllib.loads((RAIZ / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert proyecto["license"] == "MIT"
    assert proyecto["license-files"] == ["LICENSE"]


def _subcomandos() -> list[str]:
    import argparse

    from prediccion_ganadera.cli import _parser

    def acciones(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
        return next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))

    nombres = []
    for nombre, sub in acciones(_parser()).choices.items():
        anidados = [a for a in sub._actions if isinstance(a, argparse._SubParsersAction)]
        if anidados:
            nombres += [f"{nombre} {hijo}" for hijo in anidados[0].choices]
        else:
            nombres.append(nombre)
    return nombres


def test_readme_documenta_cada_subcomando():
    readme = (RAIZ / "README.md").read_text(encoding="utf-8")
    faltan = [c for c in _subcomandos() if f"prediccion-ganadera {c}" not in readme]
    assert not faltan, faltan


def test_readme_sin_secciones_pendientes():
    readme = (RAIZ / "README.md").read_text(encoding="utf-8")
    assert "Pendiente" not in readme
    assert "en construcción" not in readme


def test_guia_del_codigo_cubre_cada_modulo():
    guia = (RAIZ / "docs" / "guia_del_codigo.md").read_text(encoding="utf-8")
    paquete = RAIZ / "src" / "prediccion_ganadera"
    modulos = [
        p.relative_to(paquete).as_posix()
        for p in sorted(paquete.rglob("*.py"))
        if p.name != "__init__.py"
    ]
    faltan = [m for m in modulos if f"`{m}`" not in guia]
    assert not faltan, faltan


def test_runbook_de_operacion_cubre_lo_esencial():
    runbook = (RAIZ / "docs" / "operacion.md").read_text(encoding="utf-8")
    for tema in (".env", "docker compose", "--reanudar", "cola_revision", "crontab", "evaluar"):
        assert tema in runbook, tema


def test_enlaces_relativos_existen():
    import re

    documentos = [RAIZ / "README.md", *sorted((RAIZ / "docs").glob("*.md"))]
    rotos = []
    for documento in documentos:
        texto = documento.read_text(encoding="utf-8")
        for destino in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", texto):
            if "://" in destino or destino.startswith("mailto:"):
                continue
            if not (documento.parent / destino).exists():
                rotos.append(f"{documento.name}: {destino}")
    assert not rotos, rotos
