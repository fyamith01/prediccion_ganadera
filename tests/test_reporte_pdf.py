"""Renderizado del reporte en PDF y publicación en `reportes/<aaaa-mm-dd>/`."""

import json
from datetime import UTC, datetime

import pdfplumber
import pytest

from prediccion_ganadera.adaptadores.reportes.pdf import RenderizadorPdf
from prediccion_ganadera.adaptadores.reportes.salida import publicar_reporte
from prediccion_ganadera.herramientas.cifras_reporte import Narrativa, narrativa_determinista

AHORA = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def _texto(ruta) -> str:
    with pdfplumber.open(ruta) as pdf:
        return "\n".join(p.extract_text() or "" for p in pdf.pages)


def test_el_pdf_trae_cifras_narrativa_y_tablas(tmp_path, cifras_sinteticas):
    cifras = cifras_sinteticas()
    narrativa = Narrativa(
        resumen="El precio del macho de levante subiría en las próximas semanas.",
        puntos=["MACHO LEVANTE: mejor fecha 27/11/2026."],
        origen="ia",
    )
    destino = tmp_path / "reporte.pdf"

    RenderizadorPdf().renderizar(cifras, narrativa, destino, generado_en=AHORA)

    texto = _texto(destino)
    assert destino.read_bytes().startswith(b"%PDF-")
    for esperado in [
        "20261118T120000Z",
        "El precio del macho de levante subiría",
        "MACHO LEVANTE",
        "10.813",  # precio predicho formateado
        "27/11/2026",
        "+3,5 %",
        "312",  # MAE
        "media",
        "Predicciones por fecha",
        "redactada por IA",
    ]:
        assert esperado in texto, esperado


def test_renderizar_es_reproducible(tmp_path, cifras_sinteticas):
    narrativa = narrativa_determinista(cifras_sinteticas(), motivo="sin IA")
    a, b = tmp_path / "a.pdf", tmp_path / "b.pdf"

    RenderizadorPdf().renderizar(cifras_sinteticas(), narrativa, a, generado_en=AHORA)
    RenderizadorPdf().renderizar(cifras_sinteticas(), narrativa, b, generado_en=AHORA)

    assert a.read_bytes() == b.read_bytes()
    assert "sin IA" in _texto(a)


def test_texto_con_marcado_se_escapa_y_las_advertencias_aparecen(tmp_path, cifras_sinteticas):
    narrativa = Narrativa(resumen="Ojo: <b>precio</b> & <para>", puntos=[], origen="ia")
    destino = tmp_path / "r.pdf"

    RenderizadorPdf().renderizar(
        cifras_sinteticas(incluye_no_verificadas=True, recomendaciones=[], predicciones=[]),
        narrativa,
        destino,
        generado_en=AHORA,
    )

    texto = _texto(destino)
    assert "<b>precio</b> & <para>" in texto
    assert "sin verificar" in texto
    assert "No hay recomendaciones" in texto


def test_publicar_escribe_pdf_y_json_por_dia_sin_sobrescribir(tmp_path, cifras_sinteticas):
    cifras = cifras_sinteticas()
    narrativa = narrativa_determinista(cifras, motivo="sin IA")
    raiz = tmp_path / "reportes"

    primero = publicar_reporte(raiz, cifras, narrativa, RenderizadorPdf(), ahora=AHORA)
    segundo = publicar_reporte(raiz, cifras, narrativa, RenderizadorPdf(), ahora=AHORA)

    assert primero.pdf == raiz / "2026-11-18" / "resumen_20261118T120000Z.pdf"
    assert segundo.pdf == raiz / "2026-11-18" / "resumen_20261118T120000Z-2.pdf"
    assert primero.pdf.is_file() and segundo.pdf.is_file()
    datos = json.loads(primero.json.read_text(encoding="utf-8"))
    assert primero.json == primero.pdf.with_suffix(".json")
    assert datos["cifras"]["version"] == cifras.version
    assert datos["narrativa"]["origen"] == "determinista"
    assert datos["generado_en"] == AHORA.isoformat()


@pytest.mark.parametrize("version", ["../fuera", "a/b", "", "v 1"])
def test_una_version_rara_no_puede_escribir_fuera_de_reportes(tmp_path, cifras_sinteticas, version):
    cifras = cifras_sinteticas(version=version)

    with pytest.raises(ValueError, match="versión"):
        publicar_reporte(
            tmp_path, cifras, narrativa_determinista(cifras), RenderizadorPdf(), ahora=AHORA
        )
    assert list(tmp_path.iterdir()) == []
