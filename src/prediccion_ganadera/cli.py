"""Punto de entrada `prediccion-ganadera`."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING

from prediccion_ganadera import __version__
from prediccion_ganadera.configuracion import (
    FALTA_FUENTE,
    ErrorConfiguracion,
    cargar_configuracion,
)
from prediccion_ganadera.puertos.ia import ErrorProveedorIA

if TYPE_CHECKING:
    from prediccion_ganadera.adaptadores.almacen.revision import ColaRevision
    from prediccion_ganadera.agentes.recolector import ResultadoRecoleccion
    from prediccion_ganadera.agentes.verificador import ResultadoVerificacion

CONFIG_POR_DEFECTO = "config/agentes.yaml"
VARIABLE_DSN = "PREDICCION_PG_DSN"


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
            raise ErrorConfiguracion(FALTA_FUENTE)
        ia = None if args.sin_ia else crear_proveedor(config, "recolector")
    except (ErrorConfiguracion, ErrorProveedorIA) as error:
        print(error, file=sys.stderr)
        return 2
    agente = AgenteRecolector(
        ia=ia,
        fuente=FuenteHttp(
            config.fuente.dominios_permitidos, x509_estricto=config.fuente.x509_estricto
        ),
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


def _extraer(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from prediccion_ganadera.adaptadores.almacen.bronce import AlmacenBronce
    from prediccion_ganadera.adaptadores.almacen.plata import AlmacenPlata
    from prediccion_ganadera.herramientas.bronce_a_plata import extraer_bronce_a_plata

    try:
        config = cargar_configuracion(args.config)
    except ErrorConfiguracion as error:
        print(error, file=sys.stderr)
        return 2
    resumen = extraer_bronce_a_plata(
        AlmacenBronce(config.rutas.datos / "bronce"),
        AlmacenPlata(config.rutas.datos / "plata"),
        ahora=datetime.now(UTC),
    )
    if args.json:
        print(resumen.model_dump_json(indent=2))
    else:
        print(
            f"{len(resumen.procesados)} PDF(s) extraídos ({resumen.tablas} tablas, "
            f"{resumen.celdas} celdas), {resumen.omitidos} ya en plata, "
            f"{len(resumen.sin_tablas)} sin tablas, {len(resumen.errores)} con error"
        )
        for sha, motivo in resumen.errores.items():
            print(f"  {sha}: {motivo}")
    return 1 if resumen.errores else 0


def _verificar(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from prediccion_ganadera.adaptadores.almacen.bronce import AlmacenBronce
    from prediccion_ganadera.adaptadores.almacen.plata import AlmacenPlata
    from prediccion_ganadera.adaptadores.almacen.revision import ColaRevision
    from prediccion_ganadera.adaptadores.ia import crear_proveedor
    from prediccion_ganadera.agentes.verificador import AgenteVerificador

    try:
        config = cargar_configuracion(args.config)
        ia = None if args.sin_ia else crear_proveedor(config, "verificador")
    except (ErrorConfiguracion, ErrorProveedorIA) as error:
        print(error, file=sys.stderr)
        return 2
    datos = config.rutas.datos
    cola = ColaRevision(datos / "plata")
    agente = AgenteVerificador(
        ia=ia,
        bronce=AlmacenBronce(datos / "bronce"),
        plata=AlmacenPlata(datos / "plata"),
        cola=cola,
        dir_paginas=datos / "paginas",
        max_rectificaciones=config.limites.max_iteraciones_rectificacion,
        confianza_minima=config.limites.confianza_minima_verificacion,
        presupuesto_tokens=config.limites.presupuesto_tokens_corrida,
    )
    resultado = agente.ejecutar(ahora=datetime.now(UTC), limite=args.limite)
    if args.json:
        print(resultado.model_dump_json(indent=2))
    else:
        _imprimir_verificacion(resultado, cola)
    return 0


def _imprimir_verificacion(resultado: ResultadoVerificacion, cola: ColaRevision) -> None:
    cuenta = ", ".join(f"{estado}: {n}" for estado, n in sorted(resultado.cuenta().items()))
    print(f"{len(resultado.tablas)} tabla(s) revisadas ({cuenta or 'ninguna pendiente'})")
    for tabla in resultado.tablas:
        clave = tabla.clave
        acciones = ", ".join(tabla.acciones) or "sin cambios"
        print(
            f"  {clave.sha256[:12]} p{clave.pagina} t{clave.tabla}: {tabla.estado} "
            f"[{acciones}] {tabla.motivo}"
        )
    entradas = cola.entradas()
    print(f"Cola de revisión ({len(entradas)}):")
    for entrada in entradas:
        clave = entrada.clave
        print(f"  {clave.sha256[:12]} p{clave.pagina} t{clave.tabla}: {entrada.motivo}")
    uso = resultado.uso
    print(f"IA: {uso.llamadas_ia} llamadas, {uso.tokens} tokens, US$ {uso.costo_usd:.4f}")


def _cargar(args: argparse.Namespace) -> int:
    import os
    from datetime import UTC, datetime

    import psycopg

    from prediccion_ganadera.adaptadores.almacen.plata import AlmacenPlata
    from prediccion_ganadera.adaptadores.almacen.postgres import cargar_plata

    try:
        config = cargar_configuracion(args.config)
        dsn = args.dsn or os.environ.get(VARIABLE_DSN)
        if not dsn:
            raise ErrorConfiguracion(
                f"falta la conexión a Postgres: define {VARIABLE_DSN} o usa --dsn "
                "(ver README, «Base de datos local»)"
            )
    except ErrorConfiguracion as error:
        print(error, file=sys.stderr)
        return 2
    try:
        with psycopg.connect(dsn) as conexion:
            resumen = cargar_plata(
                AlmacenPlata(config.rutas.datos / "plata"), conexion, ahora=datetime.now(UTC)
            )
    except psycopg.Error as error:
        # libpq no incluye la contraseña en sus mensajes; el DSN nunca se imprime.
        print(f"no se pudo cargar en Postgres: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(resumen.model_dump_json(indent=2))
    else:
        print(
            f"{resumen.tablas_nuevas} tabla(s) nuevas, {resumen.tablas_actualizadas} "
            f"actualizadas, {resumen.tablas_sin_cambios} sin cambios ({resumen.celdas} celdas "
            f"escritas); {resumen.tablas_en_staging} en staging"
        )
    return 0


def _entrenar(args: argparse.Namespace) -> int:
    import os
    from datetime import UTC, datetime

    import psycopg

    from prediccion_ganadera.modelado.dataset import DatosInsuficientes
    from prediccion_ganadera.modelado.modelador import ejecutar_modelado, leer_precios

    try:
        config = cargar_configuracion(args.config)
        dsn = args.dsn or os.environ.get(VARIABLE_DSN)
        if args.fuente == "staging" and not dsn:
            raise ErrorConfiguracion(
                f"la fuente staging requiere Postgres: define {VARIABLE_DSN} o usa --dsn"
            )
    except ErrorConfiguracion as error:
        print(error, file=sys.stderr)
        return 2
    try:
        precios = leer_precios(
            args.fuente,
            datos=config.rutas.datos,
            dsn=dsn,
            incluir_no_verificadas=args.incluir_no_verificadas,
        )
        resumen = ejecutar_modelado(
            precios,
            datos=config.rutas.datos,
            fuente=args.fuente,
            incluir_no_verificadas=args.incluir_no_verificadas,
            ahora=datetime.now(UTC),
        )
    except DatosInsuficientes as error:
        print(f"no se entrenó: {error}", file=sys.stderr)
        return 1
    except psycopg.Error as error:
        print(f"no se pudo leer staging en Postgres: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(resumen.model_dump_json(indent=2))
    else:
        _imprimir_modelado(resumen)
    return 0


def _imprimir_modelado(resumen) -> None:
    m = resumen.metricas
    print(
        f"Modelo {resumen.version} ({resumen.fuente}): {resumen.fechas_historico} subastas "
        f"del {resumen.fecha_desde} al {resumen.fecha_hasta}, {resumen.precios} precios"
    )
    print(
        f"Validación temporal: error medio {m.mae:,.0f} $/kg vs {m.mae_ingenuo:,.0f} $/kg de la "
        f"línea base (último precio); mejora {m.mejora:.1%} en {m.pliegues} pliegues"
    )
    print(f"Confianza: {resumen.confianza.nivel}")
    for motivo in resumen.confianza.motivos:
        print(f"  - {motivo}")
    print("Mejor fecha por categoría (mayor variación esperada primero):")
    for r in resumen.recomendaciones:
        nota = "" if r.distinguible else "  (diferencia entre fechas menor que el error)"
        print(
            f"  {r.categoria}: {r.mejor_fecha} -> {r.precio_predicho:,.0f} $/kg "
            f"({r.variacion_esperada:+.1%} ± {r.error_esperado:,.0f}){nota}"
        )


def _reportar(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from prediccion_ganadera.adaptadores.ia import crear_proveedor
    from prediccion_ganadera.adaptadores.reportes.pdf import RenderizadorPdf
    from prediccion_ganadera.agentes.reportero import AgenteReportero
    from prediccion_ganadera.modelado.modelador import cargar_resumen

    try:
        config = cargar_configuracion(args.config)
        ia = None if args.sin_ia else crear_proveedor(config, "reportero")
    except ErrorConfiguracion as error:
        print(error, file=sys.stderr)
        return 2
    try:
        resumen = cargar_resumen(config.rutas.datos, args.version)
    except KeyError:
        if args.version is None:
            print("no hay un modelo entrenado: ejecuta `entrenar` o `run` antes", file=sys.stderr)
        else:
            print(f"la versión {args.version} no existe o no tiene resumen", file=sys.stderr)
        return 1
    agente = AgenteReportero(ia=ia, presupuesto_tokens=config.limites.presupuesto_tokens_corrida)
    publicado = agente.publicar(
        resumen,
        datos=config.rutas.datos,
        reportes=config.rutas.reportes,
        renderizador=RenderizadorPdf(),
        ahora=datetime.now(UTC),
    )
    if args.json:
        print(publicado.model_dump_json(indent=2))
        return 0
    print(f"Reporte del modelo {publicado.version}: {publicado.pdf}")
    if publicado.narrativa == "ia":
        print(f"Narrativa: redactada por IA ({publicado.uso.tokens} tokens)")
    else:
        print(f"Narrativa: narrativa determinista ({publicado.motivo})")
    for rechazo in publicado.rechazos:
        print(f"  - respuesta rechazada: {rechazo}")
    return 0


def _comparar_federado(args: argparse.Namespace) -> int:
    import os

    import psycopg

    from prediccion_ganadera.modelado.dataset import DatosInsuficientes
    from prediccion_ganadera.modelado.entrenamiento import OpcionesEntrenamiento
    from prediccion_ganadera.modelado.federado import REPARTOS, comparar_federado
    from prediccion_ganadera.modelado.modelador import leer_precios

    try:
        config = cargar_configuracion(args.config)
        dsn = args.dsn or os.environ.get(VARIABLE_DSN)
        if args.fuente == "staging" and not dsn:
            raise ErrorConfiguracion(
                f"la fuente staging requiere Postgres: define {VARIABLE_DSN} o usa --dsn"
            )
    except ErrorConfiguracion as error:
        print(error, file=sys.stderr)
        return 2
    repartos = REPARTOS if args.reparto == "todos" else (args.reparto,)
    try:
        precios = leer_precios(
            args.fuente,
            datos=config.rutas.datos,
            dsn=dsn,
            incluir_no_verificadas=args.incluir_no_verificadas,
        )
        comparaciones = [
            comparar_federado(precios.precios, r, OpcionesEntrenamiento(), rondas=args.rondas)
            for r in repartos
        ]
    except DatosInsuficientes as error:
        print(
            f"no se comparó: {error}; {len(precios.fechas)} subasta(s) con precios",
            file=sys.stderr,
        )
        return 1
    except psycopg.Error as error:
        print(f"no se pudo leer staging en Postgres: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps([c.model_dump(mode="json") for c in comparaciones], indent=2))
    else:
        for comparacion in comparaciones:
            _imprimir_comparacion(comparacion)
    return 0


def _imprimir_comparacion(c) -> None:
    clientes = ", ".join(f"{k} ({n} fechas)" for k, n in c.fechas_por_cliente.items())
    if not c.viable:
        print(f"{c.reparto}: no viable; clientes: {clientes}")
        for motivo in c.motivos:
            print(f"  - {motivo}")
        return
    print(f"{c.reparto}: clientes: {clientes}; {c.rondas} rondas federadas")
    for nombre, m in (("central", c.central), ("federado", c.federado)):
        print(
            f"  {nombre}: error medio {m.mae:,.0f} $/kg (línea base {m.mae_ingenuo:,.0f}); "
            f"mejora {m.mejora:.1%} en {m.pliegues} pliegues"
        )
    print(f"  diferencia federado − central: {c.diferencia_mae:+,.0f} $/kg")


def _run(args: argparse.Namespace) -> int:
    import json
    import os
    from datetime import UTC, datetime

    from prediccion_ganadera.adaptadores.almacen.corridas import RegistroJsonl, RegistroPostgres
    from prediccion_ganadera.orquestacion.grafo import ErrorPaso, Orquestador, abrir_checkpointer
    from prediccion_ganadera.orquestacion.pasos import OpcionesCorrida, crear_pasos

    try:
        config = cargar_configuracion(args.config)
        dsn = args.dsn or os.environ.get(VARIABLE_DSN)
        opciones = OpcionesCorrida(
            sin_ia=args.sin_ia,
            html=args.html.read_text(encoding="utf-8") if args.html else None,
            descargar=not args.solo_listar,
            desde=args.desde,
            hasta=args.hasta,
            dsn=dsn,
        )
        pasos = crear_pasos(config, opciones)
    except (ErrorConfiguracion, ErrorProveedorIA) as error:
        print(error, file=sys.stderr)
        return 2
    dir_corridas = config.rutas.datos / "corridas"
    registros = [RegistroJsonl(dir_corridas / "corridas.jsonl")]
    if dsn:
        registros.append(RegistroPostgres(dsn))
    id_corrida = args.reanudar or args.id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    with abrir_checkpointer(dir_corridas / "checkpoints.sqlite") as checkpointer:
        orquestador = Orquestador(
            pasos,
            checkpointer,
            presupuesto_tokens=config.limites.presupuesto_tokens_corrida,
            registros=registros,
        )
        try:
            if args.reanudar:
                estado = orquestador.reanudar(id_corrida)
            else:
                estado = orquestador.iniciar(id_corrida)
        except ErrorPaso as error:
            print(error, file=sys.stderr)
            return 2
        except Exception as error:
            print(
                f"la corrida {id_corrida} se interrumpió: {type(error).__name__}: {error}\n"
                f"Lo terminado quedó guardado; continúa con: "
                f"prediccion-ganadera run --reanudar {id_corrida}",
                file=sys.stderr,
            )
            return 1
    if args.json:
        print(json.dumps(estado, indent=2, ensure_ascii=False))
    else:
        _imprimir_corrida(estado)
    if estado["estado"] == "fallida":
        print(estado["error"], file=sys.stderr)
        return 1
    return 0


def _imprimir_corrida(estado: dict) -> None:
    print(
        f"Corrida {estado['id_corrida']}: {estado['estado']} "
        f"(IA: {estado['tokens']} tokens, US$ {estado['costo_usd']:.4f})"
    )
    for nombre, paso in estado["pasos"].items():
        if paso["omitido"]:
            print(f"  {nombre}: omitido ({paso['omitido']})")
        else:
            print(f"  {nombre}: {paso['duracion_s']:.2f} s, {paso['tokens']} tokens")


# -- evaluación (T9) ------------------------------------------------------------------------


def _pct(valor: float | None) -> str:
    return "n/d" if valor is None else f"{valor:.1%}"


def _evaluar_extraccion(args: argparse.Namespace) -> int:
    from prediccion_ganadera.adaptadores.almacen.bronce import AlmacenBronce
    from prediccion_ganadera.adaptadores.almacen.plata import AlmacenPlata
    from prediccion_ganadera.evaluacion.extraccion import (
        ErrorMuestra,
        cargar_muestra,
        combinar,
        evaluar_extraccion,
        tablas_de_plata,
        tablas_del_extractor,
    )

    try:
        config = cargar_configuracion(args.config)
        rutas = sorted(args.muestras.glob("*.yaml"))
        if not rutas:
            print(f"no hay muestras (*.yaml) en {args.muestras}", file=sys.stderr)
            return 1
        muestras = [(ruta, cargar_muestra(ruta)) for ruta in rutas]
    except (ErrorConfiguracion, ErrorMuestra) as error:
        print(error, file=sys.stderr)
        return 2
    bronce = AlmacenBronce(config.rutas.datos / "bronce")
    plata = AlmacenPlata(config.rutas.datos / "plata")
    evaluaciones = []
    try:
        for ruta, muestra in muestras:
            if args.fuente == "plata":
                tablas = tablas_de_plata(plata, muestra.sha256)
            else:
                pdf = bronce.ruta_pdf(muestra.sha256)
                if not pdf.is_file() and muestra.ruta:
                    pdf = Path(muestra.ruta)
                if not pdf.is_file():
                    raise ErrorMuestra(f"{ruta.name}: el PDF no está en bronce ni en «ruta»")
                tablas = tablas_del_extractor(pdf, muestra.sha256)
            evaluaciones.append((ruta, evaluar_extraccion(muestra, tablas, fuente=args.fuente)))
    except ErrorMuestra as error:
        print(f"no se evaluó: {error}", file=sys.stderr)
        return 1
    total = combinar([e for _, e in evaluaciones])
    if args.json:
        print(
            json.dumps(
                {
                    "evaluaciones": [
                        {"muestra": r.name, **e.model_dump(mode="json")} for r, e in evaluaciones
                    ],
                    "total": total.model_dump(mode="json"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    for ruta, e in evaluaciones:
        marca = (
            "revisada" if e.revisada else "BORRADOR: sin revisión humana, no es una métrica final"
        )
        print(f"{ruta.name} [{marca}] fuente={e.fuente}")
        f, p = e.filas, e.precios
        print(f"  filas encontradas: {f.encontradas}/{f.esperadas} (recall {_pct(f.recall)})")
        print(f"  precisión en páginas completas: {_pct(f.precision)}")
        print(
            f"  valores: {f.valores_correctos}/{f.valores_esperados} ({_pct(f.exactitud_valores)})"
        )
        print(
            f"  precios del resumen: {p.campos_correctos}/{p.campos_esperados} campos "
            f"({_pct(p.exactitud_campos)})"
        )
        for detalle in e.detalles:
            print(f"    - {detalle}")
    if len(evaluaciones) > 1:
        f = total.filas
        print(
            f"Total: {total.muestras} muestras ({total.revisadas} revisadas); recall "
            f"{_pct(f.recall)}, precisión {_pct(f.precision)}, valores "
            f"{_pct(f.exactitud_valores)}, precios {_pct(total.precios.exactitud_campos)}"
        )
    return 0


def _evaluar_trayectorias(args: argparse.Namespace) -> int:
    import os

    import psycopg

    from prediccion_ganadera.adaptadores.almacen.corridas import RegistroJsonl, RegistroPostgres
    from prediccion_ganadera.evaluacion.trayectorias import evaluar_trayectorias

    try:
        config = cargar_configuracion(args.config)
        dsn = args.dsn or os.environ.get(VARIABLE_DSN)
        if args.fuente == "postgres" and not dsn:
            raise ErrorConfiguracion(
                f"la fuente postgres requiere una conexión: define {VARIABLE_DSN} o usa --dsn"
            )
    except ErrorConfiguracion as error:
        print(error, file=sys.stderr)
        return 2
    if args.fuente == "postgres":
        try:
            historial = RegistroPostgres(dsn).leer()
        except psycopg.Error as error:
            print(f"no se pudo leer operacion.runs: {error}", file=sys.stderr)
            return 1
    else:
        historial = RegistroJsonl(config.rutas.datos / "corridas" / "corridas.jsonl").historial()
    if not historial:
        print("no hay corridas registradas", file=sys.stderr)
        return 1
    m = evaluar_trayectorias(historial, con_historial=args.fuente == "jsonl")
    if args.json:
        print(m.model_dump_json(indent=2))
        return 0
    estados = ", ".join(f"{n} {e}" for e, n in m.por_estado.items())
    reanudadas = "desconocidas" if m.reanudadas is None else m.reanudadas
    print(
        f"{m.corridas} corrida(s): {estados}; éxito {_pct(m.tasa_exito)}; reanudadas {reanudadas}"
    )
    print(f"Tokens {m.tokens_totales:,}; costo USD {m.costo_total_usd:.4f}")
    for paso, segundos in m.duracion_media_pasos.items():
        print(f"  {paso}: {segundos:.1f} s en promedio")
    for omision, veces in m.omisiones.items():
        print(f"  omitido {veces} vez/veces -> {omision}")
    for id_corrida, error in m.errores.items():
        print(f"  error en {id_corrida}: {error}")
    v = m.verificador
    if v.tablas:
        print(
            f"Verificador: {v.tablas} tablas {v.por_estado}; resueltas sin humano "
            f"{_pct(v.tasa_automatica)}, a revisión {_pct(v.tasa_revision)}; "
            f"{v.vision_por_tabla:.2f} llamadas de visión y {v.rectificaciones_por_tabla:.2f} "
            f"rectificaciones por tabla {v.acciones_por_tipo}"
        )
    if (r := m.recolector_ultima) is not None:
        print(
            f"Recolector (última): {r.enlaces} enlaces, cobertura {_pct(r.cobertura)}, "
            f"{r.decisiones_ia} fecha(s) decididas por IA, {r.en_revision} en revisión, "
            f"{r.descargados} descargados, {r.errores_descarga} errores"
        )
    return 0


def _evaluar_modelo(args: argparse.Namespace) -> int:
    import os

    import psycopg

    from prediccion_ganadera.evaluacion.modelo import evaluar_modelos
    from prediccion_ganadera.modelado.modelador import leer_precios

    try:
        config = cargar_configuracion(args.config)
        dsn = args.dsn or os.environ.get(VARIABLE_DSN)
        if args.seguimiento and args.fuente == "staging" and not dsn:
            raise ErrorConfiguracion(
                f"la fuente staging requiere Postgres: define {VARIABLE_DSN} o usa --dsn"
            )
    except ErrorConfiguracion as error:
        print(error, file=sys.stderr)
        return 2
    observados = None
    if args.seguimiento:
        try:
            observados = leer_precios(args.fuente, datos=config.rutas.datos, dsn=dsn).precios
        except psycopg.Error as error:
            print(f"no se pudo leer staging en Postgres: {error}", file=sys.stderr)
            return 1
    evaluacion = evaluar_modelos(config.rutas.datos, observados=observados)
    if not evaluacion.versiones:
        print("no hay modelos registrados con metadatos", file=sys.stderr)
        return 1
    if args.json:
        print(evaluacion.model_dump_json(indent=2))
        return 0
    for v in evaluacion.versiones:
        cambio = "" if v.cambio_mae is None else f" (cambio {v.cambio_mae:+,.0f} vs anterior)"
        print(
            f"{v.version}: {v.fechas_historico} subastas {v.fecha_desde}..{v.fecha_hasta}; "
            f"MAE {v.mae:,.0f} $/kg vs ingenuo {v.mae_ingenuo:,.0f}{cambio}; "
            f"mejora {_pct(v.mejora)}; MAPE {_pct(v.mape)}; confianza {v.confianza}"
        )
        for h, mae in v.mae_por_horizonte.items():
            print(f"  horizonte {h}: MAE {mae:,.0f}, mejora {_pct(v.mejora_por_horizonte[h])}")
        if (s := v.seguimiento) is not None:
            if s.comparadas:
                print(
                    f"  seguimiento: {s.comparadas}/{s.predicciones} predicciones ya observadas; "
                    f"MAE real {s.mae:,.0f} vs ingenuo {s.mae_ingenuo:,.0f}, mejora "
                    f"{_pct(s.mejora)}, dentro del error esperado {_pct(s.dentro_del_error)}"
                )
            else:
                print(f"  seguimiento: 0/{s.predicciones} predicciones con precio observado aún")
    for version in evaluacion.sin_metadatos:
        print(f"  {version}: en el índice pero sin carpeta (no se evaluó)")
    return 0


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

    extraer = sub.add_parser("extraer", help="extrae a plata (Delta Lake) los PDFs de bronce")
    extraer.add_argument("--config", default=CONFIG_POR_DEFECTO)
    extraer.add_argument("--json", action="store_true", help="resumen en JSON")
    extraer.set_defaults(func=_extraer)

    verificar = sub.add_parser(
        "verificar", help="verifica las tablas pendientes de plata contra la imagen del PDF"
    )
    verificar.add_argument("--config", default=CONFIG_POR_DEFECTO)
    verificar.add_argument(
        "--sin-ia", action="store_true", help="solo comprobaciones deterministas"
    )
    verificar.add_argument("--limite", type=int, help="máximo de tablas en esta corrida")
    verificar.add_argument("--json", action="store_true", help="trayectorias en JSON")
    verificar.set_defaults(func=_verificar)

    cargar = sub.add_parser(
        "cargar", help="carga plata en Postgres (raw; staging solo con tablas validadas)"
    )
    cargar.add_argument("--config", default=CONFIG_POR_DEFECTO)
    cargar.add_argument("--dsn", help=f"conexión a Postgres; por defecto ${VARIABLE_DSN}")
    cargar.add_argument("--json", action="store_true", help="resumen en JSON")
    cargar.set_defaults(func=_cargar)

    entrenar = sub.add_parser(
        "entrenar",
        help="entrena el modelo XGBoost, lo registra y guarda las predicciones en oro",
    )
    entrenar.add_argument("--config", default=CONFIG_POR_DEFECTO)
    entrenar.add_argument(
        "--fuente", choices=["plata", "staging"], default="plata", help="de dónde leer precios"
    )
    entrenar.add_argument("--dsn", help=f"conexión a Postgres; por defecto ${VARIABLE_DSN}")
    entrenar.add_argument(
        "--incluir-no-verificadas",
        action="store_true",
        help="usa también tablas pendientes de verificación (la confianza lo advierte)",
    )
    entrenar.add_argument("--json", action="store_true", help="resumen en JSON")
    entrenar.set_defaults(func=_entrenar)

    reportar = sub.add_parser(
        "reportar",
        help="publica en PDF el resumen del último modelo (o de --version) en reportes/",
    )
    reportar.add_argument("--config", default=CONFIG_POR_DEFECTO)
    reportar.add_argument("--version", help="versión del modelo; por defecto la más reciente")
    reportar.add_argument(
        "--sin-ia", action="store_true", help="narrativa determinista, sin backend de IA"
    )
    reportar.add_argument("--json", action="store_true", help="resumen en JSON")
    reportar.set_defaults(func=_reportar)

    federado = sub.add_parser(
        "comparar-federado",
        help="compara el XGBoost federado simulado (Flower) con el central; no escribe nada",
    )
    federado.add_argument("--config", default=CONFIG_POR_DEFECTO)
    federado.add_argument(
        "--reparto",
        choices=["todos", "anio", "seccion", "tipo_subasta"],
        default="todos",
        help="cómo se reparten las filas entre clientes simulados",
    )
    federado.add_argument(
        "--fuente", choices=["plata", "staging"], default="plata", help="de dónde leer precios"
    )
    federado.add_argument("--dsn", help=f"conexión a Postgres; por defecto ${VARIABLE_DSN}")
    federado.add_argument(
        "--incluir-no-verificadas",
        action="store_true",
        help="usa también tablas pendientes de verificación",
    )
    federado.add_argument("--rondas", type=int, default=60, help="rondas federadas")
    federado.add_argument("--json", action="store_true", help="comparaciones en JSON")
    federado.set_defaults(func=_comparar_federado)

    evaluar = sub.add_parser(
        "evaluar", help="métricas de extracción, de trayectorias de los agentes y del modelo"
    )
    tipos = evaluar.add_subparsers(dest="evaluacion", required=True)
    extraccion = tipos.add_parser(
        "extraccion", help="exactitud de extracción contra las muestras revisadas a mano"
    )
    extraccion.add_argument("--config", default=CONFIG_POR_DEFECTO)
    extraccion.add_argument(
        "--muestras", type=Path, default=Path("evaluacion/muestras"), help="carpeta de *.yaml"
    )
    extraccion.add_argument(
        "--fuente",
        choices=["extractor", "plata"],
        default="extractor",
        help="extractor: re-extrae el PDF; plata: tablas tras el verificador",
    )
    extraccion.add_argument("--json", action="store_true", help="métricas en JSON")
    extraccion.set_defaults(func=_evaluar_extraccion)

    trayectorias = tipos.add_parser(
        "trayectorias", help="métricas de las corridas a partir del registro de corridas"
    )
    trayectorias.add_argument("--config", default=CONFIG_POR_DEFECTO)
    trayectorias.add_argument(
        "--fuente",
        choices=["jsonl", "postgres"],
        default="jsonl",
        help="jsonl: data/corridas/corridas.jsonl (con historial); postgres: operacion.runs",
    )
    trayectorias.add_argument("--dsn", help=f"conexión a Postgres; por defecto ${VARIABLE_DSN}")
    trayectorias.add_argument("--json", action="store_true", help="métricas en JSON")
    trayectorias.set_defaults(func=_evaluar_trayectorias)

    modelo = tipos.add_parser("modelo", help="métricas de cada versión registrada del modelo")
    modelo.add_argument("--config", default=CONFIG_POR_DEFECTO)
    modelo.add_argument(
        "--seguimiento",
        action="store_true",
        help="compara las predicciones guardadas con los precios observados después",
    )
    modelo.add_argument(
        "--fuente", choices=["plata", "staging"], default="plata", help="precios observados"
    )
    modelo.add_argument("--dsn", help=f"conexión a Postgres; por defecto ${VARIABLE_DSN}")
    modelo.add_argument("--json", action="store_true", help="métricas en JSON")
    modelo.set_defaults(func=_evaluar_modelo)

    run = sub.add_parser(
        "run",
        help="corrida completa: recolectar, extraer, verificar, cargar, modelar y reportar "
        "(reanudable)",
    )
    run.add_argument("--config", default=CONFIG_POR_DEFECTO)
    identidad = run.add_mutually_exclusive_group()
    identidad.add_argument("--id", help="identificador de la corrida; por defecto la hora UTC")
    identidad.add_argument("--reanudar", metavar="ID", help="continúa una corrida interrumpida")
    run.add_argument("--html", type=Path, help="usa una copia local de la página")
    run.add_argument("--solo-listar", action="store_true", help="no descarga PDFs")
    run.add_argument("--sin-ia", action="store_true", help="sin backend de IA en ningún agente")
    run.add_argument("--desde", type=date.fromisoformat, help="AAAA-MM-DD")
    run.add_argument("--hasta", type=date.fromisoformat, help="AAAA-MM-DD")
    run.add_argument("--dsn", help=f"conexión a Postgres; por defecto ${VARIABLE_DSN}")
    run.add_argument("--json", action="store_true", help="estado final de la corrida en JSON")
    run.set_defaults(func=_run)
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
