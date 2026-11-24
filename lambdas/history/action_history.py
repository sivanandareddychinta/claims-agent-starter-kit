"""DynamoDB Streams consumer: archive every change to a case in the action-history
bucket (S3 Object Lock, governance mode), partitioned by case for Athena.

Object key: history/caseId=<id>/<yyyy-mm-ddTHH:MM:SS>-<eventID>.json
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from boto3.dynamodb.types import TypeDeserializer

from shared.claims_common import client, from_ddb

HISTORY_BUCKET = os.environ.get("HISTORY_BUCKET", "claims-lab-history")
_deserializer = TypeDeserializer()
REDACT = {"taskToken", "text"}  # never archive task tokens; OCR text stays in the case record


def _image(raw: dict | None) -> dict:
    if not raw:
        return {}
    item = from_ddb({k: _deserializer.deserialize(v) for k, v in raw.items()})
    for key in REDACT:
        item.pop(key, None)
    for doc in item.get("documents", []) or []:
        doc.pop("text", None)
    return item


def build_record(record: dict) -> tuple[str, dict]:
    ddb = record["dynamodb"]
    new, old = _image(ddb.get("NewImage")), _image(ddb.get("OldImage"))
    case_id = (new or old).get("caseId", "unknown")
    when = datetime.fromtimestamp(ddb.get("ApproximateCreationDateTime", 0), tz=timezone.utc)
    last_event = (new.get("timeline") or [{}])[-1] if new else {}
    entry = {
        "caseId": case_id,
        "eventName": record.get("eventName"),
        "eventId": record.get("eventID"),
        "at": when.isoformat().replace("+00:00", "Z"),
        "actor": new.get("lastActor") or last_event.get("actor", ""),
        "action": last_event.get("action", record.get("eventName")),
        "detail": last_event.get("detail", ""),
        "statusBefore": old.get("status"),
        "statusAfter": new.get("status"),
        "newImage": new,
    }
    key = f"history/caseId={case_id}/{when:%Y-%m-%dT%H:%M:%S}-{record.get('eventID')}.json"
    return key, entry


def handler(event, _context):
    written = 0
    for record in event.get("Records", []):
        key, entry = build_record(record)
        body = json.dumps(entry, default=str).encode("utf-8")
        # Write `body` to HISTORY_BUCKET under `key` with client("s3").put_object.
        # The bucket has Object Lock enabled, so the request must carry a
        # checksum: pass ChecksumAlgorithm="SHA256". Set ContentType to JSON.
        client("s3").put_object(
            Bucket=HISTORY_BUCKET,
            Key=key,
            Body=body,
            ContentType="application/json",
            ChecksumAlgorithm="SHA256",
        )
        written += 1
    return {"written": written}
