#!/usr/bin/env python3
"""Score decision-quality metrics for the Ark Trade Agent v0 eval ledger.

Per spec § Build Order step 22+ (outcome-window scorer; surfaced by W4.22
Day-1 Hermes review at `vault/Session-Logs/2026-05-07-hermes-review.md`).

v0 scope (wired in code): `next_day_direction` (T+1 directional match),
`risk_rule_compliance` (T+0 deterministic 3-rule fractions per spec § Hermes
Evaluation Metrics & Shadow Mode), `rationale_trade_match` (T+0 lexicon
sentiment alignment), and `volatility_adjusted_move` (T+5 z-score-normalized
directional match). The remaining three metrics (`thesis_accuracy`,
`max_adverse_excursion`, `catalyst_correctness`) have their dispatch slots
wired but raise NotImplementedError when called.
The vault page (`vault/Operations/outcome-scorer-config.md`) tracks which
of the wired metrics is formally blessed/live in shadow mode.

Reads decisions whose `metric_scores.score` is NULL and whose outcome
window has closed (T+1 RTH close for `next_day_direction`), shells out to
the moomoo skill `get_kline.py` for daily candles, computes a binary match
score, UPDATEs the pre-existing deferred row (`metric_scores` rows are
INSERTed deferred by `run_prediction_cycle.py` and `run_hermes_proposal.py`
at decision time).

CLI:
    python3 scripts/score_metrics.py
        [--metric METRIC_NAME]      (default: next_day_direction)
        [--decision-id ID]          (filter to a single decision)
        [--ledger-path PATH]        (override $ARK_EVAL_LEDGER_PATH)
        [--kline-script PATH]       (override moomoo get_kline.py location)
        [--flat-threshold FLOAT]    (next_day_direction flat band; default 0.005)
        [--vol-adj-threshold FLOAT] (volatility_adjusted_move σ band; default 1.0)
        [--dry-run]                 (compute + print; no UPDATEs)

Runs on LOQ where OpenD + the moomoo SDK live. Mac dev edits + scp to LOQ.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from ledger_constants import METRICS, SIGNAL_DIRECTION

DEFAULT_LEDGER_PATH = "/opt/ark-data/eval-ledger.sqlite"
DEFAULT_KLINE_SCRIPT = (
    "/opt/ark-trade-agent/.claude/skills/moomooapi/scripts/quote/get_kline.py"
)
# Absolute path required — `ssh user@host 'cmd'` skips ~/.bashrc, so ~/.local/bin
# isn't on PATH. Same discipline as HEARTBEAT.md per CLAUDE.md ## Heartbeat.
DEFAULT_UV_BIN = "/home/ark-dev/.local/bin/uv"
DEFAULT_FLAT_THRESHOLD = 0.005  # 0.5% — moves within this band count as flat
KLINE_TIMEOUT_S = 60
KLINE_WINDOW_DAYS = 10  # calendar days fetched starting at trade_date — buffer for weekends/holidays
KLINE_WINDOW_DAYS_T5 = 12  # 5 trading days + weekend/holiday buffer + 1 to find T+0
DEFAULT_VOL_ADJ_THRESHOLD = 1.0  # σ — vol-normalized "noise floor" for the realized-direction bucket

# ALL_METRICS retained as an alias for argparse choices/CLI surface compat.
# METRICS + SIGNAL_DIRECTION imported from ledger_constants — single source of truth.
ALL_METRICS = METRICS


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class DecisionRow:
    decision_id: str
    ticker: str
    trade_date: str  # YYYY-MM-DD
    decision_kind: str  # 'baseline' | 'shadow'
    order_intent_json: str
    rationale: Optional[str]


@dataclass
class ScoreResult:
    score: Optional[float]
    score_label: Optional[str]
    outcome_window_end_timestamp: Optional[str]
    notes: str


# ---------------------------------------------------------------------------
# Signal extraction
# ---------------------------------------------------------------------------


def extract_signal(decision: DecisionRow) -> Optional[str]:
    """Return the 5-tier signal in lowercase canonical form, or None if unrecognized.

    Baseline rows: `order_intent_json["signal"]` (per run_prediction_cycle.py).
    Shadow rows: `order_intent_json["shadow_decision"]["shadow_signal"]`
    (per run_hermes_proposal.py).

    Tolerant normalizer: Kimi K2.6 has been observed emitting variants like
    "Strong Buy", "Buy (high conviction)", "BUY!", "Overweight (with caveats)".
    We strip trailing punctuation/whitespace, lowercase, then check exact match
    against SIGNAL_DIRECTION; on miss, fall back to a prefix match against the 5
    canonical tokens (so "strong buy" → "buy"). Anything that fails both checks
    returns None — caller decides whether to skip-or-warn.
    """
    if not decision.order_intent_json:
        return None
    try:
        intent = json.loads(decision.order_intent_json)
    except json.JSONDecodeError:
        return None
    raw = intent.get("signal") if isinstance(intent, dict) else None
    if raw is None and isinstance(intent, dict):
        shadow_dec = intent.get("shadow_decision")
        if isinstance(shadow_dec, dict):
            raw = shadow_dec.get("shadow_signal")
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().strip("!.,;:()[]{}").lower()
    if normalized in SIGNAL_DIRECTION:
        return normalized
    # Tolerant: any leading token that matches a canonical signal wins.
    # Example: "strong buy" / "buy (high conviction)" both → "buy".
    for canonical in SIGNAL_DIRECTION:
        # match either ^canonical$ or ^...\s+canonical\b or ^canonical\b...
        if re.search(rf"\b{re.escape(canonical)}\b", normalized):
            return canonical
    return None


# ---------------------------------------------------------------------------
# Kline fetch (shell out to moomoo skill)
# ---------------------------------------------------------------------------


def fetch_daily_klines(
    ticker: str,
    start_date: str,
    end_date: str,
    *,
    kline_script: str,
    uv_bin: str,
) -> list[dict]:
    """Return daily kline rows sorted by date ascending, each with {date, close}.

    Shells out to the moomoo get_kline.py skill with --ktype 1d --json.
    Trims to one row per trading day with the keys this scorer needs.
    """
    cmd = [
        uv_bin,
        "run",
        "python",
        kline_script,
        ticker,
        "--ktype",
        "1d",
        "--start",
        start_date,
        "--end",
        end_date,
        "--rehab",
        "forward",
        "--json",
    ]
    completed = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=KLINE_TIMEOUT_S,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"get_kline.py rc={completed.returncode} for {ticker} "
            f"[{start_date}..{end_date}]: {completed.stderr.strip()[:300]}"
        )

    # The moomoo SDK emits OpenQuoteContext connect/disconnect log lines on
    # stdout interleaved with the script's `print(json.dumps(...))` payload.
    # Pick the single line that starts with `{` — get_kline.py emits its JSON
    # in one call so there's only ever one such line.
    json_line = next(
        (ln for ln in completed.stdout.splitlines() if ln.lstrip().startswith("{")),
        None,
    )
    if json_line is None:
        raise RuntimeError(
            f"get_kline.py produced no JSON object line for {ticker}; "
            f"stdout head: {completed.stdout[:300]!r}"
        )
    payload = json.loads(json_line)
    if "error" in payload:
        raise RuntimeError(f"get_kline.py error for {ticker}: {payload['error']}")

    raw = payload["data"]
    rows = [
        {"date": str(r["time"])[:10], "close": float(r["close"])}
        for r in raw
        if r.get("time") and r.get("close")
    ]
    rows.sort(key=lambda x: x["date"])
    return rows


# ---------------------------------------------------------------------------
# Metric: next_day_direction
# ---------------------------------------------------------------------------


def score_next_day_direction(
    decision: DecisionRow,
    *,
    kline_script: str,
    uv_bin: str,
    flat_threshold: float,
    today: datetime,
    **_kwargs,
) -> ScoreResult:
    """Score next_day_direction by comparing predicted vs realized direction at T+1 close.

    Outcome window closes at the first US RTH close strictly after trade_date.
    If today is earlier than that close, returns ScoreResult(score=None, notes='window-not-closed').
    """
    signal = extract_signal(decision)
    if signal is None:
        return ScoreResult(
            score=None,
            score_label=None,
            outcome_window_end_timestamp=None,
            notes="signal-unrecognized; no scoring possible",
        )
    predicted_dir = SIGNAL_DIRECTION[signal]

    try:
        trade_d = datetime.strptime(decision.trade_date, "%Y-%m-%d").date()
    except ValueError:
        return ScoreResult(
            score=None,
            score_label=None,
            outcome_window_end_timestamp=None,
            notes=f"trade_date malformed: {decision.trade_date!r}",
        )

    window_start = trade_d.isoformat()
    window_end = (trade_d + timedelta(days=KLINE_WINDOW_DAYS)).isoformat()

    # Match the prefix idiom in run_prediction_cycle.py:195 — ledger stores bare
    # tickers, moomoo skills want a market-prefixed code (US./HK./CN.).
    moomoo_code = (
        decision.ticker
        if decision.ticker.startswith(("US.", "HK.", "CN."))
        else f"US.{decision.ticker}"
    )
    klines = fetch_daily_klines(
        moomoo_code,
        window_start,
        window_end,
        kline_script=kline_script,
        uv_bin=uv_bin,
    )
    if len(klines) < 2:
        return ScoreResult(
            score=None,
            score_label=None,
            outcome_window_end_timestamp=None,
            notes=(
                f"insufficient kline rows ({len(klines)}) in window "
                f"[{window_start}..{window_end}]; need T+0 and T+1 closes"
            ),
        )

    # T+0 = first row at or after trade_date; T+1 = the immediately following row.
    t0 = None
    t1 = None
    for i, row in enumerate(klines):
        if row["date"] >= decision.trade_date:
            t0 = row
            if i + 1 < len(klines):
                t1 = klines[i + 1]
            break
    if t0 is None or t1 is None:
        return ScoreResult(
            score=None,
            score_label=None,
            outcome_window_end_timestamp=None,
            notes="window-not-closed; T+1 candle not yet available",
        )

    # Sanity: T+1 candle must be on or before "today" — otherwise we're scoring a future bar
    # that shouldn't exist yet (data-vendor artifact). Refuse to score in that case.
    try:
        t1_date = datetime.strptime(t1["date"], "%Y-%m-%d").date()
    except ValueError:
        return ScoreResult(
            score=None,
            score_label=None,
            outcome_window_end_timestamp=None,
            notes=f"T+1 date malformed: {t1['date']!r}",
        )
    if t1_date > today.date():
        return ScoreResult(
            score=None,
            score_label=None,
            outcome_window_end_timestamp=None,
            notes=f"window-not-closed; T+1 ({t1_date.isoformat()}) is in the future",
        )
    # If T+1 is today, the daily candle may still be in-progress (US RTH closes
    # at ≈21:00 UTC standard / 20:00 UTC daylight saving). Refuse to score until
    # the close has happened — otherwise a partial intraday candle gets locked
    # in as the final score.
    RTH_CLOSE_UTC_HOUR = 21  # conservative — covers EST; EDT will be one hour earlier
    if t1_date == today.date() and today.hour < RTH_CLOSE_UTC_HOUR:
        return ScoreResult(
            score=None,
            score_label=None,
            outcome_window_end_timestamp=None,
            notes=(
                f"window-not-closed; T+1 is today and current UTC hour "
                f"{today.hour} < {RTH_CLOSE_UTC_HOUR} (US RTH close)"
            ),
        )

    if t0["close"] <= 0:
        return ScoreResult(
            score=None,
            score_label=None,
            outcome_window_end_timestamp=None,
            notes=f"T+0 close non-positive ({t0['close']}); skip",
        )

    rel_move = (t1["close"] - t0["close"]) / t0["close"]
    if abs(rel_move) < flat_threshold:
        realized_dir = 0
        realized_label = "flat"
    elif rel_move > 0:
        realized_dir = +1
        realized_label = "up"
    else:
        realized_dir = -1
        realized_label = "down"

    predicted_label = {+1: "up", 0: "flat", -1: "down"}[predicted_dir]
    matched = predicted_dir == realized_dir
    score = 1.0 if matched else 0.0
    label = f"{predicted_label}_predicted_{realized_label}_realized"

    # outcome_window_end_timestamp: T+1 RTH close. We don't have intraday session times here,
    # so anchor to T+1 21:00 UTC (≈ 16:00 ET RTH close standard time) as a reasonable proxy.
    window_end_ts = f"{t1['date']}T21:00:00+00:00"

    notes = (
        f"signal={signal}; t0={t0['date']}@{t0['close']:.4f}; "
        f"t1={t1['date']}@{t1['close']:.4f}; rel_move={rel_move:+.4%}; "
        f"flat_threshold={flat_threshold:.4f}"
    )
    return ScoreResult(
        score=score,
        score_label=label,
        outcome_window_end_timestamp=window_end_ts,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Metric: risk_rule_compliance (T+0, structured-v0)
# ---------------------------------------------------------------------------

# Three deterministic rules per spec § Hermes Evaluation Metrics & Shadow Mode
# and the 2026-05-07 Hermes review (Risk Neutral edit 2 — ATR-bound stop):
#   1. stop_present     — a stop price level extractable from the rationale
#   2. stop_atr_bound   — |entry - stop| ∈ [ATR_BAND_LOW, ATR_BAND_HIGH]× ATR
#   3. qty_under_cap    — position size ≤ POSITION_QUANTITY_CAP
# Each rule contributes 1/3; Hold short-circuits to 1.0 — no position means
# no risk-management rules apply.
#
# v0 quirk: order_intent_json stores only LLM free-text fields (signal,
# investment_plan, trader_investment_plan, final_trade_decision). Until
# run_prediction_cycle.py extracts structured entry/stop/atr/quantity keys at
# decision time, this scorer regex-extracts numerics from prose. Brittle by
# construction — rationale shape evolves with TradingAgents prompts. Migration
# target: structured-v1 reads typed JSON fields rather than mining the text.
#
# Bidirectional keyword/value extraction handles both phrasings the LLM emits:
# "$181 hard stop" (W3.18 baseline: $N before keyword) and "stop at $181"
# (keyword before $N). Closest-side wins per keyword match.
ATR_BAND_LOW = 1.5   # ×ATR — stop closer than this is too tight (intraday noise floor)
ATR_BAND_HIGH = 3.0  # ×ATR — stop farther than this is too loose (oversized risk)
POSITION_QUANTITY_CAP = 5  # shares — v0 paper-trading sanity bound
KEYWORD_VALUE_WINDOW_CHARS = 80  # search window each side of a keyword match

ENTRY_KEYWORD_RE = re.compile(
    r"\b(?:entry\s+pinch|entry\s+price|entry\s+at|entry\s*[:=]|"
    r"buy\s+near|enter\s+at|enter\s+near|trigger\s+at|limit\s+at|"
    r"initiate\s+at|pinch\s+at|entry)\b",  # bare 'entry' last (proximity check provides safety)
    re.IGNORECASE,
)
STOP_KEYWORD_RE = re.compile(
    r"\b(?:hard\s+stop|stop[-\s]?loss|stop\s+at|stop\s+near|stop\s+to|"
    r"sl\s*[:=]|cut\s+at|risk\s+to|stop\s*[:=]|stop)\b",  # bare 'stop' last
    re.IGNORECASE,
)
ATR_KEYWORD_RE = re.compile(
    r"\b(?:atr|average\s+true\s+range|daily\s+range)\b",
    re.IGNORECASE,
)
QUANTITY_RE = re.compile(
    r"\b(\d{1,3})\s+shares?\b|"
    r"\b(?:quantity|qty|size\s+of|position\s+size)\s*[:=]?\s*(\d{1,3})\b",
    re.IGNORECASE,
)

# Clause-separator chars: when found in the gap between a $N candidate and the
# keyword anchor, the $N belongs to a different clause (e.g.,
# "Entry pinch at $196, hard stop at $181" — the comma before "hard stop"
# means the backward $196 is the entry's value, not the stop's).
_GAP_CLAUSE_SEPARATORS = re.compile(r"[,;.]")


def _extract_price_near_keyword(
    keyword_re: re.Pattern,
    text: str,
    *,
    require_dollar: bool = True,
    window: int = KEYWORD_VALUE_WINDOW_CHARS,
) -> Optional[float]:
    """Closest numeric value within `window` chars of any keyword match.

    For each keyword hit, look forward (keyword → $N) and backward ($N → keyword)
    up to `window` chars. A candidate is REJECTED if the gap text between the
    keyword and the $N contains a clause separator (`,`, `;`, `.`) — that signals
    the $N belongs to a different clause. Among valid candidates, the closer
    side wins. Returns None if no valid candidate in any keyword window.

    `require_dollar=True` restricts matches to $-prefixed numbers (entry, stop);
    `False` accepts bare numbers too (ATR, which the rationale may write as
    'ATR ~5.30' without a dollar sign).
    """
    pattern = r"\$\s*(\d{1,5}(?:\.\d+)?)" if require_dollar else r"\$?\s*(\d{1,5}(?:\.\d+)?)"
    pat = re.compile(pattern)
    for m in keyword_re.finditer(text):
        start, end = m.span()
        # Forward candidate
        tail = text[end:end + window]
        forward = pat.search(tail)
        fwd_dist = None
        if forward:
            fwd_gap = tail[:forward.start()]
            if _GAP_CLAUSE_SEPARATORS.search(fwd_gap):
                forward = None
            else:
                fwd_dist = forward.start()
        # Backward candidate
        head = text[max(0, start - window):start]
        backward_all = list(pat.finditer(head))
        backward = backward_all[-1] if backward_all else None
        bwd_dist = None
        if backward:
            bwd_gap = head[backward.end():]
            if _GAP_CLAUSE_SEPARATORS.search(bwd_gap):
                backward = None
            else:
                bwd_dist = len(head) - backward.end()
        # Pick closer valid candidate
        if forward and (backward is None or fwd_dist <= bwd_dist):
            try:
                return float(forward.group(1))
            except ValueError:
                continue
        if backward:
            try:
                return float(backward.group(1))
            except ValueError:
                continue
    return None


def _extract_quantity(text: str) -> Optional[int]:
    """First quantity-like token: 'N shares', 'quantity N', 'size of N', 'qty N'."""
    m = QUANTITY_RE.search(text)
    if not m:
        return None
    raw = m.group(1) or m.group(2)
    try:
        return int(raw)
    except (ValueError, TypeError):
        return None


def score_risk_rule_compliance(
    decision: DecisionRow,
    *,
    quantity_cap: int = POSITION_QUANTITY_CAP,
    atr_band_low: float = ATR_BAND_LOW,
    atr_band_high: float = ATR_BAND_HIGH,
    **_kwargs,
) -> ScoreResult:
    """T+0 structured-rule score: 3 deterministic risk-rule checks.

    Rules (each worth 1/3):
      1. stop_present     — a stop price level extractable from rationale
      2. stop_atr_bound   — |entry - stop| within [atr_band_low, atr_band_high]× ATR
      3. qty_under_cap    — position size ≤ quantity_cap (falls back to broker
         default qty=1 from run_prediction_cycle.py:234 when text has no size token)
    Hold signals short-circuit to 1.0 (no position to risk-manage).
    """
    signal = extract_signal(decision)
    window_end_ts = f"{decision.trade_date}T21:00:00+00:00"

    if signal is None:
        return ScoreResult(
            score=None,
            score_label=None,
            outcome_window_end_timestamp=None,
            notes="signal-unrecognized; no scoring possible",
        )

    if signal == "hold":
        return ScoreResult(
            score=1.0,
            score_label="structured-v0/hold-no-trade",
            outcome_window_end_timestamp=window_end_ts,
            notes="no-trade-signal-hold",
        )

    text = _extract_decision_text(decision)
    entry = _extract_price_near_keyword(ENTRY_KEYWORD_RE, text)
    stop = _extract_price_near_keyword(STOP_KEYWORD_RE, text)
    atr = _extract_price_near_keyword(ATR_KEYWORD_RE, text, require_dollar=False)
    qty = _extract_quantity(text)

    rule_stop_present = stop is not None

    band_low_val = band_high_val = distance = None
    if entry is not None and stop is not None and atr is not None and atr > 0:
        distance = abs(entry - stop)
        band_low_val = atr_band_low * atr
        band_high_val = atr_band_high * atr
        rule_stop_atr_bound = band_low_val <= distance <= band_high_val
    else:
        rule_stop_atr_bound = False

    # Qty fallback: place_paper_order() defaults qty=1 at
    # run_prediction_cycle.py:234, so when text omits a size token the broker
    # reality is qty=1 ≤ cap. Surface the inference in notes so it's auditable.
    qty_inferred = qty is None
    effective_qty = 1 if qty_inferred else qty
    rule_qty_under_cap = effective_qty <= quantity_cap

    passes = int(rule_stop_present) + int(rule_stop_atr_bound) + int(rule_qty_under_cap)
    score = round(passes / 3.0, 2)  # snap to user-facing 0.0/0.33/0.67/1.0

    label = (
        f"structured-v0/{passes}-of-3:"
        f"stop_present={rule_stop_present},"
        f"stop_atr_bound={rule_stop_atr_bound},"
        f"qty_under_cap={rule_qty_under_cap}"
    )
    band_str = (
        f"band=[{band_low_val:.2f},{band_high_val:.2f}]@distance={distance:.2f}"
        if band_low_val is not None
        else "band=N/A(entry/stop/atr missing)"
    )
    qty_str = (
        f"qty={effective_qty}(inferred-from-broker-default)"
        if qty_inferred
        else f"qty={effective_qty}"
    )
    notes = (
        f"structured-v0 regex extraction over rationale+order_intent_json "
        f"(text_len={len(text)}); signal={signal}; "
        f"entry={entry}; stop={stop}; atr={atr}; {qty_str}; "
        f"{band_str}; cap={quantity_cap}; atr_band=[{atr_band_low}x..{atr_band_high}x]"
    )
    return ScoreResult(
        score=score,
        score_label=label,
        outcome_window_end_timestamp=window_end_ts,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Metric: rationale_trade_match (T+0, heuristic-v0)
# ---------------------------------------------------------------------------

# Word-boundary lexicons for sentiment-vs-signal alignment. Tokens picked from
# the TradingAgents PM/Trader/RM blessed-baseline prompt corpus + the NVDA
# baseline rationale we have in hand. Heuristic-v0 — replace with LLM judgment
# in v1 if false-positive rate proves intolerable across the first 30 pairs.
BULL_TOKENS = re.compile(
    r"\b(buy|long|bullish|rally|uptrend|accumulate|outperform|"
    r"beat|growth|expansion|strong\s+franchise|tailwind|catalyst)\b",
    re.IGNORECASE,
)
BEAR_TOKENS = re.compile(
    r"\b(sell|short|reduce|bearish|downside|overvalued|structural\s+risk|"
    r"underperform|miss|contraction|headwind|deteriorat|cyclical\s+peak|"
    r"late[\s\-]?cycle)\b",
    re.IGNORECASE,
)
SENTIMENT_THRESHOLD = 0.2  # |sentiment| must clear this for a directional match

# Trust-boundary markers from /opt/tradingagents/tradingagents/dataflows/moomoo_news.py
# (/cso Finding 3 mitigation). The article body wrapped by these markers contains
# third-party content that the analyst LLM consumed but isn't authored opinion —
# leaving it in the scored text contaminates the lexicon count with whichever way
# the article happened to lean.
_TRUST_BOUNDARY_BLOCK = re.compile(
    r"---\s*THIRD-PARTY UNTRUSTED CONTENT BEGIN\s*---.*?"
    r"---\s*THIRD-PARTY UNTRUSTED CONTENT END\s*---",
    re.DOTALL,
)


def _extract_decision_text(decision: DecisionRow) -> str:
    """Build the text used for sentiment scoring.

    Reads the structured fields out of `order_intent_json` (not the raw JSON blob
    whose key names like `"signal": "Underweight"` would contribute false lexicon
    hits). Baseline rows expose final_trade_decision + investment_plan; shadow
    rows expose shadow_decision.shadow_rationale. Strips moomoo trust-boundary
    blocks so a bearish article body inside a Buy rationale doesn't flip the
    sentiment score.
    """
    parts: list[str] = [decision.rationale or ""]
    if decision.order_intent_json:
        try:
            intent = json.loads(decision.order_intent_json)
        except json.JSONDecodeError:
            intent = None
        if isinstance(intent, dict):
            for k in ("final_trade_decision", "investment_plan", "trader_investment_plan"):
                v = intent.get(k)
                if isinstance(v, str):
                    parts.append(v)
            shadow_dec = intent.get("shadow_decision")
            if isinstance(shadow_dec, dict):
                v = shadow_dec.get("shadow_rationale")
                if isinstance(v, str):
                    parts.append(v)
    joined = " ".join(parts)
    return _TRUST_BOUNDARY_BLOCK.sub(" [redacted-untrusted-block] ", joined)


def score_rationale_trade_match(
    decision: DecisionRow, **_kwargs
) -> ScoreResult:
    """Heuristic T+0 score: does rationale sentiment align with predicted direction?"""
    signal = extract_signal(decision)
    if signal is None:
        return ScoreResult(
            score=None,
            score_label=None,
            outcome_window_end_timestamp=None,
            notes="signal-unrecognized; rationale_trade_match cannot be scored",
        )
    predicted_dir = SIGNAL_DIRECTION[signal]

    text = _extract_decision_text(decision)
    bull = len(BULL_TOKENS.findall(text))
    bear = len(BEAR_TOKENS.findall(text))
    total = bull + bear
    sentiment = (bull - bear) / total if total > 0 else 0.0

    if predicted_dir == +1:
        matched = sentiment > SENTIMENT_THRESHOLD
    elif predicted_dir == -1:
        matched = sentiment < -SENTIMENT_THRESHOLD
    else:  # Hold / flat
        matched = abs(sentiment) <= SENTIMENT_THRESHOLD

    score = 1.0 if matched else 0.0
    label = (
        f"heuristic-v0:bull={bull},bear={bear},"
        f"sentiment={sentiment:+.2f},predicted_dir={predicted_dir:+d},"
        f"threshold=±{SENTIMENT_THRESHOLD}"
    )
    window_end_ts = f"{decision.trade_date}T21:00:00+00:00"
    notes = (
        f"heuristic-v0 lexicon pass over rationale+order_intent_json "
        f"(text_len={len(text)}); signal={signal}; "
        f"bull_hits={bull}, bear_hits={bear}, sentiment={sentiment:+.4f}"
    )
    return ScoreResult(
        score=score,
        score_label=label,
        outcome_window_end_timestamp=window_end_ts,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Metric: volatility_adjusted_move (T+5, z-score-normalized directional match)
# ---------------------------------------------------------------------------


def score_volatility_adjusted_move(
    decision: DecisionRow,
    *,
    kline_script: str,
    uv_bin: str,
    vol_adj_threshold: float,
    today: datetime,
    **_kwargs,
) -> ScoreResult:
    """Score volatility_adjusted_move by comparing predicted direction vs realized
    vol-normalized move at T+5 close.

    realized_move = (t5_close - t0_close) / t0_close
    daily_returns = 5 returns from the 6 closes T+0..T+5
    realized_vol  = statistics.stdev(daily_returns)  # sample, ddof=1
    vol_adj_move  = realized_move / realized_vol     # signed z-score-like, dimensionless
    realized_dir  = +1 if vol_adj_move > +threshold; -1 if < -threshold; 0 otherwise

    Outcome window closes at T+5 RTH close (~21:00 UTC). Refuses to score until
    5 trading-day closes are in (mirrors the T+1 guard in score_next_day_direction).
    Hold predicts flat; "flat" means |vol_adj_move| ≤ threshold (no short-circuit —
    Hold IS evaluated against realized vol-adjusted move, same as next_day_direction).
    realized_vol == 0 defers with notes='degenerate-vol' (no signal from a no-vol stock).
    """
    signal = extract_signal(decision)
    if signal is None:
        return ScoreResult(
            score=None,
            score_label=None,
            outcome_window_end_timestamp=None,
            notes="signal-unrecognized; no scoring possible",
        )
    predicted_dir = SIGNAL_DIRECTION[signal]

    try:
        trade_d = datetime.strptime(decision.trade_date, "%Y-%m-%d").date()
    except ValueError:
        return ScoreResult(
            score=None,
            score_label=None,
            outcome_window_end_timestamp=None,
            notes=f"trade_date malformed: {decision.trade_date!r}",
        )

    window_start = trade_d.isoformat()
    window_end = (trade_d + timedelta(days=KLINE_WINDOW_DAYS_T5)).isoformat()

    moomoo_code = (
        decision.ticker
        if decision.ticker.startswith(("US.", "HK.", "CN."))
        else f"US.{decision.ticker}"
    )
    klines = fetch_daily_klines(
        moomoo_code,
        window_start,
        window_end,
        kline_script=kline_script,
        uv_bin=uv_bin,
    )

    # T+0 = first row at or after trade_date; T+1..T+5 = the next 5 rows.
    t0_idx = None
    for i, row in enumerate(klines):
        if row["date"] >= decision.trade_date:
            t0_idx = i
            break
    if t0_idx is None or t0_idx + 5 >= len(klines):
        return ScoreResult(
            score=None,
            score_label=None,
            outcome_window_end_timestamp=None,
            notes=(
                f"window-not-closed; need T+0..T+5 (6 closes) in window "
                f"[{window_start}..{window_end}]; got {len(klines)} rows"
            ),
        )

    window_rows = klines[t0_idx : t0_idx + 6]  # T+0..T+5 inclusive

    # T+5 must already have happened. Same RTH-close guard as next_day_direction's
    # T+1 check — if T+5 is today, refuse to score until the close has happened
    # (US RTH closes at ≈21:00 UTC standard / 20:00 UTC daylight saving).
    try:
        t5_date = datetime.strptime(window_rows[-1]["date"], "%Y-%m-%d").date()
    except ValueError:
        return ScoreResult(
            score=None,
            score_label=None,
            outcome_window_end_timestamp=None,
            notes=f"T+5 date malformed: {window_rows[-1]['date']!r}",
        )
    if t5_date > today.date():
        return ScoreResult(
            score=None,
            score_label=None,
            outcome_window_end_timestamp=None,
            notes=f"window-not-closed; T+5 ({t5_date.isoformat()}) is in the future",
        )
    RTH_CLOSE_UTC_HOUR = 21  # conservative — covers EST; EDT will be one hour earlier
    if t5_date == today.date() and today.hour < RTH_CLOSE_UTC_HOUR:
        return ScoreResult(
            score=None,
            score_label=None,
            outcome_window_end_timestamp=None,
            notes=(
                f"window-not-closed; T+5 is today and current UTC hour "
                f"{today.hour} < {RTH_CLOSE_UTC_HOUR} (US RTH close)"
            ),
        )

    closes = [r["close"] for r in window_rows]
    # closes[0..4] are denominators in daily_returns + realized_move; a zero or
    # negative in any of those positions would crash with ZeroDivisionError,
    # which main()'s except clause does NOT catch (only TimeoutExpired +
    # RuntimeError) — would kill the entire scoring run on one bad row. Defer.
    for i, c in enumerate(closes[:5]):
        if c <= 0:
            return ScoreResult(
                score=None,
                score_label=None,
                outcome_window_end_timestamp=None,
                notes=(
                    f"non-positive close in denominator at T+{i} "
                    f"({window_rows[i]['date']}@{c}); skip"
                ),
            )

    realized_move = (closes[5] - closes[0]) / closes[0]
    daily_returns = [(closes[i + 1] - closes[i]) / closes[i] for i in range(5)]
    realized_vol = statistics.stdev(daily_returns)  # sample stdev, ddof=1

    if realized_vol == 0:
        return ScoreResult(
            score=None,
            score_label=None,
            outcome_window_end_timestamp=None,
            notes=(
                f"degenerate-vol; realized_vol=0 over T+0..T+5; "
                f"daily_returns={daily_returns}"
            ),
        )

    vol_adj_move = realized_move / realized_vol

    if vol_adj_move > vol_adj_threshold:
        realized_dir = +1
        realized_label = "up"
    elif vol_adj_move < -vol_adj_threshold:
        realized_dir = -1
        realized_label = "down"
    else:
        realized_dir = 0
        realized_label = "flat"

    predicted_label = {+1: "up", 0: "flat", -1: "down"}[predicted_dir]
    matched = predicted_dir == realized_dir
    score = 1.0 if matched else 0.0
    label = (
        f"{predicted_label}_predicted_{realized_label}_realized_"
        f"vol={realized_vol:.4f}_zmove={vol_adj_move:+.2f}"
    )

    window_end_ts = f"{window_rows[-1]['date']}T21:00:00+00:00"

    notes = (
        f"signal={signal}; t0={window_rows[0]['date']}@{closes[0]:.4f}; "
        f"t5={window_rows[-1]['date']}@{closes[5]:.4f}; "
        f"realized_move={realized_move:+.4%}; realized_vol={realized_vol:.4%}; "
        f"vol_adj_move={vol_adj_move:+.4f}; threshold=±{vol_adj_threshold}"
    )
    return ScoreResult(
        score=score,
        score_label=label,
        outcome_window_end_timestamp=window_end_ts,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Metric dispatch
# ---------------------------------------------------------------------------


def _not_implemented(metric: str) -> Callable:
    def _impl(*_args, **_kwargs):
        raise NotImplementedError(
            f"scorer for {metric!r} is not implemented in v0 — only "
            f"next_day_direction, risk_rule_compliance, rationale_trade_match, "
            f"volatility_adjusted_move"
        )

    return _impl


METRIC_DISPATCH: dict[str, Callable] = {
    "next_day_direction": score_next_day_direction,
    "risk_rule_compliance": score_risk_rule_compliance,
    "rationale_trade_match": score_rationale_trade_match,
    "volatility_adjusted_move": score_volatility_adjusted_move,
    "thesis_accuracy": _not_implemented("thesis_accuracy"),
    "max_adverse_excursion": _not_implemented("max_adverse_excursion"),
    "catalyst_correctness": _not_implemented("catalyst_correctness"),
}


# ---------------------------------------------------------------------------
# Ledger I/O
# ---------------------------------------------------------------------------


def fetch_unscored_decisions(
    ledger_path: Path,
    metric: str,
    *,
    decision_id: Optional[str] = None,
) -> list[DecisionRow]:
    """Return decisions whose metric_scores.score IS NULL for `metric`."""
    sql = """
        SELECT d.decision_id, d.ticker, d.trade_date, d.decision_kind,
               d.order_intent_json, d.rationale
        FROM decisions d
        JOIN metric_scores m ON m.decision_id = d.decision_id
        WHERE m.metric_name = ? AND m.score IS NULL
    """
    params: list = [metric]
    if decision_id:
        sql += " AND d.decision_id = ?"
        params.append(decision_id)
    sql += " ORDER BY d.trade_date ASC, d.decision_id ASC"

    with sqlite3.connect(str(ledger_path)) as conn:
        # PRAGMA foreign_keys is per-connection (not per-database) — every script
        # opening a connection must re-issue it, otherwise FK constraints silently
        # fail on this connection even though init_eval_ledger.py enabled them.
        conn.execute("PRAGMA foreign_keys = ON")
        rows = conn.execute(sql, params).fetchall()
    return [
        DecisionRow(
            decision_id=r[0],
            ticker=r[1],
            trade_date=r[2],
            decision_kind=r[3],
            order_intent_json=r[4] or "",
            rationale=r[5],
        )
        for r in rows
    ]


def update_metric_score(
    ledger_path: Path,
    decision_id: str,
    metric: str,
    result: ScoreResult,
) -> None:
    now_utc = datetime.now(timezone.utc).isoformat()
    sql = """
        UPDATE metric_scores
           SET score = ?,
               score_label = ?,
               outcome_window_end_timestamp = ?,
               computed_at = ?,
               computation_notes = ?
         WHERE decision_id = ? AND metric_name = ?
    """
    with sqlite3.connect(str(ledger_path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        cur = conn.execute(
            sql,
            (
                result.score,
                result.score_label,
                result.outcome_window_end_timestamp,
                now_utc,
                result.notes,
                decision_id,
                metric,
            ),
        )
        # The UPDATE must have hit exactly one row — otherwise the orchestrator
        # logs "SCORED" but the ledger stays NULL. Raise loudly so the caller's
        # failed-counter increments rather than silently diverging from reality.
        if cur.rowcount != 1:
            raise RuntimeError(
                f"update_metric_score rowcount={cur.rowcount} (expected 1) for "
                f"decision_id={decision_id!r} metric={metric!r}"
            )
        conn.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    p.add_argument(
        "--metric",
        default="next_day_direction",
        choices=ALL_METRICS,
        help="Metric to score (default: next_day_direction; v0 only this metric is implemented)",
    )
    p.add_argument("--decision-id", default=None, help="Score only this decision_id")
    p.add_argument(
        "--ledger-path",
        default=os.environ.get("ARK_EVAL_LEDGER_PATH", DEFAULT_LEDGER_PATH),
    )
    p.add_argument("--kline-script", default=DEFAULT_KLINE_SCRIPT)
    p.add_argument("--uv-bin", default=DEFAULT_UV_BIN, help="Absolute path to uv binary")
    p.add_argument("--flat-threshold", type=float, default=DEFAULT_FLAT_THRESHOLD)
    p.add_argument(
        "--vol-adj-threshold",
        type=float,
        default=DEFAULT_VOL_ADJ_THRESHOLD,
        help="volatility_adjusted_move σ band (default 1.0)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print; do not UPDATE the ledger",
    )
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    ledger = Path(args.ledger_path)
    if not ledger.exists():
        print(f"ERROR: ledger not found at {ledger}", file=sys.stderr)
        return 2

    scorer = METRIC_DISPATCH[args.metric]
    today = datetime.now(timezone.utc)

    targets = fetch_unscored_decisions(ledger, args.metric, decision_id=args.decision_id)
    if not targets:
        print(f"no unscored {args.metric} rows in {ledger}")
        return 0

    print(
        f"scoring metric={args.metric} targets={len(targets)} "
        f"ledger={ledger} dry_run={args.dry_run}"
    )

    scored = 0
    deferred = 0
    failed = 0
    for d in targets:
        try:
            result = scorer(
                d,
                kline_script=args.kline_script,
                uv_bin=args.uv_bin,
                flat_threshold=args.flat_threshold,
                vol_adj_threshold=args.vol_adj_threshold,
                today=today,
            )
        except NotImplementedError as exc:
            print(f"  [{d.decision_id} {d.ticker}] NOT IMPLEMENTED: {exc}")
            failed += 1
            continue
        except (subprocess.TimeoutExpired, RuntimeError) as exc:
            print(f"  [{d.decision_id} {d.ticker}] ERROR: {exc}")
            failed += 1
            continue

        tag = "SCORED" if result.score is not None else "DEFER"
        print(
            f"  [{d.decision_id} {d.ticker} kind={d.decision_kind} trade_date={d.trade_date}] "
            f"{tag} score={result.score} label={result.score_label} notes={result.notes}"
        )
        if result.score is None:
            deferred += 1
            continue

        if not args.dry_run:
            update_metric_score(ledger, d.decision_id, args.metric, result)
        scored += 1

    print(
        f"done: scored={scored} deferred={deferred} failed={failed} "
        f"(dry_run={args.dry_run})"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
