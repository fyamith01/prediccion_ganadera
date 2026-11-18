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
