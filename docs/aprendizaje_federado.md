# Aprendizaje federado simulado

`prediccion-ganadera comparar-federado` entrena el mismo XGBoost de `entrenar` de dos
formas y las compara con la misma validación temporal:

- **Central:** un solo modelo con todas las filas.
- **Federado:** varios clientes simulados guardan cada uno sus filas. En cada ronda, cada
  cliente recibe el modelo global, le agrega un árbol entrenado solo con sus datos y
  devuelve únicamente ese árbol. El servidor los une con la estrategia de *bagging* de
  Flower (`FedXgbBagging`). Las filas y los gradientes nunca salen del cliente; lo único
  compartido, además de los árboles, es el catálogo de categorías.

El comando no escribe nada: ni modelos ni predicciones. El modelo que se usa en
producción sigue siendo el central de `entrenar`.

```bash
uv run prediccion-ganadera comparar-federado                  # los tres repartos
uv run prediccion-ganadera comparar-federado --reparto anio   # un reparto
uv run prediccion-ganadera comparar-federado --rondas 100 --json
```

## Por qué es una simulación

Todos los datos vienen de una sola fuente: los resúmenes de subastas de una misma
asociación. Ningún reparto corresponde a dueños de datos distintos que no puedan
compartirlos, que es el caso para el que existe el aprendizaje federado. Lo que esta
comparación mide es **cuánta precisión cuesta entrenar sin juntar los datos** cuando
están repartidos de una forma dada. El caso real sería el de varias casas de subastas o
ferias de distintas regiones, cada una con su histórico, que quieren un modelo común sin
entregar sus cifras. El código ya tiene esa forma: un cliente por dueño de datos.

## Repartos

| Reparto | Cliente dueño de una fila | ¿Tiene sentido? |
|---|---|---|
| `anio` | año de la fecha objetivo (2025, 2026) | Sí, como simulación: dos clientes con las mismas categorías y, según el listado del 17-11-2026, 49 y 44 subastas ordinarias. Imita a dos dueños con periodos distintos. En los pliegues más antiguos el cliente más reciente puede no tener filas de entrenamiento todavía; `clientes_por_pliegue` lo muestra. |
| `seccion` | sección de la categoría en el resumen (hembra / macho) | Sí, como simulación de datos *no IID*: cada cliente ve solo sus categorías, como dos asociaciones especializadas. Es el reparto más exigente para el modelo federado. |
| `tipo_subasta` | subasta ordinaria o especial | **No.** El listado del 17-11-2026 tiene una sola subasta especial (17-10-2026) entre 95. Un cliente con una fecha no puede entrenar árboles con sentido ni validarse en el tiempo, así que el comando lo declara no viable y no calcula métricas. |

Un reparto es viable si deja al menos dos clientes y cada uno tiene al menos 12 fechas
objetivo (el mínimo de entrenamiento de la validación). Si no, la salida explica el
motivo y no fuerza la comparación. Si el histórico no alcanza ni para validar el modelo
central, el comando no compara nada y sale con código 1, como `entrenar`.

## Decisiones técnicas

- **Sin Ray ni red.** `flwr.simulation.run_simulation` necesita Ray, que es pesado y abre
  procesos. La simulación usa el cliente (`flwr.client.Client`) y la estrategia
  (`FedXgbBagging.aggregate_fit`) de Flower en el mismo proceso: el algoritmo de
  agregación es el de Flower, solo cambia el transporte. Las pruebas no usan red.
- **Un árbol por cliente y por ronda.** La agregación de Flower copia `num_parallel_tree`
  árboles de cada respuesta. Si un cliente agregara varios árboles por ronda, Flower
  descartaría todos menos el primero sin avisar (hay una prueba que lo fija).
- **Rondas y tasa de aprendizaje.** La agregación vuelve a leer el modelo global completo
  en cada unión, así que el costo crece con el cuadrado de las rondas: 300 rondas tardaban
  unos 4 minutos por reparto. Por defecto hay 60 rondas, y cada cliente aprende
  `eta × rondas_centrales / rondas / clientes`. El primer factor reparte el aprendizaje
  del modelo central entre menos rondas; el segundo compensa que el *bagging* suma los
  árboles de todos los clientes. En datos sintéticos, 60 rondas dan la misma precisión
  federada que 300 (error de 161 frente a 166 $/kg por año), en unos 5 segundos.
- **Predicción base 0.** El objetivo es la variación frente al último precio, así que
  todos los clientes parten de la línea base ingenua.

## Resultados

Datos reales: **pendiente.** Plata está vacía hasta una descarga autorizada de los PDF y
su verificación visual. Con los datos reales, este comando da la tabla para el README.

Datos sintéticos (precios que suben y bajan 10 % cada semana, 94 subastas, tres
categorías; configuración por defecto). Sirven para comprobar el método, no para sacar
conclusiones sobre el mercado:

| Modelo | Error medio ($/kg) | Mejora sobre la línea base |
|---|---|---|
| Central (300 rondas) | 49 | 89,8 % |
| Federado por año (60 rondas) | 161 | 66,6 % |
| Federado por sección (60 rondas) | 351 | 27,2 % |

Federar cuesta precisión, y mucho más cuando cada cliente ve solo sus categorías (por
sección) que cuando todos ven las mismas en periodos distintos (por año).
