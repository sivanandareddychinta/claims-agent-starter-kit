"""Shared helpers for the lab scripts: stack outputs, AWS clients, sample packs."""
from __future__ import annotations

import argparse
import json
import mimetypes
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import boto3

KIT_ROOT = Path(__file__).resolve().parents[1]
PACKS_DIR = KIT_ROOT / "data" / "claim_packs"


def base_parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument("--lab-id", default="lab", help="labId used at deploy time (stack ClaimsLab-<labId>)")
    p.add_argument("--region", default=None, help="AWS Region (defaults to your AWS CLI configuration)")
    return p


def session(region: str | None) -> boto3.session.Session:
    s = boto3.session.Session(region_name=region)
    if not s.region_name:
        sys.exit("No AWS Region configured. Pass --region or run `aws configure`.")
    return s


def outputs(sess: boto3.session.Session, lab_id: str) -> dict:
    stack = f"ClaimsLab-{lab_id}"
    try:
        desc = sess.client("cloudformation").describe_stacks(StackName=stack)["Stacks"][0]
    except Exception as exc:  # noqa: BLE001
        sys.exit(f"Could not read stack {stack} in {sess.region_name}: {exc}\nDeploy it first (see README).")
    return {o["OutputKey"]: o["OutputValue"] for o in desc.get("Outputs", [])}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def to_ddb(value):
    return json.loads(json.dumps(value), parse_float=Decimal)


def load_pack(pack_id: str) -> dict:
    path = PACKS_DIR / pack_id / "pack.json"
    if not path.exists():
        sys.exit(f"No sample pack {pack_id} in {PACKS_DIR}")
    return json.loads(path.read_text())


def eval_case_id(pack_id: str) -> str:
    """Deterministic case IDs for evaluation scenarios: PACK-07 -> EVAL-20261001-PK07."""
    return f"EVAL-20261001-PK{pack_id.split('-')[1][-2:]}"


def create_case_from_pack(sess, out: dict, pack_id: str, case_id: str, status: str = "SUBMITTED") -> dict:
    """Write a case straight to DynamoDB (bypassing the portal) and upload its files."""
    pack = load_pack(pack_id)
    s3 = sess.client("s3")
    docs = []
    for i, d in enumerate(pack["documents"], start=1):
        local = PACKS_DIR / pack_id / d["file"]
        key = f"cases/{case_id}/DOC-{case_id[-4:]}{i:04d}-{d['file']}"
        ctype = mimetypes.guess_type(local.name)[0] or "application/octet-stream"
        s3.upload_file(str(local), out["DocumentsBucket"], key, ExtraArgs={"ContentType": ctype})
        docs.append({"docId": f"DOC-{case_id[-4:]}{i:04d}", "fileName": d["file"], "s3Key": key,
                     "contentType": ctype, "uploadedAt": now_iso()})
    claim = pack["claim"]
    item = {
        "caseId": case_id, "status": status, "createdAt": now_iso(), "updatedAt": now_iso(),
        "createdBy": "lab-script", "lastActor": "lab-script", "packId": pack_id,
        "policyNumber": claim["policyNumber"], "claimType": claim["claimType"],
        "claimantName": claim["claimantName"], "claimantEmail": claim["claimantEmail"],
        "incidentDate": claim["incidentDate"], "claimedAmount": claim["claimedAmount"],
        "description": claim["description"], "documents": docs, "flags": {},
        "region": claim.get("region", ""),
        "timeline": [{"at": now_iso(), "actor": "lab-script", "action": "createClaim", "detail": f"from {pack_id}"}],
    }
    sess.resource("dynamodb").Table(out["CasesTable"]).put_item(Item=to_ddb(item))
    return item
