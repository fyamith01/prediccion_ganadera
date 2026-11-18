-- Esquema de la base de datos local (Postgres 16). Idempotente: se ejecuta en cada carga.
-- raw:     copia fiel de la capa plata, todas las tablas sin importar su verificación.
-- staging: solo las tablas aceptadas por el verificador (verificada o corregida).
-- marts:   vistas de consumo construidas sobre raw/staging.
-- Diccionario de datos: docs/diccionario_datos.md

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS marts;

CREATE TABLE IF NOT EXISTS raw.tablas (
    sha256              text        NOT NULL CHECK (sha256 ~ '^[0-9a-f]+$'),
    pagina              integer     NOT NULL CHECK (pagina >= 1),
    tabla               integer     NOT NULL CHECK (tabla >= 1),
    fecha               date,
    especial            boolean     NOT NULL,
    estado_verificacion text        NOT NULL
        CHECK (estado_verificacion IN ('pendiente', 'verificada', 'corregida', 'en_revision')),
    ajustes             jsonb       NOT NULL,
    filas               integer     NOT NULL,
    columnas            integer     NOT NULL,
    huella              text        NOT NULL,
    extraido_en         timestamptz NOT NULL,
    cargado_en          timestamptz NOT NULL,
    PRIMARY KEY (sha256, pagina, tabla)
);
COMMENT ON TABLE raw.tablas IS 'Una fila por tabla extraída de un PDF (capa plata).';

CREATE TABLE IF NOT EXISTS raw.celdas (
    sha256  text    NOT NULL,
    pagina  integer NOT NULL,
    tabla   integer NOT NULL,
    fila    integer NOT NULL,
    columna integer NOT NULL,
    valor   text,
    PRIMARY KEY (sha256, pagina, tabla, fila, columna),
    FOREIGN KEY (sha256, pagina, tabla) REFERENCES raw.tablas ON DELETE CASCADE
);
COMMENT ON TABLE raw.celdas IS 'Celdas de raw.tablas en formato largo (fila, columna, valor).';

CREATE TABLE IF NOT EXISTS staging.tablas (
    sha256              text        NOT NULL,
    pagina              integer     NOT NULL,
    tabla               integer     NOT NULL,
    fecha               date,
    especial            boolean     NOT NULL,
    estado_verificacion text        NOT NULL
        CHECK (estado_verificacion IN ('verificada', 'corregida')),
    filas               integer     NOT NULL,
    columnas            integer     NOT NULL,
    huella              text        NOT NULL,
    cargado_en          timestamptz NOT NULL,
    PRIMARY KEY (sha256, pagina, tabla),
    FOREIGN KEY (sha256, pagina, tabla) REFERENCES raw.tablas ON DELETE CASCADE
);
COMMENT ON TABLE staging.tablas IS 'Tablas aceptadas por el verificador visual.';

CREATE TABLE IF NOT EXISTS staging.celdas (
    sha256  text    NOT NULL,
    pagina  integer NOT NULL,
    tabla   integer NOT NULL,
    fila    integer NOT NULL,
    columna integer NOT NULL,
    valor   text,
    PRIMARY KEY (sha256, pagina, tabla, fila, columna),
    FOREIGN KEY (sha256, pagina, tabla) REFERENCES staging.tablas ON DELETE CASCADE
);
COMMENT ON TABLE staging.celdas IS 'Celdas de staging.tablas en formato largo.';

CREATE OR REPLACE VIEW marts.cobertura_subastas AS
SELECT
    sha256,
    fecha,
    especial,
    count(*)::integer AS tablas,
    count(*) FILTER (WHERE estado_verificacion IN ('verificada', 'corregida'))::integer
        AS tablas_validadas,
    count(*) FILTER (WHERE estado_verificacion = 'pendiente')::integer AS tablas_pendientes,
    count(*) FILTER (WHERE estado_verificacion = 'en_revision')::integer AS tablas_en_revision,
    coalesce(sum(filas) FILTER (WHERE estado_verificacion IN ('verificada', 'corregida')), 0)
        ::integer AS filas_validadas,
    max(cargado_en) AS cargado_en
FROM raw.tablas
GROUP BY sha256, fecha, especial;
COMMENT ON VIEW marts.cobertura_subastas IS 'Estado de verificación por subasta (PDF).';
