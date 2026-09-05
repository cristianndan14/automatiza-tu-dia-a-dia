# Prompt de ejemplo: recrear el pipeline de normalización y su cron en GitHub Actions

Prompt autocontenido para que cualquier agente recree desde cero [pipeline_email_normalizacion.py](pipeline_email_normalizacion.py) y el workflow de GitHub Actions que lo crronea.

---

## Prompt

Necesito un script en Python (`pipeline_email_normalizacion.py`) que implemente un pipeline de negocio con esta arquitectura de dos capas: una capa determinista (sin LLM) para limpieza de datos, y una capa asistida por IA solo para redactar texto. El flujo completo es:

### 1. Ingesta por email (IMAP)
- Conectarse a un servidor IMAP (`IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD` desde variables de entorno) usando `imaplib`.
- Buscar el email no leído más reciente cuyo asunto matchee `IMAP_ASUNTO_FILTRO` (default `"Reporte diario"`); si no hay ninguno, devolver `None` y terminar el pipeline sin error.
- Descargar el primer adjunto `.csv` del email a una carpeta local (`/tmp/pipeline_salida`). Si el email no tiene adjunto `.csv`, lanzar un error.
- Marcar el email como leído.

### 2. Normalización determinista de un dataset de ventas
El CSV tiene columnas: `ID Transacción`, `ID Cliente`, `Categoría`, `Método de Pago`, `Ubicación`, `Artículo`, `Precio Unitario`, `Cantidad`, `Total Gastado`, `Fecha de Transacción`, `Descuento Aplicado`. Reglas de limpieza, deterministas (mismo input → mismo output siempre):

- `Categoría`, `Método de Pago`, `Ubicación`: rellenar nulos con la moda de la columna (o `'DESCONOCIDO'` si no hay moda).
- `Descuento Aplicado`: convertir a tipo `boolean` de pandas (nullable).
- `Precio Unitario` / `Cantidad` / `Total Gastado`: son 3 valores relacionados por `Total = Precio * Cantidad`. Si falta exactamente uno de los tres, reconstruirlo a partir de los otros dos (redondeando: precio y total a 2 decimales, cantidad a entero). Si faltan 2 o más, marcar la fila como `monto_irreconciliable = True` y dejar los valores como están.
- `Artículo`: si está vacío, reemplazar por `f"SIN_ESPECIFICAR_{categoria}"` (usando la categoría de esa fila, o `'SIN_CATEGORIA'` si también falta) y marcar `articulo_sin_especificar = True`.
- `Fecha de Transacción`: parsear con múltiples formatos posibles (`%Y-%m-%d`, `%Y/%m/%d`, `%d-%m-%Y`, `%m/%d/%Y`), más un formato verboso en español tipo `"15 de enero de 2024"` (con diccionario de meses en español, incluyendo `septiembre`/`setiembre`). Tratar como inválidos los tokens `''`, `'N/A'`, `'S/D'`, `'NULL'`, `'NONE'`, `'00/00/0000'` (case-insensitive). Si no matchea ningún formato, poner `NaT` y marcar `fecha_invalida = True`.
- Agregar columna `requiere_revision_manual = monto_irreconciliable OR fecha_invalida`.

### 3. Reporte de ventas
- Agrupar por `Categoría` (excluyendo filas con `monto_irreconciliable`, porque ese monto sería inventado) y calcular: `ventas_totales` (suma de `Total Gastado`), `ticket_promedio` (promedio), `transacciones` (conteo). Redondear a 2 decimales y ordenar por `ventas_totales` descendente.
- Calcular qué porcentaje de las filas totales respalda ese reporte (filas usadas / filas totales * 100).
- Generar un gráfico PNG con matplotlib: 3 subplots en fila (ventas totales, ticket promedio, transacciones), cada uno como bar chart por categoría, con rotación de 45° en las etiquetas del eje X.

### 4. Cola de revisión manual
- Si quedaron filas con `requiere_revision_manual = True`, armar un DataFrame solo con esas filas y las columnas relevantes (IDs, categoría, artículo, montos, fechas, método de pago, ubicación, y las 2 flags booleanas) para exportar a CSV.

### 5. Redacción de emails con IA (Gemini)
Usar el SDK `google-genai` (`from google import genai`), modelo configurable por env var `GEMINI_MODEL` (default `gemini-2.5-flash`), con `thinking_budget=0` (para que no gaste tokens "pensando" y corte la respuesta) y `max_output_tokens` configurable (default 800).

- Función genérica que recibe `system_instruction` + prompt de usuario y devuelve el texto generado.
- Email de reporte: pedirle a Gemini que redacte un cuerpo breve y profesional en español, destacando 2-3 datos del resumen por categoría, sin repetir la tabla completa, sin firma personal ni placeholders. Debe incluir de forma clara (en sus propias palabras, no copiada literal) una aclaración sobre qué % de las transacciones respalda el número — si es 100%, aclarar que no quedó nada pendiente; si es menor, aclarar la cifra y que el resto se derivó a soporte.
- Email de pendientes: redactar un aviso de que N transacciones no se pudieron normalizar automáticamente y quedaron para revisión manual en el adjunto, resumiendo cuántos casos son por "monto irreconciliable" y cuántos por "fecha inválida" (sin listar IDs individuales), pidiendo que se completen los datos en el sistema de origen.

### 6. Envío de emails (SMTP)
- Usar `smtplib.SMTP_SSL` (host/puerto configurables, default `smtp.gmail.com:465`) con `EMAIL_USER`/`EMAIL_PASSWORD`.
- Construir el mensaje con `email.mime` (multipart, texto plano + adjuntos codificados en base64).

### 7. Orquestación (`ejecutar_pipeline`)
1. Ingesta del email; si no hay nada nuevo, terminar.
2. Leer el CSV con pandas y normalizar.
3. Guardar el dataset normalizado completo a CSV.
4. Generar reporte + gráfico, redactar el email con IA, y enviarlo **siempre** a `EMAIL_DESTINATARIO` (asunto con `[AUTO]`, marcado como "(parcial, X%)" si no llegó al 100%, y la fecha del día), adjuntando el dataset normalizado, el CSV del reporte y el PNG.
5. Si hay filas pendientes de revisión, guardarlas en un CSV aparte, redactar el email de aviso con IA, y enviarlo a `EMAIL_SOPORTE` con ese CSV adjunto. Si no hay pendientes, no mandar este segundo email.
6. Imprimir mensajes de progreso por consola en cada paso.

**Detalles adicionales:**
- Soportar carga opcional de un archivo `.env` con `python-dotenv` (si no está instalado, seguir sin fallar).
- Silenciar el logger `google_genai.models` a nivel `ERROR` (el SDK emite un warning irrelevante sobre function calling que no usamos).
- Todas las variables sensibles (credenciales IMAP/SMTP, API key de Gemini, destinatarios) deben venir de variables de entorno, nunca hardcodeadas.
- Comentarios solo donde el motivo no sea obvio (ej. por qué se usa `thinking_budget=0`, por qué se excluyen las filas irreconciliables del reporte).

El resultado debe ser un único archivo Python ejecutable con `if __name__ == '__main__': ejecutar_pipeline()`.

### 8. Workflow de GitHub Actions para cronear el pipeline

Además del script, creá un workflow (`.github/workflows/pipeline.yml`) que ejecute `pipeline_email_normalizacion.py` de forma programada:

- **Antes de escribir el workflow, preguntame con qué frecuencia u horario querés que corra** (por ejemplo: cada N minutos/horas, una vez por día a determinada hora, días de semana vs. todos los días, etc.) y traducí esa respuesta a la expresión cron correspondiente (sintaxis `cron` de GitHub Actions, en UTC). No asumas un valor por defecto sin preguntar.
- Triggers: `schedule` con el cron acordado, más `workflow_dispatch: {}` para poder dispararlo manualmente.
- Job `ejecutar-pipeline` sobre `ubuntu-latest`, asociado al Environment `pipeline` (para que las credenciales se administren ahí como secrets/vars del Environment).
- Pasos: checkout del código (`actions/checkout@v4`), setup de Python 3.11 (`actions/setup-python@v5`), instalar dependencias desde `requirements.txt`, y correr `python pipeline_email_normalizacion.py`.
- Pasar todas las variables de entorno que el script necesita (`IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD`, `IMAP_ASUNTO_FILTRO`, `SMTP_HOST`, `SMTP_PORT`, `EMAIL_USER`, `EMAIL_PASSWORD`, `EMAIL_DESTINATARIO`, `EMAIL_SOPORTE`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_MAX_OUTPUT_TOKENS`) leyéndolas cada una como `${{ secrets.NOMBRE || vars.NOMBRE }}`, para que funcione sin importar si se cargaron como Secret o como Variable del repo/Environment.
- Agregar un comentario aclarando que un push que toque este archivo re-registra el trigger de `schedule` en GitHub Actions, lo cual puede ayudar a que el primer disparo programado llegue antes.
