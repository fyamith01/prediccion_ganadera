# Operación en producción

Guía paso a paso para instalar, programar y mantener el sistema en un servidor. Todos los
comandos se ejecutan desde la raíz del repositorio.

## 1. Requisitos

| Qué | Para qué | Obligatorio |
|---|---|---|
| Python 3.13 y [uv](https://docs.astral.sh/uv/) | Ejecutar el sistema | Sí |
| Un backend de IA autenticado (`claude`, `agy`, `agent` u Ollama) | Agentes recolector, verificador y reportero | No: sin IA usa `--sin-ia` (lo dudoso queda en revisión) |
| Docker con Compose | PostgreSQL local | No: sin Postgres el paso `cargar` se omite |
| La URL de la página de la fuente y acceso saliente a ella | Descargar los resúmenes | Sí, salvo pruebas con `--html` |

## 2. Instalación

```bash
git clone <url-del-repositorio> prediccion_ganadera
cd prediccion_ganadera
uv sync --no-dev                              # dependencias de ejecución (sin las de pruebas)
uv run prediccion-ganadera validar-config     # comprueba config/agentes.yaml
```

`uv sync --no-dev` crea `.venv/` con las versiones exactas de `uv.lock`. Para correr las
pruebas en el servidor usa `uv sync` (incluye `pytest`, `ruff` y el Postgres embebido) y
luego `uv run pytest`.

## 3. Elegir y autenticar la IA

1. En `config/agentes.yaml`, elige el backend de cada agente (`agentes.<agente>.backend`).
   Ver la tabla «Backends de IA» del README.
2. Autentica el backend **con el mismo usuario del sistema que ejecutará las corridas**
   (cron no hereda tu sesión): `claude` (inicia sesión una vez), `agy` (un primer `agy`
   interactivo) o `export CURSOR_API_KEY=...`. Para Ollama: `ollama serve` y
   `ollama pull qwen2.5vl:3b`.
3. Ajusta `limites` si hace falta: `presupuesto_tokens_corrida` (tope de tokens por
   corrida), `max_iteraciones_rectificacion` y `confianza_minima_verificacion`.
4. `uv run prediccion-ganadera validar-config` debe terminar con código 0.

## 4. Variables de entorno y base de datos

Crea `.env` en la raíz (git lo ignora; no lo subas nunca). Solo la primera variable es
obligatoria; las de Postgres hacen falta si usas la base de datos:

```dotenv
# Página que publica los resúmenes en PDF (no se versiona)
PREDICCION_FUENTE_URL=https://<sitio>/<pagina-de-resumenes>/
# Opcional: dominios permitidos para descargar, separados por comas (por defecto, el de la
# URL con y sin «www.»)
# PREDICCION_FUENTE_DOMINIOS=<sitio>,www.<sitio>

POSTGRES_USER=ganaderia
POSTGRES_PASSWORD=<elige-una-clave>
POSTGRES_DB=prediccion_ganadera
POSTGRES_PORT=5432
PREDICCION_PG_DSN=postgresql://ganaderia:<la-misma-clave>@localhost:5432/prediccion_ganadera
```

```bash
docker compose --env-file .env -f infra/docker-compose.yml up -d   # descarga postgres:16-alpine la primera vez
docker compose --env-file .env -f infra/docker-compose.yml ps      # debe decir «healthy»
set -a; source .env; set +a                                         # exporta las variables a la sesión
```

`PREDICCION_FUENTE_URL` tiene prioridad sobre la sección `fuente` de `config/agentes.yaml`
(que en el repositorio viene comentada). Sin ninguna de las dos, `recolectar` y `run` terminan
con código 2 y lo explican.

La base escucha solo en `127.0.0.1` y guarda sus datos en el volumen `datos_postgres`. Los
esquemas los crea el primer `cargar` o `run`; no hay migraciones manuales.

## 5. Primera corrida

```bash
uv run prediccion-ganadera recolectar --solo-listar   # ¿la página responde?, ¿cuántos enlaces y huecos?
uv run prediccion-ganadera run --id primera           # todo el flujo; la primera vez descarga ~95 PDF
uv run prediccion-ganadera evaluar trayectorias       # ¿cómo le fue a cada agente?
```

Qué esperar de la primera corrida:

- `recolectar` descarga los PDF a `data/bronce/` (con una pausa entre solicitudes).
- `verificar` revisa cada tabla; con IA, cada tabla cuesta llamadas al backend. Si se agota
  `presupuesto_tokens_corrida`, las tablas restantes quedan `pendiente` y se verifican en la
  siguiente corrida.
- `modelar` necesita al menos 33 subastas con el resumen validado (5 pliegues de 4 fechas más
  12 de entrenamiento); si no las hay, se omite y lo explica. `reportar` se omite también.
- El reporte queda en `reportes/<aaaa-mm-dd>/resumen_<version>.pdf`.

Códigos de salida de `run`: 0 completada (aunque haya pasos omitidos), 1 fallida o caída, 2
error de configuración o id inválido/repetido.

## 6. Programar las corridas (crontab)

Las subastas son los viernes y los PDF suelen subirse uno o varios días después. Una
corrida semanal el lunes a las 7:00 captura la del viernes anterior; la siguiente recoge lo
que se haya subido tarde (todo es idempotente: nada se descarga ni se procesa dos veces).

`crontab -e` con el usuario que autenticó la IA:

```cron
# m h dom mon dow  comando
0 7 * * 1  cd /ruta/a/prediccion_ganadera && set -a && . ./.env && set +a && $HOME/.local/bin/uv run prediccion-ganadera run --id "$(date +\%Y-\%m-\%d)" >> data/corridas/cron.log 2>&1
```

- En crontab el `%` se escapa (`\%`).
- cron tiene un `PATH` mínimo: usa la ruta completa de `uv` (`which uv`) y verifica que el
  ejecutable de la IA (`which claude`) esté en el `PATH` que ve cron. Si no, añade
  `PATH=...` al inicio del crontab.
- `.env` trae la URL de la fuente, así que siempre se carga. Sin Postgres, deja fuera las
  variables `POSTGRES_*` y `PREDICCION_PG_DSN`: la corrida sigue sin el paso `cargar`.
- Un id repetido sale con código 2: por eso el id es la fecha.

## 7. Si una corrida falla o se cae

| Estado en el registro | Qué pasó | Qué hacer |
|---|---|---|
| `fallida` | Un fallo esperado (la fuente no responde, Postgres rechaza la carga); los pasos siguientes no se ejecutaron | Leer el error (`evaluar trayectorias`), corregir la causa y lanzar una corrida nueva |
| `interrumpida` | El proceso cayó por una excepción inesperada | `uv run prediccion-ganadera run --reanudar <id>` con las mismas opciones: continúa en el paso que falló |
| `en_curso` que no avanza | El proceso murió sin registrar (p. ej. el servidor se apagó) | `run --reanudar <id>` |

El estado de cada corrida está en `data/corridas/checkpoints.sqlite` (para reanudar) y en
`data/corridas/corridas.jsonl` / `operacion.runs` (para auditar).

## 8. Revisión manual (cola_revision)

Las tablas que el verificador no pudo dejar correctas quedan `en_revision` y entran a la
cola `data/plata/cola_revision`. No bloquean nada: `staging` y el modelo solo usan tablas
`verificada` o `corregida`. `verificar` imprime la cola al terminar (no la uses solo para
consultar: también procesa las tablas pendientes); el script de abajo la lista sin cambiar
nada.

Hoy no existe un comando para resolverlas; el procedimiento es: revisar el PDF
(`data/bronce/pdfs/<sha256>.pdf`) y la imagen de la página (`data/paginas/`), corregir la
causa si es general (p. ej. ajustes de extracción, ver la guía del código) y devolver la
tabla a `pendiente` para que la próxima corrida la vuelva a verificar:

```bash
uv run python - <<'EOF'
from pathlib import Path
from prediccion_ganadera.adaptadores.almacen.plata import AlmacenPlata
from prediccion_ganadera.adaptadores.almacen.revision import ColaRevision

cola = ColaRevision(Path("data/plata"))
for entrada in cola.entradas():
    print(entrada.clave, entrada.motivo)
    # Para reintentarla en la próxima corrida, descomenta:
    # AlmacenPlata(Path("data/plata")).marcar_estado(entrada.clave, "pendiente")
    # cola.quitar(entrada.clave)
EOF
```

Los enlaces cuya fecha no se pudo decidir no se descargan y se reevalúan en cada corrida
(`recolectar --json` los lista en `revision`).

## 9. Evaluación periódica

```bash
uv run prediccion-ganadera evaluar trayectorias            # éxito, costo, duración, bucle del verificador
uv run prediccion-ganadera evaluar modelo --seguimiento    # ¿las predicciones pasadas acertaron?
uv run prediccion-ganadera evaluar extraccion              # exactitud contra las muestras revisadas
```

Qué mirar: una tasa de tablas a revisión que sube indica un cambio de formato de los PDF;
un `cambio_mae` positivo entre versiones indica que el modelo empeoró al reentrenar; un
seguimiento con mejora negativa indica que el modelo no le gana al «último precio». Detalle
en [evaluacion.md](evaluacion.md).

## 10. Respaldo y limpieza

- Respaldar `data/` (bronce es la fuente de verdad: con los PDF se reconstruye todo lo
  demás) y el volumen de Postgres
  (`docker compose --env-file .env -f infra/docker-compose.yml exec postgres pg_dump -U ganaderia prediccion_ganadera > respaldo.sql`).
- `data/paginas/` es una caché de imágenes: se puede borrar.
- Las tablas Delta guardan historial; para compactarlo se puede usar `vacuum` de
  `deltalake` cuando el tamaño lo justifique (no es necesario al volumen actual).

## 11. Problemas comunes

| Síntoma | Causa probable | Solución |
|---|---|---|
| `recolectar` falla con 406 | La página bloquea clientes sin cabeceras de navegador | Ya se envían (`CABECERAS_NAVEGADOR` en `adaptadores/fuente/http.py`); si cambiaron, actualízalas |
| `authentication required` o el backend devuelve error | La IA no está autenticada para el usuario que corre el proceso | Autenticar con ese usuario (paso 3) |
| `cargar` omitido: «sin conexión a Postgres» | No está definido `PREDICCION_PG_DSN` | Exportar `.env` (paso 4) o ignorarlo si no usas Postgres |
| `no se pudo cargar en Postgres` | El contenedor no está arriba o la clave no coincide | `docker compose ... ps`; revisar `.env` |
| `modelar` omitido: «sin datos suficientes» | Menos de 33 subastas con el resumen validado | Esperar a que el verificador valide más tablas; `entrenar --incluir-no-verificadas` para explorar (el resultado lo advierte) |
| Muchas tablas `pendiente` | Sin IA o presupuesto de tokens agotado | Subir `presupuesto_tokens_corrida` o dejar que corridas sucesivas avancen |
| `run` sale con código 2 por el id | El id ya existe | Usar otro `--id` o `--reanudar` si estaba interrumpida |

## 12. Prueba local rápida (WSL o Linux, con Ollama)

Recorre todo el flujo en una máquina de desarrollo sin GPU. La IA solo se usa en unas pocas
tablas para no esperar horas (en CPU, cada llamada de visión tarda de 30 s a 2 min).

```bash
# 1. Ollama y un modelo de visión pequeño (~3 GB)
curl -fsSL https://ollama.com/install.sh | sh
ollama serve > /tmp/ollama.log 2>&1 &        # en WSL sin systemd; si hay systemd ya corre
ollama pull qwen2.5vl:3b

# 2. Dependencias, .env (paso 4) y agentes apuntando a Ollama
uv sync
sed -i 's/{backend: claude}/{backend: local}/' config/agentes.yaml   # o edítalo a mano
uv run prediccion-ganadera validar-config
set -a; source .env; set +a

# 3. Postgres (opcional)
docker compose --env-file .env -f infra/docker-compose.yml up -d

# 4. Bronce -> plata -> verificación de muestra -> Postgres -> modelo -> reporte
uv run prediccion-ganadera recolectar --sin-ia            # descarga los PDF a data/bronce/
uv run prediccion-ganadera extraer                        # tablas Delta en data/plata/
uv run prediccion-ganadera verificar --limite 4           # 4 tablas con Ollama; el resto, pendiente
uv run prediccion-ganadera cargar                         # raw / staging / marts en Postgres
uv run prediccion-ganadera entrenar --incluir-no-verificadas   # modelo + data/oro/predicciones
uv run prediccion-ganadera reportar                       # reportes/<fecha>/resumen_<version>.pdf
uv run prediccion-ganadera evaluar trayectorias
```

`entrenar --incluir-no-verificadas` usa también tablas que el verificador aún no aprobó (el
resultado lo advierte); en producción se entrena solo con lo verificado. Para devolver los
agentes a Claude, `git checkout config/agentes.yaml`. Para empezar de cero, borra `data/` y
`reportes/` (y `docker compose ... down -v` para la base).

## Resumen de comandos

| Comando | Qué hace |
|---|---|
| `validar-config` | Valida `config/agentes.yaml` |
| `recolectar` | Lista enlaces, decide fechas, reporta huecos y descarga PDF a bronce |
| `extraer` | Extrae a plata los PDF de bronce nuevos |
| `verificar` | Verifica las tablas pendientes (comprobaciones + visión) |
| `cargar` | Carga plata en Postgres |
| `entrenar` | Entrena el XGBoost, lo registra y guarda predicciones en oro |
| `reportar` | Publica el reporte en PDF del último modelo |
| `comparar-federado` | Compara el XGBoost federado simulado con el central (no escribe) |
| `run` | Todo lo anterior en orden, reanudable |
| `evaluar extraccion / trayectorias / modelo` | Métricas de evaluación (no escribe) |

Cada uno acepta `--help` con todas sus opciones.
