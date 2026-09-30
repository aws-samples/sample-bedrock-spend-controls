"""Immutable, time-bounded permissions passed to STS AssumeRole sessions."""

from __future__ import annotations

import json
from datetime import datetime, timezone

BEDROCK_RUNTIME_ACTIONS = (
    "bedrock:CountTokens",
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
)
SESSION_POLICY_MAX_CHARACTERS = 2_048


def permission_lease_policy(expires_at: datetime) -> str:
    """Return a compact session policy allowing Bedrock before ``expires_at``.

    STS intersects this policy with the role's identity policy. Consequently,
    the wildcard resource here cannot add models that the role does not
    already allow; it only applies the fixed time boundary to that allowlist.
    """
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("permission lease expiration must be timezone-aware")
    deadline = expires_at.astimezone(timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )
    document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "BedrockPermissionLease",
                "Effect": "Allow",
                "Action": list(BEDROCK_RUNTIME_ACTIONS),
                "Resource": "*",
                "Condition": {
                    "DateLessThan": {"aws:CurrentTime": deadline}
                },
            }
        ],
    }
    encoded = json.dumps(document, separators=(",", ":"), sort_keys=True)
    if len(encoded) > SESSION_POLICY_MAX_CHARACTERS:
        raise ValueError("permission lease session policy exceeds STS limit")
    return encoded
