"""EventBridge Scheduler target: escalate a case whose SLA expired.

Input: {"caseId": "...", "stage": "approval|documents", "expectedStatuses": [...], "scheduleName": "..."}
If the case has moved on, nothing happens.
"""
from __future__ import annotations

from shared.claims_common import (
    adjusters_table, from_ddb, get_case, portal_link, send_email, update_case,
)


def handler(event, _context):
    case_id = event["caseId"]
    case = get_case(case_id)
    if case.get("status") not in event.get("expectedStatuses", []):
        return {"caseId": case_id, "escalated": False, "reason": f"case is {case.get('status')}"}
    flags = dict(case.get("flags", {}))
    flags["escalated"] = True
    update_case(case_id, {"flags": flags}, "sla-monitor", "slaBreached",
                f"{event.get('stage')} SLA breached while {case.get('status')}")
    supervisors = [p for p in from_ddb(adjusters_table().scan().get("Items", [])) if p.get("role") == "supervisor"]
    for sup in supervisors:
        send_email(sup.get("email", ""), f"[{case_id}] SLA breached ({event.get('stage')})",
                   f"Case {case_id} has been {case.get('status')} past its SLA ({case.get('slaDueAt')}).\n"
                   f"Assignee: {case.get('assigneeName', '-')}, approver: {case.get('approverName', '-')}\n"
                   f"Open in the portal: {portal_link(case_id)}\n")
    return {"caseId": case_id, "escalated": True, "notified": len(supervisors)}
