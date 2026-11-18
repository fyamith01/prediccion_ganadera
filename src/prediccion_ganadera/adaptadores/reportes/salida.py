"""Publicación del reporte: `reportes/<aaaa-mm-dd>/resumen_<version>.pdf` + `.json`.

El JSON lleva las cifras, la narrativa y su origen, para auditar cada PDF. Nada se
sobrescribe: si el nombre ya existe se agrega un sufijo (`-2`, `-3`, ...). La versión
entra en el nombre del archivo, así que se valida para que no pueda salir de `raiz`.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from prediccion_ganadera.herramientas.cifras_reporte import CifrasReporte, Narrativa
from prediccion_ganadera.puertos.reportes import Renderizador

_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


@dataclass(frozen=True)
class ReportePublicado:
    pdf: Path
    json: Path


def publicar_reporte(
    raiz: Path,
    cifras: CifrasReporte,
    narrativa: Narrativa,
    renderizador: Renderizador,
    *,
    ahora: datetime,
) -> ReportePublicado:
    if not _VERSION.fullmatch(cifras.version):
        raise ValueError(
            f"versión de modelo no válida para un nombre de archivo: {cifras.version!r}"
        )
    carpeta = Path(raiz) / ahora.strftime("%Y-%m-%d")
    base, sufijo = f"resumen_{cifras.version}", 2
    nombre = base
    while (carpeta / f"{nombre}{renderizador.extension}").exists() or (
        carpeta / f"{nombre}.json"
    ).exists():
        nombre, sufijo = f"{base}-{sufijo}", sufijo + 1
    carpeta.mkdir(parents=True, exist_ok=True)
    documento = carpeta / f"{nombre}{renderizador.extension}"
    renderizador.renderizar(cifras, narrativa, documento, generado_en=ahora)
    registro = {
        "generado_en": ahora.isoformat(),
        "documento": documento.name,
        "cifras": cifras.model_dump(mode="json"),
        "narrativa": narrativa.model_dump(mode="json"),
    }
    destino_json = carpeta / f"{nombre}.json"
    destino_json.write_text(json.dumps(registro, ensure_ascii=False, indent=2), encoding="utf-8")
    return ReportePublicado(pdf=documento, json=destino_json)
