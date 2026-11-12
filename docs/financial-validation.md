# Etapa 4: validación financiera MVP

Implementa V1–V7 y D2. No ejecuta Jev, Claude, propuestas ni aprobación humana.

## Uso

`POST /api/invoices/{invoice_id}/validation?reference_date=2026-11-12`

Sin body: carga datos actuales de PostgreSQL, configuración de `Settings` y ejecuta
una transacción. La fecha omitida usa America/Bogota en el adaptador HTTP; las
reglas reciben siempre una fecha explícita. Respuestas: 404 factura inexistente,
409 estado incompatible o extracción ausente. No se conecta automáticamente al
POST de carga: integración explícita para no cambiar las etapas 1–3.

También puede llamarse `evaluate_invoice(ValidationInput, RuleConfig)` sin servicios.
La estructura interna reutiliza `ExtractedInvoiceData`: el proveedor está dentro
de `invoice.supplier`. A diferencia del ejemplo plano V2, el adaptador PostgreSQL
realiza esta transformación. Los consumidores deben usar strings para dinero.

## Configuración única

`Settings.financial_validation`, mediante JSON en la variable de entorno
`FINANCIAL_VALIDATION`. Ejemplo de demostración (no calendario empresarial real):

```json
{"ruleset_version":"demo-1","enabled":{"V1":true,"V2":true,"V3":true,"V4":true,"V5":true,"V6":true,"V7":true,"D2":true},"arithmetic_tolerance":"1.00","purchase_order_tolerance":"0.00","precision":2,"rounding":"ROUND_HALF_UP","vat_rates":["0.19"],"open_periods":[{"start":"2026-09-01","end":"2026-09-30"}],"severity":"high"}
```

Las reglas omitidas de `enabled` están habilitadas. Por defecto no hay periodos:
V7 devuelve `not_evaluated` y requiere revisión. La configuración se carga al
iniciar el proceso; reiniciar tras cambiar variables de entorno.

D2 compara igualdad dentro de tolerancia, tanto excesos como valores inferiores.
Diferencia = declarado − esperado; porcentaje sobre valor absoluto esperado;
si esperado es cero, porcentaje null. V1–V4 y D2 cuantizan ambos valores con el
redondeo explícito antes de comparar. No se restan retenciones de V4.

Un descuento null se considera incierto. Solo cero explícito o
`known_no_discount_lines` en la entrada pura acredita ausencia conocida. El
adaptador persistente no inventa esa información. No hay IVA, tarifa no soportada,
impuestos distintos de IVA o datos incompletos: no se presume validez.
V5 usa los pesos proporcionados por el tutor, sin verificar existencia del emisor.

## Persistencia y revalidación

Reutiliza rule_checks, exceptions, invoices e invoice_events; no hay migraciones.
Cada ejecución agrega comprobaciones y excepciones con UUID nuevos. No borra ni
resuelve automáticamente hallazgos históricos. `validation_run_id` se guarda en
`rule_checks.evidence_refs` y en el payload del evento VALIDATION_COMPLETED. Ese
payload conserva entrada, configuración y salida completa. Las etapas siguientes
deben seleccionar el evento/ejecución vigente y sus check IDs, no sumar todas las
excepciones OPEN históricas. No hay columna SQL dedicada para run_id; si el equipo
necesita consultas/indexación directa, coordinar esa ampliación posteriormente.

Se bloquea la fila de factura durante la operación. Un fallo revierte resultados,
excepciones y evento. La validación no incrementa data_version: corresponde al
flujo de corrección incrementarla antes de invocar esta etapa. Lee datos relacionales
actuales; para duplicados conserva la extracción porque etapa 2 omite identidad,
líneas e impuestos normalizados en esa salida especial.

V6 consulta identidad y hash excluyendo la propia factura. Diferencia recarga de
mismo documento, identidad duplicada y revalidación propia. Reutiliza también
`duplicate_of_invoice_id`. No altera el índice único existente: la carrera de
cargas concurrentes en extracción requiere coordinación con su responsable;
validación no sustituye esa protección ni repara su error de inserción.

Si hay fallos/no evaluados habilitados o bloqueos externos, solicita revisión y
pasa a EXCEPCION. En otro caso pasa a VALIDADA. Revalidar sin cambiar estado registra
solo evento; las transiciones utilizan state_machine. Nunca llega a APROBADA ni
crea review_decisions. Conserva baja confianza y excepciones de reglas ajenas.

## Compatibilidad V2

Mantiene schema_version 0.2.0 y checks/exceptions. Amplía salida con data_version,
validation_run_id, ruleset_version, requires_human_review, blocking_reasons,
rule_check_id y reason_code. `downstream_exceptions()` conserva campos y agrega
expected/actual y difference_percentage para etapas 5–6. La UI y el endpoint de
detalle no se modifican: su consolidación queda a integración de etapa 7.

Coordinar en contrato: proveedor anidado, periodos abiertos, descuento incierto,
política simétrica de D2, selección de ejecución vigente y extensiones anteriores.

## Revisión técnica final

`get_current_validation_from_db(cur, invoice_id)` devuelve exclusivamente la última
VALIDATION_COMPLETED de `invoices.data_version`, o None si esa versión aún no fue
validada. Las nuevas ejecuciones incluyen `payload.run_sequence`, creciente por
factura y asignado bajo su bloqueo de fila. Esto evita empates incluso cuando dos
revalidaciones comparten el timestamp de una transacción. No modifica tablas.

`reference_date` HTTP solo se admite con ENVIRONMENT development, test o testing.
En producción y otros entornos se rechaza con 400; la fecha efectiva procede del
servidor en America/Bogota. Se conserva inyección explícita en el motor puro.
Los errores SQL dentro de la operación devuelven 503 sin detalles de conexión;
los datos persistidos inválidos devuelven 409. Los fallos de apertura de conexión
siguen bajo la dependencia get_db compartida, fuera del handler de la operación.

Los hallazgos OPEN de reglas desactivadas conservan el bloqueo: desactivar una
regla no resuelve automáticamente una anomalía anterior. Los documentos marcados
como duplicados del registro actual no convierten al original en una recarga.

### Verificación reproducible de SQL

Se añadió `tests/test_validation_postgres.py`, opt-in. Usar exclusivamente un DSN
de desarrollo aislado y desechable, con permisos CREATE SCHEMA/pgcrypto:

```bash
NEPXA_VALIDATION_TEST_DSN='host=/ruta/socket port=55439 dbname=postgres' \
  python -m pytest tests/test_validation.py tests/test_validation_postgres.py -q
```

La prueba crea un esquema UUID con search_path local y ejecuta schema.sql dentro
de una transacción con force_rollback=True. Verifica historial, versión vigente,
transiciones, duplicados sin tablas normalizadas y rollback de una revalidación.
No debe apuntarse a una base compartida aunque utilice rollback.

En esta revisión pasó contra PostgreSQL 16 temporal; PostgreSQL 17 del despliegue
queda pendiente. La suite completa falla en dos pruebas OCR si falta Tesseract:
`test_extract_blank_image_triggers_ocr` y
`test_extract_unreadable_document_transitions_to_exception`. CI ya instala el
binario y sus datos de idioma; no se alteraron esas pruebas ni sus dependencias.
