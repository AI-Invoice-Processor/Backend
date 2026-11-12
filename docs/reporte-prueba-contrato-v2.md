# Informe de prueba contra `contrato-V2.md`

**Fecha:** 2026-11-12
**Rama:** `development-only`
**Backend:** `/home/juan-carlos-cruz/Documentos/Universidad/NEPXA 2026/Proyecto/Backend`
**Dataset:** `/home/juan-carlos-cruz/Descargas/dataset_causacion_colombia`
**Contrato revisado:** `schema_version: "0.2.0"`

## 1. Objetivo

Comprobar el flujo de las etapas 1, 2 y 3 usando PostgreSQL local, la API FastAPI y los documentos del dataset de causación de Colombia.

La prueba se ejecutó sobre una base local vacía. Primero se cargaron los datos maestros de la Etapa 0 y después se procesaron las 55 facturas del dataset.

## 2. Preparación ejecutada

Se cargaron los catálogos definidos por el contrato:

| Tabla | Registros |
| --- | ---: |
| `accounts` | 10 |
| `cost_centers` | 6 |
| `suppliers` | 18 |
| `contracts` | 22 |
| `purchase_orders` | 95 |
| `purchase_order_lines` | 144 |
| `support_documents` | 91 |

La API y PostgreSQL quedaron disponibles en:

- API: `http://localhost:8000`
- Swagger: `http://localhost:8000/docs`
- PostgreSQL: `localhost:5432`
- `/health`: `healthy`
- `/ready`: `ready`

## 3. Resultado general

```json
{
  "facturas_evaluadas": 55,
  "uploads_accepted": 55,
  "stage2_extraction_status": {
    "success": 55
  },
  "stage3_matching_status": {
    "completed": 48,
    "partial": 7
  },
  "failures": []
}
```

Persistencia observada:

- `documents`: 55 registros.
- `invoices`: 55 registros.
- `documents.processing_stage = 'matching'`: 55 registros.
- `documents.processing_status = 'success'`: 48 registros.
- `documents.processing_status = 'partial'`: 7 registros.
- No hubo resultados `ambiguous` ni `failed` en este dataset.

Eventos registrados:

| Evento | Cantidad |
| --- | ---: |
| `INVOICE_RECEIVED` | 55 |
| `EXTRACTION_COMPLETED` | 53 |
| `DUPLICATE_INVOICE` | 2 |

Los dos eventos de duplicidad corresponden a facturas repetidas del dataset.

## 4. Comparación por etapa

### Etapa 1 — Recepción de documentos

**Resultado funcional: CUMPLE.**

Se aceptaron las 55 facturas y se persistieron documentos y facturas. El flujo:

1. Recibe archivos PDF, PNG o JPG.
2. Calcula SHA-256.
3. Guarda el archivo en el volumen de documentos.
4. Crea `documents` con estado inicial.
5. Crea `invoices` con estado de negocio `RECIBIDA`.

**Diferencias de contrato detectadas:**

- La respuesta de carga actual no es exactamente la envoltura común `{request_id, invoice_id, stage, status, data, errors, created_at}`.
- La respuesta usa `filename` y `status: "accepted"`, mientras que el ejemplo del contrato usa `file_name` y `schema_version`.
- La respuesta actual no incluye explícitamente `file_type` ni `document_category`.
- El endpoint recibe un archivo por solicitud; `batch_id` permite agrupar solicitudes, pero no se recibe una lista de archivos en una sola petición.

Estas diferencias son de forma de API; no impidieron la recepción ni la persistencia.

### Etapa 2 — OCR y extracción estructurada

**Resultado funcional: CUMPLE EN LA PRUEBA DETERMINISTA.**

Las 55 facturas finalizaron con `extraction_status: "success"`. El esquema implementa:

- `schema_version: "0.2.0"`.
- `DIGITAL_TEXT`, `OCR` y `AI` como métodos permitidos.
- Montos con `Decimal` y serialización JSON como strings decimales.
- Tasas como fracción decimal, por ejemplo `"0.19"`.
- Campos ausentes como `null`.
- Evidencia, confianza y errores estructurados.
- Persistencia de la extracción en `invoices.extraction_json`.

**Límite de esta prueba:**

La lectura de los PDF y la integración con PostgreSQL fueron reales, pero la llamada externa a Claude fue sustituida por los valores del CSV del dataset para que la prueba fuera repetible y no dependiera de credenciales ni consumo externo. Por tanto, esta ejecución valida el contrato y la persistencia, pero no valida la calidad de una respuesta real de Claude.

### Etapa 3 — Búsqueda y asociación

**Resultado funcional: CUMPLE.**

El caso `FE-9001` produjo una salida equivalente al ejemplo del contrato:

```json
{
  "schema_version": "0.2.0",
  "invoice_id": "683f23f3-0fd6-48c3-bb53-d8fc11f7429d",
  "matching_status": "completed",
  "supplier": {
    "supplier_id": "6d489b0e-5c14-414f-97b9-d488d4d9dea2",
    "nit_base": "900654321",
    "name": "Tecnologías Pacífico S.A.S.",
    "matched": true
  },
  "purchase_order": {
    "purchase_order_id": "e9aace06-b2f5-4e0b-a799-c56bb47bd6c4",
    "order_number": "OC-2026-9001",
    "matched": true
  },
  "contract": {
    "contract_id": "dac5c222-b800-47bd-af4a-b61e6575f0f6",
    "contract_number": "CT-2026-001",
    "matched": true
  },
  "support_documents": [
    {
      "support_document_id": "59baac6b-ce0b-43c8-b8ea-84e4c47a5909",
      "number": "SR-2026-0001",
      "type": "INFORME_SERVICIO",
      "matched": true
    }
  ],
  "missing_documents": [],
  "candidates": []
}
```

Los UUID son diferentes a los del contrato porque PostgreSQL los genera en cada ejecución. La estructura y los valores funcionales coinciden.

Reglas verificadas:

- `matching_status` permite `completed`, `partial`, `ambiguous` y `failed`.
- El proveedor se busca primero por `nit_base` exacto.
- La orden se busca por `order_number` exacto.
- `RapidFuzz` solo se utiliza como fallback para nombre de proveedor.
- Cuando existe ambigüedad, no se asocia automáticamente y se devuelven candidatos.
- Las pruebas específicas de matching finalizaron con `3 passed`.

**Diferencia pendiente:**

El valor `failed` está permitido por el esquema y la persistencia, pero una excepción inesperada del servicio todavía se propaga como error HTTP; no se convierte automáticamente en una respuesta JSON con `matching_status: "failed"`.

## 5. Estado del caso `FE-9001` en PostgreSQL

```text
invoice_id:          683f23f3-0fd6-48c3-bb53-d8fc11f7429d
document_id:         6823859c-0e44-4399-b739-d23857081346
supplier_nit_base:   900654321
supplier_name:       Tecnologías Pacífico S.A.S.
invoice_number:      FE-9001
issue_date:          2026-09-20
po_reference:        OC-2026-9001
subtotal:            52000000.00
total:               61880000.00
currency:            COP
match_status:        completed
processing_stage:     matching
processing_status:    success
business_status:      EXTRAIDA
```

El `business_status` permanece en `EXTRAIDA` porque las etapas 4 a 7 todavía no se ejecutan. La transición a `VALIDADA` o `EXCEPCION` pertenece a la validación financiera de la Etapa 4.

## 6. Conclusión

Las etapas 1, 2 y 3 funcionan con el dataset y cumplen funcionalmente el flujo definido en `contrato-V2.md`.

El resultado de Etapa 3 para `FE-9001` corresponde con el JSON de salida del contrato. Las principales diferencias pendientes son de integración de API:

1. Alinear las respuestas de Etapa 1 con la envoltura común del contrato.
2. Exponer explícitamente `schema_version`, `file_type` y `document_category` donde corresponda.
3. Definir la respuesta JSON de `failed` ante errores inesperados de matching.
4. Ejecutar una prueba adicional con Claude real si se desea validar la extracción completa, no solo el contrato y la persistencia.

Las etapas 4, 5, 6 y 7 no forman parte de esta prueba y permanecen pendientes.
