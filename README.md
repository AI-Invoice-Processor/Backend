# AI Invoice Processor Backend

API base del proyecto AI Invoice Processor construida con FastAPI. La lógica OCR, IA, reglas contables y persistencia de facturas se integrarán en fases posteriores.

## Desarrollo

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

## Docker

El Compose de producción levanta FastAPI y PostgreSQL en la red externa `ai-invoice-network`. Para una prueba local:

```bash
docker network create ai-invoice-network 2>/dev/null || true
docker compose -f docker-compose.prod.yml up --build
```

En producción, `POSTGRES_PASSWORD` se genera dentro del servidor y se guarda en `/opt/ai-invoice-processor/.env`, que no se versiona.

El workflow `.github/workflows/deploy.yml` ejecuta las pruebas y despliega únicamente el backend en cada push a `main`. Requiere los secretos `DEPLOY_HOST`, `DEPLOY_USER`, `DEPLOY_SSH_KEY` y `DEPLOY_KNOWN_HOSTS`.
