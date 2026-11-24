"""Gateway target 'intake' — tools for the Intake agent.

Tools: getPolicy, findSimilarCases, createCase
"""
from __future__ import annotations

from datetime import datetime, timedelta

from shared.claims_common import (
    Key, ToolError, cases_table, check_case_id, dispatch, from_ddb, get_case,
    get_policy, normalise_name, require, update_case,
)

ACTOR = "intake-agent"
DUPLICATE_WINDOW_DAYS = 3


def tool_get_policy(args: dict) -> dict:
    require(args, "policyNumber")
    policy = get_policy(args["policyNumber"])
    if not policy:
        return {"found": False, "policyNumber": args["policyNumber"].upper()}
    return {"found": True, "policy": policy}


def tool_find_similar_cases(args: dict) -> dict:
    """Possible duplicates: other cases on the same policy whose incident date is
    within DUPLICATE_WINDOW_DAYS of this case's incident date."""
    require(args, "caseId")
    case = get_case(args["caseId"])
    incident = case.get("incidentDate")
    if not incident:
        raise ToolError("Case has no incident date, so duplicates cannot be checked")
    start = (datetime.fromisoformat(incident) - timedelta(days=DUPLICATE_WINDOW_DAYS)).strftime("%Y-%m-%d")
    end = (datetime.fromisoformat(incident) + timedelta(days=DUPLICATE_WINDOW_DAYS)).strftime("%Y-%m-%d")

    res = cases_table().query(
        IndexName="policy-index",
        KeyConditionExpression=Key("policyNumber").eq(case["policyNumber"]) & Key("incidentDate").between(start, end),
    )
    items = [from_ddb(it) for it in res.get("Items", [])]

    similar = []
    for other in items:
        if other["caseId"] == case["caseId"]:
            continue
        if case["caseId"].startswith("EVAL-") and not other["caseId"].startswith("EVAL-"):
            continue
        amount_gap = abs(float(other.get("claimedAmount", 0)) - float(case.get("claimedAmount", 0)))
        similar.append({
            "caseId": other["caseId"],
            "status": other.get("status"),
            "incidentDate": other.get("incidentDate"),
            "claimedAmount": other.get("claimedAmount"),
            "sameClaimant": normalise_name(other.get("claimantName")) == normalise_name(case.get("claimantName")),
            "amountDifference": round(amount_gap, 2),
        })
    return {"caseId": case["caseId"], "window": [start, end], "similarCases": similar,
            "likelyDuplicate": any(s["sameClaimant"] for s in similar)}


def coverage_check(case: dict, policy: dict | None) -> list[str]:
    """Deterministic policy checks. Returns a list of reasons the claim is out of policy."""
    if not policy:
        return [f"Policy {case.get('policyNumber')} does not exist"]
    reasons = []
    if policy.get("status") != "ACTIVE":
        reasons.append(f"Policy status is {policy.get('status')}")
    incident = case.get("incidentDate") or ""
    if incident and not (policy["startDate"] <= incident <= policy["endDate"]):
        reasons.append(f"Incident date {incident} is outside the policy period {policy['startDate']} to {policy['endDate']}")
    if case.get("claimType") not in policy.get("coveredClaimTypes", []):
        reasons.append(f"Claim type '{case.get('claimType')}' is not covered by this {policy.get('product')} policy")
    if float(case.get("claimedAmount", 0)) > float(policy.get("sumInsured", 0)):
        reasons.append(f"Claimed amount {case.get('claimedAmount')} exceeds the sum insured {policy.get('sumInsured')}")
    if normalise_name(case.get("claimantName")) != normalise_name(policy.get("holderName")):
        reasons.append(f"Claimant '{case.get('claimantName')}' is not the policy holder '{policy.get('holderName')}'")
    return reasons


def tool_create_case(args: dict) -> dict:
    """Register the submitted claim as a case: run the policy checks, record any
    duplicate the agent found, and move the case to REGISTERED."""
    require(args, "caseId")
    case_id = check_case_id(args["caseId"])
    case = get_case(case_id)
    if case.get("status") not in ("SUBMITTED", "REGISTERED"):
        raise ToolError(f"Case {case_id} is {case.get('status')}; only SUBMITTED cases can be registered")
    policy = get_policy(case.get("policyNumber", ""))
    reasons = coverage_check(case, policy)
    flags = dict(case.get("flags", {}))
    flags["outOfPolicy"] = bool(reasons)
    flags["outOfPolicyReasons"] = reasons
    duplicate_of = (args.get("duplicateOfCaseId") or "").strip().upper()
    if duplicate_of and duplicate_of not in ("NONE", "NULL", "N/A", "NO", "FALSE"):
        flags["duplicateOf"] = check_case_id(duplicate_of)
    else:
        duplicate_of = None
    fields = {
        "status": "REGISTERED",
        "flags": flags,
        "region": (policy or {}).get("region", case.get("region", "UNKNOWN")),
        "intakeNotes": (args.get("intakeNotes") or "")[:1000],
    }
    detail = "Registered" + (f"; out of policy: {'; '.join(reasons)}" if reasons else "; policy checks passed")
    if duplicate_of:
        detail += f"; possible duplicate of {duplicate_of}"
    updated = update_case(case_id, fields, ACTOR, "createCase", detail)
    return {"caseId": case_id, "status": updated["status"], "outOfPolicy": bool(reasons),
            "outOfPolicyReasons": reasons, "duplicateOf": flags.get("duplicateOf"),
            "region": updated.get("region")}


TOOLS = {
    "getPolicy": tool_get_policy,
    "findSimilarCases": tool_find_similar_cases,
    "createCase": tool_create_case,
}


def handler(event, context):
    return dispatch(event, context, TOOLS, ACTOR)
