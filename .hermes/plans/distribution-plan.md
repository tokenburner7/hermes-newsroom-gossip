# Hermes Newsroom — Distribution & Validation Plan

> **Execution model.** This plan is written for Hermes Agent to execute **one task at a time, top to bottom**. Every file path is absolute. Every command is copy‑pasteable from the repo root `/Users/tn/dev/hermes-newsroom` unless stated. Every code block is complete. Do not skip Phase 0 — nothing downstream works until the site is live.

---

## 1. Goal

Answer one question in 14 days, for $0 of infrastructure spend:

> **Does anyone want AI×Crypto research synthesis with provenance‑locked claims?**

The pipeline already produces fact‑gated, provenance‑locked articles. It has **zero distribution, zero audience, zero revenue.** This plan deploys the site, turns each article into a *product* (an X thread + a Telegram post, not a link), captures email, and runs a 14‑day experiment with hard KILL / INVEST gates.

### Strategic commitments (already decided — do not relitigate)
1. **Distribution before infrastructure.** Validate demand, then build platform.
2. **Repackaging IS the product.** The thread must deliver standalone insight; most readers never click.
3. **Speed is the wedge.** arXiv ingested every 3 hours beats human analysts.
4. **Provenance is the brand.** "Every claim hash‑locked to its source span, SHA‑256 verified."
5. **Hard stop conditions.** Defined metrics trigger KILL, INVEST, or one more week.

### Three blind spots this plan fixes
- **The site isn't deployed** → Phase 0 ships it to Cloudflare Pages.
- **Distribution treated as plumbing** → Phase 1 makes the thread a crafted product with A/B hooks and a provenance callout.
- **No experiment design** → Phase 3 defines exact metrics and stop conditions.

---

## 2. Architecture

```
                          EXISTING (do not modify)
  ingest(9 src) → select → research → draft → gate → escalate → factcheck
        → humanize → persist → publish ──► web/src/content/articles/<slug>.md
                                                    │
                          NEW (this plan)           ▼
  ┌─────────────────────────────────────────────────────────────────────┐
  │ Phase 0  DEPLOY     Astro SSG ──build──► web/dist ──wrangler──► CF Pages │
  │                     + custom domain + CF Web Analytics + GSC sitemap     │
  ├─────────────────────────────────────────────────────────────────────┤
  │ Phase 1  REPACKAGE  Article ──► newsroom.distribute ──► X thread (3 hook │
  │                     variants) + Telegram 3‑bullet ──► distributions table │
  │                     CLI: `newsroom distribute`   Temporal: distribute act │
  │                     cron: arXiv q3h + full cycle (ingest→publish→deploy)  │
  ├─────────────────────────────────────────────────────────────────────┤
  │ Phase 2  CAPTURE    Buttondown form in site footer + RSS feed +          │
  │                     subscribe CTA wired into thread tweet 10 / TG footer  │
  ├─────────────────────────────────────────────────────────────────────┤
  │ Phase 3  EXPERIMENT 14 days · daily post via xurl/telegram skills ·       │
  │                     manual engagement · weekly review · Day‑14 decision   │
  └─────────────────────────────────────────────────────────────────────┘
```

**Key design choice — generation vs. posting are split.** `newsroom distribute` only *generates and stores* the thread/Telegram payloads (negligible LLM cost). Actual posting is done by **Hermes Agent using the `xurl` skill (X) and `telegram` skill (Telegram)** in the daily runbook (Phase 3), because those are the authorized external tools. The cron job automates everything up to and including payload generation and site redeploy; a human/agent does the final post and records the URL back.

---

## 3. Tech stack

| Layer | Tool | Notes |
|---|---|---|
| Static site | Astro 6 SSG (`output: 'static'`) | already built; stays static — no server, no DB‑driven pages |
| Hosting | **Cloudflare Pages** (free) | deployed via `npx wrangler pages deploy` (no GitHub remote needed) |
| Analytics | **Cloudflare Web Analytics** (free) | zero‑JS‑overhead beacon |
| Email | **Buttondown** free tier | embeddable no‑JS form + RSS‑to‑email automation |
| RSS | `@astrojs/rss` (first‑party, build‑time) | static feed at `/rss.xml` |
| X posting | `xurl` skill (agent‑driven) | thread = reply chain |
| Telegram | `telegram` skill (agent‑driven) | 3‑bullet group‑chat post |
| Repackaging LLM | existing `LLMClient` (DeepSeek→OpenRouter) | JSON mode, ~$0.01/article |
| DB | existing Postgres 16 + pgvector | new `distributions` table via Alembic |
| Orchestration | existing Temporal | new best‑effort `distribute` activity |

**Budget:** $0 infrastructure (all free tiers). LLM repackaging ≈ $0.01/article, inside the existing $3/day envelope. **Total dev time target: ~6 hours** across all phases.

---

# Phase 0 — Deploy (build nothing else until this exists)

> Outcome of Phase 0: the 7 existing articles are live on a public URL with analytics and a verified sitemap. **Do not start Phase 1 until `https://<your-domain>` serves the homepage.**

---

### Task 0.1 — Point the Astro site at its production URL

**Objective:** Set the canonical site URL so sitemap, RSS, and schema.org emit absolute production links.

**Files to modify:**
- `/Users/tn/dev/hermes-newsroom/web/astro.config.mjs`

**Decision:** If you have/will register a domain, use it (e.g. `https://aixcrypto.news`). If not, use the Cloudflare Pages default subdomain you will get in Task 0.2 (`https://aixcrypto-news.pages.dev`). **Pick one now and use it everywhere in this plan as `BRAND_URL`.** This plan uses `https://aixcrypto.news` as the example — replace consistently.

**Edit** `web/astro.config.mjs` — change the `site` line:

```js
// @ts-check
import { defineConfig } from 'astro/config';
import mdx from '@astrojs/mdx';
import sitemap from '@astrojs/sitemap';

// https://astro.build/config
export default defineConfig({
  site: 'https://aixcrypto.news', // BRAND_URL — was http://localhost:4321
  output: 'static',
  integrations: [mdx(), sitemap()],
});
```

**Command + expected output:**
```bash
cd /Users/tn/dev/hermes-newsroom/web && npm run build
```
Expect: `[build] Complete!` and `dist/sitemap-index.xml` generated. Confirm absolute URLs:
```bash
grep -o 'https://aixcrypto.news[^<]*' /Users/tn/dev/hermes-newsroom/web/dist/sitemap-0.xml | head
```
Expect several `https://aixcrypto.news/articles/...` lines.

**Verification:** `web/dist/index.html` exists and references the 7 articles; sitemap shows production host (not localhost).

---

### Task 0.2 — Deploy `web/dist` to Cloudflare Pages via Wrangler

**Objective:** Publish the static build to a public URL with no GitHub remote required.

**Prerequisites (one‑time, interactive):**
```bash
# Free Cloudflare account required first: https://dash.cloudflare.com/sign-up
cd /Users/tn/dev/hermes-newsroom/web
npx --yes wrangler login            # opens browser; authorize once
npx --yes wrangler whoami           # expect your account email + account id
```

**Create the Pages project and deploy:**
```bash
cd /Users/tn/dev/hermes-newsroom/web
npm run build
npx --yes wrangler pages project create aixcrypto-news \
  --production-branch main || true   # idempotent; ignore "already exists"
npx --yes wrangler pages deploy dist --project-name aixcrypto-news --branch main
```

**Expected output:** `✨ Deployment complete! Take a peek over at https://aixcrypto-news.pages.dev` (or a `<hash>.aixcrypto-news.pages.dev` preview; the project root is the `.pages.dev` one).

**Verification:**
```bash
curl -sI https://aixcrypto-news.pages.dev | head -1   # expect: HTTP/2 200
curl -s https://aixcrypto-news.pages.dev | grep -o '<title>[^<]*</title>'
```
Expect the homepage title. Open it in a browser and confirm the 7 article cards render.

> If you are NOT using a custom domain, set `BRAND_URL = https://aixcrypto-news.pages.dev`, go back and re‑run Task 0.1 with that value, rebuild, and redeploy. Then skip Task 0.3.

---

### Task 0.3 — Configure the custom domain (skip if using `.pages.dev`)

**Objective:** Serve the site on `aixcrypto.news`.

**Steps (Cloudflare dashboard — no code):**
1. Register the domain (Cloudflare Registrar, ~$10/yr for `.news`, or any registrar). If budget is truly $0, **skip this task and use the `.pages.dev` subdomain.**
2. Dashboard → **Workers & Pages** → `aixcrypto-news` → **Custom domains** → **Set up a custom domain** → enter `aixcrypto.news` → follow DNS prompts (auto if domain is on Cloudflare).
3. Wait for SSL (usually < 5 min).

**Verification:**
```bash
curl -sI https://aixcrypto.news | head -1   # expect: HTTP/2 200
```

---

### Task 0.4 — Enable Cloudflare Web Analytics

**Objective:** Track unique visitors, page views, and time on page with a zero‑overhead beacon.

**Steps:**
1. Dashboard → **Analytics & Logs → Web Analytics → Add a site** → choose `aixcrypto-news` (or enter the hostname). Cloudflare issues a **beacon token**.
2. Add the beacon to the site shell so every page reports.

**File to modify:** `/Users/tn/dev/hermes-newsroom/web/src/layouts/BaseLayout.astro`

**Edit:** add the beacon `<script>` immediately before the closing `</body>` tag (after the `</style>`), replacing `YOUR_CF_BEACON_TOKEN`:

```astro
    </style>
    <!-- Cloudflare Web Analytics (zero-cost, privacy-friendly) -->
    <script defer src="https://static.cloudflareinsights.com/beacon.min.js"
      data-cf-beacon='{"token": "YOUR_CF_BEACON_TOKEN"}'></script>
  </body>
</html>
```

**Command:**
```bash
cd /Users/tn/dev/hermes-newsroom/web && npm run build \
  && npx --yes wrangler pages deploy dist --project-name aixcrypto-news --branch main
```

**Verification:**
```bash
curl -s https://aixcrypto.news | grep cloudflareinsights   # expect the beacon line
```
Visit the site once; within ~60s the Web Analytics dashboard shows 1 visit.

---

### Task 0.5 — Create the dedicated brand X account

**Objective:** A standalone brand account (NOT a personal account) for the experiment.

**Steps (manual, in a browser — agent cannot create accounts):**
1. Create `@aixcrypto_news` (or closest available; record the final handle).
2. Profile: name "AI×Crypto Synthesis"; bio: *"Provenance‑locked syntheses pairing frontier AI/security research with concrete crypto implications. Every claim hash‑locked to its source. New arXiv → analysis in 3h."*; website = `BRAND_URL`; pinned‑tweet slot reserved for the best thread.
3. Ensure the `xurl` skill is authenticated against THIS account (see Phase 3 runbook). Verify:
```bash
xurl /2/users/me   # expect the brand handle, not your personal one
```

**Verification:** `xurl /2/users/me` returns the brand account id + handle. **Record the handle** — it goes in config (Task 1.1) and the closing tweet.

---

### Task 0.6 — Submit the sitemap to Google Search Console

**Objective:** Get the 7 articles indexed; establish the organic‑search baseline the experiment measures.

**Steps:**
1. https://search.google.com/search-console → **Add property** → URL prefix `BRAND_URL`.
2. Verify via DNS TXT (if domain on Cloudflare) or the HTML‑file method. For the HTML‑file method, drop the verification file at `/Users/tn/dev/hermes-newsroom/web/public/<google-token>.html`, rebuild, redeploy, then click Verify.
3. **Sitemaps** → submit `sitemap-index.xml`.
4. Record `BRAND_URL` into config so the existing GSC health check can later read it.

**File to modify:** `/Users/tn/dev/hermes-newsroom/.env` — add:
```bash
# --- Distribution / external presence (Phase 0) ---
GSC_SITE_URL=https://aixcrypto.news/
```

**Verification:** GSC shows "Sitemap submitted — Success" with 7 discovered URLs (indexing follows over days).

---

# Phase 1 — Distribution pipeline (the product)

> Outcome: `newsroom distribute <id>` (or `--latest`) turns any published article into a crafted X thread (3 A/B hook variants + 8 body tweets + auto‑generated provenance/subscribe closer) and a 3‑bullet Telegram post, logged to a new `distributions` table. A cron chains the whole pipeline and redeploys.

---

### Task 1.1 — Add distribution settings to config

**Objective:** Centralize brand URL, handles, model, and subscribe URL.

**File to modify:** `/Users/tn/dev/hermes-newsroom/src/newsroom/config.py`

**Edit:** add this block inside `class Settings`, immediately **after** the `gsc_api_key` field (around line 110, before the `# --- Budget / safety` block):

```python
    # --- Distribution & validation (Phase 4) -------------------------------
    # Canonical production site URL (Cloudflare Pages). No trailing slash.
    brand_url: str = "https://aixcrypto.news"
    # Brand X/Twitter handle (the dedicated account, not a personal one).
    x_handle: str = "@aixcrypto_news"
    # Telegram target for the `telegram` skill: @channel username or chat id.
    telegram_channel: str = ""
    # Where threads/posts send readers to subscribe. Empty => brand_url + '#subscribe'.
    subscribe_url: str = ""
    # Model used to repackage articles into threads/posts (cheap workhorse).
    distribute_model: str = "deepseek-chat"
    # Master switch for the distribute stage (CLI + Temporal activity).
    distribution_enabled: bool = True
```

**Edit:** add this property next to the other `@property` definitions (after `openrouter_configured`, around line 194):

```python
    @property
    def effective_subscribe_url(self) -> str:
        """Subscribe destination: explicit override, else the site's #subscribe anchor."""
        return self.subscribe_url or f"{self.brand_url.rstrip('/')}/#subscribe"
```

**Verification:**
```bash
cd /Users/tn/dev/hermes-newsroom && uv run python -c \
"from newsroom.config import settings; print(settings.brand_url, settings.effective_subscribe_url, settings.distribute_model)"
```
Expect: `https://aixcrypto.news https://aixcrypto.news/#subscribe deepseek-chat`.

---

### Task 1.2 — Add the `distributions` table (model + migration)

**Objective:** Persist every generated thread/post for A/B tracking, idempotency, and posting‑status.

**File to modify:** `/Users/tn/dev/hermes-newsroom/src/newsroom/models.py`

**Edit:** append this model at the end of the file (after `SourceHealth`):

```python
class Distribution(Base):
    """A repackaged article payload for one channel (X thread / Telegram post).

    Generation and posting are decoupled: `newsroom distribute` writes a row with
    status='generated'; the operator/agent posts via the xurl/telegram skills and
    updates status='posted' + external_url. Keeps the site a static SSG (this is
    DB-only state, never rendered into web/).
    """

    __tablename__ = "distributions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    article_id: Mapped[int | None] = mapped_column(BigInteger)
    channel: Mapped[str] = mapped_column(Text, nullable=False)  # 'x' | 'telegram'
    variant: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'default'")
    )
    # Structured payload: X => {hooks[], body_tweets[], closing}; TG => {bullets[]}.
    payload_json: Mapped[dict | None] = mapped_column(JSONB)
    # Ready-to-post rendered text (X: hook A assembled thread; TG: full message).
    rendered_text: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'generated'")
    )  # 'generated' | 'posted' | 'failed'
    external_url: Mapped[str | None] = mapped_column(Text)  # tweet/post URL once posted
    in_tokens: Mapped[int | None] = mapped_column(Integer)
    out_tokens: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
```

**File to create:** `/Users/tn/dev/hermes-newsroom/alembic/versions/b2c3d4e5f6a7_add_distributions.py`

```python
"""add_distributions

Adds the ``distributions`` table backing the Phase 4 distribution pipeline:
one row per repackaged article payload per channel (X thread / Telegram post).
Generation and posting are decoupled (status: generated -> posted/failed).

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-20 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'distributions',
        sa.Column('id', sa.BigInteger(), primary_key=True),
        sa.Column('article_id', sa.BigInteger(), nullable=True),
        sa.Column('channel', sa.Text(), nullable=False),
        sa.Column('variant', sa.Text(), server_default=sa.text("'default'"), nullable=False),
        sa.Column('payload_json', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('rendered_text', sa.Text(), nullable=True),
        sa.Column('status', sa.Text(), server_default=sa.text("'generated'"), nullable=False),
        sa.Column('external_url', sa.Text(), nullable=True),
        sa.Column('in_tokens', sa.Integer(), nullable=True),
        sa.Column('out_tokens', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()')),
        sa.Column('posted_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_distributions_article_id', 'distributions', ['article_id'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_distributions_article_id', table_name='distributions')
    op.drop_table('distributions')
```

**Command + expected output:**
```bash
cd /Users/tn/dev/hermes-newsroom && uv run alembic upgrade head
```
Expect: `Running upgrade a1b2c3d4e5f6 -> b2c3d4e5f6a7, add_distributions`.

**Verification:**
```bash
cd /Users/tn/dev/hermes-newsroom && uv run python -c \
"import asyncio; from sqlalchemy import text; from newsroom.db import async_session_factory
async def m():
    async with async_session_factory() as s:
        print((await s.execute(text('select count(*) from distributions'))).scalar_one())
asyncio.run(m())"
```
Expect: `0` (table exists, empty).

---

### Task 1.3 — X thread repackaging (prompts + generator)

**Objective:** Turn an article into a 10‑tweet thread: 3 A/B hook variants, 8 substance body tweets, and a code‑generated closing tweet with the verified‑source link + subscribe CTA (links are never trusted to the LLM).

**File to create:** `/Users/tn/dev/hermes-newsroom/src/newsroom/distribute/__init__.py`

```python
"""Distribution stage: repackage a published article into channel-native products.

The thread/post IS the product, not a link. Generation is decoupled from posting:
these functions produce + persist payloads (cheap LLM); the operator/agent posts
via the xurl (X) and telegram skills and records the URL back onto the row.
"""

from __future__ import annotations

from .repackage import (
    ArticleContext,
    DistributeResult,
    TelegramResult,
    ThreadResult,
    distribute_article,
    distribute_latest,
    generate_telegram,
    generate_x_thread,
    latest_published_article_id,
    load_article_context,
)

__all__ = [
    "ArticleContext",
    "DistributeResult",
    "ThreadResult",
    "TelegramResult",
    "load_article_context",
    "latest_published_article_id",
    "generate_x_thread",
    "generate_telegram",
    "distribute_article",
    "distribute_latest",
]
```

**File to create:** `/Users/tn/dev/hermes-newsroom/src/newsroom/distribute/prompts.py`

```python
"""Repackaging prompts. The thread is the product — these prompts carry the brand
voice (analytical, specific, provenance-first) and constrain output to strict JSON.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .repackage import ArticleContext

#: Max characters per tweet we allow the model to emit (hard cap is 280; we leave room).
TWEET_SOFT_MAX = 270

X_THREAD_SYSTEM = """You are the lead writer for AI×Crypto Synthesis, an autonomous \
research desk publishing provenance-locked syntheses that pair frontier AI/security \
research with concrete crypto implications.

You repackage ONE already-published, fact-gated article into an X (Twitter) thread. \
The thread IS the product — most readers will never click through, so the thread must \
deliver real, standalone insight.

VOICE
- Analytical, not promotional. You are a sharp analyst, not a hype account.
- Specific over vague: name the mechanism, the number, the tradeoff. "Cuts policy \
violations 43% under adversarial load" beats "improves safety".
- One idea per tweet; each tweet must survive on its own if quoted.
- No tweet counters ("1/", "2/10", "🧵"). No hashtag spam (0–1 only, and only a real \
ticker/topic). No filler ("Let's dive in", "Here's why that matters", "Buckle up").
- Active voice. Short sentences. Concrete nouns.

HOOK (tweet 1) — provide THREE distinct variants for A/B testing:
  (a) finding-first: lead with the single most surprising/specific result.
  (b) stakes-first: lead with what breaks or what's now possible in crypto.
  (c) contrarian: lead with the counter-intuitive angle or the consensus it dents.
  Each hook ≤ 2 sentences; the reader decides to keep reading in the first 7 words.

BODY (exactly 8 tweets, tweets 2–9):
  - 2–4: the finding — what the research shows, with the key numbers/mechanism.
  - 5–7: the crypto bridge — translate findings into concrete on-chain / market / \
security implications (protocols, agents, MEV, oracles, key mgmt, regulation).
  - 8–9: the "so what" — who should care, what changes, the open question.

GROUND TRUTH
- Every claim must trace to the article's provenance-locked claims. Do NOT invent \
statistics. If unsure of a number, state the direction, not a fabricated figure.

CONSTRAINTS
- Each tweet ≤ 270 characters.
- Do NOT write the closing tweet (tweet 10) — it is appended automatically with the \
verified-source link and subscribe CTA.
- Do NOT include any URLs — they are appended automatically.

OUTPUT — return ONLY valid JSON (no markdown fences):
{
  "hooks": ["<variant a>", "<variant b>", "<variant c>"],
  "body_tweets": ["<t2>", "<t3>", "<t4>", "<t5>", "<t6>", "<t7>", "<t8>", "<t9>"]
}
"""

TELEGRAM_SYSTEM = """You repackage ONE published AI×Crypto Synthesis article into a \
3-bullet Telegram post for crypto/AI group chats. Readers skim. Each bullet is one \
scannable, concrete takeaway (≤ 220 chars):
  - bullet 1: the finding + the key number/mechanism.
  - bullet 2: the concrete crypto implication.
  - bullet 3: "watch this / why it matters now".
Analytical, no hype, no tweet counters. Do NOT include URLs or a headline — both are \
added automatically. Do NOT invent statistics. Return ONLY valid JSON:
{"bullets": ["<b1>", "<b2>", "<b3>"]}
"""


def _claims_block(ctx: "ArticleContext", limit: int = 12) -> str:
    if not ctx.claims:
        return "- (no locked claims found)"
    return "\n".join(f"- {c}" for c in ctx.claims[:limit])


def _implications_block(ctx: "ArticleContext") -> str:
    if not ctx.implications:
        return "- (none listed)"
    return "\n".join(f"- {x}" for x in ctx.implications)


def build_x_thread_user(ctx: "ArticleContext") -> str:
    return f"""ARTICLE TO REPACKAGE
Type: {ctx.type}
Headline: {ctx.headline}
Dek: {ctx.dek}

CRYPTO IMPLICATIONS (finding → implication):
{_implications_block(ctx)}

PROVENANCE-LOCKED CLAIMS (each hash-verified against a source span — your ground \
truth; do not exceed them):
{_claims_block(ctx)}

ARTICLE BODY (context only; do not copy verbatim):
{ctx.body[:4000]}

Write the thread now. Return only the JSON object."""


def build_telegram_user(ctx: "ArticleContext") -> str:
    return f"""ARTICLE TO REPACKAGE
Headline: {ctx.headline}
Dek: {ctx.dek}

CRYPTO IMPLICATIONS:
{_implications_block(ctx)}

PROVENANCE-LOCKED CLAIMS (ground truth):
{_claims_block(ctx)}

Write the 3 bullets now. Return only the JSON object."""
```

**File to create:** `/Users/tn/dev/hermes-newsroom/src/newsroom/distribute/repackage.py`

```python
"""Load a published article, repackage it per channel, and persist the payloads."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from ..config import settings
from ..db import get_sync_session_factory
from ..llm import LLMError, get_client
from ..models import Article, Claim, Distribution
from .prompts import (
    TELEGRAM_SYSTEM,
    TWEET_SOFT_MAX,
    X_THREAD_SYSTEM,
    build_telegram_user,
    build_x_thread_user,
)

log = logging.getLogger(__name__)

X_THREAD_LEN = 10
TWEET_HARD_MAX = 280
#: Visual delimiter between tweets in the single rendered thread blob (for copy/paste).
THREAD_SEP = "\n\n———\n\n"


@dataclass(slots=True)
class ArticleContext:
    """The slice of a published article needed to repackage it."""

    article_id: int
    slug: str
    headline: str
    dek: str
    type: str
    body: str
    implications: list[str]
    claims: list[str]

    @property
    def url(self) -> str:
        return f"{settings.brand_url.rstrip('/')}/articles/{self.slug}"


@dataclass(slots=True)
class ThreadResult:
    hooks: list[str]
    body_tweets: list[str]
    closing: str
    in_tokens: int = 0
    out_tokens: int = 0

    def assemble(self, hook_index: int = 0) -> list[str]:
        """Full 10-tweet thread for the given hook variant (0=a, 1=b, 2=c)."""
        hook = self.hooks[hook_index] if hook_index < len(self.hooks) else (
            self.hooks[0] if self.hooks else ""
        )
        return [hook, *self.body_tweets, self.closing]

    def render(self, hook_index: int = 0) -> str:
        return THREAD_SEP.join(t.strip() for t in self.assemble(hook_index) if t.strip())

    def overlong(self, hook_index: int = 0) -> list[int]:
        """1-based tweet positions that exceed the hard 280-char cap (should be empty)."""
        return [i + 1 for i, t in enumerate(self.assemble(hook_index)) if len(t) > TWEET_HARD_MAX]

    def payload(self) -> dict:
        return {"hooks": self.hooks, "body_tweets": self.body_tweets, "closing": self.closing}


@dataclass(slots=True)
class TelegramResult:
    bullets: list[str]
    rendered: str
    in_tokens: int = 0
    out_tokens: int = 0

    def payload(self) -> dict:
        return {"bullets": self.bullets, "rendered": self.rendered}


@dataclass(slots=True)
class DistributeResult:
    article_id: int
    url: str
    thread: ThreadResult | None = None
    telegram: TelegramResult | None = None
    distribution_ids: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


# --- load ---------------------------------------------------------------------

def latest_published_article_id() -> int | None:
    """The most recently published article id, or None."""
    factory = get_sync_session_factory()
    with factory() as session:
        return session.execute(
            select(Article.id)
            .where(Article.status == "published")
            .order_by(Article.published_at.desc().nullslast(), Article.id.desc())
        ).scalars().first()


def load_article_context(article_id: int) -> ArticleContext:
    """Load a published article + its locked claims. Raises if missing/not published."""
    factory = get_sync_session_factory()
    with factory() as session:
        article = session.get(Article, article_id)
        if article is None:
            raise LookupError(f"no article with id {article_id}")
        if article.status != "published":
            raise ValueError(
                f"article {article_id} status={article.status!r}; only 'published' "
                "articles can be distributed"
            )
        env = article.envelope_json or {}
        claim_ids = article.claims_used or []
        stmt = select(Claim)
        stmt = (
            stmt.where(Claim.id.in_(claim_ids)) if claim_ids
            else stmt.where(Claim.run_id == article.run_id)
        )
        claims = [c.claim_text for c in session.execute(stmt.order_by(Claim.id)).scalars().all()]
        return ArticleContext(
            article_id=article.id,
            slug=article.slug,
            headline=article.headline,
            dek=article.dek or env.get("dek", ""),
            type=article.type,
            body=article.body_final_md or article.body_md or "",
            implications=list(env.get("crypto_implications") or []),
            claims=claims,
        )


# --- closing / templating (links never trusted to the LLM) --------------------

def _closing_tweet(ctx: ArticleContext) -> str:
    sub = settings.effective_subscribe_url
    return (
        "Every claim above is hash-locked to its exact source span — SHA-256 "
        "verified, not vibes.\n\n"
        f"Full synthesis + sources:\n{ctx.url}\n\n"
        f"3-hour AI×crypto research synthesis → {sub}"
    )


def _telegram_render(ctx: ArticleContext, bullets: list[str]) -> str:
    body = "\n".join(f"• {b.strip()}" for b in bullets)
    sub = settings.effective_subscribe_url
    return (
        f"🤖×⛓ {ctx.headline}\n\n"
        f"{body}\n\n"
        f"🔗 Hash-locked sources + full synthesis:\n{ctx.url}\n\n"
        f"Subscribe (3h cadence): {sub}"
    )


def _parse_json(text: str) -> dict:
    """Tolerant JSON parse: strips accidental ```json fences before loading."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[4:].lstrip() if t.lower().startswith("json") else t
    return json.loads(t)


# --- generation ---------------------------------------------------------------

def generate_x_thread(ctx: ArticleContext) -> ThreadResult:
    client = get_client()
    if not client.providers:
        raise LLMError("no LLM providers configured for distribution")
    messages = [
        {"role": "system", "content": X_THREAD_SYSTEM},
        {"role": "user", "content": build_x_thread_user(ctx)},
    ]
    res = client.chat(
        messages, model=settings.distribute_model,
        response_format={"type": "json_object"}, max_tokens=1600, temperature=0.7,
    )
    data = _parse_json(res.text)
    hooks = [h.strip() for h in (data.get("hooks") or []) if h and h.strip()][:3]
    body = [t.strip() for t in (data.get("body_tweets") or []) if t and t.strip()][:8]
    return ThreadResult(
        hooks=hooks or [ctx.headline], body_tweets=body, closing=_closing_tweet(ctx),
        in_tokens=res.in_tokens, out_tokens=res.out_tokens,
    )


def generate_telegram(ctx: ArticleContext) -> TelegramResult:
    client = get_client()
    if not client.providers:
        raise LLMError("no LLM providers configured for distribution")
    messages = [
        {"role": "system", "content": TELEGRAM_SYSTEM},
        {"role": "user", "content": build_telegram_user(ctx)},
    ]
    res = client.chat(
        messages, model=settings.distribute_model,
        response_format={"type": "json_object"}, max_tokens=600, temperature=0.6,
    )
    data = _parse_json(res.text)
    bullets = [b.strip() for b in (data.get("bullets") or []) if b and b.strip()][:3]
    return TelegramResult(
        bullets=bullets, rendered=_telegram_render(ctx, bullets),
        in_tokens=res.in_tokens, out_tokens=res.out_tokens,
    )


# --- persistence + orchestration ---------------------------------------------

def _persist(article_id: int, channel: str, variant: str, payload: dict,
             rendered: str, in_tokens: int, out_tokens: int) -> int:
    factory = get_sync_session_factory()
    with factory() as session:
        row = Distribution(
            article_id=article_id, channel=channel, variant=variant,
            payload_json=payload, rendered_text=rendered, status="generated",
            in_tokens=in_tokens, out_tokens=out_tokens,
        )
        session.add(row)
        session.commit()
        return row.id


def distribute_article(article_id: int, channels: tuple[str, ...] = ("x", "telegram")) -> DistributeResult:
    """Repackage one published article for the given channels; persist each payload."""
    ctx = load_article_context(article_id)
    result = DistributeResult(article_id=article_id, url=ctx.url)

    if "x" in channels:
        thread = generate_x_thread(ctx)
        result.thread = thread
        over = thread.overlong()
        if over:
            result.warnings.append(f"x: tweets over 280 chars at positions {over}")
        result.distribution_ids["x"] = _persist(
            article_id, "x", "hook_a", thread.payload(), thread.render(0),
            thread.in_tokens, thread.out_tokens,
        )

    if "telegram" in channels:
        tg = generate_telegram(ctx)
        result.telegram = tg
        if len(tg.bullets) != 3:
            result.warnings.append(f"telegram: expected 3 bullets, got {len(tg.bullets)}")
        result.distribution_ids["telegram"] = _persist(
            article_id, "telegram", "default", tg.payload(), tg.rendered,
            tg.in_tokens, tg.out_tokens,
        )

    log.info("distributed article=%s channels=%s ids=%s", article_id, channels, result.distribution_ids)
    return result


def distribute_latest(channels: tuple[str, ...] = ("x", "telegram")) -> DistributeResult:
    article_id = latest_published_article_id()
    if article_id is None:
        raise LookupError("no published articles to distribute")
    return distribute_article(article_id, channels)
```

**Verification:**
```bash
cd /Users/tn/dev/hermes-newsroom && uv run python -c "import newsroom.distribute as d; print(sorted(d.__all__))"
```
Expect the exported names list with no import error.

---

### Task 1.4 — Telegram repackaging

Already implemented as `generate_telegram` / `_telegram_render` in Task 1.3 (`repackage.py`). No new file. This task is a **focused review + unit smoke test** of the Telegram path so it is not skipped.

**Objective:** Confirm the 3‑bullet Telegram render is group‑chat ready (headline + 3 bullets + hash‑locked source link + subscribe line) and link‑exact.

**Command (offline render check, no LLM):**
```bash
cd /Users/tn/dev/hermes-newsroom && uv run python -c "
from newsroom.distribute.repackage import _telegram_render, ArticleContext
ctx = ArticleContext(1,'demo-slug','Demo headline','Demo dek','research_synthesis','body',['x→y'],['c1'])
print(_telegram_render(ctx, ['Finding with a number.','Concrete crypto implication.','Why it matters now.']))
"
```
**Expected output:** a message starting `🤖×⛓ Demo headline`, three `• ` bullets, then `🔗 Hash-locked sources + full synthesis:` with `https://aixcrypto.news/articles/demo-slug`, then `Subscribe (3h cadence): https://aixcrypto.news/#subscribe`.

**Verification:** URLs are the exact config‑derived ones (not model output); message ≤ Telegram's 4096‑char limit.

---

### Task 1.5 — Add the `distribute` CLI command

**Objective:** `newsroom distribute [ARTICLE_ID] [--latest] [--channel x|telegram|all]` generates + logs payloads and prints ready‑to‑post text.

**File to modify:** `/Users/tn/dev/hermes-newsroom/src/newsroom/cli.py`

**Edit:** insert this command **after** the `publish` command (after line 553, before `run-once`):

```python
@app.command()
def distribute(
    article_id: int = typer.Argument(
        None, help="articles.id to distribute (omit and pass --latest for the newest)."
    ),
    latest: bool = typer.Option(
        False, "--latest", help="Distribute the most recently published article."
    ),
    channel: str = typer.Option(
        "all", "--channel", help="Channel to generate: x | telegram | all."
    ),
) -> None:
    """Repackage a published article into an X thread + Telegram post (logs to DB).

    Generation only — posting is done with the `xurl` (X) and `telegram` skills
    (see the Phase 3 runbook). Payloads are stored in `distributions` for tracking.
    """
    if not settings.distribution_enabled:
        console.print("[yellow]distribution disabled[/] — set DISTRIBUTION_ENABLED=true.")
        raise typer.Exit()
    _require_providers()

    chan = channel.strip().lower()
    if chan == "all":
        channels: tuple[str, ...] = ("x", "telegram")
    elif chan in ("x", "telegram"):
        channels = (chan,)
    else:
        raise typer.BadParameter("--channel must be one of: x, telegram, all")

    from .distribute import distribute_article, distribute_latest

    try:
        with console.status("Repackaging…"):
            if latest or article_id is None:
                result = distribute_latest(channels)
            else:
                result = distribute_article(article_id, channels)
    except (LookupError, ValueError) as exc:
        console.print(f"[bold red]distribute failed:[/] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[bold green]distributed[/] article_id=[cyan]{result.article_id}[/] → {result.url}"
    )
    for w in result.warnings:
        console.print(f"[yellow]⚠ {w}[/]")

    if result.thread is not None:
        console.rule("[bold]X THREAD[/] (hook variant A — variants B/C in DB payload)")
        for i, tweet in enumerate(result.thread.assemble(0), 1):
            console.print(f"[dim]{i:>2}/[/] {tweet}\n")
        console.print("[dim]A/B hook variants:[/]")
        for label, hook in zip("ABC", result.thread.hooks):
            console.print(f"  [cyan]{label}[/] {hook}")

    if result.telegram is not None:
        console.rule("[bold]TELEGRAM POST[/]")
        console.print(result.telegram.rendered)

    table = Table(show_header=True, header_style="bold magenta", title="distribution rows")
    table.add_column("channel")
    table.add_column("distribution_id", justify="right")
    for ch, did in result.distribution_ids.items():
        table.add_row(ch, str(did))
    console.print(table)
    console.print(
        "[dim]Next: post with the xurl / telegram skills, then record the URL "
        "(status='posted'). See the Phase 3 runbook.[/]"
    )
```

**Command + expected output:**
```bash
cd /Users/tn/dev/hermes-newsroom && uv run newsroom distribute --help
```
Expect the command help with `--latest` and `--channel`.

**Live run against an existing published article:**
```bash
cd /Users/tn/dev/hermes-newsroom && uv run newsroom distribute --latest --channel all
```
Expect: a printed 10‑tweet thread (tweet 10 = provenance + subscribe links), 3 hook variants, a Telegram post, and a `distribution rows` table with two ids. Confirm rows landed:
```bash
cd /Users/tn/dev/hermes-newsroom && uv run python -c "
import asyncio; from sqlalchemy import text; from newsroom.db import async_session_factory
async def m():
    async with async_session_factory() as s:
        print((await s.execute(text('select channel,status,variant from distributions order by id desc limit 4'))).all())
asyncio.run(m())"
```
Expect rows like `('telegram','generated','default')`, `('x','generated','hook_a')`.

---

### Task 1.6 — Full‑cycle pipeline script (ingest → publish → distribute → deploy)

**Objective:** One script chains the whole flow and redeploys the static site. Posting stays a separate agent step (Phase 3).

**File to create:** `/Users/tn/dev/hermes-newsroom/scripts/pipeline_cycle.sh`

```bash
#!/usr/bin/env bash
# Full newsroom cycle: ingest all sources -> run one article end-to-end and publish
# -> repackage into X/Telegram payloads -> rebuild + redeploy the static site.
# Posting to X/Telegram is a separate agent step (xurl/telegram skills).
set -euo pipefail

REPO="/Users/tn/dev/hermes-newsroom"
UV="/Users/tn/.local/bin/uv"            # from `which uv`
PROJECT="aixcrypto-news"                 # Cloudflare Pages project
LOG_DIR="$REPO/scripts/logs"
mkdir -p "$LOG_DIR"
STAMP="$(date +%Y%m%dT%H%M%S)"
exec >>"$LOG_DIR/cycle-$STAMP.log" 2>&1

cd "$REPO"
echo "=== pipeline_cycle $STAMP ==="

# 1. ingest every source (arXiv + the 8 others), then run + publish one article.
"$UV" run newsroom ingest-all || echo "WARN: ingest-all returned non-zero"
"$UV" run newsroom run-once --publish || { echo "run-once failed; aborting"; exit 1; }

# 2. repackage the just-published article into X + Telegram payloads (logged to DB).
"$UV" run newsroom distribute --latest --channel all || echo "WARN: distribute failed"

# 3. rebuild the static site (picks up the new Markdown) and redeploy to CF Pages.
cd "$REPO/web"
npm run build
npx --yes wrangler pages deploy dist --project-name "$PROJECT" --branch main

echo "=== cycle complete $STAMP ==="
```

**File to create:** `/Users/tn/dev/hermes-newsroom/scripts/arxiv_ingest.sh`

```bash
#!/usr/bin/env bash
# Fast arXiv-only refresh (the speed wedge). Runs every 3 hours.
set -euo pipefail
REPO="/Users/tn/dev/hermes-newsroom"
UV="/Users/tn/.local/bin/uv"
LOG_DIR="$REPO/scripts/logs"
mkdir -p "$LOG_DIR"
cd "$REPO"
echo "=== arxiv_ingest $(date +%Y%m%dT%H%M%S) ===" >>"$LOG_DIR/arxiv.log"
"$UV" run newsroom ingest --since 3h >>"$LOG_DIR/arxiv.log" 2>&1
```

**Commands:**
```bash
chmod +x /Users/tn/dev/hermes-newsroom/scripts/pipeline_cycle.sh \
         /Users/tn/dev/hermes-newsroom/scripts/arxiv_ingest.sh
# Dry validation of the arXiv leg only (fast, no publish/deploy):
/Users/tn/dev/hermes-newsroom/scripts/arxiv_ingest.sh && tail -5 /Users/tn/dev/hermes-newsroom/scripts/logs/arxiv.log
```
**Expected:** the arXiv log shows an ingest table (papers fetched / rows upserted).

**Verification:** both scripts are executable; `arxiv_ingest.sh` completes and logs. (Run `pipeline_cycle.sh` end‑to‑end only once you're ready to spend ~$0.02 of LLM budget and redeploy.)

> Also add `scripts/logs/` to git ignore so cycle logs aren't committed:
```bash
printf '\n# pipeline cycle logs\nscripts/logs/\n' >> /Users/tn/dev/hermes-newsroom/.gitignore
```

---

### Task 1.7 — Temporal `distribute` activity (best‑effort, additive)

**Objective:** When a run publishes inside the durable workflow, also generate distribution payloads — without ever failing the run if distribution breaks.

**File to modify:** `/Users/tn/dev/hermes-newsroom/src/newsroom/workflows.py`

**Edit 1 — add the activity** (insert after `publish_activity`, before `settle_activity`, ~line 380):

```python
@activity.defn(name="distribute")
def distribute_activity(article_id: int) -> dict:
    """Repackage a published article into X + Telegram payloads. Best-effort."""
    from .config import settings

    if not settings.distribution_enabled:
        return {"distributed": False, "reason": "disabled"}
    try:
        from .distribute import distribute_article

        result = distribute_article(article_id, ("x", "telegram"))
        activity.logger.info(
            "distribute: article_id=%s ids=%s", article_id, result.distribution_ids
        )
        return {"distributed": True, "distribution_ids": result.distribution_ids}
    except Exception as exc:  # noqa: BLE001 — distribution must never fail a run
        activity.logger.warning("distribute skipped: %s: %s", type(exc).__name__, exc)
        return {"distributed": False, "reason": f"{type(exc).__name__}: {exc}"}
```

**Edit 2 — register it** in the `ACTIVITIES` list (add `distribute_activity,` after `publish_activity,`):

```python
ACTIVITIES = [
    start_run_activity,
    research_activity,
    draft_activity,
    gate_activity,
    escalation_activity,
    factcheck_activity,
    humanize_activity,
    persist_activity,
    publish_activity,
    distribute_activity,
    settle_activity,
    mark_dlq_activity,
]
```

**Edit 3 — call it in the workflow** right after the publish block (after the `if cfg.publish and status == "fact_checked":` block that sets `published`, ~line 513). Use a short timeout and the IO retry:

```python
        # 9b. distribute (best-effort; only when actually published).
        if published and not published.get("already_published"):
            await workflow.execute_activity(
                distribute_activity, args=[published["article_id"]],
                start_to_close_timeout=_LLM_TIMEOUT, retry_policy=_IO_RETRY,
            )
```

**Verification:**
```bash
cd /Users/tn/dev/hermes-newsroom && uv run python -c \
"from newsroom.workflows import ACTIVITIES; print([a.__name__ for a in ACTIVITIES])"
```
Expect `distribute_activity` present in the list. (If the Temporal worker is running, restart it to pick up the new activity: `docker compose restart` is not needed for the worker — re‑run `uv run python -m newsroom.temporal_worker` if you use it.)

---

### Task 1.8 — Install the cron schedule (arXiv every 3h + full cycle 3×/day)

**Objective:** Automate the speed wedge (arXiv q3h) and the publish+repackage+deploy cycle.

**Command — install crontab entries (idempotent block):**
```bash
( crontab -l 2>/dev/null | grep -v 'hermes-newsroom/scripts/' ; cat <<'CRON'
# --- Hermes Newsroom distribution (Phase 4) ---
# arXiv refresh every 3 hours (the speed wedge)
0 */3 * * * /Users/tn/dev/hermes-newsroom/scripts/arxiv_ingest.sh
# Full cycle (ingest-all -> publish -> distribute -> deploy) at 09:00, 15:00, 21:00
0 9,15,21 * * * /Users/tn/dev/hermes-newsroom/scripts/pipeline_cycle.sh
CRON
) | crontab -
```

**Verification:**
```bash
crontab -l | grep hermes-newsroom
```
Expect the two lines above. (macOS: `cron` may prompt for Full Disk Access the first time — grant it in System Settings → Privacy if the jobs don't fire. Check `scripts/logs/` after the next scheduled slot.)

---

# Phase 2 — Audience capture

> Outcome: every page has a no‑JS email signup; an RSS feed feeds Buttondown's RSS‑to‑email; and every thread/Telegram post links to subscribe.

---

### Task 2.1 — Buttondown account + RSS‑to‑email

**Objective:** Free newsletter backend that auto‑emails subscribers when a new article hits the RSS feed.

**Steps (manual + one Astro file):**
1. Create a free Buttondown account → record your **username** (e.g. `aixcrypto`).
2. Settings → leave the embeddable form on (used in Task 2.2).
3. After Task 2.3 deploys `/rss.xml`: Buttondown → **Automations / RSS** → add `BRAND_URL/rss.xml` as an RSS source so new articles auto‑draft an email.

**Add the RSS feed (first‑party Astro package, build‑time, stays static):**
```bash
cd /Users/tn/dev/hermes-newsroom/web && npm install @astrojs/rss
```

**File to create:** `/Users/tn/dev/hermes-newsroom/web/src/pages/rss.xml.ts`

```ts
import rss from '@astrojs/rss';
import { getCollection } from 'astro:content';
import type { APIContext } from 'astro';

export async function GET(context: APIContext) {
  const articles = (await getCollection('articles')).sort(
    (a, b) => b.data.published_at.valueOf() - a.data.published_at.valueOf(),
  );

  return rss({
    title: 'AI×Crypto Synthesis',
    description:
      'Provenance-locked syntheses pairing frontier AI/security research with concrete crypto implications.',
    site: context.site ?? 'https://aixcrypto.news',
    items: articles.map((article) => ({
      title: article.data.headline,
      pubDate: article.data.published_at,
      description: article.data.dek,
      link: `/articles/${article.id}/`,
    })),
  });
}
```

**Command + expected:**
```bash
cd /Users/tn/dev/hermes-newsroom/web && npm run build && test -f dist/rss.xml && echo "rss ok"
```
Expect `rss ok` and `dist/rss.xml` containing the 7 articles.

---

### Task 2.2 — Email signup form in the site shell

**Objective:** A minimal, single‑input, no‑framework Buttondown form anchored at `#subscribe`, present on every page (matches the `effective_subscribe_url` anchor).

**File to modify:** `/Users/tn/dev/hermes-newsroom/web/src/layouts/BaseLayout.astro`

**Edit 1 — markup.** Replace the `<footer class="site-footer">…</footer>` block (lines ~39–47) with this (adds the subscribe section above the footer text; replace `BUTTONDOWN_USER`):

```astro
    <section id="subscribe" class="subscribe">
      <div class="wrap">
        <h2>Get the synthesis</h2>
        <p>AI×crypto research, repackaged with every claim hash-locked to its source. New arXiv → analysis in ~3 hours.</p>
        <form
          class="subscribe-form"
          action="https://buttondown.com/api/emails/embed-subscribe/BUTTONDOWN_USER"
          method="post"
          target="_blank"
        >
          <input type="email" name="email" placeholder="you@domain.com" aria-label="Email address" required />
          <input type="hidden" value="1" name="embed" />
          <button type="submit">Subscribe</button>
        </form>
      </div>
    </section>

    <footer class="site-footer">
      <div class="wrap">
        <p>
          Autonomous AI×Crypto newsroom. Every article carries a disclosure
          label — content is machine-generated and quality-gated.
        </p>
        <p class="muted">© {new Date().getFullYear()} AI×Crypto Synthesis</p>
      </div>
    </footer>
```

**Edit 2 — styles.** Add these rules inside the `<style is:global>` block, just before the closing `</style>` (after the `.badge--queued` rule):

```css
      /* Subscribe band */
      .subscribe {
        border-top: 1px solid var(--line);
        background: var(--accent-soft);
        padding: 40px 0;
      }
      .subscribe h2 {
        font-size: 22px;
        letter-spacing: -0.02em;
        margin: 0 0 6px;
      }
      .subscribe p {
        color: var(--ink-soft);
        margin: 0 0 16px;
        max-width: 56ch;
      }
      .subscribe-form {
        display: flex;
        gap: 8px;
        flex-wrap: wrap;
        max-width: 460px;
      }
      .subscribe-form input[type='email'] {
        flex: 1 1 220px;
        padding: 11px 14px;
        font: inherit;
        border: 1px solid #c9cdf5;
        border-radius: 10px;
        background: #fff;
      }
      .subscribe-form button {
        padding: 11px 20px;
        font: inherit;
        font-weight: 600;
        color: #fff;
        background: var(--accent);
        border: none;
        border-radius: 10px;
        cursor: pointer;
      }
      .subscribe-form button:hover {
        background: #4338ca;
      }
```

**Command + expected:**
```bash
cd /Users/tn/dev/hermes-newsroom/web && npm run build \
  && grep -c 'id="subscribe"' dist/index.html
```
Expect `1` (the subscribe section is on the homepage). Spot‑check an article page also contains it:
```bash
ls dist/articles/*/index.html | head -1 | xargs grep -c 'id="subscribe"'
```
Expect `1`.

**Verification:** form posts to Buttondown; the `#subscribe` anchor matches `effective_subscribe_url`.

---

### Task 2.3 — Subscribe CTA on the homepage hero

**Objective:** A primary call‑to‑action above the article feed so first‑time visitors convert immediately.

**File to modify:** `/Users/tn/dev/hermes-newsroom/web/src/pages/index.astro`

**Edit:** add a CTA link to the `.intro` section (after the existing `<p>…</p>`, before `</section>`):

```astro
  <section class="intro">
    <h1>Research, synthesized for crypto.</h1>
    <p>
      Machine-written syntheses that pair each AI / security finding with a
      concrete blockchain implication. Every article is fact-gated against its
      sources and carries a disclosure label.
    </p>
    <a class="cta" href="#subscribe">Subscribe — new analysis every ~3 hours →</a>
  </section>
```

**Edit:** add to the page `<style>` block (after the `.intro p` rule):

```css
    .cta {
      display: inline-block;
      margin-top: 16px;
      font-weight: 600;
      color: var(--accent);
    }
```

**Command + expected:**
```bash
cd /Users/tn/dev/hermes-newsroom/web && npm run build && grep -c 'class="cta"' dist/index.html
```
Expect `1`.

---

### Task 2.4 — Confirm subscribe + provenance links are wired into every payload

**Objective:** Verify (no new code) that the closing tweet and Telegram footer carry the exact subscribe + verified‑source links — the funnel's only job.

**Command:**
```bash
cd /Users/tn/dev/hermes-newsroom && uv run python -c "
from newsroom.distribute.repackage import _closing_tweet, ArticleContext
ctx = ArticleContext(1,'demo','H','D','research_synthesis','b',[],[])
print(_closing_tweet(ctx))
"
```
**Expected:** text containing `https://aixcrypto.news/articles/demo` AND `https://aixcrypto.news/#subscribe`, plus the "SHA-256 verified" provenance line.

**Verification:** the subscribe URL equals `settings.effective_subscribe_url`; the article URL equals `{brand_url}/articles/{slug}`. Redeploy the site so `#subscribe` resolves:
```bash
cd /Users/tn/dev/hermes-newsroom/web && npm run build \
  && npx --yes wrangler pages deploy dist --project-name aixcrypto-news --branch main
```

---

# Phase 3 — The 14‑day experiment

> Outcome: a daily, mostly‑automated cadence with one manual posting+engagement step, weekly review, and a hard Day‑14 decision. The cron (Task 1.8) handles ingest→publish→repackage→deploy; the agent handles posting and engagement.

---

### Task 3.1 — Stand up the metrics tracker

**Objective:** One place to log the numbers the decision gates read. No infrastructure — a CSV/sheet plus the free dashboards.

**File to create:** `/Users/tn/dev/hermes-newsroom/.hermes/experiment/metrics.csv`

```csv
date,day,threads_posted,thread_impressions_median,thread_link_click_rate_pct,x_followers,tg_post_views_median,site_unique_visitors,email_subscribers,inbound_notables,notes
2026-06-21,1,0,0,0,0,0,0,0,0,baseline
```

**Data sources (all free):**
| Metric | Where |
|---|---|
| thread impressions, link clicks, profile visits, follows | X → Analytics (per post + account) |
| Telegram post views/forwards | the post's view counter |
| site unique visitors, page views, time on page | Cloudflare Web Analytics |
| email subscribers | Buttondown dashboard |
| organic impressions/clicks | Google Search Console (lags days) |
| inbound notables | manual: DMs/QTs/replies from analysts, founders, funds |

**Verification:** the CSV exists; you can append one row per day. Open the three dashboards (X, Cloudflare, Buttondown) and confirm access.

---

### Task 3.2 — Daily distribution + posting runbook

**Objective:** A repeatable ~15‑min daily loop. Cron has already published + generated payloads; the agent posts them.

**Runbook (run once per day, ideally targeting a high‑traffic window ~14:00–16:00 UTC):**

1. **Pull the freshest payload** generated by the cron:
   ```bash
   cd /Users/tn/dev/hermes-newsroom && uv run python -c "
   import asyncio; from sqlalchemy import text; from newsroom.db import async_session_factory
   async def m():
       async with async_session_factory() as s:
           rows = (await s.execute(text(
             \"select id,channel,variant,rendered_text from distributions \"
             \"where status='generated' order by id desc limit 2\"))).all()
           for r in rows: print('---', r.id, r.channel, r.variant, '---\n', r.rendered_text, '\n')
   asyncio.run(m())"
   ```
   If nothing is `generated` (cron didn't run), generate now: `uv run newsroom distribute --latest --channel all`.

2. **Pick the hook variant** (A/B). Rotate deterministically: day 1→A, day 2→B, day 3→C, repeat. The other variants are in `payload_json.hooks`.

3. **Post the X thread** with the `xurl` skill — a reply chain. For each tweet `n`:
   - Tweet 1 (hook):
     ```
     xurl -X POST /2/tweets -d '{"text":"<hook>"}'
     ```
     capture the returned `data.id` as `PREV_ID`.
   - Tweets 2…10: reply to the previous:
     ```
     xurl -X POST /2/tweets -d '{"text":"<tweet n>","reply":{"in_reply_to_tweet_id":"<PREV_ID>"}}'
     ```
     update `PREV_ID` each time. Tweet 10 is the auto‑generated provenance + subscribe closer.
   - Record the **tweet‑1 URL** (`https://x.com/<handle>/status/<first id>`).

4. **Post the Telegram bullets** with the `telegram` skill to `settings.telegram_channel`:
   - Send the `rendered_text` of the `telegram` row as one message.

5. **Record posting status** back onto the rows (so analytics + idempotency work):
   ```bash
   cd /Users/tn/dev/hermes-newsroom && uv run python -c "
   import asyncio; from sqlalchemy import text; from newsroom.db import async_session_factory
   X_ID=NNN; X_URL='https://x.com/aixcrypto_news/status/...'   # fill in
   async def m():
       async with async_session_factory() as s:
           await s.execute(text(\"update distributions set status='posted', external_url=:u, posted_at=now() where id=:i\"), {'u':X_URL,'i':X_ID})
           await s.commit()
   asyncio.run(m())"
   ```

**Verification:** the thread is live as a connected chain ending in the provenance/subscribe tweet; the Telegram post is in the channel; both rows are `status='posted'` with URLs.

---

### Task 3.3 — Manual engagement protocol (the 2‑hour follow‑up)

**Objective:** Compound reach — the cheapest growth lever during validation.

**Per posted thread:**
1. **+2 hours:** self‑reply to tweet 1 with *"One thing the thread didn't have room for: …"* — a genuinely additional, specific data point pulled from the article's locked claims (not a restatement). This bumps the thread and rewards readers.
2. **Engage 5 relevant accounts/day:** leave one substantive reply (a number, a caveat, a counter‑point) on threads from AI‑safety researchers, crypto‑infra builders, and funds. No "great thread 🙏". Bring a fact.
3. **Quote‑tweet 1 adjacent paper/news/day** with the AI×crypto bridge angle, linking the relevant published synthesis.
4. **Log inbound notables** (DMs, QTs, replies from accounts of standing) in `metrics.csv` → `inbound_notables`.

**Verification:** daily: 1 follow‑up reply per thread, 5 outbound replies, 1 quote‑tweet, inbound logged.

---

### Task 3.4 — Weekly review + Day‑14 decision gate

**Objective:** Two checkpoints (Day 7, Day 14) that turn the raw numbers into a decision.

**Day 7 review:**
```bash
cd /Users/tn/dev/hermes-newsroom && cat .hermes/experiment/metrics.csv
```
- Compute cumulative + medians vs. the **Metrics Dashboard** thresholds below.
- If clearly trending RED on ≥3 metrics by Day 7, consider flipping ONE variable early (see AMBIGUOUS rule) rather than waiting.

**Day 14 decision** — apply the **Decision Gates** (next section). Write the verdict + rationale to:
**File to create:** `/Users/tn/dev/hermes-newsroom/.hermes/experiment/decision.md` — a short memo: each metric's final value, its color, the count of green/red, the gate triggered (KILL / INVEST / AMBIGUOUS), and the concrete next action.

**Verification:** `decision.md` exists with a clear, numbers‑backed verdict and a named next step.

---

## 4. Summary of all tasks

| # | Task | Phase | Est. min |
|---|---|---|---|
| 0.1 | Point Astro `site` at production URL | Deploy | 10 |
| 0.2 | Deploy `web/dist` to Cloudflare Pages (Wrangler) | Deploy | 25 |
| 0.3 | Configure custom domain (skippable) | Deploy | 15 |
| 0.4 | Enable Cloudflare Web Analytics | Deploy | 10 |
| 0.5 | Create dedicated brand X account | Deploy | 15 |
| 0.6 | Submit sitemap to Google Search Console | Deploy | 15 |
| 1.1 | Add distribution settings to config | Pipeline | 10 |
| 1.2 | `distributions` table (model + migration) | Pipeline | 20 |
| 1.3 | X thread repackaging (prompts + generator) | Pipeline | 40 |
| 1.4 | Telegram repackaging review + smoke test | Pipeline | 15 |
| 1.5 | `distribute` CLI command | Pipeline | 25 |
| 1.6 | Full‑cycle + arXiv scripts | Pipeline | 20 |
| 1.7 | Temporal `distribute` activity | Pipeline | 20 |
| 1.8 | Install cron schedule | Pipeline | 15 |
| 2.1 | Buttondown + RSS feed (`@astrojs/rss`) | Capture | 20 |
| 2.2 | Email signup form in site shell | Capture | 15 |
| 2.3 | Subscribe CTA on homepage | Capture | 10 |
| 2.4 | Verify subscribe/provenance links in payloads | Capture | 5 |
| 3.1 | Stand up metrics tracker | Experiment | 20 |
| 3.2 | Daily distribution + posting runbook (setup) | Experiment | 15 |
| 3.3 | Manual engagement protocol (setup) | Experiment | 5 |
| 3.4 | Weekly review + decision gate (setup) | Experiment | 10 |
| | **Total build time** | | **~355 min (~6 h)** |

> The 14‑day experiment itself runs after build: ~15 min/day posting + ~15 min/day engagement, plus the Day‑7 and Day‑14 reviews. Not counted in build time.

---

## 5. Metrics dashboard — success / kill thresholds

Evaluated **cumulatively over 14 days** (~14 threads). Each metric is colored at Day 14.

| Metric | 🔴 KILL (red) | 🟡 Ambiguous | 🟢 INVEST (green) | Source |
|---|---|---|---|---|
| Email subscribers | < 25 | 25–149 | ≥ 150 | Buttondown |
| Brand X followers | < 20 | 20–74 | ≥ 75 | X account |
| Median thread impressions | < 500 | 500–4,999 | ≥ 5,000 | X analytics |
| Thread link‑click rate (clicks/impr) | < 0.5% | 0.5–2.9% | ≥ 3% on ≥3 threads | X analytics |
| Unique site visitors | < 150 | 150–999 | ≥ 1,000 | CF Web Analytics |
| Inbound notables (analyst/founder/fund) | 0 | 1–2 | ≥ 3 | manual log |

**Secondary signals (context, not gating):** Telegram median views, GSC impressions/clicks (lag days), avg time on article page (> 60s = real reads), follow‑rate per 1k impressions.

---

## 6. Decision gates (Day 14)

Count the colors across the **6 gating metrics** above.

### 🟢 INVEST — build the platform
**Trigger:** ≥ 3 metrics green (and none of the two "demand‑proof" metrics — subscribers and inbound notables — are red).
**Action:** Demand is real. Move from "Ferrari in a garage" to platform: invest in (a) higher article cadence + more article types in the rotation, (b) a proper newsletter tier, (c) an indexed SEO cluster build‑out, (d) begin a paid/premium feed experiment. Keep provenance as the headline differentiator.

### 🔴 KILL or PIVOT‑to‑B2B
**Trigger:** ≥ 3 metrics red.
**Action:** The *consumer* thread audience isn't there. Before fully killing, test the B2B angle for **one week**: the provenance‑locked, fact‑gated synthesis as a **data feed/API for funds & research desks** (the SHA‑256 verifiability is worth more to a compliance‑sensitive buyer than to a retail reader). Send 10 direct outreach messages to crypto funds/analysts offering the verified feed. If 0 replies of interest → kill. If ≥ 2 → pursue B2B, drop consumer distribution.

### 🟡 AMBIGUOUS — one more week, flip one variable
**Trigger:** anything else (mixed greens/yellows/reds, no clear majority).
**Action:** Extend 7 days and change **exactly one** variable (so the result is attributable). Pick the highest‑leverage one not yet validated:
1. **Article type** — switch the rotation to the type with the best click‑rate (e.g. `regulatory_signal` or `prediction_market_signal` often out‑hook `research_synthesis`).
2. **Posting time** — move to the window with the best impressions in week 1.
3. **Hook style** — lock to the A/B hook variant with the best week‑1 engagement.
Re‑evaluate against the same dashboard at Day 21. Two ambiguous cycles in a row → treat as KILL.

---

## 7. Guardrails & invariants (do not violate)

- **Do not modify** ingest / research / draft / factcheck / humanize / publish stage logic. This plan only *adds* a distribution stage and a deploy path.
- **The site stays a static SSG.** No server, no DB‑driven pages. `distributions` is DB‑only state; it is never rendered into `web/`.
- **Links are never trusted to the LLM.** The provenance link and subscribe link are code‑generated from config in `_closing_tweet` / `_telegram_render`.
- **Distribution is best‑effort.** It must never fail a pipeline run (Temporal activity catches all; CLI failures don't touch published content).
- **Honesty carries through.** Threads/posts describe machine‑generated, fact‑gated synthesis; the provenance claim ("hash‑locked, SHA‑256 verified") must remain literally true — it is, via the existing fact gate.
- **Budget:** distribution LLM spend (~$0.01/article) stays inside the existing $3/day envelope; the kill‑switch still gates the upstream run.
- **Secrets:** Cloudflare/Buttondown/X tokens live in their own dashboards or `.env` (gitignored) — never hard‑coded. `BUTTONDOWN_USER`, `YOUR_CF_BEACON_TOKEN`, and the Google verification token are the only placeholders to replace, all non‑secret.

---

## 8. Appendix — replace‑these placeholders checklist

| Placeholder | Where | Replace with |
|---|---|---|
| `https://aixcrypto.news` (BRAND_URL) | `astro.config.mjs`, `config.py`, this plan | your domain or `*.pages.dev` |
| `aixcrypto-news` | wrangler commands, scripts | your CF Pages project name |
| `@aixcrypto_news` | `config.py`, closing tweet | your final brand handle |
| `YOUR_CF_BEACON_TOKEN` | `BaseLayout.astro` | Cloudflare Web Analytics token |
| `BUTTONDOWN_USER` | `BaseLayout.astro` | your Buttondown username |
| `TELEGRAM_CHANNEL` (`telegram_channel`) | `.env` / `config.py` | your Telegram @channel or chat id |
| `/Users/tn/.local/bin/uv` | `scripts/*.sh` | output of `which uv` |
| Google verification token | `web/public/` | GSC HTML‑file verification token |

**Done means:** the site is live with analytics + sitemap; `newsroom distribute --latest` prints a crafted thread + Telegram post and logs rows; the footer captures email; the cron is installed; and `metrics.csv` is collecting the numbers that will trigger the Day‑14 KILL / INVEST / AMBIGUOUS decision.
