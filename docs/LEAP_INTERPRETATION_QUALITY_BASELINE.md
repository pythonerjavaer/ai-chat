# Leap interpretation quality baseline — 2026-09-25

This is an opt-in live comparison using ten public-domain or synthetic, non-private samples. It covers two literary words, two literary sentences, two philosophy paragraphs, two technical paragraphs, and two finance paragraphs. The full inputs, provider outputs, actual routed models, latency, output length, token usage, and conservative manual review flags are stored in [`docs/evals/leap_interpretation_quality_2026-09-25.json`](evals/leap_interpretation_quality_2026-09-25.json). The reusable input fixture is [`backend/tests/fixtures/leap_interpretation_quality_samples.json`](../backend/tests/fixtures/leap_interpretation_quality_samples.json).

## Results

| Requested model | Structured successes | Mean latency, including failures | Observed stability |
|---|---:|---:|---|
| `openrouter/free` | 7 / 10 | 4.60 s | Fast, but routed across four model families; one answer invented historical background, two valid answers ignored the requested Chinese output, and three calls produced unusable structure. |
| `nvidia/nemotron-3-super-120b-a12b:free` | 5 / 10 | 14.41 s | Structured when successful, but five failures and three successful answers returned English despite the Chinese requirement. |
| `nex-agi/nex-n2.5-mini:free` | 9 / 10 | 3.22 s | Best availability, latency, Chinese consistency, and cross-domain fidelity in this run; one malformed response is covered by the free-router fallback. |
| `qwen/qwen3.8-27b:free` | 0 / 10 in the initial availability probe | < 0.4 s before rejection | Listed publicly but not callable in this environment during the probe, so it was excluded as a production default. |

The dynamic router actually returned `nex-agi/nex-n2.5-mini:free`, `nvidia/nemotron-3-ultra-550b-a55b:free`, `cohere/north-mini-code:free`, and `nvidia/nemotron-3-super-120b-a12b:free`. This demonstrates why the returned `model` field must be retained.

## Decision

Production uses `nex-agi/nex-n2.5-mini:free` as the configured free primary and `openrouter/free` as the only automatic fallback. The fallback is attempted when the fixed free model is unavailable, rate-limited, returns an invalid provider response, or does not return a JSON object. No fallback path can select OpenAI. OpenAI remains a user-selected provider only.

Prompt version `leap-interpret-v3-evidence-zh` separates word, sentence, paragraph, selection, and chapter instructions. It requires concise Simplified Chinese explanations, exact source quotations for evidence, explicit uncertainty, and no external author/history claims. Server normalization removes evidence that is not an exact substring of the supplied source or context and bounds field lengths. A prompt-version and model-policy change produces a new cache identity; translation cache, notes, and reading progress are untouched.

## Reproduce

Run the committed evaluator only when a minimal free-provider quality check is desired:

```bash
OPENROUTER_API_KEY=... python scripts/evaluate_leap_interpretation_quality.py \
  --output /tmp/leap-quality.json
```

The script calls only OpenRouter with the listed public fixture. It contains no OpenAI call path and never writes the key to its output.
