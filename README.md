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
uv run prediccion-ganadera extraer                    # PDFs de bronce -> plata (Delta Lake)
```

`extraer` lee `data/bronce/manifiesto.jsonl` y escribe `data/plata/tablas_extraidas`, una
tabla Delta en formato largo (una fila por celda, particionada por `anio`/`mes`, con
`estado_verificacion = pendiente` hasta que la verifique el agente visual). Cada PDF se
extrae una sola vez por SHA-256; un PDF ilegible se reporta y no detiene el lote.

Verificación visual (agente verificador): revisa cada tabla `pendiente` de plata.

```bash
uv run prediccion-ganadera verificar --sin-ia     # solo comprobaciones deterministas
uv run prediccion-ganadera verificar --limite 10  # con el backend de `agentes.verificador`
uv run prediccion-ganadera verificar --json       # trayectoria de cada tabla
```

1. Comprueba la tabla contra el texto de su página: filas de datos faltantes o distintas,
   renglones de totales ausentes y totales de bloque que no cuadran.
2. Si falla, rectifica sin IA: re-extrae la página con ajustes alternativos o anexa los
   renglones faltantes copiados de la página.
3. Con las comprobaciones en limpio, el modelo de visión compara la imagen de la página
   (`data/paginas/<sha256>/p<n>.png`) con las celdas. Sus correcciones solo se aplican si
   el valor aparece en el texto de la página.
4. Como máximo `limites.max_iteraciones_rectificacion` rectificaciones por tabla. Las tablas
   no resueltas quedan `en_revision` y entran a la cola `data/plata/cola_revision` (Delta);
   las aceptadas quedan `verificada` (sin cambios) o `corregida`. Sin backend o sin
   presupuesto de tokens, la tabla sigue `pendiente`.

## Decisiones de diseño

*Pendiente.*
