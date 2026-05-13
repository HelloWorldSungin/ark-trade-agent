#!/usr/bin/env python3
"""Initialize the Ark Trade Agent v0 SQLite eval ledger.

Per spec § Build Order step 17 + § Hermes Evaluation Metrics & Shadow Mode.

Schema (v0.1.0, normalized 3 tables):
    decisions       — every TradingAgents prediction cycle (baseline) and every
                      Hermes shadow proposal. Both kinds share the same shape;
                      shadow rows reference baseline rows via parent_decision_id.
    fills           — realized moomoo paper fills, attached to baseline decisions
                      only (shadow decisions are never executed).
    metric_scores   — 7 decision-quality metrics × decisions, one row per
                      (decision_id, metric_name). Lets each metric be filled in
                      as its outcome window closes, instead of forcing the whole
                      decision row to wait for the slowest metric.

This script is idempotent: re-running creates nothing if tables already exist.
The schema_meta table records the version so a future migration can detect
which DDL it's applying onto.

Default ledger path is /opt/ark-data/eval-ledger.sqlite (host bind-mount target,
matches CLAUDE.md ## TradingAgents Configuration). Override via --path or
ARK_EVAL_LEDGER_PATH env var.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from ledger_constants import SCHEMA_VERSION  # canonical version string

DDL = """
-- PRAGMA must run before executescript()'s implicit COMMIT becomes load-bearing.
-- Also: foreign_keys is a per-connection setting (not per-database), so every
-- script opening a Connection MUST re-issue this PRAGMA. See score_metrics.py,
-- run_prediction_cycle.py, run_hermes_proposal.py for the connection-opener pattern.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS decisions (
    decision_id                       TEXT PRIMARY KEY,
    parent_decision_id                TEXT REFERENCES decisions(decision_id),
    decision_kind                     TEXT NOT NULL CHECK (decision_kind IN ('baseline', 'shadow')),
    ticker                            TEXT NOT NULL,
    decision_timestamp                TEXT NOT NULL,
    trade_date                        TEXT NOT NULL,
    prompt_version                    TEXT NOT NULL,
    model_version                     TEXT NOT NULL,
    market_snapshot_json              TEXT,
    order_intent_json                 TEXT NOT NULL,
    rationale                         TEXT,
    broker_order_id                   TEXT,
    outcome_window_close_timestamp    TEXT
);

CREATE INDEX IF NOT EXISTS idx_decisions_ticker_kind_date ON decisions(ticker, decision_kind, trade_date);
CREATE INDEX IF NOT EXISTS idx_decisions_parent ON decisions(parent_decision_id);

CREATE TABLE IF NOT EXISTS fills (
    fill_id                           TEXT PRIMARY KEY,
    decision_id                       TEXT NOT NULL REFERENCES decisions(decision_id),
    fill_timestamp                    TEXT NOT NULL,
    fill_price                        REAL NOT NULL,
    fill_qty                          INTEGER NOT NULL,
    fill_side                         TEXT NOT NULL CHECK (fill_side IN ('BUY', 'SELL')),
    raw_moomoo_response_json          TEXT
);

CREATE INDEX IF NOT EXISTS idx_fills_decision ON fills(decision_id);

CREATE TABLE IF NOT EXISTS metric_scores (
    metric_score_id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id                       TEXT NOT NULL REFERENCES decisions(decision_id),
    metric_name                       TEXT NOT NULL CHECK (metric_name IN (
        'thesis_accuracy',
        'next_day_direction',
        'volatility_adjusted_move',
        'max_adverse_excursion',
        'catalyst_correctness',
        'risk_rule_compliance',
        'rationale_trade_match'
    )),
    score                             REAL,
    score_label                       TEXT,
    outcome_window_end_timestamp      TEXT,
    computed_at                       TEXT,
    computation_notes                 TEXT,
    UNIQUE (decision_id, metric_name)
);

CREATE INDEX IF NOT EXISTS idx_metric_scores_decision ON metric_scores(decision_id);
CREATE INDEX IF NOT EXISTS idx_metric_scores_name ON metric_scores(metric_name, decision_id);

CREATE TABLE IF NOT EXISTS schema_meta (
    key                               TEXT PRIMARY KEY,
    value                             TEXT NOT NULL,
    updated_at                        TEXT NOT NULL
);
"""

META_ROWS = [
    ("schema_version", SCHEMA_VERSION),
    ("description", "Ark Trade Agent v0 eval ledger — decisions/fills/metric_scores"),
    (
        "metrics",
        "thesis_accuracy,next_day_direction,volatility_adjusted_move,"
        "max_adverse_excursion,catalyst_correctness,risk_rule_compliance,"
        "rationale_trade_match",
    ),
    (
        "spec_reference",
        "vault/Specs/openclaw-hermes-trading-agent-v0-spec.md § Hermes Evaluation Metrics & Shadow Mode",
    ),
    (
        "blessed_baseline_reference",
        "vault/Specs/blessed-baseline-tradingagents-prompts-v0.2.4.md",
    ),
]


def init_ledger(db_path: Path) -> dict:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    fresh = not db_path.exists()
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(DDL)  # DDL's first statement enables PRAGMA foreign_keys
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for key, value in META_ROWS:
            conn.execute(
                "INSERT INTO schema_meta(key, value, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, now),
            )
        conn.commit()
        info = {
            "path": str(db_path),
            "fresh": fresh,
            "tables": [
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                )
            ],
            "row_counts": {
                table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("decisions", "fills", "metric_scores", "schema_meta")
            },
            "schema_version": conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0],
        }
    finally:
        conn.close()
    return info


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument(
        "--path",
        default=os.environ.get("ARK_EVAL_LEDGER_PATH", "/opt/ark-data/eval-ledger.sqlite"),
        help="SQLite ledger path (default /opt/ark-data/eval-ledger.sqlite)",
    )
    args = parser.parse_args()

    info = init_ledger(Path(args.path))
    print(f"path: {info['path']}")
    print(f"fresh: {info['fresh']}")
    print(f"schema_version: {info['schema_version']}")
    print(f"tables: {', '.join(info['tables'])}")
    for table, count in info["row_counts"].items():
        print(f"row_count.{table}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
