"""Gateway target 'routing' — tools for the Routing agent.

Tools: getWorkload, assignCase, requestApproval

There is deliberately no tool that approves a claim. requestApproval only marks
the case ready; the Step Functions workflow then waits for a person to decide in
the portal.
"""
from __future__ import annotations

from shared.claims_common import (
    ToolError, adjusters_table, dispatch, from_ddb, get_case, require, update_case,
)

ACTOR = "routing-agent"
APPROVER_ROLES = ("approver", "senior_approver")


def all_staff() -> list[dict]:
    table = adjusters_table()
    items, kwargs = [], {}
    while True:
        page = table.scan(**kwargs)
        items.extend(from_ddb(page.get("Items", [])))
        if "LastEvaluatedKey" not in page:
            return items
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def _covers_region(person: dict, region: str) -> bool:
    regions = person.get("regions", [])
    return "ALL" in regions or region in regions


def tool_get_workload(args: dict) -> dict:
    """Candidate adjusters and approvers for a case, least busy first."""
    require(args, "caseId")
    case = get_case(args["caseId"])
    region, claim_type = case.get("region", ""), case.get("claimType", "")
    amount = float(case.get("claimedAmount", 0))
    staff = all_staff()
    adjusters = sorted(
        (p for p in staff if p.get("role") == "adjuster" and _covers_region(p, region)
         and claim_type in p.get("specialties", [])),
        key=lambda p: (p.get("openCases", 0), p["adjusterId"]))
    approvers = sorted(
        (p for p in staff if p.get("role") in APPROVER_ROLES and _covers_region(p, region)
         and float(p.get("approvalLimit", 0)) >= amount),
        key=lambda p: (float(p.get("approvalLimit", 0)), p.get("openCases", 0), p["adjusterId"]))

    def view(p):
        return {k: p.get(k) for k in ("adjusterId", "name", "role", "regions", "specialties", "approvalLimit", "openCases")}

    return {"caseId": case["caseId"], "region": region, "claimType": claim_type, "claimedAmount": amount,
            "adjusters": [view(p) for p in adjusters], "approvers": [view(p) for p in approvers],
            "rule": "Pick the first adjuster and the first approver unless there is a stated reason not to."}


def tool_assign_case(args: dict) -> dict:
    require(args, "caseId", "adjusterId", "approverId")
    case = get_case(args["caseId"])
    if case.get("status") not in ("SUMMARIZED", "ASSIGNED"):
        raise ToolError(f"Case is {case.get('status')}; it must be SUMMARIZED before assignment")
    staff = {p["adjusterId"]: p for p in all_staff()}
    adjuster = staff.get(args["adjusterId"])
    approver = staff.get(args["approverId"])
    if not adjuster or adjuster.get("role") != "adjuster":
        raise ToolError(f"{args['adjusterId']} is not an adjuster")
    if not approver or approver.get("role") not in APPROVER_ROLES:
        raise ToolError(f"{args['approverId']} is not an approver")
    amount = float(case.get("claimedAmount", 0))
    if float(approver.get("approvalLimit", 0)) < amount:
        raise ToolError(f"{approver['name']} can approve up to {approver.get('approvalLimit')}; "
                        f"this claim is {amount:,.2f}. Choose an approver with a higher limit.")
    if not _covers_region(adjuster, case.get("region", "")) or not _covers_region(approver, case.get("region", "")):
        raise ToolError("The adjuster and approver must both cover the case's region")

    updated = update_case(case["caseId"], {
        "assignee": adjuster["adjusterId"], "assigneeName": adjuster["name"],
        "approver": approver["adjusterId"], "approverName": approver["name"],
        "approverUsername": approver.get("username", ""),
        "assignmentReason": (args.get("reason") or "")[:500],
        "status": "ASSIGNED",
    }, ACTOR, "assignCase", f"assignee {adjuster['name']}, approver {approver['name']}")
    adjusters_table().update_item(Key={"adjusterId": adjuster["adjusterId"]},
                                  UpdateExpression="ADD openCases :one", ExpressionAttributeValues={":one": 1})
    return {"caseId": updated["caseId"], "status": updated["status"],
            "assignee": adjuster["name"], "approver": approver["name"]}


def tool_request_approval(args: dict) -> dict:
    require(args, "caseId")
    case = get_case(args["caseId"])
    if case.get("status") != "ASSIGNED":
        raise ToolError(f"Case is {case.get('status')}; assign it before requesting approval")
    update_case(case["caseId"], {"status": "READY_FOR_APPROVAL"}, ACTOR, "requestApproval",
                f"approval requested from {case.get('approverName')}")
    return {"caseId": case["caseId"], "status": "READY_FOR_APPROVAL",
            "note": "The approver decides in the portal. Agents cannot approve or reject claims."}


TOOLS = {"getWorkload": tool_get_workload, "assignCase": tool_assign_case, "requestApproval": tool_request_approval}


def handler(event, context):
    return dispatch(event, context, TOOLS, ACTOR)
