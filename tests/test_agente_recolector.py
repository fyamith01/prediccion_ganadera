import json
from datetime import UTC, date, datetime
from pathlib import Path

from prediccion_ganadera.adaptadores.almacen.bronce import AlmacenBronce
from prediccion_ganadera.agentes.recolector import AgenteRecolector
from prediccion_ganadera.puertos.ia import ErrorProveedorIA, RespuestaIA

PAGINA = "https://www.subastas.example/resumen-de-subastas/"
UPLOADS = "https://www.subastas.example/wp-content/uploads/"
AHORA = datetime(2026, 11, 18, tzinfo=UTC)
HTML = (
    "<ul>"
    f'<li><a href="{UPLOADS}2025/01/10-01-25.pdf">Subasta 10-01-2025</a></li>'
    f'<li><a href="{UPLOADS}2025/01/17-01-25.pdf">Subasta 17-01-2025</a></li>'
    f'<li><a href="{UPLOADS}2025/01/resumen-2-1-2025.pdf">Subasta 24-01-2025</a></li>'
    f'<li><a href="{UPLOADS}2025/02/informe.pdf">Informe</a></li>'
    f'<li><a href="{UPLOADS}2025/02/31-01-25.pdf">Subasta 31-01-2025</a></li>'
    "</ul>"
)
CONFLICTO = UPLOADS + "2025/01/resumen-2-1-2025.pdf"
SIN_FECHA = UPLOADS + "2025/02/informe.pdf"


class _IAFalsa:
    def __init__(self, *respuestas: str | Exception, tokens: int = 100):
        self.respuestas = list(respuestas)
        self.prompts: list[str] = []
        self.tokens = tokens

    def completar(self, prompt: str) -> RespuestaIA:
        self.prompts.append(prompt)
        respuesta = self.respuestas.pop(0)
        if isinstance(respuesta, Exception):
            raise respuesta
        return RespuestaIA(texto=respuesta, tokens_entrada=self.tokens, tokens_salida=10)


class _FuenteFalsa:
    def __init__(self):
        self.descargadas: list[str] = []
        self.paginas: list[str] = []

    def obtener_html(self, url: str) -> str:
        self.paginas.append(url)
        return HTML

    def descargar(self, url: str) -> bytes:
        self.descargadas.append(url)
        return b"%PDF-1.7 " + url.encode()


def _agente(ia, tmp_path: Path, **kwargs) -> tuple[AgenteRecolector, _FuenteFalsa]:
    fuente = _FuenteFalsa()
    agente = AgenteRecolector(
        ia=ia,
        fuente=fuente,
        almacen=AlmacenBronce(tmp_path / "bronce"),
        url_pagina=PAGINA,
        dominios_permitidos=["www.subastas.example"],
        **kwargs,
    )
    return agente, fuente


def test_la_ia_decide_un_conflicto_entre_las_candidatas(tmp_path: Path):
    respuesta = '```json\n{"fecha": "2025-01-24", "razon": "es viernes y sigue al 17"}\n```'
    ia = _IAFalsa(respuesta)
    agente, fuente = _agente(ia, tmp_path)

    resultado = agente.ejecutar(ahora=AHORA)

    assert fuente.paginas == [PAGINA]
    assert len(ia.prompts) == 1
    assert "2025-01-24" in ia.prompts[0] and "2025-01-02" in ia.prompts[0]
    assert "2025-01-17" in ia.prompts[0]  # contexto: fechas publicadas vecinas
    [decision] = resultado.decisiones
    assert (decision.url, decision.fecha) == (CONFLICTO, date(2025, 1, 24))
    assert [r.url for r in resultado.revision] == [SIN_FECHA]
    assert resultado.huecos.faltantes == ()
    assert SIN_FECHA not in fuente.descargadas
    fechas = {r.url: r.fecha for r in resultado.descarga.nuevos}
    assert fechas[CONFLICTO] == date(2025, 1, 24)
    assert len(resultado.descarga.nuevos) == 4
    assert resultado.uso.llamadas_ia == 1
    assert resultado.uso.tokens == 110


def test_una_fecha_inventada_por_la_ia_va_a_revision(tmp_path: Path):
    ia = _IAFalsa(json.dumps({"fecha": "2025-01-23", "razon": "inventada"}))
    agente, fuente = _agente(ia, tmp_path)

    resultado = agente.ejecutar(ahora=AHORA)

    assert resultado.decisiones == []
    assert {r.url for r in resultado.revision} == {CONFLICTO, SIN_FECHA}
    motivo = next(r.motivo for r in resultado.revision if r.url == CONFLICTO)
    assert "candidatas" in motivo
    assert CONFLICTO not in fuente.descargadas
    assert resultado.huecos.faltantes == (date(2025, 1, 24),)


def test_respuesta_ilegible_o_error_del_backend_van_a_revision(tmp_path: Path):
    for respuesta in ("no sé", ErrorProveedorIA("timeout")):
        agente, _ = _agente(_IAFalsa(respuesta), tmp_path / str(id(respuesta)))

        resultado = agente.ejecutar(ahora=AHORA)

        assert CONFLICTO in {r.url for r in resultado.revision}


def test_la_ia_puede_declinar_y_eso_va_a_revision(tmp_path: Path):
    ia = _IAFalsa(json.dumps({"fecha": None, "razon": "no hay evidencia suficiente"}))
    agente, _ = _agente(ia, tmp_path)

    resultado = agente.ejecutar(ahora=AHORA)

    motivo = next(r.motivo for r in resultado.revision if r.url == CONFLICTO)
    assert "no hay evidencia suficiente" in motivo


def test_presupuesto_de_tokens_agotado_no_consulta_la_ia(tmp_path: Path):
    ia = _IAFalsa()
    agente, _ = _agente(ia, tmp_path, presupuesto_tokens=0)

    resultado = agente.ejecutar(ahora=AHORA)

    assert ia.prompts == []
    motivo = next(r.motivo for r in resultado.revision if r.url == CONFLICTO)
    assert "presupuesto" in motivo


def test_sin_ia_y_solo_listar(tmp_path: Path):
    agente, fuente = _agente(None, tmp_path)

    resultado = agente.ejecutar(html=HTML, descargar=False, ahora=AHORA)

    assert fuente.paginas == []
    assert fuente.descargadas == []
    assert resultado.descarga is None
    assert resultado.enlaces == 5
    assert {r.url for r in resultado.revision} == {CONFLICTO, SIN_FECHA}


def test_rango_explicito_para_el_reporte_de_huecos(tmp_path: Path):
    agente, _ = _agente(None, tmp_path)

    resultado = agente.ejecutar(
        html=HTML, descargar=False, desde=date(2025, 1, 1), hasta=date(2025, 2, 14)
    )

    assert resultado.huecos.faltantes == (
        date(2025, 1, 3),
        date(2025, 1, 24),
        date(2025, 2, 7),
        date(2025, 2, 14),
    )
