"""
Pipeline: ingesta por email -> normalización determinista -> reporte de
ventas -> email de salida redactado por IA.

Instalación, variables de entorno y modo de uso: ver README.md.

Dos capas, cada una con la herramienta que le corresponde:
  1) Determinista (sin LLM): limpieza y reconciliación de columnas. Mismo
     archivo de entrada -> mismo resultado, siempre.
  2) Asistida por IA: redacción del cuerpo de los emails. Ahí no hace falta
     determinismo, hace falta que se lea bien.

Siempre se manda el reporte de ventas a EMAIL_DESTINATARIO, aclarando qué
porcentaje de las transacciones lo respalda. Si quedaron filas sin poder
normalizar, se manda un segundo email a EMAIL_SOPORTE con esas filas
adjuntas para que las completen en el sistema de origen.
"""

import os
import logging
import smtplib
import imaplib
import email as email_lib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from google import genai
from google.genai import types as genai_types

# El SDK de Gemini avisa por logging, la primera vez que se llama a
# generate_content, que existe un modo alternativo (Chat.send_message) para
# function calling. No usamos function calling acá, así que no aplica.
logging.getLogger('google_genai.models').setLevel(logging.ERROR)

try:
    from dotenv import load_dotenv
    load_dotenv()  # si existe un .env en la carpeta, carga esas variables al entorno
except ImportError:
    pass  # python-dotenv es opcional; si no está instalado, se usan las variables ya exportadas

# ---------------------------------------------------------------------------
# 0) CONFIGURACIÓN
# ---------------------------------------------------------------------------

CARPETA_SALIDA = '/tmp/pipeline_salida'

IMAP_HOST = os.environ.get('IMAP_HOST')
IMAP_USER = os.environ.get('IMAP_USER')
IMAP_PASSWORD = os.environ.get('IMAP_PASSWORD')
IMAP_ASUNTO_FILTRO = os.environ.get('IMAP_ASUNTO_FILTRO', 'Reporte diario')

SMTP_HOST = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '465'))
EMAIL_USER = os.environ.get('EMAIL_USER')
EMAIL_PASSWORD = os.environ.get('EMAIL_PASSWORD')
EMAIL_DESTINATARIO = os.environ.get('EMAIL_DESTINATARIO')  # reporte de negocio
EMAIL_SOPORTE = os.environ.get('EMAIL_SOPORTE')             # cola de revisión manual

MODELO_LLM = os.environ.get('GEMINI_MODEL', 'gemini-2.5-flash')
MAX_OUTPUT_TOKENS = int(os.environ.get('GEMINI_MAX_OUTPUT_TOKENS', '800'))


# ---------------------------------------------------------------------------
# 1) NORMALIZACIÓN DETERMINISTA (idéntica a normalizar_dataset.py)
# ---------------------------------------------------------------------------

CONFIG_COLUMNAS = {
    'ID Transacción':        {'tipo': 'str',      'estrategia': 'ninguna'},
    'ID Cliente':             {'tipo': 'str',      'estrategia': 'ninguna'},
    'Categoría':              {'tipo': 'str',      'estrategia': 'moda'},
    'Método de Pago':         {'tipo': 'str',      'estrategia': 'moda'},
    'Ubicación':              {'tipo': 'str',      'estrategia': 'moda'},
    'Artículo':               {'tipo': 'str',      'estrategia': 'regla_especial'},
    'Precio Unitario':        {'tipo': 'float',    'estrategia': 'regla_especial'},
    'Cantidad':               {'tipo': 'float',    'estrategia': 'regla_especial'},
    'Total Gastado':          {'tipo': 'float',    'estrategia': 'regla_especial'},
    'Fecha de Transacción':   {'tipo': 'datetime', 'estrategia': 'regla_especial'},
    'Descuento Aplicado':     {'tipo': 'bool',     'estrategia': 'categoria_ausente'},
}


def reconstruir_precio_cantidad_total(fila):
    """Si falta un solo valor de (precio, cantidad, total) se lo reconstruye
    a partir de los otros dos. Si faltan 2 o más, no es reconciliable."""
    precio, cantidad, total = fila['Precio Unitario'], fila['Cantidad'], fila['Total Gastado']
    faltantes = sum(pd.isna(valor) for valor in (precio, cantidad, total))

    if faltantes == 0:
        return precio, cantidad, total, False
    if faltantes > 1:
        return precio, cantidad, total, True

    if pd.isna(precio):
        precio = round(total / cantidad, 2)
    elif pd.isna(cantidad):
        cantidad = round(total / precio)
    else:
        total = round(precio * cantidad, 2)
    return precio, cantidad, total, False


FORMATOS_FECHA = ['%Y-%m-%d', '%Y/%m/%d', '%d-%m-%Y', '%m/%d/%Y']
TOKENS_FECHA_INVALIDA = {'', 'N/A', 'S/D', 'NULL', 'NONE', '00/00/0000'}
MESES_ES = {
    'enero': 1, 'febrero': 2, 'marzo': 3, 'abril': 4, 'mayo': 5, 'junio': 6,
    'julio': 7, 'agosto': 8, 'septiembre': 9, 'setiembre': 9, 'octubre': 10,
    'noviembre': 11, 'diciembre': 12,
}


def _parsear_fecha_verbosa_es(texto):
    partes = texto.lower().replace(',', '').split()
    if len(partes) != 5 or partes[1] != 'de' or partes[3] != 'de':
        return None
    if partes[2] not in MESES_ES or not partes[0].isdigit() or not partes[4].isdigit():
        return None
    try:
        return pd.Timestamp(year=int(partes[4]), month=MESES_ES[partes[2]], day=int(partes[0]))
    except ValueError:
        return None


def normalizar_fecha(valor):
    if pd.isna(valor):
        return pd.NaT, True
    texto = str(valor).strip()
    if texto.upper() in TOKENS_FECHA_INVALIDA:
        return pd.NaT, True
    fecha_verbosa = _parsear_fecha_verbosa_es(texto)
    if fecha_verbosa is not None:
        return fecha_verbosa, False
    for fmt in FORMATOS_FECHA:
        try:
            return pd.to_datetime(texto, format=fmt, errors='raise'), False
        except (ValueError, TypeError):
            continue
    return pd.NaT, True


def normalizar_articulo(fila):
    articulo = fila['Artículo']
    if pd.isna(articulo):
        categoria = fila['Categoría'] if pd.notna(fila['Categoría']) else 'SIN_CATEGORIA'
        return f"SIN_ESPECIFICAR_{categoria}", True
    return articulo, False


def aplicar_estrategia_generica(serie, estrategia):
    if estrategia == 'ninguna':
        return serie
    if estrategia == 'moda':
        if serie.isna().any():
            moda = serie.mode(dropna=True)
            valor_relleno = moda.iloc[0] if not moda.empty else 'DESCONOCIDO'
            return serie.fillna(valor_relleno)
        return serie
    if estrategia == 'categoria_ausente':
        return serie.astype('boolean')
    raise ValueError(f"Estrategia genérica desconocida: {estrategia}")


def normalizar_dataset(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    for columna, config in CONFIG_COLUMNAS.items():
        if config['estrategia'] in ('moda', 'categoria_ausente', 'ninguna'):
            df[columna] = aplicar_estrategia_generica(df[columna], config['estrategia'])

    resultados = df.apply(reconstruir_precio_cantidad_total, axis=1, result_type='expand')
    df['Precio Unitario'], df['Cantidad'], df['Total Gastado'], df['monto_irreconciliable'] = (
        resultados[0], resultados[1], resultados[2], resultados[3]
    )

    resultados_item = df.apply(normalizar_articulo, axis=1, result_type='expand')
    df['Artículo'], df['articulo_sin_especificar'] = resultados_item[0], resultados_item[1]

    resultados_fecha = df['Fecha de Transacción'].apply(normalizar_fecha)
    df['Fecha de Transacción'] = resultados_fecha.apply(lambda t: t[0])
    df['fecha_invalida'] = resultados_fecha.apply(lambda t: t[1])

    df['requiere_revision_manual'] = df['monto_irreconciliable'] | df['fecha_invalida']
    return df


def generar_reporte_final(df_normalizado: pd.DataFrame):
    """Agrega ventas por categoría usando solo las filas con monto confiable
    (las `monto_irreconciliable` quedan afuera porque el número sería
    inventado). Devuelve también qué % del dataset respalda ese número, para
    que el email nunca lo muestre como si fuera el 100% cuando no lo es."""
    confiables = df_normalizado[~df_normalizado['monto_irreconciliable']]
    resumen_categoria = (
        confiables.groupby('Categoría')['Total Gastado']
        .agg(ventas_totales='sum', ticket_promedio='mean', transacciones='count')
        .round(2)
        .sort_values('ventas_totales', ascending=False)
    )
    filas_totales = len(df_normalizado)
    filas_usadas = len(confiables)
    pct_usado = (filas_usadas / filas_totales * 100) if filas_totales else 0.0
    return resumen_categoria, pct_usado, filas_usadas, filas_totales


def generar_grafico_reporte(resumen_categoria: pd.DataFrame, ruta_salida: str) -> None:
    """Arma un PNG con 3 paneles (uno por métrica de resumen_categoria) para
    dar un vistazo tipo dashboard del reporte. Es matplotlib puro sobre datos
    ya calculados -> misma capa determinista, sin LLM de por medio."""
    metricas = [
        ('ventas_totales', 'Ventas totales', 'Monto ($)'),
        ('ticket_promedio', 'Ticket promedio', 'Monto ($)'),
        ('transacciones', 'Transacciones', 'Cantidad'),
    ]
    fig, ejes = plt.subplots(1, 3, figsize=(15, 5))
    for eje, (columna, titulo, etiqueta_y) in zip(ejes, metricas):
        resumen_categoria[columna].plot(kind='bar', ax=eje, color='#4C72B0')
        eje.set_title(titulo)
        eje.set_xlabel('Categoría')
        eje.set_ylabel(etiqueta_y)
        eje.tick_params(axis='x', rotation=45)
    fig.suptitle('Reporte de ventas por categoría')
    fig.tight_layout()
    fig.savefig(ruta_salida, dpi=150)
    plt.close(fig)


def generar_cola_revision_manual(df_normalizado: pd.DataFrame) -> pd.DataFrame:
    columnas = ['ID Transacción', 'ID Cliente', 'Categoría', 'Artículo',
                'Precio Unitario', 'Cantidad', 'Total Gastado', 'Método de Pago',
                'Ubicación', 'Fecha de Transacción', 'monto_irreconciliable', 'fecha_invalida']
    return df_normalizado.loc[df_normalizado['requiere_revision_manual'], columnas].copy()


# ---------------------------------------------------------------------------
# 2) INGESTA POR EMAIL
# ---------------------------------------------------------------------------

def ingesta_email(carpeta_destino: str = CARPETA_SALIDA):
    """Busca el email no leído más reciente que matchee IMAP_ASUNTO_FILTRO,
    descarga su adjunto .csv y devuelve la ruta local. Devuelve None si no
    hay nada nuevo para procesar."""
    os.makedirs(carpeta_destino, exist_ok=True)
    with imaplib.IMAP4_SSL(IMAP_HOST) as imap:
        imap.login(IMAP_USER, IMAP_PASSWORD)
        imap.select('INBOX')
        criterio = f'(UNSEEN SUBJECT "{IMAP_ASUNTO_FILTRO}")' if IMAP_ASUNTO_FILTRO else 'UNSEEN'
        _, datos = imap.search(None, criterio)
        ids = datos[0].split()
        if not ids:
            return None

        ultimo_id = ids[-1]
        _, msg_datos = imap.fetch(ultimo_id, '(RFC822)')
        mensaje = email_lib.message_from_bytes(msg_datos[0][1])

        ruta_csv = None
        for parte in mensaje.walk():
            nombre = parte.get_filename()
            if nombre and nombre.lower().endswith('.csv'):
                ruta_csv = os.path.join(carpeta_destino, nombre)
                with open(ruta_csv, 'wb') as f:
                    f.write(parte.get_payload(decode=True))
                break

        if ruta_csv is None:
            raise RuntimeError('El email encontrado no tiene ningún adjunto .csv.')

        imap.store(ultimo_id, '+FLAGS', '\\Seen')

    return ruta_csv


# ---------------------------------------------------------------------------
# 3) REDACCIÓN DEL EMAIL CON GEMINI (acá sí se usa IA, a propósito)
# ---------------------------------------------------------------------------

def _pedir_texto_a_llm(prompt_sistema: str, prompt_usuario: str) -> str:
    cliente = genai.Client(api_key=os.environ['GEMINI_API_KEY'])
    respuesta = cliente.models.generate_content(
        model=MODELO_LLM,
        contents=prompt_usuario,
        config=genai_types.GenerateContentConfig(
            system_instruction=prompt_sistema,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            # Sin esto, gemini-2.5-flash gasta el presupuesto de tokens en
            # "pensar" antes de escribir y la respuesta sale cortada a mitad
            # de frase. Acá solo redacta texto, no necesita razonar.
            thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
        ),
    )
    return respuesta.text


def redactar_email_reporte(resumen_categoria: pd.DataFrame, pct_usado: float,
                            filas_usadas: int, filas_totales: int) -> str:
    tabla = resumen_categoria.reset_index().to_string(index=False)
    prompt_sistema = (
        'Sos un asistente que redacta emails breves y profesionales en español '
        'para un equipo de negocio, resumiendo un reporte de ventas. Destacá 2 o 3 '
        'datos relevantes, no repitas la tabla completa en prosa, y cerrá indicando '
        'que el detalle completo va adjunto. Es un email automático: no lo firmes con '
        'un nombre de persona ni dejes placeholders como "[Tu Nombre]".'
    )
    if pct_usado >= 100:
        aclaracion = (
            'El reporte se generó con el 100% de las transacciones del período: no '
            'quedó ninguna pendiente de revisión.'
        )
    else:
        aclaracion = (
            f'Importante: este reporte se generó usando solo el {pct_usado:.0f}% de las '
            f'transacciones ({filas_usadas} de {filas_totales}). Las restantes no se pudieron '
            'validar automáticamente y se derivaron a soporte por separado para su revisión, '
            'así que el número de este reporte podría subir una vez que se completen.'
        )
    prompt_usuario = (
        'Redactá el cuerpo de un email (sin asunto) que acompañe el reporte de '
        'ventas por categoría adjunto. Datos agregados:\n\n' + tabla +
        '\n\nIncluí, en tus propias palabras, esta aclaración sobre la cobertura del dato '
        'de forma clara y visible (no la copies literal, pero no omitas la cifra): ' + aclaracion
    )
    return _pedir_texto_a_llm(prompt_sistema, prompt_usuario)


def redactar_email_pendientes(cola_revision: pd.DataFrame) -> str:
    n_monto_irreconciliable = int(cola_revision['monto_irreconciliable'].sum())
    n_fecha_invalida = int(cola_revision['fecha_invalida'].sum())
    prompt_sistema = (
        'Sos un asistente que redacta emails breves y profesionales en español, '
        'avisando que un proceso automático de limpieza de datos no pudo completarse '
        'al 100% y que se necesita revisión humana. Tono claro y orientado a la acción, '
        'sin tecnicismos de código, sin culpar a nadie. No listes transacciones ni IDs '
        'individuales: el detalle fila por fila ya va en el archivo adjunto, alcanza con '
        'resumir qué tipos de problema hay y cuántos casos de cada uno. Es un email '
        'automático: no lo firmes con un nombre de persona ni dejes placeholders como '
        '"[Tu Nombre]".'
    )
    prompt_usuario = (
        f'Redactá el cuerpo de un email (sin asunto) avisando que {len(cola_revision)} '
        'transacciones no se pudieron normalizar automáticamente y quedaron marcadas '
        'para revisión manual dentro del archivo adjunto (que por eso queda incompleto). '
        f'Motivos: {n_monto_irreconciliable} con monto irreconciliable (faltan Cantidad y '
        f'Total Gastado a la vez) y {n_fecha_invalida} con fecha inválida. '
        'Pedí que se completen esos datos en el sistema de origen.'
    )
    return _pedir_texto_a_llm(prompt_sistema, prompt_usuario)


# ---------------------------------------------------------------------------
# 4) ENVÍO DEL EMAIL DE SALIDA
# ---------------------------------------------------------------------------

def enviar_email(destinatario: str, asunto: str, cuerpo: str, adjuntos: list) -> None:
    mensaje = MIMEMultipart()
    mensaje['From'] = EMAIL_USER
    mensaje['To'] = destinatario
    mensaje['Subject'] = asunto
    mensaje.attach(MIMEText(cuerpo, 'plain'))

    for ruta in adjuntos:
        with open(ruta, 'rb') as f:
            parte = MIMEBase('application', 'octet-stream')
            parte.set_payload(f.read())
        encoders.encode_base64(parte)
        parte.add_header('Content-Disposition', f'attachment; filename={os.path.basename(ruta)}')
        mensaje.attach(parte)

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.login(EMAIL_USER, EMAIL_PASSWORD)
        smtp.sendmail(EMAIL_USER, destinatario, mensaje.as_string())


# ---------------------------------------------------------------------------
# 5) ORQUESTACIÓN DEL PIPELINE COMPLETO
# ---------------------------------------------------------------------------

def ejecutar_pipeline() -> None:
    os.makedirs(CARPETA_SALIDA, exist_ok=True)

    print('1) Ingesta del archivo por email...')
    ruta_entrada = ingesta_email()
    if ruta_entrada is None:
        print('   No hay ningún email nuevo con un dataset para procesar. Fin de la corrida.')
        return

    df_crudo = pd.read_csv(ruta_entrada)
    print(f'   Archivo recibido: {ruta_entrada} ({len(df_crudo)} filas)')

    print('2) Normalización determinista (reglas fijas, sin LLM)...')
    df_normalizado = normalizar_dataset(df_crudo)
    ruta_normalizado = os.path.join(CARPETA_SALIDA, 'dataset_normalizado.csv')
    df_normalizado.to_csv(ruta_normalizado, index=False)

    cola_revision = generar_cola_revision_manual(df_normalizado)
    pendientes = len(cola_revision)
    total = len(df_normalizado)
    print(f'   {total - pendientes}/{total} filas resueltas automáticamente, {pendientes} pendientes.')

    # -- Email 1: reporte de negocio. SIEMPRE se manda. ------------------------
    print('3) Generando el reporte de ventas (con la cobertura de datos que haya)...')
    resumen_categoria, pct_usado, filas_usadas, filas_totales = generar_reporte_final(df_normalizado)
    ruta_reporte = os.path.join(CARPETA_SALIDA, 'reporte_ventas_por_categoria.csv')
    resumen_categoria.to_csv(ruta_reporte)
    ruta_grafico = os.path.join(CARPETA_SALIDA, 'reporte_ventas_por_categoria.png')
    generar_grafico_reporte(resumen_categoria, ruta_grafico)
    print(f'   Reporte armado con el {pct_usado:.0f}% de las transacciones ({filas_usadas}/{filas_totales}).')

    cuerpo_reporte = redactar_email_reporte(resumen_categoria, pct_usado, filas_usadas, filas_totales)
    sufijo_asunto = '' if pct_usado >= 100 else f' (parcial, {pct_usado:.0f}%)'
    asunto_reporte = f'[AUTO] Reporte de ventas{sufijo_asunto} — {pd.Timestamp.now():%Y-%m-%d}'
    enviar_email(EMAIL_DESTINATARIO, asunto_reporte, cuerpo_reporte, [ruta_normalizado, ruta_reporte, ruta_grafico])

    # -- Email 2: cola de revisión para soporte. Solo si hay algo pendiente. ---
    if pendientes == 0:
        print('4) No quedaron transacciones pendientes -> no se envía email a soporte.')
    else:
        print(f'4) {pendientes} transacciones pendientes -> se envía el dataset a revisar a soporte.')
        ruta_cola = os.path.join(CARPETA_SALIDA, 'cola_revision_manual.csv')
        cola_revision.to_csv(ruta_cola, index=False)

        cuerpo_soporte = redactar_email_pendientes(cola_revision)
        asunto_soporte = f'[AUTO] {pendientes} transacciones para revisar — {pd.Timestamp.now():%Y-%m-%d}'
        enviar_email(EMAIL_SOPORTE, asunto_soporte, cuerpo_soporte, [ruta_cola])

    print('Pipeline finalizado.')


if __name__ == '__main__':
    ejecutar_pipeline()
