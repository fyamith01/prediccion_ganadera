"""Punto de entrada `prediccion-ganadera`."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from prediccion_ganadera import __version__
from prediccion_ganadera.configuracion import ErrorConfiguracion, cargar_configuracion

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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prediccion-ganadera", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="comando", required=True)
    validar = sub.add_parser("validar-config", help="valida config/agentes.yaml")
    validar.add_argument("--config", default=CONFIG_POR_DEFECTO)
    validar.set_defaults(func=_validar_config)
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
