"""Step Functions task: run one workflow stage by invoking the supervisor agent
on AgentCore Runtime, then report the case state the agents left behind.

The workflow trusts the case record in DynamoDB, not the agent's prose: after
each stage this function checks that the case reached the expected status and
raises StageIncompleteError otherwise, which the workflow sends to manual review.

Input:  {"caseId": "...", "stage": "intake|documents|summary|routing|more_info|close", "context": {...}}
Output: {"caseId", "stage", "status", "lowConfidence", "outOfPolicy", "duplicateOf", "agentReply"}
"""
from __future__ import annotations

import json
import os
import time
import uuid

from shared.claims_common import client, get_case

RUNTIME_ARN_PARAM = os.environ.get("RUNTIME_ARN_PARAM", "/claims-lab/agent/runtime-arn")
QUALIFIER_PARAM = os.environ.get("QUALIFIER_PARAM", "/claims-lab/agent/qualifier")
_CONFIG_TTL_SECONDS = 60
_config_cache: dict = {}


class AgentInvocationError(Exception):
    """The Runtime call failed (retried by the workflow)."""


class StageIncompleteError(Exception):
    """The agents finished but the case did not reach the expected status."""


STAGES = {
    "intake": {
        "expected": ["REGISTERED"],
        "prompt": ("Workflow stage: INTAKE for case {caseId}.\n"
                   "Call intake_agent to register this case: check the policy, check for duplicates, "
                   "then register it. Reply with one short paragraph on the outcome."),
    },
    "documents": {
        "expected": ["DOCUMENTS_PROCESSED"],
        "prompt": ("Workflow stage: DOCUMENTS for case {caseId}.\n"
                   "Call document_agent to extract, classify and validate every document on the case. "
                   "Reply with one short paragraph listing missing documents and issues."),
    },
    "summary": {
        "expected": ["SUMMARIZED"],
        "prompt": ("Workflow stage: SUMMARY for case {caseId}.\n"
                   "Call summary_agent to build and save the case summary. Reply with the next action."),
    },
    "routing": {
        "expected": ["READY_FOR_APPROVAL"],
        "prompt": ("Workflow stage: ROUTING for case {caseId}.\n"
                   "Call routing_agent to assign an adjuster and an approver and request approval. "
                   "Then call sla_comms_agent to schedule a {slaHours}-hour approval SLA and notify the approver. "
                   "Reply with who was assigned."),
    },
    "more_info": {
        "expected": None,
        "prompt": ("Workflow stage: MORE_INFO for case {caseId}.\n"
                   "The approver asked for more information: \"{rationale}\".\n"
                   "Call sla_comms_agent to request the missing documents from the claimant and schedule a "
                   "{slaHours}-hour documents SLA. Reply with what was requested."),
    },
    "close": {
        "expected": None,
        "prompt": ("Workflow stage: CLOSE for case {caseId}. The final decision is {decision}.\n"
                   "Call sla_comms_agent to notify the claimant of the decision in plain language. "
                   "Do not change the decision."),
    },
}


def _param(name: str, default: str | None = None) -> str:
    cached = _config_cache.get(name)
    if cached and cached[1] > time.time():
        return cached[0]
    try:
        value = client("ssm").get_parameter(Name=name)["Parameter"]["Value"]
    except client("ssm").exceptions.ParameterNotFound:
        if default is None:
            raise AgentInvocationError(f"SSM parameter {name} is not set. Run scripts/set_agent_runtime.py") from None
        value = default
    if value in ("", "not-set"):
        if default is None:
            raise AgentInvocationError(f"SSM parameter {name} is not set. Run scripts/set_agent_runtime.py")
        value = default
    _config_cache[name] = (value, time.time() + _CONFIG_TTL_SECONDS)
    return value


def build_prompt(stage: str, case_id: str, context: dict) -> str:
    values = {"caseId": case_id, "slaHours": context.get("slaHours", 48),
              "rationale": str(context.get("rationale", ""))[:500].replace('"', "'"),
              "decision": context.get("decision", "")}
    return STAGES[stage]["prompt"].format(**values)


def build_session_id(case_id: str, stage: str) -> str:
    return f"{case_id}-{stage}-{uuid.uuid4().hex}"


def read_response(response: dict) -> str:
    """Collect the Runtime response body, whether streamed (SSE) or JSON."""
    body = response.get("response")
    content_type = response.get("contentType", "")
    if body is None:
        return ""
    if "text/event-stream" in content_type:
        parts = []
        for line in body.iter_lines():
            line = line.decode("utf-8") if isinstance(line, bytes) else line
            if line.startswith("data: "):
                parts.append(line[6:])
        return "\n".join(parts)
    raw = body.read()
    return raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)


def invoke_agent(prompt: str, session_id: str, payload_extra: dict) -> str:
    runtime_arn = _param(RUNTIME_ARN_PARAM)
    qualifier = _param(QUALIFIER_PARAM, "DEFAULT")
    payload = json.dumps({"prompt": prompt, **payload_extra}).encode("utf-8")

    try:
        response = client("bedrock-agentcore").invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            runtimeSessionId=session_id,
            qualifier=qualifier,
            payload=payload,
        )
        return read_response(response)
    except Exception as exc:
        raise AgentInvocationError(f"Failed to invoke agent runtime: {exc}") from exc


def handler(event, _context):
    case_id = event["caseId"]
    stage = event["stage"]
    if stage not in STAGES:
        raise ValueError(f"Unknown stage {stage}")
    context = event.get("context") or {}
    session_id = build_session_id(case_id, stage)
    if len(session_id) < 33:
        raise ValueError("Runtime session IDs must be at least 33 characters")
    prompt = build_prompt(stage, case_id, context)
    reply = invoke_agent(prompt, session_id, {"caseId": case_id, "stage": stage})
    print(json.dumps({"caseId": case_id, "stage": stage, "sessionId": session_id, "reply": reply[:2000]}))

    case = get_case(case_id)
    expected = STAGES[stage]["expected"]
    if expected and case.get("status") not in expected:
        raise StageIncompleteError(
            f"After stage '{stage}' case {case_id} is {case.get('status')}, expected {expected}. Agent said: {reply[:300]}")
    flags = case.get("flags", {})
    return {
        "caseId": case_id,
        "stage": stage,
        "status": case.get("status"),
        "lowConfidence": bool(flags.get("lowConfidence")),
        "outOfPolicy": bool(flags.get("outOfPolicy")),
        "duplicateOf": flags.get("duplicateOf") or "",
        "agentReply": reply[:1000],
    }
