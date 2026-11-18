"""`prediccion-ganadera evaluar extraccion|trayectorias|modelo`, sin red ni IA (T9e)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from prediccion_ganadera.adaptadores.almacen.corridas import RegistroCorrida, RegistroJsonl
from prediccion_ganadera.cli import main
from prediccion_ganadera.modelado.modelador import ejecutar_modelado

RAIZ = Path(__file__).parent.parent
MUESTRAS = RAIZ / "evaluacion" / "muestras"
AHORA = datetime(2026, 11, 18, 12, tzinfo=UTC)


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("PREDICCION_PG_DSN", raising=False)
    ruta = tmp_path / "agentes.yaml"
    ruta.write_text(
        f"""
version: 1
backends:
  claude: {{tipo: cli_harness, comando: ["claude", "-p", "{{prompt}}"]}}
agentes:
  recolector: {{backend: claude}}
rutas: {{datos: "{tmp_path / "data"}", reportes: "{tmp_path / "reportes"}"}}
fuente:
  url: https://www.subastas.example/resumen-de-subastas/
  dominios_permitidos: [www.subastas.example]
""",
        encoding="utf-8",
    )
    return ruta


def _correr(argv: list[str], capsys) -> tuple[int, str, str]:
    codigo = main(argv)
    salida = capsys.readouterr()
    return codigo, salida.out, salida.err


# -- extracción ---------------------------------------------------------------------------


def test_extraccion_de_las_muestras_del_repositorio(config, capsys, monkeypatch):
    monkeypatch.chdir(RAIZ)  # la ruta del PDF en la muestra es relativa a la raíz
    codigo, salida, _ = _correr(
        ["evaluar", "extraccion", "--config", str(config), "--muestras", str(MUESTRAS)], capsys
    )

    assert codigo == 0
    assert "BORRADOR" in salida  # la muestra aún no tiene revisión humana
    assert "GRAN TOTAL 389" in salida


def test_extraccion_en_json_incluye_las_razones(config, capsys, monkeypatch):
    monkeypatch.chdir(RAIZ)
    codigo, salida, _ = _correr(
        ["evaluar", "extraccion", "--config", str(config), "--muestras", str(MUESTRAS), "--json"],
        capsys,
    )

    assert codigo == 0
    datos = json.loads(salida)
    evaluacion = datos["evaluaciones"][0]
    assert evaluacion["estado"] == "borrador"
    assert evaluacion["filas"]["recall"] == pytest.approx(21 / 22)
    assert evaluacion["precios"]["exactitud_campos"] == 1.0
    assert datos["total"]["muestras"] == 1


def test_extraccion_sin_muestras(config, tmp_path, capsys):
    vacio = tmp_path / "vacio"
    vacio.mkdir()
    codigo, _, error = _correr(
        ["evaluar", "extraccion", "--config", str(config), "--muestras", str(vacio)], capsys
    )

    assert codigo == 1
    assert "no hay muestras" in error


def test_extraccion_desde_plata_vacia(config, capsys, monkeypatch):
    monkeypatch.chdir(RAIZ)
    codigo, _, error = _correr(
        ["evaluar", "extraccion", "--config", str(config), "--muestras", str(MUESTRAS),
         "--fuente", "plata"],
        capsys,
    )  # fmt: skip

    assert codigo == 1
    assert "plata" in error


# -- trayectorias -------------------------------------------------------------------------


def _registrar(config: Path) -> None:
    registro = RegistroJsonl(config.parent / "data" / "corridas" / "corridas.jsonl")
    base = {
        "iniciada_en": AHORA,
        "actualizada_en": AHORA,
        "terminada_en": None,
        "tokens": 0,
        "costo_usd": 0.0,
        "error": None,
        "pasos": {},
    }
    registro.guardar(RegistroCorrida(id_corrida="a", estado="en_curso", **base))
    registro.guardar(
        RegistroCorrida(id_corrida="a", estado="completada", **{**base, "terminada_en": AHORA})
    )


def test_trayectorias_desde_el_jsonl(config, capsys):
    _registrar(config)

    codigo, salida, _ = _correr(
        ["evaluar", "trayectorias", "--config", str(config), "--json"], capsys
    )

    assert codigo == 0
    datos = json.loads(salida)
    assert datos["corridas"] == 1
    assert datos["tasa_exito"] == 1.0
    assert datos["reanudadas"] == 0


def test_trayectorias_texto(config, capsys):
    _registrar(config)

    codigo, salida, _ = _correr(["evaluar", "trayectorias", "--config", str(config)], capsys)

    assert codigo == 0
    assert "1 corrida" in salida


def test_trayectorias_sin_corridas(config, capsys):
    codigo, _, error = _correr(["evaluar", "trayectorias", "--config", str(config)], capsys)

    assert codigo == 1
    assert "no hay corridas" in error


def test_trayectorias_postgres_sin_dsn_es_error_de_configuracion(config, capsys):
    codigo, _, error = _correr(
        ["evaluar", "trayectorias", "--config", str(config), "--fuente", "postgres"], capsys
    )

    assert codigo == 2
    assert "PREDICCION_PG_DSN" in error


def test_trayectorias_desde_postgres(config, capsys, postgres_dsn):
    from prediccion_ganadera.adaptadores.almacen.corridas import RegistroPostgres

    RegistroPostgres(postgres_dsn).guardar(
        RegistroCorrida(
            id_corrida="p", estado="fallida", iniciada_en=AHORA, actualizada_en=AHORA,
            terminada_en=AHORA, tokens=0, costo_usd=0.0, error="recolectar: 406", pasos={},
        )
    )  # fmt: skip

    codigo, salida, _ = _correr(
        ["evaluar", "trayectorias", "--config", str(config), "--fuente", "postgres",
         "--dsn", postgres_dsn, "--json"],
        capsys,
    )  # fmt: skip

    assert codigo == 0
    datos = json.loads(salida)
    assert datos["por_estado"] == {"fallida": 1}
    assert datos["reanudadas"] is None  # operacion.runs no guarda el historial


# -- modelo -------------------------------------------------------------------------------


def test_modelo_sin_versiones(config, capsys):
    codigo, _, error = _correr(["evaluar", "modelo", "--config", str(config)], capsys)

    assert codigo == 1
    assert "no hay modelos" in error


def test_modelo_con_una_version(config, capsys, precios_sinteticos):
    ejecutar_modelado(
        precios_sinteticos(40), datos=config.parent / "data", fuente="plata",
        incluir_no_verificadas=False, ahora=AHORA,
    )  # fmt: skip

    codigo, salida, _ = _correr(
        ["evaluar", "modelo", "--config", str(config), "--seguimiento", "--json"], capsys
    )

    assert codigo == 0
    datos = json.loads(salida)
    version = datos["versiones"][0]
    assert version["fechas_historico"] == 40
    # plata está vacía: no hay precios observados para comparar todavía
    assert version["seguimiento"]["comparadas"] == 0

    codigo, salida, _ = _correr(["evaluar", "modelo", "--config", str(config)], capsys)
    assert codigo == 0
    assert "MAE" in salida
