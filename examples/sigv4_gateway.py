"""SigV4 clients for the IAM-authenticated broker/admin Function URL.

SigV4 owns the HTTP Authorization header. Application credentials therefore
travel in dedicated headers:

- X-Quota-User-Token: verified end-user JWT
- X-Quota-Admin-Key: Secrets Manager-backed admin key
"""

import argparse
import json
import os
import sys
import uuid

import boto3
import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


def _credentials(session: boto3.Session | None = None):
    credentials = (session or boto3.Session()).get_credentials()
    if credentials is None:
        raise RuntimeError("AWS credentials are required to invoke the gateway")
    return credentials.get_frozen_credentials()


def _sign(method: str, url: str, headers: dict[str, str], body,
          region: str, credentials) -> dict[str, str]:
    aws_request = AWSRequest(method=method, url=url, headers=headers, data=body)
    SigV4Auth(credentials, "lambda", region).add_auth(aws_request)
    return dict(aws_request.headers)


class FunctionUrlSigV4Auth(httpx.Auth):
    """httpx auth adapter for broker and administrative API calls."""

    requires_request_body = True

    def __init__(self, user_token: str, region: str | None = None,
                 credentials=None):
        self._user_token = user_token
        self._region = region or os.environ.get("AWS_REGION", "us-east-1")
        self._credentials = credentials

    def auth_flow(self, request: httpx.Request):
        request.headers["X-Quota-User-Token"] = self._user_token
        # SDK API-key authentication may have installed a Bearer header. SigV4
        # replaces it; the user token is preserved in the dedicated header.
        request.headers.pop("Authorization", None)
        signed = _sign(
            request.method,
            str(request.url),
            dict(request.headers),
            request.content,
            self._region,
            self._credentials or _credentials(),
        )
        request.headers.update(signed)
        yield request


def signed_request(method: str, url: str, *, region: str | None = None,
                   user_token: str | None = None,
                   admin_key: str | None = None,
                   emergency_key: str | None = None,
                   aws_session: boto3.Session | None = None,
                   http_client: httpx.Client | None = None,
                   timeout=None,
                   **kwargs) -> httpx.Response:
    """Send one SigV4-signed gateway request with optional app credentials.

    Pass query values through ``params`` without pre-encoding them. HTTPX
    builds the final URL once, and that exact URL is then signed and sent.
    """
    headers = dict(kwargs.pop("headers", {}) or {})
    if user_token:
        headers["X-Quota-User-Token"] = user_token
    if admin_key:
        headers["X-Quota-Admin-Key"] = admin_key
    if emergency_key:
        headers["X-Quota-Emergency-Key"] = emergency_key

    def send(client: httpx.Client) -> httpx.Response:
        request_kwargs = dict(kwargs)
        if timeout is not None:
            request_kwargs["timeout"] = timeout
        request = client.build_request(
            method, url, headers=headers, **request_kwargs
        )
        request.headers.pop("Authorization", None)
        request.headers.update(_sign(
            request.method,
            str(request.url),
            dict(request.headers),
            request.content,
            region or os.environ.get("AWS_REGION", "us-east-1"),
            _credentials(aws_session),
        ))
        return client.send(request)

    if http_client is not None:
        return send(http_client)
    with httpx.Client() as client:
        return send(client)


def _admin_request_args(
    args,
    *,
    idempotency_key: str | None = None,
    if_match: str | None = None,
) -> tuple[str, str, dict]:
    """Build one routine or emergency request without performing I/O."""
    base = args.gateway_url.rstrip("/")
    if args.command == "create-user":
        daily = {
            "usd": args.daily_usd,
            "input_tokens": args.daily_input_tokens,
            "output_tokens": args.daily_output_tokens,
        }
        daily_thresholds = _parse_thresholds(
            getattr(args, "daily_thresholds", None)
        )
        if daily_thresholds is not None:
            daily["thresholds"] = daily_thresholds
        limits = {
            "daily": daily,
            "weekly": _optional_period(args, "weekly"),
            "monthly": _optional_period(args, "monthly"),
        }
        body = {
            "user_id": args.user_id,
            "name": args.name or args.user_id,
            "limits": limits,
        }
        rate, rate_present = _rate_payload(args)
        if rate_present:
            body["rate"] = rate
        return "POST", f"{base}/admin/users", {
            "headers": {
                "Idempotency-Key": idempotency_key or str(uuid.uuid4())
            },
            "json": body,
        }
    if args.command == "list-users":
        return "GET", f"{base}/admin/users", {}
    if args.command in {"emergency-stop", "emergency-recover"}:
        activate = args.command == "emergency-stop"
        return "POST", f"{base}/admin/emergency-stop", {
            "json": {
                "action": "activate" if activate else "recover",
                "confirmation": (
                    "STOP_ALL_BEDROCK_SESSIONS"
                    if activate
                    else "RESTORE_ALL_BEDROCK_SESSIONS"
                ),
                "reason": args.reason,
            }
        }

    if args.command == "update-user":
        current_limits = getattr(args, "current_limits", None)
        if not isinstance(current_limits, dict):
            raise ValueError("update-user requires canonical current limits")
        limits = json.loads(json.dumps(current_limits))
        changed = False
        for period in ("daily", "weekly", "monthly"):
            if getattr(args, f"disable_{period}", False):
                limits[period] = None
                changed = True
                continue
            names = ("usd", "input_tokens", "output_tokens")
            provided = {
                name: getattr(args, f"{period}_{name}")
                for name in names
                if getattr(args, f"{period}_{name}") is not None
            }
            thresholds = _parse_thresholds(
                getattr(args, f"{period}_thresholds", None)
            )
            if thresholds is not None:
                provided["thresholds"] = thresholds
            if provided:
                existing = limits.get(period) or {
                    "usd": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                }
                limits[period] = {**existing, **provided}
                changed = True
        rate, rate_present = _rate_payload(args)
        if not changed and not rate_present:
            raise ValueError(
                "update-user requires at least one quota option"
            )
        if if_match is None:
            raise ValueError("update-user requires the current user version")
        body = {"limits": limits}
        if rate_present:
            body["rate"] = rate
        if args.reason is not None:
            body["reason"] = args.reason
        return "PUT", f"{base}/admin/user/limits", {
            "params": {"user_id": args.user_id},
            "headers": {
                "Idempotency-Key": idempotency_key or str(uuid.uuid4()),
                "If-Match": if_match,
            },
            "json": body,
        }
    if args.command in {"block-user", "unblock-user"}:
        if if_match is None:
            raise ValueError(
                f"{args.command} requires the current user version"
            )
        status = "blocked" if args.command == "block-user" else "active"
        return "PUT", f"{base}/admin/user/status", {
            "params": {"user_id": args.user_id},
            "headers": {
                "Idempotency-Key": idempotency_key or str(uuid.uuid4()),
                "If-Match": if_match,
            },
            "json": {"status": status, "reason": args.reason},
        }
    if args.command == "get-usage":
        params = {"user_id": args.user_id, "period": args.period}
        if args.window:
            params["window"] = args.window
        return "GET", f"{base}/admin/user/usage", {"params": params}
    raise ValueError(f"Unsupported command: {args.command}")


def _optional_period(args, period: str) -> dict | None:
    values = {
        "usd": getattr(args, f"{period}_usd", None),
        "input_tokens": getattr(args, f"{period}_input_tokens", None),
        "output_tokens": getattr(args, f"{period}_output_tokens", None),
    }
    provided = [value is not None for value in values.values()]
    if not any(provided):
        return None
    if not all(provided):
        raise ValueError(
            f"{period} requires USD, input-token, and output-token limits"
        )
    thresholds = _parse_thresholds(getattr(args, f"{period}_thresholds", None))
    if thresholds is not None:
        values["thresholds"] = thresholds
    return values


def _parse_thresholds(raw: str | None) -> list[dict] | None:
    """``"50:warn,80:warn,100:block"`` -> API thresholds list.

    Percentages, comma-separated, each ``<percent>:<warn|block>``. Ordering
    and the at-most-one-trailing-block rule are validated by the server;
    the client only parses the shape.
    """
    if raw is None:
        return None
    entries: list[dict] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        percent, _, action = token.partition(":")
        try:
            at = float(percent) / 100
        except ValueError as exc:
            raise ValueError(
                f"threshold {token!r} must look like <percent>:<warn|block>"
            ) from exc
        if action not in ("warn", "block"):
            raise ValueError(
                f"threshold {token!r} must end in :warn or :block"
            )
        entries.append({"at": at, "action": action})
    if not entries:
        raise ValueError("thresholds must contain at least one entry")
    return entries


def _rate_payload(args) -> tuple[dict | None, bool]:
    """Return (rate, present). ``--rpm 0 --tpm 0`` disables both."""
    rpm = getattr(args, "rpm", None)
    tpm = getattr(args, "tpm", None)
    if getattr(args, "disable_rate", False):
        return None, True
    if rpm is None and tpm is None:
        return None, False
    return {"rpm": int(rpm or 0), "tpm": int(tpm or 0)}, True


def _response_body(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {"status_code": response.status_code, "body": response.text}
    return body if isinstance(body, dict) else {"response": body}


def _emit_response(response: httpx.Response) -> None:
    body = _response_body(response)
    print(json.dumps(body, indent=2, sort_keys=True))
    if response.status_code == 409:
        error = body.get("error", {})
        code = error.get("code") if isinstance(error, dict) else None
        guidance = {
            "user_already_exists": (
                "The user was not changed. Fetch the existing user and review "
                "it before deciding whether to update it."
            ),
            "version_conflict": (
                "The user changed after the preflight read. Review the current "
                "user returned in error.details, then retry intentionally."
            ),
            "idempotency_conflict": (
                "The idempotency key was reused for different content. Do not "
                "treat this request as successful."
            ),
        }.get(code, "Review the conflict response before retrying.")
        print(f"Conflict ({code or 'unknown'}): {guidance}", file=sys.stderr)
    response.raise_for_status()


def _if_match_from_detail(response: httpx.Response) -> str:
    """Return the server ETag or the exact canonical integer version."""
    response.raise_for_status()
    etag = response.headers.get("ETag")
    if etag:
        return etag
    user = _response_body(response).get("user")
    version = user.get("version") if isinstance(user, dict) else None
    if isinstance(version, int) and not isinstance(version, bool) and version >= 0:
        return str(version)
    raise RuntimeError("User detail response did not include an ETag or version")


def _execute_admin_command(args, session, request_fn=signed_request):
    """Execute one CLI command, preflighting versioned routine mutations."""
    is_emergency = args.command in {"emergency-stop", "emergency-recover"}
    if_match = None
    if args.command in {"update-user", "block-user", "unblock-user"}:
        detail = request_fn(
            "GET",
            f"{args.gateway_url.rstrip('/')}/admin/user",
            region=args.region,
            admin_key=args.admin_key,
            aws_session=session,
            params={"user_id": args.user_id},
        )
        if detail.status_code >= 400:
            _emit_response(detail)
        if_match = _if_match_from_detail(detail)
        if args.command == "update-user":
            body = _response_body(detail)
            user = body.get("user")
            limits = user.get("limits") if isinstance(user, dict) else None
            if not isinstance(limits, dict):
                raise RuntimeError("User detail response did not include limits")
            args.current_limits = limits

    method, url, request_kwargs = _admin_request_args(
        args,
        if_match=if_match,
    )
    return request_fn(
        method,
        url,
        region=args.region,
        admin_key=(None if is_emergency else args.admin_key),
        emergency_key=(args.emergency_key if is_emergency else None),
        aws_session=session,
        **request_kwargs,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SigV4-signed administrative client for the quota gateway."
    )
    parser.add_argument(
        "--gateway-url",
        default=os.environ.get("GATEWAY_URL"),
        help="BrokerApiUrl stack output (or GATEWAY_URL).",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION", "us-east-1"),
    )
    parser.add_argument(
        "--profile",
        default=os.environ.get("AWS_PROFILE"),
        help="AWS CLI profile used for SigV4 credentials.",
    )
    parser.add_argument(
        "--admin-key",
        default=os.environ.get("ADMIN_KEY"),
        help="Routine admin API key (or ADMIN_KEY).",
    )
    parser.add_argument(
        "--emergency-key",
        default=os.environ.get("EMERGENCY_ADMIN_KEY"),
        help="Break-glass emergency key (or EMERGENCY_ADMIN_KEY).",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    threshold_help = (
        "Comma-separated <percent>:<warn|block> entries, e.g. "
        "'50:warn,80:warn,100:block'. Omit the block entry for an "
        "alert-only period."
    )

    create = commands.add_parser("create-user")
    create.add_argument("user_id")
    create.add_argument("--name")
    create.add_argument("--daily-usd", type=float, required=True)
    create.add_argument("--daily-input-tokens", type=int, required=True)
    create.add_argument("--daily-output-tokens", type=int, required=True)
    create.add_argument("--daily-thresholds", help=threshold_help)
    create.add_argument("--weekly-usd", type=float)
    create.add_argument("--weekly-input-tokens", type=int)
    create.add_argument("--weekly-output-tokens", type=int)
    create.add_argument("--weekly-thresholds", help=threshold_help)
    create.add_argument("--monthly-usd", type=float)
    create.add_argument("--monthly-input-tokens", type=int)
    create.add_argument("--monthly-output-tokens", type=int)
    create.add_argument("--monthly-thresholds", help=threshold_help)
    create.add_argument("--rpm", type=int, help="Requests per minute (0 = off)")
    create.add_argument(
        "--tpm", type=int, help="Uncached input + output tokens per minute (0 = off)"
    )

    commands.add_parser("list-users")

    emergency_stop = commands.add_parser("emergency-stop")
    emergency_stop.add_argument("--reason", required=True)

    emergency_recover = commands.add_parser("emergency-recover")
    emergency_recover.add_argument("--reason", required=True)

    update = commands.add_parser("update-user")
    update.add_argument("user_id")
    update.add_argument("--daily-usd", type=float)
    update.add_argument("--daily-input-tokens", type=int)
    update.add_argument("--daily-output-tokens", type=int)
    update.add_argument("--daily-thresholds", help=threshold_help)
    update.add_argument("--disable-daily", action="store_true")
    update.add_argument("--weekly-usd", type=float)
    update.add_argument("--weekly-input-tokens", type=int)
    update.add_argument("--weekly-output-tokens", type=int)
    update.add_argument("--weekly-thresholds", help=threshold_help)
    update.add_argument("--disable-weekly", action="store_true")
    update.add_argument("--monthly-usd", type=float)
    update.add_argument("--monthly-input-tokens", type=int)
    update.add_argument("--monthly-output-tokens", type=int)
    update.add_argument("--monthly-thresholds", help=threshold_help)
    update.add_argument("--disable-monthly", action="store_true")
    update.add_argument("--rpm", type=int, help="Requests per minute (0 = off)")
    update.add_argument(
        "--tpm", type=int, help="Uncached input + output tokens per minute (0 = off)"
    )
    update.add_argument(
        "--disable-rate", action="store_true", help="Remove both rate limits"
    )
    update.add_argument("--reason")

    block = commands.add_parser("block-user")
    block.add_argument("user_id")
    block.add_argument("--reason", default="admin CLI")

    unblock = commands.add_parser("unblock-user")
    unblock.add_argument("user_id")
    unblock.add_argument("--reason", default="admin CLI")

    usage = commands.add_parser("get-usage")
    usage.add_argument("user_id")
    usage.add_argument("--window", help="UTC window in YYYY-MM-DD format.")
    usage.add_argument(
        "--period", choices=("daily", "weekly", "monthly"), default="daily"
    )
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    if not args.gateway_url:
        parser.error("--gateway-url or GATEWAY_URL is required")
    is_emergency = args.command in {"emergency-stop", "emergency-recover"}
    if is_emergency and not args.emergency_key:
        parser.error(
            "--emergency-key or EMERGENCY_ADMIN_KEY is required for "
            "break-glass commands"
        )
    if not is_emergency and not args.admin_key:
        parser.error("--admin-key or ADMIN_KEY is required")
    session = boto3.Session(
        profile_name=args.profile,
        region_name=args.region,
    )
    try:
        response = _execute_admin_command(args, session)
    except ValueError as exc:
        parser.error(str(exc))
    _emit_response(response)


if __name__ == "__main__":
    main()
