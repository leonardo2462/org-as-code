# Design Rationale

This document explains the design choices behind org-as-code. It's optional reading — the README covers everything you need to use the tool.

---

## The core loop

org-as-code enforces a simple rhythm: propose something (P-step), then validate it (V-step), repeat until it's good enough. This isn't novel — it's how peer review works, how iterative design works, how gradient descent works. We just made it explicit and measurable.

The loop prevents two failure modes that show up when AI agents work unsupervised: unbounded divergence (generating proposals forever without converging) and premature convergence (rubber-stamping the first idea without challenge).

---

## Two scores, two questions

### H(s) — what to work on next

```
H(s) = w₁·urgency + w₂·commitment + w₃·demand + w₄·blocking
```

A linear weighted sum that ranks processes by importance. Simple scheduling heuristic. Weights are configurable in `protocol/config.yaml`.

### E(x) — how close to done

```
E(x) = w_g·gaps² + w_i·inconsistencies² + w_u·uncertainty² − w_e·evidence²
```

A quadratic score that measures remaining tension in a process. Computed at every V-step.

**Why quadratic?** A linear score treats three small gaps the same as one big gap (3 × 0.3 = 0.9 vs 1 × 0.8 = 0.8). In practice, one critical blocker is far more dangerous than several minor concerns. The quadratic penalty captures this: 0.8² = 0.64 dominates 3 × 0.3² = 0.27. This is a common pattern in engineering — loss functions, power dissipation (P = I²R), kinetic energy (½mv²) all use squared terms for similar reasons.

---

## The audit trail

Every P-step and V-step is appended to `artifacts.jsonl` as a hash-chained entry:

- `prev_hash` links to the previous entry
- `entry_hash` = SHA-256 of content + previous hash
- Append-only — entries are never modified or deleted

This gives you a tamper-evident (not tamper-proof) record of every decision, rejection, and revision. You can trace any committed process back through its full history.

Verify integrity: `python org_cli.py verify`

---

## Convergence tracking

E(x) measured over successive V-steps tells you whether the loop is actually working:

```
V.0: E(x) = 0.42  (major gaps found)
V.1: E(x) = 0.18  (gaps addressed, some uncertainty remains)
V.2: E(x) = 0.06  (ready to commit)
```

Decreasing E(x) means convergence. Flat E(x) means the corrections aren't hitting the real problems. Increasing E(x) means stop iterating and rethink the approach.

---

## Where this comes from

The propose-validate-measure-repeat structure shows up across many domains — control theory, iterative optimization, scientific method, thermodynamic equilibration. We noticed the pattern, formalized it as the SYNTRIAD metapattern, and built tooling around it.

The physics analogies (Hamiltonian, energy, convergence) influenced the naming and some design choices. They're useful as engineering intuitions, not as claims about organizational physics. The code works the same whether or not you find the analogies compelling.

For the formal treatment:

- [SYNTRIAD Genesis](https://github.com/SYNTRIAD/genesis) — The metapattern: T : (S, I, C) → S'
- [Digit Dynamics](https://github.com/SYNTRIAD/digit-dynamics) — Mathematical validation via GPU-verified universal attractors
- [Semantic Thermodynamics](https://zenodo.org/records/17618208) — Formal paper on the energy functions
