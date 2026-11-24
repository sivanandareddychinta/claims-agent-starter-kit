"""Step Functions task: record the final outcome of a case.

Input: {"caseId": "...", "outcome": "APPROVED" | "REJECTED" | "MANUAL_REVIEW", "error": {...}?}
"""
from __future__ import annotations

import json

from shared.claims_common import adjusters_table, get_case, update_case

OUTCOMES = ("APPROVED", "REJECTED", "MANUAL_REVIEW")


def handler(event, _context):
    case_id, outcome = event["caseId"], event["outcome"]
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}")
    case = get_case(case_id, include_hidden=True)
    fields = {"status": outcome}
    detail = f"case {outcome.lower().replace('_', ' ')}"
    if outcome == "MANUAL_REVIEW":
        error = event.get("error") or {}
        cause = error.get("Cause", "")
        try:
            cause = json.loads(cause).get("errorMessage", cause)
        except (ValueError, AttributeError):
            pass
        fields["manualReviewReason"] = f"{error.get('Error', 'Unknown')}: {str(cause)[:800]}"
        detail = "sent to manual review: " + fields["manualReviewReason"][:300]
    remove = ["taskToken", "taskKind"] if "taskToken" in case else None
    update_case(case_id, fields, "workflow", "finalize", detail, remove=remove)

    if outcome in ("APPROVED", "REJECTED") and case.get("assignee"):
        try:
            adjusters_table().update_item(
                Key={"adjusterId": case["assignee"]},
                UpdateExpression="ADD openCases :minus",
                ConditionExpression="openCases > :zero",
                ExpressionAttributeValues={":minus": -1, ":zero": 0})
        except adjusters_table().meta.client.exceptions.ConditionalCheckFailedException:
            pass
    return {"caseId": case_id, "status": outcome}
