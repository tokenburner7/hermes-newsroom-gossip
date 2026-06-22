"""Application configuration via pydantic-settings.

Everything is sourced from environment variables (or a local `.env`) with
sensible defaults so the project is runnable out of the box for Phase 0.

Notes
-----
* ``DATABASE_URL`` uses the ``postgresql+psycopg`` dialect. psycopg 3 backs
  *both* the synchronous engine (used by Alembic) and the async engine (used
  by :mod:`newsroom.db`), so a single URL drives the whole project.
* The Docker Postgres is published on host port **5433** (see
  ``docker-compose.yml``) because a local Postgres already owns 5432 on this
  machine. Override ``DATABASE_URL`` if your setup differs.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings, populated from env / ``.env``."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Database -----------------------------------------------------------
    database_url: str = (
        "postgresql+psycopg://hermes:hermes@localhost:5433/hermes_newsroom"
    )
    # --- LLM provider credentials ------------------------------------------
    deepseek_api_key: str = ""
    openrouter_api_key: str = ""

    # --- Provider base URLs (OpenAI-compatible) ----------------------------
    deepseek_base_url: str = "https://api.deepseek.com"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"

    # --- Model ladder -------------------------------------------------------
    model_primary: str = "deepseek-chat"           # DeepSeek V4 Pro (workhorse)
    model_escalation: str = "deepseek-chat"         # same model for now; add 70B+ later
    # Cross-family eval judge, routed via OpenRouter (an *independent* family from
    # the DeepSeek primary, so the eval judge can't rubber-stamp its own output).
    # Empty => eval_judge falls back to the primary model with a note logged.
    model_eval_judge: str = ""

    # --- Quality gate / escalation (Phase 1 Week 2) ------------------------
    # Below this gate-judge quality score a draft is eligible for re-drafting on
    # the escalation model (subject to the per-day escalation cap).
    # Widened from 0.80 → 0.70 for the gossip vertical: tabloid content is
    # inherently softer and the wider gate accepts single-sourced/speculative
    # content that still meets the provenance bar.
    quality_gate_threshold: float = 0.70
    # Every Nth article also gets the cross-family eval judge (sampled audit).
    # Reduced frequency (5→8) to increase throughput for the gossip vertical.
    eval_sample_rate: int = 8

    # --- Humanizer (Phase 1 Week 2) ----------------------------------------
    # Off by default for Phase 1; flip on (or pass `run-once --humanize`) once the
    # NER-verification pass rate is trusted.
    humanize_enabled: bool = False
    # --- Embeddings (O-M1: bge-base is 768-dim, matching VECTOR(768)) -------
    embedding_model: str = "BAAI/bge-base-en-v1.5"
    embedding_dim: int = 768

    # --- Dedup (Day 5: simhash + cosine near-dup + title uniqueness) -------
    # Cosine similarity (1 - distance) at/above which a draft is a near-dup of an
    # existing article and should be suppressed. This is the *static* fallback;
    # Phase 3 recomputes an adaptive threshold into system_state['dedup_threshold']
    # (see ``adaptive_threshold_*`` below and :func:`newsroom.dedup.recompute_threshold`).
    dedup_similarity_threshold: float = 0.86
    # Max Hamming distance between 64-bit content simhashes to treat two sources
    # as near-identical (small => stricter).
    dedup_simhash_hamming_threshold: int = 3
    # Headlines closer than this Levenshtein edit distance are flagged as
    # insufficiently distinct.
    title_min_edit_distance: int = 10

    # --- Adaptive similarity threshold (Phase 3, O-M3) ---------------------
    # When enabled, ``newsroom dedup-recompute`` replaces the static threshold
    # with the Nth percentile of top-1 cosine similarity over recently-accepted
    # articles, so the near-dup gate tracks the corpus as it grows.
    adaptive_threshold_enabled: bool = True
    # Recompute window: accepted (published) articles within the last N days.
    adaptive_threshold_lookback_days: int = 7
    # Percentile of the top-1 similarity distribution to adopt as the threshold.
    adaptive_threshold_percentile: float = 95.0
    # Hard ceiling on the adaptive threshold (never block above this).
    adaptive_threshold_cap: float = 0.95
    # Below this many usable samples the existing threshold is kept untouched.
    adaptive_threshold_min_samples: int = 10

    # --- Programmatic SEO clusters (Phase 3) -------------------------------
    # Cosine similarity above which two articles are linked into the same SEO
    # cluster (pillar + supporting). Looser than the dedup gate on purpose: a
    # cluster is "same beat", not "same article".
    cluster_similarity_threshold: float = 0.7

    # --- Corrections / retraction workflow (Phase 3) -----------------------
    corrections_enabled: bool = True

    # --- External health signal: Google Search Console (Phase 3, Gate 0) ---
    # The verified Search Console property URL (e.g. https://example.com/). Empty
    # until a real indexed presence exists, which is why GSC wiring is Phase 3.
    gsc_site_url: str = ""
    # When set, the GSC health check uses the real API framework; when empty it
    # returns a healthy stub (see :mod:`newsroom.gsc_health`).
    gsc_api_key: str = ""

    # --- Distribution & validation -----------------------------------------
    # Canonical production site URL (Cloudflare Pages). No trailing slash.
    brand_url: str = "https://aixcrypto.news"  # TODO: update for gossip launch
    # Brand X/Twitter handle (the dedicated account, not a personal one).
    x_handle: str = "@aixcrypto_news"           # TODO: update for gossip launch
    # Telegram target for the `telegram` skill: @channel username or chat id.
    telegram_channel: str = ""
    # Where threads/posts send readers to subscribe. Empty => brand_url + '#subscribe'.
    subscribe_url: str = ""
    # Model used to repackage articles into threads/posts (cheap workhorse).
    distribute_model: str = "deepseek-chat"
    # Master switch for the distribute stage (CLI + Temporal activity).
    distribution_enabled: bool = True

    # --- Budget / safety (O-C3) --------------------------------------------
    # Raised from $3 → $4 for the gossip vertical: more article types, wider
    # criteria, and softer gates mean more throughput.
    daily_ceiling_usd: float = 4.0
    escalation_cap: int = 5

    # --- Temporal orchestration (Phase 2A, plan §6) ------------------------
    temporal_host: str = "localhost:7233"
    temporal_namespace: str = "newsroom"
    temporal_task_queue: str = "newsroom-pipeline"

    # --- Circuit breakers (Phase 2A, plan §6) ------------------------------
    # Shared by the per-source ingest breaker and the per-provider LLM breaker.
    # Open after this many consecutive failures; after recovery_s, allow a single
    # half-open probe; a failed probe re-opens with doubled backoff up to max.
    circuit_breaker_fail_threshold: int = 5
    circuit_breaker_recovery_s: float = 30.0
    circuit_breaker_max_backoff_s: float = 300.0  # 5 min ceiling on re-open backoff

    # --- External data sources (Phase 1) -----------------------------------
    sec_user_agent: str = "HermesNewsroom/0.1 (contact@example.com)"
    coingecko_api_key: str = ""
    fred_api_key: str = ""
    bls_api_key: str = ""
    bluesky_handle: str = ""
    bluesky_app_password: str = ""
    polymarket_base_url: str = "https://gamma-api.polymarket.com"

    # --- Source feature flags (Phase 1 — Gossip vertical) -------------------
    # Crypto sources: all off
    enable_sec: bool = False
    enable_polymarket: bool = False
    enable_coingecko: bool = False
    enable_fred: bool = False
    enable_gdelt: bool = False
    enable_hackernews: bool = False
    enable_bluesky: bool = False
    enable_bls: bool = False
    enable_treasury: bool = False
    # arXiv: off (academic papers — not gossip-relevant)
    enable_arxiv: bool = False
    # Reddit: ON — retargeted to gossip subs (r/popculturechat, r/Fauxmoi, etc.)
    enable_reddit: bool = True
    # Gossip RSS sources
    enable_tmz: bool = True
    enable_pagesix: bool = True
    enable_deadline: bool = True
    enable_variety: bool = True
    enable_justjared: bool = True
    enable_eonline: bool = True
    enable_buzzfeed: bool = True
    enable_usweekly: bool = True
    enable_thewrap: bool = True
    # X/Twitter gossip scrape
    enable_x_gossip: bool = True

    # --- LLM client (Day 3) ------------------------------------------------
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 2

    # --- Observability: OpenTelemetry (Phase 2 §6) -------------------------
    otel_enabled: bool = True
    otel_service_name: str = "hermes-newsroom"
    # OTLP HTTP traces endpoint (e.g. http://localhost:4318/v1/traces). Empty =>
    # no OTLP; spans go to the console exporter below (Phase-2 dev default).
    otel_exporter_otlp_endpoint: str = ""
    # Print spans to stderr when no OTLP endpoint is set (keeps the rich stdout
    # tables clean while still surfacing traces in development).
    otel_console_export: bool = True

    # --- Observability: Langfuse LLM tracing (optional, self-hosted) -------
    # Off by default; needs both keys to actually export. When disabled or
    # unconfigured the LLM-trace path is a no-op stub (see telemetry.py).
    langfuse_enabled: bool = False
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "http://localhost:3000"

    # --- Drift monitor (Phase 2, O-C2) -------------------------------------
    drift_check_enabled: bool = True
    # Trailing window length (days): current = last N days, baseline = prior N.
    drift_window_days: int = 7
    # KS-test alert thresholds: drift if p < p-value OR KS statistic > ks.
    drift_ks_threshold: float = 0.3
    drift_p_value_threshold: float = 0.05
    # Minimum gate scores per window before a KS-test is meaningful.
    drift_min_samples: int = 5

    # --- Source-health monitor (Phase 2, O-M2) -----------------------------
    # Ingestion-volume drop (vs the same-elapsed 7-day baseline) that alerts.
    source_health_drop_threshold: float = 0.5  # 50% drop => red alert
    source_health_baseline_days: int = 7

    @property
    def langfuse_configured(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def deepseek_configured(self) -> bool:
        return bool(self.deepseek_api_key)

    @property
    def openrouter_configured(self) -> bool:
        return bool(self.openrouter_api_key)

    @property
    def effective_subscribe_url(self) -> str:
        """Subscribe destination: explicit override, else the site's #subscribe anchor."""
        return self.subscribe_url or f"{self.brand_url.rstrip('/')}/#subscribe"

    @property
    def coingecko_configured(self) -> bool:
        return bool(self.coingecko_api_key)

    # --- PDF extraction (Day 2) --------------------------------------------
    pdf_max_chars: int = 8000
    pdf_download_timeout_s: float = 30.0
    http_user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    # --- arXiv ingestion ----------------------------------------------------
    arxiv_min_interval_s: float = 3.0
    arxiv_page_size: int = 100
    arxiv_max_results: int = 200
    arxiv_query: str = "cat:cs.AI OR cat:cs.CR OR cat:cs.LG"


@lru_cache
def get_settings() -> Settings:
    """Return a cached :class:`Settings` instance."""
    return Settings()


# Module-level singleton: ``from newsroom.config import settings``
settings = get_settings()
