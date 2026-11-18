"""`prediccion-ganadera run`: la corrida completa sin red ni IA real.

La página de subastas viene de la copia local (`--html`) y nada se descarga
(`--solo-listar`); bronce se llena antes con el PDF de muestra. El backend de IA, cuando se
usa, es un script local que responde con un sobre grabado.
"""

import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest

from prediccion_ganadera.adaptadores.almacen.bronce import AlmacenBronce
from prediccion_ganadera.cli import main

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HTML = FIXTURES / "html" / "resumen_de_subastas.html"
PDF_MUESTRA = FIXTURES / "pdfs" / "Resumen-de-subasta-de-ganado-24-07-2026.pdf"
AHORA = datetime(2026, 11, 18, tzinfo=UTC)

HARNESS_FALSO = """
import pathlib, sys
print(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
"""


@pytest.fixture
def config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("PREDICCION_PG_DSN", raising=False)
    datos = tmp_path / "data"
    AlmacenBronce(datos / "bronce").guardar(
        "https://www.subastas.example/wp-content/uploads/2026/07/muestra.pdf",
        PDF_MUESTRA.read_bytes(),
        fecha=date(2026, 7, 24),
        especial=False,
        ahora=AHORA,
    )
    harness = tmp_path / "harness_falso.py"
    harness.write_text(HARNESS_FALSO, encoding="utf-8")
    sobre = FIXTURES / "ia" / "verificador_acepta.json"
    ruta = tmp_path / "agentes.yaml"
    ruta.write_text(
        f"""
version: 1
backends:
  falso:
    tipo: cli_harness
    comando: ["{sys.executable}", "{harness}", "{{prompt}}", "{sobre}"]
agentes:
  recolector: {{backend: falso}}
  verificador: {{backend: falso}}
rutas: {{datos: "{datos}", reportes: "{tmp_path / "reportes"}"}}
fuente:
  url: https://www.subastas.example/resumen-de-subastas/
  dominios_permitidos: [www.subastas.example, subastas.example]
""",
        encoding="utf-8",
    )
    return ruta


def _run(config: Path, *extra: str) -> int:
    return main(["run", "--config", str(config), "--html", str(HTML), "--solo-listar", *extra])


def _corridas(config: Path) -> list[dict]:
    ruta = config.parent / "data" / "corridas" / "corridas.jsonl"
    return [json.loads(linea) for linea in ruta.read_text(encoding="utf-8").splitlines()]


def test_corrida_completa_sin_ia_carga_postgres_y_registra_la_corrida(
    config: Path, postgres_dsn: str, monkeypatch: pytest.MonkeyPatch, capsys
):
    monkeypatch.setenv("PREDICCION_PG_DSN", postgres_dsn)

    codigo = _run(config, "--sin-ia", "--id", "prueba", "--json")

    assert codigo == 0
    estado = json.loads(capsys.readouterr().out)
    assert estado["id_corrida"] == "prueba"
    assert estado["estado"] == "completada"
    pasos = estado["pasos"]
    assert pasos["recolectar"]["resumen"]["enlaces"] == 95
    assert pasos["recolectar"]["resumen"]["descarga"] is None
    assert pasos["extraer"]["resumen"]["tablas"] == 4
    assert len(pasos["verificar"]["resumen"]["tablas"]) == 4
    assert pasos["cargar"]["resumen"]["tablas_nuevas"] == 4
    with psycopg.connect(postgres_dsn) as conexion:
        runs = conexion.execute("SELECT id_corrida, estado FROM operacion.runs").fetchall()
        tablas = conexion.execute("SELECT count(*) FROM raw.tablas").fetchone()
    assert runs == [("prueba", "completada")]
    assert tablas == (4,)
    assert [r["estado"] for r in _corridas(config)] == ["en_curso", "completada"]


def test_sin_dsn_omite_la_carga(config: Path, capsys):
    codigo = _run(config, "--sin-ia", "--id", "c1")

    assert codigo == 0
    salida = capsys.readouterr().out
    assert "Corrida c1: completada" in salida
    assert "cargar: omitido (sin conexión a Postgres" in salida
    assert (config.parent / "data" / "corridas" / "checkpoints.sqlite").exists()


def test_con_el_backend_configurado_suma_el_uso_de_todos_los_agentes(config: Path, capsys):
    codigo = _run(config, "--id", "c1", "--json")

    assert codigo == 0
    estado = json.loads(capsys.readouterr().out)
    pasos = estado["pasos"]
    assert pasos["verificar"]["tokens"] > 0
    assert pasos["recolectar"]["tokens"] > 0  # la fecha en conflicto se consulta al backend
    assert estado["tokens"] == pasos["recolectar"]["tokens"] + pasos["verificar"]["tokens"]
    assert estado["costo_usd"] > 0


def test_una_caida_se_reanuda_sin_repetir_los_pasos_hechos(
    config: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    def cae(*_args, **_kwargs):
        raise RuntimeError("se fue la luz")

    monkeypatch.setattr("prediccion_ganadera.orquestacion.pasos.extraer_bronce_a_plata", cae)
    assert _run(config, "--sin-ia", "--id", "c1") == 1
    error = capsys.readouterr().err
    assert "se fue la luz" in error
    assert "--reanudar c1" in error
    assert _corridas(config)[-1]["estado"] == "interrumpida"

    monkeypatch.undo()
    monkeypatch.delenv("PREDICCION_PG_DSN", raising=False)
    assert _run(config, "--sin-ia", "--reanudar", "c1", "--json") == 0
    estado = json.loads(capsys.readouterr().out)

    assert estado["estado"] == "completada"
    assert estado["pasos"]["extraer"]["resumen"]["tablas"] == 4
    registros = _corridas(config)
    assert [r["estado"] for r in registros] == [
        "en_curso",
        "interrumpida",
        "en_curso",
        "completada",
    ]
    assert len({r["iniciada_en"] for r in registros}) == 1


def test_un_postgres_inalcanzable_deja_la_corrida_fallida_sin_mostrar_la_contrasena(
    config: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    dsn = "postgresql://usuario:secreto123@/ganaderia?host=/no/existe&connect_timeout=1"
    monkeypatch.setenv("PREDICCION_PG_DSN", dsn)

    codigo = _run(config, "--sin-ia", "--id", "c1")

    assert codigo == 1
    salida = capsys.readouterr()
    assert "Corrida c1: fallida" in salida.out
    assert "cargar: no se pudo cargar en Postgres" in salida.err
    assert "secreto123" not in salida.out + salida.err
    assert _corridas(config)[-1]["estado"] == "fallida"


def test_reanudar_una_corrida_inexistente_o_repetir_un_id(config: Path, capsys):
    assert _run(config, "--sin-ia", "--reanudar", "nada") == 2
    assert "no existe" in capsys.readouterr().err

    assert _run(config, "--sin-ia", "--id", "c1") == 0
    assert _run(config, "--sin-ia", "--id", "c1") == 2
    assert "ya existe" in capsys.readouterr().err


def test_id_y_reanudar_son_excluyentes(config: Path):
    assert _run(config, "--id", "a", "--reanudar", "b") == 2
