"""Claims multi-agent system for Amazon Bedrock AgentCore Runtime (Strands Agents).

Copy this file over the main.py that `agentcore create` generated for your agent,
and copy prompts/ and lab-config.json next to it (see agents/README.md).

Shape: one supervisor agent; five specialist agents exposed to it as tools
("agents as tools"); all of them deployed together as ONE Runtime. Specialists
reach the Lambda tools through AgentCore Gateway (IAM / SigV4 auth), and each
specialist gets only the Gateway tools it needs.

Environment variables
  GATEWAY_URL        your gateway URL (from register_gateway_targets.py output)   [required]
  GUARDRAIL_ID       Bedrock Guardrail applied to every model call (Task 6)        [optional]
  GUARDRAIL_VERSION  defaults to DRAFT
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
from strands import Agent, tool
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

HERE = Path(__file__).parent
CONFIG = json.loads((HERE / "lab-config.json").read_text())
REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))
GATEWAY_URL = os.environ.get(
    "GATEWAY_URL",
    "https://claimsagentsve20692364-default-claimsgateway-s2lcet4i0v.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
)
GUARDRAIL_ID = os.environ.get("GUARDRAIL_ID", "6nuipta7hy2i")
GUARDRAIL_VERSION = os.environ.get("GUARDRAIL_VERSION", "DRAFT")

# Gateway tool names are "<target>___<tool>" (see scripts/register_gateway_targets.py).
# Least privilege: each agent sees only the tools listed here.
TOOLSETS = {
    "supervisor": ["case___getCase"],
    "intake": ["case___getCase", "intake___getPolicy", "intake___findSimilarCases", "intake___createCase"],
    "document": ["case___getCase", "case___getDocuments", "document___extractDocument", "document___classifyDocument", "document___validateFields"],
    "summary": ["case___getCase", "case___getDocuments", "case___saveSummary"],
    "routing": ["case___getCase", "routing___getWorkload", "routing___assignCase", "routing___requestApproval"],
    "sla_comms": ["case___getCase", "comms___scheduleSla", "comms___sendNotification", "comms___requestDocuments"],
}

app = BedrockAgentCoreApp()
_gateway_tools: dict = {}   # filled once per invocation while the Gateway session is open


def model(role: str) -> BedrockModel:
    kwargs = {"model_id": CONFIG["models"][role], "temperature": 0.1, "max_tokens": 2000}
    if GUARDRAIL_ID:
        kwargs.update(guardrail_id=GUARDRAIL_ID, guardrail_version=GUARDRAIL_VERSION)
    return BedrockModel(**kwargs)


def system_prompt(role: str) -> str:
    return (HERE / "prompts" / f"{role}.md").read_text()


def run_specialist(role: str, request: str) -> str:
    """Build a fresh specialist for each call (no context bleeds between cases)."""
    tools = [_gateway_tools[name] for name in TOOLSETS[role] if name in _gateway_tools]
    missing = [name for name in TOOLSETS[role] if name not in _gateway_tools]
    if missing:
        return f"ERROR: Gateway tools not available: {missing}. Check the Gateway targets."
    agent = Agent(name=f"{role}_agent", model=model(role), system_prompt=system_prompt(role),
                  tools=tools, callback_handler=None)
    return str(agent(request))


# ---------------------------------------------------------------------------
# Specialists, exposed to the supervisor as tools. The docstring is what the
# supervisor reads when deciding which specialist to call.
# ---------------------------------------------------------------------------
@tool
def intake_agent(request: str) -> str:
    """Intake specialist. Registers a SUBMITTED claim as a case: checks the policy,
    looks for duplicate claims and records the result. Call first for a new claim.

    Args:
        request: Instruction for the specialist, including the case ID.
    """
    return run_specialist("intake", request)


@tool
def document_agent(request: str) -> str:
    """Document specialist. Extracts, classifies and validates every document on a
    registered case, and reports missing documents and low-confidence results.

    Args:
        request: Instruction for the specialist, including the case ID.
    """
    return run_specialist("document", request)


@tool
def summary_agent(request: str) -> str:
    """Summary specialist. Builds and saves the structured case summary (facts,
    evidence, missing information, risks, next action) after documents are processed.

    Args:
        request: Instruction for the specialist, including the case ID.
    """
    return run_specialist("summary", request)


@tool
def routing_agent(request: str) -> str:
    """Routing specialist. Assigns an adjuster and an approver whose limit covers the
    claim, then requests approval. It cannot approve or reject claims.

    Args:
        request: Instruction for the specialist, including the case ID.
    """
    return run_specialist("routing", request)


@tool
def sla_comms_agent(request: str) -> str:
    """SLA and communications specialist. Schedules SLA deadlines, emails the
    approver, assignee, claimant or supervisor, and requests missing documents.

    Args:
        request: Instruction for the specialist, including the case ID and what to send or schedule.
    """
    return run_specialist("sla_comms", request)


SPECIALISTS = [intake_agent, document_agent, summary_agent, routing_agent, sla_comms_agent]


def gateway_client() -> MCPClient:
    if not GATEWAY_URL:
        raise RuntimeError("Set the GATEWAY_URL environment variable to your AgentCore Gateway URL")
    return MCPClient(lambda: aws_iam_streamablehttp_client(
        endpoint=GATEWAY_URL, aws_region=REGION, aws_service="bedrock-agentcore"))


@app.entrypoint
def invoke(payload, context=None):
    prompt = payload.get("prompt") if isinstance(payload, dict) else None
    # Security: only ever pass a plain string to the agent (see AgentCore Runtime security best practices).
    if not isinstance(prompt, str) or not prompt.strip():
        return {"error": "payload.prompt must be a non-empty string"}

    with gateway_client() as client:
        _gateway_tools.clear()
        _gateway_tools.update({t.tool_name: t for t in client.list_tools_sync()})

        supervisor_tools = SPECIALISTS + [_gateway_tools[t] for t in TOOLSETS["supervisor"] if t in _gateway_tools]
        agent = Agent(name="supervisor_agent", model=model("supervisor"),
                      system_prompt=system_prompt("supervisor"),
                      tools=supervisor_tools, callback_handler=None)
        result = agent(prompt)
        return {"result": str(result)}


if __name__ == "__main__":
    app.run()
