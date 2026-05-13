"""Unit tests for scripts/score_metrics.py.

Highest-leverage tests per /ark-code-review --thorough findings:
1. `score_risk_rule_compliance` bare-words false-positive (Top-5 finding #1)
3. `extract_signal` non-canonical 5-tier variants (Top-5 finding #3)
"""
import json

import pytest

from score_metrics import (
    DecisionRow,
    ENTRY_PATTERN,
    STOP_PATTERN,
    TARGET_PATTERN,
    _extract_decision_text,
    _has_anchored_match,
    extract_signal,
    score_risk_rule_compliance,
)


def _decision(rationale: str = "", *, signal: str = "Buy", order_intent: dict | None = None,
              kind: str = "baseline") -> DecisionRow:
    if order_intent is None:
        order_intent = {"signal": signal}
    return DecisionRow(
        decision_id="t",
        ticker="NVDA",
        trade_date="2026-05-13",
        decision_kind=kind,
        order_intent_json=json.dumps(order_intent),
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Test #1 — score_risk_rule_compliance bare-words false-positive
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rationale", [
    "we may initiate a position with a hard stop and clear profit-target",
    "entry strategy uses a hard stop near the structure",
    "trim profit-target while observing initiate-style technicals",
])
def test_risk_rule_compliance_rejects_bare_keywords(rationale: str):
    """The Top-5 finding: bare keywords without `$N` proximity score 0/3, not 3/3.

    A rationale that says "we may initiate a position with a hard stop and clear
    profit-target" contains the three structural keywords but NO actual price
    levels. The pre-fix regex matched all three, awarding score=1.0; the post-fix
    `_has_anchored_match` requires a `$N` token within PRICE_PROXIMITY_CHARS.
    """
    result = score_risk_rule_compliance(_decision(rationale, signal="Sell"))
    assert result.score == 0.0, (
        f"bare-words rationale {rationale!r} should score 0/3, got {result.score}; "
        f"label={result.score_label}"
    )


def test_risk_rule_compliance_accepts_dollar_anchored_terms():
    """Properly-specified entry/target/stop with $N levels scores 3/3."""
    rationale = "Entry at $196, trim near $210, hard stop at $181"
    result = score_risk_rule_compliance(_decision(rationale, signal="Sell"))
    assert result.score == 1.0, f"$N-anchored rationale should score 3/3, got {result.score}"


def test_risk_rule_compliance_hold_short_circuits_to_one():
    """Hold decisions don't have entry/target/stop to specify — scoring them 0/3
    would punish a valid no-trade choice. Short-circuit to 1.0 with explicit label."""
    result = score_risk_rule_compliance(_decision("steady state cash position", signal="Hold"))
    assert result.score == 1.0
    assert "hold" in result.score_label.lower()


@pytest.mark.parametrize("text,pattern,expected", [
    # Bare words — should NOT match (post-fix behavior)
    ("we may initiate a position", ENTRY_PATTERN, False),
    ("set a profit-target near the top", TARGET_PATTERN, False),
    ("use a hard stop if it breaks down", STOP_PATTERN, False),
    # With $N nearby — should match
    ("entry at $196", ENTRY_PATTERN, True),
    ("trim near $210", TARGET_PATTERN, True),
    ("hard stop at $181", STOP_PATTERN, True),
    ("buy near $200", ENTRY_PATTERN, True),
    ("take-profit $250", TARGET_PATTERN, True),
])
def test_has_anchored_match_requires_dollar_proximity(text, pattern, expected):
    assert _has_anchored_match(pattern, text) is expected, text


# ---------------------------------------------------------------------------
# Test #3 — extract_signal non-canonical 5-tier variants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    # Canonical
    ("Buy", "buy"),
    ("BUY", "buy"),
    ("buy", "buy"),
    ("Hold", "hold"),
    ("Overweight", "overweight"),
    ("Underweight", "underweight"),
    ("Sell", "sell"),
    # Modifier-decorated variants Kimi K2.6 has emitted in the wild
    ("Strong Buy", "buy"),
    ("BUY!", "buy"),
    ("Buy (high conviction)", "buy"),
    ("Overweight (with caveats)", "overweight"),
    # Unrecognized text
    ("Wait", None),
    ("Neutral", None),
    ("", None),
    ("definitely garbage", None),
])
def test_extract_signal_tolerant_normalizer(raw, expected):
    """Kimi K2.6 has emitted variants like 'Strong Buy', 'Buy (high conviction)',
    'BUY!'. The pre-fix exact-match-only normalizer returned None for all of
    these — silently dropping decisions out of the eval ledger's denominator.
    Post-fix uses word-boundary matching against the 5 canonical tokens.
    """
    row = DecisionRow(
        decision_id="t", ticker="NVDA", trade_date="2026-05-13",
        decision_kind="baseline",
        order_intent_json=json.dumps({"signal": raw}),
        rationale="",
    )
    assert extract_signal(row) == expected, raw


def test_extract_signal_handles_malformed_order_intent_json():
    """Malformed JSON in `order_intent_json` (from a crashed prediction cycle)
    must not crash the scoring loop — return None so the row is left deferred."""
    row = DecisionRow(
        decision_id="t", ticker="NVDA", trade_date="2026-05-13",
        decision_kind="baseline",
        order_intent_json="{not valid json",
        rationale="",
    )
    assert extract_signal(row) is None


def test_extract_signal_reads_shadow_signal_for_shadow_rows():
    """Shadow rows nest the signal under shadow_decision.shadow_signal — the
    scorer must follow that path, not just `intent["signal"]`."""
    row = DecisionRow(
        decision_id="s", ticker="NVDA", trade_date="2026-05-13",
        decision_kind="shadow",
        order_intent_json=json.dumps({
            "shadow_decision": {"shadow_signal": "Strong Sell"},
        }),
        rationale="",
    )
    assert extract_signal(row) == "sell"


# ---------------------------------------------------------------------------
# Bonus: trust-boundary stripping (caught by /cso Finding 3)
# ---------------------------------------------------------------------------


def test_extract_decision_text_strips_trust_boundary_block():
    """Moomoo-vendored content is wrapped between sentinel markers. Inside the
    block is third-party article text the analyst consumed but didn't author —
    it must not contribute to lexicon-based sentiment scoring."""
    order_json = json.dumps({
        "signal": "Buy",
        "final_trade_decision": (
            "--- THIRD-PARTY UNTRUSTED CONTENT BEGIN ---\n"
            "bearish miss contraction downside\n"
            "--- THIRD-PARTY UNTRUSTED CONTENT END ---\n"
            "rally outperform"
        ),
    })
    row = DecisionRow(
        decision_id="t", ticker="NVDA", trade_date="2026-05-13",
        decision_kind="baseline", order_intent_json=order_json, rationale="",
    )
    text = _extract_decision_text(row)
    assert "bearish" not in text
    assert "miss" not in text
    assert "rally" in text  # post-block content survives
