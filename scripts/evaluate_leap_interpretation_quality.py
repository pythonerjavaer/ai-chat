#!/usr/bin/env python3
"""Run the opt-in, public-text Leap interpretation quality fixture.

This script calls OpenRouter only. It never reads or calls OpenAI. Set
OPENROUTER_API_KEY in the process environment; the key is never written to the
output. The committed report is a dated snapshot, not a live-network test.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.product_domains.interpretation import InterpretationService, PROMPT_VERSION  # noqa: E402
from backend.product_domains.interpretation_providers import (  # noqa: E402
    InterpretationProviderError,
    OpenRouterInterpretationProvider,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", nargs="+", default=["openrouter/free", "nex-agi/nex-n2.5-mini:free"])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is required in the process environment")
    samples = json.loads((ROOT / "backend/tests/fixtures/leap_interpretation_quality_samples.json").read_text())
    report = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "prompt_version": PROMPT_VERSION, "models": args.models, "samples": []}
    for sample in samples:
        system, prompt = InterpretationService._prompts(sample["scope"], sample["source_text"], sample["context_text"], "完整范围")
        row = {"sample": sample, "results": []}
        for model in args.models:
            provider = OpenRouterInterpretationProvider(api_key, model, fallback_model="")
            started = time.perf_counter()
            try:
                output = provider.generate(0, system, prompt, 700)
                structured = InterpretationService._structured_result(output.text, sample["scope"], sample["source_text"], sample["context_text"])
                row["results"].append({
                    "status": "ok", "requested_model": output.requested_model,
                    "actual_model": output.actual_model,
                    "latency_seconds": round(time.perf_counter() - started, 3),
                    "output_length": len(output.text), "usage": output.usage,
                    "structured": structured, "raw_output": output.text,
                })
            except InterpretationProviderError as exc:
                row["results"].append({
                    "status": "error", "requested_model": model,
                    "latency_seconds": round(time.perf_counter() - started, 3),
                    "error_type": type(exc).__name__, "error_code": exc.code,
                })
            time.sleep(1.2)
        report["samples"].append(row)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
