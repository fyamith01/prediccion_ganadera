"""Punto de entrada `prediccion-ganadera`."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from prediccion_ganadera import __version__
from prediccion_ganadera.configuracion import ErrorConfiguracion, cargar_configuracion
from prediccion_ganadera.puertos.ia import ErrorProveedorIA

if TYPE_CHECKING:
    from prediccion_ganadera.agentes.recolector import ResultadoRecoleccion

CONFIG_POR_DEFECTO = "config/agentes.yaml"


def _validar_config(args: argparse.Namespace) -> int:
    try:
        config = cargar_configuracion(args.config)
    except ErrorConfiguracion as error:
        print(error, file=sys.stderr)
        return 2
    print(f"Configuración válida: {args.config}")
    for agente in sorted(config.agentes):
        nombre = config.agentes[agente].backend
        print(f"  {agente} -> {nombre} ({config.backends[nombre].tipo})")
    return 0


def _recolectar(args: argparse.Namespace) -> int:
    # Importación diferida: `validar-config` no necesita httpx ni el agente.
    from prediccion_ganadera.adaptadores.almacen.bronce import AlmacenBronce
    from prediccion_ganadera.adaptadores.fuente.http import ErrorFuente, FuenteHttp
    from prediccion_ganadera.adaptadores.ia import crear_proveedor
    from prediccion_ganadera.agentes.recolector import AgenteRecolector

    try:
        config = cargar_configuracion(args.config)
        if config.fuente is None:
            raise ErrorConfiguracion(f"falta la sección 'fuente' en {args.config}")
        ia = None if args.sin_ia else crear_proveedor(config, "recolector")
    except (ErrorConfiguracion, ErrorProveedorIA) as error:
        print(error, file=sys.stderr)
        return 2
    agente = AgenteRecolector(
        ia=ia,
        fuente=FuenteHttp(config.fuente.dominios_permitidos),
        almacen=AlmacenBronce(config.rutas.datos / "bronce"),
        url_pagina=config.fuente.url,
        dominios_permitidos=config.fuente.dominios_permitidos,
        presupuesto_tokens=config.limites.presupuesto_tokens_corrida,
    )
    html = args.html.read_text(encoding="utf-8") if args.html else None
    try:
        resultado = agente.ejecutar(
            html=html, descargar=not args.solo_listar, desde=args.desde, hasta=args.hasta
        )
    except ErrorFuente as error:
        print(f"no se pudo leer la fuente: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(resultado.model_dump_json(indent=2))
    else:
        _imprimir_resumen(resultado)
    return 1 if resultado.descarga and resultado.descarga.errores else 0


def _fechas(fechas: Sequence[date]) -> str:
    return ", ".join(f.isoformat() for f in fechas) or "-"


def _imprimir_resumen(resultado: ResultadoRecoleccion) -> None:
    huecos = resultado.huecos
    print(f"Enlaces: {resultado.enlaces}")
    print(f"Rango: {huecos.desde} a {huecos.hasta} (cobertura {huecos.cobertura:.0%})")
    print(f"Faltantes ({len(huecos.faltantes)}): {_fechas(huecos.faltantes)}")
    print(f"Fuera de calendario: {_fechas(huecos.fuera_de_calendario)}")
    print(f"Duplicadas: {_fechas(huecos.duplicadas)}")
    for decision in resultado.decisiones:
        print(f"Decidida por IA: {decision.url} -> {decision.fecha} ({decision.razon})")
    print(f"Revisión manual ({len(resultado.revision)}):")
    for enlace in resultado.revision:
        print(f"  {enlace.url}: {enlace.motivo}")
    if resultado.descarga is not None:
        descarga = resultado.descarga
        print(
            f"Descargas: {len(descarga.nuevos)} nuevas, {len(descarga.omitidos)} ya registradas, "
            f"{len(descarga.errores)} con error"
        )
        for url, error in descarga.errores:
            print(f"  {url}: {error}")
    uso = resultado.uso
    print(f"IA: {uso.llamadas_ia} llamadas, {uso.tokens} tokens, US$ {uso.costo_usd:.4f}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prediccion-ganadera", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="comando", required=True)
    validar = sub.add_parser("validar-config", help="valida config/agentes.yaml")
    validar.add_argument("--config", default=CONFIG_POR_DEFECTO)
    validar.set_defaults(func=_validar_config)

    recolectar = sub.add_parser(
        "recolectar", help="lista, fecha y descarga a bronce los resúmenes de subastas"
    )
    recolectar.add_argument("--config", default=CONFIG_POR_DEFECTO)
    recolectar.add_argument("--html", type=Path, help="usa una copia local de la página")
    recolectar.add_argument("--solo-listar", action="store_true", help="no descarga PDFs")
    recolectar.add_argument("--sin-ia", action="store_true", help="conflictos a revisión")
    recolectar.add_argument("--desde", type=date.fromisoformat, help="AAAA-MM-DD")
    recolectar.add_argument("--hasta", type=date.fromisoformat, help="AAAA-MM-DD")
    recolectar.add_argument("--json", action="store_true", help="reporte en JSON")
    recolectar.set_defaults(func=_recolectar)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as salida:
        return int(salida.code or 0)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
