# Examples: The Protocol in Action

These are real cases from independent evaluations of org-as-code (conducted by [Manus AI](https://manus.im)). They demonstrate the P↔V protocol catching errors that would otherwise have shipped to production.

---

## SEC-001: Security Hardening Gone Wrong (Then Right)

**What happened:** An AI architect proposed hardening the MCP server against injection attacks. The proposal contained two architectural errors: it recommended stripping shell metacharacters from commit messages (unnecessary — `subprocess.run` with list args never invokes a shell) and misidentified the path traversal threat model.

**How the protocol caught it:** The V.0 review rejected the proposal with three blockers. The real risk was git trailer injection via newlines, not shell injection. The architect revised the proposal (P.1), the critic approved (V.1), and the builder implemented the correct fix (P.2).

**Result:** Two architectural errors caught before implementation. The correct fix (strip `\n`/`\r` only) shipped instead of a wrong fix (strip all metacharacters, which would have corrupted legitimate commit messages).

**Cycle:** P.0 → V.0 (rejected) → P.1 → V.1 (approved) → P.2 → V.2 (committed) — 6 artifacts, 5 days.

```
examples/SEC-001/
├── P.0_proposal.md          ← Initial (flawed) security proposal
├── V.0_review.yaml          ← Rejection with 3 blockers
├── P.1_revised_proposal.md  ← Corrected approach
├── V.1_review.yaml          ← Approval
├── P.2_implementation.md    ← Final implementation
└── V.2_verification.yaml   ← Human sign-off
```

---

## PERF-001: The Accuracy Cliff-Edge

**What happened:** An AI architect proposed optimizing a document analysis pipeline from 12s to 3s latency. The proposal switched to a smaller embedding model that would drop accuracy to exactly the constraint floor (F1 = 0.91, minimum = 0.91).

**How the protocol caught it:** The V.0 review rejected with critical blockers: shipping at the constraint floor is a cliff-edge, not a margin. Redis failure modes were under-specified. No rollback plan existed. The builder responded with ONNX INT8 quantization (keeping accuracy at 0.925), a full failure mode matrix, and feature-flag rollbacks for every component.

**Result:** A dangerous accuracy trade-off was replaced with a safe acceleration technique. The revised plan maintained a 0.015 F1 safety margin above the floor.

**Cycle:** P.0 → V.0 (rejected) → P.1 → V.1 (committed) — 4 artifacts, 4 days.

```
examples/PERF-001/
├── P.0_proposal.md          ← Initial proposal (accuracy at constraint floor)
├── V.0_review.yaml          ← Rejection: 3 blockers, 3 concerns
├── P.1_implementation.md    ← Revised plan with ONNX INT8, failure matrix
└── V.1_verification.yaml   ← Founder approval with conditions
```

---

## What These Cases Prove

1. **V-steps catch real errors.** Not formatting nits — architectural mistakes that would have caused incorrect security code (SEC-001) or accuracy degradation in production (PERF-001).

2. **Rejection is productive.** Both cases produced better outcomes because the first proposal was rejected. The friction is the feature.

3. **The audit trail is the documentation.** These artifacts are simultaneously the decision record, the architectural rationale, and the compliance evidence. No separate ADR needed.
