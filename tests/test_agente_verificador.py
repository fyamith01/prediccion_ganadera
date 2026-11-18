import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from prediccion_ganadera.adaptadores.almacen.bronce import AlmacenBronce
from prediccion_ganadera.adaptadores.almacen.plata import AlmacenPlata, ClaveTabla
from prediccion_ganadera.adaptadores.almacen.revision import ColaRevision
from prediccion_ganadera.adaptadores.ia.cli_harness import ProveedorCliHarness
from prediccion_ganadera.agentes.verificador import AgenteVerificador
from prediccion_ganadera.configuracion import BackendCliHarness
from prediccion_ganadera.herramientas.bronce_a_plata import extraer_bronce_a_plata
from prediccion_ganadera.puertos.ia import ErrorProveedorIA, RespuestaIA

FIXTURES = Path(__file__).parent / "fixtures"
PDF_MUESTRA = FIXTURES / "pdfs" / "Resumen-de-subasta-de-ganado-24-07-2026.pdf"
AHORA = datetime(2026, 11, 18, tzinfo=UTC)


def _veredicto(coincide=True, confianza=0.95, celdas=(), filas_faltantes=0) -> str:
    return json.dumps(
        {
            "coincide": coincide,
            "confianza": confianza,
            "encabezado_ok": coincide,
            "filas_faltantes": filas_faltantes,
            "celdas_erroneas": [
                {"fila": f, "columna": c, "valor_correcto": v} for f, c, v in celdas
            ],
            "comentario": "prueba",
        }
    )


ACEPTA = _veredicto()
CORRIGE_TITULO = _veredicto(False, 0.9, [(0, 4, "GENERAL DE"), (0, 5, "LA FERIA")])


class _IAFalsa:
    def __init__(self, *respuestas: str | Exception, tokens: int = 100):
        self.respuestas = list(respuestas)
        self.llamadas: list[tuple[str, list[Path]]] = []
        self.tokens = tokens

    def completar(self, prompt: str, imagenes=()) -> RespuestaIA:
        self.llamadas.append((prompt, list(imagenes)))
        respuesta = self.respuestas.pop(0)
        if isinstance(respuesta, Exception):
            raise respuesta
        return RespuestaIA(texto=respuesta, tokens_entrada=self.tokens, tokens_salida=10)


class _Entorno:
    def __init__(self, raiz: Path):
        self.raiz = raiz
        self.bronce = AlmacenBronce(raiz / "bronce")
        registro = self.bronce.guardar(
            "https://www.subastas.example/wp-content/uploads/2026/07/muestra.pdf",
            PDF_MUESTRA.read_bytes(),
            fecha=date(2026, 7, 24),
            especial=False,
            ahora=AHORA,
        )
        self.sha = registro.sha256
        self.plata = AlmacenPlata(raiz / "plata")
        self.cola = ColaRevision(raiz / "plata")
        extraer_bronce_a_plata(self.bronce, self.plata, ahora=AHORA)

    def agente(self, ia, **kwargs) -> AgenteVerificador:
        return AgenteVerificador(
            ia=ia,
            bronce=self.bronce,
            plata=self.plata,
            cola=self.cola,
            dir_paginas=self.raiz / "paginas",
            **kwargs,
        )

    def clave(self, pagina: int) -> ClaveTabla:
        return ClaveTabla(sha256=self.sha, pagina=pagina, tabla=1)

    def estados(self) -> dict[int, str]:
        return {p: self.plata.leer_tabla(self.clave(p)).estado for p in (1, 2, 3, 4)}

    def textos(self, pagina: int) -> list[str]:
        tabla = self.plata.leer_tabla(self.clave(pagina)).tabla
        return [" ".join(c for c in f.celdas if c) for f in tabla.filas]


@pytest.fixture
def entorno(tmp_path: Path) -> _Entorno:
    return _Entorno(tmp_path)


def test_sin_ia_anexa_los_totales_faltantes_y_deja_las_tablas_pendientes(entorno):
    resultado = entorno.agente(None).ejecutar(ahora=AHORA)

    assert entorno.estados() == {1: "pendiente", 2: "pendiente", 3: "pendiente", 4: "pendiente"}
    assert entorno.textos(1)[-1] == "GRAN TOTAL 389"
    assert entorno.textos(4)[-2:] == [
        "TOTAL LOTES 75 TOTAL ANIMALES 369 TOTAL CRIAS 9 11",
        "GRAN TOTAL 389",
    ]
    por_pagina = {t.clave.pagina: t for t in resultado.tablas}
    assert por_pagina[1].acciones == ["anexar_renglones: 1"]
    assert por_pagina[2].acciones == []
    assert all("sin verificación visual" in t.motivo for t in resultado.tablas)
    assert resultado.uso.llamadas_ia == 0


def test_el_verificador_ve_la_imagen_y_acepta(entorno):
    ia = _IAFalsa(ACEPTA, ACEPTA, ACEPTA, ACEPTA)

    resultado = entorno.agente(ia).ejecutar(ahora=AHORA)

    assert entorno.estados() == {1: "corregida", 2: "verificada", 3: "verificada", 4: "corregida"}
    prompt, imagenes = ia.llamadas[1]
    assert imagenes == [entorno.raiz / "paginas" / entorno.sha / "p2.png"]
    assert imagenes[0].read_bytes().startswith(b"\x89PNG")
    assert "f15: [0] 8 1 ML | [1] 250" in prompt
    assert "GRAN TOTAL 389" in ia.llamadas[0][0]  # la fila anexada viaja al modelo
    assert resultado.uso.llamadas_ia == 4
    assert resultado.uso.tokens == 4 * 110
    assert entorno.cola.entradas() == []


def test_aplica_correcciones_de_celdas_que_aparecen_en_la_pagina(entorno):
    ia = _IAFalsa(CORRIGE_TITULO, ACEPTA, ACEPTA, ACEPTA, ACEPTA)

    resultado = entorno.agente(ia).ejecutar(ahora=AHORA)

    fila0 = entorno.plata.leer_tabla(entorno.clave(1)).tabla.filas[0].celdas
    assert fila0[4:6] == ("GENERAL DE", "LA FERIA")
    assert entorno.estados()[1] == "corregida"
    pagina1 = next(t for t in resultado.tablas if t.clave.pagina == 1)
    assert pagina1.acciones == ["anexar_renglones: 1", "corregir_celdas: 2"]
    assert pagina1.verificaciones == 2


def test_rechaza_valores_que_no_estan_en_la_pagina_y_manda_a_revision(entorno):
    ia = _IAFalsa(_veredicto(False, 0.9, [(12, 2, "9,999")]), ACEPTA, ACEPTA, ACEPTA)

    resultado = entorno.agente(ia).ejecutar(ahora=AHORA)

    assert entorno.estados()[1] == "en_revision"
    assert entorno.textos(1)[-1] == "GRAN TOTAL 389"  # lo rectificado se conserva
    (entrada,) = entorno.cola.entradas()
    assert entrada.clave == entorno.clave(1)
    assert "9,999" in entrada.motivo
    assert resultado.cuenta() == {"corregida": 1, "verificada": 2, "en_revision": 1}


def test_respeta_el_maximo_de_rectificaciones(entorno):
    ia = _IAFalsa(*[CORRIGE_TITULO] * 5)

    resultado = entorno.agente(ia, max_rectificaciones=2).ejecutar(limite=1, ahora=AHORA)

    (tabla,) = resultado.tablas
    assert tabla.acciones == ["anexar_renglones: 1", "corregir_celdas: 2"]
    assert len(ia.llamadas) == 2
    assert tabla.estado == "en_revision"
    assert "2 rectificaciones" in tabla.motivo
    assert [e.intentos for e in entorno.cola.entradas()] == [2]


@pytest.mark.parametrize(
    ("respuesta", "motivo"),
    [
        (ErrorProveedorIA("sin red"), "sin red"),
        ("no sé", "ilegible"),
        (_veredicto(True, 0.5), "confianza"),
        (_veredicto(False, 0.9, filas_faltantes=2), "sin correcciones aplicables"),
    ],
)
def test_respuestas_no_concluyentes_van_a_revision(entorno, respuesta, motivo):
    resultado = entorno.agente(_IAFalsa(respuesta)).ejecutar(limite=1, ahora=AHORA)

    (tabla,) = resultado.tablas
    assert tabla.estado == "en_revision"
    assert motivo in tabla.motivo
    assert len(entorno.cola.entradas()) == 1


def test_presupuesto_agotado_deja_las_tablas_pendientes(entorno):
    ia = _IAFalsa(ACEPTA, tokens=500)

    resultado = entorno.agente(ia, presupuesto_tokens=300).ejecutar(ahora=AHORA)

    assert entorno.estados() == {1: "corregida", 2: "pendiente", 3: "pendiente", 4: "pendiente"}
    assert len(ia.llamadas) == 1
    assert "presupuesto" in next(t for t in resultado.tablas if t.clave.pagina == 2).motivo
    assert entorno.cola.entradas() == []


def test_una_segunda_corrida_no_repite_tablas_resueltas(entorno):
    entorno.agente(_IAFalsa(*[ACEPTA] * 4)).ejecutar(ahora=AHORA)

    segunda = entorno.agente(_IAFalsa()).ejecutar(ahora=AHORA)

    assert segunda.tablas == []


def test_una_tabla_verificada_sale_de_la_cola(entorno):
    entorno.agente(_IAFalsa("no sé")).ejecutar(limite=1, ahora=AHORA)
    entorno.plata.marcar_estado(entorno.clave(1), "pendiente")  # una persona la devuelve

    entorno.agente(_IAFalsa(ACEPTA)).ejecutar(limite=1, ahora=AHORA)

    # En esta corrida no cambió nada: la corrección quedó en la anterior (y en el historial Delta).
    assert entorno.estados()[1] == "verificada"
    assert entorno.cola.entradas() == []


# Harness falso local: responde como `claude -p --output-format json` con un sobre grabado
# y falla si la imagen que el prompt menciona no existe.
HARNESS_FALSO = """
import json, pathlib, re, sys
prompt, sobre = sys.argv[1], sys.argv[2]
rutas = re.findall(r"^- (/\\S+\\.png)$", prompt, re.MULTILINE)
if not rutas or not all(pathlib.Path(r).is_file() for r in rutas):
    print(json.dumps({"type": "result", "is_error": True, "result": "sin imagen"}))
    sys.exit(0)
print(pathlib.Path(sobre).read_text(encoding="utf-8"))
"""


@pytest.mark.parametrize(
    ("sobre", "estado"),
    [("verificador_acepta.json", "corregida"), ("verificador_corrige_titulo.json", "en_revision")],
)
def test_con_el_harness_falso_y_respuestas_grabadas(entorno, sobre, estado):
    backend = BackendCliHarness(
        tipo="cli_harness",
        comando=[sys.executable, "-c", HARNESS_FALSO, "{prompt}", str(FIXTURES / "ia" / sobre)],
    )

    resultado = entorno.agente(ProveedorCliHarness(backend), max_rectificaciones=2).ejecutar(
        limite=1, ahora=AHORA
    )

    (tabla,) = resultado.tablas
    assert tabla.estado == estado
    assert resultado.uso.costo_usd > 0


def test_un_pdf_que_falta_en_bronce_va_a_revision(entorno):
    entorno.bronce.ruta_pdf(entorno.sha).unlink()

    resultado = entorno.agente(_IAFalsa()).ejecutar(limite=1, ahora=AHORA)

    (tabla,) = resultado.tablas
    assert tabla.estado == "en_revision"
    assert "no se pudo leer el PDF" in tabla.motivo
