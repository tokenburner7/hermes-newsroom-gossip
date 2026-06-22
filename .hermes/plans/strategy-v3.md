# Strategic Outline v3 — Hermes Newsroom

> **Status:** For review. Resolves the Grok-proposed pivot. Supersedes the strategic framing in distribution-plan-v2.md without replacing its task-level execution.

---

## 0. Meta-Review — Assessing Both Reviews

### Hermes's review (my own)

**What I got right:** Sequencing is the fatal question and Grok gets it wrong. Building a multi-tenant platform for zero users is premature scaling. The components list ignores the existing codebase. The vision is strong and belongs in the INVEST backlog.

**What I got wrong:**
1. **Human review.** I claimed human review contradicts provenance. Opus correctly pointed out this conflates *editing claims* (bad, breaks hash-locks) with *accept/reject gating* (fine, preserves provenance). For B2B buyers, "machine-generated, provenance-locked, human-signed-off" is strictly stronger. This was a mistake — I rejected the steelman.
2. **Sequencing score too harsh.** Grok's prose defers multi-tenancy to last ("launch a few niches, prove the model, *then* productize"). The inversion is in the component list, not the narrative. 3/10 is fairer than 2/10.
3. **Missed the targeting error entirely.** I critiqued Grok's build order but didn't interrogate whether the v2 plan's *own* experiment design was aimed at the right market. Retail crypto-Twitter is the wrong primary audience for a provenance/auditability moat. This was the most important strategic finding and Opus found it, not me.

### Opus's review

**What Opus got right:**
1. **The market targeting error.** This is the core contribution. The plan runs its primary experiment against the audience least likely to pay for the actual differentiator, and demotes the high-fit audience to a post-failure fallback. This is the rightest thing in either review.
2. **The confounding problem.** A single-niche, consumer-only experiment can't distinguish "product has no demand" from "crypto retail doesn't value it" from "distribution execution was weak." Only the first should kill the project. Adding a parallel B2B arm disambiguates.
3. **44% of Grok's components already exist.** Mapped to the codebase and documented. This is the kind of specific refutation that should end the "build now" argument.
4. **Correcting my human review error.** Accept/reject gating preserves provenance; editing doesn't. Clear, correct, important.
5. **The two cheap dual-purpose additions.** Per-distribution metrics on the existing table, and naming `channel` as an extension seam. Both serve the experiment now and the platform later, at near-zero cost.

**What Opus could have been sharper on:**
1. The parallel B2B arm is described conceptually but not operationally. Which tasks change in the v2 plan? Which specific outreach goes out on which day? The concept is right; the task-level mapping is missing.
2. The INVEST thresholds critique ("aggressive for a cold-start account") is an observation without a concrete alternative. Should thresholds be lowered, or should the gate logic change?

### Resolution

Opus's review was stronger than mine — it found the thing I missed and correctly challenged my errors. The synthesis is:

> **Hermes is right about *when* (validate before building). Grok is right about *who* (professional buyers, not retail). Opus is right about *where the current plan is wrong* (aimed at the wrong primary audience).**

---

## 1. Strategic Position (Resolved)

| Question | Answer |
|----------|--------|
| Execute the v2 distribution plan? | **Yes.** It remains the execution spine. |
| Adopt Grok's build order? | **No.** Platform infrastructure waits for demand signal. |
| Adopt Grok's customer read? | **Yes.** The moat (provenance/auditability) monetizes with professionals, not consumers. |
| Change the v2 plan? | **Two cheap additions.** Per-distribution metrics + channel seam. B2B outreach and human review are on-deck, not built yet. |
| Build Grok's 9 components? | **Three** already exist. **Two** are in the plan. **One** gets a cheap seed. **Three** (dashboard, topic onboarding, multi-tenancy) are parked behind the INVEST gate. |

---

## 2. The Two Reviews Agree — Grok's Vision Parked, Not Rejected

Grok's vision ("run the pipes for specialized knowledge communities") is the right destination. It is parked behind the Day-14 INVEST gate as a clearly-defined backlog:

| Component | Current state | When to build |
|-----------|--------------|---------------|
| Modular Content Pipeline | Already exists | — |
| Configuration Layer | Already exists (single-tenant) | Generalize after 2+ verticals validated |
| Monitoring & Quality Control | Already exists (circuit breakers, drift, health, kill-switch) | — |
| Data Sources Integration Layer | Already exists (9 connectors with normalized upsert) | — |
| Distribution Hub | v2 plan builds minimal version (X + Telegram + RSS) | Extend per-channel, demand-pulled |
| Analytics & Intelligence | v2 adds per-distribution metrics (cheap seed) | Full layer after INVEST |
| Central Dashboard | Does not exist | **Parked** — after 3+ paying customers |
| Topic Onboarding Engine | Does not exist | **Parked** — after second vertical validated |
| Multi-Tenant Architecture | Does not exist | **Parked** — after third vertical validated |

---

## 3. What Changes in the v2 Plan (2 Modifications)

### Change 1: B2B outreach — on-deck, not in-flight

**Decision:** Do not send outreach messages during the initial 14-day experiment. Focus the experiment on consumer distribution only. Keep the B2B capability fully documented and ready to deploy from the Day-14 decision gate.

**Rationale:** The targeting critique from Opus stands — the moat monetizes with professionals, not consumers — but splitting focus across two audiences in a 14-day experiment dilutes both. Run the consumer experiment clean. If consumer metrics are inconclusive (AMBIGUOUS-extend), the B2B arm is ready to deploy as the "flip one variable" choice. If consumer metrics are red, the v2 plan's KILL → B2B pivot logic handles it. The targets, template, and gating criteria are documented in §5 (On-Deck Capabilities) — ready, not executing.

### Change 2: Add two cheap dual-purpose additions

**2a. Per-distribution performance metrics on the `distributions` table.**

Add two columns: `impressions` (nullable Integer) and `link_clicks` (nullable Integer). Filled during the daily runbook from X/Telegram analytics.

Why this matters now: the plan rotates 3 A/B hook variants but only logs aggregate medians to a flat CSV. Per-row metrics are what actually let you pick the winning hook. The AMBIGUOUS-extend rule in §6 says "lock to the A/B hook variant with the best week-1 engagement" — the `metrics.csv` cannot answer which hook is best. Per-row metrics can.

Why this matters later: this is the literal seed of Grok's analytics layer. Same table, more rows, no re-architecture.

**Task:** Add to the Alembic migration in Task 1.2:
```python
sa.Column('impressions', sa.Integer(), nullable=True),
sa.Column('link_clicks', sa.Integer(), nullable=True),
```
Add to the `Distribution` model:
```python
impressions: Mapped[int | None] = mapped_column(Integer)
link_clicks: Mapped[int | None] = mapped_column(Integer)
```
Update the Phase 3 daily runbook to fill these columns after posting.

Cost: ~15 additional minutes in Task 1.2. Zero risk.

**2b. Name `channel` as an intentional extension seam.**

The `distributions.channel` column is already a free-text discriminator (`'x'`, `'telegram'`). Documenting it as an extension seam means adding Discord, webhooks, or an API feed later becomes "a new `channel` value + a poster script" — not a re-architecture.

**Task:** Add a comment in `models.py` above the `channel` field:
```python
channel: Mapped[str] = mapped_column(Text, nullable=False)
# Extension seam: new distribution targets = new 'channel' values (e.g. 'discord',
# 'webhook', 'api') + a corresponding poster — no schema change needed.
```

Cost: 2 minutes. Already exists.

### Change 3: Human review — future toggle, default off

**Decision:** Accept/reject gating does not contradict provenance — Opus was correct. But do not build it now. Reserve as a future feature flag (`human_review_enabled: bool = False` in config), to be implemented in the INVEST phase for the B2B feed where "machine-generated, provenance-locked, human-signed-off" is a selling point. For the initial experiment: off. Human *editing* of claims remains forbidden — it breaks hash-locks.

**Correction to the v2 guardrails (§7):** The v2 plan's guardrails should note that accept/reject gating is provenance-safe, but no code for it is built in this phase.

---

## 4. What Does NOT Change

Everything else in distribution-plan-v2.md stays exactly as written:

- Phase 0 (deploy) — unchanged
- Phase 1 (distribution pipeline) — unchanged except the two cheap additions in Task 1.2
- Phase 2 (audience capture) — unchanged
- Phase 3 (experiment) — unchanged (consumer-only, single-track)
- All Claude Code dispatch commands — unchanged
- All verification commands — unchanged
- The additive-only architecture — unchanged
- The generation/posting split — unchanged
- The static SSG invariant — unchanged
- The zero-infrastructure-spend constraint — unchanged

---

## 5. On-Deck Capabilities (Designed, Documented, Not Executing)

These are capabilities the strategy acknowledges as correct directionally but gates behind the Day-14 decision. They are fully specified so the INVEST / AMBIGUOUS-extend decision can deploy them immediately.

### 5.1 B2B Outreach (triggered by KILL or AMBIGUOUS-extend)

If the Day-14 consumer metrics are red or ambiguous, deploy B2B outreach as the fallback probe. Targets are the provenance-prizing research desks where the moat is worth money:

| # | Target | Site | Why a fit | Contact channel |
|---|--------|------|-----------|-----------------|
| 1 | Messari | messari.io | Sells data products; values verified feeds | X DM / contact form |
| 2 | The Block Research | theblock.co/research | Sells research + data dashboards | research contact form |
| 3 | Coin Metrics | coinmetrics.io | Network/market data; research arm | contact form |
| 4 | Kaiko | kaiko.com | Market-data provider; "verified feeds" | sales form |
| 5 | a16z crypto | a16zcrypto.com | Engineering-savvy fund; prizes provenance | X DM |
| 6 | Galaxy Research | galaxy.com/research | Institutional research | press inbox |
| 7 | Delphi Digital | delphidigital.io | Members-funded research desk | contact form |
| 8 | Nansen | nansen.ai | On-chain analytics; research-driven | contact form |
| 9 | Glassnode | glassnode.com | On-chain data + Insights research | contact form |
| 10 | Pantera Capital | panteracapital.com | Publishes Blockchain Letter; thesis-driven | contact form |

**Outreach template:**
> We run an autonomous research desk that turns frontier AI/security papers into crypto-implication briefs within ~3 hours of publication — and every claim is hash-locked (SHA-256) to its exact source span, so it's auditable, not vibes. I think [TARGET]'s [research/data] team could use this as a verifiable, fast AI×crypto signal feed. Worth a 15-minute call to show you the provenance trail on a live example?

**B2B gating criteria (for when deployed):** 0 replies = no signal. 1 reply = probe further. ≥2 replies of genuine interest = strong INVEST signal, weighted above any single consumer metric.

### 5.2 Human Review Toggle (INVEST phase B2B feature)

A feature flag (`human_review_enabled: bool = False` in config) that adds an accept/reject gating step before publish. When enabled, articles are held in `status='pending_review'` until a human approves or kills them. Accept/reject gating does not modify claims and therefore does not violate provenance hash-locks. Default: off. Target use case: B2B feed where "machine-generated, provenance-locked, human-signed-off" is a compliance selling point.

### 5.3 Grok's Platform Vision (INVEST backlog)

Grok's 9-component architecture is the INVEST-phase backlog. The 3 genuinely new components (Central Dashboard, Topic Onboarding Engine, Multi-Tenant Architecture) are parked behind validated demand — at minimum, one successful consumer vertical and one B2B customer expressing willingness to pay.

---

## 6. Execution Order (Updated)

```
                          NOW
  Task 0.0  Sanity check (site builds)
  Task 0.1–0.6  Phase 0: Deploy site to Cloudflare Pages
  Task 1.1–1.8  Phase 1: Distribution pipeline
       ↑ includes per-distribution metrics (Change 2a) and channel seam (Change 2b)
  Task 2.1–2.4  Phase 2: Email capture + RSS
  Task 3.1      Metrics tracker
                          │
                          ▼
                  WEEK 1–2 CONSUMER
                  14 days · ~14 threads
                  daily posting + engagement
                  metrics logged to CSV
                  B2B outreach: ON DECK (not executing)
                          │
                    ┌─────┴─────┐
                    ▼           ▼
              DAY 7 REVIEW   DAY 14 GATE
              (course check)  Apply v2 decision logic
                              Write decision.md
                                    │
                    ┌───────────────┼───────────────┐
                    ▼               ▼               ▼
                  INVEST          AMBIGUOUS        KILL
               Build platform   Extend 7 days    → Deploy B2B
               + scale up       flip 1 var       outreach from
                                (B2B outreach    §5.1 on-deck
                                 is one option)   → if 0 replies
                                                 after 10 targets:
                                                 shut down
```

---

## 7. Summary of Changes from v2 → v3

| # | What | Type | Impact |
|---|------|------|--------|
| 1 | B2B outreach gated behind Day-14 decision (on-deck, not in-flight) | Sequence | No execution during experiment; fully documented in §5.1 for immediate deployment from the gate |
| 2a | Per-distribution performance metrics on `distributions` table | Additive | 15 min task change; enables hook A/B winner selection |
| 2b | `channel` column documented as extension seam | Documentation | 2 min; already exists |
| 3 | Human review defined as future toggle, default off, not built now | Design | No code change; INVEST-phase feature |

**Everything else in distribution-plan-v2.md: unchanged.**

---

## 8. What Needs Updating (for build execution)

| File | Action |
|------|--------|
| `distribution-plan-v2.md` | No rewrite needed. This strategy-v3.md is the overlay. |
| `BUILD-PROMPT.md` | Minor: add the two `distributions` columns (`impressions`, `link_clicks`) to Task 1.2 instructions. |
| `alembic/versions/b2c3d4e5f6a7_add_distributions.py` | Add `impressions` and `link_clicks` columns. |
| `src/newsroom/models.py` | Add `impressions` and `link_clicks` fields + channel seam comment. |

**No changes needed for B2B or human review — those are on-deck, not built.**

---

**Approved. Ready to build. Execute BUILD-PROMPT.md with the two cheap additions from Change 2.**
