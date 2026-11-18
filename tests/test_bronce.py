import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from prediccion_ganadera.adaptadores.almacen.bronce import AlmacenBronce, ErrorContenido
from prediccion_ganadera.adaptadores.fuente.http import ErrorFuente
from prediccion_ganadera.herramientas.descarga import descargar_nuevos
from prediccion_ganadera.herramientas.enlaces import EnlaceSubasta, fecha_de_texto

AHORA = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
PDF = b"%PDF-1.7\n contenido de prueba"
UPLOADS = "https://www.subastas.example/wp-content/uploads/"


def _enlace(nombre: str, texto: str, archivo: date | None = None) -> EnlaceSubasta:
    return EnlaceSubasta(
        url=UPLOADS + nombre,
        texto=texto,
        fecha_texto=fecha_de_texto(texto),
        fecha_archivo=archivo,
        especial="Esp" in texto,
    )


def test_guarda_pdf_por_hash_y_registra_en_manifiesto(tmp_path: Path):
    almacen = AlmacenBronce(tmp_path / "bronce")

    registro = almacen.guardar(
        UPLOADS + "a.pdf", PDF, fecha=date(2026, 1, 9), especial=False, ahora=AHORA
    )

    sha = hashlib.sha256(PDF).hexdigest()
    assert registro.sha256 == sha
    assert (tmp_path / "bronce" / registro.ruta).read_bytes() == PDF
    assert registro.ruta == f"pdfs/{sha}.pdf"
    assert almacen.registros() == [registro]
    assert almacen.urls_registradas() == {UPLOADS + "a.pdf"}


def test_guardar_es_idempotente_por_url(tmp_path: Path):
    almacen = AlmacenBronce(tmp_path)
    primero = almacen.guardar(UPLOADS + "a.pdf", PDF, fecha=None, especial=False, ahora=AHORA)

    segundo = almacen.guardar(UPLOADS + "a.pdf", PDF, fecha=None, especial=False, ahora=AHORA)

    assert segundo == primero
    assert len(almacen.registros()) == 1


def test_rechaza_contenido_que_no_es_pdf(tmp_path: Path):
    almacen = AlmacenBronce(tmp_path)

    with pytest.raises(ErrorContenido, match="PDF"):
        almacen.guardar(
            UPLOADS + "a.pdf", b"<html>406</html>", fecha=None, especial=False, ahora=AHORA
        )

    assert almacen.registros() == []
    assert not (tmp_path / "pdfs").exists() or list((tmp_path / "pdfs").iterdir()) == []


class _FuenteFalsa:
    def __init__(self, respuestas: dict[str, bytes | Exception]):
        self.respuestas = respuestas
        self.pedidas: list[str] = []

    def descargar(self, url: str) -> bytes:
        self.pedidas.append(url)
        respuesta = self.respuestas[url]
        if isinstance(respuesta, Exception):
            raise respuesta
        return respuesta


def test_descargar_nuevos_omite_registrados_y_acumula_errores(tmp_path: Path):
    almacen = AlmacenBronce(tmp_path)
    ya = _enlace("ya.pdf", "Subasta 02-01-2026")
    almacen.guardar(ya.url, PDF, fecha=ya.fecha, especial=False, ahora=AHORA)
    nuevo = _enlace("nuevo.pdf", "Subasta Esp. 17-10-2026")
    caido = _enlace("caido.pdf", "Subasta 09-01-2026")
    html = _enlace("html.pdf", "Subasta 16-01-2026")
    conflicto = _enlace("2-1-2025.pdf", "Subasta 24-01-2025", archivo=date(2025, 1, 2))
    fuente = _FuenteFalsa(
        {
            nuevo.url: PDF + b"nuevo",
            caido.url: ErrorFuente("503 tras 3 reintentos"),
            html.url: b"<html></html>",
            conflicto.url: PDF + b"conflicto",
        }
    )

    resultado = descargar_nuevos(
        [ya, nuevo, caido, html, conflicto],
        fuente,
        almacen,
        fechas_decididas={conflicto.url: date(2025, 1, 24)},
        ahora=AHORA,
    )

    assert ya.url not in fuente.pedidas
    assert [r.url for r in resultado.nuevos] == [nuevo.url, conflicto.url]
    assert resultado.nuevos[0].especial is True
    assert resultado.nuevos[1].fecha == date(2025, 1, 24)
    assert resultado.omitidos == [ya.url]
    assert [url for url, _ in resultado.errores] == [caido.url, html.url]
    assert "503" in resultado.errores[0][1]
    assert len(almacen.registros()) == 3
