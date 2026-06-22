"""Finance vertical — STYLE_GUIDE + article type definitions for the
Macro & Markets content product.

Swap this in when running the pipeline with --vertical finance.
"""

from __future__ import annotations

# --- Editorial voice for finance audience -----------------------------------

FINANCE_STYLE_GUIDE = """\
VOICE: Analytical, precise, and data-forward. Write for an audience of
retail and professional investors who read Bloomberg, The Daily Upside,
and Morning Brew. They understand yield curves, basis points, Fed
policy tools, and economic indicators. Do NOT explain what CPI means or
why the Fed matters.

STRUCTURE (every article):
1. LEDE — The number. What changed and by how much. Put the data point
   up front, not buried in paragraph three.
2. CONTEXT — How this compares to prior prints, consensus expectations,
   and the trailing trend. Is this a one-off or a regime shift?
3. REACTION — What markets did (equities, bonds, FX, crypto). Name the
   tickers that moved. Show the magnitude.
4. IMPLICATIONS — What this means for the next Fed decision, the next
   earnings season, the next payroll print. Connect the dots forward.
5. BOTTOM LINE — One sentence. What the reader should take away.

TONAL RULES:
- Lead with the data point: "CPI rose 3.4% YoY in May, 0.1pp above consensus."
  NOT "The Bureau of Labor Statistics released its monthly CPI report today..."
- Precision over narrative: "2Y yield dropped 12bp to 4.72%" not "bonds rallied"
- Name the instruments: "SPX futures +0.4%, DXY -0.3%, BTC +1.2%"
- No market clichés: "risk-on/risk-off", "bulls and bears", "buy the dip"
- No predictions disguised as analysis: "If trend holds, the June dot plot
  implies..." NOT "The Fed will definitely cut in September"
- Attribution required: every claim that isn't common knowledge must trace to
  a source row in the database
- Max 2 adjectives per paragraph; prefer numbers to adjectives
- Headlines: specific, numeric when possible, ≤80 chars
  Good: "May CPI 3.4% — Core Cools to 3.6%, 2Y Yield Drops 12bp"
  Bad: "Inflation Report Surprises Markets"

ARTICLE TYPES:
- macro_print_reaction: same-hour analysis of a data release (CPI, NFP, GDP, FOMC)
- market_context: broader market snapshot, multi-asset
- earnings_digest: key earnings reports deconstructed
- fed_watch: FOMC minutes, speeches, dot-plot analysis
- weekly_macro: Sunday/monday look-ahead with calendar + positioning
"""

# --- Article type → source mapping for finance vertical --------------------

FINANCE_ARTICLE_TYPE_SOURCE_MAP: dict[str, str] = {
    "macro_print_reaction": "bls",           # CPI, NFP, PPI from BLS
    "market_context": "coingecko",           # existing — multi-asset macro snapshot
    "earnings_digest": "sec",                # existing — 8-K/10-Q analysis
    "fed_watch": "fred",                     # existing — rate + macro context
    "weekly_macro": "bls",                   # cross-source synthesis
}

# --- Vertical metadata -----------------------------------------------------

FINANCE_VERTICAL = {
    "name": "Macro & Markets",
    "slug": "finance",
    "description": (
        "Same-hour reactions to economic data prints, earnings, "
        "and Fed moves with historical context."
    ),
    "brand_url": "",
    "x_handle": "",
    "style_guide": FINANCE_STYLE_GUIDE,
    "article_type_source_map": FINANCE_ARTICLE_TYPE_SOURCE_MAP,
    "required_sources": ["bls", "treasury", "fred", "sec", "coingecko"],
    "optional_sources": ["reddit", "hackernews"],
}

# --- Gossip vertical -------------------------------------------------------

GOSSIP_STYLE_GUIDE = """\
VOICE: Write like a text from a chronically-online, well-connected friend.
Snarky, fast, name-dropping, insider-y. The reader feels like they're
getting a DM from someone who hears everything first. Group chat, not
analyst report. @DeuxMoi energy but with provenance.

STRUCTURE (every article):
1. LEDE — The news. WHO did WHAT. Front-load the name and the action.
   "Selena Gomez and Benny Blanco are engaged, per sources close to the
   couple." NOT "In a surprising turn of events..."
2. THE DETAILS — What we know. How we know it. Sources named. Timeline.
3. CONTEXT — Why this matters. How this fits the person's arc. Previous
   related stories. "This comes three months after..."
4. THE ANGLE — Read between the lines. PR move? Damage control? Genuine?
5. THE TAKE — One sharp sentence. What the reader should walk away thinking.

TONAL RULES:
- Names first, actions second. "Timothée Chalamet has signed..." NOT
  "A new project has attracted..."
- Drop the journalist distance. "Per sources close to the production..."
  "Insiders tell us..." "A rep confirmed to..."
- Speed over formality. Short paragraphs. 1-3 sentences.
- Specificity: "$4.2M per episode" not "a lucrative deal"
- No clickbait: never promise more than the article delivers
- No moralizing: report, don't judge
- Headlines: punchy, name-forward, ≤80 chars
  Good: "Zendaya to Star in Guadagnino's New Film"
  Bad:  "Major Casting News: A-List Star Joins Upcoming Project"
- Banned openers: "In a shocking turn of events...", "Fans are losing
  it over...", "The internet is buzzing...", "In these uncertain times..."
- Citations: every non-obvious fact gets a [^claim_N] marker
- Quotes: use exact words when available
- No hedging chains: "may possibly be considering" → kill it
- End with a kick: the last line should resonate

ATTRIBUTION TIERS (how to source claims):
- CONFIRMED: Rep statement, court filing, official announcement,
  named on-record source → lead with it
- REPORTED: Trade publication (Variety, Deadline, THR), established
  gossip outlet (TMZ, Page Six) with named sourcing → cite the outlet
- DEVELOPING: Single unnamed source, tip submission, blind item →
  flag as unconfirmed, name the source type
- RUMOR: Social media chatter, fan speculation → only include if the
  chatter itself is the story (e.g. "Fans are speculating that...")
"""

GOSSIP_ARTICLE_TYPE_SOURCE_MAP: dict[str, str] = {
    "breaking_sighting": "tmz",         # TMZ fastest for sightings
    "feud_coverage": "reddit",          # r/popculturechat + r/Fauxmoi
    "casting_news": "deadline",         # Deadline = gold standard for casting
    "box_office_report": "deadline",    # Numbers + analysis
    "blind_item": "reddit",             # r/Deuxmoi + r/Fauxmoi
    "relationship_update": "tmz",       # TMZ breaks relationships
    "album_drop": "variety",            # Variety music coverage
    "fashion_moment": "eonline",        # E! = fashion + red carpet
    "career_milestone": "variety",      # Variety for industry milestones
    "viral_moment": "reddit",           # Social media aggregation
}

GOSSIP_VERTICAL = {
    "name": "Gossip",
    "slug": "gossip",
    "description": (
        "Celebrity news, entertainment industry, and pop culture — "
        "fast, sourced, and snarky."
    ),
    "brand_url": "",
    "x_handle": "",
    "style_guide": GOSSIP_STYLE_GUIDE,
    "article_type_source_map": GOSSIP_ARTICLE_TYPE_SOURCE_MAP,
    "required_sources": [
        "tmz", "pagesix", "deadline", "variety", "justjared",
        "eonline", "buzzfeed", "usweekly", "thewrap", "reddit",
    ],
    "optional_sources": ["x_gossip"],
}
