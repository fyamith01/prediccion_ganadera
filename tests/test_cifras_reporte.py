"""Cifras del reporte: calculadas en código a partir del resumen del modelado y de oro.

El modelo de IA solo redacta sobre estas cifras; aquí se fija qué cifras hay, cómo se
escriben (formato colombiano: punto de miles, coma decimal) y la barrera que detecta
números que no están en los hechos entregados.
"""

import random
from datetime import UTC, date, datetime, timedelta

import pytest

from prediccion_ganadera.adaptadores.almacen.modelos import RegistroModelos
from prediccion_ganadera.adaptadores.almacen.oro import AlmacenOro
from prediccion_ganadera.herramientas.cifras_reporte import (
    calcular_cifras,
    formato_fecha,
    formato_numero,
    formato_porcentaje,
    narrativa_determinista,
    numeros_de,
    numeros_no_permitidos,
)
from prediccion_ganadera.modelado.modelador import cargar_resumen, ejecutar_modelado
from prediccion_ganadera.modelado.variables import PrecioCategoria, ResultadoPrecios

INICIO = date(2025, 1, 3)  # viernes
AHORA = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
BASES = {"VACA HORRA": 7800, "MACHO LEVANTE": 10500, "HEMBRA LEVANTE": 9800}


def _precios(semanas: int) -> ResultadoPrecios:
    azar = random.Random(3)
    precios = [
        PrecioCategoria(
            fecha=INICIO + timedelta(weeks=i),
            especial=False,
            sha256="a" * 64,
            seccion="hembra",
            categoria=categoria,
            unidad="kg",
            animales=40,
            peso_cabeza=300,
            precio_minimo=precio,
            precio_maximo=precio,
            precio_promedio=precio,
            valor_animal=precio * 300,
        )
        for i in range(semanas)
        for categoria, base in BASES.items()
        for precio in [round(base * (1.1 if i % 2 else 1.0) * azar.uniform(0.995, 1.005))]
    ]
    return ResultadoPrecios(
        precios=precios, descartes=[], tablas_resumen=semanas, tablas_omitidas=0
    )


@pytest.fixture(scope="module")
def modelado(tmp_path_factory):
    datos = tmp_path_factory.mktemp("datos")
    resumen = ejecutar_modelado(
        _precios(60), datos=datos, fuente="plata", incluir_no_verificadas=False, ahora=AHORA
    )
    predicciones = AlmacenOro(datos / "oro").leer_predicciones(resumen.version)
    return datos, resumen, predicciones


@pytest.fixture(scope="module")
def cifras(modelado):
    _datos, resumen, predicciones = modelado
    return calcular_cifras(resumen, predicciones)


def test_formatos_en_espanol():
    assert formato_numero(1234567.891) == "1.234.568"
    assert formato_numero(12.345, decimales=1) == "12,3"
    assert formato_numero(-4500) == "-4.500"
    assert formato_porcentaje(0.1234) == "12,3 %"
    assert formato_porcentaje(-0.031, signo=True) == "-3,1 %"
    assert formato_porcentaje(0.05, signo=True) == "+5,0 %"
    assert formato_fecha(date(2026, 11, 20)) == "20/11/2026"


def test_el_resumen_del_modelado_queda_junto_al_modelo(modelado):
    datos, resumen, _ = modelado

    assert (datos / "modelos" / resumen.version / "resumen.json").is_file()
    assert cargar_resumen(datos, resumen.version) == resumen
    assert cargar_resumen(datos) == resumen  # sin versión: la más reciente
    with pytest.raises(KeyError):
        cargar_resumen(datos, "no-existe")
    with pytest.raises(KeyError):
        RegistroModelos(datos / "vacio").leer_resumen()


def test_oro_devuelve_las_predicciones_de_una_version(modelado):
    _datos, resumen, predicciones = modelado

    assert len(predicciones) == resumen.predicciones_oro == 12
    assert [(p.fecha, p.categoria) for p in predicciones] == sorted(
        (p.fecha, p.categoria) for p in predicciones
    )
    assert {p.categoria for p in predicciones} == set(BASES)
    assert AlmacenOro(_datos / "oro").leer_predicciones("otra") == []


def test_cifras_salen_del_resumen_y_de_oro(modelado, cifras):
    _datos, resumen, predicciones = modelado

    assert cifras.version == resumen.version
    assert cifras.fechas_historico == 60
    assert (cifras.fecha_desde, cifras.fecha_hasta) == (resumen.fecha_desde, resumen.fecha_hasta)
    assert cifras.mae == resumen.metricas.mae
    assert cifras.mejora == resumen.metricas.mejora
    assert cifras.confianza == resumen.confianza.nivel
    assert [h.horizonte for h in cifras.horizontes] == sorted(resumen.metricas.por_horizonte)
    assert [r.categoria for r in cifras.recomendaciones] == [
        r.categoria for r in resumen.recomendaciones
    ]
    assert len(cifras.predicciones) == len(predicciones)
    # fechas futuras agrupadas: una entrada por viernes, todas posteriores al histórico
    assert cifras.fechas_futuras == sorted({p.fecha for p in predicciones})
    assert min(cifras.fechas_futuras) > cifras.fecha_hasta


def test_los_hechos_traen_las_cifras_ya_formateadas(cifras):
    hechos = cifras.hechos()

    assert cifras.version in hechos
    assert formato_numero(cifras.mae) in hechos
    assert formato_porcentaje(cifras.mejora, signo=True) in hechos
    for recomendacion in cifras.recomendaciones:
        assert recomendacion.categoria in hechos
        assert formato_numero(recomendacion.precio_predicho) in hechos
        assert formato_fecha(recomendacion.mejor_fecha) in hechos
    assert cifras.confianza in hechos
    assert numeros_de(hechos) == cifras.numeros_permitidos()


def test_barrera_detecta_numeros_que_no_estan_en_los_hechos():
    permitidos = numeros_de("precio 9.800 $/kg, mejora +12,3 % el 20/11/2026")

    assert numeros_no_permitidos("Se espera 9.800 el 20/11/2026.", permitidos) == []
    assert numeros_no_permitidos("Mejora de 12,3 %, no de 15 %.", permitidos) == ["15"]
    # la misma cifra en otro formato tampoco vale: se exige copiarla tal cual
    assert numeros_no_permitidos("precio 9800 o 9,800", permitidos) == ["9800", "9,800"]


def test_narrativa_determinista_solo_usa_cifras_de_los_hechos(cifras):
    narrativa = narrativa_determinista(cifras, motivo="sin IA")

    assert narrativa.origen == "determinista"
    assert narrativa.motivo == "sin IA"
    assert cifras.confianza in narrativa.resumen
    assert narrativa.puntos
    texto = " ".join([narrativa.resumen, *narrativa.puntos])
    assert numeros_no_permitidos(texto, cifras.numeros_permitidos()) == []


def test_sin_recomendaciones_la_narrativa_lo_dice(cifras):
    vacias = cifras.model_copy(update={"recomendaciones": [], "predicciones": []})

    narrativa = narrativa_determinista(vacias)

    assert any("no hay recomendaciones" in p for p in narrativa.puntos)
