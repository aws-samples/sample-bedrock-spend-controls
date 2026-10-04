from datetime import datetime, timezone

import pytest

from bedrock_spend_controls.quota_periods import (
    aggregate_daily_rows,
    calendar_window,
    evaluate_limits,
    normalize_thresholds,
    quota_reason,
)


@pytest.mark.parametrize(
    ("period", "instant", "start", "end"),
    [
        (
            "daily",
            "2028-02-29T23:59:59+00:00",
            "2028-02-29T00:00:00+00:00",
            "2028-03-01T00:00:00+00:00",
        ),
        (
            "weekly",
            "2027-01-01T12:00:00+00:00",
            "2026-12-28T00:00:00+00:00",
            "2027-01-04T00:00:00+00:00",
        ),
        (
            "monthly",
            "2028-02-29T12:00:00+00:00",
            "2028-02-01T00:00:00+00:00",
            "2028-03-01T00:00:00+00:00",
        ),
        (
            "monthly",
            "2026-12-31T23:59:59+00:00",
            "2026-12-01T00:00:00+00:00",
            "2027-01-01T00:00:00+00:00",
        ),
    ],
)
def test_calendar_period_boundaries(period, instant, start, end):
    window = calendar_window(period, datetime.fromisoformat(instant))
    assert window.start.isoformat() == start
    assert window.end.isoformat() == end


def test_naive_timestamps_are_interpreted_as_utc():
    window = calendar_window("daily", datetime(2026, 9, 9, 23, 0))
    assert window.start.tzinfo == timezone.utc
    assert window.key == "2026-09-09"


def test_daily_ledger_derives_week_and_month_without_prefixed_rollups():
    now = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)  # Wednesday
    usage = aggregate_daily_rows(
        [
            {"window": "2026-08-31", "cost_micro": 1, "requests": 1},
            {"window": "2026-09-01", "cost_micro": 2, "requests": 1},
            {"window": "2026-09-07", "cost_micro": 4, "requests": 1},
            {"window": "2026-09-09", "cost_micro": 8, "requests": 1},
            # Non-date rows (future rollups or request markers) are ignored.
            {"window": "W#2026-09-07", "cost_micro": 1_000},
        ],
        now,
    )
    assert usage["daily"]["cost_micro"] == 8
    assert usage["weekly"]["cost_micro"] == 12
    assert usage["monthly"]["cost_micro"] == 14
    assert usage["monthly"]["requests"] == 3


def test_simultaneous_limits_report_deterministic_period_and_dimension():
    now = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
    usage = aggregate_daily_rows(
        [
            {
                "window": "2026-09-09",
                "cost_micro": 10,
                "input_tokens": 20,
                "output_tokens": 30,
            }
        ],
        now,
    )
    evaluation = evaluate_limits(
        {
            "daily": {"usd_micro": 10, "input_tokens": 0, "output_tokens": 0},
            "weekly": {"usd_micro": 0, "input_tokens": 20, "output_tokens": 0},
            "monthly": {"usd_micro": 0, "input_tokens": 0, "output_tokens": 30},
        },
        usage,
        now,
    )
    assert [
        (item.period, item.dimension) for item in evaluation.breaches
    ] == [
        ("daily", "usd"),
        ("weekly", "input_tokens"),
        ("monthly", "output_tokens"),
    ]
    assert quota_reason(evaluation) == (
        "auto: daily USD quota exhausted in 2026-09-09"
    )


def test_null_period_and_zero_dimensions_are_not_enforced():
    now = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
    usage = aggregate_daily_rows(
        [{"window": "2026-09-09", "cost_micro": 100}], now
    )
    evaluation = evaluate_limits(
        {
            "daily": {"usd_micro": 0, "input_tokens": 0, "output_tokens": 0},
            "weekly": None,
            "monthly": None,
        },
        usage,
        now,
    )
    assert not evaluation.over_budget
    assert evaluation.maximum_ratio == 0


@pytest.mark.parametrize(
    "entry",
    [
        {"at": float("inf"), "action": "warn"},
        {"at": float("-inf"), "action": "warn"},
        {"at": float("nan"), "action": "warn"},
        {"at_bps": float("inf"), "action": "warn"},
        {"at_bps": float("nan"), "action": "block"},
    ],
)
def test_non_finite_thresholds_are_rejected_as_values(entry):
    """``json.loads`` lets ``Infinity``/``NaN`` through; converting them to
    basis points raised OverflowError/ValueError out of the API (500)."""
    with pytest.raises(ValueError, match=r"thresholds\[0\]\.at"):
        normalize_thresholds([entry])


def test_warn_threshold_reached_exactly_is_reported():
    """57 of 100 against a 57 % threshold: ``0.57 * 10_000`` is 5699.99...
    in floating point, so the ratio comparison missed the crossing. The
    integer comparison the block check uses is exact."""
    now = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
    usage = aggregate_daily_rows(
        [{"window": "2026-09-09", "input_tokens": 57}], now
    )
    evaluation = evaluate_limits(
        {
            "daily": {
                "usd_micro": 0,
                "input_tokens": 100,
                "output_tokens": 0,
                "thresholds": normalize_thresholds(
                    [{"at": 0.57, "action": "warn"}, {"at": 1.0, "action": "block"}]
                ),
            },
            "weekly": None,
            "monthly": None,
        },
        usage,
        now,
    )
    assert [(w.period, w.at_bps) for w in evaluation.warnings] == [("daily", 5700)]
    assert not evaluation.over_budget

    below = evaluate_limits(
        {
            "daily": {
                "usd_micro": 0,
                "input_tokens": 100,
                "output_tokens": 0,
                "thresholds": normalize_thresholds(
                    [{"at": 0.57, "action": "warn"}, {"at": 1.0, "action": "block"}]
                ),
            },
            "weekly": None,
            "monthly": None,
        },
        aggregate_daily_rows([{"window": "2026-09-09", "input_tokens": 56}], now),
        now,
    )
    assert below.warnings == ()
