"""Register the real PO, contract, and support PDFs referenced by invoices.

The seed data already stores the relationships in PostgreSQL. This backfill
adds the corresponding files to the documents registry and keeps the operation
idempotent by using each storage reference as its stable identity.
"""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import psycopg


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def related_references(cur: psycopg.Cursor) -> list[tuple[str, str]]:
    cur.execute(
        """
        SELECT DISTINCT po.pdf_ref, 'purchase_order'
          FROM purchase_orders po
          JOIN invoices i ON i.purchase_order_id = po.id
         WHERE po.pdf_ref IS NOT NULL
        UNION
        SELECT DISTINCT c.pdf_ref, 'contract'
          FROM contracts c
          JOIN invoices i ON i.contract_id = c.id
         WHERE c.pdf_ref IS NOT NULL
        UNION
        SELECT DISTINCT sd.pdf_ref, 'support'
          FROM support_documents sd
          JOIN purchase_orders po ON po.id = sd.purchase_order_id
          JOIN invoices i ON i.purchase_order_id = po.id
         WHERE sd.pdf_ref IS NOT NULL
        ORDER BY 1
        """
    )
    return [
        (str(storage_ref), str(category)) for storage_ref, category in cur.fetchall()
    ]


def register_documents(
    database_url: str, source_root: Path, documents_dir: Path
) -> tuple[int, int]:
    source_root = source_root.resolve()
    documents_dir = documents_dir.resolve()
    documents_dir.mkdir(parents=True, exist_ok=True)
    registered = 0
    missing = 0

    with psycopg.connect(database_url) as conn:
        with conn.cursor() as cur:
            references = related_references(cur)
            for storage_ref, category in references:
                relative_pdf = Path(storage_ref).relative_to("pdf")
                source = (source_root / relative_pdf).resolve()
                if source_root not in source.parents or not source.is_file():
                    missing += 1
                    print(f"Archivo relacionado no encontrado: {source}")
                    continue

                target = documents_dir / source.name
                target.write_bytes(source.read_bytes())
                document_id = uuid5(NAMESPACE_URL, f"nepxa:{storage_ref}")
                digest = sha256(source)
                cur.execute(
                    """
                    INSERT INTO documents (
                        id, file_name, mime_type, category, storage_ref,
                        sha256, size_bytes, processing_stage, processing_status
                    )
                    SELECT %s, %s, 'application/pdf', %s, %s,
                           %s, %s, 'related_document_registry', 'success'
                     WHERE NOT EXISTS (
                         SELECT 1 FROM documents WHERE storage_ref = %s
                     )
                    """,
                    (
                        document_id,
                        source.name,
                        category,
                        storage_ref,
                        digest,
                        source.stat().st_size,
                        storage_ref,
                    ),
                )
                registered += cur.rowcount
        conn.commit()
    return registered, missing


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.getenv(
            "DATABASE_URL",
            "postgresql://invoice:local-dev-password-change-me@localhost:5432/invoices",
        ),
    )
    parser.add_argument(
        "--source-root",
        type=Path,
        required=True,
        help="Dataset PDF directory, e.g. salida/pdf",
    )
    parser.add_argument(
        "--documents-dir",
        type=Path,
        default=Path(os.getenv("DOCUMENTS_DIR", "data/documents")),
    )
    args = parser.parse_args()
    registered, missing = register_documents(
        args.database_url, args.source_root, args.documents_dir
    )
    print(
        f"Documentos relacionados registrados: {registered}; archivos faltantes: {missing}"
    )


if __name__ == "__main__":
    main()
