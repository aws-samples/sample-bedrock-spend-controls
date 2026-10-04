"""Qualify a deployed permission lease through the complete broker path.

Live mode creates (or reuses) one Cognito sandbox test user, obtains its JWT,
vends credentials through the IAM-authenticated Function URL, calls
CountTokens before the effective permission deadline, and confirms that the
same STS keys receive AccessDenied after that deadline. It never prints the
JWT, password, or vended credentials and performs no IAM policy mutation.

The default is a dry run that prints the plan and makes no AWS calls; add
``--live`` to execute. Live mode deletes the Cognito user it created (reused
users are left alone; ``--keep-user`` skips the deletion). The quota row the
broker auto-provisions for that user stays in the users table: the admin API
has no delete, so block it or leave it as a record of the run.

Run it from the repository root::

    python qualification/lease_stack_qualification.py --stack-name <stack>
    python qualification/lease_stack_qualification.py --profile <sandbox> \\
        --stack-name <stack> --lease-seconds 300 --live
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

if __package__ in (None, ""):
    # Running as ``python qualification/lease_stack_qualification.py``: make
    # the repository root importable so ``examples`` resolves.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from examples.sigv4_gateway import signed_request  # noqa: E402

_ACCESS_DENIED = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}
LEASE_CHOICES = (60, 300, 900)
# The STS keys always live 900 s; a lease longer than that is capped by the
# session's own expiry, so the deadline check only needs the lease window.
STS_SESSION_SECONDS = 900


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


def expected_lease_window(lease_seconds: int) -> tuple[int, int]:
    """Accepted ``expiration - now`` range for a lease of ``lease_seconds``.

    The broker may shorten a lease to the JWT's remaining lifetime and the
    vend itself takes time, so allow up to a minute (or half the lease for
    the 60 s dial) under the nominal value and a few seconds over it.
    """
    if lease_seconds not in LEASE_CHOICES:
        raise ValueError(f"lease seconds must be one of {LEASE_CHOICES}")
    low = max(lease_seconds // 2, lease_seconds - 60)
    return low, lease_seconds + 5


def dry_run_plan(args) -> dict:
    """Describe what ``--live`` would do without touching AWS."""
    low, high = expected_lease_window(args.lease_seconds)
    return {
        "mode": "dry-run",
        "stack": args.stack_name,
        "region": args.region,
        "profile": args.profile or "(default credential chain)",
        "username": args.username,
        "model_id": args.model_id,
        "lease_seconds": args.lease_seconds,
        "accepted_effective_lease_seconds": [low, high],
        "planned_operations": [
            "sts:GetCallerIdentity",
            f"cloudformation:DescribeStacks on {args.stack_name}",
            "cognito-idp:AdminCreateUser (skipped if the user exists), "
            "AdminSetUserPassword, InitiateAuth",
            "POST /v1/credentials on the broker Function URL (SigV4)",
            "bedrock:CountTokens before and after the permission deadline",
        ],
        "cleanup": {
            "cognito_user": (
                "kept (--keep-user)" if args.keep_user
                else "deleted if this run created it"
            ),
            "quota_row": (
                f"'{args.username}' stays in the users table; the admin API has "
                "no delete. Block it or leave it as a record of the run."
            ),
        },
        "next_step": "re-run with --live after reviewing this plan",
    }


def _outputs(session: boto3.Session, stack_name: str) -> dict[str, str]:
    stack = session.client("cloudformation").describe_stacks(
        StackName=stack_name
    )["Stacks"][0]
    return {
        output["OutputKey"]: output["OutputValue"]
        for output in stack.get("Outputs", [])
    }


def _user_jwt(
    session: boto3.Session,
    user_pool_id: str,
    client_id: str,
    username: str,
) -> tuple[str, bool]:
    """Return (id token, created) for the sandbox test user."""
    cognito = session.client("cognito-idp")
    password = secrets.token_urlsafe(24) + "aA1!"
    created = True
    try:
        cognito.admin_create_user(
            UserPoolId=user_pool_id,
            Username=username,
            MessageAction="SUPPRESS",
        )
    except cognito.exceptions.UsernameExistsException:
        created = False
    cognito.admin_set_user_password(
        UserPoolId=user_pool_id,
        Username=username,
        Password=password,
        Permanent=True,
    )
    auth = cognito.initiate_auth(
        ClientId=client_id,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )
    return auth["AuthenticationResult"]["IdToken"], created


def _delete_user(session: boto3.Session, user_pool_id: str, username: str) -> bool:
    cognito = session.client("cognito-idp")
    try:
        cognito.admin_delete_user(UserPoolId=user_pool_id, Username=username)
    except cognito.exceptions.UserNotFoundException:
        return False
    return True


def _count_tokens(runtime, model_id: str) -> int:
    response = runtime.count_tokens(
        modelId=model_id,
        input={
            "converse": {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"text": "permission lease qualification"}],
                    }
                ]
            }
        },
    )
    return int(response["inputTokens"])


def run(args) -> dict:
    if not args.live:
        return dry_run_plan(args)
    if not args.profile:
        raise ValueError("--profile is required with --live")
    session = boto3.Session(
        profile_name=args.profile,
        region_name=args.region,
    )
    caller = session.client("sts").get_caller_identity()
    outputs = _outputs(session, args.stack_name)
    token, created_user = _user_jwt(
        session,
        outputs["DemoUserPoolId"],
        outputs["DemoUserPoolClientId"],
        args.username,
    )
    cleanup = {
        "cognito_user_created": created_user,
        "cognito_user_deleted": False,
        "quota_row_retained": True,
    }
    try:
        result = _qualify(args, session, outputs, token)
    finally:
        if created_user and not args.keep_user:
            cleanup["cognito_user_deleted"] = _delete_user(
                session, outputs["DemoUserPoolId"], args.username
            )
    result.update({"account": caller["Account"], "cleanup": cleanup})
    return result


def _qualify(args, session: boto3.Session, outputs: dict, token: str) -> dict:
    lease_id = str(uuid.uuid4())
    response = signed_request(
        "POST",
        outputs["BrokerApiUrl"].rstrip("/") + "/v1/credentials",
        region=args.region,
        user_token=token,
        aws_session=session,
        timeout=30,
        headers={"X-Quota-Lease-Id": lease_id},
        content=b"",
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"credential vend failed with HTTP {response.status_code}: "
            f"{response.text}"
        )
    body = response.json()
    expiration = _parse_time(body["expiration"])
    sts_expiration = _parse_time(body["sts_expiration"])
    issued_at = datetime.now(timezone.utc)
    effective_seconds = int((expiration - issued_at).total_seconds())
    sts_seconds = int((sts_expiration - issued_at).total_seconds())
    low, high = expected_lease_window(args.lease_seconds)
    if not low <= effective_seconds <= high:
        raise RuntimeError(
            f"unexpected effective lease duration: {effective_seconds}s "
            f"(expected {low}-{high}s for a {args.lease_seconds}s lease; is "
            "the stack's lease dial set to that value?)"
        )
    sts_low = max(STS_SESSION_SECONDS - 60, args.lease_seconds - 60)
    if not sts_low <= sts_seconds <= STS_SESSION_SECONDS + 5:
        raise RuntimeError(f"unexpected STS duration: {sts_seconds}s")

    runtime = boto3.client(
        "bedrock-runtime",
        region_name=args.region,
        aws_access_key_id=body["aws_access_key_id"],
        aws_secret_access_key=body["aws_secret_access_key"],
        aws_session_token=body["aws_session_token"],
    )
    input_tokens = _count_tokens(runtime, args.model_id)

    sleep_seconds = max(
        0.0,
        (expiration - datetime.now(timezone.utc)).total_seconds() + 0.5,
    )
    time.sleep(sleep_seconds)
    started = time.monotonic()
    denied_code = ""
    while time.monotonic() - started <= args.denial_timeout_seconds:
        try:
            _count_tokens(runtime, args.model_id)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in _ACCESS_DENIED:
                denied_code = code
                break
            raise
        time.sleep(args.poll_seconds)
    if not denied_code:
        raise RuntimeError(
            "Bedrock remained authorized after the permission deadline"
        )

    return {
        "mode": "live",
        "region": args.region,
        "stack": args.stack_name,
        "username": args.username,
        "model_id": args.model_id,
        "lease_seconds": args.lease_seconds,
        "lease_id_matches": body.get("lease_id") == lease_id,
        "effective_lease_seconds_observed": effective_seconds,
        "sts_seconds_observed": sts_seconds,
        "pre_deadline_input_tokens": input_tokens,
        "post_deadline_denied": True,
        "post_deadline_error": denied_code,
        "denial_observed_after_seconds": round(time.monotonic() - started, 3),
        "credential_material_printed": False,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--profile", default="", help="AWS CLI profile; required with --live."
    )
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument(
        "--stack-name", default="BedrockSpendControls"
    )
    parser.add_argument(
        "--username", default="quota-lease-qualification"
    )
    parser.add_argument(
        "--model-id", default="amazon.nova-micro-v1:0"
    )
    parser.add_argument(
        "--lease-seconds",
        type=int,
        choices=LEASE_CHOICES,
        default=300,
        help="The lease dial value the stack is currently set to.",
    )
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--denial-timeout-seconds", type=int, default=60)
    parser.add_argument(
        "--live",
        action="store_true",
        help="Execute against AWS; without it only the plan is printed.",
    )
    parser.add_argument(
        "--keep-user",
        action="store_true",
        help="Do not delete the Cognito user this run created.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    result = run(_parser().parse_args(argv))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
