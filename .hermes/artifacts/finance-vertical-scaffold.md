# Finance Vertical (Macro & Markets) — Scaffold

## What was built

### New source modules
- `src/newsroom/sources/bls.py` — BLS API v2 (CPI, unemployment, payrolls, PPI)
- `src/newsroom/sources/treasury.py` — US Treasury FiscalData API (yields, debt)

### Vertical definition
- `src/newsroom/verticals/__init__.py` — FINANCE_VERTICAL with:
  - `FINANCE_STYLE_GUIDE` — finance-native editorial voice
  - `FINANCE_ARTICLE_TYPE_SOURCE_MAP` — 5 article types mapped to sources
  - Vertical metadata (name, slug, required sources)

### Configuration
- `config.py` — added `bls_api_key`, `enable_bls`, `enable_treasury`

### Pipeline integration
- `sources/__init__.py` — registered `bls` and `treasury`
- `pipeline/draft.py` — `draft()` and `_build_system_prompt()` now accept `style_guide`
- `cli.py` — `run-once` accepts `--vertical finance` which:
  - Swaps STYLE_GUIDE to finance voice
  - Swaps article-type→source mapping
  - Resolves source IDs from finance sources (BLS, FRED, SEC, CoinGecko, Treasury)

## Article types (finance vertical)

| Type | Primary Source | Description |
|------|---------------|-------------|
| macro_print_reaction | bls | Same-hour CPI/NFP/PPI analysis |
| market_context | coingecko | Multi-asset macro snapshot |
| earnings_digest | sec | 8-K/10-Q earnings analysis |
| fed_watch | fred | FOMC minutes, rate decisions |
| weekly_macro | bls | Sunday look-ahead with calendar |

## To activate

```bash
# 1. Set API keys in .env
BLS_API_KEY=your_bls_registration_key
ENABLE_BLS=true
ENABLE_TREASURY=true

# 2. Ingest finance sources
uv run newsroom ingest --source bls
uv run newsroom ingest --source treasury

# 3. Run finance pipeline
uv run newsroom run-once --vertical finance --type macro_print_reaction --publish

# 4. Cycle through all finance types (cron)
uv run newsroom run-once --vertical finance --type market_context --publish
uv run newsroom run-once --vertical finance --type earnings_digest --publish
uv run newsroom run-once --vertical finance --type fed_watch --publish
uv run newsroom run-once --vertical finance --type weekly_macro --publish
```

## What's NOT yet done (next steps)

1. **BLS API key** — get a free key at https://data.bls.gov/registrationEngine/
2. **Finance SSG site** — configure a separate Astro/CF Pages deploy (e.g. `macro-markets.mport.net`)
3. **Distribution** — finance-specific X handle + Telegram channel
4. **Phase-0 filter** — BLS data needs no filtering (all observations are relevant)
5. **Cycle script** — extend `scripts/cycle.py` to support `--vertical finance`
6. **Cron job** — create Hermes cron for finance article cycling
7. **Dev Tools vertical** — next scaffold candidate (lowest risk, highest reuse after finance)

## Architecture notes

The `--vertical` flag enables multi-vertical operation from a single pipeline instance.
Each vertical gets:
- Its own STYLE_GUIDE (editorial voice)
- Its own article-type→source mapping
- Shared pipeline stages (research, draft, factcheck, humanize, publish, distribute)

To add a new vertical:
1. Create source modules in `sources/`
2. Define vertical config in `verticals/` (style guide + type map + metadata)
3. Add `blah_api_key` + `enable_blah` to `config.py`
4. Register source in `sources/__init__.py`
5. Register vertical in `cli.py`'s `_VERTICALS` dict (in `_run_once_impl`)
