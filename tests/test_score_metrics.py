"""Unit tests for scripts/score_metrics.py.

Highest-leverage tests:
1. `score_risk_rule_compliance` structured-v0 — 3 deterministic rules
3. `extract_signal` non-canonical 5-tier variants (Top-5 finding #3)
4. `score_volatility_adjusted_move` — T+5 z-score-normalized directional match
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from score_metrics import (
    ATR_BAND_HIGH,
    ATR_BAND_LOW,
    ATR_KEYWORD_RE,
    DecisionRow,
    ENTRY_KEYWORD_RE,
    POSITION_QUANTITY_CAP,
    STOP_KEYWORD_RE,
    _extract_decision_text,
    _extract_price_near_keyword,
    _extract_quantity,
    extract_signal,
    score_risk_rule_compliance,
    score_volatility_adjusted_move,
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
# Test #1 — score_risk_rule_compliance structured-v0 (3 rules)
# ---------------------------------------------------------------------------


def test_risk_rule_hold_short_circuits_to_one():
    """Hold signal → 1.0 with notes='no-trade-signal-hold'. No position to risk-manage."""
    result = score_risk_rule_compliance(_decision("steady state cash position", signal="Hold"))
    assert result.score == 1.0
    assert result.notes == "no-trade-signal-hold"
    assert "hold" in result.score_label.lower()


def test_risk_rule_signal_unrecognized_returns_none():
    """Garbage signal → defer (score=None) so the row is left for a re-pass after fix."""
    result = score_risk_rule_compliance(_decision("foo bar", signal="Maybe"))
    assert result.score is None
    assert "signal-unrecognized" in result.notes


def test_risk_rule_3_of_3_with_explicit_atr_inband():
    """All 3 rules pass: $181 stop + $196 entry + $5 ATR + 3 shares → score=1.0.
    Distance=$15; band=[1.5×5=$7.50, 3×5=$15.00] → $15.00 is within band (boundary)."""
    rationale = "Entry pinch at $196, hard stop at $181, ATR ~$5, plan 3 shares"
    result = score_risk_rule_compliance(_decision(rationale, signal="Buy"))
    assert result.score == 1.0, f"got {result.score}; label={result.score_label}; notes={result.notes}"
    assert "3-of-3" in result.score_label


def test_risk_rule_w3_18_baseline_2_of_3_no_atr():
    """W3.18 NVDA reality per vault: '$196 entry pinch, $202–$204 trim zone, $181 hard stop'.
    No ATR mention → Rule 2 (stop_atr_bound) cannot be evaluated → fails.
    Stop extracted ($181) + qty fallback to broker default (1 ≤ 5) → 2/3 → 0.67."""
    rationale = (
        "PM landed on Underweight with specific levels: $196 entry pinch, "
        "$202–$204 trim zone, $181 hard stop, 3-6 month horizon."
    )
    result = score_risk_rule_compliance(_decision(rationale, signal="Underweight"))
    assert result.score == 0.67, f"got {result.score}; notes={result.notes}"
    assert "stop_present=True" in result.score_label
    assert "stop_atr_bound=False" in result.score_label
    assert "qty_under_cap=True" in result.score_label
    assert "inferred-from-broker-default" in result.notes


def test_risk_rule_backward_keyword_number_before_keyword():
    """W3.18-style phrasing '$181 hard stop' — number BEFORE keyword.
    Bidirectional extractor's backward pass must pick this up."""
    rationale = "$196 entry pinch, $181 hard stop, ATR ~$5"
    result = score_risk_rule_compliance(_decision(rationale, signal="Buy"))
    assert result.score == 1.0, (
        f"backward extraction should land 3/3; got {result.score}; "
        f"notes={result.notes}"
    )


def test_risk_rule_forward_keyword_number_after_keyword():
    """Forward-style phrasing 'hard stop at $181'."""
    rationale = "entry at $196, hard stop at $181, ATR ~$5, 2 shares"
    result = score_risk_rule_compliance(_decision(rationale, signal="Buy"))
    assert result.score == 1.0


def test_risk_rule_stop_too_tight_fails_atr_bound():
    """Entry $196 / stop $195 (distance=$1) vs ATR $5 → band [$7.50, $15] → $1 < $7.50.
    Stop_atr_bound fails; stop_present + qty_under_cap pass → 2/3."""
    rationale = "entry $196, hard stop $195, ATR ~$5, 1 share"
    result = score_risk_rule_compliance(_decision(rationale, signal="Buy"))
    assert result.score == 0.67
    assert "stop_atr_bound=False" in result.score_label


def test_risk_rule_stop_too_loose_fails_atr_bound():
    """Entry $196 / stop $150 (distance=$46) vs ATR $5 → band [$7.50, $15] → $46 > $15.
    Stop_atr_bound fails; stop_present + qty_under_cap pass → 2/3."""
    rationale = "entry $196, hard stop $150, ATR ~$5, 1 share"
    result = score_risk_rule_compliance(_decision(rationale, signal="Buy"))
    assert result.score == 0.67
    assert "stop_atr_bound=False" in result.score_label


def test_risk_rule_qty_over_cap_fails_rule_3():
    """20 shares > default cap 5 → qty_under_cap fails. Entry/stop/atr in-band → 2/3.
    Distance |196-188|=8 ∈ [1.5×5=7.5, 3×5=15]."""
    rationale = "entry $196, hard stop $188, ATR ~$5, 20 shares"
    result = score_risk_rule_compliance(_decision(rationale, signal="Buy"))
    assert result.score == 0.67, f"got {result.score}; notes={result.notes}"
    assert "qty_under_cap=False" in result.score_label


def test_risk_rule_quantity_cap_param_override():
    """Allow callers to bump the qty cap (will matter once paper-trading lifts qty=1)."""
    rationale = "entry $196, hard stop $188, ATR ~$5, 20 shares"
    result = score_risk_rule_compliance(
        _decision(rationale, signal="Buy"),
        quantity_cap=25,
    )
    assert result.score == 1.0, f"got {result.score}; notes={result.notes}"


def test_risk_rule_score_quantization():
    """Three rules → discrete output set {0.0, 0.33, 0.67, 1.0}.

    The qty-inferred fallback (1 share when text omits a size token) means the
    bare 'no levels' rationale scores 0.33 via the inferred qty=1 ≤ cap pass.
    A genuine 0.0 requires an explicit qty>cap to defeat the fallback.
    """
    # 0/3: signal=Buy, qty explicitly over cap, no other extractable levels
    r0 = score_risk_rule_compliance(_decision("buying 100 shares on a gut feel", signal="Buy"))
    assert r0.score == 0.0, f"got {r0.score}; notes={r0.notes}"
    # 1/3: stop present, no entry, no atr (rule 2 fails), qty over cap (rule 3 fails)
    r1 = score_risk_rule_compliance(_decision("hard stop at $181, 99 shares", signal="Buy"))
    assert r1.score == 0.33, f"got {r1.score}; notes={r1.notes}"
    # 2/3 and 3/3 covered by other tests above


# ---------------------------------------------------------------------------
# Extractor unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    # Forward (keyword before number)
    ("hard stop at $181", 181.0),
    ("stop $200", 200.0),
    ("stop-loss $150.25", 150.25),
    # Backward (number before keyword)
    ("$181 hard stop", 181.0),
    ("level $175 was the stop-loss", 175.0),
    # Closer-side wins
    ("$196 entry pinch, $181 hard stop", 181.0),  # for stop keyword
    # Bare keyword, no $N anywhere → None
    ("we use a stop strategy here", None),
])
def test_extract_price_near_keyword_stop(text, expected):
    assert _extract_price_near_keyword(STOP_KEYWORD_RE, text) == expected


@pytest.mark.parametrize("text,expected", [
    ("entry pinch at $196", 196.0),
    ("entry $196", 196.0),
    ("$196 entry pinch, $181 hard stop", 196.0),  # closer-side: backward $196 wins
    ("buy near $200", 200.0),
])
def test_extract_price_near_keyword_entry(text, expected):
    assert _extract_price_near_keyword(ENTRY_KEYWORD_RE, text) == expected


@pytest.mark.parametrize("text,expected", [
    ("ATR ~$5.30", 5.30),
    ("ATR is 5.30", 5.30),       # no $ — require_dollar=False
    ("ATR of $7", 7.0),
    ("14-day ATR 8.5", 8.5),
    ("average true range $5", 5.0),
    ("no atr value here at all", None),
])
def test_extract_price_near_keyword_atr_allows_bare_numbers(text, expected):
    assert _extract_price_near_keyword(ATR_KEYWORD_RE, text, require_dollar=False) == expected


@pytest.mark.parametrize("text,expected", [
    ("1 share", 1),
    ("3 shares", 3),
    ("plan 2 shares of NVDA", 2),
    ("quantity 5", 5),
    ("qty: 10", 10),
    ("size of 7", 7),
    ("position size 4", 4),
    ("no quantity mentioned", None),
])
def test_extract_quantity(text, expected):
    assert _extract_quantity(text) == expected


def test_atr_band_constants_match_hermes_review():
    """2026-05-07 Hermes review (Risk Neutral edit 2): 'hard stop lies within
    1.5×ATR of current price'. Band low must be 1.5×."""
    assert ATR_BAND_LOW == 1.5
    assert ATR_BAND_HIGH == 3.0
    assert POSITION_QUANTITY_CAP == 5


# ---------------------------------------------------------------------------
# Shadow row handling
# ---------------------------------------------------------------------------


def test_risk_rule_shadow_row_reads_shadow_rationale():
    """Shadow rows store rationale in `decision.rationale` (= shadow_rationale, per
    run_hermes_proposal.py insert_shadow_decision). The scorer must extract levels
    from the shadow's text, not silently miss it."""
    shadow_payload = {
        "shadow_decision": {
            "shadow_signal": "Buy",
            "shadow_rationale": "shadow proposes entry $200, hard stop $190, ATR ~$5, 2 shares",
        },
    }
    row = DecisionRow(
        decision_id="s", ticker="NVDA", trade_date="2026-05-13",
        decision_kind="shadow",
        order_intent_json=json.dumps(shadow_payload),
        rationale="shadow proposes entry $200, hard stop $190, ATR ~$5, 2 shares",
    )
    result = score_risk_rule_compliance(row)
    assert result.score == 1.0, f"got {result.score}; notes={result.notes}"


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


# ---------------------------------------------------------------------------
# Test #4 — score_volatility_adjusted_move (T+5 z-score-normalized directional)
# ---------------------------------------------------------------------------


def _vol_decision(signal: str = "Buy", trade_date: str = "2026-05-06") -> DecisionRow:
    return DecisionRow(
        decision_id="v",
        ticker="NVDA",
        trade_date=trade_date,
        decision_kind="baseline",
        order_intent_json=json.dumps({"signal": signal}),
        rationale="",
    )


def _vol_kline_rows(closes: list[float], start_date: str = "2026-05-06") -> list[dict]:
    """Build T+0..T+(len-1) kline rows. Calendar-day-stepped dates are fine — the
    scorer identifies T+0 as the first row whose date >= trade_date, then takes
    the next 5 rows as T+1..T+5, independent of whether those calendar dates
    fall on a weekend in reality."""
    base = datetime.strptime(start_date, "%Y-%m-%d").date()
    return [
        {"date": (base + timedelta(days=i)).isoformat(), "close": c}
        for i, c in enumerate(closes)
    ]


def test_volatility_adjusted_defers_when_window_not_closed(monkeypatch):
    """T+5 in the future → defer with notes containing 'window-not-closed'."""
    klines = _vol_kline_rows([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
    monkeypatch.setattr("score_metrics.fetch_daily_klines", lambda *a, **k: klines)
    # T+5 == 2026-05-11; today = 2026-05-10 → window not yet closed
    result = score_volatility_adjusted_move(
        _vol_decision(signal="Buy"),
        kline_script="x", uv_bin="x",
        vol_adj_threshold=1.0,
        today=datetime(2026, 5, 10, 22, 0, tzinfo=timezone.utc),
    )
    assert result.score is None
    assert "window-not-closed" in result.notes


def test_volatility_adjusted_defers_on_zero_close_in_denominator(monkeypatch):
    """A zero in closes[1..4] (rare moomoo data glitch) would crash with
    ZeroDivisionError inside the daily_returns comprehension. Scorer must defer
    on any non-positive close in the denominator positions T+0..T+4."""
    klines = _vol_kline_rows([100.0, 101.0, 0.0, 103.0, 104.0, 105.0])
    monkeypatch.setattr("score_metrics.fetch_daily_klines", lambda *a, **k: klines)
    result = score_volatility_adjusted_move(
        _vol_decision(signal="Buy"),
        kline_script="x", uv_bin="x",
        vol_adj_threshold=1.0,
        today=datetime(2026, 5, 13, 22, 0, tzinfo=timezone.utc),
    )
    assert result.score is None
    assert "non-positive close in denominator at T+2" in result.notes


def test_volatility_adjusted_defers_on_degenerate_vol(monkeypatch):
    """All-equal closes → realized_vol=0 → defer with notes='degenerate-vol'.
    No directional signal can be extracted from a no-vol stock."""
    klines = _vol_kline_rows([100.0, 100.0, 100.0, 100.0, 100.0, 100.0])
    monkeypatch.setattr("score_metrics.fetch_daily_klines", lambda *a, **k: klines)
    result = score_volatility_adjusted_move(
        _vol_decision(signal="Buy"),
        kline_script="x", uv_bin="x",
        vol_adj_threshold=1.0,
        # After T+5 close so the window-not-closed guard doesn't shadow this
        today=datetime(2026, 5, 13, 22, 0, tzinfo=timezone.utc),
    )
    assert result.score is None
    assert "degenerate-vol" in result.notes


def test_volatility_adjusted_score_1_when_predicted_matches_realized(monkeypatch):
    """Buy predicts +1; sustained ~1% daily uptrend → vol_adj_move >> 1 →
    realized_dir=+1 → score 1.0."""
    klines = _vol_kline_rows([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
    monkeypatch.setattr("score_metrics.fetch_daily_klines", lambda *a, **k: klines)
    result = score_volatility_adjusted_move(
        _vol_decision(signal="Buy"),
        kline_script="x", uv_bin="x",
        vol_adj_threshold=1.0,
        today=datetime(2026, 5, 13, 22, 0, tzinfo=timezone.utc),
    )
    assert result.score == 1.0, f"got {result.score}; notes={result.notes}"
    assert result.score_label.startswith("up_predicted_up_realized_")
    assert "zmove=+" in result.score_label
    assert result.outcome_window_end_timestamp == "2026-05-11T21:00:00+00:00"


def test_volatility_adjusted_score_0_when_predicted_mismatches_realized(monkeypatch):
    """Underweight predicts -1; sustained ~1% daily uptrend → realized_dir=+1 → 0.0.
    Same NVDA-baseline-on-rally shape as next_day_direction's 0.0-on-all-3-rows
    pattern in the 2026-05-12 first-smoke."""
    klines = _vol_kline_rows([100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
    monkeypatch.setattr("score_metrics.fetch_daily_klines", lambda *a, **k: klines)
    result = score_volatility_adjusted_move(
        _vol_decision(signal="Underweight"),
        kline_script="x", uv_bin="x",
        vol_adj_threshold=1.0,
        today=datetime(2026, 5, 13, 22, 0, tzinfo=timezone.utc),
    )
    assert result.score == 0.0, f"got {result.score}; notes={result.notes}"
    assert result.score_label.startswith("down_predicted_up_realized_")


@pytest.mark.parametrize("closes,expected_score,realized_label", [
    # Hold-in-band: tiny realized move, jittery returns → |zmove| < 1.0 → realized=flat → match
    ([100.0, 100.5, 99.5, 100.3, 99.8, 100.1], 1.0, "flat"),
    # Hold-out-of-band: directional trend → |zmove| >> 1.0 → realized=up → mismatch
    ([100.0, 101.0, 102.0, 103.0, 104.0, 105.0], 0.0, "up"),
])
def test_volatility_adjusted_hold_in_and_out_of_band(monkeypatch, closes, expected_score,
                                                    realized_label):
    """Hold predicts flat (predicted_dir=0). Inside band → 1.0; outside → 0.0.
    Hold IS evaluated against realized vol-adjusted move (no short-circuit;
    mirrors next_day_direction's Hold semantics)."""
    klines = _vol_kline_rows(closes)
    monkeypatch.setattr("score_metrics.fetch_daily_klines", lambda *a, **k: klines)
    result = score_volatility_adjusted_move(
        _vol_decision(signal="Hold"),
        kline_script="x", uv_bin="x",
        vol_adj_threshold=1.0,
        today=datetime(2026, 5, 13, 22, 0, tzinfo=timezone.utc),
    )
    assert result.score == expected_score, f"got {result.score}; notes={result.notes}"
    assert f"flat_predicted_{realized_label}_realized_" in result.score_label
