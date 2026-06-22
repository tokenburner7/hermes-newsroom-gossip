# Hermes Newsroom — Adversarial Prompt Review

Reviewer: adversarial prompt engineer. Lens: *what makes the articles boring* (generic
tone, no edge, no crypto depth) and *what lets the model hallucinate or drift*. Scores
are out of 10. Brutally honest, production-grade.

## Summary table

| # | Prompt | File | Score | Impact |
|---|--------|------|------:|--------|
| 1 | `_SYSTEM_PROMPT` (research_synthesis) | pipeline/research.py | 7.5 | MED |
| 2 | `_REPROMPT` | pipeline/research.py | 7.0 | MED (left) |
| 3 | `_EVIDENCE_FOOTER` | pipeline/research.py | 7.0 | MED |
| 4 | `regulatory_signal` persona | pipeline/research.py | 6.0 | HIGH |
| 5 | `market_context` persona | pipeline/research.py | 6.5 | HIGH |
| 6 | `infrastructure_spotlight` persona | pipeline/research.py | 5.5 | HIGH |
| 7 | `prediction_market_signal` persona | pipeline/research.py | 6.5 | HIGH |
| 8 | `weekly_deep_dive` persona | pipeline/research.py | 5.5 | HIGH |
| 9 | `STYLE_GUIDE` | pipeline/draft.py | 8.0 | MED |
| 10 | `_build_system_prompt` base | pipeline/draft.py | 8.0 | MED |
| 11 | `_TYPE_KEY_HINTS` | pipeline/draft.py | 8.0 | LOW (left) |
| 12 | `_HUMANIZE_SYSTEM` | pipeline/humanize.py | 5.0 | HIGH |
| 13 | `_HUMANIZE_INSTRUCTION` | pipeline/humanize.py | 6.0 | HIGH |
| 14 | `X_THREAD_SYSTEM` | distribute/prompts.py | 9.0 | LOW (left) |
| 15 | `TELEGRAM_SYSTEM` | distribute/prompts.py | 7.5 | MED |
| 16 | `_SYSTEM_PROMPT` + `_RUBRIC_BLOCK` (judge) | eval/judge.py | 7.5 | MED |

Mean before fixes: **6.9**. After fixes, the HIGH-impact prompts (4–8, 12–13) are
rebuilt and the targeted MED prompts are sharpened.

---

## Detailed critique

### 1. research `_SYSTEM_PROMPT` (research_synthesis) — 7.5/10
**Strengths:** Real beat (zk, TEEs, on-chain inference, MEV, consensus, mechanism
design). Hard anti-hallucination spine — verbatim hash-locked spans, "drop the claim"
if no span, numbers immutable. Tools-not-memory is explicit.
**Weaknesses:** No *bar* for what claim is worth extracting — "claims that matter" is
undefined, so the model harvests background trivia to clear `MIN_CLAIMS`. `confidence`
field is required but never defined (what is 0.3 vs 0.9?). Crypto implication is asked
for but not made specific — "name a concrete implication" invites "useful for
blockchain."
**Missing:** A load-bearing-claim definition, a confidence scale, implication
specificity (named primitive + mechanism), and a thin-source escape hatch (don't pad).

### 2. research `_REPROMPT` — 7.0/10
**Strengths:** Reasserts legitimacy ("legitimate analysis of public research"), restates
the exact action, names `MIN_CLAIMS`. **Weaknesses:** Slightly generic. **Missing:**
nothing critical — adequate. *Left unchanged (within scope but low marginal value).*

### 3. research `_EVIDENCE_FOOTER` — 7.0/10
**Strengths:** Mirrors the system-prompt evidence rules so every non-synthesis type
inherits the same spine. **Weaknesses:** It dominates the short per-type preambles, and
carries the same gaps as #1 (no implication specificity, no anti-padding). **Missing:**
load-bearing filter + specific-implication rule + units/baseline on numbers.

### 4–8. Per-type research personas — 5.5–6.5/10 (the weakest prompts in the system)
Each is 1–3 sentences. They name a beat but bake in **no real crypto expertise**, which
is exactly what makes the resulting articles boring. Specifics:
- **regulatory_signal (6.0):** No securities-law depth (Howey, security-vs-commodity,
  staking/custody/ETF precedent). "Check Polymarket" implies a tool that may not exist —
  hallucination risk. No routine-vs-precedent triage.
- **market_context (6.5):** Good "never invent a narrative for flat markets." But shallow
  — no dominance, funding/basis, stablecoin flows, or macro (rates/DXY) framing.
- **infrastructure_spotlight (5.5):** Pure checklist ("what it does, the crypto
  connection, caveats"). No category taxonomy, no shipped-vs-roadmap discipline, no
  definition of what a *real* crypto connection is.
- **prediction_market_signal (6.5):** Good 24h/7d comparison, but assumes all snapshots
  exist and never asks for resolution criteria or liquidity context (thin-book noise).
- **weekly_deep_dive (5.5):** "The thread connecting developments" — no demand for an
  actual thesis, no quiet-week escape hatch, so it defaults to a roundup.
**Missing across all five:** domain depth, edge-case handling, and a "don't manufacture
drama" instruction.

### 9. draft `STYLE_GUIDE` — 8.0/10
**Strengths:** Best prompt in the repo. "Do not explain basics," concrete tonal
contrasts ("340ms overhead on geth v1.14" not "low latency"), banned hype words, the
"one 'This'-sentence per paragraph" anti-AI tell. **Weaknesses:** No **thesis/angle**
mandate — the #1 source of boring is neutral recitation, and the guide never says "have
a take." Banned-opener list is one item. All-caps LEDE/BODY beats risk literal robotic
headers. **Missing:** angle requirement, expanded banned openers, sentence-rhythm rule,
anti-restatement rule.

### 10. draft `_build_system_prompt` base — 8.0/10
**Strengths:** Tight hard rules (only provided claims, no new facts, immutable numbers,
`[^claim_N]` markers), explicit JSON schema with inline comments. **Weaknesses:** No
guidance when claims are too thin for 400–700 words → invites padding. No thesis
requirement. **Missing:** anti-padding edge case + one-argument mandate.

### 11. draft `_TYPE_KEY_HINTS` — 8.0/10
Concrete, well-scoped JSON hints. `probability_trajectory`/`market_snapshot` shapes are
good. *Left unchanged (low impact).*

### 12. humanize `_HUMANIZE_SYSTEM` — 5.0/10
One bland line ("You are a senior copy editor"). No voice, no standard, no mandate to
actually de-AI the prose. **Missing:** an editor persona with an ear for machine tells.

### 13. humanize `_HUMANIZE_INSTRUCTION` — 6.0/10
**Strengths:** Immutability constraints are airtight; citation markers preserved; clean
output contract. **Weaknesses:** Its entire job is to make prose sound human, yet it
gives **zero** guidance on what "natural" means or which AI tells to remove ("improve
flow, sentence variety, and tone" is vague). At temp 0.7 with vague instructions it
risks drift (then discarded — wasted spend). **Missing:** a concrete AI-tell removal
list (formulaic transitions, rule of three, "not just X but Y," inflated vocabulary,
em-dash overuse, vague attributions, nominalizations) and a "less generic, not more"
guardrail so it doesn't smooth the edge out of the copy.

### 14. distribute `X_THREAD_SYSTEM` — 9.0/10
**Strengths:** Excellent. "The thread IS the product," A/B hook variants, concrete
example, banned filler, char cap, ground-truth rule, strict JSON. **Weaknesses:** The
fixed 8-tweet skeleton can feel formulaic. *Left unchanged (already strong).*

### 15. distribute `TELEGRAM_SYSTEM` — 7.5/10
**Strengths:** Concise, scannable, char caps, no-hype, no-invented-stats. **Weaknesses:**
No persona/voice line, generic bullet-3, no concrete example to anchor specificity.
**Missing:** voice + an example like the X prompt has + a "state the direction if
unsure" hallucination guard.

### 16. eval `_SYSTEM_PROMPT` + `_RUBRIC_BLOCK` (judge) — 7.5/10
**Strengths:** Independent-judge framing ("did not write it… not to be charitable"),
weighted criteria, "be harsh on accuracy/citation_integrity," strict JSON, the clever
"no double-quotes in rationale" hook for the salvage parser, deterministic weighting in
Python. **Weaknesses:** **No score anchors** — a 0–1 scale with only "1.0 excellent /
0.0 unacceptable" makes the judge cluster around 0.7 and become noise; this is the
single highest-leverage judge fix. `originality` (the boring-detector) is only lightly
described. **Missing:** per-band anchors (0.8/0.5/0.2) and a sharper originality
definition that explicitly punishes flat summaries and "useful for blockchain."

---

## Top 5 structural issues (across all prompts)

1. **No "boring" defense.** Only the X thread and style guide push for edge/specificity.
   Research personas, humanize, and the judge rubric do nothing to demand a *thesis* or
   punish flat summary — so the pipeline's default output is a competent, lifeless recap.
2. **"Specific crypto implication" is asserted, never operationalized.** Every stage
   repeats the synthesis-not-summary mantra but none shows what a *named-primitive +
   mechanism* implication looks like, so models satisfy it with vague gestures.
3. **Per-type personas are too thin to carry domain expertise.** 1–3 sentences cannot
   substitute for a real analyst's priors. This is where crypto depth should live and
   it is nearly empty.
4. **No score anchors / calibration in the judge.** The gate decision (escalate < 0.80)
   rides on an uncalibrated 0–1 scale that drifts to the mean.
5. **The humanizer is toothless.** The one stage whose literal purpose is to remove AI
   tells lists none of them, so it can only fix "flow" while leaving the giveaways.

---

## Ranked change list (by impact)

| Rank | Change | Prompt(s) | Status |
|-----:|--------|-----------|--------|
| 1 | Rebuild humanize prompt with concrete AI-tell removal list + real editor persona | 12, 13 | APPLIED |
| 2 | Rewrite all 5 per-type research personas with crypto depth + edge cases + anti-drama | 4–8 | APPLIED |
| 3 | Add score anchors + sharpen `originality` in judge | 16 | APPLIED |
| 4 | Add load-bearing-claim bar, confidence scale, specific-implication rule, anti-padding to research system prompt + footer | 1, 3 | APPLIED |
| 5 | Add thesis mandate, expanded banned openers, rhythm/anti-restatement to STYLE_GUIDE | 9 | APPLIED |
| 6 | Add anti-padding + one-argument mandate to draft base prompt | 10 | APPLIED |
| 7 | Add voice + concrete example + direction-not-fabrication to Telegram prompt | 15 | APPLIED |
| — | Reprompt, type hints, X thread | 2, 11, 14 | left (adequate/strong) |

All edits modify **string content only** — every variable (`%(min_claims)d`,
`{article_type}`, `{body}`, `{extra_block}`, JSON schema keys/braces, `[^claim_N]`) is
preserved.
