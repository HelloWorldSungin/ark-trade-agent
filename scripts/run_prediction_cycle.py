#!/usr/bin/env python3
"""Run one TradingAgents prediction cycle, paper-trade the result, log to eval ledger.

Per spec § Build Order step 18 + § Hermes Evaluation Metrics & Shadow Mode.

Host-side orchestrator (Path A — TradingAgents container stays vendor-pure):
1. Invoke TradingAgents container via `docker compose run` with chutes/K2.6 + moomoo
   content vendors enabled. Capture decision JSON via sentinel markers in stdout.
2. Parse 5-tier rating (Buy / Overweight / Hold / Underweight / Sell).
3. Insert one row in `decisions` (kind=baseline) + 7 rows in `metric_scores`
   (NULL score, "deferred-outcome-window-pending" notes — Hermes Week 4+ populates).
4. Map signal to moomoo paper-trade:
     Buy / Overweight  → BUY 1 share market SIMULATE via place_order.py skill
     Hold              → no order, decision row only
     Underweight / Sell → skip (v0 smoke has no existing position to close;
                          short-selling left for follow-up)
5. If order placed, write broker_order_id back onto the decision row. Fill rows
   are deferred (a separate poll-fills script materializes them when OpenD reports
   the actual fill price/qty/timestamp).

Defaults assume LOQ paths. Override via env vars or CLI flags as documented below.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sqlite3
import sys
import textwrap
import uuid
from datetime import datetime, timezone

from ledger_constants import CANONICAL_SIGNALS, METRICS

# ---------- Defaults (LOQ-shaped) ----------
DEFAULT_LEDGER_PATH = os.environ.get("ARK_EVAL_LEDGER_PATH", "/opt/ark-data/eval-ledger.sqlite")
DEFAULT_TRADINGAGENTS_DIR = os.environ.get("ARK_TRADINGAGENTS_DIR", "/opt/tradingagents")
DEFAULT_PROJECT_DIR = os.environ.get("ARK_PROJECT_DIR", "/opt/ark-trade-agent")
DEFAULT_PROMPT_VERSION = "v0.2.4-blessed-2026-05-07"  # matches blessed-baseline vault page
DEFAULT_MODEL = "moonshotai/Kimi-K2.6-TEE"
DEFAULT_CONTAINER_TIMEOUT_SEC = 1500
# Absolute path mandatory — `ssh user@host 'cmd'` and systemd ExecStart both skip
# ~/.bashrc, so `~/.local/bin` won't be on PATH. Mirrors score_metrics.py:53.
DEFAULT_UV_BIN = os.environ.get("ARK_UV_BIN", "/home/ark-dev/.local/bin/uv")
SENTINEL_BEGIN = "__ARK_DECISION_JSON__"
SENTINEL_END = "__ARK_DECISION_JSON_END__"

# METRICS + CANONICAL_SIGNALS imported from ledger_constants at top of file.

# ---------- Argument validators ----------
# build_inner_script() interpolates `ticker` and `trade_date` via f-string repr()
# into Python source that runs inside the TradingAgents container. repr() is partial
# protection against argv injection — these regex validators are the real guard.
# Match common US-listing shapes (NVDA, BRK.B, BF.B). Reject anything else loudly
# at argparse time so a cron/OpenClaw caller can't reach build_inner_script with
# unvalidated input.
TICKER_RE = re.compile(r"^[A-Z]{1,5}(\.[A-Z]{1,2})?$")
TRADE_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_ticker(s: str) -> str:
    if not TICKER_RE.match(s):
        raise argparse.ArgumentTypeError(
            f"ticker {s!r} must match [A-Z]{{1,5}}(\\.[A-Z]{{1,2}})? (e.g. NVDA, BRK.B)"
        )
    return s


def _validate_trade_date(s: str) -> str:
    if not TRADE_DATE_RE.match(s):
        raise argparse.ArgumentTypeError(f"trade_date {s!r} must be YYYY-MM-DD")
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"trade_date {s!r}: {e}") from e
    return s


# ---------- Inner Python snippet (runs inside TradingAgents container) ----------
def build_inner_script(ticker: str, trade_date: str, model: str, debate_rounds: int) -> str:
    return textwrap.dedent(f"""
        import json
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.default_config import DEFAULT_CONFIG

        config = DEFAULT_CONFIG.copy()
        config["llm_provider"] = "chutes"
        config["deep_think_llm"] = {model!r}
        config["quick_think_llm"] = {model!r}
        config["checkpoint_enabled"] = True
        config["max_debate_rounds"] = {debate_rounds}
        config["max_risk_discuss_rounds"] = {debate_rounds}
        # Switch news + social to moomoo to exercise the W3.15 vendors;
        # core_stock_apis + technical_indicators + fundamental_data stay on yfinance
        # (moomoo doesn't implement those methods → dispatcher would fall back anyway).
        config["data_vendors"] = dict(config["data_vendors"])
        config["data_vendors"]["news_data"] = "moomoo"
        config["data_vendors"]["social_data"] = "moomoo"

        ta = TradingAgentsGraph(debug=False, config=config)
        state, decision = ta.propagate({ticker!r}, {trade_date!r})

        out = {{
            "ticker": {ticker!r},
            "trade_date": {trade_date!r},
            "model": {model!r},
            "signal": decision,
            "market_report": state.get("market_report", "") or "",
            "sentiment_report": state.get("sentiment_report", "") or "",
            "news_report": state.get("news_report", "") or "",
            "fundamentals_report": state.get("fundamentals_report", "") or "",
            "investment_plan": state.get("investment_plan", "") or "",
            "trader_investment_plan": state.get("trader_investment_plan", "") or "",
            "final_trade_decision": state.get("final_trade_decision", "") or "",
        }}
        print({SENTINEL_BEGIN!r})
        print(json.dumps(out))
        print({SENTINEL_END!r})
    """).strip()


def run_tradingagents_in_container(inner_script: str, tradingagents_dir: str, timeout_sec: int) -> str:
    """Run the inner script inside the TradingAgents container via stdin. Return container stdout."""
    cmd = ["docker", "compose", "run", "--rm", "-T", "--entrypoint", "python", "tradingagents", "-"]
    proc = subprocess.run(
        cmd,
        input=inner_script,
        cwd=tradingagents_dir,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
    )
    if proc.returncode != 0:
        sys.stderr.write(f"[orchestrator] container exited {proc.returncode}\n")
        sys.stderr.write(f"[orchestrator] stderr tail:\n{proc.stderr[-3000:]}\n")
        raise RuntimeError(f"TradingAgents container failed (exit {proc.returncode})")
    return proc.stdout


def extract_decision_json(stdout: str) -> dict:
    begin = stdout.find(SENTINEL_BEGIN)
    end = stdout.find(SENTINEL_END)
    if begin < 0 or end < 0 or end <= begin:
        sys.stderr.write(f"[orchestrator] container stdout (last 3000 chars):\n{stdout[-3000:]}\n")
        raise RuntimeError("decision JSON sentinels not found in container stdout")
    payload = stdout[begin + len(SENTINEL_BEGIN):end].strip()
    return json.loads(payload)


# ---------- Ledger writes ----------
def insert_decision_row(conn: sqlite3.Connection, decision_id: str, decision: dict, prompt_version: str) -> None:
    market_snapshot_json = json.dumps({
        "market_report": decision["market_report"],
        "fundamentals_report": decision["fundamentals_report"],
        "news_report": decision["news_report"],
        "sentiment_report": decision["sentiment_report"],
    })
    order_intent_json = json.dumps({
        "signal": decision["signal"],
        "investment_plan": decision["investment_plan"],
        "trader_investment_plan": decision["trader_investment_plan"],
        "final_trade_decision": decision["final_trade_decision"],
    })
    conn.execute(
        """
        INSERT INTO decisions (
            decision_id, parent_decision_id, decision_kind, ticker,
            decision_timestamp, trade_date, prompt_version, model_version,
            market_snapshot_json, order_intent_json, rationale,
            broker_order_id, outcome_window_close_timestamp
        ) VALUES (?, NULL, 'baseline', ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
        """,
        (
            decision_id,
            decision["ticker"],
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            decision["trade_date"],
            prompt_version,
            decision["model"],
            market_snapshot_json,
            order_intent_json,
            decision["final_trade_decision"][:4000],
        ),
    )


def insert_deferred_metric_rows(conn: sqlite3.Connection, decision_id: str) -> None:
    for metric in METRICS:
        conn.execute(
            """
            INSERT INTO metric_scores (decision_id, metric_name, score, score_label,
                outcome_window_end_timestamp, computed_at, computation_notes)
            VALUES (?, ?, NULL, NULL, NULL, NULL, 'deferred-outcome-window-pending')
            """,
            (decision_id, metric),
        )


def update_decision_broker_order(conn: sqlite3.Connection, decision_id: str, broker_order_id: str) -> None:
    conn.execute(
        "UPDATE decisions SET broker_order_id = ? WHERE decision_id = ?",
        (broker_order_id, decision_id),
    )


# ---------- moomoo paper trade ----------
def normalize_signal(signal: str) -> str | None:
    """Return canonical 5-tier signal in lowercase, or None if unrecognized.

    Tolerant of Kimi-emitted variants ("Strong Buy", "BUY!", "Buy (high conviction)",
    "Overweight (with caveats)") via word-boundary search against the 5 canonical
    tokens. Mirrors score_metrics.extract_signal's normalization rule.
    """
    s = (signal or "").strip().strip("!.,;:()[]{}").lower()
    for c in CANONICAL_SIGNALS:
        if re.search(rf"\b{c}\b", s):
            return c
    return None


def signal_to_side(signal: str) -> str | None:
    """Map 5-tier signal to BUY or None. Returns None for Hold, Underweight, Sell,
    OR unrecognized text. Use normalize_signal() separately to distinguish the
    unrecognized-text case — main() needs that distinction for exit-code routing."""
    canon = normalize_signal(signal)
    if canon in ("buy", "overweight"):
        return "BUY"
    return None  # hold / underweight / sell / unrecognized — v0 short-side skipped


def place_paper_order(project_dir: str, ticker: str, side: str, qty: int = 1,
                      uv_bin: str = DEFAULT_UV_BIN) -> dict:
    code = f"US.{ticker}" if not ticker.startswith(("US.", "HK.", "CN.")) else ticker
    cmd = [
        uv_bin, "run", "python",
        f"{project_dir}/.claude/skills/moomooapi/scripts/trade/place_order.py",
        "--code", code,
        "--side", side,
        "--quantity", str(qty),
        "--order-type", "MARKET",
        "--price", "0",
        "--trd-env", "SIMULATE",
        "--json",
    ]
    proc = subprocess.run(cmd, cwd=project_dir, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        sys.stderr.write(f"[orchestrator] place_order exited {proc.returncode}\n")
        sys.stderr.write(f"[orchestrator] stderr:\n{proc.stderr}\n")
        return {"success": False, "stderr": proc.stderr, "stdout": proc.stdout}
    # moomoo SDK pollutes stdout with OpenQuoteContext connect/disconnect log lines
    # (the same noise pattern score_metrics.py:177 already learned to handle). Scan
    # for the first line starting with `{` rather than naively taking the last line.
    # Contract: rc=0 ⇒ stdout MUST contain a parseable JSON object. Violation = hard
    # failure (return success=False with a parse_error breadcrumb) so the decision row
    # is NOT committed with a NULL broker_order_id — that previous silent path
    # untethered live paper trades from the eval ledger.
    json_line = next(
        (ln for ln in proc.stdout.splitlines() if ln.lstrip().startswith("{")),
        None,
    )
    if json_line is None:
        sys.stderr.write(
            "[orchestrator] place_order rc=0 but stdout has no JSON line; "
            "treating as failure to avoid orphaning the decision row\n"
            f"[orchestrator] stdout:\n{proc.stdout}\n"
        )
        return {
            "success": False,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "parse_error": "no-json-line-in-stdout",
        }
    try:
        parsed = json.loads(json_line)
    except json.JSONDecodeError as e:
        sys.stderr.write(
            f"[orchestrator] place_order rc=0 but JSON line failed to decode: {e}\n"
            f"[orchestrator] stdout:\n{proc.stdout}\n"
        )
        return {
            "success": False,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "parse_error": f"json-decode-failed: {e}",
        }
    return {"success": True, "raw": proc.stdout, "parsed": parsed}


# ---------- Main ----------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("ticker", type=_validate_ticker, help="US ticker, e.g. NVDA or BRK.B")
    parser.add_argument("trade_date", type=_validate_trade_date, help="YYYY-MM-DD")
    parser.add_argument("--ledger", default=DEFAULT_LEDGER_PATH)
    parser.add_argument("--tradingagents-dir", default=DEFAULT_TRADINGAGENTS_DIR)
    parser.add_argument("--project-dir", default=DEFAULT_PROJECT_DIR)
    parser.add_argument("--prompt-version", default=DEFAULT_PROMPT_VERSION)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--debate-rounds", type=int, default=1, help="max_debate_rounds + max_risk_discuss_rounds (default 1)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_CONTAINER_TIMEOUT_SEC)
    parser.add_argument("--dry-run", action="store_true", help="Skip moomoo paper trade; log decision only")
    args = parser.parse_args()

    print(f"[orchestrator] starting prediction cycle: {args.ticker} {args.trade_date}", flush=True)
    print(f"[orchestrator] model={args.model} debate_rounds={args.debate_rounds} dry_run={args.dry_run}", flush=True)

    inner_script = build_inner_script(args.ticker, args.trade_date, args.model, args.debate_rounds)
    print(f"[orchestrator] invoking TradingAgents container (timeout {args.timeout}s) ...", flush=True)

    stdout = run_tradingagents_in_container(inner_script, args.tradingagents_dir, args.timeout)
    decision = extract_decision_json(stdout)
    print(f"[orchestrator] signal: {decision['signal']!r}", flush=True)

    decision_id = f"baseline-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"

    conn = sqlite3.connect(args.ledger)
    conn.execute("PRAGMA foreign_keys = ON;")
    paper_trade_failed = False
    try:
        # Single explicit transaction: decision row + 7 metric_scores rows commit
        # together or neither commits. Closes the orphan-decision window where
        # insert_deferred_metric_rows could partially complete after the decision
        # row was already inserted (autocommit gap).
        with conn:
            insert_decision_row(conn, decision_id, decision, args.prompt_version)
            insert_deferred_metric_rows(conn, decision_id)
        print(f"[orchestrator] inserted decision_id={decision_id} + 7 deferred metric rows", flush=True)

        canonical = normalize_signal(decision["signal"])
        side = "BUY" if canonical in ("buy", "overweight") else None
        if args.dry_run:
            print("[orchestrator] --dry-run: skipping moomoo paper trade", flush=True)
        elif canonical is None:
            # Unrecognized — distinguish from intentional Hold/Sell.
            sys.stderr.write(
                f"[orchestrator] WARNING: signal {decision['signal']!r} did not "
                f"normalize to any of the 5 canonical tiers (buy/overweight/hold/"
                f"underweight/sell). No paper trade fired; decision row recorded.\n"
            )
            paper_trade_failed = True
        elif side is None:
            print(f"[orchestrator] signal {decision['signal']!r} → canonical={canonical!r}; "
                  f"no paper trade fired (Hold or v0-unsupported short side)", flush=True)
        else:
            print(f"[orchestrator] firing moomoo paper trade: {side} 1 share "
                  f"{decision['ticker']} MARKET SIMULATE ...", flush=True)
            result = place_paper_order(args.project_dir, decision["ticker"], side)
            broker_order_id = None
            if result["success"]:
                parsed = result.get("parsed")
                if isinstance(parsed, dict):
                    # Use explicit `is None` check so an order_id of 0 (falsy int)
                    # doesn't get treated as missing — the broker contract is
                    # presence/absence, not truthiness.
                    oid = parsed.get("order_id")
                    if oid is None:
                        oid = parsed.get("orderId")
                    if oid is not None:
                        broker_order_id = str(oid)
                if broker_order_id is not None:
                    update_decision_broker_order(conn, decision_id, broker_order_id)
                    conn.commit()
                    print(f"[orchestrator] order accepted; broker_order_id={broker_order_id}",
                          flush=True)
                else:
                    sys.stderr.write(
                        f"[orchestrator] WARNING: place_order rc=0 + parseable JSON, but "
                        f"no order_id field. Decision row remains UNLINKED to a broker order.\n"
                        f"[orchestrator] raw stdout:\n{result.get('raw', '')}\n"
                    )
                    paper_trade_failed = True
            else:
                sys.stderr.write(
                    "[orchestrator] paper trade FAILED; decision row remains without "
                    f"broker_order_id. parse_error={result.get('parse_error')!r}\n"
                )
                paper_trade_failed = True
    finally:
        conn.close()

    print(f"[orchestrator] DONE — decision_id={decision_id}", flush=True)
    if paper_trade_failed:
        # Exit 3 signals "decision row landed but the broker-link did not".
        # Cron callers can detect this and trigger a reconciliation step rather
        # than treating the run as fully successful.
        print("[orchestrator] EXIT 3: paper trade unlinked or failed", flush=True)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
