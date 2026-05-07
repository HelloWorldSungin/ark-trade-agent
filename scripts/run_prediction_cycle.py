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
import subprocess
import sqlite3
import sys
import textwrap
import uuid
from datetime import datetime, timezone
from pathlib import Path

# ---------- Defaults (LOQ-shaped) ----------
DEFAULT_LEDGER_PATH = os.environ.get("ARK_EVAL_LEDGER_PATH", "/opt/ark-data/eval-ledger.sqlite")
DEFAULT_TRADINGAGENTS_DIR = os.environ.get("ARK_TRADINGAGENTS_DIR", "/opt/tradingagents")
DEFAULT_PROJECT_DIR = os.environ.get("ARK_PROJECT_DIR", "/opt/ark-trade-agent")
DEFAULT_PROMPT_VERSION = "v0.2.4-blessed-2026-05-07"  # matches blessed-baseline vault page
DEFAULT_MODEL = "moonshotai/Kimi-K2.6-TEE"
DEFAULT_CONTAINER_TIMEOUT_SEC = 1500
SENTINEL_BEGIN = "__ARK_DECISION_JSON__"
SENTINEL_END = "__ARK_DECISION_JSON_END__"

METRICS = (
    "thesis_accuracy",
    "next_day_direction",
    "volatility_adjusted_move",
    "max_adverse_excursion",
    "catalyst_correctness",
    "risk_rule_compliance",
    "rationale_trade_match",
)


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
def signal_to_side(signal: str) -> str | None:
    s = (signal or "").strip().lower()
    if s in ("buy", "overweight"):
        return "BUY"
    if s in ("hold",):
        return None
    return None  # underweight / sell skipped for v0 smoke (no existing position)


def place_paper_order(project_dir: str, ticker: str, side: str, qty: int = 1) -> dict:
    code = f"US.{ticker}" if not ticker.startswith(("US.", "HK.", "CN.")) else ticker
    cmd = [
        "uv", "run", "python",
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
    try:
        return {"success": True, "raw": proc.stdout, "parsed": json.loads(proc.stdout.strip().split("\n")[-1])}
    except json.JSONDecodeError:
        return {"success": True, "raw": proc.stdout, "parsed": None}


# ---------- Main ----------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("ticker", help="US ticker, e.g. NVDA")
    parser.add_argument("trade_date", help="YYYY-MM-DD")
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
    try:
        insert_decision_row(conn, decision_id, decision, args.prompt_version)
        insert_deferred_metric_rows(conn, decision_id)
        conn.commit()
        print(f"[orchestrator] inserted decision_id={decision_id} + 7 deferred metric rows", flush=True)

        side = signal_to_side(decision["signal"])
        if args.dry_run:
            print(f"[orchestrator] --dry-run: skipping moomoo paper trade", flush=True)
        elif side is None:
            print(f"[orchestrator] signal {decision['signal']!r} → no paper trade fired (Hold or unsupported sell branch)", flush=True)
        else:
            print(f"[orchestrator] firing moomoo paper trade: {side} 1 share {decision['ticker']} MARKET SIMULATE ...", flush=True)
            result = place_paper_order(args.project_dir, decision["ticker"], side)
            if result["success"]:
                broker_order_id = None
                parsed = result.get("parsed")
                if isinstance(parsed, dict):
                    broker_order_id = str(parsed.get("order_id") or parsed.get("orderId") or "") or None
                if broker_order_id:
                    update_decision_broker_order(conn, decision_id, broker_order_id)
                    conn.commit()
                    print(f"[orchestrator] order accepted; broker_order_id={broker_order_id}", flush=True)
                else:
                    print(f"[orchestrator] order accepted but no order_id in parsed JSON; raw stdout:\n{result['raw']}", flush=True)
            else:
                print(f"[orchestrator] paper trade FAILED; decision row remains without broker_order_id", flush=True)
    finally:
        conn.close()

    print(f"[orchestrator] DONE — decision_id={decision_id}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
