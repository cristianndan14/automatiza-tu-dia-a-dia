# Pipeline de normalización con ingesta y salida por email

Script único (`pipeline_email_normalizacion.py`) que arma el flujo completo de la demo:

1. Busca en una casilla de email el CSV a procesar (adjunto de un mail no leído).
2. Lo normaliza con reglas **deterministas** (nada de IA en esta parte: siempre da el mismo resultado con el mismo archivo).
3. **Siempre** manda un email con el reporte de ventas por categoría (a `EMAIL_DESTINATARIO`), con el CSV del reporte y un PNG con gráficos (ventas totales, ticket promedio y transacciones por categoría) adjuntos. Si no se pudo normalizar el 100% del dataset, el cuerpo del email —redactado por un LLM (API de Gemini)— aclara explícitamente qué % de las transacciones respalda ese número.
4. Si quedó algo pendiente de revisión, manda un **segundo email separado** a soporte (`EMAIL_SOPORTE`) con el dataset de esas transacciones adjunto, y el detalle de qué revisar redactado por el LLM en el cuerpo. Si no queda nada pendiente, este segundo email no se envía.

## 1. Requisitos

- Python 3.9 o superior.
- Una cuenta de Gmail (u otro proveedor con IMAP/SMTP) para la casilla que recibe el CSV y envía el resultado. Pueden ser la misma casilla o dos distintas.
- Una API key de Gemini (para la redacción del email) — se consigue gratis en [aistudio.google.com/apikey](https://aistudio.google.com/apikey).

## 2. Instalación

```bash
git clone <tu-repo>   # o simplemente parate en la carpeta donde tenés el script
cd <carpeta-del-proyecto>

python3 -m venv venv
source venv/bin/activate        # en Windows: venv\Scripts\activate

pip install -r requirements.txt
```

`requirements.txt`:

```
pandas>=2.0
numpy>=1.24
matplotlib>=3.8
google-genai>=0.3
python-dotenv>=1.0
```

## 3. Configurar las credenciales

El script lee todo desde variables de entorno, nunca hardcodeadas en el código. La forma más simple en local es con un archivo `.env`:

```bash
cp .env.example .env
```

Y completar `.env` con tus datos reales:

```
IMAP_HOST=imap.gmail.com
IMAP_USER=tu_casilla@gmail.com
IMAP_PASSWORD=tu_contraseña_de_aplicacion
IMAP_ASUNTO_FILTRO=Reporte diario

SMTP_HOST=smtp.gmail.com
SMTP_PORT=465
EMAIL_USER=tu_casilla@gmail.com
EMAIL_PASSWORD=tu_contraseña_de_aplicacion
EMAIL_DESTINATARIO=destinatario@ejemplo.com
EMAIL_SOPORTE=soporte@ejemplo.com

GEMINI_API_KEY=AIza...
GEMINI_MODEL=gemini-2.5-flash
GEMINI_MAX_OUTPUT_TOKENS=800
```

`EMAIL_DESTINATARIO` recibe siempre el reporte de ventas (completo o parcial). `EMAIL_SOPORTE` recibe el dataset con las transacciones pendientes, solo cuando efectivamente queda alguna — pueden ser la misma casilla o dos distintas.

**Importante: `.env` ya está listado en `.gitignore` — no lo saques de ahí, porque tiene credenciales reales.**

### Si usás Gmail

Gmail ya no acepta tu contraseña normal para IMAP/SMTP. Necesitás:

1. Activar la verificación en dos pasos en tu cuenta de Google.
2. Generar una "Contraseña de aplicación" en https://myaccount.google.com/apppasswords (elegí "Otra" y ponele un nombre, por ejemplo "pipeline-python").
3. Usar esa contraseña de 16 caracteres como `IMAP_PASSWORD` / `EMAIL_PASSWORD` — no la contraseña de tu cuenta.
4. Verificar que IMAP esté habilitado: Gmail → Configuración → Reenvío y correo POP/IMAP → Habilitar IMAP.

Si usás otro proveedor (Outlook, un dominio propio, etc.), cambiá `IMAP_HOST`/`SMTP_HOST`/`SMTP_PORT` según su documentación.

## 4. Correrlo

El script se conecta siempre contra IMAP/SMTP/Gemini reales (no tiene modo simulación). Con el `.env` completo:

1. Mandate a vos mismo (a la casilla configurada en `IMAP_USER`) un email de prueba con:
   - Asunto que contenga el texto de `IMAP_ASUNTO_FILTRO` (por defecto "Reporte diario").
   - El CSV adjunto (por ejemplo `retail_store_sales_demo50_es.csv`).
2. Corré el script:
   ```bash
   python3 pipeline_email_normalizacion.py
   ```
3. Revisá la casilla de `EMAIL_DESTINATARIO`: debería llegar el email con el resultado.

Si no hay ningún email nuevo sin leer que matchee el asunto, el script no hace nada y termina (pensado para correr en un cron cada pocos minutos).

Los archivos que genera en cada corrida quedan en `/tmp/pipeline_salida/` (dataset normalizado, reporte de ventas y el PNG con los gráficos, si se pudieron generar).

## 5. Automatizarlo con GitHub Actions

El workflow `.github/workflows/pipeline.yml` corre el pipeline solo, sin depender de tu máquina:

- **`schedule`** con cron `*/6 * * * *` → lo dispara automáticamente cada 6 minutos.
- **`workflow_dispatch`** → agrega el botón "Run workflow" en la pestaña **Actions** del repo en GitHub, para correrlo a mano cuando quieras.

Para que funcione, el `.env` no viaja al repo (está en `.gitignore`), así que hay que cargar las mismas variables como **secrets** del repositorio:

1. En GitHub: **Settings → Secrets and variables → Actions → New repository secret**.
2. Creá un secret por cada variable que usa el workflow, con el mismo nombre y el valor real: `IMAP_HOST`, `IMAP_USER`, `IMAP_PASSWORD`, `IMAP_ASUNTO_FILTRO`, `SMTP_HOST`, `SMTP_PORT`, `EMAIL_USER`, `EMAIL_PASSWORD`, `EMAIL_DESTINATARIO`, `EMAIL_SOPORTE`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_MAX_OUTPUT_TOKENS`.
3. Hacé push del workflow a la rama `main` (los workflows programados solo corren desde la rama por defecto).
4. Para probarlo sin esperar al cron: pestaña **Actions** → "Pipeline de normalización y reporte" → **Run workflow**.

Tené en cuenta:
- El cron de GitHub Actions no es exacto — bajo carga puede haber demora de varios minutos respecto al horario pactado.
- Si el repositorio pasa 60 días sin ningún commit, GitHub desactiva los workflows programados automáticamente (hay que reactivarlos a mano desde Actions).

## 6. Ajustar el modelo de IA

La variable `GEMINI_MODEL` en el `.env` trae `gemini-2.5-flash` como valor de ejemplo. Confirmá en [ai.google.dev/gemini-api/docs/models](https://ai.google.dev/gemini-api/docs/models) cuál es el identificador de modelo vigente y actualizalo ahí si querés usar otro. `GEMINI_MAX_OUTPUT_TOKENS` controla el límite de tokens de la respuesta del modelo (default 800); subilo si ves las respuestas cortadas a mitad de frase.

## 7. Problemas comunes

- **`imaplib.IMAP4.error: [AUTHENTICATIONFAILED]`** → estás usando tu contraseña normal de Gmail en vez de una contraseña de aplicación, o IMAP está deshabilitado en la cuenta.
- **No encuentra ningún email nuevo** → revisá que el asunto del mail de prueba contenga exactamente el texto de `IMAP_ASUNTO_FILTRO`, y que el mail esté como "no leído" en la bandeja de entrada.
- **`KeyError: 'GEMINI_API_KEY'`** → falta esa variable en el `.env` (localmente) o como secret (en GitHub Actions), o no se cargó (verificá que `python-dotenv` esté instalado y que el `.env` esté en la misma carpeta desde donde corrés el script).
- **El email no llega pero el script no tira error** → revisá la carpeta de spam del destinatario, y que `EMAIL_USER`/`EMAIL_PASSWORD` correspondan a la misma casilla que autoriza el envío por SMTP.
- **El workflow de GitHub Actions no corre** → confirmá que esté en `main`, que hayas cargado todos los secrets, y que el repo no lleve más de 60 días sin commits (revisá la pestaña Actions por si está pausado).
