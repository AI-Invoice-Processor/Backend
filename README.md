# AI Invoice Processor Backend

API base del proyecto AI Invoice Processor construida con FastAPI. La lógica OCR, IA, reglas contables y persistencia de facturas se integrarán en fases posteriores.

## Desarrollo Local con Docker

Para levantar el entorno completo de desarrollo (FastAPI con hot-reload en `localhost:8000` y PostgreSQL en `localhost:5432`):

1. **Crear la red Docker (solo la primera vez):**
   ```bash
   docker network create ai-invoice-network 2>/dev/null || true
   ```

2. **Configurar variables de entorno:**
   ```bash
   cp .env.example .env
   ```
   Abre `.env` y coloca tu `ANTHROPIC_API_KEY` para que la extracción con IA funcione.

3. **Iniciar los servicios:**
   ```bash
   docker compose -f docker-compose.local.yml up --build
   ```

4. **Crear tablas y poblar la base de datos (Seed):**
   Una vez que el contenedor esté corriendo, inicializa el esquema y carga los datos sintéticos con:
   ```bash
   # Desde el entorno virtual local (apuntando a localhost:5432):
   export SEED_DATA_DIR="/ruta/a/tus/archivos_csv"
   python -m app.seed
   ```

---

## Desarrollo Local con Python Nativo

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
pytest
uvicorn app.main:app --reload
```

Endpoints iniciales:

- `GET /health`: liveness del proceso.
- `GET /ready`: comprueba la conexión con PostgreSQL.
- `GET /api/invoices`: contrato inicial, actualmente devuelve una lista vacía.
- `POST /api/invoices`: valida PDF, PNG y JPG de hasta 10 MB y acepta el archivo para procesamiento futuro.
- `POST /api/invoices/{invoice_id}/matching`: ejecuta nuevamente la Etapa 3 y asocia la factura contra proveedores, órdenes de compra, contratos y soportes maestros.

Cuando la Etapa 2 obtiene datos de extracción, la carga de una factura también ejecuta automáticamente la Etapa 3. Las coincidencias exactas usan el NIT y el número de orden; RapidFuzz sólo se usa para nombres de proveedores y devuelve candidatos sin asociar cuando existe ambigüedad.

## Documentación de la API

FastAPI genera automáticamente la documentación interactiva:

- Swagger UI: `GET /api/docs`
- ReDoc: `GET /api/redoc`
- Especificación OpenAPI JSON: `GET /api/openapi.json`

## Docker

El Compose de producción levanta FastAPI y PostgreSQL en la red externa `ai-invoice-network`. Para una prueba local:

```bash
docker network create ai-invoice-network 2>/dev/null || true
docker compose -f docker-compose.prod.yml up --build
```

En producción, `POSTGRES_PASSWORD` se genera dentro del servidor y se guarda en `/home/ubuntu/ai-invoice-processor/.env`, que no se versiona.

El workflow `.github/workflows/deploy.yml` ejecuta las pruebas y despliega únicamente el backend en cada push a `main`. Requiere los secretos `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY` y `DEPLOY_KNOWN_HOSTS`.
