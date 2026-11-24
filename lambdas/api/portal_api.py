"""HTTP API behind the adjuster portal (Cognito JWT authorizer on every route).

Routes
  GET  /me                                  who am I, which groups
  GET  /claims?status=&mine=true            list cases
  POST /claims                              create a claim + pre-signed upload URLs
  GET  /claims/{caseId}                     case detail + pre-signed download URLs
  POST /claims/{caseId}/submit              start the case workflow
  POST /claims/{caseId}/documents           add documents (initial or requested later)
  POST /claims/{caseId}/documents/complete  tell the workflow requested documents are uploaded
  POST /claims/{caseId}/decision            approve / reject / request more information
"""
from __future__ import annotations

import json
import os
import re
import time

from boto3.dynamodb.conditions import Attr

from shared.claims_common import (
    DOCS_BUCKET, Key, ToolError, adjusters_table, cases_table, client, from_ddb, get_case,
    new_case_id, new_doc_id, now_iso, parse_date, to_ddb, update_case,
)

STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN", "")
UPLOAD_URL_SECONDS = 900
CLAIM_TYPES = ("motor", "health", "property")
ALLOWED_CONTENT_TYPES = ("image/png", "image/jpeg", "application/pdf")
DECISIONS = ("APPROVE", "REJECT", "MORE_INFO")


class HttpError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------
def claims_of(event) -> dict:
    return event.get("requestContext", {}).get("authorizer", {}).get("jwt", {}).get("claims", {})


def username_of(event) -> str:
    c = claims_of(event)
    return c.get("username") or c.get("cognito:username") or c.get("sub", "")


def groups_of(event) -> list[str]:
    raw = claims_of(event).get("cognito:groups", "")
    if isinstance(raw, list):
        return raw
    return [g for g in re.split(r"[\s,\[\]]+", str(raw)) if g]


def body_of(event) -> dict:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HttpError(400, "Body must be JSON") from exc


def respond(status: int, payload) -> dict:
    return {"statusCode": status, "headers": {"content-type": "application/json"},
            "body": json.dumps(from_ddb(payload), default=str)}


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", name or "file")[:80]


def upload_slots(case_id: str, files: list[dict]) -> tuple[list[dict], list[dict]]:
    if not isinstance(files, list) or not 1 <= len(files) <= 10:
        raise HttpError(400, "files must list 1 to 10 files")
    docs, uploads = [], []
    s3 = client("s3")
    for f in files:
        content_type = f.get("contentType", "")
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise HttpError(400, f"{f.get('fileName')}: content type must be one of {ALLOWED_CONTENT_TYPES}")
        doc_id = new_doc_id()
        key = f"cases/{case_id}/{doc_id}-{safe_name(f.get('fileName', 'file'))}"
        url = s3.generate_presigned_url("put_object", ExpiresIn=UPLOAD_URL_SECONDS,
                                        Params={"Bucket": DOCS_BUCKET, "Key": key, "ContentType": content_type})
        docs.append({"docId": doc_id, "fileName": f.get("fileName"), "s3Key": key,
                     "contentType": content_type, "uploadedAt": now_iso()})
        uploads.append({"docId": doc_id, "fileName": f.get("fileName"), "uploadUrl": url, "contentType": content_type})
    return docs, uploads


def staff_by_username(username: str) -> dict | None:
    for p in from_ddb(adjusters_table().scan().get("Items", [])):
        if p.get("username") == username:
            return p
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
def get_me(event, _params):
    person = staff_by_username(username_of(event)) or {}
    return 200, {"username": username_of(event), "groups": groups_of(event),
                 "adjusterId": person.get("adjusterId"), "name": person.get("name")}


def list_claims(event, _params):
    q = event.get("queryStringParameters") or {}
    table = cases_table()
    if q.get("status"):
        items = table.query(IndexName="status-index", KeyConditionExpression=Key("status").eq(q["status"]),
                            ScanIndexForward=False, Limit=100).get("Items", [])
    elif q.get("mine") == "true":
        person = staff_by_username(username_of(event))
        if not person:
            return 200, {"cases": []}
        items = table.query(IndexName="assignee-index", KeyConditionExpression=Key("assignee").eq(person["adjusterId"]),
                            Limit=100).get("Items", [])
        items += table.scan(FilterExpression=Attr("approver").eq(person["adjusterId"]), Limit=200).get("Items", [])
    else:
        items = table.scan(Limit=200).get("Items", [])
    fields = ("caseId", "status", "claimType", "claimantName", "claimedAmount", "incidentDate", "createdAt",
              "assigneeName", "approverName", "slaDueAt", "flags")
    seen, cases = set(), []
    for item in sorted(from_ddb(items), key=lambda i: i.get("createdAt", ""), reverse=True):
        if item["caseId"] not in seen:
            seen.add(item["caseId"])
            cases.append({k: item.get(k) for k in fields})
    return 200, {"cases": cases}


def create_claim(event, _params):
    b = body_of(event)
    missing = [k for k in ("policyNumber", "claimType", "claimantName", "claimantEmail", "incidentDate",
                           "claimedAmount", "description", "files") if not b.get(k)]
    if missing:
        raise HttpError(400, f"Missing fields: {', '.join(missing)}")
    if b["claimType"] not in CLAIM_TYPES:
        raise HttpError(400, f"claimType must be one of {CLAIM_TYPES}")
    incident = parse_date(b["incidentDate"])
    if not incident:
        raise HttpError(400, "incidentDate must be YYYY-MM-DD")
    try:
        amount = round(float(b["claimedAmount"]), 2)
    except (TypeError, ValueError) as exc:
        raise HttpError(400, "claimedAmount must be a number") from exc
    case_id = new_case_id()
    docs, uploads = upload_slots(case_id, b["files"])
    item = {
        "caseId": case_id, "status": "DRAFT", "createdAt": now_iso(), "updatedAt": now_iso(),
        "createdBy": username_of(event), "lastActor": username_of(event),
        "policyNumber": b["policyNumber"].strip().upper(), "claimType": b["claimType"],
        "claimantName": b["claimantName"].strip()[:120], "claimantEmail": b["claimantEmail"].strip()[:200],
        "incidentDate": incident, "claimedAmount": amount, "description": b["description"][:2000],
        "documents": docs, "flags": {}, "packId": b.get("packId", ""),
        "timeline": [{"at": now_iso(), "actor": username_of(event), "action": "createClaim",
                      "detail": f"{len(docs)} document(s)"}],
    }
    cases_table().put_item(Item=to_ddb(item), ConditionExpression="attribute_not_exists(caseId)")
    return 201, {"caseId": case_id, "uploads": uploads}


def get_claim(event, params):
    case = get_case(params["caseId"])
    s3 = client("s3")
    for d in case.get("documents", []):
        d.pop("text", None)
        d["downloadUrl"] = s3.generate_presigned_url("get_object", ExpiresIn=600,
                                                     Params={"Bucket": DOCS_BUCKET, "Key": d["s3Key"]})
    return 200, case


def _all_uploaded(case: dict) -> list[str]:
    s3, missing = client("s3"), []
    for d in case.get("documents", []):
        try:
            s3.head_object(Bucket=DOCS_BUCKET, Key=d["s3Key"])
        except Exception:  # noqa: BLE001
            missing.append(d.get("fileName") or d["docId"])
    return missing


def submit_claim(event, params):
    case = get_case(params["caseId"])
    if case.get("status") != "DRAFT":
        raise HttpError(409, f"Case is {case.get('status')}; only DRAFT cases can be submitted")
    missing = _all_uploaded(case)
    if missing:
        raise HttpError(409, f"These files have not been uploaded yet: {', '.join(missing)}")
    update_case(case["caseId"], {"status": "SUBMITTED"}, username_of(event), "submitClaim", "claim submitted",
                condition="#status = :draft", condition_values={":draft": "DRAFT"})
    execution = client("stepfunctions").start_execution(
        stateMachineArn=STATE_MACHINE_ARN, name=f"{case['caseId']}-{int(time.time())}",
        input=json.dumps({"caseId": case["caseId"], "slaHours": 48}))
    update_case(case["caseId"], {"executionArn": execution["executionArn"]}, "workflow", "startWorkflow",
                "case workflow started")
    return 202, {"caseId": case["caseId"], "status": "SUBMITTED", "executionArn": execution["executionArn"]}


def add_documents(event, params):
    case = get_case(params["caseId"])
    if case.get("status") not in ("DRAFT", "AWAITING_DOCUMENTS"):
        raise HttpError(409, f"Documents can only be added while the case is DRAFT or AWAITING_DOCUMENTS (it is {case.get('status')})")
    docs, uploads = upload_slots(case["caseId"], body_of(event).get("files", []))
    update_case(case["caseId"], {"documents": case.get("documents", []) + docs}, username_of(event),
                "addDocuments", f"{len(docs)} document(s) added")
    return 201, {"caseId": case["caseId"], "uploads": uploads}


def documents_complete(event, params):
    case = get_case(params["caseId"], include_hidden=True)
    if case.get("status") != "AWAITING_DOCUMENTS" or case.get("taskKind") != "documents" or not case.get("taskToken"):
        raise HttpError(409, "The case is not waiting for documents")
    missing = _all_uploaded(case)
    if missing:
        raise HttpError(409, f"These files have not been uploaded yet: {', '.join(missing)}")
    token = case["taskToken"]
    update_case(case["caseId"], {"status": "DOCUMENTS_RECEIVED"}, username_of(event), "documentsComplete",
                "requested documents uploaded", remove=["taskToken", "taskKind"],
                condition="taskToken = :tok", condition_values={":tok": token})
    client("stepfunctions").send_task_success(taskToken=token, output=json.dumps({"documentsReceived": True}))
    return 202, {"caseId": case["caseId"], "status": "DOCUMENTS_RECEIVED"}


def decide(event, params):
    groups = groups_of(event)
    if not {"approvers", "supervisors"} & set(groups):
        raise HttpError(403, "Only members of the approvers or supervisors group can decide claims")
    b = body_of(event)
    decision, rationale = (b.get("decision") or "").upper(), (b.get("rationale") or "").strip()
    if decision not in DECISIONS:
        raise HttpError(400, f"decision must be one of {DECISIONS}")
    if len(rationale) < 5:
        raise HttpError(400, "A rationale of at least a few words is required")
    case = get_case(params["caseId"], include_hidden=True)
    if case.get("status") != "PENDING_APPROVAL" or case.get("taskKind") != "approval" or not case.get("taskToken"):
        raise HttpError(409, f"Case is {case.get('status')}; it is not waiting for a decision")
    user = username_of(event)
    if "supervisors" not in groups and case.get("approverUsername") and case["approverUsername"] != user:
        raise HttpError(403, f"This case is assigned to approver {case.get('approverName')}")
    token = case["taskToken"]
    record = {"decision": decision, "rationale": rationale[:2000], "decidedBy": user, "decidedAt": now_iso()}
    status = {"APPROVE": "DECIDED", "REJECT": "DECIDED", "MORE_INFO": "MORE_INFO_REQUESTED"}[decision]
    update_case(case["caseId"], {"decision": record, "status": status}, user, "decision",
                f"{decision}: {rationale[:200]}", remove=["taskToken", "taskKind"],
                condition="taskToken = :tok", condition_values={":tok": token})
    try:
        client("stepfunctions").send_task_success(taskToken=token, output=json.dumps(record))
    except Exception as exc:  # noqa: BLE001 - e.g. the task timed out
        update_case(case["caseId"], {"status": "MANUAL_REVIEW", "manualReviewReason": f"Decision could not resume workflow: {exc}"},
                    "workflow", "decisionFailed", str(exc)[:300])
        raise HttpError(409, "The workflow is no longer waiting for this decision; the case was sent to manual review") from exc
    return 202, {"caseId": case["caseId"], "status": status, "decision": record}


ROUTES = {
    "GET /me": get_me,
    "GET /claims": list_claims,
    "POST /claims": create_claim,
    "GET /claims/{caseId}": get_claim,
    "POST /claims/{caseId}/submit": submit_claim,
    "POST /claims/{caseId}/documents": add_documents,
    "POST /claims/{caseId}/documents/complete": documents_complete,
    "POST /claims/{caseId}/decision": decide,
}


def handler(event, _context):
    route = ROUTES.get(event.get("routeKey", ""))
    if not route:
        return respond(404, {"error": f"No route {event.get('routeKey')}"})
    try:
        status, payload = route(event, event.get("pathParameters") or {})
        return respond(status, payload)
    except HttpError as exc:
        return respond(exc.status, {"error": str(exc)})
    except ToolError as exc:
        return respond(404 if "not found" in str(exc) else 400, {"error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"level": "ERROR", "route": event.get("routeKey"), "error": repr(exc)}))
        return respond(500, {"error": "Internal error. See CloudWatch logs."})
