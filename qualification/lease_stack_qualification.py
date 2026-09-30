"""Qualify a deployed permission lease through the complete broker path.

This creates or reuses one Cognito sandbox test user, obtains its JWT, vends
credentials through the IAM-authenticated Function URL, calls CountTokens
before the effective permission deadline, and confirms that the same STS keys
receive AccessDenied after that deadline. It never prints the JWT, password, or
vended credentials and performs no IAM policy mutation or resource deletion.
"""

from __future__ import annotations

import argparse
import json
import secrets
import time
import uuid
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

from examples.sigv4_gateway import signed_request

_ACCESS_DENIED = {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc
    )


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
) -> str:
    cognito = session.client("cognito-idp")
    password = secrets.token_urlsafe(24) + "aA1!"
    try:
        cognito.admin_create_user(
            UserPoolId=user_pool_id,
            Username=username,
            MessageAction="SUPPRESS",
        )
    except cognito.exceptions.UsernameExistsException:
        pass
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
    return auth["AuthenticationResult"]["IdToken"]


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
    session = boto3.Session(
        profile_name=args.profile,
        region_name=args.region,
    )
    caller = session.client("sts").get_caller_identity()
    outputs = _outputs(session, args.stack_name)
    token = _user_jwt(
        session,
        outputs["DemoUserPoolId"],
        outputs["DemoUserPoolClientId"],
        args.username,
    )
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
    if not 240 <= effective_seconds <= 305:
        raise RuntimeError(
            f"unexpected effective lease duration: {effective_seconds}s"
        )
    if not 840 <= sts_seconds <= 905:
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
        "account": caller["Account"],
        "region": args.region,
        "stack": args.stack_name,
        "username": args.username,
        "model_id": args.model_id,
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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
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
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--denial-timeout-seconds", type=int, default=60)
    return parser


def main(argv: list[str] | None = None) -> int:
    result = run(_parser().parse_args(argv))
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
