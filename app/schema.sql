-- AI Invoice Processor · Modelo lógico MVP (PostgreSQL 17)
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- 1. Datos maestros
CREATE TABLE IF NOT EXISTS accounts (
  code text PRIMARY KEY,
  name text NOT NULL
);

CREATE TABLE IF NOT EXISTS cost_centers (
  code text PRIMARY KEY,
  name text NOT NULL
);

CREATE TABLE IF NOT EXISTS suppliers (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  nit_base text NOT NULL UNIQUE,
  dv char(1) NOT NULL,
  legal_name text NOT NULL,
  person_type text NOT NULL CHECK (person_type IN ('JURIDICA', 'NATURAL')),
  tax_regime text NOT NULL CHECK (tax_regime IN ('COMUN', 'SIMPLE')),
  is_self_withholder boolean NOT NULL DEFAULT false,
  city text
);

CREATE TABLE IF NOT EXISTS contracts (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  contract_number text NOT NULL UNIQUE,
  supplier_id uuid NOT NULL REFERENCES suppliers(id),
  contract_type text NOT NULL,
  object text NOT NULL,
  total_amount numeric(18,2) NOT NULL,
  start_date date NOT NULL,
  end_date date NOT NULL,
  payment_days integer NOT NULL,
  default_account text NOT NULL REFERENCES accounts(code),
  default_cost_center text NOT NULL REFERENCES cost_centers(code),
  pdf_ref text
);

CREATE TABLE IF NOT EXISTS purchase_orders (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  order_number text NOT NULL UNIQUE,
  supplier_id uuid NOT NULL REFERENCES suppliers(id),
  contract_id uuid REFERENCES contracts(id),
  order_date date NOT NULL,
  subtotal numeric(18,2) NOT NULL,
  tax_amount numeric(18,2) NOT NULL,
  total numeric(18,2) NOT NULL,
  status text NOT NULL CHECK (status IN ('RECIBIDA', 'RECIBIDA_PARCIAL', 'PENDIENTE_RECEPCION')),
  pdf_ref text
);

CREATE TABLE IF NOT EXISTS purchase_order_lines (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  purchase_order_id uuid NOT NULL REFERENCES purchase_orders(id),
  line_no integer NOT NULL,
  description text NOT NULL,
  quantity numeric(18,4) NOT NULL,
  unit_price numeric(18,2) NOT NULL,
  amount numeric(18,2) NOT NULL,
  UNIQUE (purchase_order_id, line_no)
);

CREATE TABLE IF NOT EXISTS support_documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  purchase_order_id uuid NOT NULL REFERENCES purchase_orders(id),
  support_type text NOT NULL CHECK (support_type IN ('ACTA_RECEPCION', 'REMISION', 'ENTRADA_ALMACEN', 'INFORME_SERVICIO')),
  number text NOT NULL UNIQUE,
  received_date date NOT NULL,
  received_amount numeric(18,2) NOT NULL,
  pdf_ref text
);

-- 2. Recepción, extracción y asociación (Etapas 1-3)
CREATE TABLE IF NOT EXISTS documents (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  file_name text NOT NULL,
  mime_type text NOT NULL,
  category text NOT NULL CHECK (category IN ('invoice', 'purchase_order', 'contract', 'support')),
  storage_ref text NOT NULL,
  sha256 text NOT NULL,
  size_bytes bigint,
  batch_id uuid,
  processing_stage text,
  processing_status text NOT NULL CHECK (processing_status IN ('pending', 'processing', 'success', 'partial', 'failed')),
  error jsonb,
  duplicate_of_invoice_id uuid,
  received_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_documents_sha256 ON documents (sha256);
CREATE INDEX IF NOT EXISTS ix_documents_batch ON documents (batch_id);

CREATE TABLE IF NOT EXISTS invoices (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  document_id uuid NOT NULL UNIQUE REFERENCES documents(id),
  supplier_id uuid REFERENCES suppliers(id),
  supplier_nit_base text,
  supplier_dv char(1),
  supplier_name text,
  prefix text,
  number text,
  issue_date date,
  currency char(3) NOT NULL DEFAULT 'COP',
  subtotal numeric(18,2),
  total numeric(18,2),
  retentions_total numeric(18,2),
  net_payable numeric(18,2),
  po_reference text,
  extraction_method text CHECK (extraction_method IN ('DIGITAL_TEXT', 'OCR', 'AI')),
  extraction_confidence numeric(5,4),
  extraction_json jsonb,
  purchase_order_id uuid REFERENCES purchase_orders(id),
  contract_id uuid REFERENCES contracts(id),
  match_status text CHECK (match_status IN ('completed', 'partial', 'ambiguous', 'failed')),
  match_detail jsonb,
  risk_level text CHECK (risk_level IN ('low', 'medium', 'high')),
  risk_score numeric(5,4),
  risk_confidence numeric(5,4),
  business_status text NOT NULL CHECK (business_status IN ('RECIBIDA', 'EXTRAIDA', 'VALIDADA', 'EXCEPCION', 'PROPUESTA', 'APROBADA', 'RECHAZADA')),
  data_version integer NOT NULL DEFAULT 1,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_invoices_identity
  ON invoices (supplier_nit_base, prefix, number)
  WHERE supplier_nit_base IS NOT NULL AND prefix IS NOT NULL AND number IS NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint WHERE conname = 'fk_documents_duplicate_of'
  ) THEN
    ALTER TABLE documents
      ADD CONSTRAINT fk_documents_duplicate_of
      FOREIGN KEY (duplicate_of_invoice_id) REFERENCES invoices(id);
  END IF;
END $$;

CREATE TABLE IF NOT EXISTS invoice_lines (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id uuid NOT NULL REFERENCES invoices(id),
  line_no integer NOT NULL,
  description text,
  quantity numeric(18,4),
  unit_price numeric(18,2),
  discount_amount numeric(18,2),
  amount numeric(18,2),
  UNIQUE (invoice_id, line_no)
);

CREATE TABLE IF NOT EXISTS invoice_taxes (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id uuid NOT NULL REFERENCES invoices(id),
  tax_type text NOT NULL,
  rate numeric(7,4),
  base numeric(18,2),
  amount numeric(18,2)
);

-- 3. Validación financiera (Etapa 4)
CREATE TABLE IF NOT EXISTS rule_checks (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id uuid NOT NULL REFERENCES invoices(id),
  data_version integer NOT NULL,
  ruleset_version text NOT NULL,
  evaluation_date date NOT NULL,
  rule_id text NOT NULL,
  invoice_line_no integer,
  result text NOT NULL CHECK (result IN ('passed', 'failed', 'warning', 'not_evaluated')),
  comparison_basis text,
  expected_amount numeric(18,2),
  actual_amount numeric(18,2),
  difference numeric(18,2),
  difference_pct numeric(9,4),
  tolerance numeric(18,4),
  expected_text text,
  actual_text text,
  reason text,
  evidence_refs jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_rule_checks_invoice ON rule_checks (invoice_id, data_version);

CREATE TABLE IF NOT EXISTS exceptions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id uuid NOT NULL REFERENCES invoices(id),
  rule_check_id uuid NOT NULL REFERENCES rule_checks(id),
  exception_type text NOT NULL,
  severity text NOT NULL CHECK (severity IN ('low', 'medium', 'high')),
  message text NOT NULL,
  requires_review boolean NOT NULL DEFAULT true,
  resolution_status text NOT NULL CHECK (resolution_status IN ('OPEN', 'CORRECTED', 'AUTHORIZED', 'DISMISSED')),
  created_at timestamptz NOT NULL DEFAULT now()
);

-- 4. Propuesta de causación (Etapa 6)
CREATE TABLE IF NOT EXISTS accounting_proposals (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id uuid NOT NULL REFERENCES invoices(id),
  data_version integer NOT NULL,
  status text NOT NULL CHECK (status IN ('BLOCKED_PENDING_REVIEW', 'READY_FOR_APPROVAL', 'APPROVED', 'REJECTED', 'OBSOLETE')),
  supplier_id uuid REFERENCES suppliers(id),
  account_code text REFERENCES accounts(code),
  cost_center_code text REFERENCES cost_centers(code),
  subtotal numeric(18,2),
  tax_total numeric(18,2),
  total numeric(18,2),
  explanation_summary text,
  explanation_details text,
  recommendation text,
  generated_by text NOT NULL CHECK (generated_by IN ('TEMPLATE', 'CLAUDE')),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (invoice_id, data_version)
);

-- 5. Revisión humana y trazabilidad (Etapa 7)
CREATE TABLE IF NOT EXISTS review_decisions (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id uuid NOT NULL REFERENCES invoices(id),
  exception_id uuid REFERENCES exceptions(id),
  proposal_id uuid REFERENCES accounting_proposals(id),
  decision_type text NOT NULL CHECK (decision_type IN ('REQUEST_REVIEW', 'CORRECT_DATA', 'ATTACH_SUPPORT', 'AUTHORIZE_EXCEPTION', 'APPROVE_PROPOSAL', 'REJECT')),
  decided_by text NOT NULL,
  reason text,
  evidence_document_id uuid REFERENCES documents(id),
  requires_revalidation boolean NOT NULL DEFAULT false,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS invoice_events (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  invoice_id uuid REFERENCES invoices(id),
  document_id uuid REFERENCES documents(id),
  event_type text NOT NULL,
  occurred_at timestamptz NOT NULL DEFAULT now(),
  actor_type text NOT NULL CHECK (actor_type IN ('system', 'ai', 'human')),
  actor_name text,
  from_status text,
  to_status text,
  rule_ref text,
  reason text,
  payload jsonb
);