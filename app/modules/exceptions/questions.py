"""Centralized definition of TypeSafe Jev questions, options, and criteria.

Following the TypeSafe craft recommendations:
- Keep all question wording in one place for auditing and iteration.
- One judgment per question.
- Plain descriptions in criteria, descriptive option keys.
- Bundle questions that share state.
"""

from typesafe_sdk import Choice, Noul

EXCEPTION_CATEGORY_CRITERIA = {
    "amount_mismatch": (
        "Discrepancy in monetary amounts, unit prices, subtotal, total, or difference against "
        "purchase order or contract budgeted amounts."
    ),
    "tax_mismatch": (
        "Discrepancy in tax calculation, VAT (IVA) rate, tax base, or statutory retentions."
    ),
    "supplier_mismatch": (
        "Discrepancy in supplier identification, NIT, verification digit (DV), or vendor name."
    ),
    "duplicate_invoice": (
        "Duplicate invoice detected by document hash, identical invoice number/prefix, or preexisting record."
    ),
    "missing_support": (
        "Missing mandatory supporting documentation such as goods receipt (acta de recepción), delivery remisión, or warehouse entry."
    ),
    "contract_expired": (
        "Invoice issue date is outside active contract validity period or contract is inactive."
    ),
    "purchase_order_missing": (
        "Invoice lacks a required purchase order reference or purchase order could not be linked."
    ),
    "incomplete_information": (
        "Required invoice fields, lines, or tax breakdowns are missing, illegible, or low extraction confidence."
    ),
    "other": (
        "Discrepancy or warning that does not clearly belong to any of the specific categories above."
    ),
}

RISK_LEVEL_CRITERIA = {
    "low": (
        "Minor clerical deviation, negligible monetary difference within tolerable bounds, "
        "low audit impact, complete supporting context."
    ),
    "medium": (
        "Moderate monetary discrepancy or non-critical documentation variance that requires "
        "standard review before approval."
    ),
    "high": (
        "Significant monetary difference, major tax variance, missing purchase order, or substantial "
        "compliance failure requiring supervisor authorization."
    ),
    "critical": (
        "Severe financial irregularity, potential fraud, duplicate invoice, unauthorized supplier, "
        "or expired contract requiring immediate blocking."
    ),
}

# Calibrated numerical weights for calculating continuous risk score from risk level probabilities
RISK_LEVEL_WEIGHTS = {
    "low": 0.15,
    "medium": 0.45,
    "high": 0.80,
    "critical": 1.00,
}

# Confidence and ambiguity thresholds (calibrated defaults as per TypeSafe guidelines)
MIN_CONFIDENCE_THRESHOLD = 0.70
MAX_AMBIGUITY_THRESHOLD = 0.50
MIN_SUFFICIENCY_THRESHOLD = 0.60


def build_stage5_questions() -> dict:
    """Build the dictionary of questions to send in a single system_one bundle."""
    return {
        "category": Choice(
            instructions=(
                "Which category best classifies the primary financial or compliance exception "
                "described in `findings` and `context`?"
            ),
            criteria=EXCEPTION_CATEGORY_CRITERIA,
        ),
        "risk_level": Choice(
            instructions=(
                "What is the overall financial and compliance risk level of this invoice discrepancy?"
            ),
            criteria=RISK_LEVEL_CRITERIA,
        ),
        "information_sufficiency": Noul(
            instructions=(
                "Is the available information in `findings` and `context` sufficient to evaluate "
                "the exception and assess compliance risk?"
            )
        ),
        "ambiguity_detected": Noul(
            instructions=(
                "Are there conflicting signals, unclear discrepancies, or ambiguous circumstances "
                "in the invoice evaluation data?"
            )
        ),
    }
