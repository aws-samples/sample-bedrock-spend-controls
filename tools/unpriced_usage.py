#!/usr/bin/env python3
"""List daily ledger rows whose USD estimate is known to be incomplete.

The usage processor never prices a dimension at zero silently. When an
invocation-log record carries a dimension (prompt-cache read/write tokens,
generated images) that the resolved catalog entry has no rate for, the
processor prices it at the conservative fallback rate when one exists,
increments ``unpriced_requests`` on the subject's daily row, and unions the
dimension name into the row's ``missing_dimensions`` string set. This tool
scans the usage table for those rows so an operator can decide whether to
add the dimension to ``cdk/config/model-pricing.json`` (future events) or
repair the historical aggregate by hand (never automatic).

The processor writes every request to a subject row and to a per-model row
(``<subject>#model#<model_id>``). One line is printed per subject and day;
the per-model rows are folded into its ``models`` column
(``<model_id>:<unpriced_requests>``) so nothing is listed twice.

    cdk/.venv/bin/python tools/unpriced_usage.py \
        --table "$(aws cloudformation describe-stacks --stack-name BedrockSpendControls \
            --query "Stacks[0].Outputs[?OutputKey=='UsageTableName'].OutputValue | [0]" \
            --output text)" \
        --since 2026-09-01

Requires read access to the usage table. A full table scan: fine at the
scale this sample targets (tens of thousands of rows); use --since to
bound it.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

import boto3
from boto3.dynamodb.conditions import Attr

MICRO = 1_000_000
MODEL_LEDGER_SEPARATOR = "#model#"
_SENTINEL_PREFIXES = ("REQUEST#", "RATE#")


def _rows(table, since: str | None):
    condition = Attr("unpriced_requests").gt(0)
    if since:
        condition = condition & Attr("window").gte(since)
    kwargs = {"FilterExpression": condition}
    while True:
        response = table.scan(**kwargs)
        for item in response.get("Items", []):
            user_id = str(item.get("user_id", ""))
            if user_id.startswith(_SENTINEL_PREFIXES):
                continue
            yield item
        last = response.get("LastEvaluatedKey")
        if not last:
            return
        kwargs["ExclusiveStartKey"] = last


def _summary(item: dict, user_id: str) -> dict:
    return {
        "user_id": user_id,
        "window": str(item["window"]),
        "requests": int(item.get("requests", 0)),
        "unpriced_requests": int(item.get("unpriced_requests", 0)),
        "missing_dimensions": sorted(
            str(value) for value in item.get("missing_dimensions", ())
        ),
        "cost_usd": int(item.get("cost_micro", 0)) / MICRO,
        "cache_read_tokens": int(item.get("cache_read_tokens", 0)),
        "cache_write_tokens": int(item.get("cache_write_tokens", 0)),
        "images": int(item.get("images", 0)),
        "models": [],
    }


def collect_rows(table, since: str | None) -> list[dict]:
    """One row per (subject, day) with its per-model rows folded in."""
    subjects: dict[tuple[str, str], dict] = {}
    model_rows: list[tuple[str, str, dict]] = []
    for item in _rows(table, since):
        user_id = str(item["user_id"])
        subject, separator, model_id = user_id.partition(
            MODEL_LEDGER_SEPARATOR
        )
        if separator:
            model_rows.append((subject, model_id, item))
            continue
        subjects[(user_id, str(item["window"]))] = _summary(item, user_id)
    for subject, model_id, item in model_rows:
        key = (subject, str(item["window"]))
        row = subjects.get(key)
        if row is None:
            # Subject row missing or already repaired by hand: keep the
            # model's figures visible rather than dropping them.
            row = subjects[key] = _summary(item, subject)
        row["models"].append(
            f"{model_id}:{int(item.get('unpriced_requests', 0))}"
        )
    rows = list(subjects.values())
    for row in rows:
        row["models"].sort()
    rows.sort(key=lambda row: (row["window"], row["user_id"]))
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--table", required=True, help="Usage table name")
    parser.add_argument("--region", default=None)
    parser.add_argument(
        "--since",
        default=None,
        help="Only rows with window >= this ISO date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--format", choices=("table", "json"), default="table"
    )
    args = parser.parse_args(argv)
    if args.since:
        date.fromisoformat(args.since)  # validate early
    table = boto3.resource("dynamodb", region_name=args.region).Table(
        args.table
    )
    rows = collect_rows(table, args.since)
    if args.format == "json":
        json.dump(rows, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    if not rows:
        print("No daily rows with unpriced requests.")
        return 0
    header = (
        "window", "user_id", "requests", "unpriced", "missing_dimensions",
        "cost_usd", "cache_read", "cache_write", "images", "models",
    )
    print("\t".join(header))
    for row in rows:
        print(
            "\t".join(
                str(value)
                for value in (
                    row["window"],
                    row["user_id"],
                    row["requests"],
                    row["unpriced_requests"],
                    ",".join(row["missing_dimensions"]),
                    f"{row['cost_usd']:.6f}",
                    row["cache_read_tokens"],
                    row["cache_write_tokens"],
                    row["images"],
                    ",".join(row["models"]),
                )
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
