import os
import glob
import pdfplumber
import pandas as pd

def procesar_carpeta_pdfs(carpeta_entrada, carpeta_salida="tablas_base"):
    if not os.path.exists(carpeta_salida):
        os.makedirs(carpeta_salida)
        
    patron_busqueda = os.path.join(carpeta_entrada, "*.pdf")
    archivos_pdf = glob.glob(patron_busqueda)
    
    if not archivos_pdf:
        print(f"No se encontraron archivos PDF en la ruta: {carpeta_entrada}")
        return
        
    print(f"Se encontraron {len(archivos_pdf)} archivos PDF para procesar.\n")
    todas_las_tablas_df = []
    
    #AJUSTES CRÍTICOS: Forzar detección por espacios en blanco y texto alineado
    ajustes_tablas = {
        "vertical_strategy": "text",     # Divide columnas según alineación de texto
        "horizontal_strategy": "text",   # Divide filas según líneas de texto
        "snap_tolerance": 3,             # Tolerancia de cercanía para caracteres
        "join_tolerance": 3,
    }
    
    for ruta_pdf in archivos_pdf:
        nombre_pdf = os.path.basename(ruta_pdf)
        print(f"Procesando: {nombre_pdf}...")
        
        with pdfplumber.open(ruta_pdf) as pdf:
            tabla_contador = 1
            
            for num_pagina, pagina in enumerate(pdf.pages, start=1):
                # Se pasan los ajustes optimizados para tus tablas sin rejilla completa
                tablas = pagina.extract_tables(table_settings=ajustes_tablas)
                
                for tabla in tablas:
                    df_actual = pd.DataFrame(tabla)
                    
                    if df_actual.empty or df_actual.dropna(how='all').empty:
                        continue
                    
                    # Limpieza básica: quitar filas completamente vacías creadas por los saltos
                    df_actual = df_actual.dropna(how='all').reset_index(drop=True)
                    
                    # Configurar fila 0 como encabezado
                    df_actual.columns = df_actual.iloc[0]
                    df_actual = df_actual[1:].reset_index(drop=True)
                    
                    # Normalizar nombres de columnas
                    df_actual.columns = [str(c).strip() if c is not None else f"Col_{i}" for i, c in enumerate(df_actual.columns)]
                    
                    # Metadatos para no perder el origen del lote
                    df_actual['Origen_PDF'] = nombre_pdf
                    df_actual['Numero_Pagina'] = num_pagina
                    df_actual['ID_Tabla'] = f"Tabla_{tabla_contador}"
                    
                    todas_las_tablas_df.append(df_actual)
                    
                    nombre_csv = f"{os.path.splitext(nombre_pdf)[0]}_pag{num_pagina}_t{tabla_contador}.csv"
                    ruta_guardado_csv = os.path.join(carpeta_salida, nombre_csv)
                    df_actual.to_csv(ruta_guardado_csv, index=False, encoding='utf-8-sig')
                    
                    tabla_contador += 1
                    
        print(f"Finalizado: {nombre_pdf} ({tabla_contador - 1} tablas extraídas).\n")
        
    if todas_las_tablas_df:
        print("Generando archivo consolidado maestro...")
        ruta_excel_maestro = os.path.join(carpeta_salida, "TODAS_LAS_TABLAS_CONSOLIDADAS.xlsx")
        with pd.ExcelWriter(ruta_excel_maestro, engine='openpyxl') as writer:
            for idx, df in enumerate(todas_las_tablas_df, start=1):
                nombre_pestana = f"T{idx}_P{df['Numero_Pagina'].iloc[0]}"
                df.to_excel(writer, sheet_name=nombre_pestana[:31], index=False)
                
        print(f"Proceso completado. Archivos en '{carpeta_salida}'.")
        return todas_las_tablas_df
    else:
        print("No se pudo extraer ninguna tabla con los nuevos ajustes.")
        return []

# Configuración de tu entorno de ejecución
CARPETA_DE_MIS_PDFS = "data/datos_pagina" 
lista_dataframes = procesar_carpeta_pdfs(CARPETA_DE_MIS_PDFS)
