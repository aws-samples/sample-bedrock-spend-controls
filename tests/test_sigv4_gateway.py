from uuid import UUID

import httpx
import pytest
from botocore.credentials import Credentials

from examples.sigv4_gateway import (
    FunctionUrlSigV4Auth,
    _admin_request_args,
    _emit_response,
    _execute_admin_command,
    _parser,
    signed_request,
)


def test_httpx_auth_preserves_user_token_outside_authorization():
    auth = FunctionUrlSigV4Auth(
        "user-jwt",
        region="us-east-1",
        credentials=Credentials("AKID", "SECRET", "SESSION"),
    )
    request = httpx.Request(
        "POST",
        "https://example.lambda-url.us-east-1.on.aws/v1/credentials",
        headers={"Authorization": "Bearer sdk-api-key"},
        content=b"",
    )

    signed = next(auth.auth_flow(request))

    assert signed.headers["X-Quota-User-Token"] == "user-jwt"
    assert signed.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert signed.headers["X-Amz-Security-Token"] == "SESSION"


def test_signed_admin_request_keeps_admin_key_outside_authorization():
    class AwsSession:
        def get_credentials(self):
            return Credentials("AKID", "SECRET", "SESSION")

    captured = {}

    def handler(request):
        captured["request"] = request
        return httpx.Response(200, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = signed_request(
            "POST",
            "https://example.lambda-url.us-east-1.on.aws/admin/users",
            region="us-east-1",
            admin_key="admin-secret",
            aws_session=AwsSession(),
            http_client=client,
            timeout=17,
            json={"user_id": "alice"},
        )

    assert response.status_code == 200
    request = captured["request"]
    assert request.headers["X-Quota-Admin-Key"] == "admin-secret"
    assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
    assert request.extensions["timeout"] == {
        "connect": 17,
        "read": 17,
        "write": 17,
        "pool": 17,
    }


def test_signed_exact_user_query_is_encoded_once_before_signing():
    class AwsSession:
        def get_credentials(self):
            return Credentials("AKID", "SECRET", "SESSION")

    user_id = "tenant/audit/usage and status?#café"
    captured = {}

    def handler(request):
        captured["request"] = request
        return httpx.Response(200, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        signed_request(
            "GET",
            "https://example.lambda-url.us-east-1.on.aws/admin/user/usage",
            region="us-east-1",
            admin_key="admin-secret",
            aws_session=AwsSession(),
            http_client=client,
            params={"user_id": user_id, "window": "2026-09-02"},
        )

    request = captured["request"]
    assert request.url.path == "/admin/user/usage"
    assert request.url.params["user_id"] == user_id
    assert request.url.params["window"] == "2026-09-02"
    assert "%252F" not in str(request.url)
    assert request.headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")


def test_signed_emergency_request_uses_separate_break_glass_header():
    class AwsSession:
        def get_credentials(self):
            return Credentials("AKID", "SECRET", "SESSION")

    captured = {}

    def handler(request):
        captured["request"] = request
        return httpx.Response(202, request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        signed_request(
            "POST",
            "https://example.lambda-url.us-east-1.on.aws/admin/emergency-stop",
            region="us-east-1",
            emergency_key="break-glass-secret",
            aws_session=AwsSession(),
            http_client=client,
            json={"action": "activate"},
        )

    request = captured["request"]
    assert request.headers["X-Quota-Emergency-Key"] == "break-glass-secret"
    assert "X-Quota-Admin-Key" not in request.headers


def test_admin_cli_create_and_update_include_all_quota_dimensions():
    create = _parser().parse_args([
        "--gateway-url", "https://example.test",
        "--admin-key", "secret",
        "create-user", "tenant/audit/usage",
        "--daily-usd", "25",
        "--daily-input-tokens", "1000000",
        "--daily-output-tokens", "200000",
    ])
    method, url, kwargs = _admin_request_args(create)
    assert (method, url) == ("POST", "https://example.test/admin/users")
    UUID(kwargs["headers"]["Idempotency-Key"])
    assert kwargs["json"] == {
        "user_id": "tenant/audit/usage",
        "name": "tenant/audit/usage",
        "limits": {
            "daily": {
                "usd": 25.0,
                "input_tokens": 1_000_000,
                "output_tokens": 200_000,
            },
            "weekly": None,
            "monthly": None,
        },
    }

    update = _parser().parse_args([
        "--gateway-url", "https://example.test",
        "--admin-key", "secret",
        "update-user", "tenant/audit/usage",
        "--daily-usd", "30",
        "--daily-input-tokens", "2000000",
        "--daily-output-tokens", "400000",
    ])
    update.current_limits = {
        "daily": {"usd": 25, "input_tokens": 1_000_000, "output_tokens": 200_000},
        "weekly": None,
        "monthly": None,
    }
    method, url, kwargs = _admin_request_args(
        update,
        idempotency_key="00000000-0000-4000-8000-000000000011",
        if_match='"7"',
    )
    assert method == "PUT"
    assert url == "https://example.test/admin/user/limits"
    assert kwargs["params"] == {"user_id": "tenant/audit/usage"}
    assert kwargs["headers"] == {
        "Idempotency-Key": "00000000-0000-4000-8000-000000000011",
        "If-Match": '"7"',
    }
    assert kwargs["json"] == {
        "limits": {
            "daily": {
                "usd": 30.0,
                "input_tokens": 2_000_000,
                "output_tokens": 400_000,
            },
            "weekly": None,
            "monthly": None,
        },
    }
    assert "reason" not in kwargs["json"]

    reasoned_update = _parser().parse_args([
        "--gateway-url", "https://example.test",
        "--admin-key", "secret",
        "update-user", "tenant/audit/usage",
        "--daily-usd", "35",
        "--reason", "Annual allocation",
    ])
    reasoned_update.current_limits = kwargs["json"]["limits"]
    _, _, reasoned_kwargs = _admin_request_args(
        reasoned_update,
        idempotency_key="00000000-0000-4000-8000-000000000012",
        if_match='"8"',
    )
    assert reasoned_kwargs["json"] == {
        "limits": {
            "daily": {
                "usd": 35.0,
                "input_tokens": 2_000_000,
                "output_tokens": 400_000,
            },
            "weekly": None,
            "monthly": None,
        },
        "reason": "Annual allocation",
    }

    usage = _parser().parse_args([
        "--gateway-url", "https://example.test",
        "--admin-key", "secret",
        "get-usage", "team/audit/usage",
        "--window", "2026-09-02",
    ])
    method, url, usage_kwargs = _admin_request_args(usage)
    assert (method, url) == ("GET", "https://example.test/admin/user/usage")
    assert usage_kwargs["params"] == {
        "user_id": "team/audit/usage",
        "period": "daily",
        "window": "2026-09-02",
    }


def test_admin_cli_update_reason_does_not_replace_a_quota_option():
    update = _parser().parse_args([
        "--gateway-url", "https://example.test",
        "--admin-key", "secret",
        "update-user", "tenant/audit/usage",
        "--reason", "No quota supplied",
    ])
    update.current_limits = {
        "daily": {"usd": 1, "input_tokens": 1, "output_tokens": 1},
        "weekly": None,
        "monthly": None,
    }

    with pytest.raises(
        ValueError,
        match="update-user requires at least one quota option",
    ):
        _admin_request_args(update, if_match='"7"')


def test_admin_cli_supports_optional_weekly_and_monthly_periods():
    create = _parser().parse_args([
        "--gateway-url", "https://example.test",
        "create-user", "tenant/calendar",
        "--daily-usd", "1",
        "--daily-input-tokens", "100",
        "--daily-output-tokens", "50",
        "--weekly-usd", "5",
        "--weekly-input-tokens", "500",
        "--weekly-output-tokens", "250",
        "--monthly-usd", "20",
        "--monthly-input-tokens", "2000",
        "--monthly-output-tokens", "1000",
    ])
    _, _, create_kwargs = _admin_request_args(create)
    assert create_kwargs["json"]["limits"]["weekly"] == {
        "usd": 5.0,
        "input_tokens": 500,
        "output_tokens": 250,
    }
    assert create_kwargs["json"]["limits"]["monthly"]["usd"] == 20.0

    update = _parser().parse_args([
        "--gateway-url", "https://example.test",
        "update-user", "tenant/calendar", "--disable-monthly",
    ])
    update.current_limits = create_kwargs["json"]["limits"]
    _, _, update_kwargs = _admin_request_args(update, if_match='"1"')
    assert update_kwargs["json"]["limits"]["monthly"] is None
    assert update_kwargs["json"]["limits"]["weekly"]["usd"] == 5.0


def test_admin_cli_emergency_commands_include_explicit_confirmation():
    stop = _parser().parse_args([
        "--gateway-url", "https://example.test",
        "--admin-key", "secret",
        "emergency-stop", "--reason", "security incident",
    ])
    method, url, kwargs = _admin_request_args(stop)
    assert (method, url) == (
        "POST",
        "https://example.test/admin/emergency-stop",
    )
    assert kwargs["json"] == {
        "action": "activate",
        "confirmation": "STOP_ALL_BEDROCK_SESSIONS",
        "reason": "security incident",
    }

    recover = _parser().parse_args([
        "--gateway-url", "https://example.test",
        "--admin-key", "secret",
        "emergency-recover", "--reason", "incident resolved",
    ])
    _, _, kwargs = _admin_request_args(recover)
    assert kwargs["json"]["action"] == "recover"
    assert kwargs["json"]["confirmation"] == (
        "RESTORE_ALL_BEDROCK_SESSIONS"
    )


def _json_response(status_code, url, payload, *, headers=None):
    request = httpx.Request("GET", url)
    return httpx.Response(
        status_code,
        headers=headers,
        json=payload,
        request=request,
    )


def test_versioned_update_fetches_detail_then_sends_etag_and_uuid():
    args = _parser().parse_args([
        "--gateway-url", "https://example.test",
        "--region", "us-east-1",
        "--admin-key", "secret",
        "update-user", "tenant/audit/usage",
        "--daily-usd", "30",
        "--reason", "Reviewed increase",
    ])
    calls = []

    def request_fn(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if method == "GET":
            return _json_response(
                200,
                url,
                {"user": {"user_id": "tenant/audit/usage", "version": 7, "limits": {"daily": {"usd": 25, "input_tokens": 1_000_000, "output_tokens": 200_000}, "weekly": None, "monthly": None}}},
                headers={"ETag": '"7"'},
            )
        return _json_response(200, url, {"updated": True})

    response = _execute_admin_command(args, object(), request_fn=request_fn)

    assert response.status_code == 200
    assert [(method, url) for method, url, _ in calls] == [
        ("GET", "https://example.test/admin/user"),
        ("PUT", "https://example.test/admin/user/limits"),
    ]
    assert calls[0][2]["params"] == {"user_id": "tenant/audit/usage"}
    mutation = calls[1][2]
    assert mutation["params"] == {"user_id": "tenant/audit/usage"}
    assert mutation["headers"]["If-Match"] == '"7"'
    UUID(mutation["headers"]["Idempotency-Key"])
    assert mutation["json"] == {
        "limits": {
            "daily": {
                "usd": 30.0,
                "input_tokens": 1_000_000,
                "output_tokens": 200_000,
            },
            "weekly": None,
            "monthly": None,
        },
        "reason": "Reviewed increase",
    }
    assert mutation["admin_key"] == "secret"
    assert mutation["emergency_key"] is None


@pytest.mark.parametrize(
    ("command", "status", "reason"),
    [
        ("block-user", "blocked", "incident containment"),
        ("unblock-user", "active", "incident resolved"),
    ],
)
def test_status_mutation_uses_version_fallback_and_explicit_reason(
    command, status, reason
):
    args = _parser().parse_args([
        "--gateway-url", "https://example.test",
        "--admin-key", "secret",
        command, "tenant/usage/audit", "--reason", reason,
    ])
    calls = []

    def request_fn(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if method == "GET":
            return _json_response(
                200,
                url,
                {"user": {"user_id": "tenant/usage/audit", "version": 3}},
            )
        return _json_response(200, url, {"status": status})

    _execute_admin_command(args, object(), request_fn=request_fn)

    mutation = calls[1][2]
    assert calls[0][1] == "https://example.test/admin/user"
    assert calls[0][2]["params"] == {"user_id": "tenant/usage/audit"}
    assert calls[1][1] == f"https://example.test/admin/user/status"
    assert mutation["params"] == {"user_id": "tenant/usage/audit"}
    assert mutation["headers"]["If-Match"] == "3"
    UUID(mutation["headers"]["Idempotency-Key"])
    assert mutation["json"] == {"status": status, "reason": reason}


def test_emergency_execution_stays_one_request_without_routine_headers():
    args = _parser().parse_args([
        "--gateway-url", "https://example.test",
        "--emergency-key", "break-glass-secret",
        "emergency-stop", "--reason", "security incident",
    ])
    calls = []

    def request_fn(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return _json_response(202, url, {"state": "activating"})

    _execute_admin_command(args, object(), request_fn=request_fn)

    assert len(calls) == 1
    _, url, kwargs = calls[0]
    assert url == "https://example.test/admin/emergency-stop"
    assert kwargs["admin_key"] is None
    assert kwargs["emergency_key"] == "break-glass-secret"
    assert "headers" not in kwargs
    assert "Idempotency-Key" not in str(kwargs)
    assert "If-Match" not in str(kwargs)


def test_conflict_guidance_is_emitted_and_http_failure_is_preserved(capsys):
    response = _json_response(
        409,
        "https://example.test/admin/user/limits?user_id=tenant%2Faudit",
        {
            "error": {
                "code": "version_conflict",
                "message": "The user configuration changed.",
            }
        },
    )

    with pytest.raises(httpx.HTTPStatusError):
        _emit_response(response)

    captured = capsys.readouterr()
    assert '"code": "version_conflict"' in captured.out
    assert "Review the current user" in captured.err


# ---------------------------------------------------------------------------
# Pre-publication review fixes (M-12 and CLI lows).
# ---------------------------------------------------------------------------

from examples.sigv4_gateway import _parse_thresholds, _rate_payload  # noqa: E402


def _user_detail(url, *, rate=None, version=4):
    return _json_response(
        200,
        url,
        {
            "user": {
                "user_id": "alice",
                "version": version,
                "limits": {
                    "daily": {"usd": 5, "input_tokens": 100, "output_tokens": 50},
                    "weekly": None,
                    "monthly": None,
                },
                "rate": rate,
            }
        },
        headers={"ETag": f'"{version}"'},
    )


@pytest.mark.parametrize(
    ("argv", "current_rate", "expected"),
    [
        (["--rpm", "30"], {"rpm": 10, "tpm": 5000}, {"rpm": 30, "tpm": 5000}),
        (["--tpm", "9000"], {"rpm": 10, "tpm": 5000}, {"rpm": 10, "tpm": 9000}),
        (["--rpm", "30"], None, {"rpm": 30, "tpm": 0}),
        (["--rpm", "0", "--tpm", "0"], {"rpm": 10, "tpm": 5000}, {"rpm": 0, "tpm": 0}),
    ],
)
def test_update_user_rate_merges_with_the_current_rate(argv, current_rate, expected):
    args = _parser().parse_args([
        "--gateway-url", "https://example.test", "update-user", "alice", *argv,
    ])
    calls = []

    def request_fn(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if method == "GET":
            return _user_detail(url, rate=current_rate)
        return _json_response(200, url, {"updated": True})

    args.admin_key = "secret"
    _execute_admin_command(args, object(), request_fn=request_fn)

    mutation = calls[1][2]
    assert mutation["json"]["rate"] == expected
    assert mutation["json"]["limits"]["daily"] == {"usd": 5, "input_tokens": 100, "output_tokens": 50}


def test_disable_rate_still_removes_both_limits():
    args = _parser().parse_args([
        "--gateway-url", "https://example.test", "update-user", "alice", "--disable-rate",
    ])
    assert _rate_payload(args, current={"rpm": 10, "tpm": 5000}) == (None, True)
    assert _rate_payload(args.__class__(), current={"rpm": 10}) == (None, False)


def test_thresholds_are_whole_percentages_between_1_and_1000():
    assert _parse_thresholds("50:warn, 80:warn,100:block") == [
        {"at": 0.5, "action": "warn"},
        {"at": 0.8, "action": "warn"},
        {"at": 1.0, "action": "block"},
    ]
    assert _parse_thresholds("1000:block") == [{"at": 10.0, "action": "block"}]
    with pytest.raises(ValueError, match="whole-number percentage"):
        _parse_thresholds("0.5:warn")
    with pytest.raises(ValueError, match="between 1 and 1000"):
        _parse_thresholds("0:warn")
    with pytest.raises(ValueError, match="between 1 and 1000"):
        _parse_thresholds("1001:block")
    with pytest.raises(ValueError, match="must end in :warn or :block"):
        _parse_thresholds("50:alert")


def test_list_users_accepts_filters_and_pagination_flags():
    args = _parser().parse_args([
        "--gateway-url", "https://example.test", "list-users",
        "--limit", "200", "--cursor", "abc", "--status", "blocked", "--query", "ali",
    ])
    method, url, kwargs = _admin_request_args(args)
    assert (method, url) == ("GET", "https://example.test/admin/users")
    assert kwargs["params"] == {"limit": 200, "cursor": "abc", "status": "blocked", "query": "ali"}

    plain = _parser().parse_args(["--gateway-url", "https://example.test", "list-users"])
    assert _admin_request_args(plain) == ("GET", "https://example.test/admin/users", {})
    with pytest.raises(SystemExit):
        _parser().parse_args(["--gateway-url", "https://example.test", "list-users", "--status", "paused"])


def test_list_users_all_follows_next_cursor_and_merges_pages():
    args = _parser().parse_args([
        "--gateway-url", "https://example.test", "list-users", "--all", "--status", "active",
    ])
    args.admin_key = "secret"
    calls = []
    pages = {
        None: {"users": [{"user_id": "a"}, {"user_id": "b"}], "next_cursor": "c2"},
        "c2": {"users": [{"user_id": "c"}], "next_cursor": "c3"},
        "c3": {"users": [], "next_cursor": None},
    }

    def request_fn(method, url, **kwargs):
        calls.append(kwargs.get("params", {}))
        return _json_response(200, url, pages[kwargs.get("params", {}).get("cursor")])

    response = _execute_admin_command(args, object(), request_fn=request_fn)

    assert response.status_code == 200
    assert response.json() == {
        "users": [{"user_id": "a"}, {"user_id": "b"}, {"user_id": "c"}],
        "next_cursor": None,
        "pages": 3,
    }
    assert [call.get("cursor") for call in calls] == [None, "c2", "c3"]
    assert all(call["status"] == "active" for call in calls)
    response.raise_for_status()  # synthetic response is a complete httpx.Response


def test_list_users_all_returns_a_failing_page_unchanged():
    args = _parser().parse_args(["--gateway-url", "https://example.test", "list-users", "--all"])
    args.admin_key = "secret"
    calls = 0

    def request_fn(method, url, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return _json_response(200, url, {"users": [{"user_id": "a"}], "next_cursor": "bad"})
        return _json_response(400, url, {"error": {"type": "invalid_request_error", "message": "Invalid cursor."}})

    response = _execute_admin_command(args, object(), request_fn=request_fn)
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "Invalid cursor."


def test_secret_flags_warn_on_the_command_line_but_not_from_the_environment(capsys, monkeypatch):
    monkeypatch.delenv("ADMIN_KEY", raising=False)
    args = _parser().parse_args([
        "--gateway-url", "https://example.test", "--admin-key", "shh", "list-users",
    ])
    assert args.admin_key == "shh"
    assert "--admin-key puts the key in your shell history" in capsys.readouterr().err

    monkeypatch.setenv("ADMIN_KEY", "from-env")
    args = _parser().parse_args(["--gateway-url", "https://example.test", "list-users"])
    assert args.admin_key == "from-env"
    assert capsys.readouterr().err == ""

    monkeypatch.setenv("EMERGENCY_ADMIN_KEY", "glass")
    args = _parser().parse_args([
        "--gateway-url", "https://example.test", "emergency-stop", "--reason", "x",
    ])
    assert args.emergency_key == "glass"
    assert capsys.readouterr().err == ""


def test_help_text_prefers_environment_variables_over_key_flags():
    text = _parser().format_help()
    assert "ADMIN_KEY" in text and "EMERGENCY_ADMIN_KEY" in text
    examples = [line for line in text.splitlines() if "python examples/sigv4_gateway.py" in line]
    assert examples, "help should include usage examples"
    assert not any("--admin-key" in line or "--emergency-key" in line for line in examples)
    assert "kept for compatibility" in text
