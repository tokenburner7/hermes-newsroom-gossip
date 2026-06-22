#!/usr/bin/env python3
"""Automated newsroom cycle — one round-robin article per invocation.

Designed to be called by cron every 6h. A single cycle:

  1. Loads (or initialises) the persistent cycle state.
  2. Picks the next article type, round-robin from ``CYCLE_TYPES``.
  3. Checks source freshness; ingests any source class older than 24h first.
  4. Runs ``newsroom run-once --type <type> --publish --humanize --skip-embed``.
  5. On a passing fact gate, rebuilds the Astro site and deploys to Cloudflare.
  6. Advances and persists the cycle state.
  7. Appends one audit row to the experiment metrics CSV.

Decoupling: the pipeline is driven purely through the ``newsroom`` CLI via
``subprocess`` — this script never imports pipeline code. The only direct DB
access is a read-only freshness/slug lookup over the ``sources``/``articles``
tables.

Failure policy: a cycle never exits non-zero. Ingest/run/build/deploy failures
are logged and swallowed so cron keeps cycling on the next tick.
"""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- Paths & constants -------------------------------------------------------

REPO = Path("/Users/tn/dev/hermes-newsroom-v2-gossip")
WEB_DIR = REPO / "web"
STATE_FILE = Path("/Users/tn/.hermes/newsroom_gossip_cycle_state.json")
METRICS_CSV = REPO / ".hermes" / "experiment" / "metrics.csv"

CF_PROJECT = "aixcrypto-news"
# Gossip sources are fast-moving; 4h freshness is reasonable.
FRESHNESS_MAX_AGE_H = 4

#: Gossip article types to cycle through. Drama-first rotation biased toward
#: scandal, sightings, feuds, and blind items — the high-energy tabloid types
#: that fuel the new UX (BREAKING bars, heat meters, EXCLUSIVE stamps).
CYCLE_TYPES: list[str] = [
    "breaking_sighting",        # tmz / justjared
    "blind_item",               # reddit
    "feud_coverage",            # reddit
    "scandal_alert",            # tmz — NEW: breaking scandal
    "relationship_update",      # tmz / usweekly
    "casting_news",             # deadline / variety
    "album_drop",               # variety / buzzfeed
    "who_wore_it_better",       # justjared / eonline — NEW: fashion face-off
    "fashion_moment",           # eonline / justjared
    "viral_moment",             # reddit / buzzfeed
    "box_office_report",        # deadline / thewrap
    "career_milestone",         # variety / deadline
]

#: Article type -> source class(es) whose freshness gates the run.
TYPE_FRESHNESS: dict[str, list[str]] = {
    "breaking_sighting": ["tmz", "justjared"],
    "blind_item": ["reddit"],
    "feud_coverage": ["reddit"],
    "scandal_alert": ["tmz", "pagesix", "x_gossip"],
    "relationship_update": ["tmz", "usweekly", "pagesix"],
    "casting_news": ["deadline", "variety"],
    "album_drop": ["variety", "buzzfeed"],
    "who_wore_it_better": ["justjared", "eonline"],
    "fashion_moment": ["eonline", "justjared"],
    "viral_moment": ["reddit", "buzzfeed"],
    "box_office_report": ["deadline", "thewrap"],
    "career_milestone": ["variety", "deadline"],
}

#: metrics.csv schema (engagement columns are filled later by the daily runbook).
METRICS_HEADER = [
    "date", "article_slug", "channel", "impressions", "engagements",
    "link_clicks", "subscribers_new", "subscribers_total", "site_visitors",
    "return_visitors", "citation_clicks", "median_dwell_s", "notes",
]

#: Cron runs with a minimal PATH; all the tools we shell out to live here.
_EXTRA_PATH = "/Users/tn/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin"


# --- Small helpers -----------------------------------------------------------

def log(tag: str, msg: str) -> None:
    """Print a ``[tag] msg`` line and flush (so cron logs stay ordered)."""
    print(f"[{tag}] {msg}", flush=True)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_z(dt: datetime) -> str:
    """ISO-8601 UTC with a trailing Z, second precision."""
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def subprocess_env() -> dict[str, str]:
    """Process env with tool path prepend, wide columns, and env file loading.

    COLUMNS=400 keeps run-once markers on single lines for clean parsing.
    CF credentials are loaded explicitly from .env so cron jobs see them.
    """
    env = dict(os.environ)
    env["PATH"] = _EXTRA_PATH + ":" + env.get("PATH", "")
    env["COLUMNS"] = "400"
    # Load env files explicitly — cron may not have them pre-loaded.
    for env_path in [Path("/Users/tn/.hermes/.env"), REPO / ".env"]:
        try:
            with open(env_path) as fh:
                for line in fh:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        key, _, val = line.partition("=")
                        key, val = key.strip(), val.strip().strip("'\"").strip('"')
                        if key and key not in env:
                            env[key] = val
        except Exception:
            pass
    return env


def resolve_uv() -> str:
    """Absolute path to the ``uv`` launcher (PATH-independent for cron)."""
    return shutil.which("uv", path=_EXTRA_PATH) or shutil.which("uv") or "uv"


def resolve_npm() -> str:
    """Absolute path to ``npm`` (PATH-independent for cron)."""
    return shutil.which("npm", path=_EXTRA_PATH) or shutil.which("npm") or "npm"


def resolve_npx() -> str:
    """Absolute path to ``npx`` (PATH-independent for cron)."""
    return shutil.which("npx", path=_EXTRA_PATH) or shutil.which("npx") or "npx"


# --- Cycle state -------------------------------------------------------------

def load_state() -> dict:
    """Read the cycle state file, returning a sane default on any problem."""
    default = {"last_type": None, "last_run_at": None, "cycle_index": 0}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log("state", f"no state file — starting fresh at {STATE_FILE}")
        return default
    except (json.JSONDecodeError, OSError) as exc:
        log("state", f"WARNING: unreadable state ({exc}) — resetting to default")
        return default
    if not isinstance(data, dict) or not isinstance(data.get("cycle_index"), int):
        log("state", "WARNING: malformed state — resetting to default")
        return default
    return {**default, **data}


def save_state(state: dict) -> None:
    """Persist the cycle state atomically (write tmp, then replace)."""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
        tmp.replace(STATE_FILE)
    except OSError as exc:
        log("state", f"WARNING: could not write state file: {exc}")


# --- Database (read-only freshness + slug lookup) ----------------------------

def db_url() -> str | None:
    """psycopg connection URL from the env / .env, or None if unavailable."""
    try:
        from dotenv import load_dotenv

        load_dotenv(REPO / ".env", override=False)
    except Exception:  # noqa: BLE001 — dotenv is best-effort; env may already be set
        pass
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        return None
    # SQLAlchemy uses postgresql+psycopg://; psycopg.connect wants plain postgresql://.
    return url.replace("+psycopg", "")


def freshness_by_class(source_classes: list[str]) -> dict[str, tuple[int, datetime | None]]:
    """Return {source_class: (row_count, max_retrieved_at)} for the given classes."""
    url = db_url()
    if url is None:
        log("fresh", "WARNING: DATABASE_URL not set — skipping freshness check")
        return {}
    try:
        import psycopg

        out: dict[str, tuple[int, datetime | None]] = {}
        with psycopg.connect(url, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                for sc in source_classes:
                    cur.execute(
                        "SELECT COUNT(*), MAX(retrieved_at) FROM sources "
                        "WHERE source_class = %s",
                        (sc,),
                    )
                    count, max_ts = cur.fetchone()
                    out[sc] = (int(count), max_ts)
        return out
    except Exception as exc:  # noqa: BLE001 — DB issues must not abort the cycle
        log("fresh", f"WARNING: freshness query failed ({type(exc).__name__}: {exc})")
        return {}


def lookup_slug(article_id: int) -> str | None:
    """Best-effort slug lookup for a published article id (for metrics)."""
    url = db_url()
    if url is None or article_id is None:
        return None
    try:
        import psycopg

        with psycopg.connect(url, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT slug FROM articles WHERE id = %s", (article_id,))
                row = cur.fetchone()
                return row[0] if row else None
    except Exception:  # noqa: BLE001 — slug is non-critical metadata
        return None


# --- Freshness check + ingest ------------------------------------------------

def ensure_fresh(article_type: str) -> None:
    """Print freshness per source class; ingest any class older than 24h.

    No rows for a class is a warning, not a failure — run-once handles a missing
    source cleanly ("No source to run").
    """
    classes = TYPE_FRESHNESS.get(article_type, [])
    stats = freshness_by_class(classes)
    now = utcnow()
    for sc in classes:
        count, max_ts = stats.get(sc, (0, None))
        if count == 0 or max_ts is None:
            log("fresh", f"{sc}: 0 rows — WARNING: no data (run-once will handle)")
            continue
        age_h = (now - max_ts).total_seconds() / 3600.0
        freshest = iso_z(max_ts)
        if age_h > FRESHNESS_MAX_AGE_H:
            log("fresh", f"{sc}: {count} rows, freshest={freshest} ({age_h:.0f}h old) ✗ stale → ingesting")
            run_ingest(sc)
        else:
            log("fresh", f"{sc}: {count} rows, freshest={freshest} ({age_h:.0f}h old) ✓")


def run_ingest(source_class: str) -> None:
    """Ingest a single source class. Failures warn and continue (stale > none)."""
    cmd = [resolve_uv(), "run", "newsroom", "ingest", "--source", source_class]
    log("ingest", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, cwd=str(REPO), env=subprocess_env(),
            capture_output=True, text=True, timeout=900,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log("ingest", f"WARNING: ingest {source_class} failed to run: {exc}")
        return
    if proc.returncode == 0:
        log("ingest", f"{source_class}: ok")
    else:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        log("ingest", f"WARNING: ingest {source_class} returned {proc.returncode}: {' / '.join(tail)}")


# --- Pipeline run ------------------------------------------------------------

def run_pipeline(article_type: str) -> dict:
    """Run run-once for the type; parse the outcome from its captured output.

    Returns ``{status, fact_checked, article_id, slug, returncode}`` where status
    is one of: ``published``, ``fact_fail``, ``no_source``, ``error``.
    """
    cmd = [
        resolve_uv(), "run", "newsroom", "run-once",
        "--type", article_type, "--vertical", "gossip",
        "--publish", "--humanize", "--skip-embed",
    ]
    log("run", " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, cwd=str(REPO), env=subprocess_env(),
            capture_output=True, text=True, timeout=1800,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log("run", f"ERROR: run-once failed to execute: {exc}")
        return {"status": "error", "fact_checked": False, "article_id": None,
                "slug": None, "returncode": -1}

    out = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n", flush=True)
    return parse_run_output(out, proc.returncode)


def parse_run_output(out: str, returncode: int) -> dict:
    """Extract status / article_id / slug from captured run-once output."""
    result = {"status": "error", "fact_checked": False, "article_id": None,
              "slug": None, "returncode": returncode}

    if "No source to run" in out:
        result["status"] = "no_source"
        log("run", "no source to run — skipping (ingest may have found nothing)")
        return result

    if "kill-switch is ON" in out:
        result["status"] = "kill_switch"
        log("run", "kill-switch active — skipping cycle (reset with: newsroom killswitch off)")
        return result

    if "daily budget exhausted" in out:
        result["status"] = "budget_exhausted"
        log("run", "budget exhausted — skipping cycle (ceiling hit, will retry next tick)")
        return result

    m_id = re.search(r"persisted article id=(\d+)", out)
    if m_id:
        result["article_id"] = int(m_id.group(1))

    if "fact-gate: PASS" in out:
        result["fact_checked"] = True
        # Slug: prefer the published line; fall back to a DB lookup by id.
        m_slug = re.search(r"publish:\s+(?:already published \()?([a-z0-9][a-z0-9-]*)", out)
        slug = m_slug.group(1) if m_slug else lookup_slug(result["article_id"])
        result["slug"] = slug
        result["status"] = "published"
        log("run", f"fact-gate: PASS — article {result['article_id']} published (slug={slug})")
    elif "fact-gate: FAIL" in out:
        result["status"] = "fact_fail"
        log("run", f"fact-gate: FAIL — article {result['article_id']} drafted, not published")
    else:
        # Neither marker: surface the last non-empty line for diagnosis.
        tail = [ln for ln in out.strip().splitlines() if ln.strip()][-1:]
        log("run", f"ERROR: no fact-gate verdict (rc={returncode}): {tail[0] if tail else '(no output)'}")
    return result


# --- Build & deploy ----------------------------------------------------------

def build_and_deploy() -> bool:
    """npm build + wrangler deploy. Each step's failure is logged, never fatal."""
    if not _npm_build():
        return False
    return _wrangler_deploy()


def _npm_build() -> bool:
    cmd = [resolve_npm(), "run", "build"]
    log("build", "npm run build")
    try:
        proc = subprocess.run(
            cmd, cwd=str(WEB_DIR), env=subprocess_env(),
            capture_output=True, text=True, timeout=600,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log("build", f"ERROR: build failed to run: {exc}")
        return False
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        log("build", f"ERROR: build failed (rc={proc.returncode}):")
        for ln in tail:
            print(f"        {ln}", flush=True)
        return False
    summary = _astro_build_summary(proc.stdout or "")
    log("build", f"npm run build — {summary}")
    return True


def _astro_build_summary(stdout: str) -> str:
    """Pull a short 'N page(s) built' summary out of Astro's build output."""
    m = re.search(r"(\d+)\s+page\(s\)\s+built", stdout)
    if m:
        time_m = re.search(r"in\s+([\d.]+(?:ms|s))", stdout)
        return f"{m.group(1)} pages" + (f" ({time_m.group(1)})" if time_m else "") + " ✓"
    return "OK ✓"


def _wrangler_deploy() -> bool:
    cmd = [resolve_npx(), "wrangler", "pages", "deploy", "dist",
           "--project-name", CF_PROJECT, "--commit-dirty=true"]
    log("deploy", "npx wrangler pages deploy dist")
    try:
        proc = subprocess.run(
            cmd, cwd=str(WEB_DIR), env=subprocess_env(),
            capture_output=True, text=True, timeout=600,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log("deploy", f"ERROR: deploy failed to run: {exc}")
        return False
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-5:]
        log("deploy", f"ERROR: deploy failed (rc={proc.returncode}):")
        for ln in tail:
            print(f"        {ln}", flush=True)
        return False
    log("deploy", "wrangler pages deploy — OK")
    return True


# --- Metrics -----------------------------------------------------------------

def append_metrics(slug: str | None, notes: str) -> None:
    """Append one audit row to metrics.csv (header written if missing)."""
    try:
        METRICS_CSV.parent.mkdir(parents=True, exist_ok=True)
        needs_header = not METRICS_CSV.exists() or METRICS_CSV.stat().st_size == 0
        row = {k: "" for k in METRICS_HEADER}
        row["date"] = utcnow().strftime("%Y-%m-%d")
        row["article_slug"] = slug or ""
        row["notes"] = notes
        with METRICS_CSV.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=METRICS_HEADER)
            if needs_header:
                writer.writeheader()
            writer.writerow(row)
        log("metrics", f"logged row — slug={slug or '—'} notes='{notes}'")
    except OSError as exc:
        log("metrics", f"WARNING: could not write metrics row: {exc}")


# --- Orchestration -----------------------------------------------------------

def run_cycle() -> None:
    """Execute exactly one cycle end to end."""
    state = load_state()
    idx = state["cycle_index"] % len(CYCLE_TYPES)
    article_type = CYCLE_TYPES[idx]
    next_type = CYCLE_TYPES[(idx + 1) % len(CYCLE_TYPES)]

    started = utcnow()
    log("cycle", f"{started.strftime('%Y-%m-%d %H:%M')} UTC — starting {article_type} cycle")

    # 1. freshness → ingest stale sources
    ensure_fresh(article_type)

    # 2. run the pipeline
    result = run_pipeline(article_type)

    # 3. build + deploy only when an article actually passed the fact gate.
    # If a prior deploy failed, retry it even when this cycle has nothing new (N8).
    deployed = False
    if result["status"] == "published" or state.get("pending_deploy"):
        deployed = build_and_deploy()
        if deployed:
            state.pop("pending_deploy", None)  # clear stale flag
    else:
        log("build", f"skipped — nothing new to ship (status={result['status']})")

    # If deploy failed but article passed, flag for next cycle retry (N8).
    if result["status"] == "published" and not deployed:
        state["pending_deploy"] = True

    # 4. advance + persist state (always, so the cycle keeps rotating)
    new_state = {
        "last_type": article_type,
        "last_run_at": iso_z(started),
        "cycle_index": (idx + 1) % len(CYCLE_TYPES),
    }
    save_state(new_state)

    # 5. metrics audit row
    gate = {"published": "PASS", "fact_fail": "FAIL", "kill_switch": "kill_switch",
            "no_source": "no_source", "budget_exhausted": "budget_exhausted",
            "error": "error"}.get(result["status"], result["status"])
    notes = f"type={article_type}; fact_gate={gate}"
    if result["status"] == "published":
        notes += "; deployed" if deployed else "; deploy_failed"
    append_metrics(result["slug"], notes)

    # 6. closing summary
    if result["status"] == "published" and deployed:
        log("done", f"cycle complete — article {result['article_id']} live, next cycle: {next_type}")
    elif result["status"] == "published":
        log("done", f"cycle complete — article {result['article_id']} published but deploy failed, next cycle: {next_type}")
    else:
        log("done", f"cycle complete — no article shipped ({result['status']}), next cycle: {next_type}")


def main() -> int:
    try:
        run_cycle()
    except Exception as exc:  # noqa: BLE001 — never let cron see a non-zero exit
        log("fatal", f"unexpected error: {type(exc).__name__}: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
