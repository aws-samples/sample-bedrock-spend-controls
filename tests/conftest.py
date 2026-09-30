"""Shared fixtures: a minimal in-memory fake of the DynamoDB resource API.

The fake implements exactly the expression subset the app uses (ADD/SET
with if_not_exists, attribute_not_exists conditions, `<=` comparisons,
GSI-style query by attribute equality). Tests run with no network and no
AWS credentials.
"""

import copy
import os
import secrets
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from boto3.dynamodb.types import TypeDeserializer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "quota_periods_layer" / "python"))
sys.path.insert(0, str(ROOT / "gateway"))
sys.path.insert(0, str(ROOT / "usage_processor"))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("USERS_TABLE", "users-test")
os.environ.setdefault("USAGE_TABLE", "usage-test")
os.environ.setdefault("ADMIN_AUDIT_TABLE", "admin-audit-test")
# HS256 dev-mode JWT verification for tests (no IdP / network needed). The
# key is generated per session so no signing secret lives in the repository;
# test modules read it back from the environment when they mint tokens.
os.environ.setdefault("JWT_SHARED_SECRET", secrets.token_urlsafe(32))

from botocore.exceptions import ClientError  # noqa: E402


def _boto_condition_ok(condition, item: dict) -> bool:
    """Evaluate the small subset of boto3.dynamodb.conditions the app uses in
    scan FilterExpressions: Attr(x).eq(v), .gt(v), .gte(v), .exists(),
    Key(x).begins_with(v), and And/Or combinations."""
    from boto3.dynamodb.conditions import (
        And,
        AttributeExists,
        BeginsWith,
        Equals,
        GreaterThan,
        GreaterThanEquals,
        Or,
    )

    if isinstance(condition, And):
        return all(_boto_condition_ok(part, item) for part in condition._values)
    if isinstance(condition, Or):
        return any(_boto_condition_ok(part, item) for part in condition._values)
    attribute = condition._values[0].name
    present = attribute in item
    if isinstance(condition, AttributeExists):
        return present
    if not present:
        return False
    value = item[attribute]
    operand = condition._values[1]
    if isinstance(condition, Equals):
        return value == operand
    if isinstance(condition, GreaterThan):
        return value > operand
    if isinstance(condition, GreaterThanEquals):
        return value >= operand
    if isinstance(condition, BeginsWith):
        return str(value).startswith(str(operand))
    raise AssertionError(f"unsupported FilterExpression in fake: {condition}")


class FakeTable:
    def __init__(self, name: str, key_attrs: list[str]):
        self.name = name
        self.key_attrs = key_attrs
        self.items: dict[tuple, dict] = {}

    # -- helpers ---------------------------------------------------------
    def _key(self, key: dict) -> tuple:
        return tuple(key[a] for a in self.key_attrs)

    # -- API -------------------------------------------------------------
    def put_item(
        self,
        Item: dict,
        ConditionExpression: str | None = None,
        ExpressionAttributeValues: dict | None = None,
        ExpressionAttributeNames: dict | None = None,
    ):
        key = self._key(Item)
        current = self.items.get(key, {})
        values = ExpressionAttributeValues or {}
        names = ExpressionAttributeNames or {}
        if ConditionExpression and not self._condition_ok(
            ConditionExpression, current, values, names
        ):
            raise ClientError(
                {
                    "Error": {
                        "Code": "ConditionalCheckFailedException",
                        "Message": "The conditional request failed",
                    }
                },
                "PutItem",
            )
        self.items[key] = dict(Item)
        return {}

    def get_item(self, Key: dict, ConsistentRead: bool = False):
        item = self.items.get(self._key(Key))
        return {"Item": dict(item)} if item else {}

    def scan(self, **kwargs):
        # Stable ordering by primary key so Limit + ExclusiveStartKey paging is
        # deterministic (real DynamoDB order is unspecified, but tests need a
        # fixed one). Only the subset of the scan API the app uses.
        ordered = [dict(v) for _, v in sorted(self.items.items())]
        condition = kwargs.get("FilterExpression")
        if condition is not None:
            ordered = [item for item in ordered if _boto_condition_ok(condition, item)]
        start = 0
        exclusive = kwargs.get("ExclusiveStartKey")
        if exclusive:
            start_key = self._key(exclusive)
            keys = [self._key(v) for v in ordered]
            start = keys.index(start_key) + 1 if start_key in keys else 0
        limit = kwargs.get("Limit")
        page = ordered[start:start + limit] if limit else ordered[start:]
        result = {"Items": page}
        if limit and (start + limit) < len(ordered):
            last = page[-1]
            result["LastEvaluatedKey"] = {a: last[a] for a in self.key_attrs}
        return result

    def query(
        self,
        IndexName=None,
        KeyConditionExpression=None,
        ExpressionAttributeValues=None,
        ExpressionAttributeNames=None,
        Limit=None,
        ScanIndexForward=True,
        ExclusiveStartKey=None,
        **kwargs,
    ):
        import re

        expression = str(KeyConditionExpression)
        for placeholder, attribute in (ExpressionAttributeNames or {}).items():
            expression = expression.replace(placeholder, attribute)
        match = re.fullmatch(
            r"([A-Za-z0-9_]+)\s*=\s*(:[A-Za-z0-9_]+)"
            r"(?:\s+AND\s+([A-Za-z0-9_]+)\s+BETWEEN\s+"
            r"(:[A-Za-z0-9_]+)\s+AND\s+(:[A-Za-z0-9_]+))?",
            expression,
        )
        if not match:
            raise AssertionError(f"unsupported query in fake: {expression}")
        partition_attr, partition_value, sort_attr, start_value, end_value = (
            match.groups()
        )
        values = ExpressionAttributeValues or {}
        matches = [
            dict(item)
            for item in self.items.values()
            if item.get(partition_attr) == values[partition_value]
            and (
                sort_attr is None
                or values[start_value]
                <= item.get(sort_attr, "")
                <= values[end_value]
            )
        ]
        order_attr = sort_attr or (
            "event_key" if IndexName or "event_key" in self.key_attrs
            else self.key_attrs[-1]
        )
        matches.sort(
            key=lambda item: item.get(order_attr, ""),
            reverse=not ScanIndexForward,
        )
        start = 0
        if ExclusiveStartKey:
            exclusive = self._key(ExclusiveStartKey)
            keys = [self._key(item) for item in matches]
            start = keys.index(exclusive) + 1 if exclusive in keys else 0
        page = matches[start : start + Limit] if Limit else matches[start:]
        result = {"Items": page}
        if Limit and start + Limit < len(matches):
            last = page[-1]
            cursor_attributes = list(self.key_attrs)
            if IndexName and partition_attr not in cursor_attributes:
                cursor_attributes.append(partition_attr)
            result["LastEvaluatedKey"] = {
                attribute: last[attribute]
                for attribute in cursor_attributes
            }
        return result

    def update_item(self, Key: dict, UpdateExpression: str,
                    ExpressionAttributeValues: dict | None = None,
                    ExpressionAttributeNames: dict | None = None,
                    ConditionExpression: str | None = None,
                    ReturnValues: str | None = None):
        values = ExpressionAttributeValues or {}
        names = ExpressionAttributeNames or {}
        key = self._key(Key)
        item = self.items.get(key, dict(Key))

        import re

        expression_text = UpdateExpression + " " + (ConditionExpression or "")
        used_placeholders = set(re.findall(r":[A-Za-z0-9_]+", expression_text))
        unused_placeholders = set(values) - used_placeholders
        if unused_placeholders:
            raise ClientError(
                {
                    "Error": {
                        "Code": "ValidationException",
                        "Message": (
                            "unused expression values: "
                            + ", ".join(sorted(unused_placeholders))
                        ),
                    }
                },
                "UpdateItem",
            )

        if ConditionExpression and not self._condition_ok(
            ConditionExpression, item, values, names
        ):
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException",
                           "Message": "The conditional request failed"}},
                "UpdateItem",
            )

        self._apply_update(UpdateExpression, item, values, names)
        self.items[key] = item
        if ReturnValues == "ALL_NEW":
            return {"Attributes": dict(item)}
        return {}

    # -- expression evaluation (targeted subset) --------------------------
    def _condition_ok(
        self,
        expr: str,
        item: dict,
        values: dict,
        names: dict | None = None,
    ) -> bool:
        expr = expr.strip()
        for placeholder, attribute in (names or {}).items():
            expr = expr.replace(placeholder, attribute)

        def strip_outer(text: str) -> str:
            while text.startswith("(") and text.endswith(")"):
                depth = 0
                encloses_all = True
                for index, character in enumerate(text):
                    if character == "(":
                        depth += 1
                    elif character == ")":
                        depth -= 1
                    if depth == 0 and index < len(text) - 1:
                        encloses_all = False
                        break
                if not encloses_all:
                    break
                text = text[1:-1].strip()
            return text

        def split_top_level(text: str, operator: str) -> list[str]:
            depth = 0
            start = 0
            parts: list[str] = []
            marker = f" {operator} "
            index = 0
            while index < len(text):
                character = text[index]
                if character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
                elif depth == 0 and text.startswith(marker, index):
                    parts.append(text[start:index].strip())
                    index += len(marker)
                    start = index
                    continue
                index += 1
            if parts:
                parts.append(text[start:].strip())
            return parts

        expr = strip_outer(expr)
        for operator, reducer in (
            ("OR", any),
            ("AND", all),
        ):
            parts = split_top_level(expr, operator)
            if parts:
                return reducer(
                    self._condition_ok(part, item, values) for part in parts
                )

        if expr.startswith("attribute_not_exists(") and expr.endswith(")"):
            attribute = expr[len("attribute_not_exists(") : -1].strip()
            return attribute not in item
        if expr.startswith("attribute_exists(") and expr.endswith(")"):
            attribute = expr[len("attribute_exists(") : -1].strip()
            return attribute in item

        import re

        match = re.fullmatch(r"([A-Za-z0-9_]+)\s*(<=|<|=)\s*(:[A-Za-z0-9_]+)", expr)
        if not match:
            raise AssertionError(f"unsupported condition in fake: {expr}")
        attribute, operator, placeholder = match.groups()
        if attribute not in item:
            return False
        left = item[attribute]
        right = values[placeholder]
        if operator == "<=":
            return left <= right
        if operator == "<":
            return left < right
        return left == right

    @staticmethod
    def _split_top_level(text: str) -> list[str]:
        """Split on commas that are not inside parentheses."""
        parts, depth, current = [], 0, ""
        for ch in text:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if ch == "," and depth == 0:
                parts.append(current.strip())
                current = ""
            else:
                current += ch
        if current.strip():
            parts.append(current.strip())
        return parts

    def _apply_update(self, expr: str, item: dict, values: dict, names: dict) -> None:
        # Split into ADD / SET / REMOVE sections (keywords uppercase in the app).
        import re
        sections: dict[str, str] = {}
        for match in re.finditer(r"\b(ADD|SET|REMOVE)\b", expr):
            keyword = match.group(1)
            start = match.end()
            next_match = re.search(r"\b(?:ADD|SET|REMOVE)\b", expr[start:])
            end = start + next_match.start() if next_match else len(expr)
            sections[keyword] = expr[start:end].strip()
        if "ADD" in sections:
            for part in self._split_top_level(sections["ADD"]):
                attr, placeholder = part.split()
                addend = values[placeholder]
                if isinstance(addend, (set, frozenset, list)):
                    # DynamoDB set semantics: ADD unions the members.
                    current = item.get(attr)
                    current = set(current) if isinstance(current, (set, list)) else set()
                    item[attr] = current | set(addend)
                else:
                    item[attr] = int(item.get(attr, 0)) + int(addend)
        if "REMOVE" in sections:
            for part in self._split_top_level(sections["REMOVE"]):
                item.pop(names.get(part.strip(), part.strip()), None)
        if "SET" in sections:
            for part in self._split_top_level(sections["SET"]):
                target, _, rhs = part.partition("=")
                target = names.get(target.strip(), target.strip())
                rhs = rhs.strip()
                if rhs.startswith("if_not_exists"):
                    arithmetic = re.fullmatch(
                        r"if_not_exists\(([^,]+),\s*(:[A-Za-z0-9_]+)\)"
                        r"\s*\+\s*(:[A-Za-z0-9_]+)",
                        rhs,
                    )
                    if arithmetic:
                        source, default, increment = arithmetic.groups()
                        source = names.get(source.strip(), source.strip())
                        item[target] = int(item.get(source, values[default])) + int(
                            values[increment]
                        )
                        continue
                    inner = rhs[len("if_not_exists("):rhs.rindex(")")]
                    _attr_name, placeholder = [x.strip() for x in inner.split(",")]
                    if target not in item:
                        item[target] = values[placeholder]
                else:
                    item[target] = values[rhs]


_DESERIALIZER = TypeDeserializer()


def _decode_attribute_map(values: dict | None) -> dict:
    return {
        key: _DESERIALIZER.deserialize(value)
        for key, value in (values or {}).items()
    }


class FakeDynamoClient:
    """Atomic low-level transaction subset used by production store code."""

    def __init__(self, resource):
        self.resource = resource

    def transact_write_items(self, TransactItems):  # noqa: N803
        snapshots = {
            name: copy.deepcopy(table.items)
            for name, table in self.resource.tables.items()
        }
        try:
            for action in TransactItems:
                if "Put" in action:
                    put = action["Put"]
                    self.resource.Table(put["TableName"]).put_item(
                        Item=_decode_attribute_map(put["Item"]),
                        ConditionExpression=put.get("ConditionExpression"),
                        ExpressionAttributeValues=_decode_attribute_map(
                            put.get("ExpressionAttributeValues")
                        ),
                        ExpressionAttributeNames=put.get(
                            "ExpressionAttributeNames"
                        ),
                    )
                    continue
                if "Update" in action:
                    update = action["Update"]
                    self.resource.Table(update["TableName"]).update_item(
                        Key=_decode_attribute_map(update["Key"]),
                        UpdateExpression=update["UpdateExpression"],
                        ConditionExpression=update.get("ConditionExpression"),
                        ExpressionAttributeValues=_decode_attribute_map(
                            update.get("ExpressionAttributeValues")
                        ),
                        ExpressionAttributeNames=update.get(
                            "ExpressionAttributeNames"
                        ),
                    )
                    continue
                raise AssertionError(
                    f"unsupported transaction action in fake: {action}"
                )
        except ClientError as exc:
            for name, items in snapshots.items():
                self.resource.tables[name].items = items
            raise ClientError(
                {
                    "Error": {
                        "Code": "TransactionCanceledException",
                        "Message": str(exc),
                    }
                },
                "TransactWriteItems",
            ) from exc
        return {}


class FakeDynamoDB:
    """Stands in for boto3.resource('dynamodb')."""

    def __init__(self):
        self.tables: dict[str, FakeTable] = {}
        self.meta = SimpleNamespace(client=FakeDynamoClient(self))

    def add_table(self, name: str, key_attrs: list[str]) -> FakeTable:
        self.tables[name] = FakeTable(name, key_attrs)
        return self.tables[name]

    def Table(self, name: str) -> FakeTable:  # noqa: N802 (boto3 API)
        return self.tables[name]


class FakeSNS:
    def __init__(self):
        self.published: list[dict] = []

    def publish(self, **kwargs):
        self.published.append(kwargs)
        return {"MessageId": "fake"}


@pytest.fixture
def fake_dynamodb():
    db = FakeDynamoDB()
    db.add_table(os.environ["USERS_TABLE"], ["user_id"])
    db.add_table(os.environ["USAGE_TABLE"], ["user_id", "window"])
    db.add_table(
        os.environ["ADMIN_AUDIT_TABLE"], ["subject_id", "event_key"]
    )
    return db


@pytest.fixture
def fake_sns():
    return FakeSNS()
