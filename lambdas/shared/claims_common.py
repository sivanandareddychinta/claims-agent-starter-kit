"""Shared helpers for every Lambda in the Claims & Case Management lab.

Everything that touches AWS goes through the small client factories below so the
unit tests can swap in moto or stubs. Nothing in here contains business rules;
those live in the tool modules.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Any, Callable

import boto3
from boto3.dynamodb.conditions import Key  # noqa: F401  (re-exported for tools)
from botocore.config import Config

# ---------------------------------------------------------------------------
# Configuration (set by the CDK stack as Lambda environment variables)
# ---------------------------------------------------------------------------
CASES_TABLE = os.environ.get("CASES_TABLE", "ClaimsLabCases")
POLICIES_TABLE = os.environ.get("POLICIES_TABLE", "ClaimsLabPolicies")
ADJUSTERS_TABLE = os.environ.get("ADJUSTERS_TABLE", "ClaimsLabAdjusters")
DOCS_BUCKET = os.environ.get("DOCS_BUCKET", "claims-lab-docs")
PORTAL_URL = os.environ.get("PORTAL_URL", "https://example.cloudfront.net")
SENDER_EMAIL = os.environ.get("SENDER_EMAIL", "")
NOTIFY_OVERRIDE_EMAIL = os.environ.get("NOTIFY_OVERRIDE_EMAIL", "")

CASE_ID_PATTERN = re.compile(r"^(CLM|EVAL)-[0-9]{8}-[A-Z0-9]{4}$")


def region() -> str:
    return os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"


# ---------------------------------------------------------------------------
# Client factories (cached per Lambda container; tests call reset_clients())
# ---------------------------------------------------------------------------
@lru_cache(maxsize=None)
def ddb():
    return boto3.resource("dynamodb", region_name=region())


@lru_cache(maxsize=None)
def client(name: str):
    if name == "s3":
        # SigV4 is required for pre-signed URLs on buckets created since 2020.
        return boto3.client("s3", region_name=region(),
                            config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
    return boto3.client(name, region_name=region())


def reset_clients() -> None:
    ddb.cache_clear()
    client.cache_clear()


def cases_table():
    return ddb().Table(CASES_TABLE)


def policies_table():
    return ddb().Table(POLICIES_TABLE)


def adjusters_table():
    return ddb().Table(ADJUSTERS_TABLE)


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------
class ToolError(Exception):
    """An expected, explainable failure. The message is returned to the agent."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now().replace(microsecond=0).isoformat().replace("+00:00", "Z")


def iso_in(minutes: float) -> str:
    return (now() + timedelta(minutes=minutes)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_case_id(prefix: str = "CLM") -> str:
    return f"{prefix}-{now():%Y%m%d}-{uuid.uuid4().hex[:4].upper()}"


def new_doc_id() -> str:
    return "DOC-" + uuid.uuid4().hex[:8].upper()


def to_ddb(value: Any) -> Any:
    """Convert floats to Decimal (DynamoDB resource API requirement)."""
    return json.loads(json.dumps(value, default=str), parse_float=Decimal)


def from_ddb(value: Any) -> Any:
    """Convert Decimals back to int/float so results are JSON-serialisable."""
    if isinstance(value, list):
        return [from_ddb(v) for v in value]
    if isinstance(value, dict):
        return {k: from_ddb(v) for k, v in value.items()}
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    return value


def require(args: dict, *names: str) -> None:
    missing = [n for n in names if args.get(n) in (None, "")]
    if missing:
        raise ToolError(f"Missing required argument(s): {', '.join(missing)}")


def check_case_id(case_id: str) -> str:
    case_id = (case_id or "").strip().upper()
    if not CASE_ID_PATTERN.match(case_id):
        raise ToolError(f"'{case_id}' is not a valid case ID (expected e.g. CLM-20261016-AB12)")
    return case_id


def parse_amount(text: Any) -> float | None:
    """'INR 45,600.00' -> 45600.0 ; returns None when no number is present."""
    if text is None:
        return None
    if isinstance(text, (int, float, Decimal)):
        return float(text)
    cleaned = re.sub(r"[^0-9.\-]", "", str(text).replace(",", ""))
    if cleaned in ("", ".", "-"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


_DATE_FORMATS = ("%Y-%m-%d", "%d %b %Y", "%d %B %Y", "%d/%m/%Y", "%d-%m-%Y", "%b %d, %Y", "%d.%m.%Y")


def parse_date(text: Any) -> str | None:
    """Normalise the date formats used on the claim documents to YYYY-MM-DD."""
    if not text:
        return None
    raw = str(text).strip().rstrip(".")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def normalise_name(text: Any) -> str:
    return re.sub(r"[^a-z ]", "", str(text or "").lower()).strip()


# ---------------------------------------------------------------------------
# Case record helpers
# ---------------------------------------------------------------------------
HIDDEN_FIELDS = {"taskToken"}


def get_case(case_id: str, include_hidden: bool = False) -> dict:
    case_id = check_case_id(case_id)
    item = cases_table().get_item(Key={"caseId": case_id}).get("Item")
    if not item:
        raise ToolError(f"Case {case_id} was not found")
    item = from_ddb(item)
    if not include_hidden:
        for f in HIDDEN_FIELDS:
            item.pop(f, None)
    return item


def update_case(case_id: str, fields: dict, actor: str, action: str, detail: str = "",
                remove: list[str] | None = None, condition: str | None = None,
                condition_values: dict | None = None) -> dict:
    """Set fields on the case and append one entry to its timeline.

    Every write in the lab goes through here, so the timeline (and the S3 action
    history fed by DynamoDB Streams) records who changed what.
    """
    fields = dict(fields)
    fields["updatedAt"] = now_iso()
    fields["lastActor"] = actor
    names, values, sets = {}, {}, []
    for i, (k, v) in enumerate(fields.items()):
        names[f"#f{i}"] = k
        values[f":v{i}"] = to_ddb(v)
        sets.append(f"#f{i} = :v{i}")
    names["#tl"] = "timeline"
    values[":event"] = to_ddb([{"at": fields["updatedAt"], "actor": actor, "action": action, "detail": detail[:500]}])
    values[":empty"] = []
    sets.append("#tl = list_append(if_not_exists(#tl, :empty), :event)")
    expr = "SET " + ", ".join(sets)
    if remove:
        for j, r in enumerate(remove):
            names[f"#r{j}"] = r
        expr += " REMOVE " + ", ".join(f"#r{j}" for j in range(len(remove)))
    kwargs = dict(Key={"caseId": case_id}, UpdateExpression=expr,
                  ExpressionAttributeNames=names, ExpressionAttributeValues=values,
                  ReturnValues="ALL_NEW")
    cond = "attribute_exists(caseId)" + (f" AND ({condition})" if condition else "")
    kwargs["ConditionExpression"] = cond
    if "#status" in cond:  # "status" is a DynamoDB reserved word
        names["#status"] = "status"
    if condition_values:
        values.update(to_ddb(condition_values))
    try:
        result = cases_table().update_item(**kwargs)
    except cases_table().meta.client.exceptions.ConditionalCheckFailedException as exc:
        raise ToolError(f"Case {case_id} is not in a state that allows '{action}'") from exc
    item = from_ddb(result["Attributes"])
    item.pop("taskToken", None)
    return item


def find_document(case: dict, doc_id: str) -> tuple[int, dict]:
    for i, d in enumerate(case.get("documents", [])):
        if d.get("docId") == doc_id:
            return i, d
    raise ToolError(f"Document {doc_id} is not on case {case['caseId']}")


def get_policy(policy_number: str) -> dict | None:
    item = policies_table().get_item(Key={"policyNumber": (policy_number or "").strip().upper()}).get("Item")
    return from_ddb(item) if item else None


# ---------------------------------------------------------------------------
# Notifications (Amazon SES)
# ---------------------------------------------------------------------------
def send_email(to_address: str, subject: str, body: str) -> dict:
    """Send a plain-text email. In the lab every message can be redirected to one
    verified inbox (NOTIFY_OVERRIDE_EMAIL) because SES starts in sandbox mode."""
    if not SENDER_EMAIL:
        return {"sent": False, "reason": "SENDER_EMAIL is not configured; email skipped"}
    recipient = NOTIFY_OVERRIDE_EMAIL or to_address
    if not recipient:
        return {"sent": False, "reason": "no recipient address"}
    if NOTIFY_OVERRIDE_EMAIL and to_address and to_address != NOTIFY_OVERRIDE_EMAIL:
        body = f"[Lab redirect: originally addressed to {to_address}]\n\n{body}"
    try:
        client("sesv2").send_email(
            FromEmailAddress=SENDER_EMAIL,
            Destination={"ToAddresses": [recipient]},
            Content={"Simple": {"Subject": {"Data": subject[:200]}, "Body": {"Text": {"Data": body[:20000]}}}},
        )
        return {"sent": True, "to": recipient}
    except Exception as exc:
        if "TooManyRequests" in type(exc).__name__ or "LimitExceeded" in type(exc).__name__:
            return {"sent": False, "reason": f"SES sandbox 24h quota exceeded: {exc}", "to": recipient}
        raise


def portal_link(case_id: str) -> str:
    return f"{PORTAL_URL.rstrip('/')}/#/cases/{case_id}"


# ---------------------------------------------------------------------------
# AgentCore Gateway tool dispatch
# ---------------------------------------------------------------------------
TOOL_DELIMITER = "___"


def gateway_tool_name(context: Any) -> str:
    """Gateway passes '<targetName>___<toolName>' in the client context."""
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    full = custom.get("bedrockAgentCoreToolName", "")
    return full.split(TOOL_DELIMITER, 1)[1] if TOOL_DELIMITER in full else full


def dispatch(event: dict, context: Any, tools: dict[str, Callable[[dict], dict]], actor: str) -> dict:
    """Route a Gateway call to the matching tool function and return JSON.

    Errors are returned as {"error": ...} rather than raised so the agent can
    read the reason and decide what to do next.
    """
    name = gateway_tool_name(context) or event.pop("_tool", "")
    fn = tools.get(name)
    if fn is None:
        return {"error": f"Unknown tool '{name}'. Available: {sorted(tools)}"}
    try:
        return from_ddb(fn(event or {}))
    except ToolError as exc:
        return {"error": str(exc)}
    except NotImplementedError as exc:  # an open TODO(Task n) in the starter kit
        return {"error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - surface a short message, log the rest
        print(json.dumps({"level": "ERROR", "tool": name, "actor": actor, "error": repr(exc)}))
        return {"error": f"{name} failed: {type(exc).__name__}. See CloudWatch logs."}
