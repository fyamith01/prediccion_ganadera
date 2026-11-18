import json
import random
from datetime import UTC, date, datetime, timedelta

import pandas as pd
import pytest
from deltalake import DeltaTable

from prediccion_ganadera.adaptadores.almacen.modelos import RegistroModelos
from prediccion_ganadera.adaptadores.almacen.oro import AlmacenOro
from prediccion_ganadera.modelado.dataset import DatosInsuficientes, filas_prediccion, serie_precios
from prediccion_ganadera.modelado.entrenamiento import (
    OpcionesEntrenamiento,
    entrenar_modelo,
    recomendaciones,
)
from prediccion_ganadera.modelado.variables import PrecioCategoria

INICIO = date(2025, 1, 3)  # viernes
AHORA = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
BASES = {"VACA HORRA": 7800, "MACHO LEVANTE": 10500, "HEMBRA LEVANTE": 9800}


def _precio(fecha: date, categoria: str, promedio: int) -> PrecioCategoria:
    return PrecioCategoria(
        fecha=fecha,
        especial=False,
        sha256="a" * 64,
        seccion="hembra",
        categoria=categoria,
        unidad="kg",
        animales=40,
        peso_cabeza=300,
        precio_minimo=promedio,
        precio_maximo=promedio,
        precio_promedio=promedio,
        valor_animal=promedio * 300,
    )


def _alternantes(semanas: int, *, semilla: int = 3) -> list[PrecioCategoria]:
    """Precios que suben y bajan 10 % semana a semana: un patrón aprendible a partir de
    la tendencia, que la línea base del último precio no captura."""
    azar = random.Random(semilla)
    return [
        _precio(
            INICIO + timedelta(weeks=i),
            categoria,
            round(base * (1.1 if i % 2 else 1.0) * azar.uniform(0.995, 1.005)),
        )
        for i in range(semanas)
        for categoria, base in BASES.items()
    ]


@pytest.fixture(scope="module")
def entrenado():
    return entrenar_modelo(_alternantes(70), OpcionesEntrenamiento(), ahora=AHORA)


def test_filas_de_prediccion_parten_de_la_ultima_subasta():
    serie = serie_precios(_alternantes(10))

    futuras = filas_prediccion(serie, horizontes=(1, 2, 3))

    ultima = pd.Timestamp(INICIO + timedelta(weeks=9))
    assert set(futuras.fecha_referencia) == {ultima}
    assert sorted(futuras.fecha.unique()) == [ultima + pd.Timedelta(weeks=h) for h in (1, 2, 3)]
    assert all(f.dayofweek == 4 for f in futuras.fecha)  # viernes
    assert len(futuras) == 3 * len(BASES)
    vaca = futuras[(futuras.categoria == "VACA HORRA") & (futuras.horizonte == 2)].iloc[0]
    assert vaca.ultimo_precio == serie[serie.categoria == "VACA HORRA"].precio.iloc[-1]
    assert vaca.semanas_desde_ultimo == pytest.approx(2)


def test_entrena_valida_y_supera_la_linea_base_en_un_patron_aprendible(entrenado):
    assert entrenado.metricas.pliegues == 5
    assert entrenado.metricas.mejora > 0.5
    assert entrenado.fechas_historico == 70
    assert entrenado.confianza.nivel in {"alta", "media"}
    assert entrenado.categorias == sorted(BASES)


def test_ranking_cubre_proximas_fechas_por_categoria(entrenado):
    ranking = entrenado.ranking
    ultima = pd.Timestamp(INICIO + timedelta(weeks=69))

    assert len(ranking) == 4 * len(BASES)
    assert set(ranking.fecha) == {ultima + pd.Timedelta(weeks=h) for h in (1, 2, 3, 4)}
    fila = ranking.iloc[0]
    assert fila.precio_predicho == pytest.approx(fila.ultimo_precio * (1 + fila.variacion_esperada))
    assert fila.error_esperado == pytest.approx(
        entrenado.metricas.por_horizonte[int(fila.horizonte)].mae
    )
    # el patrón alterna: la última semana (índice 69, impar) fue alta, así que la
    # siguiente debería predecirse más baja que la de dos semanas después
    vaca = ranking[ranking.categoria == "VACA HORRA"].set_index("horizonte")
    assert vaca.loc[1, "precio_predicho"] < vaca.loc[2, "precio_predicho"]


def test_recomendaciones_marcan_si_la_diferencia_entre_fechas_supera_el_error():
    fechas = [pd.Timestamp("2026-11-20"), pd.Timestamp("2026-11-27")]
    ranking = pd.DataFrame(
        {
            "fecha": fechas * 2,
            "categoria": ["A", "A", "B", "B"],
            "horizonte": [1, 2, 1, 2],
            "ultimo_precio": [100.0, 100.0, 200.0, 200.0],
            "precio_predicho": [101.0, 102.0, 230.0, 210.0],
            "variacion_esperada": [0.01, 0.02, 0.15, 0.05],
            "error_esperado": [5.0, 5.0, 5.0, 5.0],
        }
    )

    b, a = recomendaciones(ranking)

    assert (b.categoria, a.categoria) == ("B", "A")  # primero la de mayor variación
    assert (b.mejor_fecha, b.precio_predicho, b.distinguible) == (date(2026, 11, 20), 230.0, True)
    assert (a.mejor_fecha, a.distinguible) == (date(2026, 11, 27), False)


def test_historico_corto_no_entrena_y_lo_dice():
    with pytest.raises(DatosInsuficientes, match="fechas"):
        entrenar_modelo(_alternantes(20), OpcionesEntrenamiento(), ahora=AHORA)


def test_entrenamiento_es_reproducible(entrenado):
    otro = entrenar_modelo(_alternantes(70), OpcionesEntrenamiento(), ahora=AHORA)

    pd.testing.assert_frame_equal(entrenado.ranking, otro.ranking)


def test_registro_guarda_y_recupera_el_modelo(tmp_path, entrenado):
    registro = RegistroModelos(tmp_path / "modelos")

    version = registro.guardar(entrenado, ahora=AHORA)
    segunda = registro.guardar(entrenado, ahora=AHORA)

    assert version != segunda
    carpeta = tmp_path / "modelos" / version
    assert (carpeta / "modelo.json").is_file()
    metadatos = json.loads((carpeta / "metadatos.json").read_text(encoding="utf-8"))
    assert metadatos["version"] == version
    assert metadatos["confianza"]["nivel"] == entrenado.confianza.nivel
    assert metadatos["categorias"] == sorted(BASES)
    assert metadatos["fechas"] == {"desde": "2025-01-03", "hasta": "2026-05-01", "cantidad": 70}
    assert metadatos["metricas"]["pliegues"] == 5
    assert [e["version"] for e in registro.listar()] == [version, segunda]

    modelo, guardados = registro.cargar()
    assert guardados["version"] == segunda
    futuras = filas_prediccion(serie_precios(_alternantes(70)), horizontes=(1, 2, 3, 4))
    pd.testing.assert_series_equal(
        pd.Series(modelo.predict(futuras)), pd.Series(entrenado.modelo.predict(futuras))
    )
    with pytest.raises(KeyError):
        registro.cargar("no-existe")


def test_oro_guarda_predicciones_una_vez_por_version(tmp_path, entrenado):
    oro = AlmacenOro(tmp_path / "oro")

    escritas = oro.guardar_predicciones("v1", entrenado, ahora=AHORA)
    repetidas = oro.guardar_predicciones("v1", entrenado, ahora=AHORA)

    assert (escritas, repetidas) == (12, 0)
    tabla = DeltaTable(tmp_path / "oro" / "predicciones").to_pandas()
    assert len(tabla) == 12
    assert set(tabla.version) == {"v1"}
    assert set(tabla.confianza) == {entrenado.confianza.nivel}
