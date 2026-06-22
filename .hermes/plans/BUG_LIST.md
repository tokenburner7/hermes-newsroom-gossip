# Distribution System — Bug Hunt

**Scope:** the newly-built distribution stage (config block, `Distribution` model + migration,
`distribute/` package, `distribute` CLI command, Temporal `distribute_activity`, `post_thread.py`,
`pipeline_cycle.sh`, `arxiv_ingest.sh`) plus the integration points it touches (LLM client, publish
stage, db session factories).

**Method:** read every listed file end-to-end, traced the generation → persist → post flow, and
cross-checked the ORM model against the migration and the idempotency / budget / kill-switch paths.
Nothing was modified.

## Summary

| Severity | Count |
|----------|-------|
| CRITICAL | 1 |
| HIGH     | 3 |
| MEDIUM   | 6 |
| LOW      | 8 |
| **Total**| **18** |

> Note on a non-bug that was explicitly flagged for checking: `DistributeResult.skipped` **is**
> safely initialized (`field(default_factory=list)`, `repackage.py:93`), so the CLI loop
> `for ch in result.skipped` (`cli.py:605`) can never hit `None`. Likewise `distributions.id`
> auto-generates correctly — a single-column `BigInteger` PK resolves to `autoincrement="auto"`
> (BIGSERIAL) exactly like every other table, so inserts that omit `id` are fine. The alembic
> revision chain is linear with a single head (`b2c3d4e5f6a7` → `a1b2c3d4e5f6` → … → initial).

---

## CRITICAL

### C1 — `distribute_activity` timeout/crash fails the whole workflow *and* skips spend reconciliation
- **File:** `src/newsroom/workflows.py:536-547` (call site), `382-400` (activity), `403-415` (settle)
- **Description:** `distribute_activity` is documented as best-effort ("distribution must never fail a
  run", line 398) and internally wraps everything in `try/except Exception`. But that `try/except`
  only catches exceptions raised *inside the Python function*. A Temporal `start_to_close` timeout
  (`_LLM_TIMEOUT` = 5 min) or a worker crash is enforced by the Temporal server and surfaces as an
  `ActivityError` at the **call site** (`workflows.py:538`), which is **not** wrapped in
  `try/except` (unlike the `research` step at 463-473). After `_IO_RETRY` (3 attempts) is exhausted
  the unhandled `ActivityError` fails the entire `PipelineWorkflow` — for a run whose article was
  already published and persisted. Worse, the distribute step runs at **9b, before** `settle_activity`
  (step 10, line 544). So when distribute fails, `settle` never executes and the budget reservation
  made in `start_run_activity` (`reserve(est_usd=0.02)`, line 133) is **never reconciled**. Unsettled
  reservations accumulate in `budget_day.reserved_usd`; over time `reserve_budget()` (the SQL function
  at `3e1fe4d03ee4_initial_schema.py:57`) starts denying new runs even though actual spend is tiny —
  silent budget-state corruption from a "best-effort" step.
- **Reproduction:**
  1. Run `PipelineWorkflow` with `cfg.publish=True` on an article that passes the fact gate.
  2. Make the distribute LLM call hang past 5 min (slow provider) or kill the worker mid-`distribute_activity`.
  3. Observe: workflow ends `failed`; `runs`/budget show the research reservation still in
     `reserved_usd`, never moved to `actual_usd`.
- **Fix:** wrap the distribute call in `try/except ActivityError` and swallow it (log only), mirroring
  the research DLQ pattern; and move `settle_activity` **before** the distribute step (or make settle
  run unconditionally in a `finally`-style path) so spend is always reconciled regardless of
  distribution outcome. Optionally give distribute its own short non-retryable policy.

---

## HIGH

### H1 — No unique constraint on `(article_id, channel)`; idempotency guard is TOCTOU
- **File:** `alembic/versions/b2c3d4e5f6a7_add_distributions.py:28-45` (only a non-unique
  `ix_distributions_article_id`), `src/newsroom/distribute/repackage.py:144-155` + `234-245`
- **Description:** The module docstring promises idempotency that is "safe to re-run without piling up
  duplicate payloads" for "the 3x/day cron and the Temporal activity" (`repackage.py:255-257`). But
  `_already_distributed` (check) and `_persist` (insert) run in **separate sessions/transactions**,
  and the table has **no unique constraint** backing the guard — only a plain index. Two distributors
  running concurrently (e.g. the Temporal `distribute_activity` and the shell-cron `newsroom distribute`
  in `pipeline_cycle.sh:49`) can both pass the check and both insert, producing duplicate `generated`
  rows for the same `(article, channel)`. The guard works only single-threaded.
- **Reproduction:** Invoke `distribute_article(id, ("x",))` twice concurrently for the same published
  article → two `channel='x'` rows. (Sequentially the second is correctly skipped.)
- **Fix:** add a partial unique index, e.g.
  `CREATE UNIQUE INDEX ... ON distributions (article_id, channel) WHERE status IN ('generated','posted')`,
  and treat a unique-violation on insert as "already distributed" (catch `IntegrityError`, rollback,
  return the existing id).

### H2 — `distribute` CLI / `post_thread` ignore the kill-switch and never meter LLM spend against the budget
- **File:** `src/newsroom/cli.py:577-580` (only checks `distribution_enabled` + providers),
  `src/newsroom/distribute/repackage.py:191-229` (LLM calls), `src/newsroom/workflows.py:382-400`
- **Description:** Every other LLM path reserves/settles budget and refuses to run while the
  kill-switch is tripped (`run-once` checks `kill_switch_active()` at `cli.py:708`; `start_run_activity`
  at `workflows.py:119`). The distribution path does **neither**: `distribute_article` makes 2 LLM
  calls (`generate_x_thread`, `generate_telegram`) with no `kill_switch_active()` check and never
  records the cost in `spend_ledger`/`budget_day`. The token counts are stored on the `Distribution`
  row for analytics (`in_tokens`/`out_tokens`) but bypass the daily ceiling (`daily_ceiling_usd`,
  O-C3) entirely. So `newsroom distribute` keeps spending real money even when the global safety
  kill-switch is ON.
- **Reproduction:** `newsroom killswitch on --reason test`, then `newsroom distribute --latest` → the
  LLM is still called and money spent; `newsroom budget` shows no corresponding spend.
- **Fix:** in the distribute CLI command and the activity, gate on `kill_switch_active()` like
  `run-once`, and route distribution spend through `reserve()`/`settle()` (or at least `settle()` to
  ledger it) so it counts toward the ceiling.

### H3 — `post_thread.py` partial failure orphans live tweets and the row falls outside the dedup guard (duplicate-thread risk)
- **File:** `scripts/post_thread.py:122-138`, interacting with
  `src/newsroom/distribute/repackage.py:144-155`
- **Description:** When some tweets post and a later one fails, the row is set to `status="failed"`
  with `external_url` = the live thread root (lines 130-138). Two problems: (1) the already-posted
  tweets are now **live on X** but the row says `failed`, with no clean recovery path (`--latest`
  only selects `status='generated'`, line 69, so the partial can't be resumed); (2) `_already_distributed`
  only counts `('generated','posted')`, so a `failed` row makes the article look **not distributed** —
  a subsequent `newsroom distribute <id> --force` creates a fresh `generated` row whose
  `post_thread --latest` then **re-posts the entire thread**, duplicating the (already partially live)
  content publicly.
- **Reproduction:** Force `_xurl_post_tweet` to raise on tweet 3 of 10 → 2 tweets live, row `failed`.
  Then `newsroom distribute <id> --force` + `post_thread --latest` → full thread posted again on top
  of the orphaned 2 tweets.
- **Fix:** record posted tweet ids on the row (so a partial can resume from the last id), keep a
  recoverable status distinct from a clean `failed`, and have the dedup guard treat a row that has any
  posted tweet ids / `external_url` as "already touched" so a forced re-gen cannot blind-repost.

---

## MEDIUM

### M1 — `distribute` CLI does not catch `LLMError`; an LLM failure dumps a raw traceback
- **File:** `src/newsroom/cli.py:592-600`
- **Description:** The command catches only `(LookupError, ValueError)`. `generate_x_thread` /
  `generate_telegram` raise `LLMError` (a `RuntimeError`, see `llm/client.py:50`) when no provider
  succeeds or the breaker is open. That escapes the handler and prints an unfriendly traceback,
  unlike `test-llm` which correctly catches `LLMError` (`cli.py:345`).
- **Reproduction:** Configure a bad/expired key (or trip the provider breaker) and run
  `newsroom distribute --latest` → traceback instead of a clean "distribute failed:" message.
- **Fix:** add `LLMError` to the `except` tuple in the distribute command (import it at the top of the
  function as `test-llm` does).

### M2 — `_parse_json` does not validate that the result is a JSON object
- **File:** `src/newsroom/distribute/repackage.py:180-186`, used at `203` and `224`
- **Description:** `_parse_json` is annotated `-> dict` but returns whatever `json.loads` yields. If a
  model returns a JSON array or scalar (`[...]`, `"..."`, `123`), the subsequent `data.get("hooks")`
  / `data.get("bullets")` raises `AttributeError`, which is **not** a `ValueError`/`LookupError`, so
  it escapes the CLI handler (M1) as a traceback and is only masked in the best-effort activity.
  Boundary data from the LLM should be validated. (`json_object` mode usually forces an object, but
  failover providers/edge cases are not guaranteed.)
- **Reproduction:** Stub the LLM to return `"[]"` → `AttributeError: 'list' object has no attribute 'get'`.
- **Fix:** after parsing, `if not isinstance(data, dict): raise ValueError("expected JSON object")`,
  or coerce to `{}`.

### M3 — X thread body-tweet count is never validated or warned (thread can silently be < 10 tweets)
- **File:** `src/newsroom/distribute/repackage.py:204-209` and `262-274`
- **Description:** The X system prompt demands exactly 8 body tweets (`prompts.py:38`) for a 10-tweet
  thread, but generation only truncates with `[:8]` and never checks for a short result. Telegram
  *does* warn on bullet-count mismatch (`repackage.py:282-283`); X has no equivalent. A model that
  returns 4 body tweets yields a 6-tweet "thread" with no warning. The `X_THREAD_LEN = 10` constant
  (line 25) that would express this invariant is defined but never used.
- **Reproduction:** Stub the LLM to return 3 body tweets → row persisted, CLI prints a 5-tweet thread,
  no warning.
- **Fix:** add a `warnings.append(...)` when `len(body_tweets) != 8` (mirror the telegram check) and
  actually use `X_THREAD_LEN` in the assembled-length assertion/warning.

### M4 — Misleading green "distributed" output when every channel was skipped
- **File:** `src/newsroom/cli.py:602-606`
- **Description:** When all requested channels are already distributed (idempotent skip, no `--force`),
  `result.distribution_ids` is empty and `result.skipped` is full, yet the command still prints the
  green `distributed article_id=… → url` header before the `↷ already distributed` lines. The headline
  output asserts work was done when nothing was generated.
- **Reproduction:** `newsroom distribute <id>` twice → the second run prints the green "distributed"
  banner despite skipping everything.
- **Fix:** branch the summary line on `result.distribution_ids` (e.g. print "nothing to do —
  already distributed" when it's empty).

### M5 — `pipeline_cycle.sh` can die silently: `set -e` + `pipefail` + `2>/dev/null` on the helper
- **File:** `scripts/pipeline_cycle.sh:9`, `23-29`
- **Description:** `latest_published_id()` runs `uv run python -c '…' 2>/dev/null | tail -1`. Under
  `set -euo pipefail`, if the python call fails (DB unreachable, import error), `pipefail` propagates
  its non-zero status through the pipe, and the command-substitution assignment `BEFORE_ID="$(…)"`
  then trips `set -e` and exits the whole script — while `2>/dev/null` has already discarded the
  error, and `exec >> log` means there is no diagnostic in the log either. The cycle dies before the
  intentionally-tolerant `|| echo WARN` guards on `ingest-all`/`run-once` ever run. The "empty string
  if none" intent only holds when python exits 0; a real error becomes a silent abort.
- **Reproduction:** Stop Postgres, run the script → it exits immediately with no useful log line.
- **Fix:** capture without killing the script, e.g. `BEFORE_ID="$(latest_published_id || true)"`, and
  drop `2>/dev/null` (or tee stderr to the log) so failures are visible.

### M6 — Telegram payload is persisted (and reported as success) even with zero bullets
- **File:** `src/newsroom/distribute/repackage.py:225-229`, `276-287`
- **Description:** If the model returns no usable bullets, `tg.bullets == []`; the code emits a warning
  but still persists a `generated` telegram row whose rendered body is just the header + links with no
  content (`_telegram_render` produces an empty bullet block). The distribution counts as done and the
  idempotency guard will then *skip* regeneration, so a contentless post is what an operator would ship.
- **Reproduction:** Stub the LLM to return `{"bullets": []}` → a `generated` telegram row with an
  empty body; re-running without `--force` won't fix it.
- **Fix:** treat an empty-bullets result as a generation failure (raise/`skip` rather than persist), so
  it is retried instead of silently locked in.

---

## LOW

### L1 — Duplicate `categories` column on the `Source` model
- **File:** `src/newsroom/models.py:46` and `:58`
- **Description:** `categories: Mapped[list[str] | None] = mapped_column(ARRAY(Text))` is declared
  twice in `Source`; the second silently shadows the first. Harmless at runtime but a clear
  copy-paste smell that can confuse readers and tooling. (In a reviewed file though on `Source`, not
  `Distribution`.)
- **Fix:** delete the duplicate declaration.

### L2 — `distributions.created_at` is nullable in the migration but non-optional in the ORM
- **File:** `alembic/versions/b2c3d4e5f6a7_add_distributions.py:42` vs `src/newsroom/models.py:259-261`
- **Description:** The migration omits `nullable=False` on `created_at` (every other table sets it,
  e.g. `runs.created_at` at `3e1fe4d03ee4…:109`), so the DB column is nullable, while the ORM declares
  `Mapped[datetime]` (non-optional). The `server_default=now()` masks it, but the model/DB contract
  diverges.
- **Fix:** add `nullable=False` to the migration column (and/or a follow-up migration).

### L3 — Dead code: `TWEET_SOFT_MAX` imported but unused; `X_THREAD_LEN` defined but unused
- **File:** `src/newsroom/distribute/repackage.py:17` (import) and `:25` (constant)
- **Description:** `TWEET_SOFT_MAX` is imported from `prompts` but never referenced in `repackage.py`;
  `X_THREAD_LEN = 10` is defined but never used (no thread-length assertion uses it — see M3). Dead
  symbols that imply validation that doesn't exist.
- **Fix:** remove the unused import and either use `X_THREAD_LEN` for length validation or delete it.

### L4 — Over/under-counted tweet length: raw `len()` vs X's weighted character count
- **File:** `src/newsroom/distribute/repackage.py:67-69` (`overlong`), `scripts/post_thread.py:114`
- **Description:** The 280-char check uses Python `len()`. X counts every URL as 23 (t.co) and
  emoji/CJK as 2. The closing tweet contains a URL + the subscribe URL, and Telegram/closing text
  contain emoji, so `len()` will over-count URLs (false "OVER-280" warnings) and under-count emoji.
  The warnings are advisory only, but they can mislead the operator.
- **Fix:** use a tweet-length library (e.g. `twitter-text`) or approximate URLs as 23 chars when
  computing the cap.

### L5 — `rendered_text` for an X row is frozen to hook A even after posting B/C
- **File:** `src/newsroom/distribute/repackage.py:271-274` (persists `thread.render(0)`),
  `scripts/post_thread.py:141-145` (updates `variant` but not `rendered_text`)
- **Description:** The X row stores `rendered_text` = hook-A assembly and `variant='hook_a'`. When the
  operator posts hook B/C, `post_thread` updates `variant` to `hook_b`/`hook_c` but leaves
  `rendered_text` showing hook A, so the row's rendered text and the actually-posted thread disagree.
- **Fix:** when posting, also rewrite `rendered_text` from the chosen hook (or stop storing a
  hook-specific rendering and derive it from `payload_json` on demand).

### L6 — `load_article_context` claims fallback can match orphan claims when `run_id` is NULL
- **File:** `src/newsroom/distribute/repackage.py:124-129`
- **Description:** When `claims_used` is empty the query falls back to `Claim.run_id == article.run_id`.
  If `article.run_id` is `None` (nullable, `models.py:108`), SQLAlchemy emits `WHERE run_id IS NULL`,
  pulling in every claim with a NULL `run_id` (unrelated orphans) as this article's "ground truth".
- **Fix:** guard the fallback: if both `claims_used` and `run_id` are empty, return no claims rather
  than matching NULL.

### L7 — Truncated LLM output (`finish_reason='length'`) is not detected before JSON parse
- **File:** `src/newsroom/distribute/repackage.py:199-209`, `220-229`
- **Description:** `generate_x_thread` requests `max_tokens=1600` for ~11 tweets of JSON. If the model
  hits the cap, `res.finish_reason` is `'length'` and the JSON is truncated → `_parse_json` raises a
  `JSONDecodeError`. The code never inspects `finish_reason`, so a length-truncation surfaces as a
  generic parse error with no actionable hint.
- **Fix:** check `res.finish_reason == 'length'` and raise a clear "output truncated, raise max_tokens"
  error (and/or bump the X budget).

### L8 — `post_thread` thread URL malforms when `x_handle` is empty
- **File:** `scripts/post_thread.py:89-91`
- **Description:** `_thread_url` does `settings.x_handle.lstrip("@")`; if `x_handle` is unset the URL
  becomes `https://x.com//status/<id>` (double slash). The default is populated, so this is an
  edge/config-hardening nit.
- **Fix:** validate `x_handle` is non-empty at startup, or fall back to a numeric `i/status/<id>` URL.
</content>
</invoke>
