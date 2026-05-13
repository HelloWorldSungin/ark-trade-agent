#!/usr/bin/env python3
"""Score decision-quality metrics for the Ark Trade Agent v0 eval ledger.

Per spec § Build Order step 22+ (outcome-window scorer; surfaced by W4.22
Day-1 Hermes review at `vault/Session-Logs/2026-05-07-hermes-review.md`).

v0 scope: `next_day_direction` only. The other six metrics
(`thesis_accuracy`, `volatility_adjusted_move`, `max_adverse_excursion`,
`catalyst_correctness`, `risk_rule_compliance`, `rationale_trade_match`)
have their dispatch slots wired but raise NotImplementedError when called.
This is the smallest unit that unblocks the Hermes-Shadow Delta — once a
baseline+shadow pair both have a non-NULL `next_day_direction` score, the
delta SQL in `run_hermes_proposal.py` returns a row.

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
        [--dry-run]                 (compute + print; no UPDATEs)

Runs on LOQ where OpenD + the moomoo SDK live. Mac dev edits + scp to LOQ.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

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

ALL_METRICS = (
    "thesis_accuracy",
    "next_day_direction",
    "volatility_adjusted_move",
    "max_adverse_excursion",
    "catalyst_correctness",
    "risk_rule_compliance",
    "rationale_trade_match",
)

# 5-tier signal vocabulary from spec § Hermes Evaluation Metrics & Shadow Mode.
# Mapped to direction sign for `next_day_direction` scoring.
SIGNAL_DIRECTION = {
    "buy": +1,
    "overweight": +1,
    "hold": 0,
    "underweight": -1,
    "sell": -1,
}


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
    """Return the 5-tier signal in lowercase, or None if unrecognized.

    Baseline rows: `order_intent_json["signal"]` (per run_prediction_cycle.py).
    Shadow rows: `order_intent_json["shadow_decision"]["shadow_signal"]`
    (per run_hermes_proposal.py).
    """
    intent = json.loads(decision.order_intent_json) if decision.order_intent_json else {}
    raw = intent.get("signal")
    if raw is None:
        shadow_dec = intent.get("shadow_decision")
        if isinstance(shadow_dec, dict):
            raw = shadow_dec.get("shadow_signal")
    if not isinstance(raw, str):
        return None
    normalized = raw.strip().lower()
    return normalized if normalized in SIGNAL_DIRECTION else None


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
# Metric: risk_rule_compliance (T+0, heuristic-v0)
# ---------------------------------------------------------------------------

# Three risk-rule ingredients the rationale/order_intent must mention for a
# directional trade: entry price, exit/target zone, hard stop. Patterns scan
# for either explicit price levels ("$181") or named structure ("stop loss").
# Heuristic-v0 — future iterations can replace with LLM judgment or with
# direct order_intent_json structured fields once run_prediction_cycle.py
# stops emitting only text blobs.
ENTRY_PATTERN = re.compile(
    r"(entry|enter\s+at|buy\s+near|near\s+\$\s*\d+|limit\s+\$\s*\d+|"
    r"around\s+\$\s*\d+|initiate|entry\s+price)",
    re.IGNORECASE,
)
TARGET_PATTERN = re.compile(
    r"(target|trim|take[\s\-]?profit|exit\s+\$\s*\d+|sell\s+at\s+\$\s*\d+|"
    r"upside\s+\$\s*\d+|tp\s*[:=]\s*\$?\d+|profit[\s\-]?target)",
    re.IGNORECASE,
)
STOP_PATTERN = re.compile(
    r"(stop[\s\-]?loss|stop\s+\$\s*\d+|sl\s*[:=]\s*\$?\d+|cut\s+at\s+\$\s*\d+|"
    r"hard\s+stop|risk\s+\$\s*\d+|downside\s+\$\s*\d+|stop\s+at\s+\$\s*\d+)",
    re.IGNORECASE,
)


def score_risk_rule_compliance(
    decision: DecisionRow, **_kwargs
) -> ScoreResult:
    """Heuristic T+0 score: count of (entry, target, stop) mentions / 3."""
    text = (decision.rationale or "") + " " + (decision.order_intent_json or "")
    has_entry = bool(ENTRY_PATTERN.search(text))
    has_target = bool(TARGET_PATTERN.search(text))
    has_stop = bool(STOP_PATTERN.search(text))
    count = int(has_entry) + int(has_target) + int(has_stop)
    score = count / 3.0
    label = (
        f"heuristic-v0/{count}-of-3:"
        f"entry={has_entry},target={has_target},stop={has_stop}"
    )
    window_end_ts = f"{decision.trade_date}T21:00:00+00:00"
    notes = (
        f"heuristic-v0 regex pass over rationale+order_intent_json "
        f"(text_len={len(text)}); ingredients found: "
        f"entry={has_entry}, target={has_target}, stop={has_stop}"
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

    text = (decision.rationale or "") + " " + (decision.order_intent_json or "")
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
# Metric dispatch
# ---------------------------------------------------------------------------


def _not_implemented(metric: str) -> Callable:
    def _impl(*_args, **_kwargs):
        raise NotImplementedError(
            f"scorer for {metric!r} is not implemented in v0 — only "
            f"next_day_direction, risk_rule_compliance, rationale_trade_match"
        )

    return _impl


METRIC_DISPATCH: dict[str, Callable] = {
    "next_day_direction": score_next_day_direction,
    "risk_rule_compliance": score_risk_rule_compliance,
    "rationale_trade_match": score_rationale_trade_match,
    "thesis_accuracy": _not_implemented("thesis_accuracy"),
    "volatility_adjusted_move": _not_implemented("volatility_adjusted_move"),
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
        conn.execute(
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
