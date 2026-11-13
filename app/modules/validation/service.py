from decimal import localcontext

from app.modules.validation.rules import RULES, missing
from app.schemas.validation import (
    RuleConfig,
    ValidationException,
    ValidationInput,
    ValidationOutput,
)


def evaluate_invoice(
    data: ValidationInput | dict, config: RuleConfig | None = None
) -> ValidationOutput:
    data = ValidationInput.model_validate(data)
    config = config or RuleConfig()
    checks = []
    with localcontext() as context:
        context.prec = 60
        for rule_id, rule in RULES.items():
            results = (
                rule(data, config)
                if config.enabled.get(rule_id, True)
                else [missing(rule_id, "Regla desactivada", "DISABLED")]
            )
            for check in results:
                check.ruleset_version = config.ruleset_version
                checks.append(check)
    exceptions = []
    for check in checks:
        if check.result != "passed" and check.reason_code != "DISABLED":
            exceptions.append(
                ValidationException(
                    rule_check_id=check.rule_check_id,
                    rule_id=check.rule_id,
                    type="DIFERENCIA_VS_OC"
                    if check.rule_id == "D2"
                    else check.reason_code,
                    severity=config.severity,
                    description=check.reason,
                    expected_amount=check.expected_amount,
                    actual_amount=check.actual_amount,
                    difference=check.difference,
                    difference_pct=check.difference_pct,
                    evidence_refs=check.evidence_refs,
                )
            )
    blockers = list(data.external_blockers)
    if not any(c.reason_code != "DISABLED" for c in checks):
        blockers.append("ALL_RULES_DISABLED")
    return ValidationOutput(
        invoice_id=data.invoice_id,
        data_version=data.data_version,
        ruleset_version=config.ruleset_version,
        checks=checks,
        exceptions=exceptions,
        requires_human_review=bool(exceptions or blockers),
        blocking_reasons=blockers,
    )


def downstream_exceptions(output: ValidationOutput) -> list[dict]:
    """Add aliases needed by stages 5 and 6 without removing contract fields."""
    return [
        dict(
            e.model_dump(mode="json"),
            expected=str(e.expected_amount) if e.expected_amount is not None else None,
            actual=str(e.actual_amount) if e.actual_amount is not None else None,
            difference_percentage=str(e.difference_pct)
            if e.difference_pct is not None
            else None,
        )
        for e in output.exceptions
    ]
