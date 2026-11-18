# Guía del código

Mapa del repositorio para ubicar rápido qué archivo hace qué y dónde se hace cada cambio
típico. Las rutas de módulos son relativas a `src/prediccion_ganadera/`.

## Carpetas de la raíz

| Carpeta / archivo | Qué hay | ¿Versionado? |
|---|---|---|
| `src/prediccion_ganadera/` | El paquete Python (todo el código) | Sí |
| `tests/` | Pruebas `pytest`; `tests/fixtures/` tiene el PDF de ejemplo, la página HTML guardada y respuestas de IA sintéticas | Sí |
| `config/agentes.yaml` | Backend de IA por agente, límites, rutas de datos y fuente | Sí |
| `infra/docker-compose.yml` | PostgreSQL 16 local | Sí |
| `evaluacion/muestras/` | Muestras revisadas a mano para medir la extracción | Sí |
| `docs/` | Documentación (esta guía, operación, diccionario, evaluación, federado) | Sí |
| `data/` | Datos generados: `bronce/`, `plata/`, `oro/`, `modelos/`, `paginas/`, `corridas/` | No (`.gitignore`) |
| `reportes/` | PDF publicados por fecha | No |
| `.env` | Credenciales de Postgres y `PREDICCION_PG_DSN` | No (nunca se sube) |
| `pyproject.toml`, `uv.lock` | Dependencias y el comando `prediccion-ganadera` | Sí |

## Flujo de una corrida y archivos que intervienen

| Paso | Lo dispara | Agente / herramienta | Escribe en |
|---|---|---|---|
| 1 recolectar | `orquestacion/pasos.py` → `recolectar` | `agentes/recolector.py` + `herramientas/enlaces.py`, `calendario.py`, `descarga.py`, `adaptadores/fuente/http.py` | `data/bronce/` (`adaptadores/almacen/bronce.py`) |
| 2 extraer | `extraer` | `herramientas/bronce_a_plata.py` + `extraer_tablas_pdf.py` | `data/plata/tablas_extraidas` (`adaptadores/almacen/plata.py`) |
| 3 verificar | `verificar` | `agentes/verificador.py` + `herramientas/paginas.py`, `comprobaciones.py` | plata, `data/plata/cola_revision` (`adaptadores/almacen/revision.py`), `data/paginas/` |
| 4 cargar | `cargar` | `adaptadores/almacen/postgres.py` | Postgres `raw`, `staging`, `marts` |
| 5 modelar | `modelar` | `modelado/modelador.py` + `variables.py`, `dataset.py`, `entrenamiento.py` | `data/modelos/` (`adaptadores/almacen/modelos.py`), `data/oro/predicciones` (`adaptadores/almacen/oro.py`) |
| 6 reportar | `reportar` | `agentes/reportero.py` + `herramientas/cifras_reporte.py` | `reportes/` (`adaptadores/reportes/pdf.py`, `salida.py`) |
| registro | `orquestacion/grafo.py` | `adaptadores/almacen/corridas.py` | `data/corridas/corridas.jsonl`, `operacion.runs` |

## Módulo por módulo

### Entrada y configuración

| Módulo | Responsabilidad |
|---|---|
| `cli.py` | Comando `prediccion-ganadera`: un `_nombre` por subcomando (`_recolectar`, `_verificar`, `_run`, `_evaluar_modelo`, ...) y sus argumentos en `_parser()`. Códigos de salida: 0 bien, 1 sin datos o fallo esperado, 2 configuración |
| `configuracion.py` | Lee y valida `config/agentes.yaml` con pydantic (backends, agentes, límites, rutas, fuente); arma el comando del harness sin shell |

### Puertos (contratos)

| Módulo | Responsabilidad |
|---|---|
| `puertos/ia.py` | `ProveedorIA.completar(prompt, imagenes)` → `RespuestaIA`; `UsoIA` acumula llamadas, tokens y costo por agente |
| `puertos/corridas.py` | `RegistroCorrida` (lo que se guarda de cada corrida) y el protocolo `RegistroCorridas` |
| `puertos/reportes.py` | `Renderizador`: cómo se dibuja un reporte (PDF hoy) |

### Adaptadores

| Módulo | Responsabilidad |
|---|---|
| `adaptadores/ia/cli_harness.py` | Ejecuta `claude -p`, `agy -p` o `agent -p` con tiempo límite, lee el sobre JSON y detecta rechazos «suaves» (`status`) |
| `adaptadores/ia/ollama.py` | Llama a Ollama (`/api/generate`) con imágenes en base64, `format: "json"` y `num_ctx`; avisa si la respuesta se corta |
| `adaptadores/fuente/http.py` | Cliente HTTP con cabeceras de navegador (`CABECERAS_NAVEGADOR`), reintentos, lista de dominios permitidos y redirecciones controladas |
| `adaptadores/almacen/bronce.py` | PDF por SHA-256 en `data/bronce/pdfs/` + `manifiesto.jsonl`; rechaza lo que no empieza con `%PDF-` |
| `adaptadores/almacen/plata.py` | Tabla Delta `tablas_extraidas` (una fila por celda): leer, reemplazar y marcar el estado de una tabla |
| `adaptadores/almacen/revision.py` | Cola de revisión manual (Delta `cola_revision`) |
| `adaptadores/almacen/oro.py` | Tabla Delta `oro/predicciones` por versión de modelo |
| `adaptadores/almacen/modelos.py` | Registro de modelos: `data/modelos/<version>/` + `registro.jsonl` |
| `adaptadores/almacen/postgres.py` | DDL (`esquema_postgres.sql`) y carga idempotente plata → `raw`/`staging` |
| `adaptadores/almacen/corridas.py` | Registro de corridas en JSONL y en `operacion.runs` |
| `adaptadores/reportes/pdf.py` | Dibuja el reporte con reportlab |
| `adaptadores/reportes/salida.py` | Publica `reportes/<aaaa-mm-dd>/resumen_<version>.pdf` + `.json` sin sobrescribir |

### Herramientas deterministas

| Módulo | Responsabilidad |
|---|---|
| `herramientas/enlaces.py` | Lista los enlaces a PDF de la página y resuelve la fecha (texto del enlace, nombre de archivo, carpeta de subida) |
| `herramientas/calendario.py` | Viernes esperados en un rango: faltantes, fuera de calendario y duplicados |
| `herramientas/descarga.py` | Descarga a bronce solo los PDF nuevos |
| `herramientas/extraer_tablas_pdf.py` | `extraer_tablas` con pdfplumber; `AJUSTES_POR_DEFECTO` y `AJUSTES_ALTERNATIVOS` |
| `herramientas/bronce_a_plata.py` | Extrae a plata los PDF de bronce que faltan; errores por PDF no detienen el lote |
| `herramientas/paginas.py` | Imagen PNG de una página (pypdfium2) y sus líneas de texto |
| `herramientas/comprobaciones.py` | Comprueba una tabla contra el texto de su página (filas, totales) |
| `herramientas/cifras_reporte.py` | Cifras del reporte, formato de números en español y la salvaguarda contra cifras inventadas |

### Agentes

| Módulo | Responsabilidad |
|---|---|
| `agentes/recolector.py` | Decide fechas en conflicto con la IA (`PROMPT_CONFLICTO`), reporta huecos y descarga |
| `agentes/verificador.py` | Bucle comprobar → rectificar → verificar con visión (`PROMPT_VERIFICACION`), cola de revisión |
| `agentes/reportero.py` | Narrativa del reporte (`PROMPT_NARRATIVA`, máximo `MAX_PUNTOS` viñetas) con reintento y respaldo determinista |

### Modelado

| Módulo | Responsabilidad |
|---|---|
| `modelado/variables.py` | Convierte el «RESUMEN GENERAL DE LA FERIA» en precios por categoría (`CATEGORIAS`, `TOLERANCIA_VALOR`) |
| `modelado/dataset.py` | Variables sin fuga de futuro, horizontes (`HORIZONTES`), pliegues temporales, métricas y nivel de confianza (`FECHAS_MINIMAS`, `FECHAS_ANIO`, `MEJORA_MINIMA`, `PLIEGUES_MINIMOS`) |
| `modelado/entrenamiento.py` | XGBoost (`PARAMETROS_XGB`), ranking de próximas fechas × categorías y recomendaciones |
| `modelado/modelador.py` | Lee precios, entrena, registra y publica predicciones (lo usan `entrenar` y el paso `modelar`) |
| `modelado/federado.py` | XGBoost federado simulado con Flower (`RONDAS_FEDERADAS`, `REPARTOS`) |

### Orquestación y evaluación

| Módulo | Responsabilidad |
|---|---|
| `orquestacion/grafo.py` | Grafo LangGraph (`ORDEN_PASOS`), estado de la corrida, checkpointer SQLite, registro y reanudación |
| `orquestacion/pasos.py` | Conecta cada paso del grafo con su agente o herramienta; crea los backends antes de empezar |
| `evaluacion/extraccion.py` | Exactitud de extracción contra `evaluacion/muestras/*.yaml` |
| `evaluacion/trayectorias.py` | Métricas de las corridas a partir del registro |
| `evaluacion/modelo.py` | Métricas por versión del modelo y seguimiento de sus predicciones |

## Si te piden... modifica

| Cambio | Dónde | Notas |
|---|---|---|
| Usar otra IA en un agente | `config/agentes.yaml` → `agentes.<agente>.backend` | Sin tocar código. `validar-config` lo comprueba |
| Agregar otro harness de IA | `config/agentes.yaml` → nuevo backend `cli_harness` con `{prompt}` | Si su JSON de salida es distinto, ajusta `adaptadores/ia/cli_harness.py` |
| Cambiar el modelo local | `config/agentes.yaml` → `backends.local.modelo` | Debe soportar visión para el verificador |
| Cambiar la página de origen o los dominios | Variables `PREDICCION_FUENTE_URL` y `PREDICCION_FUENTE_DOMINIOS` en `.env` (o la sección `fuente` de `config/agentes.yaml`; la variable gana, ver `configuracion.py`) | Si cambia el HTML, revisa `herramientas/enlaces.py` |
| Las subastas pasan a otro día de la semana | `VIERNES` en `herramientas/calendario.py` y los viernes de predicción en `modelado/dataset.py` (`filas_prediccion`) | |
| Un nuevo formato de nombre de archivo con fecha | `_FECHA_NOMBRE` / `_FECHA_TEXTO` en `herramientas/enlaces.py` | Agrega el caso a `tests/test_enlaces.py` |
| Más o menos rectificaciones, confianza mínima, presupuesto de tokens | `config/agentes.yaml` → `limites` | |
| Otros ajustes de extracción de tablas | `AJUSTES_POR_DEFECTO` / `AJUSTES_ALTERNATIVOS` en `herramientas/extraer_tablas_pdf.py` | Mide antes y después con `evaluar extraccion` |
| Cambiar lo que pregunta un agente | `PROMPT_*` en `agentes/recolector.py`, `verificador.py`, `reportero.py` | Mantén el formato JSON de respuesta que esperan |
| Una categoría de ganado nueva | `CATEGORIAS` en `modelado/variables.py` | Sin entrada, la categoría igual entra con su clave compacta |
| Horizontes de predicción (cuántas subastas adelante) | `HORIZONTES` en `modelado/dataset.py` | |
| Umbrales del nivel de confianza | `FECHAS_MINIMAS`, `FECHAS_ANIO`, `MEJORA_MINIMA`, `PLIEGUES_MINIMOS` en `modelado/dataset.py` | |
| Hiperparámetros de XGBoost | `PARAMETROS_XGB` en `modelado/entrenamiento.py` | Compara versiones con `evaluar modelo` |
| Agregar o reordenar pasos de la corrida | `ORDEN_PASOS` en `orquestacion/grafo.py` + la función en `orquestacion/pasos.py` | Las pruebas de `test_orquestacion.py` fijan el orden |
| Textos o secciones del PDF | `adaptadores/reportes/pdf.py` (`TITULO`, `NOTA_IA`, `AVISO_FINAL`) | |
| Carpeta de datos o de reportes | `config/agentes.yaml` → `rutas` | |
| Tablas o columnas de Postgres | `adaptadores/almacen/esquema_postgres.sql` + `postgres.py` + `docs/diccionario_datos.md` | Una prueba compara el diccionario con el esquema real |
| Un subcomando nuevo | `cli.py` (función `_nombre` + entrada en `_parser()`) y el README | Una prueba exige que cada subcomando aparezca en el README |

## Pruebas

`uv run pytest` corre todo sin red, sin IA real y sin Docker: la IA se simula con un
harness falso y sobres JSON sintéticos (`tests/fixtures/ia/`), Postgres con un servidor
embebido (fixture `postgres_dsn` en `tests/conftest.py`) y los precios con
`precios_sinteticos`. `uv run ruff check .` y `uv run ruff format --check .` revisan el
estilo. Para probar un cambio, busca el archivo `tests/test_<módulo>.py` correspondiente.
