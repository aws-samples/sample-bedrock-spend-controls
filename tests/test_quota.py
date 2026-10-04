from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.quota import (
    MICRO,
    EmergencyVersionConflict,
    LeaseRateLimited,
    QuotaStore,
    current_window,
    model_ledger_subject,
    validate_user_id,
)


def _put_usage(store: QuotaStore, user_id: str, **values) -> None:
    store._usage.put_item(  # noqa: SLF001 - focused store test
        Item={
            "user_id": user_id,
            "window": current_window(),
            "cost_micro": values.get("cost_micro", 0),
            "input_tokens": values.get("input_tokens", 0),
            "output_tokens": values.get("output_tokens", 0),
            "requests": values.get("requests", 0),
        }
    )


def _daily(usd: float, input_tokens: int, output_tokens: int) -> dict:
    """API-shaped limits with only the daily period enabled."""
    return {
        "daily": {
            "usd": usd,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
        "weekly": None,
        "monthly": None,
    }


def _admin_set_limits(
    store: QuotaStore, user_id: str, limits: dict, *, version: int
) -> None:
    store.update_admin_limits(
        user_id,
        limits,
        reason="test",
        expected_version=version,
        actor="admin",
        auth_method="shared-key",
        idempotency_key=f"{user_id}-limits-{version}",
        request_hash="h",
    )


@pytest.mark.parametrize(
    "user_id",
    ["bob#model#anthropic.claude-haiku", "#bob", "bob#", "a#b"],
)
def test_user_ids_containing_hash_are_rejected(user_id):
    """'#' is the ledger key separator: ``bob#model#<m>`` is exactly where
    bob's per-model ledger lives, so a subject with that identity would
    read and write another subject's rows."""
    assert "#" in model_ledger_subject("bob", "m")
    with pytest.raises(ValueError, match="'#'"):
        validate_user_id(user_id)


@pytest.mark.parametrize(
    "user_id",
    ["tenant/alice", "alice@example.com", "tenant:alice", "arn:aws:sts::1:assumed-role/r/s"],
)
def test_path_email_and_tenant_style_user_ids_stay_valid(user_id):
    assert validate_user_id(user_id) == user_id


def test_get_or_provision_creates_defaults_without_resetting_existing(
    fake_dynamodb,
):
    store = QuotaStore(dynamodb=fake_dynamodb)
    created = store.get_or_provision_user("new", name="New User")
    assert created.name == "New User"
    assert created.daily_usd_micro > 0

    _admin_set_limits(store, "new", _daily(1, 123, 50), version=created.version)
    assert store.get_or_provision_user("new").daily_input_tokens == 123


def test_zero_limits_disable_individual_dimensions(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.put_user("unlimited", "Unlimited", limits=_daily(0, 0, 0))
    user = store.get_user("unlimited")
    _put_usage(
        store,
        "unlimited",
        cost_micro=999 * MICRO,
        input_tokens=999,
        output_tokens=999,
    )
    assert not store.is_over_budget(user)


def test_each_configured_dimension_blocks_at_exact_limit(fake_dynamodb):
    dimensions = [
        {"cost_micro": MICRO},
        {"input_tokens": 100},
        {"output_tokens": 50},
    ]
    for index, usage in enumerate(dimensions):
        store = QuotaStore(dynamodb=fake_dynamodb)
        user_id = f"user-{index}"
        store.put_user(user_id, user_id, limits=_daily(1.0, 100, 50))
        _put_usage(store, user_id, **usage)
        assert store.is_over_budget(store.get_user(user_id))


def test_positive_sub_micro_budget_is_not_unlimited(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.put_user("tiny", "Tiny", limits=_daily(0.0000004, 0, 0))
    assert store.get_user("tiny").daily_usd_micro == 1


def test_usd_limits_round_to_nearest_micro_without_float_truncation(
    fake_dynamodb,
):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.put_user("alice", "Alice", limits=_daily(1.000044, 0, 0))
    assert store.get_user("alice").daily_usd_micro == 1_000_044

    _admin_set_limits(store, "alice", _daily(2.000044, 0, 0), version=1)
    assert store.get_user("alice").daily_usd_micro == 2_000_044


def test_session_mapping_preserves_full_identity_and_has_ttl(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.put_user("auth0|full-subject", "Subject", limits=_daily(1, 10, 10))
    store.record_session("safe-session", "auth0|full-subject")
    assert store.resolve_session("safe-session") == "auth0|full-subject"
    item = fake_dynamodb.Table("users-test").get_item(
        Key={"user_id": "SESSION#safe-session"}
    )["Item"]
    assert item["expires_at"] > int(datetime.now(timezone.utc).timestamp())
    assert [user.user_id for user in store.list_users()] == [
        "auth0|full-subject"
    ]


def test_logical_lease_retry_never_extends_fixed_deadline(fake_dynamodb):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    store = QuotaStore(
        dynamodb=fake_dynamodb,
        lease_seconds=300,
        refresh_overlap_seconds=10,
        refresh_jitter_seconds=5,
        vend_rate_limit_per_minute=6,
        jitter_fn=lambda maximum: maximum,
    )
    store.put_user("alice", "Alice", limits=_daily(1, 10, 10))

    first = store.reserve_lease("alice", "lease-a", now=now)
    retry = store.reserve_lease(
        "alice", "lease-a", now=now + timedelta(seconds=20)
    )

    assert first.created
    assert not retry.created
    assert retry.lease_id == first.lease_id
    assert retry.generation == first.generation == 1
    assert retry.expires_at == first.expires_at == now + timedelta(seconds=300)
    assert retry.refresh_after == now + timedelta(seconds=295)

    # A different lease ID before the refresh window is another process for
    # the same identity: it joins the current lease and cannot extend it.
    joined = store.reserve_lease(
        "alice", "lease-b", now=now + timedelta(seconds=20)
    )
    assert joined.joined
    assert not joined.created
    assert joined.lease_id == first.lease_id
    assert joined.generation == first.generation
    assert joined.expires_at == first.expires_at
    assert joined.refresh_after == first.refresh_after

    replacement = store.reserve_lease(
        "alice", "lease-b", now=first.refresh_after
    )
    assert replacement.created
    assert not replacement.joined
    assert replacement.generation == 2
    assert replacement.expires_at > first.expires_at


def test_second_process_joins_and_both_renew_into_one_generation(
    fake_dynamodb,
):
    """Two backend replicas serving one identity share a single lease."""
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    store = QuotaStore(
        dynamodb=fake_dynamodb,
        lease_seconds=300,
        refresh_overlap_seconds=10,
        refresh_jitter_seconds=0,
        vend_rate_limit_per_minute=6,
        jitter_fn=lambda maximum: 0,
    )
    store.put_user("alice", "Alice", limits=_daily(1, 10, 10))

    pod_a = store.reserve_lease("alice", "pod-a-1", now=now)
    pod_b = store.reserve_lease(
        "alice", "pod-b-1", now=now + timedelta(seconds=30)
    )
    assert pod_b.joined and pod_b.expires_at == pod_a.expires_at

    # Both rotate to fresh IDs when the window opens. The first starts
    # generation 2; the second joins it instead of starting generation 3.
    renew_at = pod_a.refresh_after
    pod_a_next = store.reserve_lease("alice", "pod-a-2", now=renew_at)
    pod_b_next = store.reserve_lease(
        "alice", "pod-b-2", now=renew_at + timedelta(seconds=1)
    )
    assert pod_a_next.created and pod_a_next.generation == 2
    assert pod_b_next.joined and pod_b_next.generation == 2
    assert pod_b_next.expires_at == pod_a_next.expires_at
    assert store.get_active_lease("alice").lease_id == "pod-a-2"


def test_losing_a_concurrent_lease_start_joins_the_winner(fake_dynamodb):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    store = QuotaStore(
        dynamodb=fake_dynamodb,
        lease_seconds=300,
        refresh_overlap_seconds=10,
        refresh_jitter_seconds=0,
        vend_rate_limit_per_minute=6,
        jitter_fn=lambda maximum: 0,
    )
    store.put_user("alice", "Alice", limits=_daily(1, 10, 10))
    table = store._users  # noqa: SLF001 - inject a race at the store boundary
    real_update = table.update_item

    def racing_update(**kwargs):
        # Simulate another replica winning the conditional lease write
        # between this caller's read and its own write.
        if kwargs.get("Key") == {"user_id": "alice"} and "lease_id" in str(
            kwargs.get("UpdateExpression", "")
        ):
            table.update_item = real_update
            store.reserve_lease("alice", "winner", now=now)
        return real_update(**kwargs)

    table.update_item = racing_update
    try:
        loser = store.reserve_lease("alice", "loser", now=now)
    finally:
        table.update_item = real_update

    assert loser.joined
    assert loser.lease_id == "winner"
    assert loser.generation == 1


def test_vend_rate_limit_counts_retries_and_resets_next_minute(fake_dynamodb):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    store = QuotaStore(
        dynamodb=fake_dynamodb,
        lease_seconds=60,
        refresh_overlap_seconds=10,
        refresh_jitter_seconds=0,
        vend_rate_limit_per_minute=2,
        jitter_fn=lambda maximum: 0,
    )
    store.put_user("alice", "Alice", limits=_daily(1, 10, 10))

    store.reserve_lease("alice", "lease-a", now=now)
    store.reserve_lease("alice", "lease-a", now=now + timedelta(seconds=1))
    with pytest.raises(LeaseRateLimited) as error:
        store.reserve_lease(
            "alice", "lease-a", now=now + timedelta(seconds=2)
        )
    assert error.value.retry_after == now + timedelta(minutes=1)

    replacement = store.reserve_lease(
        "alice", "lease-b", now=now + timedelta(minutes=1)
    )
    assert replacement.created


def test_reserved_lease_is_never_rolled_back_after_publish(fake_dynamodb):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    store = QuotaStore(
        dynamodb=fake_dynamodb,
        lease_seconds=60,
        refresh_overlap_seconds=10,
        refresh_jitter_seconds=0,
        vend_rate_limit_per_minute=6,
        jitter_fn=lambda maximum: 0,
    )
    store.put_user("alice", "Alice", limits=_daily(1, 10, 10))
    first = store.reserve_lease("alice", "lease-a", now=now)

    retry = store.reserve_lease(
        "alice", "lease-a", now=now + timedelta(seconds=1)
    )
    assert retry.expires_at == first.expires_at
    joined = store.reserve_lease(
        "alice", "lease-b", now=now + timedelta(seconds=2)
    )
    assert joined.joined and joined.expires_at == first.expires_at
    assert store.get_active_lease("alice").lease_id == "lease-a"


def test_emergency_stop_state_keeps_vending_closed_through_recovery(
    fake_dynamodb,
):
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    store = QuotaStore(dynamodb=fake_dynamodb)
    assert not store.emergency_stop_active()

    activating = store.set_emergency_desired(
        active=True,
        actor="admin@example.com",
        reason="security exercise",
        now=now,
    )
    assert activating["state"] == "activating"
    assert store.emergency_stop_active()
    store.mark_emergency_applied(active=True, now=now + timedelta(seconds=1))
    assert store.get_emergency_state()["state"] == "active"

    recovering = store.set_emergency_desired(
        active=False,
        actor="admin@example.com",
        reason="exercise complete",
        now=now + timedelta(seconds=2),
    )
    assert recovering["state"] == "recovering"
    assert store.emergency_stop_active()
    store.mark_emergency_applied(active=False, now=now + timedelta(seconds=3))
    assert not store.emergency_stop_active()
    assert store.list_users() == []


def test_emergency_desired_write_is_conditional_on_the_observed_generation(
    fake_dynamodb, monkeypatch
):
    """Activate racing recover: the second writer's read is stale, so its
    unconditional put used to overwrite the first. Now it fails closed with
    the state the winner wrote."""
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.set_emergency_desired(
        active=True, actor="operator-a", reason="incident", now=now
    )
    real_read = store.get_emergency_state
    stale = {
        "state": "inactive",
        "desired_active": False,
        "generation": 0,
        "actor": "",
        "reason": "",
    }
    reads = iter([stale])
    monkeypatch.setattr(
        store, "get_emergency_state", lambda: next(reads, None) or real_read()
    )

    with pytest.raises(EmergencyVersionConflict) as conflict:
        store.set_emergency_desired(
            active=False,
            actor="operator-b",
            reason="false alarm",
            now=now + timedelta(seconds=1),
        )
    assert conflict.value.current["generation"] == 1
    assert conflict.value.current["state"] == "activating"
    # The winner's request is intact and vending stays closed.
    state = real_read()
    assert state["actor"] == "operator-a"
    assert state["desired_active"] is True
    assert store.emergency_stop_active()

    # Non-zero generations are guarded the same way (generation = :expected):
    # a reader that observed generation 1 after generation 2 was written.
    store.mark_emergency_applied(active=True, generation=1, now=now)
    store.set_emergency_desired(
        active=False, actor="operator-b", reason="done", now=now + timedelta(seconds=2)
    )
    assert real_read()["generation"] == 2
    reads = iter(
        [{**stale, "generation": 1, "state": "active", "desired_active": True}]
    )
    with pytest.raises(EmergencyVersionConflict) as late:
        store.set_emergency_desired(
            active=True, actor="operator-c", reason="again", now=now + timedelta(seconds=3)
        )
    assert late.value.current["generation"] == 2
    assert real_read()["actor"] == "operator-b"


def test_emergency_state_normalizes_dynamodb_decimals(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store._users.put_item(  # noqa: SLF001 - focused store boundary test
        Item={
            "user_id": "CONFIG#EMERGENCY_STOP",
            "state": "inactive",
            "desired_active": False,
            "generation": Decimal("2"),
            "applied_generation": Decimal("2"),
        }
    )

    state = store.get_emergency_state()

    assert state["generation"] == 2
    assert type(state["generation"]) is int
    assert state["applied_generation"] == 2
    assert type(state["applied_generation"]) is int


def test_existing_daily_rows_feed_weekly_and_monthly_limits(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    now = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)
    store.put_user(
        "calendar-user",
        "Calendar User",
        limits={
            "daily": {"usd": 100, "input_tokens": 0, "output_tokens": 0},
            "weekly": {"usd": 0.000006, "input_tokens": 0, "output_tokens": 0},
            "monthly": {"usd": 0.000020, "input_tokens": 0, "output_tokens": 0},
        },
    )
    for window, cost in (
        ("2026-09-01", 10),
        ("2026-09-07", 4),
        ("2026-09-09", 3),
    ):
        store._usage.put_item(  # noqa: SLF001 - existing ledger rows
            Item={"user_id": "calendar-user", "window": window, "cost_micro": cost}
        )

    user = store.get_user("calendar-user")
    usage = store.get_current_usage("calendar-user", now)
    evaluation = store.evaluate_user_quota(user, now)

    assert usage["daily"]["cost_micro"] == 3
    assert usage["weekly"]["cost_micro"] == 7
    assert usage["monthly"]["cost_micro"] == 17
    assert [(item.period, item.dimension) for item in evaluation.breaches] == [
        ("weekly", "usd")
    ]


def test_period_limit_change_reconciles_automatic_but_not_manual_status(
    fake_dynamodb,
):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.put_user("alice", "Alice", limits=_daily(1, 100, 50))
    store.set_user_status(
        "alice",
        "blocked",
        "auto: daily USD quota exhausted in 2026-09-09",
        origin="automatic",
    )
    automatic = store.get_user("alice")
    status, _, origin = store.status_after_limit_change(
        automatic, _daily(1, 100, 50)
    )
    assert (status, origin) == ("active", "automatic")

    store.set_user_status("alice", "blocked", "security review", origin="admin")
    manual = store.get_user("alice")
    assert store.status_after_limit_change(manual, _daily(1, 100, 50)) == (
        "blocked",
        "security review",
        "admin",
    )


def test_auto_block_reactivates_only_when_current_window_is_under_quota(
    fake_dynamodb,
):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.put_user("alice", "Alice", limits=_daily(1.0, 100, 50))
    store.set_user_status("alice", "blocked", "auto: quota exhausted yesterday", origin="automatic")

    refreshed = store.refresh_auto_status(store.get_user("alice"))
    assert refreshed.active

    store.set_user_status("alice", "blocked", "auto: quota exhausted today", origin="automatic")
    _put_usage(store, "alice", cost_micro=MICRO)
    still_blocked = store.refresh_auto_status(store.get_user("alice"))
    assert not still_blocked.active


def test_manual_block_is_never_auto_reactivated(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.put_user("alice", "Alice", limits=_daily(1.0, 100, 50))
    store.set_user_status("alice", "blocked", "admin API", origin="admin")
    assert not store.refresh_auto_status(store.get_user("alice")).active
    event = fake_dynamodb.Table("users-test").get_item(
        Key={"user_id": "REVOCATION#alice"}
    )["Item"]
    assert event["desired_status"] == "blocked"
    assert store.list_users()[0].user_id == "alice"


def test_daily_windows_are_isolated(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store._usage.put_item(  # noqa: SLF001
        Item={
            "user_id": "alice",
            "window": "1999-01-01",
            "cost_micro": 10,
            "input_tokens": 20,
            "output_tokens": 30,
            "requests": 1,
        }
    )
    assert store.get_window_usage("alice")["requests"] == 0
    assert store.get_window_usage("alice", "1999-01-01")["input_tokens"] == 20


def test_user_pagination_excludes_session_rows(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    for index in range(3):
        store.put_user(f"u{index}", f"U{index}", limits=_daily(1, 10, 10))
    store.record_session("session", "u0")

    seen: set[str] = set()
    cursor = None
    while True:
        users, cursor = store.list_users_page(limit=2, cursor=cursor)
        seen.update(user.user_id for user in users)
        if not cursor:
            break
    assert seen == {"u0", "u1", "u2"}


def test_concurrent_admin_create_wins_over_auto_provision(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    table = fake_dynamodb.Table("users-test")
    original_put = table.put_item
    raced = False

    def put_with_admin_winner(**kwargs):
        nonlocal raced
        item = kwargs["Item"]
        if item.get("user_id") == "race" and not raced:
            raced = True
            original_put(
                Item={
                    "user_id": "race",
                    "name": "Admin Winner",
                    "status": "blocked",
                    "status_reason": "created concurrently",
                    "daily_usd_micro": 9 * MICRO,
                    "daily_input_tokens": 900,
                    "daily_output_tokens": 90,
                    "version": 1,
                    "created_at": "2030-01-01T00:00:00+00:00",
                    "updated_at": "2030-01-01T00:00:00+00:00",
                    "status_origin": "admin",
                }
            )
        return original_put(**kwargs)

    table.put_item = put_with_admin_winner

    winner = store.get_or_provision_user("race", name="Auto Default")

    assert winner.name == "Admin Winner"
    assert winner.status == "blocked"
    assert winner.daily_usd_micro == 9 * MICRO
    assert winner.status_origin == "admin"


def test_automatic_status_advances_version_but_bookkeeping_does_not(
    fake_dynamodb,
):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store._users.put_item(  # noqa: SLF001 - bare row without a version
        Item={
            "user_id": "minimal",
            "name": "Minimal",
            "status": "active",
            "daily_usd_micro": MICRO,
            "daily_input_tokens": 100,
            "daily_output_tokens": 50,
        }
    )

    store.record_session("minimal-session", "minimal")
    unversioned = store.get_user("minimal")
    assert unversioned.version == 0
    assert unversioned.status_origin == "admin"  # missing attribute

    store.set_user_status(
        "minimal", "blocked", "auto: quota exhausted", origin="automatic"
    )
    updated = store.get_user("minimal")
    assert updated.version == 1
    assert updated.status_origin == "automatic"
    assert updated.updated_at

    store.record_session("minimal-session-2", "minimal")
    assert store.get_user("minimal").version == 1


def test_set_user_status_requires_a_known_origin(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.put_user("alice", "Alice", limits=_daily(1, 100, 50))
    with pytest.raises(TypeError):
        store.set_user_status("alice", "blocked", "auto: no origin")  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="origin"):
        store.set_user_status("alice", "blocked", "x", origin="legacy")
    assert store.get_user("alice").active


def test_stale_automatic_refresh_cannot_override_newer_admin_status(
    fake_dynamodb,
):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.put_user("alice", "Alice", limits=_daily(1, 100, 50))
    store.set_user_status("alice", "blocked", "auto: old automatic block", origin="automatic")
    stale_automatic = store.get_user("alice")
    assert stale_automatic.status_origin == "automatic"

    store.update_admin_status(
        "alice",
        "blocked",
        "auto: operator-authored reason",
        expected_version=stale_automatic.version,
        actor="admin-shared-key",
        auth_method="shared-key",
        idempotency_key="manual-wins",
        request_hash="manual-wins-hash",
    )

    refreshed = store.refresh_auto_status(stale_automatic)

    assert not refreshed.active
    assert refreshed.status_origin == "admin"
    assert refreshed.status_reason == "auto: operator-authored reason"
    assert refreshed.version == stale_automatic.version + 1


class _WireShortCircuit(Exception):
    """Raised by the before-send hook after capturing the signed request."""


# ---------------------------------------------------------------------------
# Thresholds and rate limits: storage round-trip and evaluation
# ---------------------------------------------------------------------------


def test_thresholds_round_trip_through_storage_and_snapshot(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.put_user(
        "alice",
        "Alice",
        limits={
            "daily": {
                "usd": 10,
                "input_tokens": 0,
                "output_tokens": 0,
                "thresholds": [
                    {"at": 0.5, "action": "warn"},
                    {"at": 0.8, "action": "warn"},
                    {"at": 1.5, "action": "block"},
                ],
            },
            "weekly": {
                "usd": 50,
                "input_tokens": 0,
                "output_tokens": 0,
                "thresholds": [{"at": 1.0, "action": "warn"}],  # alert-only
            },
            "monthly": None,
            "rate": {"rpm": 30, "tpm": 0},
        },
    )
    user = store.get_user("alice")
    assert user.daily_thresholds == (
        {"at_bps": 5000, "action": "warn"},
        {"at_bps": 8000, "action": "warn"},
        {"at_bps": 15000, "action": "block"},
    )
    assert user.weekly_thresholds == ({"at_bps": 10000, "action": "warn"},)
    assert user.monthly_thresholds is None  # disabled period stores []
    assert user.rpm == 30 and user.tpm == 0 and user.rate_limited

    # Audit snapshot -> user keeps every threshold and the rate limits.
    snapshot = QuotaStore._user_snapshot(user)
    assert snapshot["limits"]["daily"]["thresholds"][2] == {
        "at_bps": 15000, "action": "block",
    }
    assert snapshot["rate"] == {"rpm": 30, "tpm": 0}
    restored = QuotaStore._snapshot_to_user(snapshot)
    assert restored.daily_thresholds == user.daily_thresholds
    assert restored.rpm == 30


def test_row_without_thresholds_resolves_to_deployment_default(
    fake_dynamodb, monkeypatch
):
    from dataclasses import replace

    from app import quota as quota_module

    monkeypatch.setattr(
        quota_module,
        "settings",
        replace(quota_module.settings, warn_threshold=0.75),
    )
    store = QuotaStore(dynamodb=fake_dynamodb)
    store._users.put_item(  # noqa: SLF001 - row without a thresholds list
        Item={
            "user_id": "plain",
            "name": "Plain",
            "status": "active",
            "daily_usd_micro": MICRO,
            "daily_input_tokens": 100,
            "daily_output_tokens": 50,
        }
    )
    user = store.get_user("plain")
    assert user.daily_thresholds is None
    assert user.period_limits["daily"]["thresholds"] == [
        {"at_bps": 7500, "action": "warn"},
        {"at_bps": 10000, "action": "block"},
    ]
    assert not user.rate_limited


def test_alert_only_period_is_never_over_budget_at_vend(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.put_user(
        "alice",
        "Alice",
        limits={
            "daily": {
                "usd": 1,
                "input_tokens": 0,
                "output_tokens": 0,
                "thresholds": [{"at": 1.0, "action": "warn"}],
            },
            "weekly": None,
            "monthly": None,
        },
    )
    _put_usage(store, "alice", cost_micro=50 * MICRO)  # 5000 %
    user = store.get_user("alice")
    evaluation = store.evaluate_user_quota(user)
    assert not evaluation.over_budget
    assert [w.at_bps for w in evaluation.warnings] == [10000]
    assert evaluation.ratios["daily.usd"] == 50.0


def test_block_threshold_above_limit_blocks_only_when_reached(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.put_user(
        "alice",
        "Alice",
        limits={
            "daily": {
                "usd": 1,
                "input_tokens": 0,
                "output_tokens": 0,
                "thresholds": [{"at": 1.2, "action": "block"}],
            },
            "weekly": None,
            "monthly": None,
        },
    )
    _put_usage(store, "alice", cost_micro=int(1.19 * MICRO))
    assert not store.is_over_budget(store.get_user("alice"))
    _put_usage(store, "alice", cost_micro=int(1.2 * MICRO))
    evaluation = store.evaluate_user_quota(store.get_user("alice"))
    assert evaluation.over_budget
    assert evaluation.breaches[0].at_bps == 12000


def test_rate_limit_evaluation_reads_current_minute_counter(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.put_user(
        "alice",
        "Alice",
        limits={
            "daily": {"usd": 100, "input_tokens": 0, "output_tokens": 0},
            "weekly": None,
            "monthly": None,
            "rate": {"rpm": 3, "tpm": 0},
        },
    )
    now = datetime(2030, 1, 1, 10, 30, 15, tzinfo=timezone.utc)
    store._usage.put_item(  # noqa: SLF001 - what the processor writes
        Item={"user_id": "RATE#alice", "window": "2030-01-01T10:30",
              "requests": 3, "tokens": 900}
    )
    user = store.get_user("alice")
    blocked = store.evaluate_user_quota(user, now)
    assert blocked.over_budget
    assert blocked.breaches[0].period == "minute"
    assert blocked.breaches[0].dimension == "rpm"
    from app.quota import quota_reason
    assert quota_reason(blocked) == (
        "auto: rpm rate limit reached in minute 2030-01-01T10:30"
    )
    # Next minute: no counter row, under quota -> automatic block would lift.
    assert not store.evaluate_user_quota(
        user, now + timedelta(minutes=1)
    ).over_budget


def test_limit_change_can_disable_rate_limits_with_null(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.put_user(
        "alice", "Alice",
        limits={
            "daily": {"usd": 1, "input_tokens": 0, "output_tokens": 0},
            "weekly": None, "monthly": None, "rate": {"rpm": 5, "tpm": 5},
        },
    )
    assert store.get_user("alice").rate_limited
    result = store.update_admin_limits(
        "alice",
        {
            "daily": {"usd": 1, "input_tokens": 0, "output_tokens": 0},
            "weekly": None,
            "monthly": None,
            "rate": None,
        },
        reason="drop rate limits",
        expected_version=1,
        actor="admin",
        auth_method="shared-key",
        idempotency_key="drop-rate",
        request_hash="h",
    )
    assert not result.user.rate_limited
    assert store.get_user("alice").rpm == 0


def test_transaction_client_sends_typed_values_unmodified(monkeypatch):
    """Transactions must go through a genuine low-level DynamoDB client.

    A boto3 *resource* meta client carries the document-interface transform,
    which re-serializes TypeSerializer output into nested maps ({"S": ...}
    becomes {"M": {"S": {"S": ...}}}). DynamoDB then rejects the item key
    with a schema ValidationError and every admin write surfaces as a 503.
    The in-memory fake models a low-level client, so only a wire-shape
    assertion against the real boto3 client can catch this regression.
    """
    import json as jsonlib

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")

    store = QuotaStore()  # real boto3 resource + transaction client
    captured: dict = {}

    def capture(request, **_kwargs):
        captured["body"] = request.body
        raise _WireShortCircuit()

    store._client.meta.events.register_first(  # noqa: SLF001
        "before-send.dynamodb.TransactWriteItems", capture
    )
    typed_item = store._serialize({"user_id": "alice", "version": 1})  # noqa: SLF001
    with pytest.raises(_WireShortCircuit):
        store._client.transact_write_items(  # noqa: SLF001
            TransactItems=[{"Put": {"TableName": "probe", "Item": typed_item}}]
        )

    sent = jsonlib.loads(captured["body"])["TransactItems"][0]["Put"]["Item"]
    assert sent["user_id"] == {"S": "alice"}
    assert sent["version"] == {"N": "1"}


# ---------------------------------------------------------------------------
# Per-model budgets: derivation and admin mutation
# ---------------------------------------------------------------------------


def test_model_scoped_weekly_and_monthly_derive_from_model_daily_rows(
    fake_dynamodb,
):
    store = QuotaStore(dynamodb=fake_dynamodb)
    now = datetime(2026, 9, 9, 12, tzinfo=timezone.utc)  # Wednesday
    store.put_user(
        "alice", "Alice",
        limits={"daily": {"usd": 1000, "input_tokens": 0, "output_tokens": 0},
                "weekly": None, "monthly": None},
    )
    store.update_admin_model_budget(
        "alice", "opus",
        {
            "daily": None,
            "weekly": {"usd": 0.000006, "input_tokens": 0, "output_tokens": 0},
            "monthly": {"usd": 0.000020, "input_tokens": 0, "output_tokens": 0},
        },
        reason="cap opus per week/month",
        expected_version=1, actor="admin", auth_method="shared-key",
        idempotency_key="opus-budget", request_hash="h",
    )
    # Per-model daily rows written by the processor; the subject rows carry
    # more (other models) and must NOT be what the model budget sees.
    for window, cost in (("2026-09-01", 10), ("2026-09-07", 4), ("2026-09-09", 3)):
        store._usage.put_item(  # noqa: SLF001
            Item={"user_id": "alice#model#opus", "window": window, "cost_micro": cost}
        )
        store._usage.put_item(  # noqa: SLF001
            Item={"user_id": "alice", "window": window, "cost_micro": cost * 100}
        )

    usage = store.get_model_usage("alice", "opus", now)
    assert usage["daily"]["cost_micro"] == 3
    assert usage["weekly"]["cost_micro"] == 7
    assert usage["monthly"]["cost_micro"] == 17

    user = store.get_user("alice")
    evaluation = store.evaluate_user_quota(user, now)
    assert [(b.model_id, b.period, b.dimension) for b in evaluation.breaches] == [
        ("opus", "weekly", "usd")
    ]
    assert evaluation.ratios["model.opus.monthly.usd"] == 17 / 20
    from app.quota import quota_reason
    assert quota_reason(evaluation) == (
        "auto: weekly USD quota exhausted for model opus in 2026-09-07"
    )


def test_model_budget_round_trips_through_snapshot(fake_dynamodb):
    store = QuotaStore(dynamodb=fake_dynamodb)
    store.put_user("alice", "Alice", limits=_daily(1, 100, 50))
    result = store.update_admin_model_budget(
        "alice", "opus",
        {"daily": {"usd": 2, "input_tokens": 0, "output_tokens": 0,
                   "thresholds": [{"at": 1.0, "action": "warn"}]},
         "weekly": None, "monthly": None},
        reason="soft cap", expected_version=1, actor="admin",
        auth_method="shared-key", idempotency_key="k1", request_hash="h1",
    )
    assert result.user.version == 2
    assert result.user.model_budget_limits["opus"]["daily"]["usd_micro"] == 2 * MICRO
    assert result.user.model_budget_limits["opus"]["daily"]["thresholds"] == [
        {"at_bps": 10000, "action": "warn"}
    ]
    snapshot = QuotaStore._user_snapshot(result.user)
    restored = QuotaStore._snapshot_to_user(snapshot)
    assert restored.model_budget_limits == result.user.model_budget_limits

    # Replay with the same key/hash returns the same result without a write.
    replay = store.update_admin_model_budget(
        "alice", "opus", {"daily": {"usd": 9, "input_tokens": 0, "output_tokens": 0},
                          "weekly": None, "monthly": None},
        reason="ignored", expected_version=2, actor="admin",
        auth_method="shared-key", idempotency_key="k1", request_hash="h1",
    )
    assert replay.replayed and replay.user.version == 2

    # Removing the only budget drops the attribute entirely.
    removed = store.update_admin_model_budget(
        "alice", "opus", None, reason="done", expected_version=2, actor="admin",
        auth_method="shared-key", idempotency_key="k2", request_hash="h2",
    )
    assert removed.user.model_budgets is None
    assert "model_budgets" not in store._users.get_item(  # noqa: SLF001
        Key={"user_id": "alice"}
    )["Item"]
    with pytest.raises(KeyError):
        store.update_admin_model_budget(
            "alice", "opus", None, reason="again", expected_version=3,
            actor="admin", auth_method="shared-key",
            idempotency_key="k3", request_hash="h3",
        )
