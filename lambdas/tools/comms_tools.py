"""Gateway target 'comms' — tools for the SLA & comms agent.

Tools: scheduleSla, sendNotification, requestDocuments
"""
from __future__ import annotations

import json
import os
from datetime import timedelta

from shared.claims_common import (
    ToolError, adjusters_table, client, dispatch, from_ddb, get_case, now, portal_link,
    require, send_email, update_case,
)

ACTOR = "sla-comms-agent"
SCHEDULE_GROUP = os.environ.get("SCHEDULE_GROUP", "claims-lab-sla")
SLA_TARGET_ARN = os.environ.get("SLA_TARGET_ARN", "")
SCHEDULER_ROLE_ARN = os.environ.get("SCHEDULER_ROLE_ARN", "")
# Lab setting: when > 0, every SLA is this many minutes so escalation can be seen live.
SLA_MINUTES_OVERRIDE = float(os.environ.get("SLA_MINUTES_OVERRIDE", "0") or 0)

SLA_STAGES = {
    "approval": ["READY_FOR_APPROVAL", "PENDING_APPROVAL"],
    "documents": ["AWAITING_DOCUMENTS"],
}
RECIPIENT_ROLES = ("assignee", "approver", "claimant", "supervisor")


def tool_schedule_sla(args: dict) -> dict:
    require(args, "caseId", "stage", "hours")
    stage = args["stage"]
    if stage not in SLA_STAGES:
        raise ToolError(f"stage must be one of {sorted(SLA_STAGES)}")
    try:
        hours = float(args["hours"])
    except (TypeError, ValueError) as exc:
        raise ToolError("hours must be a number") from exc
    if not 0 < hours <= 24 * 30:
        raise ToolError("hours must be between 0 and 720")
    case = get_case(args["caseId"])
    minutes = SLA_MINUTES_OVERRIDE or hours * 60
    due = now() + timedelta(minutes=minutes)
    name = f"{case['caseId']}-{stage}-{due:%H%M%S}"
    client("scheduler").create_schedule(
        Name=name,
        GroupName=SCHEDULE_GROUP,
        ScheduleExpression=f"at({due:%Y-%m-%dT%H:%M:%S})",
        ScheduleExpressionTimezone="UTC",
        FlexibleTimeWindow={"Mode": "OFF"},
        ActionAfterCompletion="DELETE",
        Target={
            "Arn": SLA_TARGET_ARN,
            "RoleArn": SCHEDULER_ROLE_ARN,
            "Input": json.dumps({"caseId": case["caseId"], "stage": stage,
                                 "expectedStatuses": SLA_STAGES[stage], "scheduleName": name}),
        },
    )
    due_iso = due.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    update_case(case["caseId"], {"slaDueAt": due_iso, "slaStage": stage, "slaHours": hours}, ACTOR,
                "scheduleSla", f"{stage} SLA due {due_iso}" + (" (lab override)" if SLA_MINUTES_OVERRIDE else ""))
    print(json.dumps({
        "actor": ACTOR,
        "tool": "scheduleSla",
        "caseId": case["caseId"],
        "stage": stage,
        "slaDueAt": due_iso,
        "slaHours": hours,
        "overrideMinutes": SLA_MINUTES_OVERRIDE
    }))
    return {"caseId": case["caseId"], "stage": stage, "slaDueAt": due_iso, "schedule": name}


def resolve_recipient(case: dict, role: str) -> tuple[str, str]:
    if role == "claimant":
        return case.get("claimantName", "Claimant"), case.get("claimantEmail", "")
    if role in ("assignee", "approver"):
        person_id = case.get(role)
        if not person_id:
            raise ToolError(f"Case has no {role} yet")
        person = from_ddb(adjusters_table().get_item(Key={"adjusterId": person_id}).get("Item") or {})
        return person.get("name", person_id), person.get("email", "")
    for page_item in from_ddb(adjusters_table().scan().get("Items", [])):
        if page_item.get("role") == "supervisor":
            return page_item.get("name", "Supervisor"), page_item.get("email", "")
    raise ToolError("No supervisor is configured")


def tool_send_notification(args: dict) -> dict:
    require(args, "caseId", "recipientRole", "subject", "message")
    role = args["recipientRole"]
    if role not in RECIPIENT_ROLES:
        raise ToolError(f"recipientRole must be one of {RECIPIENT_ROLES}")
    case = get_case(args["caseId"])
    name, address = resolve_recipient(case, role)
    body = f"Dear {name},\n\n{args['message']}\n\nCase: {case['caseId']}\nOpen in the portal: {portal_link(case['caseId'])}\n"
    result = send_email(address, f"[{case['caseId']}] {args['subject']}", body)
    update_case(case["caseId"], {}, ACTOR, "sendNotification",
                f"to {role}: {args['subject'][:100]} ({'sent' if result.get('sent') else result.get('reason')})")
    print(json.dumps({
        "actor": ACTOR,
        "tool": "sendNotification",
        "caseId": case["caseId"],
        "recipientRole": role,
        "subject": args["subject"],
        "emailResult": result
    }))
    return {"caseId": case["caseId"], "recipientRole": role, **result}


def tool_request_documents(args: dict) -> dict:
    require(args, "caseId", "documentTypes", "message")
    doc_types = args["documentTypes"]
    if not isinstance(doc_types, list) or not doc_types:
        raise ToolError("documentTypes must be a non-empty list")
    case = get_case(args["caseId"])
    request = {"documentTypes": [str(d)[:50] for d in doc_types][:10], "message": str(args["message"])[:1000],
               "requestedAt": now().replace(microsecond=0).isoformat().replace("+00:00", "Z")}
    body = (f"Dear {case.get('claimantName', 'Claimant')},\n\n{request['message']}\n\n"
            f"Documents needed: {', '.join(request['documentTypes'])}\n"
            f"Upload them here: {portal_link(case['caseId'])}\n")
    result = send_email(case.get("claimantEmail", ""), f"[{case['caseId']}] Documents needed for your claim", body)
    update_case(case["caseId"], {"missingDocumentRequest": request}, ACTOR, "requestDocuments",
                f"requested {', '.join(request['documentTypes'])}")
    return {"caseId": case["caseId"], "requested": request["documentTypes"], **result}


TOOLS = {"scheduleSla": tool_schedule_sla, "sendNotification": tool_send_notification,
         "requestDocuments": tool_request_documents}


def handler(event, context):
    return dispatch(event, context, TOOLS, ACTOR)
