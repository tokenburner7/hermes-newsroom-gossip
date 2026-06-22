"""Unit tests for the fact-gate span scorer (``newsroom.pipeline.factcheck``).

Pure logic, no DB / no network — the scorer is the gate's whole decision. These
lock in the fix for the gossip pipeline blocking every article: a span the model
copied verbatim from the body text must pass even when punctuation or a small edit
diverges from how the source stored it, while a span that simply is not in the body
(an ISO metadata timestamp the model lifted from a search result) must still fail.
"""

from __future__ import annotations

from newsroom.pipeline.factcheck import PASS_THRESHOLD, span_score


# --- exact / normalized grounding -------------------------------------------

def test_exact_substring_scores_one():
    source = 'Mayor Gina Ortiz Jones said she supports canceling the concert.'
    score, method = span_score("she supports canceling the concert", source)
    assert score == 1.0
    assert method == "exact"


def test_curly_vs_straight_quotes_still_exact():
    # Source stored smart quotes; the model emitted straight quotes (or vice versa).
    source = 'The rep said “they are just friends” in a statement.'
    score, method = span_score('the rep said "they are just friends"', source)
    assert score == 1.0
    assert method == "exact"


def test_em_dash_and_ellipsis_normalized_to_ascii():
    source = "It was a quiet hard-launch — not a one-off … fans noticed."
    score, _ = span_score("a quiet hard-launch - not a one-off ...", source)
    assert score == 1.0


# --- the regression: grounded-but-drifted spans must pass --------------------

def test_grounded_span_with_reformatted_quote_passes():
    # Claim #188 shape: the span IS in the body, but the source stored the inner quote
    # with smart quotes and a comma where the model put a period — "trafilatura
    # reformatted it". Punctuation folding + coverage must recover it above the bar;
    # the old single-longest-block scorer dropped this to ~0.35.
    source = (
        "At a press conference that city's mayor said she supports "
        "“canceling the @kanyewest concert,” said Mayor Gina Ortiz Jones."
    )
    span = (
        "that city's mayor said she supports "
        '"canceling the @kanyewest concert." Mayor Gina Ortiz Jones'
    )
    score, method = span_score(span, source)
    assert method == "fuzzy"
    assert score >= PASS_THRESHOLD


def test_two_separate_quotes_sum_via_coverage():
    # Single-longest-block scoring would credit only one half (~0.5); coverage sums
    # both grounded runs.
    source = "Alpha bravo charlie delta. Totally unrelated filler. Echo foxtrot golf hotel."
    span = "alpha bravo charlie delta echo foxtrot golf hotel"
    score, _ = span_score(span, source)
    assert score >= PASS_THRESHOLD


# --- the regression: metadata / hallucinations must still fail ---------------

def test_iso_timestamp_metadata_fails():
    # Claim #190 shape: the model quoted published_at metadata, not body text.
    source = "Kanye West concert news broke as fans reacted online to the announcement."
    score, _ = span_score("2026-06-21T23:43:51+00:00", source)
    assert score < PASS_THRESHOLD


def test_unrelated_paraphrase_fails():
    source = "Mayor Gina Ortiz Jones said she supports canceling the concert."
    score, _ = span_score("the album debuted at number one on the chart", source)
    assert score < PASS_THRESHOLD


def test_empty_span_or_source_scores_zero():
    assert span_score("", "some source text") == (0.0, "empty")
    assert span_score("a span", "") == (0.0, "empty")
