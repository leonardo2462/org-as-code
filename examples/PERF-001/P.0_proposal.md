# PERF-001: Optimize Document Analysis Pipeline

**Process:** feature
**Agent:** architect
**Date:** 2026-03-10
**H(s) = 0.920 → ESCALATE to founder**

## State

Document analysis pipeline: p95 = 12s, target ≤ 3s. Three enterprise prospects (TotalEnergies, ING, Siemens) cite latency as primary blocker.

```
Stage           | p50   | p95   | CPU%  | Memory
----------------|-------|-------|-------|-------
OCR (Tesseract) | 1.8s  | 3.8s  | 94%   | 2.1GB
NLP extraction  | 0.9s  | 2.1s  | 78%   | 1.4GB
Entity resolve  | 0.7s  | 1.9s  | 45%   | 0.8GB
Embedding       | 1.1s  | 2.4s  | 88%   | 1.8GB
Index write     | 0.3s  | 0.6s  | 12%   | 0.2GB
TOTAL (serial)  | 4.8s  | 10.8s | —     | 6.3GB
```

## Intention

Reduce p95 latency from 12s to ≤3s without reducing accuracy below F1 ≥ 0.91 (current: 0.93) or increasing infra cost by more than 40%.

## Decomposition

| Part | Description | Expected Gain |
|------|-------------|---------------|
| D1 | Replace Tesseract with GPU-accelerated OCR (Textract/EasyOCR) | p95: 3.8s → 0.8s |
| D2 | Parallelize NLP + Entity Resolution (asyncio) | p95: 4.0s → 2.1s |
| D3 | Batch embedding with model switch to all-MiniLM-L6-v2 (5x faster) | p95: 2.4s → 0.4s |
| D4 | Async index writes via Redis queue | p95: 0.6s → 0s (off critical path) |

**D3 risk:** Model switch drops F1 from 0.93 to 0.91 — exactly at constraint floor. Mitigation: A/B test on 10% traffic for 7 days before rollout.

## Expected Result

```
Stage           | p50   | p95
----------------|-------|------
OCR (Textract)  | 0.4s  | 0.8s
NLP + Entity    | 0.6s  | 1.2s
Embedding       | 0.1s  | 0.4s
Index write     | 0s    | 0s
TOTAL           | 1.1s  | 2.4s   ← target ≤3s ✓
```

Additional cost: +$2,810/month (+34%, within 40% ceiling).
