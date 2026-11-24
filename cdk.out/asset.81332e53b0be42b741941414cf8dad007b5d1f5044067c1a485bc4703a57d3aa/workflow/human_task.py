"""Step Functions task (waitForTaskToken): park the case until a person acts.

The task token is stored on the case in DynamoDB and never leaves AWS. The
approver gets an email with a link to the portal; the portal API (signed-in
user, approvers group) calls SendTaskSuccess with the decision.

Input: {"caseId": "...", "kind": "approval" | "documents", "taskToken": "..."}
"""
from __future__ import annotations

from shared.claims_common import (
    adjusters_table, from_ddb, get_case, portal_link, send_email, update_case,
)

KINDS = {
    "approval": {"status": "PENDING_APPROVAL", "allowedFrom": ["READY_FOR_APPROVAL"]},
    "documents": {"status": "AWAITING_DOCUMENTS", "allowedFrom": ["MORE_INFO_REQUESTED", "PENDING_APPROVAL"]},
}


def handler(event, _context):
    case_id, kind, token = event["caseId"], event["kind"], event["taskToken"]
    spec = KINDS[kind]
    case = get_case(case_id)
    if case.get("status") not in spec["allowedFrom"]:
        raise ValueError(f"Case {case_id} is {case.get('status')}; cannot wait for {kind}")
    update_case(case_id, {"taskToken": token, "taskKind": kind, "status": spec["status"]},
                "workflow", f"waitFor{kind.capitalize()}", f"waiting for {kind}")

    if kind == "approval" and case.get("approver"):
        approver = from_ddb(adjusters_table().get_item(Key={"adjusterId": case["approver"]}).get("Item") or {})
        summary = case.get("summary", {})
        body = (f"Hello {approver.get('name', 'Approver')},\n\n"
                f"Case {case_id} ({case.get('claimType')}, {case.get('claimedAmount')}) is ready for your decision.\n\n"
                f"Summary: {summary.get('facts', 'see portal')[:800]}\n"
                f"Recommended next action: {summary.get('nextAction', 'see portal')}\n\n"
                f"Review and decide in the portal (sign-in required): {portal_link(case_id)}\n")
        send_email(approver.get("email", ""), f"[{case_id}] Approval needed", body)
    return {"caseId": case_id, "waitingFor": kind}
