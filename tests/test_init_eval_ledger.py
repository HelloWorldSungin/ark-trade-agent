"""Unit tests for scripts/init_eval_ledger.py.

Top-5 finding #5: Re-init on a populated ledger MUST preserve existing rows.
The biggest catastrophic-failure-mode in the stack is "user runs the init
script, eval ledger gets wiped". Currently nothing pins the `CREATE TABLE IF
NOT EXISTS` discipline at the test layer — one character DDL typo in a future
PR could replace it with `CREATE TABLE` and silently destroy the working
sample of decision/outcome pairs that the Hermes-Shadow Delta depends on.

Also pins the CHECK constraints on `decision_kind` and `metric_name` —
these are the SQL-layer guard against typo'd kind/metric strings drifting
from the canonical 7-metric vocabulary.
"""
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from init_eval_ledger import SCHEMA_VERSION, init_ledger


@pytest.fixture
def populated_ledger(tmp_path: Path):
    """Fresh ledger with 1 baseline decision + 7 metric_scores + 1 fill."""
    db_path = tmp_path / "test-ledger.sqlite"
    init_ledger(db_path)

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    decision_id = f"baseline-{uuid.uuid4().hex[:8]}"

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """INSERT INTO decisions(decision_id, parent_decision_id, decision_kind,
           ticker, decision_timestamp, trade_date, prompt_version, model_version,
           market_snapshot_json, order_intent_json, rationale, broker_order_id,
           outcome_window_close_timestamp)
           VALUES (?, NULL, 'baseline', 'NVDA', ?, '2026-05-13',
                   'v0.2.4-test', 'kimi-test', '{}', '{"signal":"Buy"}', 'r', NULL, NULL)""",
        (decision_id, now),
    )
    for metric in (
        "thesis_accuracy", "next_day_direction", "volatility_adjusted_move",
        "max_adverse_excursion", "catalyst_correctness", "risk_rule_compliance",
        "rationale_trade_match",
    ):
        conn.execute(
            """INSERT INTO metric_scores(decision_id, metric_name, score, score_label,
               outcome_window_end_timestamp, computed_at, computation_notes)
               VALUES (?, ?, NULL, NULL, NULL, NULL, 'test-deferred')""",
            (decision_id, metric),
        )
    conn.execute(
        """INSERT INTO fills(fill_id, decision_id, fill_timestamp, fill_price, fill_qty,
           fill_side, raw_moomoo_response_json)
           VALUES (?, ?, ?, 207.83, 1, 'BUY', '{}')""",
        (f"fill-{uuid.uuid4().hex[:8]}", decision_id, now),
    )
    conn.commit()
    conn.close()
    return db_path


# ---------------------------------------------------------------------------
# Test #5 — re-init preserves rows
# ---------------------------------------------------------------------------


def test_init_ledger_reinit_preserves_decisions_metrics_fills(populated_ledger: Path):
    """CREATE TABLE IF NOT EXISTS is the only thing standing between a re-run
    of the init script and a wiped ledger. Pin it explicitly."""
    info = init_ledger(populated_ledger)
    assert info["fresh"] is False
    assert info["row_counts"]["decisions"] == 1
    assert info["row_counts"]["metric_scores"] == 7
    assert info["row_counts"]["fills"] == 1


def test_init_ledger_idempotent_schema_meta_updates_timestamp_only(populated_ledger: Path):
    """schema_meta rows use ON CONFLICT DO UPDATE — keys stay the same, only
    the updated_at field gets refreshed."""
    conn = sqlite3.connect(populated_ledger)
    before = dict(conn.execute("SELECT key, value FROM schema_meta").fetchall())
    conn.close()
    init_ledger(populated_ledger)
    conn = sqlite3.connect(populated_ledger)
    after = dict(conn.execute("SELECT key, value FROM schema_meta").fetchall())
    conn.close()
    assert before == after  # keys + values stable across re-init
    assert after["schema_version"] == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# CHECK-constraint pins
# ---------------------------------------------------------------------------


def test_metric_name_check_rejects_typos(tmp_path: Path):
    """metric_scores.metric_name CHECK constraint protects against typo'd
    metric names drifting from the canonical 7-metric vocabulary."""
    db_path = tmp_path / "test.sqlite"
    init_ledger(db_path)
    conn = sqlite3.connect(db_path)
    # First insert a valid decision row so the FK target exists
    conn.execute(
        """INSERT INTO decisions(decision_id, parent_decision_id, decision_kind,
           ticker, decision_timestamp, trade_date, prompt_version, model_version,
           order_intent_json) VALUES ('d', NULL, 'baseline', 'NVDA',
           '2026-05-13T00:00:00+00:00', '2026-05-13', 'v', 'm', '{}')"""
    )
    conn.commit()
    # Hyphenated variant (instead of underscored) must be rejected
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO metric_scores(decision_id, metric_name, score)
               VALUES ('d', 'thesis-accuracy', 1.0)"""
        )
    conn.close()


def test_decision_kind_check_rejects_unknown_kind(tmp_path: Path):
    """decisions.decision_kind CHECK pins the 2-kind taxonomy ('baseline', 'shadow').
    A future feature wanting a third kind must explicitly amend the DDL."""
    db_path = tmp_path / "test.sqlite"
    init_ledger(db_path)
    conn = sqlite3.connect(db_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO decisions(decision_id, parent_decision_id, decision_kind,
               ticker, decision_timestamp, trade_date, prompt_version, model_version,
               order_intent_json) VALUES ('d2', NULL, 'live', 'NVDA',
               '2026-05-13T00:00:00+00:00', '2026-05-13', 'v', 'm', '{}')"""
        )
    conn.close()


def test_foreign_keys_enforced_on_each_connection(tmp_path: Path):
    """PRAGMA foreign_keys is per-connection — every script opening a Connection
    must re-issue it. Verifies the PRAGMA actually rejects orphan fill inserts
    when enabled."""
    db_path = tmp_path / "test.sqlite"
    init_ledger(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO fills(fill_id, decision_id, fill_timestamp, fill_price,
               fill_qty, fill_side) VALUES
               ('orphan', 'nonexistent-decision', '2026-05-13T00:00:00+00:00',
                100.0, 1, 'BUY')"""
        )
    conn.close()
