import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from prediccion_ganadera.adaptadores.almacen.plata import AlmacenPlata, ClaveTabla
from prediccion_ganadera.cli import main
from prediccion_ganadera.herramientas.extraer_tablas_pdf import (
    AJUSTES_POR_DEFECTO,
    FilaExtraida,
    TablaExtraida,
)

AHORA = datetime(2026, 11, 18, tzinfo=UTC)
SHA = "c" * 64


def _config(tmp_path: Path) -> Path:
    datos = tmp_path / "data"
    plata = AlmacenPlata(datos / "plata")
    plata.guardar(
        sha256=SHA,
        fecha=date(2026, 7, 24),
        especial=False,
        tablas=[
            TablaExtraida(
                pagina=pagina,
                indice=1,
                columnas=2,
                filas=(FilaExtraida(indice=0, celdas=("Lote", "7")),),
                ajustes=AJUSTES_POR_DEFECTO,
            )
            for pagina in (1, 2)
        ],
        ahora=AHORA,
    )
    plata.marcar_estado(ClaveTabla(sha256=SHA, pagina=1, tabla=1), "verificada")
    config = tmp_path / "agentes.yaml"
    config.write_text(
        f"""
version: 1
backends: {{}}
agentes: {{}}
rutas: {{datos: "{datos}", reportes: "{tmp_path / "reportes"}"}}
""",
        encoding="utf-8",
    )
    return config


def test_cargar_lleva_plata_a_postgres_y_es_idempotente(
    tmp_path: Path, postgres_dsn: str, monkeypatch: pytest.MonkeyPatch, capsys
):
    config = _config(tmp_path)
    monkeypatch.setenv("PREDICCION_PG_DSN", postgres_dsn)

    assert main(["cargar", "--config", str(config)]) == 0
    primera = capsys.readouterr().out
    assert main(["cargar", "--config", str(config), "--json"]) == 0
    segunda = json.loads(capsys.readouterr().out)

    assert "2 tabla(s) nuevas" in primera
    assert "1 en staging" in primera
    assert segunda == {
        "tablas_nuevas": 0,
        "tablas_actualizadas": 0,
        "tablas_sin_cambios": 2,
        "tablas_en_staging": 1,
        "celdas": 0,
    }


def test_la_opcion_dsn_tiene_prioridad_sobre_el_entorno(
    tmp_path: Path, postgres_dsn: str, monkeypatch: pytest.MonkeyPatch, capsys
):
    monkeypatch.setenv("PREDICCION_PG_DSN", "postgresql://nadie@/x?host=/no/existe")

    codigo = main(["cargar", "--config", str(_config(tmp_path)), "--dsn", postgres_dsn])

    assert codigo == 0
    assert "2 tabla(s) nuevas" in capsys.readouterr().out


def test_sin_dsn_termina_con_error_de_configuracion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    monkeypatch.delenv("PREDICCION_PG_DSN", raising=False)

    assert main(["cargar", "--config", str(_config(tmp_path))]) == 2
    assert "PREDICCION_PG_DSN" in capsys.readouterr().err


def test_un_servidor_inalcanzable_no_muestra_la_contrasena(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    # Socket Unix en un directorio inexistente: falla al instante y sin red.
    dsn = "postgresql://usuario:secreto123@/ganaderia?host=/no/existe&connect_timeout=1"
    monkeypatch.setenv("PREDICCION_PG_DSN", dsn)

    assert main(["cargar", "--config", str(_config(tmp_path))]) == 1
    salida = capsys.readouterr()
    assert "no se pudo cargar en Postgres" in salida.err
    assert "secreto123" not in salida.err + salida.out
