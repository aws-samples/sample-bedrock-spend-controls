"""Control-plane API tests for the runtime-only credential broker."""

import json
import os
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import jwt as pyjwt
import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

import app.main as gateway
from app.broker import BrokerError, VendedCredentials, session_name_for
from app.quota import MICRO, QuotaStore, current_window

SECRET = os.environ["JWT_SHARED_SECRET"]  # per-session HS256 key from conftest
ADMIN = {"Authorization": "Bearer admin-secret"}
EMERGENCY = {"X-Quota-Emergency-Key": "emergency-secret"}
# What every period reports when no thresholds list has been configured.
DEFAULT_THRESHOLDS = [
    {"at": 0.8, "action": "warn"},
    {"at": 1.0, "action": "block"},
]


def make_jwt(sub: str, **extra) -> str:
    return pyjwt.encode(
        {"sub": sub, "exp": int(time.time()) + 3600, **extra},
        SECRET,
        algorithm="HS256",
    )


class FakeBroker:
    def __init__(self):
        self.users: list[str] = []
        self.claims: list[dict] = []
        self.permission_deadlines = []

    def vend(self, identity, *, permission_deadline=None):
        self.users.append(identity.user_id)
        self.claims.append(identity.claims)
        self.permission_deadlines.append(permission_deadline)
        effective_expiration = (
            permission_deadline.isoformat()
            if permission_deadline is not None
            else "2026-08-18T12:15:00+00:00"
        )
        return VendedCredentials(
            access_key_id="ASIAFAKE",
            secret_access_key="secret",  # nosec B106  # fake STS response
            session_token="token",
            expiration=effective_expiration,
            sts_expiration="2026-08-18T12:15:00+00:00",
            user_id=identity.user_id,
            session_name=session_name_for(identity.user_id),
        )


class FakeCloudWatch:
    def __init__(self, *, results=None, alarms=None, metrics=None,
                 error=None):
        self.results = results or []
        self.alarms = alarms or []
        self.metrics = metrics or []
        self.error = error
        self.metric_calls = []
        self.alarm_calls = []
        self.list_calls = []

    def get_metric_data(self, **kwargs):
        self.metric_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"MetricDataResults": self.results}

    def describe_alarms(self, **kwargs):
        self.alarm_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return {"MetricAlarms": self.alarms}

    def list_metrics(self, **kwargs):
        self.list_calls.append(kwargs)
        if self.error is not None:
            raise self.error
        wanted = {
            dimension["Name"] for dimension in kwargs.get("Dimensions", [])
        }
        matches = [
            metric for metric in self.metrics
            if wanted <= {
                dimension["Name"]
                for dimension in metric.get("Dimensions", [])
            }
        ]
        return {"Metrics": matches}


@pytest.fixture
def client(fake_dynamodb, monkeypatch):
    store = QuotaStore(dynamodb=fake_dynamodb)
    fake_broker = FakeBroker()
    monkeypatch.setattr(gateway, "_store", store)
    monkeypatch.setattr(gateway, "_broker", fake_broker)
    monkeypatch.setattr(gateway, "_admin_key", "admin-secret")
    monkeypatch.setattr(gateway, "_emergency_key", "emergency-secret")
    monkeypatch.setattr(gateway, "_cloudwatch", None)
    return TestClient(gateway.app), store, fake_broker


def _vend(api, token: str):
    return api.post(
        "/v1/credentials",
        headers={"Authorization": f"Bearer {token}"},
    )


def _limits(
    usd: float = 1,
    input_tokens: int = 100,
    output_tokens: int = 50,
    *,
    weekly: dict | None = None,
    monthly: dict | None = None,
) -> dict:
    return {
        "limits": {
            "daily": {
                "usd": usd,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            },
            "weekly": weekly,
            "monthly": monthly,
        }
    }


def _seed_user(
    store: QuotaStore,
    user_id: str,
    name: str,
    usd: float = 1,
    input_tokens: int = 100,
    output_tokens: int = 50,
) -> None:
    """Create an active row with a daily-only budget."""
    store.put_user(
        user_id, name, limits=_limits(usd, input_tokens, output_tokens)["limits"]
    )


def test_health_describes_runtime_only_event_driven_architecture(client):
    api, _, _ = client
    assert api.get("/healthz").json() == {
        "status": "ok",
        "inference_endpoint": "bedrock-runtime",
        "metering": "cloudwatch-logs-subscription",
    }
    assert api.post("/v1/responses").status_code == 404
    assert api.post("/anthropic/v1/messages").status_code == 404


def test_vend_requires_valid_jwt_and_auto_provisions(client):
    api, store, broker = client
    assert api.post("/v1/credentials").status_code == 401
    assert _vend(api, "not-a-jwt").status_code == 401

    response = _vend(api, make_jwt("alice", email="alice@example.com"))
    assert response.status_code == 200
    assert response.json()["endpoint"].startswith(
        "https://bedrock-runtime."
    )
    assert response.json()["user_id"] == "alice"
    assert response.headers["x-quota-enabled-periods"] == "daily"
    assert broker.users == ["alice"]
    assert broker.claims[0]["email"] == "alice@example.com"
    assert "exp" in broker.claims[0]
    assert store.get_user("alice").name == "alice@example.com"
    assert store.resolve_session(session_name_for("alice")) == "alice"


def test_reserved_internal_identity_prefixes_are_rejected(client):
    api, store, broker = client

    response = _vend(api, make_jwt("CONFIG#EMERGENCY_STOP"))

    assert response.status_code == 401
    assert "reserved internal prefix" in response.json()["error"]["message"]
    assert not store.emergency_stop_active()
    assert broker.users == []

    created = api.post(
        "/admin/users",
        json={
            "user_id": "REVOCATION#alice",
            **_limits(1, 10, 10),
        },
        headers=ADMIN,
    )
    assert created.status_code == 400


def test_dedicated_user_header_coexists_with_sigv4(client):
    api, _, _ = client
    response = api.post(
        "/v1/credentials",
        headers={
            "Authorization": "AWS4-HMAC-SHA256 Credential=example",
            "X-Quota-User-Token": make_jwt("alice"),
        },
    )
    assert response.status_code == 200


def test_auto_provision_can_be_disabled(client, monkeypatch):
    import app.auth as auth_module
    from app.config import Settings

    api, _, _ = client
    monkeypatch.setenv("AUTO_PROVISION_USERS", "false")
    fresh = Settings()
    monkeypatch.setattr(gateway, "settings", fresh)
    monkeypatch.setattr(auth_module, "settings", fresh)
    assert _vend(api, make_jwt("unknown")).status_code == 401


def test_quota_identity_can_be_a_tenant_claim(client, monkeypatch):
    import app.auth as auth_module
    from app.config import Settings

    api, store, broker = client
    monkeypatch.setenv("JWT_USER_CLAIM", "tenant_id")
    fresh = Settings()
    monkeypatch.setattr(gateway, "settings", fresh)
    monkeypatch.setattr(auth_module, "settings", fresh)

    response = _vend(
        api, make_jwt("human-user", tenant_id="tenant-acme")
    )
    assert response.status_code == 200
    assert broker.users == ["tenant-acme"]
    assert store.get_user("tenant-acme") is not None


def test_manual_and_usage_blocks_prevent_vending(client):
    api, store, broker = client
    _seed_user(store, "manual", "Manual", 1, 100, 50)
    store.set_user_status("manual", "blocked", "admin API", origin="admin")
    assert _vend(api, make_jwt("manual")).status_code == 403

    _seed_user(store, "spent", "Spent", 1, 100, 50)
    store._usage.put_item(  # noqa: SLF001
        Item={
            "user_id": "spent",
            "window": current_window(),
            "cost_micro": MICRO,
            "input_tokens": 0,
            "output_tokens": 0,
            "requests": 1,
        }
    )
    response = _vend(api, make_jwt("spent"))
    assert response.status_code == 429
    assert store.get_user("spent").status == "blocked"
    assert broker.users == []


def test_existing_credentials_are_not_revoked_by_current_status_block(client):
    """Blocking denies the next vend/refresh; already-issued keys keep
    their fixed deadline (the revocation layer cuts them out-of-band)."""
    api, store, broker = client

    issued = _vend(api, make_jwt("alice"))
    assert issued.status_code == 200
    assert issued.json()["expiration"]  # permission deadline, always set

    store.set_user_status("alice", "blocked", "admin baseline", origin="admin")
    rejected = _vend(api, make_jwt("alice"))

    assert rejected.status_code == 403
    assert broker.users == ["alice"]
    # The control plane has no handle that mutates the already-returned set.
    assert issued.json()["aws_access_key_id"] == "ASIAFAKE"


def test_overlapping_vends_from_two_processes_share_one_lease(client):
    """A second process with a fresh lease ID joins the current lease: it
    gets credentials, but with the same deadline, so it cannot extend
    access early. The same lease ID retries the fixed deadline."""
    api, _, broker = client

    first = api.post(
        "/v1/credentials",
        headers={
            "Authorization": f"Bearer {make_jwt('alice')}",
            "X-Quota-Lease-Id": "lease-a",
        },
    )
    fresh_id = _vend(api, make_jwt("alice"))
    retry = api.post(
        "/v1/credentials",
        headers={
            "Authorization": f"Bearer {make_jwt('alice')}",
            "X-Quota-Lease-Id": "lease-a",
        },
    )

    assert first.status_code == 200
    assert fresh_id.status_code == 200
    assert fresh_id.json()["expiration"] == first.json()["expiration"]
    assert fresh_id.json()["lease_id"] == "lease-a"
    assert fresh_id.json()["lease_generation"] == 1
    assert retry.status_code == 200
    assert retry.json()["expiration"] == first.json()["expiration"]
    assert broker.users == ["alice", "alice", "alice"]
    assert len(set(broker.permission_deadlines)) == 1


def test_lease_mode_retries_fixed_lease_and_joins_premature_new_id(
    client, monkeypatch
):
    api, _, broker = client
    monkeypatch.setattr(
        gateway,
        "settings",
        replace(
            gateway.settings,
            permission_lease_seconds=300,
        ),
    )
    token = make_jwt("alice")

    first = api.post(
        "/v1/credentials",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Quota-Lease-Id": "lease-a",
        },
    )
    retry = api.post(
        "/v1/credentials",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Quota-Lease-Id": "lease-a",
        },
    )
    premature = api.post(
        "/v1/credentials",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Quota-Lease-Id": "lease-b",
        },
    )

    assert first.status_code == retry.status_code == 200
    assert first.json()["lease_id"] == retry.json()["lease_id"] == "lease-a"
    assert first.json()["expiration"] == retry.json()["expiration"]
    assert first.json()["refresh_after"] == retry.json()["refresh_after"]
    assert first.json()["sts_expiration"] == "2026-08-18T12:15:00+00:00"
    assert broker.permission_deadlines[0] == broker.permission_deadlines[1]
    assert premature.status_code == 200
    assert premature.json()["lease_id"] == "lease-a"
    assert premature.json()["expiration"] == first.json()["expiration"]
    assert premature.json()["refresh_after"] == first.json()["refresh_after"]
    assert broker.permission_deadlines[2] == broker.permission_deadlines[0]


def test_failed_sts_attempt_keeps_fixed_lease_for_same_id_retry(
    client, monkeypatch
):
    api, store, _ = client
    monkeypatch.setattr(
        gateway,
        "settings",
        replace(gateway.settings),
    )

    class FailingOnceBroker(FakeBroker):
        def __init__(self):
            super().__init__()
            self.failed = False

        def vend(self, identity, *, permission_deadline=None):
            if not self.failed:
                self.failed = True
                raise BrokerError(500, "simulated STS failure")
            return super().vend(
                identity, permission_deadline=permission_deadline
            )

    broker = FailingOnceBroker()
    monkeypatch.setattr(gateway, "_broker", broker)
    token = make_jwt("alice")

    failed = api.post(
        "/v1/credentials",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Quota-Lease-Id": "lease-a",
        },
    )
    premature = api.post(
        "/v1/credentials",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Quota-Lease-Id": "lease-b",
        },
    )
    retry = api.post(
        "/v1/credentials",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Quota-Lease-Id": "lease-a",
        },
    )

    assert failed.status_code == 500
    # The reserved lease survives the STS failure: another ID joins it and
    # the same-ID retry gets the original deadline.
    assert premature.status_code == 200
    assert premature.json()["lease_id"] == "lease-a"
    assert retry.status_code == 200
    assert retry.json()["lease_id"] == "lease-a"
    assert retry.json()["expiration"] == premature.json()["expiration"]
    assert store.get_active_lease("alice").lease_id == "lease-a"


def test_post_reservation_gate_recheck_catches_concurrent_block(
    client, monkeypatch
):
    api, store, broker = client
    monkeypatch.setattr(
        gateway,
        "settings",
        replace(gateway.settings),
    )
    original_reserve = store.reserve_lease

    def reserve_then_block(*args, **kwargs):
        reservation = original_reserve(*args, **kwargs)
        store.set_user_status("alice", "blocked", "race test", origin="admin")
        return reservation

    monkeypatch.setattr(store, "reserve_lease", reserve_then_block)
    response = api.post(
        "/v1/credentials",
        headers={
            "Authorization": f"Bearer {make_jwt('alice')}",
            "X-Quota-Lease-Id": "lease-a",
        },
    )

    assert response.status_code == 403
    assert broker.users == []


def test_previous_auto_block_is_reactivated_at_next_vend(client):
    api, store, broker = client
    _seed_user(store, "fresh", "Fresh", 1, 100, 50)
    store.set_user_status("fresh", "blocked", "auto: previous window", origin="automatic")
    assert _vend(api, make_jwt("fresh")).status_code == 200
    assert store.get_user("fresh").active
    assert broker.users == ["fresh"]


def test_admin_create_list_update_block_and_usage(client):
    api, store, _ = client
    created = api.post(
        "/admin/users",
        json={
            "user_id": "tenant/acme",
            "name": "ACME",
            **_limits(25, 1_000_000, 200_000),
        },
        headers=ADMIN,
    )
    assert created.status_code == 200
    assert created.json()["limits"]["daily"]["output_tokens"] == 200_000

    updated = api.put(
        "/admin/user/limits",
        params={"user_id": "tenant/acme"},
        json=_limits(30, 2_000_000, 400_000),
        headers=ADMIN,
    )
    assert updated.status_code == 200
    assert updated.json()["limits"] == {
        "daily": {
            "usd": 30.0,
            "input_tokens": 2_000_000,
            "output_tokens": 400_000,
            # A period submitted without thresholds keeps the deployment
            # default (warn at 80 %, block at 100 %).
            "thresholds": DEFAULT_THRESHOLDS,
        },
        "weekly": None,
        "monthly": None,
    }

    blocked = api.put(
        "/admin/user/status",
        params={"user_id": "tenant/acme"},
        json={"status": "blocked", "reason": "admin test"},
        headers=ADMIN,
    )
    assert blocked.status_code == 200
    assert store.get_user("tenant/acme").status_reason == "admin test"

    listing = api.get("/admin/users", headers=ADMIN).json()
    assert listing["users"][0]["user_id"] == "tenant/acme"
    usage = api.get(
        "/admin/user/usage", params={"user_id": "tenant/acme"}, headers=ADMIN
    ).json()
    assert usage["requests"] == 0


def test_admin_summary_exposes_single_guarantee(client):
    api, store, _ = client
    _seed_user(store, "alice", "Alice", 1, 100, 50)
    body = api.get("/admin/summary", headers=ADMIN).json()
    assert body["enforcement"]["mode"] == "layered"
    assert body["enforcement"]["credential_ttl_seconds"] == 900
    assert body["enforcement"]["permission_lease_seconds"] == 300
    assert body["enforcement"]["permission_lease_source"] == (
        "deployment_default"
    )
    assert body["enforcement"]["refresh_overlap_seconds"] == 10
    assert body["enforcement"]["refresh_jitter_seconds"] == 5
    assert body["enforcement"]["vend_rate_limit_per_minute"] == 6
    assert body["enforcement"]["revocation_policy_shards"] == 19
    assert body["enforcement"]["revocation_reconcile_minutes"] == 5
    assert body["observability"] == {
        "source": "bedrock_model_invocation_logs",
        "delivery": "cloudwatch_logs_subscription",
        "metrics_namespace": "BedrockSpendControls",
        "detection_lag_metric": "DetectionLagMilliseconds",
    }


def test_operations_is_read_only_safe_and_reports_revocation_health(
    client, monkeypatch
):
    api, store, _ = client
    now = datetime.now(timezone.utc)
    alarm_names = {
        "emergency_failure": "emergency-failure-alarm",
        "emergency_dlq": "emergency-dlq-alarm",
        "revocation_failure": "revocation-failure-alarm",
        "revocation_overflow": "revocation-overflow-alarm",
        "revocation_dlq": "revocation-dlq-alarm",
        "revocation_iterator_age": "revocation-age-alarm",
    }
    monkeypatch.setattr(
        gateway,
        "settings",
        replace(
            gateway.settings,
            vended_credential_ttl_seconds=3600,
            operations_alarm_names_json=json.dumps(alarm_names),
        ),
    )
    requested = store.set_emergency_desired(
        active=True,
        actor="emergency-shared-key",
        reason="sensitive incident detail",
        now=now - timedelta(minutes=2),
    )
    store.mark_emergency_applied(
        active=True,
        generation=requested["generation"],
        now=now - timedelta(minutes=1),
    )
    cloudwatch = FakeCloudWatch(
        results=[
            {
                "Id": "detectionlag",
                "StatusCode": "Complete",
                "Values": [1250.0],
                "Timestamps": [now - timedelta(minutes=1)],
            },
            {
                "Id": "revsyncsuccess",
                "StatusCode": "Complete",
                "Values": [1.0],
                "Timestamps": [now - timedelta(minutes=1)],
            },
            {
                "Id": "revsyncfailure",
                "StatusCode": "Complete",
                "Values": [],
                "Timestamps": [],
            },
            {
                "Id": "revoverflow",
                "StatusCode": "Complete",
                "Values": [],
                "Timestamps": [],
            },
            {
                "Id": "revokeddesired",
                "StatusCode": "Complete",
                "Values": [7.0],
                "Timestamps": [now - timedelta(minutes=1)],
            },
            {
                "Id": "emergencyfailure",
                "StatusCode": "Complete",
                "Values": [],
                "Timestamps": [],
            },
        ],
        alarms=[
            {
                "AlarmName": "emergency-failure-alarm",
                "StateValue": "OK",
                "StateUpdatedTimestamp": now - timedelta(minutes=1),
            },
            {
                "AlarmName": "emergency-dlq-alarm",
                "StateValue": "INSUFFICIENT_DATA",
                "StateUpdatedTimestamp": now - timedelta(minutes=3),
            },
            {
                "AlarmName": "revocation-failure-alarm",
                "StateValue": "OK",
                "StateUpdatedTimestamp": now - timedelta(minutes=1),
            },
            {
                "AlarmName": "revocation-overflow-alarm",
                "StateValue": "OK",
                "StateUpdatedTimestamp": now - timedelta(minutes=1),
            },
            {
                "AlarmName": "revocation-dlq-alarm",
                "StateValue": "OK",
                "StateUpdatedTimestamp": now - timedelta(minutes=1),
            },
            {
                "AlarmName": "revocation-age-alarm",
                "StateValue": "OK",
                "StateUpdatedTimestamp": now - timedelta(minutes=1),
            },
        ],
    )
    monkeypatch.setattr(gateway, "_cloudwatch", cloudwatch)

    assert api.get("/admin/operations").status_code == 403
    response = api.get("/admin/operations", headers=ADMIN)

    assert response.status_code == 200
    body = response.json()
    assert body["configuration"] == {
        "mode": "layered",
        "credential_ttl_seconds": 3600,
        "permission_lease_seconds": 300,
        "permission_lease_source": "deployment_default",
        "permission_lease_default_seconds": 300,
        "refresh_overlap_seconds": 10,
        "refresh_jitter_seconds": 5,
        "vend_rate_limit_per_minute": 6,
        "revocation_policy_shards": 19,
        "revocation_policy_max_characters": 6144,
        "revocation_reconcile_minutes": 5,
    }
    assert body["emergency"]["state"] == "active"
    assert body["emergency"]["converged"] is True
    assert body["emergency"]["generation"] == 1
    assert body["metrics"]["detection_lag_p95_ms"] == 1250.0
    assert body["metrics"]["telemetry_status"] == "complete"
    assert body["metrics"]["revoked_identities_desired"] == 7
    assert body["metrics"]["reconciliation_status"] == "current"
    assert "qualification" not in body
    assert body["cloudwatch"]["status"] == "available"
    assert {alarm["key"] for alarm in body["alarms"]} == set(alarm_names)
    assert next(
        alarm for alarm in body["alarms"] if alarm["key"] == "emergency_dlq"
    )["state"] == "INSUFFICIENT_DATA"
    serialized = json.dumps(body).lower()
    assert "sensitive incident detail" not in serialized
    assert "emergency-shared-key" not in serialized
    assert "secret" not in serialized
    assert cloudwatch.metric_calls and cloudwatch.alarm_calls


def test_operations_marks_partial_cloudwatch_evidence_unknown(
    client, monkeypatch
):
    api, _, _ = client
    now = datetime.now(timezone.utc)
    alarm_names = {
        "revocation_failure": "revocation-failure-alarm",
        "revocation_overflow": "revocation-overflow-alarm",
        "revocation_dlq": "revocation-dlq-alarm",
        "revocation_iterator_age": "revocation-age-alarm",
    }
    monkeypatch.setattr(
        gateway,
        "settings",
        replace(
            gateway.settings,
            vended_credential_ttl_seconds=3600,
            operations_alarm_names_json=json.dumps(alarm_names),
        ),
    )
    results = [
        {
            "Id": query_id,
            "StatusCode": (
                "PartialData" if query_id == "revsyncfailure" else "Complete"
            ),
            "Values": [1.0] if query_id == "revsyncsuccess" else [],
            "Timestamps": (
                [now - timedelta(minutes=1)]
                if query_id == "revsyncsuccess"
                else []
            ),
        }
        for query_id in (
            "detectionlag",
            "revsyncsuccess",
            "revsyncfailure",
            "revoverflow",
            "revokeddesired",
            "emergencyfailure",
        )
    ]
    alarms = [
        {
            "AlarmName": alarm_name,
            "StateValue": "OK",
            "StateUpdatedTimestamp": now,
        }
        for alarm_name in alarm_names.values()
    ]
    monkeypatch.setattr(
        gateway,
        "_cloudwatch",
        FakeCloudWatch(results=results, alarms=alarms),
    )

    body = api.get("/admin/operations", headers=ADMIN).json()

    assert body["cloudwatch"]["status"] == "partial"
    assert body["metrics"]["reconciliation_status"] == "unknown"


def test_operations_gracefully_reports_unavailable_cloudwatch(
    client, monkeypatch
):
    api, _, _ = client
    error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}},
        "GetMetricData",
    )
    monkeypatch.setattr(gateway, "_cloudwatch", FakeCloudWatch(error=error))

    response = api.get("/admin/operations", headers=ADMIN)

    assert response.status_code == 200
    body = response.json()
    assert body["cloudwatch"] == {
        "status": "unavailable",
        "error_code": "AccessDenied",
    }
    assert body["configuration"]["mode"] == "layered"
    assert body["configuration"]["permission_lease_seconds"] == 300
    assert body["emergency"]["state"] == "inactive"
    assert body["metrics"]["detection_lag_p95_ms"] is None
    # Revocation is always on: unavailable telemetry is unknown, never N/A.
    assert body["metrics"]["reconciliation_status"] == "unknown"
    assert all(alarm["state"] == "UNAVAILABLE" for alarm in body["alarms"])
    # The sweep state comes from DynamoDB, not CloudWatch, so it is still
    # answered; "never ran" until the first real pass writes its row.
    assert body["auto_block_sweep"] == {
        "schedule": "00:05 UTC daily",
        "status": "never_ran",
        "last_run": None,
    }


def test_operations_reports_the_last_auto_block_sweep(
    client, fake_dynamodb, monkeypatch
):
    api, _, _ = client
    monkeypatch.setattr(gateway, "_cloudwatch", FakeCloudWatch(results=[], alarms=[]))
    fake_dynamodb.Table("users-test").put_item(
        Item={
            "user_id": "CONFIG#AUTO_BLOCK_SWEEP",
            "ran_at": "2026-09-15T00:05:04+00:00",
            "dry_run": False,
            "evaluated": 3,
            "lifted": 2,
            "still_blocked": 1,
            "admin_blocked": 0,
            "raced": 0,
            "lifted_users": ["alice", "bob"],
            "failures": [],
        }
    )

    body = api.get("/admin/operations", headers=ADMIN).json()

    sweep = body["auto_block_sweep"]
    assert sweep["status"] == "ok"
    assert sweep["schedule"] == "00:05 UTC daily"
    assert sweep["last_run"]["ran_at"] == "2026-09-15T00:05:04+00:00"
    assert sweep["last_run"]["lifted"] == 2
    assert sweep["last_run"]["still_blocked"] == 1
    assert sweep["last_run"]["lifted_users"] == ["alice", "bob"]
    assert "user_id" not in sweep["last_run"]
    # The bookkeeping row never shows up as a quota subject.
    users = api.get("/admin/users", headers=ADMIN).json()
    assert "CONFIG#AUTO_BLOCK_SWEEP" not in json.dumps(users)

    fake_dynamodb.Table("users-test").put_item(
        Item={
            "user_id": "CONFIG#AUTO_BLOCK_SWEEP",
            "ran_at": "2026-09-16T00:05:02+00:00",
            "dry_run": False,
            "evaluated": 1,
            "lifted": 0,
            "still_blocked": 0,
            "admin_blocked": 0,
            "raced": 0,
            "lifted_users": [],
            "failures": [{"user_id": "carol", "error": "throttled"}],
        }
    )
    assert api.get("/admin/operations", headers=ADMIN).json()["auto_block_sweep"][
        "status"
    ] == "failed"


def _usage_metric(name: str, value: str) -> dict:
    return {
        "MetricName": "Requests",
        "Dimensions": [{"Name": name, "Value": value}],
    }


def test_usage_metrics_requires_admin_and_validates_days(client):
    api, _, _ = client
    assert api.get("/admin/usage/metrics").status_code == 403
    for days in (0, 31):
        response = api.get(
            f"/admin/usage/metrics?days={days}", headers=ADMIN
        )
        assert response.status_code == 400
        assert "days must be between 1 and 30" in (
            response.json()["error"]["message"]
        )


def test_usage_metrics_aggregates_models_and_top_users(client, monkeypatch):
    api, store, _ = client
    # Only user-a still exists in the users table; user-b metered and was
    # deleted, so its top-user entry must fall back to the raw quota key.
    _seed_user(store, "user-a", "Ada Lovelace", 1.0, 100, 50)
    now = datetime.now(timezone.utc)
    today = datetime.combine(
        now.date(), datetime.min.time(), tzinfo=timezone.utc
    )
    yesterday = today - timedelta(days=1)
    results = [
        {"Id": "t_cost_usd", "StatusCode": "Complete",
         "Timestamps": [today, yesterday], "Values": [0.25, 0.5]},
        {"Id": "t_requests", "StatusCode": "Complete",
         "Timestamps": [today, yesterday], "Values": [3.0, 5.0]},
        {"Id": "t_input_tokens", "StatusCode": "Complete",
         "Timestamps": [today], "Values": [120.0]},
        {"Id": "t_output_tokens", "StatusCode": "Complete",
         "Timestamps": [today], "Values": [40.0]},
        {"Id": "m0_cost_usd", "StatusCode": "Complete",
         "Timestamps": [today, yesterday], "Values": [0.05, 0.5]},
        {"Id": "m0_requests", "StatusCode": "Complete",
         "Timestamps": [today, yesterday], "Values": [1.0, 5.0]},
        {"Id": "m1_cost_usd", "StatusCode": "Complete",
         "Timestamps": [today], "Values": [0.2]},
        {"Id": "m1_requests", "StatusCode": "Complete",
         "Timestamps": [today], "Values": [2.0]},
        {"Id": "u0_cost", "StatusCode": "Complete",
         "Timestamps": [today], "Values": [0.05]},
        {"Id": "u0_requests", "StatusCode": "Complete",
         "Timestamps": [today], "Values": [1.0]},
        {"Id": "u1_cost", "StatusCode": "Complete",
         "Timestamps": [today, yesterday], "Values": [0.2, 0.5]},
        {"Id": "u1_requests", "StatusCode": "Complete",
         "Timestamps": [today, yesterday], "Values": [2.0, 5.0]},
    ]
    cloudwatch = FakeCloudWatch(
        results=results,
        metrics=[
            _usage_metric("Model", "amazon.nova-micro-v1:0"),
            _usage_metric("Model", "anthropic.claude-haiku"),
            _usage_metric("UserId", "user-a"),
            _usage_metric("UserId", "user-b"),
        ],
    )
    monkeypatch.setattr(gateway, "_cloudwatch", cloudwatch)

    response = api.get("/admin/usage/metrics?days=7", headers=ADMIN)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "available"
    assert body["period"] == "daily"
    assert len(body["days"]) == 7
    assert body["days"][-1] == now.date().isoformat()
    assert body["totals"]["cost_usd"] == 0.75
    assert body["totals"]["requests"] == 8.0
    # Models are discovered via ListMetrics and reported alphabetically.
    assert [entry["model"] for entry in body["models"]] == [
        "amazon.nova-micro-v1:0",
        "anthropic.claude-haiku",
    ]
    nova = body["models"][0]
    assert nova["totals"]["cost_usd"] == 0.55
    assert nova["series"]["cost_usd"][-1] == 0.05
    assert nova["series"]["cost_usd"][-2] == 0.5
    assert sum(nova["series"]["requests"]) == 6.0
    # Top users are ordered by range cost; names resolve from the users
    # table and deleted identities fall back to the raw key. Each entry
    # says which control path it belongs to so the overview can label it.
    assert body["top_users"] == [
        {"user_id": "user-b", "name": "user-b", "cost_usd": 0.7,
         "requests": 7, "granularity": "user"},
        {"user_id": "user-a", "name": "Ada Lovelace", "cost_usd": 0.05,
         "requests": 1, "granularity": "user"},
    ]
    # One batched GetMetricData: totals + 2 models x 4 + 2 users x 2.
    queries = cloudwatch.metric_calls[0]["MetricDataQueries"]
    assert len(queries) == 4 + 8 + 4
    assert all(query["MetricStat"]["Period"] == 86400 for query in queries)
    dimension_filters = [
        call["Dimensions"][0]["Name"] for call in cloudwatch.list_calls
    ]
    assert dimension_filters == ["Model", "UserId"]


def test_usage_metrics_marks_partial_and_unavailable(client, monkeypatch):
    api, _, _ = client
    incomplete = FakeCloudWatch(
        results=[
            {"Id": "t_cost_usd", "StatusCode": "InternalError",
             "Timestamps": [], "Values": []},
        ],
        metrics=[_usage_metric("Model", "amazon.nova-micro-v1:0")],
    )
    monkeypatch.setattr(gateway, "_cloudwatch", incomplete)
    body = api.get("/admin/usage/metrics", headers=ADMIN).json()
    assert body["status"] == "partial"
    assert len(body["days"]) == 14

    error = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}},
        "ListMetrics",
    )
    monkeypatch.setattr(gateway, "_cloudwatch", FakeCloudWatch(error=error))
    body = api.get("/admin/usage/metrics?days=7", headers=ADMIN).json()
    assert body["status"] == "unavailable"
    assert body["error_code"] == "AccessDenied"
    assert body["models"] == []
    assert body["top_users"] == []
    assert len(body["days"]) == 7


def test_admin_summary_and_operations_follow_the_runtime_dial(
    client, monkeypatch
):
    """The lease window is a runtime dial, not a deployment mode."""
    api, _, _ = client

    baseline = api.get("/admin/summary", headers=ADMIN).json()["enforcement"]
    assert baseline["permission_lease_seconds"] == 300
    assert baseline["permission_lease_source"] == "deployment_default"

    updated = api.put(
        "/admin/enforcement",
        headers=ADMIN,
        json={"permission_lease_seconds": 60, "reason": "incident response"},
    )
    assert updated.status_code == 200
    assert updated.json()["permission_lease_seconds"] == 60
    assert updated.json()["source"] == "runtime"

    enforcement = api.get("/admin/summary", headers=ADMIN).json()[
        "enforcement"
    ]
    assert enforcement["mode"] == "layered"
    assert enforcement["permission_lease_seconds"] == 60
    assert enforcement["permission_lease_source"] == "runtime"

    monkeypatch.setattr(
        gateway,
        "_cloudwatch",
        FakeCloudWatch(
            error=ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}},
                "GetMetricData",
            )
        ),
    )
    operations = api.get("/admin/operations", headers=ADMIN).json()
    assert operations["configuration"]["permission_lease_seconds"] == 60
    assert operations["configuration"]["permission_lease_source"] == (
        "runtime"
    )
    assert operations["configuration"][
        "permission_lease_default_seconds"
    ] == 300


def test_emergency_stop_requires_break_glass_confirmation_and_gates_recovery(
    client,
):
    api, store, broker = client
    routine_admin = api.post(
        "/admin/emergency-stop",
        json={
            "action": "activate",
            "confirmation": "STOP_ALL_BEDROCK_SESSIONS",
            "reason": "exercise",
        },
        headers=ADMIN,
    )
    assert routine_admin.status_code == 403

    wrong = api.post(
        "/admin/emergency-stop",
        json={
            "action": "activate",
            "confirmation": "yes",
            "reason": "exercise",
        },
        headers=EMERGENCY,
    )
    assert wrong.status_code == 400
    assert wrong.json()["error"]["type"] == "confirmation_required"

    activated = api.post(
        "/admin/emergency-stop",
        json={
            "action": "activate",
            "confirmation": "STOP_ALL_BEDROCK_SESSIONS",
            "reason": "security exercise",
        },
        headers=EMERGENCY,
    )
    assert activated.status_code == 202
    assert activated.json()["state"] == "activating"
    assert activated.json()["actor"] == "emergency-shared-key"
    assert activated.json()["generation"] == 1
    assert _vend(api, make_jwt("alice")).status_code == 503
    assert broker.users == []

    repeated = api.post(
        "/admin/emergency-stop",
        json={
            "action": "activate",
            "confirmation": "STOP_ALL_BEDROCK_SESSIONS",
            "reason": "security exercise",
        },
        headers=EMERGENCY,
    )
    assert repeated.json()["idempotent"] is False
    assert repeated.json()["retry"] is True
    assert repeated.json()["generation"] == 2

    recovering = api.post(
        "/admin/emergency-stop",
        json={
            "action": "recover",
            "confirmation": "RESTORE_ALL_BEDROCK_SESSIONS",
            "reason": "exercise complete",
        },
        headers=EMERGENCY,
    )
    assert recovering.status_code == 202
    assert recovering.json()["state"] == "recovering"
    assert recovering.json()["generation"] == 3
    assert _vend(api, make_jwt("alice")).status_code == 503

    store.mark_emergency_applied(active=False)
    assert _vend(api, make_jwt("alice")).status_code == 200
    assert broker.users == ["alice"]


def test_admin_requires_authorization_and_valid_payloads(client):
    api, store, _ = client
    assert api.get("/admin/users").status_code == 403
    assert api.post(
        "/admin/users", content="{", headers={**ADMIN, "Content-Type": "application/json"}
    ).status_code == 400
    assert api.post(
        "/admin/users", json={"limits": _limits()["limits"]}, headers=ADMIN
    ).status_code == 400

    _seed_user(store, "alice", "Alice", 1, 100, 50)
    for payload in (
        {},
        {"limits": {"daily": {"usd": -1, "input_tokens": 1, "output_tokens": 1}}},
        {"limits": {"daily": {"usd": 1, "input_tokens": 1.5, "output_tokens": 1}}},
        {"limits": {"daily": {"usd": 1, "input_tokens": 1, "output_tokens": False}}},
        {"limits": {"daily": None, "weekly": None, "monthly": None}},
        {**_limits(2), "reason": 123},
    ):
        assert api.put(
            "/admin/user/limits",
            params={"user_id": "alice"},
            json=payload,
            headers=ADMIN,
        ).status_code == 400
    assert api.put(
        "/admin/user/status",
        params={"user_id": "alice"},
        json={"status": "invalid"},
        headers=ADMIN,
    ).status_code == 400


def _enable_admin_jwt(monkeypatch):
    import app.auth as auth_module
    from app.config import Settings

    monkeypatch.setenv("ADMIN_JWT_CLAIM", "groups")
    monkeypatch.setenv("ADMIN_JWT_VALUE", "quota-admins")
    fresh = Settings()
    monkeypatch.setattr(gateway, "settings", fresh)
    monkeypatch.setattr(auth_module, "settings", fresh)


def test_admin_jwt_group_is_supported(client, monkeypatch):
    api, _, _ = client
    _enable_admin_jwt(monkeypatch)
    accepted = make_jwt("admin", groups=["quota-admins"])
    rejected = make_jwt("user", groups=["developers"])
    assert api.get(
        "/admin/summary",
        headers={"Authorization": f"Bearer {accepted}"},
    ).status_code == 200
    assert api.get(
        "/admin/summary",
        headers={"Authorization": f"Bearer {rejected}"},
    ).status_code == 403


def test_duplicate_admin_create_preserves_existing_user(client):
    api, store, _ = client
    _seed_user(store, "alice", "Original", 7, 700, 70)
    store.set_user_status("alice", "blocked", "auto: existing state", origin="automatic")
    before = store.get_user("alice")

    response = api.post(
        "/admin/users",
        json={
            "user_id": "alice",
            "name": "Replacement",
            **_limits(1, 1, 1),
        },
        headers={**ADMIN, "Idempotency-Key": "duplicate-create"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["type"] == "user_already_exists"
    assert store.get_user("alice") == before


def test_minimal_row_reads_as_version_zero_and_upgrades_on_first_mutation(
    client,
):
    """A row carrying only the required attributes reads with every
    optional field at its documented default."""
    api, store, _ = client
    store._users.put_item(  # noqa: SLF001 - seed a bare row
        Item={
            "user_id": "minimal",
            "name": "Minimal",
            "status": "active",
            "status_reason": "",
            "daily_usd_micro": MICRO,
            "daily_input_tokens": 100,
            "daily_output_tokens": 50,
        }
    )

    detail = api.get("/admin/user", params={"user_id": "minimal"}, headers=ADMIN)
    assert detail.status_code == 200
    assert detail.headers["etag"] == '"0"'
    # No stored thresholds list: the period reports the deployment default.
    assert detail.json()["user"]["limits"] == {
        "daily": {
            "usd": 1.0,
            "input_tokens": 100,
            "output_tokens": 50,
            "thresholds": DEFAULT_THRESHOLDS,
        },
        "weekly": None,
        "monthly": None,
    }
    assert detail.json()["user"]["rate"] is None
    assert detail.json()["user"]["version"] == 0
    assert detail.json()["user"]["created_at"] is None
    assert detail.json()["user"]["updated_at"] is None
    # A missing status_origin is admin-owned: machines never lift it.
    assert detail.json()["user"]["status_origin"] == "admin"

    updated = api.put(
        "/admin/user/limits",
        params={"user_id": "minimal"},
        json=_limits(2, 100, 50),
        headers={**ADMIN, "Idempotency-Key": "minimal-upgrade"},
    )
    assert updated.status_code == 200
    assert updated.headers["etag"] == '"1"'
    assert updated.json()["user"]["version"] == 1
    assert updated.json()["user"]["created_at"] is None
    assert updated.json()["user"]["updated_at"]
    assert updated.json()["user"]["status_origin"] == "admin"


def test_admin_mutations_enforce_version_and_durable_idempotency(client):
    api, store, _ = client
    created = api.post(
        "/admin/users",
        json={"user_id": "alice", **_limits()},
        headers={**ADMIN, "Idempotency-Key": "create-alice"},
    )
    assert created.status_code == 200
    assert created.headers["etag"] == '"1"'
    create_replay = api.post(
        "/admin/users",
        json={"user_id": "alice", **_limits()},
        headers={**ADMIN, "Idempotency-Key": "create-alice"},
    )
    assert create_replay.status_code == 200
    assert create_replay.json() == created.json()
    create_mismatch = api.post(
        "/admin/users",
        json={"user_id": "alice", **_limits(9)},
        headers={**ADMIN, "Idempotency-Key": "create-alice"},
    )
    assert create_mismatch.status_code == 409
    assert create_mismatch.json()["error"]["type"] == (
        "idempotency_conflict"
    )

    request_headers = {
        **ADMIN,
        "If-Match": '"1"',
        "Idempotency-Key": "limits-alice",
    }
    first = api.put(
        "/admin/user/limits",
        params={"user_id": "alice"},
        json={**_limits(2), "reason": "  Quarterly increase  "},
        headers=request_headers,
    )
    replay = api.put(
        "/admin/user/limits",
        params={"user_id": "alice"},
        json={**_limits(2), "reason": "Quarterly increase"},
        headers=request_headers,
    )
    assert first.status_code == replay.status_code == 200
    assert replay.json() == first.json()
    assert replay.headers["etag"] == first.headers["etag"] == '"2"'
    assert store.get_user("alice").version == 2
    events, _ = store.list_admin_audit_page(
        user_id="alice", limit=10
    )
    assert events[0]["reason"] == "Quarterly increase"

    mismatch = api.put(
        "/admin/user/limits",
        params={"user_id": "alice"},
        json={**_limits(2), "reason": "Different justification"},
        headers=request_headers,
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["type"] == "idempotency_conflict"

    stale = api.put(
        "/admin/user/limits",
        params={"user_id": "alice"},
        json=_limits(4),
        headers={
            **ADMIN,
            "If-Match": '"1"',
            "Idempotency-Key": "stale-limits",
        },
    )
    assert stale.status_code == 409
    assert stale.headers["etag"] == '"2"'
    assert stale.json()["error"]["type"] == "version_conflict"
    assert stale.json()["error"]["details"]["current_user"]["version"] == 2
    assert store.get_user("alice").daily_usd_micro == 2 * MICRO


@pytest.mark.parametrize(
    "reason_payload",
    ({}, {"reason": "   "}),
    ids=("omitted", "blank"),
)
def test_limit_reason_is_optional_and_defaults_to_not_provided(
    client, reason_payload
):
    api, store, _ = client
    _seed_user(store, "alice", "Alice", 1, 100, 50)

    response = api.put(
        "/admin/user/limits",
        params={"user_id": "alice"},
        json={**_limits(1, 100, 75), **reason_payload},
        headers={
            **ADMIN,
            "If-Match": '"1"',
            "Idempotency-Key": f"limit-reason-{len(reason_payload)}",
        },
    )

    assert response.status_code == 200
    assert set(response.json()) == {"user_id", "updated", "limits", "user"}
    events, _ = store.list_admin_audit_page(user_id="alice", limit=10)
    assert events[0]["reason"] == gateway.DEFAULT_ADMIN_REASON == "not provided"


def test_status_transaction_updates_user_revocation_audit_and_rolls_back_conflict(
    client,
):
    api, store, _ = client
    created = api.post(
        "/admin/users",
        json={"user_id": "alice"},
        headers={**ADMIN, "Idempotency-Key": "status-create"},
    )
    assert created.status_code == 200

    blocked = api.put(
        "/admin/user/status",
        params={"user_id": "alice"},
        json={"status": "blocked", "reason": "operator request"},
        headers={
            **ADMIN,
            "If-Match": '"1"',
            "Idempotency-Key": "status-block",
        },
    )
    assert blocked.status_code == 200
    assert blocked.json()["user"]["version"] == 2
    assert blocked.json()["user"]["status_origin"] == "admin"
    revocation = store._users.get_item(  # noqa: SLF001
        Key={"user_id": "REVOCATION#alice"}
    )["Item"]
    assert revocation["desired_status"] == "blocked"
    events, _ = store.list_admin_audit_page(
        user_id="alice", limit=10
    )
    assert [event["event_type"] for event in events] == [
        "user.status.updated",
        "user.created",
    ]

    before_revocation = dict(revocation)
    before_events = list(events)
    stale = api.put(
        "/admin/user/status",
        params={"user_id": "alice"},
        json={"status": "active", "reason": "stale request"},
        headers={
            **ADMIN,
            "If-Match": '"1"',
            "Idempotency-Key": "status-stale",
        },
    )
    assert stale.status_code == 409
    assert store._users.get_item(  # noqa: SLF001
        Key={"user_id": "REVOCATION#alice"}
    )["Item"] == before_revocation
    assert store.list_admin_audit_page(
        user_id="alice", limit=10
    )[0] == before_events


@pytest.mark.parametrize(
    "reason_payload",
    ({}, {"reason": "   "}),
    ids=("omitted", "blank"),
)
def test_status_reason_fallback_is_normalized_before_store_and_audit(
    client, reason_payload
):
    api, store, _ = client
    _seed_user(store, "alice", "Alice", 1, 100, 50)

    response = api.put(
        "/admin/user/status",
        params={"user_id": "alice"},
        json={"status": "blocked", **reason_payload},
        headers={
            **ADMIN,
            "If-Match": '"1"',
            "Idempotency-Key": f"status-reason-{len(reason_payload)}",
        },
    )

    fallback = "not provided"
    assert response.status_code == 200
    assert response.json()["reason"] == fallback
    assert response.json()["user"]["status_reason"] == fallback
    assert store.get_user("alice").status_reason == fallback
    events, _ = store.list_admin_audit_page(user_id="alice", limit=10)
    assert events[0]["reason"] == fallback


def test_status_reason_hash_is_canonical_for_trimmed_and_fallback_replays(
    client,
):
    api, store, _ = client
    for user_id in ("trimmed", "fallback"):
        _seed_user(store, user_id, user_id.title(), 1, 100, 50)

    trimmed_headers = {
        **ADMIN,
        "If-Match": '"1"',
        "Idempotency-Key": "status-trimmed-replay",
    }
    padded = api.put(
        "/admin/user/status",
        params={"user_id": "trimmed"},
        json={"status": "blocked", "reason": "  Policy review  "},
        headers=trimmed_headers,
    )
    trimmed = api.put(
        "/admin/user/status",
        params={"user_id": "trimmed"},
        json={"status": "blocked", "reason": "Policy review"},
        headers=trimmed_headers,
    )

    assert padded.status_code == trimmed.status_code == 200
    assert padded.json() == trimmed.json()
    assert padded.json()["reason"] == "Policy review"
    assert padded.headers["etag"] == trimmed.headers["etag"] == '"2"'
    assert store.get_user("trimmed").version == 2
    trimmed_events, _ = store.list_admin_audit_page(
        user_id="trimmed", limit=10
    )
    assert [event["event_type"] for event in trimmed_events] == [
        "user.status.updated"
    ]
    assert trimmed_events[0]["reason"] == "Policy review"

    fallback_headers = {
        **ADMIN,
        "If-Match": '"1"',
        "Idempotency-Key": "status-fallback-replay",
    }
    omitted = api.put(
        "/admin/user/status",
        params={"user_id": "fallback"},
        json={"status": "blocked"},
        headers=fallback_headers,
    )
    blank = api.put(
        "/admin/user/status",
        params={"user_id": "fallback"},
        json={"status": "blocked", "reason": "  "},
        headers=fallback_headers,
    )

    assert omitted.status_code == blank.status_code == 200
    assert omitted.json() == blank.json()
    assert store.get_user("fallback").version == 2
    fallback_events, _ = store.list_admin_audit_page(
        user_id="fallback", limit=10
    )
    assert [event["event_type"] for event in fallback_events] == [
        "user.status.updated"
    ]


def test_canonical_admin_routes_disambiguate_path_like_identities(client):
    api, store, _ = client
    user_ids = ("team", "team/audit", "team/usage", "team/usage-history")
    today = datetime.now(timezone.utc).date().isoformat()
    for index, user_id in enumerate(user_ids, start=1):
        created = api.post(
            "/admin/users",
            json={"user_id": user_id, "name": f"Identity {user_id}"},
            headers={
                **ADMIN,
                "Idempotency-Key": f"canonical-create-{index}",
            },
        )
        assert created.status_code == 200
        store._usage.put_item(  # noqa: SLF001
            Item={
                "user_id": user_id,
                "window": today,
                "cost_micro": index * MICRO,
                "input_tokens": index * 10,
                "output_tokens": index * 5,
                "requests": index,
            }
        )

    for index, user_id in enumerate(user_ids, start=1):
        detail = api.get(
            "/admin/user", params={"user_id": user_id}, headers=ADMIN
        )
        assert detail.status_code == 200
        assert detail.json()["user"]["user_id"] == user_id
        assert detail.json()["user"]["name"] == f"Identity {user_id}"

        usage = api.get(
            "/admin/user/usage",
            params={"user_id": user_id, "window": today},
            headers=ADMIN,
        )
        assert usage.status_code == 200
        assert usage.json()["user_id"] == user_id
        assert usage.json()["requests"] == index

        history = api.get(
            "/admin/user/usage-history",
            params={"user_id": user_id, "start": today, "end": today},
            headers=ADMIN,
        )
        assert history.status_code == 200
        assert history.json()["user_id"] == user_id
        assert history.json()["usage"][0]["user_id"] == user_id

        audit = api.get(
            "/admin/user/audit",
            params={"user_id": user_id},
            headers=ADMIN,
        )
        assert audit.status_code == 200
        assert audit.json()["user_id"] == user_id
        assert {event["user_id"] for event in audit.json()["events"]} == {
            user_id
        }

        limits = api.put(
            "/admin/user/limits",
            params={"user_id": user_id},
            json={**_limits(index + 10), "reason": "route regression"},
            headers={
                **ADMIN,
                "If-Match": detail.headers["etag"],
                "Idempotency-Key": f"canonical-limits-{index}",
            },
        )
        assert limits.status_code == 200
        assert limits.json()["user_id"] == user_id
        assert limits.json()["user"]["version"] == 2

        status = api.put(
            "/admin/user/status",
            params={"user_id": user_id},
            json={"status": "blocked", "reason": "route regression"},
            headers={
                **ADMIN,
                "If-Match": limits.headers["etag"],
                "Idempotency-Key": f"canonical-status-{index}",
            },
        )
        assert status.status_code == 200
        assert status.json()["user_id"] == user_id
        assert status.json()["user"]["version"] == 3


def test_canonical_mutation_hash_binds_the_exact_query_identity(client):
    api, store, _ = client
    for user_id in ("team", "team/audit"):
        _seed_user(store, user_id, user_id, 1, 100, 50)
    headers = {
        **ADMIN,
        "If-Match": '"1"',
        "Idempotency-Key": "canonical-identity-bound",
    }

    first = api.put(
        "/admin/user/limits",
        params={"user_id": "team"},
        json={**_limits(2), "reason": "identity binding"},
        headers=headers,
    )
    conflict = api.put(
        "/admin/user/limits",
        params={"user_id": "team/audit"},
        json={**_limits(2), "reason": "identity binding"},
        headers=headers,
    )

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["error"]["type"] == "idempotency_conflict"
    assert store.get_user("team/audit").version == 1


@pytest.mark.parametrize(
    ("method", "route"),
    (
        ("get", "/admin/user"),
        ("get", "/admin/user/usage"),
        ("get", "/admin/user/usage-history"),
        ("get", "/admin/user/audit"),
        ("put", "/admin/user/limits"),
        ("put", "/admin/user/status"),
    ),
)
def test_canonical_user_id_must_be_valid_and_appear_once(
    client, method, route
):
    api, _, _ = client

    missing = api.request(method, route, headers=ADMIN)
    duplicate = api.request(
        method,
        f"{route}?user_id=team&user_id=team%2Faudit",
        headers=ADMIN,
    )
    reserved = api.request(
        method,
        route,
        params={"user_id": "CONFIG#internal"},
        headers=ADMIN,
    )

    for response in (missing, duplicate):
        assert response.status_code == 400
        assert response.json() == {
            "error": {
                "message": "user_id must be provided exactly once.",
                "type": "invalid_request_error",
                "code": "invalid_request_error",
            }
        }
    assert reserved.status_code == 400
    assert reserved.json() == {
        "error": {
            "message": "user identity uses a reserved internal prefix",
            "type": "invalid_request_error",
            "code": "invalid_request_error",
        }
    }


def test_path_style_user_routes_do_not_exist(client):
    """Only the ``/admin/user?user_id=`` family addresses a subject."""
    api, store, _ = client
    _seed_user(store, "team/audit", "Team Audit")

    for method, path in (
        ("get", "/admin/users/team"),
        ("get", "/admin/users/team/audit"),
        ("get", "/admin/users/team/usage"),
        ("put", "/admin/users/team/limits"),
        ("put", "/admin/users/team/status"),
    ):
        response = api.request(method, path, json={}, headers=ADMIN)
        assert response.status_code in (404, 405), (method, path)


def test_jwt_admin_audit_actor_uses_subject_with_tenant_quota_identity(
    client, monkeypatch
):
    import app.auth as auth_module
    from app.config import Settings

    api, store, broker = client
    monkeypatch.setenv("JWT_USER_CLAIM", "tenant_id")
    monkeypatch.setenv("ADMIN_JWT_CLAIM", "groups")
    monkeypatch.setenv("ADMIN_JWT_VALUE", "quota-admins")
    fresh = Settings()
    monkeypatch.setattr(gateway, "settings", fresh)
    monkeypatch.setattr(auth_module, "settings", fresh)
    token = make_jwt(
        "human-admin",
        tenant_id="tenant-acme",
        groups=["quota-admins"],
    )

    vended = _vend(api, token)
    assert vended.status_code == 200
    assert vended.json()["user_id"] == "tenant-acme"
    assert broker.users == ["tenant-acme"]

    detail = api.get(
        "/admin/user",
        params={"user_id": "tenant-acme"},
        headers={"X-Quota-User-Token": token},
    )
    updated = api.put(
        "/admin/user/limits",
        params={"user_id": "tenant-acme"},
        json={**_limits(2), "reason": "Tenant quota review"},
        headers={
            "X-Quota-User-Token": token,
            "If-Match": detail.headers["etag"],
            "Idempotency-Key": "tenant-limit-review",
        },
    )

    assert updated.status_code == 200
    events, _ = store.list_admin_audit_page(
        user_id="tenant-acme", limit=10
    )
    assert events[0]["actor"] == "human-admin"
    assert events[0]["auth_method"] == "jwt"


def test_admin_audit_attributes_verified_actor_without_credentials(
    client, monkeypatch
):
    api, store, _ = client
    _enable_admin_jwt(monkeypatch)
    token = make_jwt("admin-subject", groups=["quota-admins"])
    response = api.post(
        "/admin/users",
        json={"user_id": "jwt-user"},
        headers={
            "X-Quota-User-Token": token,
            "Idempotency-Key": "jwt-create",
        },
    )
    assert response.status_code == 200
    events = api.get(
        "/admin/user/audit",
        params={"user_id": "jwt-user"},
        headers={"X-Quota-User-Token": token},
    ).json()["events"]
    assert events[0]["actor"] == "admin-subject"
    assert events[0]["auth_method"] == "jwt"

    raw_audit = json.dumps(
        list(store._admin_audit.items.values()),  # noqa: SLF001
        default=str,
    )
    assert token not in raw_audit
    assert "admin-secret" not in raw_audit

    shared = api.post(
        "/admin/users",
        json={"user_id": "shared-user"},
        headers={**ADMIN, "Idempotency-Key": "shared-create"},
    )
    assert shared.status_code == 200
    shared_events = api.get(
        "/admin/user/audit", params={"user_id": "shared-user"}, headers=ADMIN
    ).json()["events"]
    assert shared_events[0]["actor"] == "admin-shared-key"
    assert shared_events[0]["auth_method"] == "shared-key"


def test_detail_usage_history_audit_pagination_and_user_filters(client):
    api, store, _ = client
    for user_id, name in (("alice", "Alice Smith"), ("bob", "Bob Jones")):
        assert api.post(
            "/admin/users",
            json={"user_id": user_id, "name": name},
            headers={**ADMIN, "Idempotency-Key": f"create-{user_id}"},
        ).status_code == 200
    assert api.put(
        "/admin/user/status",
        params={"user_id": "bob"},
        json={"status": "blocked", "reason": "test"},
        headers={
            **ADMIN,
            "If-Match": '"1"',
            "Idempotency-Key": "block-bob",
        },
    ).status_code == 200

    today = datetime.now(timezone.utc).date()
    for offset, requests in ((0, 3), (1, 2), (2, 1)):
        window = (today - timedelta(days=offset)).isoformat()
        store._usage.put_item(  # noqa: SLF001
            Item={
                "user_id": "alice",
                "window": window,
                "cost_micro": requests * MICRO,
                "input_tokens": requests * 10,
                "output_tokens": requests * 5,
                "requests": requests,
            }
        )

    detail = api.get("/admin/user", params={"user_id": "alice"}, headers=ADMIN)
    assert detail.status_code == 200
    assert detail.headers["etag"] == '"1"'
    assert detail.json()["user"]["name"] == "Alice Smith"

    first_history = api.get(
        "/admin/user/usage-history",
        params={"user_id": "alice", "limit": 2},
        headers=ADMIN,
    ).json()
    assert [row["requests"] for row in first_history["usage"]] == [3, 2]
    assert first_history["next_cursor"]
    second_history = api.get(
        "/admin/user/usage-history",
        params={
            "user_id": "alice",
            "limit": 2,
            "cursor": first_history["next_cursor"],
        },
        headers=ADMIN,
    ).json()
    assert [row["requests"] for row in second_history["usage"]] == [1]

    oldest_invalid = (today - timedelta(days=366)).isoformat()
    invalid = api.get(
        "/admin/user/usage-history",
        params={"user_id": "alice", "start": oldest_invalid},
        headers=ADMIN,
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["type"] == "usage_range_outside_retention"

    first_audit = api.get(
        "/admin/audit", params={"user_id": "bob", "limit": 1}, headers=ADMIN
    ).json()
    assert first_audit["events"][0]["event_type"] == "user.status.updated"
    assert first_audit["next_cursor"]
    second_audit = api.get(
        "/admin/audit",
        params={
            "user_id": "bob",
            "limit": 1,
            "cursor": first_audit["next_cursor"],
        },
        headers=ADMIN,
    ).json()
    assert second_audit["events"][0]["event_type"] == "user.created"

    blocked = api.get(
        "/admin/users", params={"status": "blocked"}, headers=ADMIN
    ).json()["users"]
    assert [user["user_id"] for user in blocked] == ["bob"]
    searched = api.get(
        "/admin/users", params={"query": "smith"}, headers=ADMIN
    ).json()["users"]
    assert [user["user_id"] for user in searched] == ["alice"]


def test_user_listing_skips_sentinel_rows_without_returning_an_empty_page(client):
    api, store, _ = client
    store._users.put_item(  # noqa: SLF001
        Item={"user_id": "CONFIG#FIRST", "state": "internal"}
    )
    _seed_user(store, "z-user", "Zed", 1, 10, 10)

    page = api.get("/admin/users?limit=1", headers=ADMIN).json()

    assert [user["user_id"] for user in page["users"]] == ["z-user"]


def test_admin_principal_is_derived_from_one_verified_jwt(client, monkeypatch):
    api, store, _ = client
    monkeypatch.setattr(
        gateway,
        "settings",
        replace(
            gateway.settings,
            admin_jwt_claim="groups",
            admin_jwt_value="quota-admins",
        ),
    )

    class CountingVerifier:
        def __init__(self):
            self.calls = 0

        def verify(self, token):
            self.calls += 1
            assert token == "verified-admin-token"  # nosec B105  # placeholder
            return gateway.Identity(
                user_id="verified-admin",
                claims={"groups": ["quota-admins"]},
            )

    counting = CountingVerifier()
    monkeypatch.setattr(gateway, "_verifier", counting)

    response = api.post(
        "/admin/users",
        json={"user_id": "single-verification"},
        headers={
            "X-Quota-User-Token": "verified-admin-token",
            "Idempotency-Key": "single-verification",
        },
    )

    assert response.status_code == 200
    assert counting.calls == 1
    events, _ = store.list_admin_audit_page(
        user_id="single-verification", limit=10
    )
    assert events[0]["actor"] == "verified-admin"


def test_cursors_are_schema_checked_and_bound_to_the_original_query(client):
    api, store, _ = client
    for user_id in ("alice", "bob"):
        assert api.post(
            "/admin/users",
            json={"user_id": user_id},
            headers={**ADMIN, "Idempotency-Key": f"cursor-{user_id}"},
        ).status_code == 200
    first_page = api.get("/admin/users?limit=1", headers=ADMIN).json()
    cursor = first_page["next_cursor"]
    assert cursor

    malformed = api.get(
        "/admin/users",
        params={"cursor": json.dumps({"user_id": "alice"})},
        headers=ADMIN,
    )
    rebound = api.get(
        "/admin/users",
        params={"cursor": cursor, "status": "blocked"},
        headers=ADMIN,
    )
    assert malformed.status_code == 400
    assert rebound.status_code == 400

    today = datetime.now(timezone.utc).date()
    for user_id in ("alice", "bob"):
        for offset in (0, 1):
            store._usage.put_item(  # noqa: SLF001
                Item={
                    "user_id": user_id,
                    "window": (today - timedelta(days=offset)).isoformat(),
                    "requests": 1,
                }
            )
    history = api.get(
        "/admin/user/usage-history",
        params={"user_id": "alice", "limit": 1},
        headers=ADMIN,
    ).json()
    reused = api.get(
        "/admin/user/usage-history",
        params={"user_id": "bob", "limit": 1, "cursor": history["next_cursor"]},
        headers=ADMIN,
    )
    assert reused.status_code == 400


def test_non_conditional_transaction_cancellation_is_retryable_not_version_conflict(
    client, monkeypatch
):
    api, store, _ = client
    _seed_user(store, "alice", "Alice", 1, 100, 50)

    class CanceledClient:
        def transact_write_items(self, **kwargs):
            raise ClientError(
                {
                    "Error": {
                        "Code": "TransactionCanceledException",
                        "Message": "transaction conflict",
                    },
                    "CancellationReasons": [
                        {"Code": "TransactionConflict"},
                        {"Code": "None"},
                        {"Code": "None"},
                    ],
                },
                "TransactWriteItems",
            )

    monkeypatch.setattr(store, "_client", CanceledClient())

    response = api.put(
        "/admin/user/limits",
        params={"user_id": "alice"},
        json=_limits(2),
        headers={
            **ADMIN,
            "If-Match": '"1"',
            "Idempotency-Key": "service-cancel",
        },
    )

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "transaction_unavailable"
    assert response.headers["retry-after"] == "1"
    assert store.get_user("alice").version == 1
    assert store.list_admin_audit_page(user_id="alice", limit=10)[0] == []


def test_admin_user_payload_exposes_lease_timing_but_never_the_lease_id(
    client, monkeypatch
):
    api, _, _ = client
    monkeypatch.setattr(
        gateway,
        "settings",
        replace(
            gateway.settings,
            permission_lease_seconds=300,
        ),
    )
    token = make_jwt("alice")

    before = api.get(
        "/admin/user",
        params={"user_id": "alice"},
        headers={"X-Quota-Admin-Key": "admin-secret"},
    )
    vend = api.post(
        "/v1/credentials",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Quota-Lease-Id": "lease-a",
        },
    )
    after = api.get(
        "/admin/user",
        params={"user_id": "alice"},
        headers={"X-Quota-Admin-Key": "admin-secret"},
    )
    listed = api.get(
        "/admin/users",
        headers={"X-Quota-Admin-Key": "admin-secret"},
    )

    assert vend.status_code == 200
    if before.status_code == 200:
        assert before.json()["user"]["lease"] is None
    lease = after.json()["user"]["lease"]
    assert lease is not None
    assert lease["active"] is True
    assert lease["generation"] >= 1
    assert lease["lease_seconds"] == 300
    assert lease["expires_at"] == vend.json()["expiration"]
    assert lease["refresh_after"] == vend.json()["refresh_after"]
    assert "lease_id" not in lease  # the renewal token must never leak
    row = next(u for u in listed.json()["users"] if u["user_id"] == "alice")
    assert row["lease"]["generation"] == lease["generation"]


def test_admin_user_lease_serializes_grant_time_duration(client):
    """Lease timing reflects the window at grant, not the current dial."""
    api, _, _ = client
    dial = api.put(
        "/admin/enforcement",
        headers=ADMIN,
        json={"permission_lease_seconds": 900, "reason": "batch window"},
    )
    assert dial.status_code == 200
    token = make_jwt("victor")
    vend = api.post(
        "/v1/credentials", headers={"Authorization": f"Bearer {token}"}
    )
    # Dial back down: existing lease rows must keep their issued window.
    back = api.put(
        "/admin/enforcement",
        headers=ADMIN,
        json={"permission_lease_seconds": 60, "reason": "post-batch"},
    )
    assert back.status_code == 200
    listed = api.get(
        "/admin/users", headers={"X-Quota-Admin-Key": "admin-secret"}
    )
    assert vend.status_code == 200
    assert listed.status_code == 200
    row = next(u for u in listed.json()["users"] if u["user_id"] == "victor")
    assert row["lease"]["lease_seconds"] == 900


# ---------------------------------------------------------------------------
# Workload mode: reserved namespace, granularity surfacing, list filter
# ---------------------------------------------------------------------------


def _seed_workload(store, workload_id="workload:payments", **overrides):
    _seed_user(
        store,
        workload_id,
        overrides.get("name", "payments"),
        overrides.get("daily_usd", 5.0),
        overrides.get("daily_input_tokens", 0),
        overrides.get("daily_output_tokens", 0),
    )


def test_workload_namespace_cannot_vend_credentials(client):
    api, store, broker = client

    response = _vend(api, make_jwt("workload:payments"))

    assert response.status_code == 401
    assert "workload:" in response.json()["error"]["message"]
    assert broker.users == []
    # The rejected vend must not auto-provision a row in the namespace.
    assert store.get_user("workload:payments") is None


PAYMENTS_ROSTER_ENTRY = {
    "name": "payments",
    "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "profile_arn": (
        "arn:aws:bedrock:us-east-1:123456789012:"
        "application-inference-profile/abc123"
    ),
    "role_arn": "arn:aws:iam::123456789012:role/payments-batch",
    "enforcement_ready": True,
}
REPORTS_ROSTER_ENTRY = {
    "name": "reports",
    "model": "us.amazon.nova-pro-v1:0",
    "profile_arn": (
        "arn:aws:bedrock:us-east-1:123456789012:"
        "application-inference-profile/def456"
    ),
    "role_arn": "",
    "enforcement_ready": False,
}


def _inline_roster(monkeypatch, roster: dict) -> None:
    """Point the broker at an inline roster (no Parameter Store)."""
    from dataclasses import replace as dc_replace

    monkeypatch.setattr(gateway, "_workload_roster_cache", None)
    monkeypatch.setattr(
        gateway,
        "settings",
        dc_replace(
            gateway.settings,
            workload_roster_parameter_name="",
            workload_roster_json=json.dumps(roster),
        ),
    )


class FakeSsm:
    def __init__(self, value: str | None, error: str | None = None):
        self.value = value
        self.error = error
        self.calls = 0

    def get_parameter(self, Name: str):
        self.calls += 1
        if self.error:
            raise ClientError(
                {"Error": {"Code": self.error, "Message": Name}},
                "GetParameter",
            )
        return {"Parameter": {"Name": Name, "Value": self.value}}


def test_workload_rows_carry_granularity_and_enforcement_ready(
    client, monkeypatch
):
    api, store, _ = client
    _seed_workload(store, "workload:payments")
    _seed_workload(store, "workload:reports", name="reports")
    _inline_roster(
        monkeypatch,
        {
            "workload:payments": PAYMENTS_ROSTER_ENTRY,
            "workload:reports": REPORTS_ROSTER_ENTRY,
        },
    )

    detail = api.get(
        "/admin/user",
        params={"user_id": "workload:payments"},
        headers={"X-Quota-Admin-Key": "admin-secret"},
    )
    assert detail.status_code == 200
    payload = detail.json()["user"]
    assert payload["granularity"] == "workload"
    assert payload["enforcement_ready"] is True
    # The identity block is what lets the console present a workload as a
    # workload: what it calls, through which profile, on which role.
    assert payload["workload"] == {
        "workload_id": "workload:payments",
        "name": "payments",
        "model": PAYMENTS_ROSTER_ENTRY["model"],
        "profile_arn": PAYMENTS_ROSTER_ENTRY["profile_arn"],
        "role_arn": PAYMENTS_ROSTER_ENTRY["role_arn"],
        "enforcement_ready": True,
        "registered": True,
        "tag": {"key": "bedrock-spend-controls-workload", "value": "payments"},
    }

    reports = api.get(
        "/admin/user",
        params={"user_id": "workload:reports"},
        headers={"X-Quota-Admin-Key": "admin-secret"},
    ).json()["user"]
    assert reports["enforcement_ready"] is False
    assert reports["workload"]["role_arn"] is None
    assert reports["workload"]["registered"] is True


def test_workload_roster_is_read_from_parameter_store_and_cached(
    client, monkeypatch
):
    from dataclasses import replace as dc_replace

    api, store, _ = client
    _seed_workload(store, "workload:payments")
    ssm = FakeSsm(json.dumps({"workload:payments": PAYMENTS_ROSTER_ENTRY}))
    monkeypatch.setattr(gateway, "_ssm", ssm)
    monkeypatch.setattr(gateway, "_workload_roster_cache", None)
    monkeypatch.setattr(
        gateway,
        "settings",
        dc_replace(
            gateway.settings,
            workload_roster_parameter_name="/spend-controls/workloads",
            # The inline fallback must NOT win when a parameter is named.
            workload_roster_json=json.dumps(
                {"workload:payments": {**PAYMENTS_ROSTER_ENTRY,
                                       "enforcement_ready": False}}
            ),
        ),
    )

    first = api.get(
        "/admin/user",
        params={"user_id": "workload:payments"},
        headers=ADMIN,
    ).json()["user"]
    second = api.get(
        "/admin/user",
        params={"user_id": "workload:payments"},
        headers=ADMIN,
    ).json()["user"]

    assert first["enforcement_ready"] is True
    assert first["workload"]["profile_arn"] == PAYMENTS_ROSTER_ENTRY["profile_arn"]
    assert second == first
    # One GetParameter for both requests: the roster is deploy-time static.
    assert ssm.calls == 1


def test_workload_roster_falls_back_when_parameter_store_is_unreadable(
    client, monkeypatch
):
    from dataclasses import replace as dc_replace

    api, store, _ = client
    _seed_workload(store, "workload:payments")
    monkeypatch.setattr(gateway, "_ssm", FakeSsm(None, error="AccessDenied"))
    monkeypatch.setattr(gateway, "_workload_roster_cache", None)
    monkeypatch.setattr(
        gateway,
        "settings",
        dc_replace(
            gateway.settings,
            workload_roster_parameter_name="/spend-controls/workloads",
            workload_roster_json=json.dumps(
                {"workload:payments": PAYMENTS_ROSTER_ENTRY}
            ),
        ),
    )

    detail = api.get(
        "/admin/user",
        params={"user_id": "workload:payments"},
        headers=ADMIN,
    )

    # Degraded, not broken: the inline copy answers and the row still reads
    # as a workload.
    assert detail.status_code == 200
    assert detail.json()["user"]["workload"]["registered"] is True
    workloads = api.get("/admin/workloads", headers=ADMIN).json()
    assert workloads["roster_source"] == "fallback"


def test_admin_workloads_joins_roster_with_metered_rows(client, monkeypatch):
    api, store, _ = client
    # payments has metered; reports is configured but silent since deploy;
    # retired metered once but was removed from the config.
    _seed_workload(store, "workload:payments")
    _seed_workload(store, "workload:retired", name="retired")
    _seed_user(store, "alice", "alice", 1.0, 0, 0)
    _inline_roster(
        monkeypatch,
        {
            "workload:payments": PAYMENTS_ROSTER_ENTRY,
            "workload:reports": REPORTS_ROSTER_ENTRY,
        },
    )

    assert api.get("/admin/workloads").status_code == 403
    response = api.get("/admin/workloads", headers=ADMIN)

    assert response.status_code == 200
    body = response.json()
    assert body["tag_key"] == "bedrock-spend-controls-workload"
    assert body["roster_source"] == "environment"
    by_id = {entry["workload_id"]: entry for entry in body["workloads"]}
    # JWT users never appear here.
    assert set(by_id) == {
        "workload:payments", "workload:reports", "workload:retired"
    }
    payments = by_id["workload:payments"]
    assert payments["registered"] is True
    assert payments["enforcement_ready"] is True
    assert payments["subject"]["user_id"] == "workload:payments"
    assert payments["subject"]["granularity"] == "workload"
    assert payments["subject"]["today"]["requests"] == 0
    assert "current_usage" in payments["subject"]
    reports = by_id["workload:reports"]
    assert reports["registered"] is True
    assert reports["enforcement_ready"] is False
    assert reports["model"] == "us.amazon.nova-pro-v1:0"
    assert reports["subject"] is None  # no row until first invocation
    retired = by_id["workload:retired"]
    assert retired["registered"] is False
    assert retired["enforcement_ready"] is False
    assert retired["model"] is None
    assert retired["subject"]["name"] == "retired"


def test_summary_splits_subjects_by_control_path(client, monkeypatch):
    api, store, _ = client
    _seed_user(store, "alice", "alice", 1.0, 0, 0)
    _seed_user(store, "bob", "bob", 1.0, 0, 0)
    store.set_user_status("bob", "blocked", "manual", origin="admin")
    _seed_workload(store, "workload:payments")
    _seed_workload(store, "workload:reports", name="reports")
    _seed_workload(store, "workload:retired", name="retired")
    store.set_user_status("workload:reports", "blocked", "auto: daily", origin="automatic")
    _inline_roster(
        monkeypatch,
        {
            "workload:payments": PAYMENTS_ROSTER_ENTRY,
            "workload:reports": REPORTS_ROSTER_ENTRY,
            "workload:silent": {**REPORTS_ROSTER_ENTRY, "name": "silent"},
        },
    )
    today = current_window()
    for subject, cost_usd, requests in (
        ("alice", 0.25, 2),
        ("workload:payments", 4.0, 40),
    ):
        store._usage.put_item(  # noqa: SLF001
            Item={
                "user_id": subject,
                "window": today,
                "cost_micro": int(cost_usd * MICRO),
                "input_tokens": 0,
                "output_tokens": 0,
                "requests": requests,
            }
        )

    enforcement = api.get("/admin/summary", headers=ADMIN).json()[
        "enforcement"
    ]

    # All-subject figures are unchanged for existing consumers.
    assert enforcement["total_users"] == 5
    assert enforcement["blocked_users"] == 2
    assert enforcement["today"]["cost_usd"] == 4.25
    subjects = enforcement["subjects"]
    assert subjects["users"] == {
        "total": 2,
        "blocked": 1,
        "today": {"cost_usd": 0.25, "input_tokens": 0,
                  "output_tokens": 0, "requests": 2},
    }
    workloads = subjects["workloads"]
    assert workloads["total"] == 3
    assert workloads["blocked"] == 1
    assert workloads["configured"] == 3
    assert workloads["metering_only"] == 1  # reports: no role
    assert workloads["unregistered"] == 1  # retired: row, no roster entry
    assert workloads["awaiting_traffic"] == 1  # silent: roster, no row
    assert workloads["today"]["cost_usd"] == 4.0
    assert workloads["today"]["requests"] == 40


def test_admin_cannot_hand_create_a_workload_row(client):
    api, store, _ = client

    response = api.post(
        "/admin/users",
        json={"user_id": "workload:rogue", "name": "rogue"},
        headers=ADMIN,
    )

    assert response.status_code == 400
    assert "workloads.json" in response.json()["error"]["message"]
    assert store.get_user("workload:rogue") is None


def test_workload_missing_from_registry_reports_not_ready(client):
    api, store, _ = client
    _seed_workload(store, "workload:orphan", name="orphan")

    detail = api.get(
        "/admin/user",
        params={"user_id": "workload:orphan"},
        headers={"X-Quota-Admin-Key": "admin-secret"},
    ).json()["user"]

    assert detail["granularity"] == "workload"
    assert detail["enforcement_ready"] is False


def test_plain_users_report_user_granularity_without_enforcement_field(
    client,
):
    api, store, _ = client
    _seed_user(store, "alice", "alice", 1.0, 0, 0)

    detail = api.get(
        "/admin/user",
        params={"user_id": "alice"},
        headers={"X-Quota-Admin-Key": "admin-secret"},
    ).json()["user"]

    assert detail["granularity"] == "user"
    assert "enforcement_ready" not in detail


def test_list_users_filters_by_granularity(client):
    api, store, _ = client
    _seed_user(store, "alice", "alice", 1.0, 0, 0)
    _seed_workload(store, "workload:payments")

    everyone = api.get(
        "/admin/users", headers={"X-Quota-Admin-Key": "admin-secret"}
    ).json()["users"]
    assert {user["user_id"] for user in everyone} == {
        "alice",
        "workload:payments",
    }

    workloads = api.get(
        "/admin/users",
        params={"granularity": "workload"},
        headers={"X-Quota-Admin-Key": "admin-secret"},
    ).json()["users"]
    assert [user["user_id"] for user in workloads] == ["workload:payments"]

    users = api.get(
        "/admin/users",
        params={"granularity": "user"},
        headers={"X-Quota-Admin-Key": "admin-secret"},
    ).json()["users"]
    assert [user["user_id"] for user in users] == ["alice"]

    invalid = api.get(
        "/admin/users",
        params={"granularity": "tenant"},
        headers={"X-Quota-Admin-Key": "admin-secret"},
    )
    assert invalid.status_code == 400


def test_workload_admin_mutations_use_standard_endpoints(client):
    """Workloads are ordinary rows: limits and manual blocks just work."""
    api, store, _ = client
    _seed_workload(store, "workload:payments")
    current = api.get(
        "/admin/user",
        params={"user_id": "workload:payments"},
        headers={"X-Quota-Admin-Key": "admin-secret"},
    )

    updated = api.put(
        "/admin/user/limits",
        params={"user_id": "workload:payments"},
        headers={
            "X-Quota-Admin-Key": "admin-secret",
            "If-Match": current.headers["ETag"],
            "Idempotency-Key": "workload-limit-test",
        },
        json={**_limits(9.5, 0, 0), "reason": "workload budget bump"},
    )

    assert updated.status_code == 200
    assert updated.json()["user"]["limits"]["daily"]["usd"] == 9.5


def test_monthly_limit_enable_uses_existing_daily_ledger_and_blocks_immediately(
    client,
):
    api, store, _ = client
    _seed_user(store, "alice", "Alice", 100, 0, 0)
    now = datetime.now(timezone.utc)
    daily_costs = (
        ((1, 7_000_000),)
        if now.day == 1
        else ((1, 4_000_000), (now.day, 3_000_000))
    )
    for day, cost_micro in daily_costs:
        store._usage.put_item(  # noqa: SLF001 - earlier days of this month
            Item={
                "user_id": "alice",
                "window": now.replace(day=day).date().isoformat(),
                "cost_micro": cost_micro,
                "requests": 1,
            }
        )
    detail = api.get(
        "/admin/user", params={"user_id": "alice"}, headers=ADMIN
    )

    blocked = api.put(
        "/admin/user/limits",
        params={"user_id": "alice"},
        headers={
            **ADMIN,
            "If-Match": detail.headers["etag"],
            "Idempotency-Key": "enable-monthly",
        },
        json={
            **_limits(
                100,
                0,
                0,
                monthly={
                    "usd": 6,
                    "input_tokens": 0,
                    "output_tokens": 0,
                },
            ),
            "reason": "monthly guardrail",
        },
    )

    assert blocked.status_code == 200
    assert blocked.json()["user"]["status"] == "blocked"
    assert blocked.json()["user"]["status_reason"].startswith(
        "auto: monthly USD quota exhausted"
    )
    assert store.get_user("alice").monthly_limits_enabled

    raised = api.put(
        "/admin/user/limits",
        params={"user_id": "alice"},
        headers={
            **ADMIN,
            "If-Match": blocked.headers["etag"],
            "Idempotency-Key": "raise-monthly",
        },
        json={
            **_limits(
                100,
                0,
                0,
                monthly={
                    "usd": 8,
                    "input_tokens": 0,
                    "output_tokens": 0,
                },
            ),
            "reason": "approved increase",
        },
    )
    assert raised.status_code == 200
    assert raised.json()["user"]["status"] == "active"


def test_limit_idempotency_replay_returns_original_canonical_result(client):
    api, store, _ = client
    _seed_user(store, "alice", "Alice", 1, 100, 50)
    first_headers = {
        **ADMIN,
        "If-Match": '"1"',
        "Idempotency-Key": "original-limit-change",
    }
    first = api.put(
        "/admin/user/limits",
        params={"user_id": "alice"},
        headers=first_headers,
        json={**_limits(2), "reason": "first"},
    )
    second = api.put(
        "/admin/user/limits",
        params={"user_id": "alice"},
        headers={
            **ADMIN,
            "If-Match": first.headers["etag"],
            "Idempotency-Key": "later-limit-change",
        },
        json={**_limits(3), "reason": "second"},
    )
    replay = api.put(
        "/admin/user/limits",
        params={"user_id": "alice"},
        headers=first_headers,
        json={**_limits(2), "reason": "first"},
    )

    assert second.status_code == 200
    assert second.json()["user"]["limits"]["daily"]["usd"] == 3
    assert replay.status_code == 200
    assert replay.headers["etag"] == first.headers["etag"]
    assert replay.json() == first.json()


def test_period_usage_and_history_include_calendar_boundaries(client):
    api, store, _ = client
    _seed_user(store, "alice", "Alice", 100, 0, 0)
    now = datetime.now(timezone.utc)
    store._usage.put_item(  # noqa: SLF001 - focused API history setup
        Item={
            "user_id": "alice",
            "window": now.date().isoformat(),
            "cost_micro": 2_000_000,
            "input_tokens": 20,
            "output_tokens": 10,
            "requests": 1,
        }
    )

    current = api.get(
        "/admin/user/usage",
        params={"user_id": "alice", "period": "monthly"},
        headers=ADMIN,
    )
    assert current.status_code == 200
    assert current.json()["period"] == "monthly"
    assert current.json()["window_start"].endswith("T00:00:00+00:00")
    assert current.json()["resets_at"] == current.json()["window_end"]
    assert current.json()["cost_usd"] == 2

    history = api.get(
        "/admin/user/usage-history",
        params={
            "user_id": "alice",
            "period": "monthly",
            "start": now.replace(day=1).date().isoformat(),
            "end": now.replace(day=1).date().isoformat(),
        },
        headers=ADMIN,
    )
    assert history.status_code == 200
    assert history.json()["period"] == "monthly"
    assert history.json()["usage"][0]["period"] == "monthly"
    assert history.json()["usage"][0]["cost_usd"] == 2

    misaligned = api.get(
        "/admin/user/usage-history",
        params={
            "user_id": "alice",
            "period": "weekly",
            "start": now.date().isoformat(),
            "end": now.date().isoformat(),
        },
        headers=ADMIN,
    )
    if now.weekday() == 0:
        assert misaligned.status_code == 200
    else:
        assert misaligned.status_code == 400
        assert "weekly period start" in misaligned.json()["error"]["message"]


def test_lightweight_user_list_omits_period_queries_for_lease_poll(client):
    api, store, _ = client
    _seed_user(store, "alice", "Alice", 1, 100, 50)

    response = api.get(
        "/admin/users",
        params={"include_usage": "false", "limit": 50},
        headers=ADMIN,
    )

    assert response.status_code == 200
    row = response.json()["users"][0]
    assert row["user_id"] == "alice"
    assert "today" not in row
    assert "current_usage" not in row


# ---------------------------------------------------------------------------
# Layered enforcement: runtime dial + block/unblock race coherence
# ---------------------------------------------------------------------------


def test_enforcement_dial_validates_and_audits(client, fake_dynamodb):
    api, _, _ = client

    listed = api.get("/admin/enforcement", headers=ADMIN).json()
    assert listed["valid_permission_lease_seconds"] == [60, 300, 900]
    assert listed["default_permission_lease_seconds"] == 300
    assert listed["source"] == "deployment_default"

    invalid = api.put(
        "/admin/enforcement",
        headers=ADMIN,
        json={"permission_lease_seconds": 120, "reason": "nope"},
    )
    assert invalid.status_code == 400
    assert "must be one of" in invalid.json()["error"]["message"]

    not_an_int = api.put(
        "/admin/enforcement",
        headers=ADMIN,
        json={"permission_lease_seconds": "60", "reason": "strings no"},
    )
    assert not_an_int.status_code == 400

    ok = api.put(
        "/admin/enforcement",
        headers=ADMIN,
        json={"permission_lease_seconds": 60, "reason": "live demo: tighter lease"},
    )
    assert ok.status_code == 200
    assert ok.json()["generation"] == 1

    # House convention: a missing reason is stored as "not provided"
    # (consistent with every other admin mutation), it is not rejected.
    defaulted = api.put(
        "/admin/enforcement",
        headers=ADMIN,
        json={"permission_lease_seconds": 300},
    )
    assert defaulted.status_code == 200
    assert defaulted.json()["generation"] == 2

    # Immutable audit trail: the change writes a CONFIG#ENFORCEMENT_AUDIT row.
    table = fake_dynamodb.Table("users-test")
    audit_rows = sorted(
        (
            item
            for item in table.scan()["Items"]
            if str(item["user_id"]).startswith("CONFIG#ENFORCEMENT_AUDIT#")
        ),
        key=lambda item: int(item["generation"]),
    )
    assert len(audit_rows) == 2
    assert audit_rows[0]["permission_lease_seconds"] == 60
    assert audit_rows[0]["previous_permission_lease_seconds"] == 300
    assert audit_rows[0]["reason"] == "live demo: tighter lease"


def test_enforcement_dial_is_versioned_and_idempotent(client, monkeypatch):
    api, _, _ = client
    metric = []
    monkeypatch.setattr(
        gateway.emf,
        "record_enforcement_dial",
        lambda actor, seconds: metric.append((actor, seconds)),
    )
    headers = {**ADMIN, "If-Match": '"0"', "Idempotency-Key": "dial-change-1"}
    body = {"permission_lease_seconds": 60, "reason": "incident response"}

    first = api.put("/admin/enforcement", headers=headers, json=body)
    replay = api.put("/admin/enforcement", headers=headers, json=body)
    stale = api.put(
        "/admin/enforcement",
        headers={**ADMIN, "If-Match": '"0"', "Idempotency-Key": "dial-change-2"},
        json={"permission_lease_seconds": 900, "reason": "stale operator"},
    )
    reused = api.put(
        "/admin/enforcement",
        headers=headers,
        json={"permission_lease_seconds": 300, "reason": "different request"},
    )

    assert first.status_code == replay.status_code == 200
    assert first.json() == replay.json()
    assert first.headers["etag"] == replay.headers["etag"] == '"1"'
    assert stale.status_code == 409
    assert stale.json()["error"]["type"] == "version_conflict"
    assert stale.headers["etag"] == '"1"'
    assert reused.status_code == 409
    assert reused.json()["error"]["type"] == "idempotency_conflict"
    assert metric == [("admin-shared-key", 60)]


def test_vend_deadline_follows_the_runtime_dial(client):
    api, _, broker = client
    api.put(
        "/admin/enforcement",
        headers=ADMIN,
        json={"permission_lease_seconds": 60, "reason": "tight window"},
    )

    response = _vend(api, make_jwt("alice"))

    assert response.status_code == 200
    deadline = datetime.fromisoformat(response.json()["expiration"])
    remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
    assert 50 <= remaining <= 61


def test_unblock_at_vend_rewrites_the_revocation_sentinel(
    client, fake_dynamodb
):
    """Race coherence between the two enforcement paths.

    An automatic block writes the row + sentinel (deny shards gain the
    identity). When the window resets, the vend-path unblock must rewrite
    the sentinel so the revocation fast path strips the deny; otherwise a
    freshly-unblocked user would stay IAM-denied until the repair schedule.
    """
    api, store, _ = client
    vend_before = api.post(
        "/v1/credentials",
        headers={
            "Authorization": f"Bearer {make_jwt('alice')}",
            "X-Quota-Lease-Id": "lease-race",
        },
    )
    assert vend_before.status_code == 200
    store.set_user_status(
        "alice",
        "blocked",
        "auto: quota exhausted in 2026-09-08",
        origin="automatic",
    )
    table = fake_dynamodb.Table("users-test")
    sentinel = table.get_item(Key={"user_id": "REVOCATION#alice"}).get(
        "Item"
    )
    assert sentinel and sentinel["desired_status"] == "blocked"

    # New window, usage table empty: the vend-path refresh_auto_status
    # lifts the automatic block and must re-fire the revocation fast path.
    # Same lease ID: a valid non-extending retry of the fixed deadline.
    recovered = api.post(
        "/v1/credentials",
        headers={
            "Authorization": f"Bearer {make_jwt('alice')}",
            "X-Quota-Lease-Id": "lease-race",
        },
    )

    assert recovered.status_code == 200
    assert store.get_user("alice").status == "active"
    sentinel = table.get_item(Key={"user_id": "REVOCATION#alice"}).get(
        "Item"
    )
    assert sentinel["desired_status"] == "active"


# ---------------------------------------------------------------------------
# Thresholds list, alert-only budgets, and rpm/tpm through the admin API
# ---------------------------------------------------------------------------


def test_admin_api_round_trips_thresholds_and_rate(client):
    api, store, _ = client
    created = api.post(
        "/admin/users",
        json={
            "user_id": "alice",
            "limits": {
                "daily": {
                    "usd": 10,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "thresholds": [
                        {"at": 0.5, "action": "warn"},
                        {"at": 0.9, "action": "warn"},
                        {"at": 1.25, "action": "block"},
                    ],
                },
                "weekly": {
                    "usd": 40,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "thresholds": [{"at": 1.0, "action": "warn"}],
                },
                "monthly": None,
            },
            "rate": {"rpm": 60, "tpm": 100_000},
        },
        headers={**ADMIN, "Idempotency-Key": "create-thresholds"},
    )
    assert created.status_code == 200, created.text
    body = created.json()["user"]
    assert body["limits"]["daily"]["thresholds"] == [
        {"at": 0.5, "action": "warn"},
        {"at": 0.9, "action": "warn"},
        {"at": 1.25, "action": "block"},
    ]
    assert body["limits"]["weekly"]["thresholds"] == [
        {"at": 1.0, "action": "warn"}
    ]
    assert body["rate"] == {"rpm": 60, "tpm": 100_000}
    assert store.get_user("alice").rpm == 60

    # Re-submitting a period WITHOUT thresholds keeps the stored list.
    updated = api.put(
        "/admin/user/limits",
        params={"user_id": "alice"},
        json={"limits": {"daily": {"usd": 20, "input_tokens": 0, "output_tokens": 0}}},
        headers={**ADMIN, "If-Match": '"1"', "Idempotency-Key": "keep-thresholds"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["limits"]["daily"]["usd"] == 20.0
    assert updated.json()["limits"]["daily"]["thresholds"][2]["at"] == 1.25
    assert updated.json()["user"]["rate"] == {"rpm": 60, "tpm": 100_000}

    # A rate-only update is a valid mutation; null disables both.
    dropped = api.put(
        "/admin/user/limits",
        params={"user_id": "alice"},
        json={"rate": None, "reason": "remove rate limits"},
        headers={**ADMIN, "If-Match": '"2"', "Idempotency-Key": "drop-rate"},
    )
    assert dropped.status_code == 200, dropped.text
    assert dropped.json()["user"]["rate"] is None
    assert dropped.json()["user"]["version"] == 3
    events, _ = store.list_admin_audit_page(user_id="alice", limit=10)
    assert events[0]["after"]["rate"] == {"rpm": 0, "tpm": 0}
    assert events[0]["before"]["rate"] == {"rpm": 60, "tpm": 100_000}


@pytest.mark.parametrize(
    ("thresholds", "message"),
    [
        (
            [{"at": 0.8, "action": "block"}, {"at": 1.0, "action": "block"}],
            "block' entry must be the last",
        ),
        (
            [{"at": 1.0, "action": "block"}, {"at": 1.5, "action": "warn"}],
            "block' entry must be the last",
        ),
        (
            [{"at": 0.8, "action": "warn"}, {"at": 0.8, "action": "block"}],
            "strictly increasing",
        ),
        (
            [{"at": 0.9, "action": "warn"}, {"at": 0.5, "action": "warn"}],
            "strictly increasing",
        ),
        ([{"at": 0, "action": "warn"}], "greater than 0"),
        ([{"at": 11, "action": "block"}], "at most 10"),
        ([{"at": 0.5, "action": "notify"}], "action must be one of"),
        ([], "non-empty list"),
        ([{"at": 0.5}], "action must be one of"),
        ([{"at": 0.5, "action": "warn", "extra": 1}], "unknown keys"),
    ],
)
def test_admin_api_rejects_invalid_thresholds(client, thresholds, message):
    api, store, _ = client
    _seed_user(store, "alice", "Alice", 1, 100, 50)
    response = api.put(
        "/admin/user/limits",
        params={"user_id": "alice"},
        json={
            "limits": {
                "daily": {
                    "usd": 1,
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "thresholds": thresholds,
                }
            }
        },
        headers=ADMIN,
    )
    assert response.status_code == 400
    assert message in response.json()["error"]["message"]


@pytest.mark.parametrize(
    "rate",
    [{"rpm": -1}, {"rpm": 1.5}, {"rpm": True}, {"rps": 1}, "fast", {"rpm": "6"}],
)
def test_admin_api_rejects_invalid_rate(client, rate):
    api, store, _ = client
    _seed_user(store, "alice", "Alice", 1, 100, 50)
    response = api.put(
        "/admin/user/limits",
        params={"user_id": "alice"},
        json={"rate": rate},
        headers=ADMIN,
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_alert_only_budget_vends_far_over_limit(client):
    api, store, _ = client
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
    store._usage.put_item(  # noqa: SLF001
        Item={"user_id": "alice", "window": current_window(), "cost_micro": 40 * MICRO}
    )
    vend = _vend(api, make_jwt("alice"))
    assert vend.status_code == 200, vend.text
    assert store.get_user("alice").status == "active"


def test_block_threshold_above_limit_is_honoured_at_vend(client):
    api, store, _ = client
    store.put_user(
        "alice",
        "Alice",
        limits={
            "daily": {
                "usd": 1,
                "input_tokens": 0,
                "output_tokens": 0,
                "thresholds": [{"at": 1.5, "action": "block"}],
            },
            "weekly": None,
            "monthly": None,
        },
    )
    store._usage.put_item(  # noqa: SLF001
        Item={"user_id": "alice", "window": current_window(), "cost_micro": int(1.4 * MICRO)}
    )
    assert _vend(api, make_jwt("alice")).status_code == 200
    store._usage.put_item(  # noqa: SLF001
        Item={"user_id": "alice", "window": current_window(), "cost_micro": int(1.5 * MICRO)}
    )
    blocked = _vend(api, make_jwt("alice"))
    assert blocked.status_code == 429
    assert blocked.headers["x-quota-breached-period"] == "daily"
    assert "at 150%" in store.get_user("alice").status_reason


def test_rate_limited_subject_is_refused_at_vend_and_recovers_next_minute(client):
    api, store, _ = client
    store.put_user(
        "alice",
        "Alice",
        limits={
            "daily": {"usd": 100, "input_tokens": 0, "output_tokens": 0},
            "weekly": None,
            "monthly": None,
            "rate": {"rpm": 5, "tpm": 0},
        },
    )
    now = datetime.now(timezone.utc)
    minute = now.strftime("%Y-%m-%dT%H:%M")
    store._usage.put_item(  # noqa: SLF001 - what the metering processor writes
        Item={"user_id": "RATE#alice", "window": minute, "requests": 5, "tokens": 10}
    )
    blocked = _vend(api, make_jwt("alice"))
    assert blocked.status_code == 429
    assert blocked.headers["x-quota-breached-period"] == "minute"
    assert blocked.headers["x-quota-breached-dimension"] == "rpm"
    user = store.get_user("alice")
    assert user.status == "blocked"
    assert user.status_origin == "automatic"
    assert user.status_reason.startswith("auto: rpm rate limit reached")

    # Pretend the minute has rolled: the counter row for "now" is gone.
    store._usage.items.pop(("RATE#alice", minute))  # noqa: SLF001
    recovered = _vend(api, make_jwt("alice"))
    assert recovered.status_code == 200, recovered.text
    assert store.get_user("alice").status == "active"


# ---------------------------------------------------------------------------
# Per-model budgets through the admin API
# ---------------------------------------------------------------------------


def _opus_budget(usd: float = 2.0) -> dict:
    return {
        "limits": {
            "daily": {"usd": usd, "input_tokens": 0, "output_tokens": 0},
            "weekly": None,
            "monthly": None,
        }
    }


def test_model_budget_crud_with_reason_idempotency_and_etag(client):
    api, store, _ = client
    _seed_user(store, "alice", "Alice", 100, 0, 0)

    created = api.put(
        "/admin/user/model-budget",
        params={"user_id": "alice", "model_id": "anthropic.claude-opus-4-7"},
        json={**_opus_budget(2), "reason": "  Opus is expensive  "},
        headers={**ADMIN, "If-Match": '"1"', "Idempotency-Key": "opus-1"},
    )
    assert created.status_code == 200, created.text
    assert created.headers["etag"] == '"2"'
    body = created.json()
    assert body["updated"] is True
    assert body["model_id"] == "anthropic.claude-opus-4-7"
    assert body["model_budgets"]["anthropic.claude-opus-4-7"]["daily"] == {
        "usd": 2.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "thresholds": DEFAULT_THRESHOLDS,
    }
    assert body["user"]["model_budgets"] == body["model_budgets"]
    assert body["user"]["limits"]["daily"]["usd"] == 100.0  # untouched

    # Exact replay is stable; changed content under the same key conflicts.
    replay = api.put(
        "/admin/user/model-budget",
        params={"user_id": "alice", "model_id": "anthropic.claude-opus-4-7"},
        json={**_opus_budget(2), "reason": "Opus is expensive"},
        headers={**ADMIN, "If-Match": '"1"', "Idempotency-Key": "opus-1"},
    )
    assert replay.status_code == 200 and replay.json() == body
    mismatch = api.put(
        "/admin/user/model-budget",
        params={"user_id": "alice", "model_id": "anthropic.claude-opus-4-7"},
        json={**_opus_budget(3), "reason": "Opus is expensive"},
        headers={**ADMIN, "If-Match": '"1"', "Idempotency-Key": "opus-1"},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["error"]["type"] == "idempotency_conflict"

    # Stale If-Match.
    stale = api.put(
        "/admin/user/model-budget",
        params={"user_id": "alice", "model_id": "anthropic.claude-opus-4-7"},
        json=_opus_budget(5),
        headers={**ADMIN, "If-Match": '"1"', "Idempotency-Key": "opus-stale"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["type"] == "version_conflict"
    assert stale.headers["etag"] == '"2"'

    # Detail shows the budget; the audit trail has before/after with budgets.
    detail = api.get("/admin/user", params={"user_id": "alice"}, headers=ADMIN)
    assert "anthropic.claude-opus-4-7" in detail.json()["user"]["model_budgets"]
    events, _ = store.list_admin_audit_page(user_id="alice", limit=10)
    assert events[0]["event_type"] == "user.model_budget.updated"
    assert events[0]["reason"] == "Opus is expensive"

    # Second model budget, then per-model usage endpoint.
    second = api.put(
        "/admin/user/model-budget",
        params={"user_id": "alice", "model_id": "openai.gpt-oss-20b"},
        json=_opus_budget(1),
        headers={**ADMIN, "If-Match": '"2"', "Idempotency-Key": "gpt-1"},
    )
    assert second.status_code == 200
    assert sorted(second.json()["model_budgets"]) == [
        "anthropic.claude-opus-4-7",
        "openai.gpt-oss-20b",
    ]
    store._usage.put_item(  # noqa: SLF001 - what the processor writes
        Item={"user_id": "alice#model#openai.gpt-oss-20b",
              "window": current_window(), "cost_micro": 250_000, "requests": 3}
    )
    usage = api.get(
        "/admin/user/model-usage",
        params={"user_id": "alice", "model_id": "openai.gpt-oss-20b"},
        headers=ADMIN,
    )
    assert usage.status_code == 200
    assert usage.json()["current_usage"]["daily"]["cost_usd"] == 0.25
    assert usage.json()["current_usage"]["daily"]["requests"] == 3

    # Remove one; the other remains.
    removed = api.delete(
        "/admin/user/model-budget",
        params={"user_id": "alice", "model_id": "anthropic.claude-opus-4-7"},
        headers={**ADMIN, "If-Match": '"3"', "Idempotency-Key": "opus-rm"},
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["removed"] is True
    assert list(removed.json()["model_budgets"]) == ["openai.gpt-oss-20b"]
    missing = api.delete(
        "/admin/user/model-budget",
        params={"user_id": "alice", "model_id": "anthropic.claude-opus-4-7"},
        headers={**ADMIN, "If-Match": '"4"', "Idempotency-Key": "opus-rm-2"},
    )
    assert missing.status_code == 404


def test_model_budget_rejects_models_outside_the_allowlist(client, monkeypatch):
    api, store, _ = client
    from dataclasses import replace as dc_replace

    monkeypatch.setattr(
        gateway,
        "settings",
        dc_replace(
            gateway.settings,
            allowed_model_arns_json=json.dumps([
                "arn:aws:bedrock:us-east-1::foundation-model/openai.gpt-oss-20b",
                "arn:aws:bedrock:us-east-1:111122223333:inference-profile/us.anthropic.claude-opus-4-7",
                "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-*",
            ]),
        ),
    )
    _seed_user(store, "alice", "Alice", 100, 0, 0)

    denied = api.put(
        "/admin/user/model-budget",
        params={"user_id": "alice", "model_id": "meta.llama3-3-70b-instruct-v1:0"},
        json=_opus_budget(),
        headers={**ADMIN, "Idempotency-Key": "llama"},
    )
    assert denied.status_code == 400
    assert "allowed_model_arns" in denied.json()["error"]["message"]

    for allowed in ("openai.gpt-oss-20b", "us.anthropic.claude-opus-4-7", "amazon.nova-lite-v1:0"):
        response = api.put(
            "/admin/user/model-budget",
            params={"user_id": "alice", "model_id": allowed},
            json=_opus_budget(),
            headers={**ADMIN, "Idempotency-Key": f"ok-{allowed}"},
        )
        assert response.status_code == 200, (allowed, response.text)


@pytest.mark.parametrize(
    ("model_id", "body", "message"),
    [
        ("arn:aws:bedrock:us-east-1::foundation-model/x", _opus_budget(), "not an ARN"),
        ("a#model#b", _opus_budget(), "reserved"),
        ("", _opus_budget(), "non-empty"),
        ("opus", {"limits": {"daily": None, "weekly": None, "monthly": None}}, "at least one quota period"),
        ("opus", {"limits": {"daily": {"usd": 1, "input_tokens": 0, "output_tokens": 0}, "rate": {"rpm": 1}}}, "rate limits"),
        ("opus", {}, "limits is required"),
    ],
)
def test_model_budget_validation(client, model_id, body, message):
    api, store, _ = client
    _seed_user(store, "alice", "Alice", 100, 0, 0)
    response = api.put(
        "/admin/user/model-budget",
        params={"user_id": "alice", "model_id": model_id},
        json=body,
        headers=ADMIN,
    )
    assert response.status_code == 400, response.text
    assert message in response.json()["error"]["message"]


def test_model_budget_below_current_model_usage_blocks_immediately_and_lifts_on_removal(client):
    api, store, _ = client
    _seed_user(store, "alice", "Alice", 100, 0, 0)
    store._usage.put_item(  # noqa: SLF001
        Item={"user_id": "alice#model#opus", "window": current_window(), "cost_micro": 5 * MICRO}
    )
    store._usage.put_item(  # noqa: SLF001
        Item={"user_id": "alice", "window": current_window(), "cost_micro": 5 * MICRO}
    )
    blocked = api.put(
        "/admin/user/model-budget",
        params={"user_id": "alice", "model_id": "opus"},
        json={**_opus_budget(2), "reason": "clamp opus"},
        headers={**ADMIN, "If-Match": '"1"', "Idempotency-Key": "clamp"},
    )
    assert blocked.status_code == 200, blocked.text
    user = blocked.json()["user"]
    assert user["status"] == "blocked"
    assert user["status_origin"] == "automatic"
    assert "for model opus" in user["status_reason"]
    assert _vend(api, make_jwt("alice")).status_code == 403

    lifted = api.request(
        "DELETE",
        "/admin/user/model-budget",
        params={"user_id": "alice", "model_id": "opus"},
        json={"reason": "lift the clamp"},
        headers={**ADMIN, "If-Match": '"2"', "Idempotency-Key": "unclamp"},
    )
    assert lifted.status_code == 200, lifted.text
    assert lifted.json()["user"]["status"] == "active"
    assert _vend(api, make_jwt("alice")).status_code == 200


def test_vend_preflight_ignores_model_budgets_by_design(client):
    """At vend time the model is unknown, so pre-flight is subject-level.
    A subject whose model budget is NOT yet breached vends normally even
    when a model ledger is close to its cap."""
    api, store, _ = client
    _seed_user(store, "alice", "Alice", 100, 0, 0)
    store.update_admin_model_budget(
        "alice", "opus", _opus_budget(2)["limits"], reason="cap",
        expected_version=1, actor="admin", auth_method="shared-key",
        idempotency_key="cap", request_hash="h",
    )
    store._usage.put_item(  # noqa: SLF001 - 99 % of the opus budget
        Item={"user_id": "alice#model#opus", "window": current_window(), "cost_micro": 1_980_000}
    )
    vend = _vend(api, make_jwt("alice"))
    assert vend.status_code == 200
    assert "X-Quota-Breached-Period" not in vend.headers


def test_reconciliation_disabled_returns_explicit_payload(client, monkeypatch):
    api, _, _ = client
    monkeypatch.setattr(
        gateway, "settings", replace(gateway.settings, reconciliation_enabled=False)
    )
    response = api.get("/admin/reconciliation", headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["runs"] == []
    assert "reconciliation_enabled=true" in body["message"]


def test_reconciliation_requires_admin(client):
    api, _, _ = client
    assert api.get("/admin/reconciliation").status_code == 403


def test_reconciliation_lists_stored_runs_newest_first(client, monkeypatch):
    """The broker serves stored RECONCILE# rows and never calls Cost Explorer;
    ordinary ledger rows sharing the usage table are ignored."""
    api, store, _ = client
    monkeypatch.setattr(
        gateway,
        "settings",
        replace(gateway.settings, reconciliation_enabled=True, reconcile_lag_days=2),
    )
    from decimal import Decimal

    for day, billed in (("2026-09-10", "1.5"), ("2026-09-12", "3.25"), ("2026-09-11", "2")):
        store._usage.put_item(  # noqa: SLF001
            Item={
                "user_id": f"RECONCILE#{day}",
                "window": day,
                "run_at": f"{day}T06:00:00+00:00",
                "result": {
                    "day": day,
                    "aggregate": {
                        "estimated_usd": Decimal("1"),
                        "billed_usd": Decimal(billed),
                        "delta_usd": Decimal(billed) - 1,
                        "delta_percent": Decimal("10.5"),
                    },
                    "workloads": [
                        {"name": "payments", "tag_inactive": True, "billed_usd": Decimal("0")}
                    ],
                    "tag_inactive_workloads": ["payments"],
                },
                "expires_at": 4_000_000_000,
            }
        )
    store._usage.put_item(  # noqa: SLF001 - normal ledger row, must not leak in
        Item={"user_id": "alice", "window": "2026-09-12", "cost_micro": 5}
    )

    response = api.get("/admin/reconciliation", headers=ADMIN, params={"limit": 2})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is True
    assert body["lag_days"] == 2
    assert [run["day"] for run in body["runs"]] == ["2026-09-12", "2026-09-11"]
    latest = body["latest"]
    assert latest["day"] == "2026-09-12"
    assert latest["run_at"] == "2026-09-12T06:00:00+00:00"
    # Decimals are rendered as JSON numbers, ints stay ints.
    assert latest["aggregate"]["billed_usd"] == 3.25
    assert latest["aggregate"]["estimated_usd"] == 1
    assert latest["aggregate"]["delta_percent"] == 10.5
    assert latest["tag_inactive_workloads"] == ["payments"]
    assert latest["workloads"][0]["tag_inactive"] is True
