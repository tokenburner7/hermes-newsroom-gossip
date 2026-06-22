"""Typer CLI entry point.

Phase 0 commands: ``ingest`` (arXiv), ``select``, ``test-llm``, ``config``,
``dbcheck``, plus the Day 4-5 pipeline drivers ``research``, ``factcheck`` and the
end-to-end ``run-once``. Later phases add ``publish``, ``replay`` and
``killswitch`` (plan §3.2).
"""

from __future__ import annotations

import asyncio

import typer
from rich.console import Console
from rich.table import Table

from .config import settings

app = typer.Typer(
    name="newsroom",
    help="Autonomous AI x Crypto newsroom — Phase 0 CLI.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()

#: Rich-styled label per circuit-breaker state, shared by `ingest-all` and `breaker`.
_BREAKER_STATE_DISP = {
    "closed": "[green]closed[/]",
    "open": "[bold red]OPEN[/]",
    "half_open": "[yellow]half-open[/]",
}

# Map article_type → source_class(es) for the selector.
# Default gossip article type → source mapping (used when no --vertical specified).
ARTICLE_TYPE_SOURCE_MAP: dict[str, str] = {
    "breaking_sighting": "tmz",
    "feud_coverage": "reddit",
    "casting_news": "deadline",
    "box_office_report": "deadline",
    "blind_item": "reddit",
    "relationship_update": "tmz",
    "album_drop": "variety",
    "fashion_moment": "eonline",
    "career_milestone": "variety",
    "viral_moment": "reddit",
}


@app.command()
def ingest(
    source: str = typer.Option(
        None,
        "--source",
        help="Ingest a single non-arXiv source (sec, polymarket, coingecko, fred, "
        "gdelt, hackernews, reddit, bluesky). Omit for arXiv.",
    ),
    since: str = typer.Option(
        "1d", "--since", help="arXiv look-back window, e.g. '1d', '12h', '2w'."
    ),
    max_results: int = typer.Option(
        None, "--max-results", help="Cap papers fetched (default: config value)."
    ),
    full_text: bool = typer.Option(
        False,
        "--full-text/--abstract-only",
        help="Download each PDF and extract full text (slower; paced to the "
        "arXiv rate limit). Default: abstract only.",
    ),
) -> None:
    """Ingest one source into `sources` (arXiv by default; --source for others)."""
    # Non-arXiv sources route through the multi-source orchestrator.
    if source is not None and source != "arxiv":
        from .ingest import ingest_source
        from .sources import SOURCES

        if source not in SOURCES:
            raise typer.BadParameter(
                f"unknown source {source!r}; choose from {', '.join(SOURCES)}"
            )
        console.print(f"[bold cyan]{source} ingest[/]")
        with console.status(f"Ingesting {source}…"):
            fetched, upserted = asyncio.run(ingest_source(source))
        _print_ingest_counts(source, fetched, upserted)
        return

    # Imported lazily so `--help` works even before the DB layer is reachable.
    from .sources.arxiv import ingest as arxiv_ingest
    from .sources.arxiv import parse_since

    try:
        window = parse_since(since)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    mode = "full-text (PDF)" if full_text else "abstract only"
    console.print(
        f"[bold cyan]arXiv ingest[/] query=[dim]{settings.arxiv_query}[/] "
        f"since=[green]{since}[/] mode=[yellow]{mode}[/] "
        f"rate=<=1 req/{settings.arxiv_min_interval_s:g}s"
    )

    status_msg = (
        "Fetching from arXiv, extracting PDFs, and upserting..."
        if full_text
        else "Fetching from arXiv and upserting..."
    )
    with console.status(status_msg):
        fetched, upserted = asyncio.run(
            arxiv_ingest(window, max_results=max_results, full_text=full_text)
        )

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("papers fetched", str(fetched))
    table.add_row("rows upserted", str(upserted))
    console.print(table)
    console.print("[bold green]ingest complete[/]")


def _print_ingest_counts(name: str, fetched: int, upserted: int) -> None:
    """Render a small fetched/upserted table for a single source."""
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("source", name)
    table.add_row("items fetched", str(fetched))
    table.add_row("rows upserted", str(upserted))
    console.print(table)
    console.print("[bold green]ingest complete[/]")


@app.command(name="ingest-all")
def ingest_all_cmd() -> None:
    """Run every ingestion source; report per-source counts and circuit-breaker state."""
    from .ingest import breaker_snapshot, ingest_all

    with console.status("Ingesting all sources…"):
        counts = asyncio.run(ingest_all())

    breakers = breaker_snapshot()
    table = Table(show_header=True, header_style="bold magenta", title="ingest-all")
    table.add_column("source")
    table.add_column("fetched", justify="right")
    table.add_column("upserted", justify="right")
    table.add_column("breaker")
    table.add_column("fails", justify="right")
    total_f = total_u = 0
    for name, (fetched, upserted) in counts.items():
        b = breakers.get(name, {})
        state = b.get("state", "closed")
        table.add_row(
            name,
            str(fetched),
            str(upserted),
            _BREAKER_STATE_DISP.get(state, state),
            str(b.get("consecutive_failures", 0)),
        )
        total_f += fetched
        total_u += upserted
    table.add_row("[bold]total[/]", f"[bold]{total_f}[/]", f"[bold]{total_u}[/]", "", "")
    console.print(table)
    console.print("[bold green]ingest-all complete[/]")


@app.command()
def breaker() -> None:
    """Show every circuit-breaker's state: LLM providers + ingest sources (plan §6)."""
    from .ingest import breaker_snapshot as source_breaker_snapshot
    from .llm import get_client, provider_breaker_snapshot
    from .sources import SOURCES

    # Breakers are created lazily per process, so a fresh CLI run has touched none
    # yet. Overlay the live snapshots onto the full known universe (every provider +
    # source) so untouched breakers still render their default CLOSED state.
    client = get_client()
    provider_states = provider_breaker_snapshot()
    source_states = source_breaker_snapshot()
    rows: list[tuple[str, str, dict]] = [
        ("provider", name, provider_states.get(name, {})) for name in client.providers
    ] + [("source", name, source_states.get(name, {})) for name in sorted(SOURCES)]

    table = Table(show_header=True, header_style="bold magenta", title="circuit breakers")
    table.add_column("kind")
    table.add_column("name")
    table.add_column("state")
    table.add_column("fails", justify="right")
    table.add_column("backoff", justify="right")
    table.add_column("retry in", justify="right")
    table.add_column("lifetime ok/fail", justify="right")

    open_count = 0
    for kind, name, snap in rows:
        state = snap.get("state", "closed")
        if state == "open":
            open_count += 1
        backoff = snap.get("backoff_s")
        retry = snap.get("retry_after_s")
        table.add_row(
            kind,
            name,
            _BREAKER_STATE_DISP.get(state, state),
            str(snap.get("consecutive_failures", 0)),
            f"{backoff:g}s" if backoff else "—",
            f"{retry:g}s" if retry else "—",
            f"{snap.get('total_successes', 0)}/{snap.get('total_failures', 0)}",
        )
    console.print(table)

    if not client.providers:
        console.print(
            "[dim]no LLM providers configured "
            "(set DEEPSEEK_API_KEY / OPENROUTER_API_KEY)[/]"
        )
    if open_count:
        console.print(f"[bold red]⚠ {open_count} breaker(s) OPEN[/]")
    else:
        console.print("[green]all breakers closed[/]")


@app.command()
def dbcheck() -> None:
    """Verify the DB connection (SELECT 1) and report source counts."""
    from .db import ping

    try:
        ok = asyncio.run(ping())
    except Exception as exc:  # noqa: BLE001 — surface any connection error
        console.print(f"[bold red]DB connection FAILED:[/] {exc}")
        raise typer.Exit(code=1) from exc
    if not ok:
        console.print("[bold red]DB check returned unexpected value[/]")
        raise typer.Exit(code=1)

    console.print("[bold green]DB OK[/]")
    counts = asyncio.run(_source_counts())
    table = Table(show_header=True, header_style="bold magenta", title="sources")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("total sources", str(counts["total"]))
    table.add_row("with full text (>2k chars)", str(counts["full_text"]))
    table.add_row("with categories", str(counts["with_categories"]))
    for sc, n in counts["by_class"].items():
        table.add_row(f"class: {sc}", str(n))
    console.print(table)


async def _source_counts() -> dict:
    """Aggregate counts over the `sources` table for `dbcheck`."""
    from sqlalchemy import func, select

    from .db import async_session_factory
    from .models import Source

    async with async_session_factory() as session:
        total = (await session.execute(select(func.count(Source.id)))).scalar_one()
        full_text = (
            await session.execute(
                select(func.count(Source.id)).where(
                    func.length(Source.cleaned_text) > 2000
                )
            )
        ).scalar_one()
        with_categories = (
            await session.execute(
                select(func.count(Source.id)).where(
                    func.cardinality(Source.categories) > 0
                )
            )
        ).scalar_one()
        by_class_rows = (
            await session.execute(
                select(Source.source_class, func.count(Source.id)).group_by(
                    Source.source_class
                )
            )
        ).all()
    return {
        "total": total,
        "full_text": full_text,
        "with_categories": with_categories,
        "by_class": {sc: n for sc, n in by_class_rows},
    }


@app.command()
def select(
    limit: int = typer.Option(20, "--limit", help="Max papers to display."),
    source_class: str = typer.Option(
        None,
        "--source-class",
        help="Filter by source class (arxiv, sec, polymarket, coingecko, fred, "
        "gdelt, hackernews, reddit, bluesky). Default: arxiv.",
    ),
) -> None:
    """List sources that pass the Phase-0 filter (cs.AI∧cs.CR OR crypto keyword)."""
    from .select import select_sources

    source_classes = [source_class] if source_class is not None else None
    results = asyncio.run(select_sources(source_classes=source_classes, limit=limit))
    if not results:
        console.print(
            "[yellow]No papers passed the Phase-0 filter.[/] "
            "Run [bold]newsroom ingest[/] first, or widen --since."
        )
        return

    table = Table(show_header=True, header_style="bold magenta", title="selected papers")
    table.add_column("#", justify="right")
    table.add_column("score", justify="right")
    table.add_column("src_id", justify="right")
    table.add_column("arxiv id")
    table.add_column("cat", justify="center")
    table.add_column("keywords")
    table.add_column("title")
    for i, r in enumerate(results, 1):
        title = (r.title[:60] + "…") if len(r.title) > 61 else r.title
        table.add_row(
            str(i),
            f"{r.score:.1f}",
            str(r.source_id),
            r.external_id,
            "[green]✓[/]" if r.category_match else "·",
            ", ".join(r.keyword_hits[:4]) or "—",
            title,
        )
    console.print(table)
    console.print(f"[bold green]{len(results)} paper(s) selected[/]")


@app.command(name="test-llm")
def test_llm(
    prompt: str = typer.Option(
        "In one sentence, what is the relationship between zero-knowledge proofs "
        "and blockchain scalability?",
        "--prompt",
        help="Prompt to send to the model.",
    ),
    model: str = typer.Option(
        None, "--model", help="Model id (default: config model_primary)."
    ),
) -> None:
    """Send a simple chat to DeepSeek (failover OpenRouter) and print the reply."""
    from .llm import LLMError, get_client

    client = get_client()
    if not client.providers:
        console.print(
            "[bold red]No LLM providers configured.[/] Set DEEPSEEK_API_KEY / "
            "OPENROUTER_API_KEY in .env."
        )
        raise typer.Exit(code=1)

    model = model or settings.model_primary
    console.print(
        f"[bold cyan]test-llm[/] model=[green]{model}[/] "
        f"providers=[dim]{', '.join(client.providers)}[/]"
    )
    messages = [
        {"role": "system", "content": "You are a concise AI×crypto research assistant."},
        {"role": "user", "content": prompt},
    ]
    try:
        with console.status("Calling the model..."):
            result = client.chat(messages, model=model, max_tokens=256, temperature=0.4)
    except LLMError as exc:
        console.print(f"[bold red]LLM call failed:[/] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"\n[bold]{result.text.strip()}[/]\n")
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("provider", result.provider)
    table.add_row("model", result.model)
    table.add_row("in_tokens", str(result.in_tokens))
    table.add_row("out_tokens", str(result.out_tokens))
    table.add_row("finish_reason", str(result.finish_reason))
    console.print(table)


@app.command()
def config() -> None:
    """Print the effective (non-secret) configuration."""
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("setting")
    table.add_column("value")
    table.add_row("database_url", _redact(settings.database_url))
    table.add_row("model_primary", settings.model_primary)
    table.add_row("model_escalation", settings.model_escalation)
    table.add_row("model_eval_judge", settings.model_eval_judge or "(not set)")
    table.add_row("embedding_model", f"{settings.embedding_model} ({settings.embedding_dim}d)")
    table.add_row("daily_ceiling_usd", f"${settings.daily_ceiling_usd:g}")
    table.add_row("escalation_cap", str(settings.escalation_cap))
    table.add_row("deepseek_key_set", "yes" if settings.deepseek_api_key else "no")
    table.add_row("openrouter_key_set", "yes" if settings.openrouter_api_key else "no")
    console.print(table)


@app.command()
def budget() -> None:
    """Show today's budget ledger and the kill-switch state."""
    from .budget import budget_status

    status = asyncio.run(budget_status())
    table = Table(
        show_header=True, header_style="bold magenta", title=f"budget {status['day']}"
    )
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("ceiling", f"${status['ceiling_usd']:.4f}")
    table.add_row("reserved", f"${status['reserved_usd']:.4f}")
    table.add_row("actual", f"${status['actual_usd']:.4f}")
    table.add_row("remaining", f"${status['remaining']:.4f}")
    table.add_row("escalations", f"{status['escalations']}/{status['escalation_cap']}")
    table.add_row(
        "kill-switch",
        "[bold red]ON[/]" if status["kill_switch"] else "[green]off[/]",
    )
    console.print(table)


@app.command()
def killswitch(
    action: str = typer.Argument(..., help="on | off | status"),
    reason: str = typer.Option(
        "", "--reason", help="Reason to record when tripping the kill-switch."
    ),
    vertical: str = typer.Option(
        "",
        "--vertical",
        help="Per-vertical kill-switch (e.g. 'finance'). Empty = global.",
    ),
) -> None:
    """Trip, reset, or inspect a kill-switch (global or per-vertical)."""
    from .budget import kill_switch_active, reset_kill_switch, trip_kill_switch

    act = action.strip().lower()
    if act == "on":
        asyncio.run(trip_kill_switch(reason, vertical=vertical))
        target = f"({vertical})" if vertical else ""
        console.print(f"[bold red]kill-switch ON[/] {target}" + (f" — {reason}" if reason else ""))
    elif act == "off":
        asyncio.run(reset_kill_switch(vertical=vertical))
        target = f"({vertical})" if vertical else ""
        console.print(f"[bold green]kill-switch OFF[/] {target}")
    elif act == "status":
        active = asyncio.run(kill_switch_active(vertical=vertical))
        target = f" ({vertical})" if vertical else ""
        state = "[bold red]ON[/]" if active else "[bold green]OFF[/]"
        console.print(f"kill-switch{target}: {state}")
    else:
        raise typer.BadParameter("action must be one of: on, off, status")


# --- Day 4-5: pipeline drivers ----------------------------------------------


def _resolve_source_id(
    source_id: int | None, *, article_type: str = "breaking_sighting",
    type_map: dict[str, str] | None = None,
) -> int | None:
    """Use the given source id, else the top-ranked source for the article type.

    If ``type_map`` is provided (from a vertical definition), it overrides the
    default ``ARTICLE_TYPE_SOURCE_MAP``.
    """
    if source_id is not None:
        return source_id
    from .select import selected_source_ids

    effective_map = type_map if type_map is not None else ARTICLE_TYPE_SOURCE_MAP
    source_class = effective_map.get(article_type, "arxiv")
    ids = asyncio.run(selected_source_ids(source_classes=[source_class], limit=1))
    return ids[0] if ids else None


def _require_providers() -> None:
    from .llm import get_client

    if not get_client().providers:
        console.print(
            "[bold red]No LLM providers configured.[/] Set DEEPSEEK_API_KEY / "
            "OPENROUTER_API_KEY in .env."
        )
        raise typer.Exit(code=1)


@app.command()
def research(
    source_id: int = typer.Argument(..., help="sources.id to research (see `select`)."),
    article_type: str = typer.Option(
        "breaking_sighting", "--type", help="Article type for the run."
    ),
) -> None:
    """Run just the research phase for one paper (native tool loop → record_claims)."""
    _require_providers()
    from .pipeline import research as run_research

    console.print(f"[bold cyan]research[/] source_id=[green]{source_id}[/]")
    with console.status("Running the DeepSeek tool loop..."):
        try:
            result = run_research([source_id], article_type)
        except (LookupError, ValueError) as exc:
            console.print(f"[bold red]research failed:[/] {exc}")
            raise typer.Exit(code=1) from exc

    table = Table(show_header=True, header_style="bold magenta", title="research result")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("run_id", str(result.run_id))
    table.add_row("claims recorded", str(len(result.claim_ids)))
    table.add_row("tool turns", str(result.tool_turns))
    table.add_row("attempts", str(result.attempts))
    table.add_row("in/out tokens", f"{result.in_tokens}/{result.out_tokens}")
    table.add_row("status", "[green]ok[/]" if result.ok else "[red]DLQ[/]")
    console.print(table)
    if result.error:
        console.print(f"[bold red]error:[/] {result.error}")
        raise typer.Exit(code=1)
    if result.research_notes:
        console.print("\n[bold]research notes[/]\n" + result.research_notes)
    console.print(f"\n[dim]claim ids: {result.claim_ids}[/]")


@app.command()
def factcheck(
    run_id: int = typer.Argument(..., help="runs.id of a completed research run."),
) -> None:
    """Run the local fact-gate on a completed research run's claims."""
    from .pipeline import fact_check

    fc = fact_check(run_id)
    if fc.total == 0:
        console.print(f"[yellow]No claims found for run_id={run_id}.[/]")
        raise typer.Exit(code=1)

    table = Table(show_header=True, header_style="bold magenta", title=f"fact-gate run {run_id}")
    table.add_column("claim_id", justify="right")
    table.add_column("source_id", justify="right")
    table.add_column("score", justify="right")
    table.add_column("method")
    table.add_column("passed", justify="center")
    for c in fc.claim_results:
        table.add_row(
            str(c.claim_id),
            str(c.source_id) if c.source_id is not None else "—",
            f"{c.entailment_score:.2f}",
            c.method,
            "[green]✓[/]" if c.passed else "[red]✗[/]",
        )
    console.print(table)
    verdict = "[bold green]PASS[/]" if fc.passed else "[bold red]FAIL[/]"
    console.print(
        f"{verdict}  {fc.num_passed}/{fc.total} claims  "
        f"pass_rate=[cyan]{fc.pass_rate:.0%}[/]  "
        f"staging≥80%=[{'green' if fc.meets_staging else 'red'}]{fc.meets_staging}[/]"
    )


@app.command()
def publish(
    run_id: int = typer.Argument(
        ..., help="runs.id of a fact-checked article to publish."
    ),
) -> None:
    """Render a fact-gated article to the Astro content collection (web/)."""
    from .pipeline import publish as run_publish

    try:
        result = run_publish(run_id)
    except ValueError as exc:
        console.print(f"[bold red]publish failed:[/] {exc}")
        raise typer.Exit(code=1) from exc

    if result.already_published:
        console.print(
            f"[yellow]already published[/] run_id={run_id} slug=[cyan]{result.slug}[/]"
        )
    else:
        console.print(f"[bold green]published[/] run_id={run_id}")

    table = Table(show_header=True, header_style="bold magenta", title="publish result")
    table.add_column("field")
    table.add_column("value")
    table.add_row("run_id", str(result.run_id))
    table.add_row("article_id", str(result.article_id))
    table.add_row("slug", result.slug)
    table.add_row("status", result.status)
    table.add_row("file", result.file_path)
    console.print(table)


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
    force: bool = typer.Option(
        False, "--force", help="Re-generate even if a payload already exists."
    ),
) -> None:
    """Repackage a published article into an X thread + Telegram post (logs to DB).

    Generation only — posting is done with scripts/post_thread.py (X) and the
    `telegram` skill (see the Phase 3 runbook). Payloads are stored in
    `distributions` for tracking. Idempotent unless --force.
    """
    if not settings.distribution_enabled:
        console.print("[yellow]distribution disabled[/] — set DISTRIBUTION_ENABLED=true.")
        raise typer.Exit()
    _require_providers()

    # Safety gate: distribution is an LLM path — honour the kill-switch like run-once.
    from .budget import kill_switch_active

    if asyncio.run(kill_switch_active()):
        console.print(
            "[bold red]kill-switch is ON[/] — refusing to distribute. "
            "Reset with [bold]newsroom killswitch off[/]."
        )
        raise typer.Exit(code=1)

    chan = channel.strip().lower()
    if chan == "all":
        channels: tuple[str, ...] = ("x", "telegram")
    elif chan in ("x", "telegram"):
        channels = (chan,)
    else:
        raise typer.BadParameter("--channel must be one of: x, telegram, all")

    from .distribute import distribute_article, distribute_latest
    from .llm import LLMError

    try:
        with console.status("Repackaging…"):
            if latest or article_id is None:
                result = distribute_latest(channels, force=force)
            else:
                result = distribute_article(article_id, channels, force=force)
    except (LookupError, ValueError, LLMError) as exc:
        console.print(f"[bold red]distribute failed:[/] {exc}")
        raise typer.Exit(code=1) from exc

    if result.distribution_ids:
        console.print(
            f"[bold green]distributed[/] article_id=[cyan]{result.article_id}[/] → {result.url}"
        )
    else:
        console.print(
            f"[yellow]nothing to do[/] — article_id=[cyan]{result.article_id}[/] "
            "already distributed (use --force to regenerate)."
        )
    for ch in result.skipped:
        console.print(f"[yellow]↷ {ch}: already distributed (use --force to regenerate)[/]")
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
        "[dim]Next: post the X thread with scripts/post_thread.py and the Telegram "
        "row with the telegram skill, then record the URL. See the Phase 3 runbook.[/]"
    )


@app.command(name="run-once")
def run_once(
    source_id: int = typer.Argument(
        None, help="sources.id to run (default: top Phase-0-selected paper)."
    ),
    skip_embed: bool = typer.Option(
        False, "--skip-embed", help="Skip the embedding step (avoids loading the model)."
    ),
    do_publish: bool = typer.Option(
        False,
        "--publish",
        help="Publish to the Astro content collection if the fact gate passes.",
    ),
    article_type: str = typer.Option(
        "breaking_sighting", "--type", help="Article type to research + draft."
    ),
    humanize_flag: bool = typer.Option(
        False,
        "--humanize",
        help="Run the verified humanizer before publish (default: off for Phase 1; "
        "also enabled by HUMANIZE_ENABLED).",
    ),
    vertical: str = typer.Option(
        "",
        "--vertical",
        help="Content vertical to use (e.g. 'finance'). Swaps STYLE_GUIDE, "
        "article-type→source map, and voice. Empty = default Gossip.",
    ),
) -> None:
    """Full Phase-1 pipeline for one paper, traced end-to-end (Phase 2 §6).

    Wraps the whole run in a single OpenTelemetry ``run_once`` span (so every
    stage span shares one trace) and prints the trace id. The pipeline itself
    lives in :func:`_run_once_impl`.
    """
    from .telemetry import current_trace_id, init_telemetry, root_span

    init_telemetry()
    with root_span("run_once", article_type=article_type, source_id=source_id) as sp:
        if sp is not None:
            console.print(f"[dim]trace_id=[/][cyan]{current_trace_id()}[/]")
        _run_once_impl(
            source_id,
            skip_embed=skip_embed,
            do_publish=do_publish,
            article_type=article_type,
            humanize_flag=humanize_flag,
            vertical=vertical,
        )


def _run_once_impl(
    source_id: int | None = None,
    *,
    skip_embed: bool = False,
    do_publish: bool = False,
    article_type: str = "breaking_sighting",
    humanize_flag: bool = False,
    vertical: str = "",
) -> None:
    """Full Phase-1 pipeline for one paper: research → draft → gate → escalate → factcheck → humanize → embed.

    With ``--publish`` the article is rendered into the Astro content collection
    (web/) when the fact gate passes; otherwise it is persisted (`fact_checked`
    if the gate passes, else `drafted`) and embedded only.

    If ``vertical`` is set (e.g. 'finance'), the corresponding STYLE_GUIDE and
    article-type→source map from ``newsroom.verticals`` are used.
    """
    _require_providers()
    from .budget import (
        ensure_budget_day,
        estimate_cost_usd,
        kill_switch_active,
        reserve,
        settle,
    )
    from .eval import gate_judge_detailed
    from .pipeline import draft as run_draft
    from .pipeline import escalate_if_needed
    from .pipeline import fact_check
    from .pipeline import persist_article
    from .pipeline import record_run_error
    from .pipeline import research as run_research

    # Resolve vertical config
    vertical_style_guide: str | None = None
    vertical_type_map: dict[str, str] | None = None
    if vertical:
        try:
            from .verticals import FINANCE_VERTICAL, GOSSIP_VERTICAL

            _VERTICALS = {"finance": FINANCE_VERTICAL, "gossip": GOSSIP_VERTICAL}
            v = _VERTICALS.get(vertical)
            if v is None:
                console.print(
                    f"[bold red]Unknown vertical:[/] {vertical!r}. "
                    f"Choose from: {', '.join(_VERTICALS)}"
                )
                raise typer.Exit(code=1)
            vertical_style_guide = v["style_guide"]
            vertical_type_map = v["article_type_source_map"]
            console.print(f"[bold cyan]vertical:[/] [green]{v['name']}[/]")
        except ImportError:
            console.print(
                f"[yellow]Vertical module not importable for {vertical!r}; "
                f"using default Gossip.[/]"
            )

    # Safety gate: never start a run while the kill-switch is tripped.
    if asyncio.run(kill_switch_active(vertical=vertical or "")):
        console.print(
            "[bold red]kill-switch is ON[/] — refusing to run. "
            "Reset with [bold]newsroom killswitch off[/]."
        )
        raise typer.Exit(code=1)

    sid = _resolve_source_id(source_id, article_type=article_type, type_map=vertical_type_map)
    if sid is None:
        console.print("[bold red]No source to run.[/] Ingest + select papers first.")
        raise typer.Exit(code=1)
    console.print(f"[bold cyan]run-once[/] source_id=[green]{sid}[/] type=[green]{article_type}[/]")

    # Reserve budget before any LLM spend; bail if the ceiling is hit.
    budget_vertical = vertical or "gossip"
    asyncio.run(ensure_budget_day(vertical=budget_vertical))
    if not asyncio.run(reserve(est_usd=0.02, vertical=budget_vertical)):
        console.print(
            "[bold red]daily budget exhausted[/] — reservation denied. "
            "See [bold]newsroom budget[/]."
        )
        raise typer.Exit(code=1)

    # 1. research
    with console.status("1/4 research (tool loop)…"):
        try:
            research_kwargs: dict = {}
            if vertical_type_map:
                research_kwargs["source_classes"] = list(
                    set(vertical_type_map.values())
                )
            research_result = run_research([sid], article_type, **research_kwargs)
        except (LookupError, ValueError) as exc:
            console.print(f"[bold red]research failed:[/] {exc}")
            raise typer.Exit(code=1) from exc
    if not research_result.ok:
        console.print(f"[bold red]research → DLQ:[/] {research_result.error}")
        raise typer.Exit(code=1)
    run_id = research_result.run_id
    console.print(
        f"  [green]✓[/] research: run_id={run_id}, "
        f"{len(research_result.claim_ids)} claims, {research_result.tool_turns} tool turns"
    )

    # 2. draft
    with console.status("2/4 draft (JSON mode)…"):
        try:
            envelope = run_draft(run_id, research_result, article_type=article_type, style_guide=vertical_style_guide)
        except ValueError as exc:
            record_run_error(run_id, f"draft failed: {exc}")
            console.print(f"[bold red]draft failed:[/] {exc}")
            raise typer.Exit(code=1) from exc
        except Exception as exc:
            record_run_error(run_id, f"draft unexpected: {type(exc).__name__}: {exc}")
            console.print(f"[bold red]draft unexpected error:[/] {type(exc).__name__}: {exc}")
            raise typer.Exit(code=1) from exc
    console.print(f"  [green]✓[/] draft: “{envelope.headline}”")

    # 2b. gate judge — score the draft (quality_score) and drive escalation
    claims_txt, source_spans = _run_claims(run_id)
    with console.status("gate judge (quality score)…"):
        gate_scores, gate_result = gate_judge_detailed(envelope.body, claims_txt, source_spans)
    if gate_result is not None:
        asyncio.run(
            settle(
                run_id,
                estimate_cost_usd(gate_result.in_tokens, gate_result.out_tokens, gate_result.model),
                vertical=budget_vertical,
            )
        )
    console.print(f"  [green]✓[/] gate judge: quality_score=[cyan]{gate_scores.weighted:.2f}[/]")

    # 2c. escalation — re-draft on the escalation model if below threshold (budget permitting)
    esc = escalate_if_needed(
        run_id, research_result, envelope, gate_scores,
        claims=claims_txt, source_spans=source_spans, article_type=article_type,
    )
    envelope = esc.envelope
    gate_scores = esc.scores
    quality_score = esc.final_score
    if esc.escalated:
        marker = "used escalated" if esc.used_escalated else "kept primary"
        console.print(f"  [green]↑[/] escalation: {esc.reason} [dim]({marker})[/]")
    else:
        console.print(f"  [dim]·[/] escalation: {esc.reason}")

    # 3. fact-gate (staging bar: ≥80% claim pass-rate for gossip verticals;
    #    the 100% bar was designed for arXiv research papers with verbatim LaTeX
    #    spans and is too strict for web-article sources.)
    fc = fact_check(run_id, envelope)
    pass_bar = fc.meets_staging
    verdict = "[green]PASS[/]" if pass_bar else "[red]FAIL[/]"
    console.print(
        f"  [{'green' if pass_bar else 'red'}]{'✓' if pass_bar else '✗'}[/] "
        f"fact-gate: {verdict} {fc.num_passed}/{fc.total} (rate={fc.pass_rate:.0%})"
        f"{' [dim](staging bar ≥80%)[/]' if pass_bar and not fc.passed else ''}"
    )

    # 3b. humanize (optional; verified — any drift falls back to the original draft)
    do_humanize = humanize_flag or settings.humanize_enabled
    body_final = None
    verify = None
    if do_humanize:
        from .pipeline import humanize_detailed, verify_humanize

        with console.status("humanize (style transfer + NER verify)…"):
            humanized_text, hum_result = humanize_detailed(envelope.body, article_type)
            if hum_result is not None:
                asyncio.run(
                    settle(
                        run_id,
                        estimate_cost_usd(hum_result.in_tokens, hum_result.out_tokens, hum_result.model),
                        vertical=budget_vertical,
                    )
                )
            verify = verify_humanize(envelope.body, humanized_text, source_spans)
        if verify.passed:
            body_final = humanized_text
            console.print(
                f"  [green]✓[/] humanize: verified clean "
                f"(fact_rate={verify.fact_pass_rate:.0%}) — using humanized body"
            )
        else:
            console.print(
                f"  [yellow]·[/] humanize: drift detected → keeping original draft "
                f"[dim]({verify.reason})[/]"
            )

    # persist
    status = "fact_checked" if fc.meets_staging else "drafted"
    article_id = persist_article(
        run_id, envelope, slug_suffix=research_result.source_ids[0],
        fact_pass_rate=fc.pass_rate, quality_score=quality_score,
        body_final_md=body_final, status=status,
        vertical=budget_vertical,
    )
    console.print(f"  [green]✓[/] persisted article id={article_id} (status={status})")

    # record judge outcomes: the gate score, the humanize verdict, and — on every
    # Nth article — the independent cross-family eval judge.
    from .eval import run_eval_judge, should_sample_eval, store_eval

    gate_scores.judge_kind = "gate"
    store_eval(article_id, gate_scores)
    if verify is not None:
        from .pipeline import record_humanize

        record_humanize(article_id, verify)
    eval_scores = None
    if should_sample_eval(run_id):
        with console.status("eval judge (independent, sampled)…"):
            eval_scores = run_eval_judge(article_id=article_id)
        console.print(
            f"  [green]✓[/] eval judge [dim]{eval_scores.judge_model}[/]: "
            f"weighted=[cyan]{eval_scores.weighted:.2f}[/]"
        )

    # Reconcile spend against the reservation.
    actual_cost = estimate_cost_usd(
        research_result.in_tokens, research_result.out_tokens, settings.model_primary
    )
    asyncio.run(settle(run_id, actual_cost, vertical=budget_vertical))
    from .telemetry import set_current_attributes

    set_current_attributes(
        cost_usd=actual_cost,
        in_tokens=research_result.in_tokens,
        out_tokens=research_result.out_tokens,
    )
    console.print(f"  [green]✓[/] budget: settled ${actual_cost:.4f} actual")

    # 4. embed
    if skip_embed:
        console.print("  [yellow]·[/] embed: skipped (--skip-embed)")
    else:
        with console.status("4/4 embed (bge-base-en-v1.5)…"):
            try:
                from .embedding import embed_article

                embed_article(article_id)
                console.print("  [green]✓[/] embed: 768-dim vector stored")
            except Exception as exc:  # noqa: BLE001 — embedding is best-effort here
                console.print(f"  [yellow]·[/] embed skipped: {type(exc).__name__}: {exc}")

    # 5. publish (optional)
    publish_result = None
    if do_publish:
        if status != "fact_checked":
            console.print(
                f"  [yellow]·[/] publish: skipped — fact gate did not pass "
                f"(status={status})"
            )
        else:
            from .pipeline import publish as run_publish

            try:
                publish_result = run_publish(run_id, envelope)
            except ValueError as exc:
                console.print(f"  [bold red]publish failed:[/] {exc}")
                raise typer.Exit(code=1) from exc
            if publish_result.already_published:
                console.print(f"  [green]✓[/] publish: already published ({publish_result.slug})")
            else:
                console.print(
                    f"  [green]✓[/] publish: {publish_result.slug} → {publish_result.file_path}"
                )

    # summary
    table = Table(show_header=True, header_style="bold magenta", title="run-once summary")
    table.add_column("field")
    table.add_column("value")
    table.add_row("run_id", str(run_id))
    table.add_row("article_id", str(article_id))
    table.add_row("headline", envelope.headline)
    table.add_row("claims used", str(len(envelope.claims_used)))
    table.add_row("implications", str(len(envelope.implications)))
    table.add_row("fact gate", "PASS" if fc.passed else "FAIL")
    table.add_row("fact pass rate", f"{fc.pass_rate:.0%}")
    table.add_row("quality score (gate)", f"{quality_score:.2f}")
    table.add_row(
        "escalated",
        "yes (used)" if esc.used_escalated else ("yes (kept primary)" if esc.escalated else "no"),
    )
    if do_humanize:
        table.add_row("humanized", "yes" if body_final else "no (drift → original)")
    if eval_scores is not None:
        table.add_row("eval judge", f"{eval_scores.weighted:.2f} ({eval_scores.judge_model})")
    table.add_row("tokens (research+draft)", str(research_result.in_tokens + research_result.out_tokens))
    table.add_row("est. cost (research)", f"${actual_cost:.4f}")
    if do_publish:
        table.add_row(
            "published",
            publish_result.slug if publish_result else "no (gate not passed)",
        )
    console.print(table)
    console.print("[bold green]run-once complete[/]")


def _run_claims(run_id: int) -> tuple[list[str], list[str]]:
    """Return (claim_texts, supporting_spans) for a run's locked claims."""
    from sqlalchemy import select as _select

    from .db import get_sync_session_factory
    from .models import Claim

    factory = get_sync_session_factory()
    with factory() as session:
        claims = list(
            session.execute(
                _select(Claim).where(Claim.run_id == run_id).order_by(Claim.id)
            ).scalars().all()
        )
    return [c.claim_text for c in claims], [c.supporting_span for c in claims]


def _print_rubric(title: str, scores) -> None:
    """Render a RubricScores as a per-criterion table."""
    from .eval import RUBRIC_WEIGHTS

    table = Table(show_header=True, header_style="bold magenta", title=title)
    table.add_column("criterion")
    table.add_column("weight", justify="right")
    table.add_column("score", justify="right")
    for crit, weight in RUBRIC_WEIGHTS.items():
        table.add_row(crit, f"{weight:.2f}", f"{getattr(scores, crit):.2f}")
    table.add_row("[bold]weighted[/]", "1.00", f"[bold cyan]{scores.weighted:.3f}[/]")
    console.print(table)
    if scores.judge_model:
        console.print(f"[dim]model: {scores.judge_model}[/]")
    if scores.note:
        console.print(f"[yellow]note:[/] {scores.note}")
    if scores.rationale:
        console.print(f"[dim]rationale:[/] {scores.rationale}")


@app.command(name="eval")
def eval_cmd(
    run_id: int = typer.Argument(..., help="runs.id of the article to evaluate."),
    article_id: int = typer.Option(
        None, "--article-id", help="Evaluate this articles.id directly (overrides RUN_ID)."
    ),
    gate_only: bool = typer.Option(
        False, "--gate-only", help="Run only the fast gate judge (skip the independent eval judge)."
    ),
) -> None:
    """Run the quality judges on an existing article: gate + independent eval judge."""
    _require_providers()
    from .eval import evaluate

    target = f"article_id={article_id}" if article_id is not None else f"run_id={run_id}"
    console.print(f"[bold cyan]eval[/] {target}")
    try:
        if article_id is not None:
            out = evaluate(article_id=article_id, with_eval=not gate_only)
        else:
            out = evaluate(run_id=run_id, with_eval=not gate_only)
    except LookupError as exc:
        console.print(f"[bold red]eval failed:[/] {exc}")
        raise typer.Exit(code=1) from exc

    _print_rubric("gate judge (primary model)", out["gate"])
    if "eval" in out:
        _print_rubric("eval judge (independent / cross-family)", out["eval"])
        diff = abs(out["gate"].weighted - out["eval"].weighted)
        console.print(f"[bold]gate ↔ eval divergence:[/] {diff:.3f}")
    console.print("[bold green]eval complete[/] — scores stored in `evals`.")


@app.command(name="eval-stats")
def eval_stats_cmd() -> None:
    """Show the eval summary: gate-vs-eval-judge agreement and correlation."""
    from .eval import eval_stats

    stats = eval_stats()

    def _fmt(x) -> str:
        if x is None:
            return "—"
        return f"{x:.3f}" if isinstance(x, float) else str(x)

    table = Table(show_header=True, header_style="bold magenta", title="eval stats")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("total eval rows", str(stats["total_evals"]))
    for kind, n in sorted(stats["counts_by_kind"].items()):
        table.add_row(f"  rows ({kind})", str(n))
    table.add_row("articles w/ gate", str(stats["articles_with_gate"]))
    table.add_row("articles w/ eval", str(stats["articles_with_eval"]))
    table.add_row("mean gate score", _fmt(stats["mean_gate"]))
    table.add_row("mean eval score", _fmt(stats["mean_eval"]))
    table.add_row("paired articles", str(stats["paired"]))
    table.add_row("mean abs diff (gate-eval)", _fmt(stats["mean_abs_diff"]))
    table.add_row("gate↔eval correlation", _fmt(stats["correlation"]))
    console.print(table)
    if not stats["pairs"]:
        console.print(
            "[yellow]No gate/eval paired articles yet.[/] "
            "Run [bold]newsroom eval RUN_ID[/] on a few articles to populate correlation."
        )


@app.command(name="dedup-check")
def dedup_check(
    source_id: int = typer.Argument(..., help="sources.id to check for duplicates."),
    store: bool = typer.Option(
        False,
        "--store/--no-store",
        help="Persist the computed simhash into sources.content_simhash.",
    ),
) -> None:
    """Run the dedup chain (url-hash -> simhash) for one source."""
    from .dedup import (
        _load_source,
        check_dedup,
        compute_simhash,
        store_simhash,
    )

    source = asyncio.run(_load_source(source_id))
    if source is None:
        raise typer.BadParameter(f"no source with id {source_id}")

    simhash_val = compute_simhash(source["text"])
    result = asyncio.run(check_dedup(source_id, source["text"], source["url"]))
    if store:
        asyncio.run(store_simhash(source_id, simhash_val))

    verdict = "[bold red]DUPLICATE[/]" if result.is_duplicate else "[bold green]unique[/]"
    table = Table(
        show_header=True,
        header_style="bold magenta",
        title=f"dedup-check source_id={source_id}",
    )
    table.add_column("field")
    table.add_column("value", justify="right")
    table.add_row("source_class", source["source_class"])
    table.add_row("url", source["url"])
    table.add_row("simhash", str(simhash_val))
    table.add_row("verdict", verdict)
    table.add_row("method", result.method)
    table.add_row(
        "duplicate_of",
        str(result.duplicate_of) if result.duplicate_of is not None else "—",
    )
    table.add_row("score", f"{result.score:.4f}")
    if store:
        table.add_row("stored", "[green]yes[/]")
    console.print(table)


@app.command(name="dedup-recompute")
def dedup_recompute(
    apply: bool = typer.Option(
        True,
        "--apply/--dry-run",
        help="Persist the new threshold into system_state (default); "
        "--dry-run only reports what would change.",
    ),
) -> None:
    """Recompute the adaptive near-dup similarity threshold (O-M3)."""
    if not settings.adaptive_threshold_enabled:
        console.print(
            "[yellow]adaptive threshold disabled[/] — set "
            "ADAPTIVE_THRESHOLD_ENABLED=true to enable."
        )
        raise typer.Exit()

    from .dedup import recompute_threshold

    with console.status("Recomputing similarity threshold…"):
        res = asyncio.run(recompute_threshold(store=apply))

    table = Table(
        show_header=True, header_style="bold magenta", title="dedup threshold recompute"
    )
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("old threshold", f"{res.old_threshold:.4f}")
    table.add_row("new threshold", f"{res.new_threshold:.4f}")
    table.add_row("usable samples", str(res.n_samples))
    table.add_row(
        f"p{settings.adaptive_threshold_percentile:g} (raw)",
        _fmt_opt(res.percentile_value, "{:.4f}"),
    )
    table.add_row("capped at 0.95", "[yellow]yes[/]" if res.capped else "no")
    delta = res.new_threshold - res.old_threshold
    if res.applied:
        verdict = f"[bold green]updated[/] ({'+' if delta >= 0 else ''}{delta:.4f})"
    elif res.n_samples < settings.adaptive_threshold_min_samples:
        verdict = "[yellow]kept (insufficient data)[/]"
    else:
        verdict = "[dim]unchanged[/]"
    table.add_row("result", verdict)
    if not apply and res.n_samples >= settings.adaptive_threshold_min_samples:
        table.add_row("stored", "[yellow]no (--dry-run)[/]")
    console.print(table)
    console.print(f"[dim]{res.reason}[/]")


# --- Human review queue (Phase 1 Week 3) -----------------------------------


@app.command()
def queue() -> None:
    """List articles awaiting human review (review_path='queued')."""
    from .db import async_session_factory
    from .models import Article
    from sqlalchemy import select

    async def _list():
        async with async_session_factory() as session:
            stmt = (
                select(Article)
                .where(Article.review_path == "queued")
                .order_by(Article.updated_at.desc())
                .limit(50)
            )
            return (await session.execute(stmt)).scalars().all()

    rows = asyncio.run(_list())
    if not rows:
        console.print("[green]Review queue is empty.[/]")
        return

    table = Table(show_header=True, header_style="bold magenta", title=f"review queue ({len(rows)} items)")
    table.add_column("id", justify="right")
    table.add_column("headline")
    table.add_column("quality", justify="right")
    table.add_column("type")
    table.add_column("updated", justify="right")
    for a in rows:
        table.add_row(
            str(a.id),
            (a.headline[:65] + "\u2026") if len(a.headline) > 66 else a.headline,
            f"{a.quality_score:.2f}" if a.quality_score else "\u2014",
            a.type or "\u2014",
            a.updated_at.strftime("%m-%d %H:%M") if a.updated_at else "\u2014",
        )
    console.print(table)


@app.command()
def review(
    action: str = typer.Argument(..., help="APPROVE or REJECT"),
    article_id: int = typer.Argument(..., help="Article ID to review."),
    reason: str = typer.Option("", "--reason", help="Reason (required for REJECT)."),
) -> None:
    """Approve or reject a queued article."""
    from datetime import datetime, timezone
    from .db import async_session_factory
    from .models import Article
    from sqlalchemy import select

    act = action.strip().upper()
    if act not in ("APPROVE", "REJECT"):
        raise typer.BadParameter("action must be APPROVE or REJECT")
    if act == "REJECT" and not reason:
        raise typer.BadParameter("--reason is required for REJECT")

    async def _apply():
        async with async_session_factory() as session:
            a = (await session.execute(select(Article).where(Article.id == article_id))).scalar_one_or_none()
            if a is None:
                return None, "not found"
            if a.review_path != "queued":
                return a, f"not queued (current: {a.review_path})"
            if act == "APPROVE":
                a.review_path = "human_reviewed"
                a.label = "AI-assisted \u00b7 human-reviewed"
                a.status = "fact_checked"
            else:
                a.review_path = "rejected"
                a.label = "Rejected \u2014 " + reason[:80]
                a.status = "rejected"
            a.updated_at = datetime.now(timezone.utc)
            await session.commit()
            return a, act.lower()

    a, status = asyncio.run(_apply())
    if a is None:
        console.print(f"[red]{status}[/]")
    else:
        console.print(f"[green]\u2713[/] Article {article_id}: [bold]{a.headline[:80]}[/] \u2192 {a.review_path}")


@app.command(name="queue-stats")
def queue_stats() -> None:
    """Show review queue statistics."""
    from .db import async_session_factory
    from .models import Article
    from sqlalchemy import select, func

    async def _stats():
        async with async_session_factory() as session:
            total = (await session.execute(select(func.count()).select_from(Article))).scalar()
            queued = (await session.execute(
                select(func.count()).where(Article.review_path == "queued").select_from(Article)
            )).scalar()
            human = (await session.execute(
                select(func.count()).where(Article.review_path == "human_reviewed").select_from(Article)
            )).scalar()
            auto = (await session.execute(
                select(func.count()).where(Article.review_path == "auto_gated").select_from(Article)
            )).scalar()
            return {"total": total, "queued": queued, "auto_gated": auto, "human_reviewed": human}

    stats = asyncio.run(_stats())
    table = Table(show_header=True, header_style="bold magenta", title="queue stats")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row("total articles", str(stats["total"]))
    table.add_row("auto-gated", str(stats["auto_gated"]))
    table.add_row("human reviewed", str(stats["human_reviewed"]))
    table.add_row("queued", str(stats["queued"]))
    console.print(table)


# --- Corrections / retraction workflow (Phase 3) ----------------------------


@app.command()
def retract(
    article_id: int = typer.Argument(..., help="articles.id to retract."),
    reason: str = typer.Option(..., "--reason", help="Why the article is retracted."),
) -> None:
    """Retract an article: status=retracted, add notice, bump dateModified, republish."""
    if not settings.corrections_enabled:
        console.print("[yellow]corrections disabled[/] — set CORRECTIONS_ENABLED=true.")
        raise typer.Exit()

    from .corrections import retract as run_retract

    try:
        res = run_retract(article_id, reason)
    except (LookupError, ValueError) as exc:
        console.print(f"[bold red]retract failed:[/] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[bold red]⛔ retracted[/] article {article_id}: [bold]{res.slug}[/]")
    table = Table(show_header=True, header_style="bold magenta", title="retraction")
    table.add_column("field")
    table.add_column("value")
    table.add_row("article_id", str(res.article_id))
    table.add_row("status", res.status)
    table.add_row("reason", res.note)
    table.add_row("file", res.file_path)
    console.print(table)


@app.command()
def correct(
    article_id: int = typer.Argument(..., help="articles.id to correct."),
    note: str = typer.Option(..., "--note", help='Correction note, e.g. "Fixed X to Y".'),
) -> None:
    """Apply a correction: append note, bump correction_count + dateModified, republish."""
    if not settings.corrections_enabled:
        console.print("[yellow]corrections disabled[/] — set CORRECTIONS_ENABLED=true.")
        raise typer.Exit()

    from .corrections import correct as run_correct

    try:
        res = run_correct(article_id, note)
    except (LookupError, ValueError) as exc:
        console.print(f"[bold red]correct failed:[/] {exc}")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[bold green]✏️ corrected[/] article {article_id} "
        f"(correction #{res.correction_count}): [bold]{res.slug}[/]"
    )
    table = Table(show_header=True, header_style="bold magenta", title="correction")
    table.add_column("field")
    table.add_column("value")
    table.add_row("article_id", str(res.article_id))
    table.add_row("status", res.status)
    table.add_row("correction_count", str(res.correction_count))
    table.add_row("note", res.note)
    table.add_row("file", res.file_path)
    console.print(table)


# --- Phase 2 §6: drift, source health, dashboard ----------------------------

#: Rich colour per source-health status, plus the trend glyph it renders with.
_HEALTH_STYLE = {
    "green": ("green", "→"),
    "yellow": ("yellow", "↘"),
    "red": ("bold red", "↓"),
    "new": ("cyan", "new"),
    "idle": ("dim", "—"),
}


def _fmt_opt(value, fmt: str = "{:.3f}", dash: str = "—") -> str:
    """Format an optional number, rendering ``None`` as a dash."""
    return dash if value is None else fmt.format(value)


@app.command()
def drift() -> None:
    """Run the KS drift check on the trailing gate-score distribution (O-C2)."""
    if not settings.drift_check_enabled:
        console.print(
            "[yellow]drift check disabled[/] — set DRIFT_CHECK_ENABLED=true to enable."
        )
        raise typer.Exit()

    from .eval import compute_drift

    with console.status("Running KS drift check…"):
        res = compute_drift()

    if res.insufficient:
        console.print(f"[yellow]drift: insufficient data[/] — {res.note}")
        console.print(
            f"[dim]baseline={res.n_baseline} scores, current={res.n_current} scores; "
            f"need ≥{settings.drift_min_samples} per window.[/]"
        )
        return

    table = Table(show_header=True, header_style="bold magenta", title="drift check (KS-test)")
    table.add_column("metric")
    table.add_column("value", justify="right")
    table.add_row(
        "baseline window",
        f"{res.n_baseline} scores · median {_fmt_opt(res.baseline_median)}",
    )
    table.add_row(
        "current window",
        f"{res.n_current} scores · median {_fmt_opt(res.current_median)}",
    )
    table.add_row("KS statistic", f"{res.ks_statistic:.3f}  (alert > {settings.drift_ks_threshold})")
    table.add_row("p-value", f"{res.p_value:.4f}  (alert < {settings.drift_p_value_threshold})")
    verdict = (
        "[bold red]DRIFT DETECTED[/]" if res.drift_detected else "[bold green]stable[/]"
    )
    table.add_row("verdict", verdict)
    console.print(table)
    console.print(f"[dim]{res.note}[/] — result stored in `evals` (judge_kind='drift').")


@app.command()
def health() -> None:
    """Show ingestion health per source (volume vs the same-elapsed 7-day baseline)."""
    from .sources.health import check_source_health

    with console.status("Checking source health…"):
        results = asyncio.run(check_source_health())

    table = Table(show_header=True, header_style="bold magenta", title="source health")
    table.add_column("source")
    table.add_column("status", justify="center")
    table.add_column("today", justify="right")
    table.add_column("7d avg", justify="right")
    table.add_column("trend", justify="right")
    for source_class in sorted(results):
        st = results[source_class]
        style, glyph = _HEALTH_STYLE.get(st.status, ("white", "?"))
        if st.status in ("red", "yellow"):
            trend = f"{glyph} {st.drop_pct:.0%}"
        else:
            trend = glyph
        table.add_row(
            source_class,
            f"[{style}]{st.status}[/]",
            str(st.items_today),
            f"{st.baseline_avg:.1f}",
            trend,
        )
    console.print(table)
    alerts = [s.source_class for s in results.values() if s.is_alert]
    if alerts:
        console.print(
            f"[bold red]⚠ {len(alerts)} source(s) below "
            f"{settings.source_health_drop_threshold:.0%} of baseline:[/] {', '.join(alerts)}"
        )
    else:
        console.print("[green]all sources within healthy ingestion volume[/]")


def _kv_table(title: str, rows: list[tuple[str, str]]) -> Table:
    """Small two-column key/value table used by the dashboard panels."""
    table = Table(show_header=False, box=None, title=title, title_style="bold cyan", pad_edge=False)
    table.add_column("k", style="dim")
    table.add_column("v", justify="right")
    for key, value in rows:
        table.add_row(key, value)
    return table


@app.command()
def dashboard() -> None:
    """Single-view summary of the whole newsroom (today, budget, eval, sources, queue)."""
    from rich.columns import Columns
    from rich.panel import Panel

    from .dashboard import gather_dashboard

    with console.status("Gathering newsroom state…"):
        data = asyncio.run(gather_dashboard())

    today, bud, ev, hc = data.today, data.budget, data.evals, data.health_counts

    today_panel = Panel(
        _kv_table(
            "Today",
            [
                ("published", str(today.published)),
                ("articles touched", str(today.touched)),
                ("fact-pass rate", _fmt_opt(today.fact_pass_rate, "{:.0%}")),
                ("quality avg", _fmt_opt(today.quality_avg, "{:.2f}")),
            ],
        ),
        border_style="green",
    )
    budget_panel = Panel(
        _kv_table(
            "Budget",
            [
                ("spent / ceiling", f"${bud['actual_usd']:.3f} / ${bud['ceiling_usd']:.2f}"),
                ("remaining", f"${bud['remaining']:.3f}"),
                ("escalations", f"{bud['escalations']}/{bud['escalation_cap']}"),
                ("kill-switch", "[bold red]ON[/]" if bud["kill_switch"] else "[green]off[/]"),
            ],
        ),
        border_style="red" if bud["kill_switch"] else "yellow",
    )
    eval_panel = Panel(
        _kv_table(
            "Eval",
            [
                ("mean gate score", _fmt_opt(ev["mean_gate"], "{:.3f}")),
                ("mean eval score", _fmt_opt(ev["mean_eval"], "{:.3f}")),
                ("gate↔eval divergence", _fmt_opt(ev["mean_abs_diff"], "{:.3f}")),
                ("paired articles", str(ev["paired"])),
            ],
        ),
        border_style="magenta",
    )
    queue_panel = Panel(
        _kv_table(
            "Queue",
            [
                ("awaiting review", str(data.queue_depth)),
            ],
        ),
        border_style="blue",
    )
    sources_panel = Panel(
        _kv_table(
            "Sources",
            [
                ("[green]green[/]", str(hc.get("green", 0))),
                ("[yellow]yellow[/]", str(hc.get("yellow", 0))),
                ("[bold red]red[/]", str(hc.get("red", 0))),
                ("[cyan]new[/] / [dim]idle[/]", f"{hc.get('new', 0)} / {hc.get('idle', 0)}"),
            ],
        ),
        border_style="red" if hc.get("red", 0) else "green",
    )

    console.rule(f"[bold]Newsroom Dashboard[/] — {data.day}")
    console.print(Columns([today_panel, budget_panel, eval_panel], equal=True, expand=True))
    console.print(Columns([sources_panel, queue_panel], equal=True, expand=True))
    red_sources = [c for c, s in data.health.items() if s.is_alert]
    if red_sources:
        console.print(f"[bold red]⚠ source alerts:[/] {', '.join(red_sources)}")


def _redact(url: str) -> str:
    """Hide the password component of a DB URL for display."""
    import re

    return re.sub(r"(://[^:]+:)([^@]+)(@)", r"\1***\3", url)


if __name__ == "__main__":
    app()
