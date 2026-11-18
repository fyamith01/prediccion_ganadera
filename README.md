# Predicción ganadera

Sistema multiagente que recolecta los resúmenes de subastas ganaderas publicados en PDF,
extrae sus tablas, las verifica visualmente con un modelo de IA configurable, las almacena
en un Data Lake (bronce/plata/oro con Delta Lake) y en PostgreSQL, entrena un modelo XGBoost
para recomendar cuándo y qué ganado vender, y publica resúmenes periódicos en PDF.

> Estado: en construcción. Las secciones marcadas como *pendiente* se completan a medida que
> se implementa cada componente.

## Arquitectura

*Pendiente:* diagrama de agentes, herramientas y flujo de datos.

## Capas de datos

| Capa | Contenido | Formato |
|---|---|---|
| bronce | PDF originales, inmutables, nombrados por SHA-256 + manifiesto | PDF + JSONL |
| plata | Tablas extraídas y verificadas | Delta Lake |
| oro | Variables del modelo y predicciones | Delta Lake |

## Backends de IA

Cada agente usa el backend definido en [`config/agentes.yaml`](config/agentes.yaml):
un harness en modo headless (`claude -p`, `agy -p`, `agent -p` de Cursor) o un modelo local con
Ollama. Los comandos se ejecutan sin shell y el prompt viaja como un único argumento.

## Cómo ejecutarlo

Requisitos: Python 3.13 y [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run prediccion-ganadera validar-config
uv run pytest
```

Recolección (agente recolector): lista los resúmenes publicados, resuelve sus fechas,
reporta las subastas semanales faltantes y descarga los PDF nuevos a `data/bronce/`.

```bash
uv run prediccion-ganadera recolectar                 # lee la página y descarga
uv run prediccion-ganadera recolectar --solo-listar   # sin descargas
uv run prediccion-ganadera recolectar --sin-ia --json # conflictos de fecha a revisión manual
```

## Decisiones de diseño

*Pendiente.*
