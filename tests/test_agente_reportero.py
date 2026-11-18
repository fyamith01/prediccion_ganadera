"""Agente reportero: la IA redacta sobre cifras calculadas en código y nunca agrega números.

Sin red ni IA real: un proveedor en memoria con respuestas en cola y el harness falso
local con sobres SINTÉTICOS (`tests/fixtures/ia/reportero_*.json`).
"""

import json
import sys
from pathlib import Path

import pytest

from prediccion_ganadera.adaptadores.ia.cli_harness import ProveedorCliHarness
from prediccion_ganadera.agentes.reportero import AgenteReportero
from prediccion_ganadera.configuracion import BackendCliHarness
from prediccion_ganadera.herramientas.cifras_reporte import numeros_no_permitidos
from prediccion_ganadera.puertos.ia import ErrorProveedorIA, RespuestaIA

FIXTURES = Path(__file__).parent / "fixtures" / "ia"


def _narrativa_de(sobre: str) -> str:
    return json.loads((FIXTURES / sobre).read_text(encoding="utf-8"))["result"]


BUENA = _narrativa_de("reportero_narrativa.json")
INVENTA = _narrativa_de("reportero_inventa_cifras.json")


class _IAFalsa:
    def __init__(self, *respuestas: str | Exception) -> None:
        self.respuestas = list(respuestas)
        self.prompts: list[str] = []

    def completar(self, prompt, imagenes=()):
        self.prompts.append(prompt)
        respuesta = self.respuestas.pop(0)
        if isinstance(respuesta, Exception):
            raise respuesta
        return RespuestaIA(texto=respuesta, tokens_entrada=100, tokens_salida=20, costo_usd=0.01)


def test_usa_la_narrativa_de_la_ia_cuando_solo_cita_los_hechos(cifras_sinteticas):
    cifras = cifras_sinteticas()
    ia = _IAFalsa(BUENA)

    resultado = AgenteReportero(ia=ia).redactar(cifras)

    assert resultado.narrativa.origen == "ia"
    assert resultado.narrativa.resumen.startswith("Con 94 subastas")
    assert len(resultado.narrativa.puntos) == 2
    assert resultado.rechazos == []
    assert (resultado.uso.llamadas_ia, resultado.uso.tokens) == (1, 120)
    # el prompt lleva los hechos y la regla de copiar las cifras tal cual
    assert cifras.hechos() in ia.prompts[0]
    assert "tal cual" in ia.prompts[0]


def test_reintenta_una_vez_si_inventa_cifras_y_dice_cuales(cifras_sinteticas):
    ia = _IAFalsa(INVENTA, BUENA)

    resultado = AgenteReportero(ia=ia).redactar(cifras_sinteticas())

    assert resultado.narrativa.origen == "ia"
    assert resultado.uso.llamadas_ia == 2
    assert len(resultado.rechazos) == 1
    assert "15" in resultado.rechazos[0] and "120" in resultado.rechazos[0]
    assert "15, 120" in ia.prompts[1]


def test_si_insiste_en_inventar_usa_la_narrativa_determinista(cifras_sinteticas):
    cifras = cifras_sinteticas()

    resultado = AgenteReportero(ia=_IAFalsa(INVENTA, INVENTA)).redactar(cifras)

    narrativa = resultado.narrativa
    assert narrativa.origen == "determinista"
    assert "números que no están en las cifras" in narrativa.motivo
    assert len(resultado.rechazos) == 2
    texto = " ".join([narrativa.resumen, *narrativa.puntos])
    assert numeros_no_permitidos(texto, cifras.numeros_permitidos()) == []


@pytest.mark.parametrize(
    ("respuestas", "motivo"),
    [
        (("no es json", "tampoco"), "respuesta ilegible"),
        (('{"resumen": "", "puntos": []}',) * 2, "respuesta ilegible"),
        (('{"resumen": "ok", "puntos": "no es lista"}',) * 2, "respuesta ilegible"),
        ((ErrorProveedorIA("se acabó el tiempo"),), "el backend de IA falló: se acabó el tiempo"),
    ],
)
def test_respuestas_invalidas_o_errores_terminan_en_la_narrativa_determinista(
    cifras_sinteticas, respuestas, motivo
):
    resultado = AgenteReportero(ia=_IAFalsa(*respuestas)).redactar(cifras_sinteticas())

    assert resultado.narrativa.origen == "determinista"
    assert motivo in resultado.narrativa.motivo
    assert resultado.uso.llamadas_ia == len(respuestas)


def test_sin_ia_o_sin_presupuesto_no_llama_al_backend(cifras_sinteticas):
    sin_ia = AgenteReportero(ia=None).redactar(cifras_sinteticas())
    ia = _IAFalsa(BUENA)
    sin_presupuesto = AgenteReportero(ia=ia, presupuesto_tokens=0).redactar(cifras_sinteticas())

    assert sin_ia.narrativa.origen == sin_presupuesto.narrativa.origen == "determinista"
    assert "desactivada" in sin_ia.narrativa.motivo
    assert "presupuesto" in sin_presupuesto.narrativa.motivo
    assert ia.prompts == []


def test_el_presupuesto_corta_el_reintento(cifras_sinteticas):
    ia = _IAFalsa(INVENTA, BUENA)

    resultado = AgenteReportero(ia=ia, presupuesto_tokens=100).redactar(cifras_sinteticas())

    assert len(ia.prompts) == 1  # la primera llamada gastó 120 tokens
    assert resultado.narrativa.origen == "determinista"
    assert "presupuesto" in resultado.narrativa.motivo


# Harness falso local: imprime el sobre grabado como si fuera `claude -p`.
HARNESS_FALSO = """
import pathlib, sys
print(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
"""


@pytest.mark.parametrize(
    ("sobre", "origen", "llamadas"),
    [("reportero_narrativa.json", "ia", 1), ("reportero_inventa_cifras.json", "determinista", 2)],
)
def test_con_el_harness_falso_y_sobres_sinteticos(cifras_sinteticas, sobre, origen, llamadas):
    backend = BackendCliHarness(
        tipo="cli_harness",
        comando=[sys.executable, "-c", HARNESS_FALSO, "{prompt}", str(FIXTURES / sobre)],
    )

    resultado = AgenteReportero(ia=ProveedorCliHarness(backend)).redactar(cifras_sinteticas())

    assert resultado.narrativa.origen == origen
    assert resultado.uso.llamadas_ia == llamadas
    assert resultado.uso.costo_usd > 0
