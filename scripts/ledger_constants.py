"""Single source of truth for eval-ledger constants shared across orchestrators.

A typo in one script's local copy of `METRICS` or `SCHEMA_VERSION` would silently
desync from the SQL CHECK constraint in init_eval_ledger.py's DDL — by the time
the divergence surfaces (an INSERT raises IntegrityError, or a scorer writes
to a phantom metric_name), Hermes-Shadow Delta math is already corrupted.
Pulling these into one module makes the dependency edge explicit and gives
tests a single import to pin.

Consolidated per /ark-code-review --thorough cross-cutting finding: the 7-metric
tuple was previously duplicated across 4 files (init_eval_ledger.py DDL inline,
run_prediction_cycle.py, run_hermes_proposal.py, score_metrics.py).
"""
from __future__ import annotations


# Schema version of the eval ledger. Must match the value written into the
# schema_meta table by init_eval_ledger.py. Bump on incompatible DDL changes.
SCHEMA_VERSION = "0.1.0"


# The 7 decision-quality metrics. Order is canonical: writers iterate this tuple
# when inserting deferred metric_scores rows, and the SQL CHECK constraint in
# init_eval_ledger.py's DDL enumerates the same 7 strings in the same order.
METRICS: tuple[str, ...] = (
    "thesis_accuracy",
    "next_day_direction",
    "volatility_adjusted_move",
    "max_adverse_excursion",
    "catalyst_correctness",
    "risk_rule_compliance",
    "rationale_trade_match",
)


# The 5-tier signal vocabulary in canonical lowercase form. Order matters for
# the prefix-match normalizer in extract_signal()/normalize_signal() — the most
# specific match wins, so we list directional signals before the neutral one.
CANONICAL_SIGNALS: tuple[str, ...] = (
    "buy",
    "overweight",
    "hold",
    "underweight",
    "sell",
)


# Direction sign for each canonical signal: +1 (long), 0 (flat), -1 (short).
# Used by next_day_direction scoring + by run_prediction_cycle's signal_to_side.
SIGNAL_DIRECTION: dict[str, int] = {
    "buy": +1, "overweight": +1,
    "hold": 0,
    "underweight": -1, "sell": -1,
}


# The 12 prompt-bearing TradingAgents agents per vault/Specs/blessed-baseline-
# tradingagents-prompts-v0.2.4.md. Hermes proposes edits at role-level granularity.
BLESSED_ROLES: tuple[str, ...] = (
    "Market Analyst",
    "Fundamentals Analyst",
    "News Analyst",
    "Social Media Analyst",
    "Bull Researcher",
    "Bear Researcher",
    "Research Manager",
    "Trader",
    "Risk Aggressive",
    "Risk Conservative",
    "Risk Neutral",
    "Portfolio Manager",
)
