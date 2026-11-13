"""Comparative benchmark script: Jev TypeSafe AI vs Traditional Rules vs Claude LLM.

Evaluates against the labeled dataset measuring:
- Category Classification Accuracy
- Risk Level Accuracy
- Precision for Human Review
- Recall for Human Review
- False Positives (FP)
- False Negatives (FN)
- Latency (avg, min, max)
- Cost per evaluation
"""

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Load local environment
load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR.parent))

from app.modules.exceptions.adapter import JevExceptionAdapter
from app.modules.exceptions.evaluator import (
    ClaudeExceptionEvaluator,
    ExceptionEvaluatorService,
    RuleBasedFallbackEvaluator,
)
from app.schemas.exceptions import (
    EvaluationContextInput,
    ExceptionFindingInput,
    Stage5EvaluationInput,
    Stage5EvaluationOutput,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("benchmark")

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR.parent))
DATASET_PATH = BASE_DIR / "labeled_dataset.json"

# Pricing models (USD per million tokens)
PRICING = {
    "jev": {"input": 0.15, "output": 0.60},
    "claude": {"input": 3.00, "output": 15.00},
    "rules": {"input": 0.00, "output": 0.00},
}


def load_dataset() -> list[dict[str, Any]]:
    with DATASET_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def calculate_cost(approach: str, input_tokens: int, output_tokens: int) -> float:
    rates = PRICING.get(approach, {"input": 0.0, "output": 0.0})
    cost = (input_tokens / 1_000_000.0) * rates["input"] + (output_tokens / 1_000_000.0) * rates["output"]
    return cost


def run_benchmark():
    dataset = load_dataset()
    logger.info("Loaded %d test cases from %s", len(dataset), DATASET_PATH)

    typesafe_key = os.getenv("TYPESAFE_API_KEY")
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")

    approaches: dict[str, Any] = {}

    # 1. Traditional Rules
    approaches["rules"] = lambda item: RuleBasedFallbackEvaluator.evaluate(item)

    # 2. Jev TypeSafe AI
    if typesafe_key:
        jev_adapter = JevExceptionAdapter(api_key=typesafe_key)
        jev_service = ExceptionEvaluatorService(jev_adapter=jev_adapter)
        approaches["jev"] = lambda item: jev_service.evaluate_exceptions(item)
    else:
        logger.warning("TYPESAFE_API_KEY no encontrada. Omitiendo Jev en vivo.")

    # 3. Claude Sonnet
    if anthropic_key:
        claude_eval = ClaudeExceptionEvaluator(api_key=anthropic_key)
        approaches["claude"] = lambda item: claude_eval.evaluate(item)
    else:
        logger.warning("ANTHROPIC_API_KEY no encontrada. Omitiendo Claude en vivo.")

    results: dict[str, dict[str, Any]] = {}

    for name, runner in approaches.items():
        logger.info("Running evaluation for approach: %s ...", name)
        cat_matches = 0
        risk_matches = 0
        tp = 0
        fp = 0
        tn = 0
        fn = 0
        latencies = []
        total_input_tokens = 0
        total_output_tokens = 0
        details = []

        for case in dataset:
            cid = case["id"]
            gt = case["ground_truth"]
            exp_cat = gt["expected_category"]
            exp_risk = gt["expected_risk_level"]
            exp_review = gt["requires_human_review"]

            # Construct Pydantic model
            inp_data = Stage5EvaluationInput(
                invoice_id=case["input"]["invoice_id"],
                exceptions=[ExceptionFindingInput(**e) for e in case["input"]["exceptions"]],
                context=EvaluationContextInput(**case["input"]["context"]),
            )

            t0 = time.perf_counter()
            out: Stage5EvaluationOutput = runner(inp_data)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0

            pred_cat = out.risk_assessment.exception_category
            pred_risk = out.risk_assessment.risk_level
            pred_review = out.risk_assessment.requires_human_review

            # Track tokens
            in_tok = out.audit_trace.input_tokens or 0 if out.audit_trace else 0
            out_tok = out.audit_trace.output_tokens or 0 if out.audit_trace else 0
            total_input_tokens += in_tok
            total_output_tokens += out_tok
            latencies.append(out.audit_trace.latency_ms if (out.audit_trace and out.audit_trace.latency_ms > 0) else elapsed_ms)

            # Accuracy
            if pred_cat == exp_cat:
                cat_matches += 1
            if pred_risk == exp_risk:
                risk_matches += 1

            # Confusion matrix on requires_human_review (Positive = Requires Review)
            if exp_review and pred_review:
                tp += 1
            elif not exp_review and pred_review:
                fp += 1
            elif not exp_review and not pred_review:
                tn += 1
            elif exp_review and not pred_review:
                fn += 1

            details.append({
                "case_id": cid,
                "expected_category": exp_cat,
                "predicted_category": pred_cat,
                "expected_risk": exp_risk,
                "predicted_risk": pred_risk,
                "expected_review": exp_review,
                "predicted_review": pred_review,
                "latency_ms": elapsed_ms,
            })

        total = len(dataset)
        precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
        f1 = (2 * precision * recall) / (precision + recall) if (precision + recall) > 0 else 0.0
        avg_latency = sum(latencies) / len(latencies) if latencies else 0.0
        total_cost = calculate_cost(name, total_input_tokens, total_output_tokens)
        cost_per_eval = total_cost / total if total > 0 else 0.0

        results[name] = {
            "total_cases": total,
            "category_accuracy": round(cat_matches / total, 4),
            "risk_level_accuracy": round(risk_matches / total, 4),
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1_score": round(f1, 4),
            "true_positives": tp,
            "false_positives": fp,
            "true_negatives": tn,
            "false_negatives": fn,
            "avg_latency_ms": round(avg_latency, 2),
            "min_latency_ms": round(min(latencies), 2) if latencies else 0.0,
            "max_latency_ms": round(max(latencies), 2) if latencies else 0.0,
            "total_cost_usd": round(total_cost, 6),
            "cost_per_evaluation_usd": round(cost_per_eval, 6),
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "details": details,
        }

    # Save benchmark results
    out_file = BASE_DIR / "benchmark_results.json"
    with out_file.open("w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)

    logger.info("Benchmark finished. Results saved to %s", out_file)
    print("\n" + "=" * 80)
    print("RESUMEN DE BENCHMARK COMPARATIVO")
    print("=" * 80)
    print(f"{'Métrica':<30} | {'Reglas Tradicionales':<16} | {'Jev TypeSafe AI':<16} | {'Claude Sonnet':<16}")
    print("-" * 86)
    m_names = [
        ("category_accuracy", "Exactitud Categoría"),
        ("risk_level_accuracy", "Exactitud Nivel Riesgo"),
        ("precision", "Precisión (Review)"),
        ("recall", "Recall de Excepciones"),
        ("false_positives", "Falsos Positivos (FP)"),
        ("false_negatives", "Falsos Negativos (FN)"),
        ("avg_latency_ms", "Latencia Media (ms)"),
        ("cost_per_evaluation_usd", "Costo / Eval (USD)"),
    ]
    for key, label in m_names:
        r_val = results.get("rules", {}).get(key, "N/A")
        j_val = results.get("jev", {}).get(key, "N/A")
        c_val = results.get("claude", {}).get(key, "N/A")
        print(f"{label:<30} | {r_val!s:<16} | {j_val!s:<16} | {c_val!s:<16}")
    print("=" * 80 + "\n")
    return results


if __name__ == "__main__":
    run_benchmark()
