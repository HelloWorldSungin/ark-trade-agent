"""Unit tests for scripts/run_prediction_cycle.py.

Top-5 finding #4: place_paper_order must scan for the first `{`-starting line in
moomoo SDK stdout, not naively take the last line. This is the same lesson
score_metrics.py:177 already learned; pre-fix, prediction_cycle.py silently
swallowed JSONDecodeError on rc=0 stdout and committed decision rows with
NULL broker_order_id.
"""
import re
from unittest.mock import patch

import pytest

from run_prediction_cycle import (
    DEFAULT_UV_BIN,
    _validate_ticker,
    _validate_trade_date,
    normalize_signal,
    place_paper_order,
    signal_to_side,
)


class _FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


# ---------------------------------------------------------------------------
# Test #4 — place_paper_order final-line JSON parsing
# ---------------------------------------------------------------------------


def test_place_paper_order_picks_first_brace_line_not_last():
    """moomoo SDK emits OpenQuoteContext connect/disconnect log lines on stdout
    AFTER the JSON payload — the pre-fix `.split('\\n')[-1]` would pick up the
    disconnect line instead of the JSON object and silently return parsed=None.
    """
    stdout = (
        '[OpenQuoteContext] connecting...\n'
        '{"order_id": 1133546, "status": "SUBMITTING"}\n'
        '[OpenQuoteContext] disconnected\n'
    )
    with patch("run_prediction_cycle.subprocess.run",
               return_value=_FakeProc(0, stdout=stdout)):
        result = place_paper_order("/tmp/project", "NVDA", "BUY")
    assert result["success"] is True
    assert result["parsed"] == {"order_id": 1133546, "status": "SUBMITTING"}


def test_place_paper_order_treats_no_json_line_as_hard_failure():
    """If rc=0 but stdout contains no JSON object line, that's a contract violation —
    the previous silent `parsed=None` path committed decision rows with NULL
    broker_order_id, untethering them from the broker."""
    stdout = "[OpenQuoteContext] connect failed: timeout\n"
    with patch("run_prediction_cycle.subprocess.run",
               return_value=_FakeProc(0, stdout=stdout)):
        result = place_paper_order("/tmp/project", "NVDA", "BUY")
    assert result["success"] is False
    assert result.get("parse_error") == "no-json-line-in-stdout"


def test_place_paper_order_treats_malformed_json_as_hard_failure():
    stdout = "[OpenQuoteContext] connecting...\n{ malformed json\n"
    with patch("run_prediction_cycle.subprocess.run",
               return_value=_FakeProc(0, stdout=stdout)):
        result = place_paper_order("/tmp/project", "NVDA", "BUY")
    assert result["success"] is False
    assert "json-decode-failed" in result.get("parse_error", "")


def test_place_paper_order_uses_absolute_uv_bin_by_default():
    """`uv` must be invoked by absolute path; ssh + systemd both skip ~/.bashrc.
    score_metrics.py learned this lesson — prediction_cycle now matches."""
    captured_cmd: list[str] = []

    def _capture(cmd, **_kwargs):
        captured_cmd.extend(cmd)
        return _FakeProc(0, stdout='{"order_id": 1}\n')

    with patch("run_prediction_cycle.subprocess.run", side_effect=_capture):
        place_paper_order("/tmp/project", "NVDA", "BUY")
    assert captured_cmd[0] == DEFAULT_UV_BIN
    assert DEFAULT_UV_BIN.startswith("/")


def test_place_paper_order_normalizes_bare_ticker_to_us_prefix():
    captured_cmd: list[str] = []

    def _capture(cmd, **_kwargs):
        captured_cmd.extend(cmd)
        return _FakeProc(0, stdout='{"order_id": 1}\n')

    with patch("run_prediction_cycle.subprocess.run", side_effect=_capture):
        place_paper_order("/tmp/project", "NVDA", "BUY")
    assert "US.NVDA" in captured_cmd
    assert "US.US.NVDA" not in captured_cmd


def test_place_paper_order_preserves_already_prefixed_ticker():
    captured_cmd: list[str] = []

    def _capture(cmd, **_kwargs):
        captured_cmd.extend(cmd)
        return _FakeProc(0, stdout='{"order_id": 1}\n')

    with patch("run_prediction_cycle.subprocess.run", side_effect=_capture):
        place_paper_order("/tmp/project", "HK.0700", "BUY")
    assert "HK.0700" in captured_cmd
    assert "US.HK.0700" not in captured_cmd


# ---------------------------------------------------------------------------
# Bonus: signal normalization + argv validators (Phase 1+4 fixes)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("Strong Buy", "buy"),
    ("Buy (high conviction)", "buy"),
    ("BUY!", "buy"),
    ("Overweight (with caveats)", "overweight"),
    ("Hold", "hold"),
    ("garbage", None),
])
def test_normalize_signal_tolerates_kimi_variants(raw, expected):
    assert normalize_signal(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    ("Buy", "BUY"),
    ("Overweight", "BUY"),
    ("Hold", None),
    ("Sell", None),
    ("Underweight", None),
    ("garbage", None),
])
def test_signal_to_side_maps_to_buy_or_none(raw, expected):
    assert signal_to_side(raw) == expected


@pytest.mark.parametrize("bad", [
    "nvda",       # wrong case
    "NVDA NVDA",  # whitespace
    "'; rm -rf /",  # shell-injection attempt
    "NVDA!",      # punctuation
    "1234",       # digits
    "",           # empty
])
def test_validate_ticker_rejects_unsafe_input(bad):
    import argparse  # noqa: PLC0415
    with pytest.raises(argparse.ArgumentTypeError):
        _validate_ticker(bad)


@pytest.mark.parametrize("bad", [
    "2026-13-01",  # invalid month
    "2026-05-32",  # invalid day
    "not-a-date",
    "2026/05/13",  # wrong separator
    "26-05-13",    # 2-digit year
])
def test_validate_trade_date_rejects_unsafe_input(bad):
    import argparse  # noqa: PLC0415
    with pytest.raises(argparse.ArgumentTypeError):
        _validate_trade_date(bad)


def test_validate_ticker_accepts_canonical_shapes():
    assert _validate_ticker("NVDA") == "NVDA"
    assert _validate_ticker("BRK.B") == "BRK.B"


def test_validate_trade_date_accepts_iso_date():
    assert _validate_trade_date("2026-05-13") == "2026-05-13"
