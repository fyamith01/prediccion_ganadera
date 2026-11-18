# Evaluación del sistema

Un sistema multiagente se evalúa en tres niveles: si los datos que produce son correctos
(extracción), si los agentes recorren un camino razonable para producirlos (trayectorias)
y si el modelo que se entrena con ellos predice mejor que una regla simple (modelo). Cada
nivel tiene un módulo en `src/prediccion_ganadera/evaluacion/` y un subcomando de
`prediccion-ganadera evaluar`. Ninguno escribe datos ni llama a un backend de IA: solo
leen lo que ya existe.

| Nivel | Comando | Módulo | Lee |
|---|---|---|---|
| Extracción | `evaluar extraccion` | `evaluacion/extraccion.py` | `evaluacion/muestras/*.yaml` + PDF (bronce o `ruta`) o plata |
| Trayectorias | `evaluar trayectorias` | `evaluacion/trayectorias.py` | `data/corridas/corridas.jsonl` o `operacion.runs` |
| Modelo | `evaluar modelo` | `evaluacion/modelo.py` | `data/modelos/*/metadatos.json`, `data/oro/predicciones`, precios de plata/staging |

Códigos de salida comunes: 0 evaluado, 1 no hay nada que evaluar o un dato no está
disponible (sin muestras, sin corridas, sin modelos, PDF ausente, Postgres caído), 2
error de configuración (configuración inválida, muestra mal escrita, fuente Postgres sin
DSN). Todos aceptan `--json` para guardar o comparar resultados.

## 1. Exactitud de extracción

```bash
uv run prediccion-ganadera evaluar extraccion                   # re-extrae los PDF
uv run prediccion-ganadera evaluar extraccion --fuente plata    # tablas tras el verificador
```

**Qué compara.** Una *muestra revisada* es un YAML en `evaluacion/muestras/` con lo que una
persona leyó en un PDF: algunos renglones con cifras tal como se imprimen y los precios del
«RESUMEN GENERAL DE LA FERIA». Se compara con las tablas del extractor (`--fuente
extractor`, por defecto) o con las que quedaron en plata después del verificador visual
(`--fuente plata`); la diferencia entre ambas mide cuánto aporta la rectificación.

**Métricas.**

| Métrica | Definición | Por qué así |
|---|---|---|
| Recall de filas | renglones esperados encontrados / esperados | un renglón está «encontrado» si alguna fila extraída de esa página tiene los mismos números en el mismo orden; el extractor parte palabras entre celdas (`VACA HO`, `RRA`) pero no números |
| Precisión de filas | filas extraídas con cifras que están en la muestra / filas extraídas con cifras | solo en páginas con `completa: true`; en una página parcial no se sabe si una fila extra es un error |
| Exactitud de valores | números acertados / números esperados | para cada renglón esperado, la fila extraída con más números en común; mide errores parciales (una cifra mal leída) |
| Exactitud de precios | campos correctos / campos esperados (6 por categoría) | se interpretan las tablas con el mismo código del modelado (`precios_de_resumen`); es exactamente lo que consume el modelo, y una fila descartada cuenta como categoría faltante |

Cada diferencia se lista en el detalle (`página 1: falta o difiere «GRAN TOTAL 389»`,
`precios: VACA HORRA precio_promedio = 7850, la muestra dice 7805`).

**Borrador vs revisada.** Una muestra con `estado: borrador` no la ha revisado una persona:
la salida la marca como `BORRADOR` y el JSON trae `"revisada": false`. Para que una métrica
cuente como final, la muestra debe estar en `estado: revisada` con `revisado_por` y
`revisado_en` (el validador lo exige).

**Muestra actual.** `evaluacion/muestras/subasta_2026-07-24.yaml` (el PDF de prueba
del repositorio): página 1 completa (9 categorías, 2 totales de bloque y el gran total) y 10
de los 75 lotes, elegidos con una regla fija: cada 8.º lote en orden de página empezando por
el primero (lotes 8, 24, 33, 41, 49, 57, 74, 82, 90 y 120). Se transcribió desde la imagen de
las páginas, no desde la salida del extractor. Resultado contra el extractor: 21/22 filas
(falta el «GRAN TOTAL 389» del pie de página, que el detector de tablas deja fuera y que el
verificador agrega con la acción `anexar_renglones`), 146/147 valores y 54/54 campos de precio.

**Cómo agregar una muestra.**

1. Descarga el PDF con `recolectar` (queda en `data/bronce/pdfs/<sha256>.pdf`) o pon su ruta
   relativa a la raíz del repositorio en `ruta`.
2. Copia `subasta_2026-07-24.yaml`, cambia `pdf`, `sha256` (`sha256sum archivo.pdf`) y
   `fecha`, y transcribe las filas leyendo el PDF, no la salida del extractor.
3. Marca `completa: true` solo en páginas transcritas completas.
4. Cuando la hayas verificado, pon `estado: revisada`, `revisado_por` y `revisado_en`.

La prueba `tests/test_muestras_evaluacion.py` valida todas las muestras versionadas (formato,
SHA-256 del PDF y que cada renglón tenga cifras).

## 2. Trayectorias de los agentes

```bash
uv run prediccion-ganadera evaluar trayectorias                  # desde el JSONL
uv run prediccion-ganadera evaluar trayectorias --fuente postgres   # desde operacion.runs
```

Cada `run` deja en el registro de corridas el resumen de cada paso (ver la sección
«Corrida completa» del README). De ahí:

| Métrica | Definición |
|---|---|
| Tasa de éxito | corridas `completada` / (`completada` + `fallida` + `interrumpida`); las `en_curso` no cuentan |
| Reanudadas | corridas con más de una versión `en_curso` en el historial. Con `--fuente postgres` aparece como desconocida, porque `operacion.runs` guarda solo la última versión |
| Duración media por paso, tokens, costo | del registro de cada paso |
| Omisiones y errores | `paso: motivo` (p. ej. `cargar: sin conexión a Postgres`) y el error de cada corrida no completada |
| Verificador: resueltas sin humano | (`verificada` + `corregida`) / (esas + `en_revision`). Las `pendiente` (sin IA o sin presupuesto) no tienen decisión y se excluyen |
| Verificador: llamadas de visión y rectificaciones por tabla | costo del bucle verificar → rectificar; `acciones_por_tipo` dice qué rectificación se usó (`reextraer`, `anexar_renglones`, `corregir_celdas`) |
| Recolector | enlaces, cobertura del calendario de viernes, fechas decididas por la IA, enlaces en revisión y descargas (de la corrida más reciente que recolectó) |

Con estas cifras se responde, por ejemplo: ¿el bucle de rectificación converge o agota
las iteraciones?, ¿cuánto cuesta verificar una tabla?, ¿qué paso domina la duración?

## 3. Modelo predictivo

```bash
uv run prediccion-ganadera evaluar modelo                  # validación de cada versión
uv run prediccion-ganadera evaluar modelo --seguimiento    # + error real de las predicciones
```

- **Validación (al entrenar).** De `data/modelos/<version>/metadatos.json`: MAE y MAPE de la
  validación temporal frente al ingenuo («el precio será el último conocido»), por horizonte,
  la mejora (`1 − MAE / MAE ingenuo`), el nivel de confianza con sus motivos y el cambio frente
  a la versión anterior (un `cambio_mae` negativo significa que reentrenar mejoró).
- **Seguimiento (después de entrenar).** Con `--seguimiento`, las predicciones guardadas en
  `oro.predicciones` se cruzan con los precios observados después (plata o `--fuente staging`,
  solo tablas validadas y categorías en $/kg). Da el error real fuera de muestra, la mejora real
  frente al ingenuo y la fracción de predicciones que cayó dentro del error esperado que
  anunció el modelo. Solo existe para fechas que ya pasaron y tienen datos validados.

## Dónde tocar

| Si quieres... | Modifica |
|---|---|
| Cambiar qué renglones cuentan para la extracción | `_tiene_cifras` en `evaluacion/extraccion.py` |
| Agregar una métrica de trayectoria | `MetricasVerificador` / `MetricasRecolector` y `_verificador` / `_recolector` en `evaluacion/trayectorias.py` |
| Agregar una métrica del modelo | `MetricasVersion` / `SeguimientoPredicciones` en `evaluacion/modelo.py` |
| Cambiar la salida en texto | `_evaluar_extraccion`, `_evaluar_trayectorias`, `_evaluar_modelo` en `cli.py` |
