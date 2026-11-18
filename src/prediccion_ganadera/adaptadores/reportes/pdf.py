"""Reporte en PDF con reportlab (platypus): narrativa arriba, cifras en tablas abajo.

Todo el texto pasa por `escape`, porque la narrativa viene de un modelo de IA y
reportlab interpreta marcado tipo HTML en los párrafos. El PDF es reproducible
(`invariant`): las mismas cifras y la misma narrativa dan los mismos bytes.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import (
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from prediccion_ganadera.herramientas.cifras_reporte import (
    CifrasReporte,
    Narrativa,
    formato_fecha,
    formato_numero,
    formato_porcentaje,
)

TITULO = "Resumen del modelo de precios de subastas ganaderas"
NOTA_IA = (
    "Narrativa redactada por IA sobre las cifras de este reporte. Las cifras se calcularon "
    "en código y se comprobó que la narrativa no agrega números que no estén en ellas."
)
AVISO_FINAL = (
    "Las predicciones son estimaciones con el error indicado en cada fila; si la diferencia "
    "entre fechas no supera ese error, ninguna fecha es claramente mejor."
)


class RenderizadorPdf:
    extension = ".pdf"

    def __init__(self) -> None:
        estilos = getSampleStyleSheet()
        self._titulo = estilos["Title"]
        self._seccion = estilos["Heading2"]
        self._normal = estilos["BodyText"]
        self._nota = ParagraphStyle("nota", parent=estilos["BodyText"], fontSize=8, leading=10)
        self._celda = ParagraphStyle("celda", parent=estilos["BodyText"], fontSize=8, leading=10)
        self._aviso = ParagraphStyle(
            "aviso", parent=estilos["BodyText"], textColor=colors.HexColor("#9a3412")
        )

    def _p(self, texto: str, estilo: ParagraphStyle | None = None) -> Paragraph:
        return Paragraph(escape(texto), estilo or self._normal)

    def _tabla(self, encabezado: list[str], filas: list[list[str]]) -> Table:
        datos = [[self._p(c, self._celda) for c in fila] for fila in [encabezado, *filas]]
        tabla = Table(datos, repeatRows=1, hAlign="LEFT")
        tabla.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e5e7eb")),
                    ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#9ca3af")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        return tabla

    def _lista(self, textos: list[str]) -> ListFlowable:
        return ListFlowable(
            [ListItem(self._p(t)) for t in textos], bulletType="bullet", leftIndent=12
        )

    def renderizar(
        self,
        cifras: CifrasReporte,
        narrativa: Narrativa,
        destino: Path,
        *,
        generado_en: datetime,
    ) -> None:
        doc = SimpleDocTemplate(
            str(destino),
            pagesize=A4,
            leftMargin=2 * cm,
            rightMargin=2 * cm,
            topMargin=2 * cm,
            bottomMargin=2 * cm,
            title=TITULO,
            author="prediccion-ganadera",
            invariant=1,
        )
        doc.build(self._contenido(cifras, narrativa, generado_en))

    def _contenido(self, cifras: CifrasReporte, narrativa: Narrativa, generado_en: datetime):
        partes = [
            self._p(TITULO, self._titulo),
            self._p(
                f"Generado el {formato_fecha(generado_en.date())} · modelo {cifras.version} · "
                f"confianza {cifras.confianza}"
            ),
        ]
        if cifras.incluye_no_verificadas:
            partes.append(
                self._p(
                    "Advertencia: el modelo incluye tablas sin verificar visualmente; las "
                    "cifras pueden tener errores de extracción.",
                    self._aviso,
                )
            )
        partes += [Spacer(1, 6), self._p("Resumen", self._seccion), self._p(narrativa.resumen)]
        if narrativa.puntos:
            partes.append(self._lista(narrativa.puntos))
        origen = (
            NOTA_IA
            if narrativa.origen == "ia"
            else f"Narrativa generada sin IA (motivo: {narrativa.motivo or 'no indicado'})."
        )
        partes.append(self._p(origen, self._nota))

        partes += [
            self._p("Indicadores del modelo", self._seccion),
            self._tabla(
                ["Indicador", "Valor"],
                [
                    ["Subastas en el histórico", str(cifras.fechas_historico)],
                    [
                        "Rango de fechas",
                        f"{formato_fecha(cifras.fecha_desde)} a "
                        f"{formato_fecha(cifras.fecha_hasta)}",
                    ],
                    ["Precios por categoría", formato_numero(cifras.precios)],
                    [
                        "Filas descartadas / tablas omitidas",
                        f"{cifras.descartes} / {cifras.tablas_omitidas}",
                    ],
                    ["Error medio (MAE)", f"{formato_numero(cifras.mae)} $/kg"],
                    ["Línea base (último precio)", f"{formato_numero(cifras.mae_ingenuo)} $/kg"],
                    [
                        "Mejora frente a la línea base",
                        formato_porcentaje(cifras.mejora, signo=True),
                    ],
                    ["Error porcentual (MAPE)", formato_porcentaje(cifras.mape)],
                    ["Pliegues de validación temporal", str(cifras.pliegues)],
                    ["Fuente", cifras.fuente],
                    ["Confianza", cifras.confianza],
                ],
            ),
            self._p("Recomendaciones por categoría", self._seccion),
        ]
        if cifras.recomendaciones:
            partes.append(
                self._tabla(
                    [
                        "Categoría",
                        "Mejor fecha",
                        "Último $/kg",
                        "Esperado $/kg",
                        "Variación",
                        "Error ±",
                        "¿Distinguible?",
                    ],  # fmt: skip
                    [
                        [
                            r.categoria,
                            formato_fecha(r.mejor_fecha),
                            formato_numero(r.ultimo_precio),
                            formato_numero(r.precio_predicho),
                            formato_porcentaje(r.variacion_esperada, signo=True),
                            formato_numero(r.error_esperado),
                            "sí" if r.distinguible else "no",
                        ]
                        for r in cifras.recomendaciones
                    ],
                )
            )
        else:
            partes.append(self._p("No hay recomendaciones en esta corrida."))

        partes += [
            self._p("Precisión por horizonte", self._seccion),
            self._tabla(
                ["Subastas adelante", "MAE $/kg", "Línea base $/kg", "Mejora"],
                [
                    [
                        str(h.horizonte),
                        formato_numero(h.mae),
                        formato_numero(h.mae_ingenuo),
                        formato_porcentaje(h.mejora, signo=True),
                    ]
                    for h in cifras.horizontes
                ],
            ),
            self._p("Predicciones por fecha", self._seccion),
        ]
        if cifras.predicciones:
            partes.append(
                self._tabla(
                    [
                        "Fecha",
                        "Categoría",
                        "Horizonte",
                        "Último $/kg",
                        "Esperado $/kg",
                        "Variación",
                        "Error ±",
                    ],  # fmt: skip
                    [
                        [
                            formato_fecha(p.fecha),
                            p.categoria,
                            str(p.horizonte),
                            formato_numero(p.ultimo_precio),
                            formato_numero(p.precio_predicho),
                            formato_porcentaje(p.variacion_esperada, signo=True),
                            formato_numero(p.error_esperado),
                        ]
                        for p in cifras.predicciones
                    ],
                )
            )
        else:
            partes.append(self._p("No hay predicciones guardadas para esta versión."))
        if cifras.motivos_confianza:
            partes += [
                self._p("Motivos de la confianza", self._seccion),
                self._lista(cifras.motivos_confianza),
            ]
        partes += [Spacer(1, 12), self._p(AVISO_FINAL, self._nota)]
        return partes
