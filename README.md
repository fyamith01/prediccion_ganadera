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
| oro | Predicciones del modelo por versión (los modelos van en `data/modelos/`) | Delta Lake |

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

## Base de datos local

PostgreSQL 16 con Docker Compose ([`infra/docker-compose.yml`](infra/docker-compose.yml)),
publicado solo en `127.0.0.1`. Crea un `.env` en la raíz (está ignorado por git):

```dotenv
POSTGRES_USER=ganaderia
POSTGRES_PASSWORD=<elige-una-clave>
POSTGRES_DB=prediccion_ganadera
POSTGRES_PORT=5432
PREDICCION_PG_DSN=postgresql://ganaderia:<la-misma-clave>@localhost:5432/prediccion_ganadera
```

```bash
docker compose --env-file .env -f infra/docker-compose.yml up -d   # descarga postgres:16-alpine
set -a; source .env; set +a                                         # exporta PREDICCION_PG_DSN
uv run prediccion-ganadera cargar          # plata -> raw / staging / marts
uv run prediccion-ganadera cargar --json   # resumen de la carga
```

`cargar` crea los esquemas si no existen y es idempotente: identifica cada tabla por el
SHA-256 del PDF, la página y su índice, y solo reescribe las que cambiaron. `raw` recibe
todas las tablas de plata; `staging`, solo las `verificada` o `corregida`;
`marts.cobertura_subastas` resume el avance de la verificación por subasta. Prefiere
`PREDICCION_PG_DSN` a `--dsn`, que deja la contraseña visible en la lista de procesos.
Cada campo está descrito en el [diccionario de datos](docs/diccionario_datos.md).

Las pruebas de Postgres no usan Docker ni red: levantan un PostgreSQL 16 embebido
(`pixeltable-pgserver`) con una base nueva por prueba, o usan el servidor de
`PREDICCION_PG_DSN_PRUEBAS` si la defines.

## Corrida completa (orquestador)

`run` encadena los agentes en un grafo de estados de LangGraph:
recolectar → extraer → verificar → cargar → modelar.

```bash
uv run prediccion-ganadera run                          # corrida con id = hora UTC
uv run prediccion-ganadera run --id semanal --sin-ia    # sin backend de IA
uv run prediccion-ganadera run --reanudar semanal       # continúa una corrida caída
uv run prediccion-ganadera run --solo-listar --json     # sin descargas; estado final en JSON
```

- **Estado y reanudación.** El estado tipado de la corrida (pasos hechos, tokens, costo,
  presupuesto restante) se guarda después de cada paso en
  `data/corridas/checkpoints.sqlite`. Si el proceso cae, `--reanudar <id>` continúa en el
  paso que falló sin repetir los anteriores. Usa las opciones de la línea de comandos
  actual, así que repite las que usaste al iniciar.
- **Paradas.** Un fallo esperado (la fuente no responde, Postgres no acepta la carga)
  termina la corrida como `fallida` sin ejecutar los pasos siguientes (código de salida 1).
  Sin conexión a Postgres el paso `cargar` se omite y la corrida termina igual. Si no hay
  histórico validado suficiente, `modelar` se omite con el motivo (ver «Modelo predictivo»).
- **Presupuesto.** `limites.presupuesto_tokens_corrida` es de la corrida completa: cada
  agente recibe lo que dejaron los anteriores.
- **Registro.** Cada inicio, reanudación, final o caída queda en
  `data/corridas/corridas.jsonl` y, si hay DSN, en la tabla `operacion.runs` de Postgres
  (estado, tokens, costo, duración y resumen de cada paso).

## Modelo predictivo

`entrenar` responde «¿qué día conviene vender y qué ganado?» con un modelo XGBoost sobre
el precio promedio por kilo de cada categoría en cada subasta.

```bash
uv run prediccion-ganadera entrenar                           # desde plata, solo tablas validadas
uv run prediccion-ganadera entrenar --fuente staging          # desde Postgres (PREDICCION_PG_DSN)
uv run prediccion-ganadera entrenar --incluir-no-verificadas  # también tablas pendientes (lo advierte)
uv run prediccion-ganadera entrenar --json                    # resumen completo en JSON
```

- **Datos.** Los precios salen de la tabla «RESUMEN GENERAL DE LA FERIA» de cada subasta
  (ver [diccionario de datos](docs/diccionario_datos.md)). Las filas que no cuadran se
  descartan con su motivo, nunca se completan. Las categorías rematadas por cabeza y las
  subastas especiales quedan fuera del modelo.
- **Variables y objetivo.** Para cada categoría, fecha de subasta y horizonte h (1 a 4
  subastas adelante), las variables salen solo de subastas anteriores a la fecha de
  referencia: último precio, media de los cuatro últimos, tendencia, animales y peso de la
  última subasta, oferta total, semanas desde el último precio, mes y semana del año. El
  objetivo es la variación frente al último precio conocido; así la línea base ingenua
  («el precio no cambia») es predecir cero.
- **Validación temporal.** Cinco pliegues de ventana creciente, cuatro fechas por pliegue:
  se entrena con fechas anteriores y se prueba con las siguientes, sin mezclar fechas ni
  categorías de una misma subasta entre ambos lados. El error se reporta en $/kg por
  horizonte, frente a la línea base.
- **Confianza (honesta).** El histórico es corto: subastas semanales desde enero de 2025.
  El nivel es `baja` con menos de 26 subastas, menos de 3 pliegues o si el modelo no
  supera a la línea base; `media` con menos de 52 subastas (no hay un año completo para
  aprender la estacionalidad) o con una mejora menor al 10 %; `alta` en otro caso. Los
  umbrales son heurísticos y cada motivo se imprime con el resultado.
- **Ranking.** El modelo final, entrenado con todo el histórico, predice los cuatro
  viernes siguientes a la última subasta para cada categoría. Por categoría se recomienda
  la fecha de mayor precio esperado y las categorías se ordenan por variación esperada. Si
  la diferencia entre fechas no supera el error de validación, se indica que no es
  distinguible.
- **Registro.** Cada entrenamiento queda en `data/modelos/<version>/` (`modelo.json`,
  `metadatos.json` con datos, parámetros, métricas y confianza) y en
  `data/modelos/registro.jsonl`; las predicciones van a la tabla Delta
  `data/oro/predicciones`. Nada se sobrescribe.
- **Sin histórico suficiente** el comando no entrena, explica cuántas subastas hay y
  cuántas tablas faltan por validar, y sale con código 1.

## Decisiones de diseño

*Pendiente.*
