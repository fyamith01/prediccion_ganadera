"""Registro de modelos en disco: una carpeta por versión y un índice JSONL.

```
data/modelos/
├── registro.jsonl              # una línea por versión (fecha, confianza, error)
└── <version>/
    ├── modelo.json             # booster XGBoost (formato JSON nativo)
    ├── metadatos.json          # datos, variables, parámetros, métricas y confianza
    └── resumen.json            # resumen del modelado (lo que lee el agente reportero)
```

La versión es la marca de tiempo UTC del entrenamiento; si ya existe se le agrega un
sufijo. Nada se sobrescribe: cada entrenamiento queda reproducible y comparable.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import xgboost as xgb

from prediccion_ganadera.modelado.dataset import COLUMNAS_VARIABLES
from prediccion_ganadera.modelado.entrenamiento import ModeloXGB, ResultadoEntrenamiento


class RegistroModelos:
    def __init__(self, raiz: Path) -> None:
        self.raiz = Path(raiz)
        self.indice = self.raiz / "registro.jsonl"

    def _nueva_version(self, ahora: datetime) -> str:
        base = ahora.strftime("%Y%m%dT%H%M%SZ")
        version, sufijo = base, 2
        while (self.raiz / version).exists():
            version, sufijo = f"{base}-{sufijo}", sufijo + 1
        return version

    def guardar(self, resultado: ResultadoEntrenamiento, *, ahora: datetime) -> str:
        version = self._nueva_version(ahora)
        carpeta = self.raiz / version
        carpeta.mkdir(parents=True)
        resultado.modelo.guardar(carpeta / "modelo.json")
        metadatos = {
            "version": version,
            "creado_en": ahora.isoformat(),
            "xgboost": xgb.__version__,
            "parametros": resultado.modelo.parametros,
            "variables": COLUMNAS_VARIABLES,
            "categorias": resultado.categorias,
            "opciones": resultado.opciones.model_dump(mode="json"),
            "fechas": {
                "desde": resultado.fecha_desde.isoformat(),
                "hasta": resultado.fecha_hasta.isoformat(),
                "cantidad": resultado.fechas_historico,
            },
            "filas_entrenamiento": resultado.filas_entrenamiento,
            "huella_datos": resultado.huella_datos,
            "metricas": resultado.metricas.model_dump(mode="json"),
            "confianza": resultado.confianza.model_dump(mode="json"),
        }
        (carpeta / "metadatos.json").write_text(
            json.dumps(metadatos, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        entrada = {
            "version": version,
            "creado_en": metadatos["creado_en"],
            "confianza": resultado.confianza.nivel,
            "mae": resultado.metricas.mae,
            "mejora": resultado.metricas.mejora,
        }
        with self.indice.open("a", encoding="utf-8") as archivo:
            archivo.write(json.dumps(entrada, ensure_ascii=False) + "\n")
        return version

    def listar(self) -> list[dict[str, Any]]:
        if not self.indice.exists():
            return []
        lineas = self.indice.read_text(encoding="utf-8").splitlines()
        return [json.loads(linea) for linea in lineas if linea.strip()]

    def cargar(self, version: str | None = None) -> tuple[ModeloXGB, dict[str, Any]]:
        """El modelo de la versión pedida (o la más reciente); `KeyError` si no existe."""
        if version is None:
            entradas = self.listar()
            if not entradas:
                raise KeyError("no hay modelos registrados")
            version = entradas[-1]["version"]
        carpeta = self.raiz / version
        if not (carpeta / "metadatos.json").is_file():
            raise KeyError(version)
        metadatos = json.loads((carpeta / "metadatos.json").read_text(encoding="utf-8"))
        modelo = ModeloXGB.cargar(
            carpeta / "modelo.json", metadatos["categorias"], metadatos["parametros"]
        )
        return modelo, metadatos

    def guardar_resumen(self, version: str, resumen: dict[str, Any]) -> None:
        carpeta = self.raiz / version
        if not carpeta.is_dir():
            raise KeyError(version)
        (carpeta / "resumen.json").write_text(
            json.dumps(resumen, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def leer_resumen(self, version: str | None = None) -> dict[str, Any]:
        """El resumen de la versión pedida (o la más reciente); `KeyError` si no existe."""
        if version is None:
            entradas = self.listar()
            if not entradas:
                raise KeyError("no hay modelos registrados")
            version = entradas[-1]["version"]
        ruta = self.raiz / version / "resumen.json"
        if not ruta.is_file():
            raise KeyError(version)
        return json.loads(ruta.read_text(encoding="utf-8"))
