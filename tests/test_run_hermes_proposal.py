"""Unit tests for scripts/run_hermes_proposal.py.

Top-5 finding #2: parse_hermes_response embedded END-sentinel in shadow_rationale.
Plus apply-gate doctrinal hard floor (the v0 Shadow Mode invariant) and trust-boundary
sanitization (cso-Finding 3 mitigation).
"""
import json

import pytest

from run_hermes_proposal import (
    SCHEMA_VERSION,
    SENTINEL_BEGIN,
    SENTINEL_END,
    _sanitize_untrusted_excerpt,
    can_apply_proposal_safely,
    parse_hermes_response,
)


def _wrap(payload: dict | str) -> str:
    body = json.dumps(payload) if isinstance(payload, dict) else payload
    return f"some preamble\n{SENTINEL_BEGIN}\n{body}\n{SENTINEL_END}\nsome trailer\n"


# ---------------------------------------------------------------------------
# Test #2 — parse_hermes_response embedded END-sentinel
# ---------------------------------------------------------------------------


def test_parse_hermes_response_embedded_end_sentinel_in_rationale():
    """Top-5 finding: the Hermes prompt itself asks Hermes to honor the
    sentinel contract, so the literal __HERMES_PROPOSAL_JSON_END__ string
    can appear inside the shadow_rationale prose. Pre-fix `str.index` used
    first-occurrence and truncated the JSON at the wrong byte. Post-fix
    uses `rindex` so the OUTER END marker wins.
    """
    payload = {
        "proposed_edits": [],
        "shadow_decision": {
            "shadow_signal": "Hold",
            "shadow_rationale": (
                f"the parser is required to find {SENTINEL_END} as the outer "
                f"marker even when it appears in prose like this"
            ),
        },
    }
    stdout = _wrap(payload)
    parsed, _, status = parse_hermes_response(stdout)
    assert status == "ok", status
    assert parsed is not None
    assert parsed["shadow_decision"]["shadow_signal"] == "Hold"


def test_parse_hermes_response_missing_sentinels():
    parsed, _, status = parse_hermes_response("no markers here")
    assert parsed is None
    assert status == "sentinel-not-found"


def test_parse_hermes_response_rejects_null_proposed_edits():
    """`{"proposed_edits": null, ...}` would render as "no edits proposed"
    in the MD — indistinguishable from a successful empty-edit response.
    Reject loudly via schema check."""
    payload = {"proposed_edits": None,
               "shadow_decision": {"shadow_signal": "Hold", "shadow_rationale": "x"}}
    parsed, _, status = parse_hermes_response(_wrap(payload))
    assert parsed is None
    assert "schema-invalid" in status, status
    assert "proposed_edits" in status


def test_parse_hermes_response_rejects_missing_shadow_signal():
    payload = {"proposed_edits": [],
               "shadow_decision": {"shadow_rationale": "missing signal"}}
    parsed, _, status = parse_hermes_response(_wrap(payload))
    assert parsed is None
    assert "schema-invalid" in status
    assert "shadow_signal" in status


def test_parse_hermes_response_accepts_valid_empty_edits():
    payload = {"proposed_edits": [],
               "shadow_decision": {"shadow_signal": "Hold", "shadow_rationale": "steady"}}
    parsed, _, status = parse_hermes_response(_wrap(payload))
    assert status == "ok"
    assert parsed is not None
    assert parsed["proposed_edits"] == []


def test_parse_hermes_response_rejects_top_level_array():
    parsed, _, status = parse_hermes_response(_wrap('[1, 2, 3]'))
    assert parsed is None
    assert "json-not-object" in status, status


# ---------------------------------------------------------------------------
# Apply-gate doctrinal hard floor (v0 Shadow Mode invariant)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ledger_state,replay_passed", [
    # Even with everything green, v0 must refuse.
    ({"sample_size_gate_cleared": True, "scored_outcome_pairs": 100}, True),
    ({"sample_size_gate_cleared": True, "scored_outcome_pairs": 30}, True),
    ({"sample_size_gate_cleared": False, "scored_outcome_pairs": 5}, False),
    ({"sample_size_gate_cleared": True, "scored_outcome_pairs": 50}, False),
])
def test_can_apply_proposal_safely_always_returns_false_in_v0(ledger_state, replay_passed):
    """Pins the Shadow Mode v0 invariant: `auto-apply NEVER`. A future branch
    reorder that lets True through this gate must fail this test loudly."""
    allow, reason = can_apply_proposal_safely(ledger_state, replay_passed)
    assert allow is False
    assert reason  # non-empty explanation


# ---------------------------------------------------------------------------
# Trust-boundary sanitization (cso-Finding 3 mitigation)
# ---------------------------------------------------------------------------


def test_sanitize_strips_sentinel_strings_from_excerpts():
    """An attacker who plants `__HERMES_PROPOSAL_JSON__` text in a moomoo
    news article — which then flows through TradingAgents into the analyst
    rationale — could otherwise corrupt parse_hermes_response."""
    attack = (
        f"normal text {SENTINEL_BEGIN} planted-fake-json {SENTINEL_END} "
        f'and a """ triple quote'
    )
    cleaned = _sanitize_untrusted_excerpt(attack)
    assert SENTINEL_BEGIN not in cleaned
    assert SENTINEL_END not in cleaned
    assert '"""' not in cleaned
    assert "normal text" in cleaned  # legitimate prose survives


# ---------------------------------------------------------------------------
# SCHEMA_VERSION cross-file alignment
# ---------------------------------------------------------------------------


def test_schema_version_matches_init_eval_ledger():
    """init_eval_ledger.py and run_hermes_proposal.py must agree on SCHEMA_VERSION;
    the proposal MD frontmatter is compared against the ledger's schema_meta row."""
    from init_eval_ledger import SCHEMA_VERSION as INIT_VERSION  # noqa: PLC0415
    assert SCHEMA_VERSION == INIT_VERSION, (
        f"SCHEMA_VERSION drift: hermes={SCHEMA_VERSION!r} vs init={INIT_VERSION!r}"
    )
