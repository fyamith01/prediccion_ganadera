"""`prediccion-ganadera entrenar` y el paso `modelar`, sin red ni IA.

Plata se llena con tablas de resumen sintéticas en el mismo formato partido que
produce el extractor (`'VACA HO', 'RRA'`), una por viernes.
"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import psycopg
import pytest
from deltalake import DeltaTable

from prediccion_ganadera.adaptadores.almacen.plata import AlmacenPlata, ClaveTabla
from prediccion_ganadera.adaptadores.almacen.postgres import cargar_plata
from prediccion_ganadera.cli import main
from prediccion_ganadera.configuracion import cargar_configuracion
from prediccion_ganadera.herramientas.extraer_tablas_pdf import (
    AJUSTES_POR_DEFECTO,
    FilaExtraida,
    TablaExtraida,
)
from prediccion_ganadera.orquestacion.pasos import OpcionesCorrida, crear_pasos

INICIO = date(2025, 1, 3)  # viernes
AHORA = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
CATEGORIAS = {("VACA HO", "RRA"): 7800, ("MACHO L", "EVANTE"): 10500, ("HEMBRA", "LEVANTE"): 9800}


def _resumen(semana: int) -> TablaExtraida:
    filas: list[tuple[str, ...]] = [
        ("resumen", "_subasta_tipo", "", "RESUMEN", "GENERAL D", "E LA FERIA", "", ""),
        ("GANAD", "O HEMBRA DE PRIMER", "A", "", "", "", "", ""),
    ]
    for (a, b), base in CATEGORIAS.items():
        precio = round(base * (1.1 if semana % 2 else 1.0))
        filas.append((a, b, "40", "300", f"{precio:,}", f"{precio:,}", f"{precio:,}",
                      f"{precio * 300:,}"))  # fmt: skip
    return TablaExtraida(
        pagina=1,
        indice=1,
        columnas=8,
        filas=tuple(FilaExtraida(indice=i, celdas=f) for i, f in enumerate(filas)),
        ajustes=AJUSTES_POR_DEFECTO,
    )


def _llenar_plata(datos: Path, semanas: int, *, estado: str = "verificada") -> AlmacenPlata:
    plata = AlmacenPlata(datos / "plata")
    for i in range(semanas):
        sha = f"{i:064x}"
        fecha = INICIO + timedelta(weeks=i)
        plata.guardar(sha256=sha, fecha=fecha, especial=False, tablas=[_resumen(i)], ahora=AHORA)
        if estado != "pendiente":
            plata.marcar_estado(ClaveTabla(sha256=sha, pagina=1, tabla=1), estado)
    return plata


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("PREDICCION_PG_DSN", raising=False)
    ruta = tmp_path / "agentes.yaml"
    ruta.write_text(
        f"""
version: 1
backends:
  local: {{tipo: ollama, modelo: qwen2.5vl:3b}}
agentes:
  recolector: {{backend: local}}
  verificador: {{backend: local}}
rutas: {{datos: "{tmp_path / "data"}", reportes: "{tmp_path / "reportes"}"}}
fuente:
  url: https://www.subastas.example/resumen-de-subastas/
  dominios_permitidos: [www.subastas.example]
""",
        encoding="utf-8",
    )
    return ruta


def _datos(config: Path) -> Path:
    return config.parent / "data"


def test_entrena_registra_el_modelo_y_guarda_predicciones(config: Path, capsys):
    _llenar_plata(_datos(config), 40)

    codigo = main(["entrenar", "--config", str(config), "--json"])

    assert codigo == 0
    resumen = json.loads(capsys.readouterr().out)
    version = resumen["version"]
    assert resumen["fuente"] == "plata"
    assert resumen["fechas_historico"] == 40
    assert resumen["incluye_no_verificadas"] is False
    # 40 subastas son menos de un año: la confianza no puede ser alta
    assert resumen["confianza"]["nivel"] in {"media", "baja"}
    assert any("menos de un año" in m for m in resumen["confianza"]["motivos"])
    assert [r["categoria"] for r in resumen["recomendaciones"]] != []
    assert resumen["predicciones_oro"] == 12
    assert (_datos(config) / "modelos" / version / "modelo.json").is_file()
    oro = DeltaTable(_datos(config) / "oro" / "predicciones").to_pandas()
    assert set(oro.version) == {version}


def test_salida_de_texto_muestra_confianza_y_recomendaciones(config: Path, capsys):
    _llenar_plata(_datos(config), 40)

    assert main(["entrenar", "--config", str(config)]) == 0

    salida = capsys.readouterr().out
    assert "Confianza:" in salida
    assert "VACA HORRA" in salida
    assert "línea base" in salida


def test_sin_historico_suficiente_no_entrena_y_explica_por_que(config: Path, capsys):
    _llenar_plata(_datos(config), 30, estado="pendiente")

    codigo = main(["entrenar", "--config", str(config)])

    assert codigo == 1
    error = capsys.readouterr().err
    assert "no se entrenó" in error
    assert "30 tabla(s) de resumen sin validar" in error
    assert not (_datos(config) / "modelos").exists()


def test_incluir_no_verificadas_entrena_y_lo_advierte(config: Path, capsys):
    _llenar_plata(_datos(config), 40, estado="pendiente")

    codigo = main(["entrenar", "--config", str(config), "--incluir-no-verificadas", "--json"])

    assert codigo == 0
    resumen = json.loads(capsys.readouterr().out)
    assert resumen["incluye_no_verificadas"] is True
    assert any("sin verificar" in m for m in resumen["confianza"]["motivos"])


def test_desde_staging_requiere_dsn_y_entrena_con_lo_validado(
    config: Path, postgres_dsn: str, capsys
):
    plata = _llenar_plata(_datos(config), 40)
    with psycopg.connect(postgres_dsn) as conexion:
        cargar_plata(plata, conexion, ahora=AHORA)

    assert main(["entrenar", "--config", str(config), "--fuente", "staging"]) == 2
    codigo = main(
        ["entrenar", "--config", str(config), "--fuente", "staging", "--dsn", postgres_dsn,
         "--json"]
    )  # fmt: skip

    assert codigo == 0
    resumen = json.loads(capsys.readouterr().out)
    assert resumen["fuente"] == "staging"
    assert resumen["fechas_historico"] == 40


def test_paso_modelar_entrena_u_omite_segun_los_datos(config: Path):
    configuracion = cargar_configuracion(config)
    estado = {"presupuesto_restante": 1000}

    vacio = crear_pasos(configuracion, OpcionesCorrida(sin_ia=True))["modelar"](estado)
    _llenar_plata(_datos(config), 40)
    entrenado = crear_pasos(configuracion, OpcionesCorrida(sin_ia=True))["modelar"](estado)

    assert vacio.omitido.startswith("sin datos suficientes")
    assert entrenado.omitido is None
    assert entrenado.resumen["fechas_historico"] == 40
    assert (_datos(config) / "modelos" / entrenado.resumen["version"]).is_dir()
