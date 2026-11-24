#!/usr/bin/env python3
"""Register the five tool Lambdas as targets on your AgentCore Gateway.

Run after `agentcore add gateway` + `agentcore deploy` created the gateway:

    python scripts/register_gateway_targets.py --lab-id lab --gateway-name ClaimsGateway

It also attaches the stack's GatewayInvokePolicy to the gateway's IAM role so
the gateway may invoke the Lambdas. Safe to re-run: existing targets are skipped.
Target names become tool-name prefixes: intake___getPolicy, document___extractDocument, ...
"""
import json
import sys
import time

from kit import KIT_ROOT, base_parser, outputs, session

TARGETS = {
    "intake": ("IntakeToolsArn", "Intake agent tools: policy lookup, duplicate check, registration"),
    "document": ("DocumentToolsArn", "Document agent tools: Textract extraction, classification, validation"),
    "case": ("CaseToolsArn", "Case read tools and summary saving"),
    "routing": ("RoutingToolsArn", "Routing agent tools: workload, assignment, approval request"),
    "comms": ("CommsToolsArn", "SLA & comms agent tools: SLA timers, notifications, document requests"),
}


def find_gateway(ctl, name_or_id: str) -> dict:
    token = None
    while True:
        page = ctl.list_gateways(**({"nextToken": token} if token else {}))
        for g in page.get("items", []):
            if name_or_id in (g["gatewayId"], g["name"]) or g["name"].lower().startswith(name_or_id.lower()):
                return ctl.get_gateway(gatewayIdentifier=g["gatewayId"])
        token = page.get("nextToken")
        if not token:
            sys.exit(f"No gateway named '{name_or_id}'. Run `agentcore add gateway` and `agentcore deploy` first.")


def main():
    p = base_parser(__doc__)
    p.add_argument("--gateway-name", required=True, help="gateway name or ID")
    args = p.parse_args()
    sess = session(args.region)
    out = outputs(sess, args.lab_id)
    ctl = sess.client("bedrock-agentcore-control")
    gw = find_gateway(ctl, args.gateway_name)
    gw_id = gw["gatewayId"]
    print(f"Gateway {gw['name']} ({gw_id}) {gw.get('gatewayUrl', '')}")

    role_name = gw["roleArn"].split("/")[-1]
    sess.client("iam").attach_role_policy(RoleName=role_name, PolicyArn=out["GatewayInvokePolicyArn"])
    print(f"Attached tool-invoke policy to gateway role {role_name}")

    existing = {t["name"] for t in ctl.list_gateway_targets(gatewayIdentifier=gw_id).get("items", [])}
    for name, (output_key, description) in TARGETS.items():
        if name in existing:
            print(f"  {name}: already registered")
            continue
        schema = json.loads((KIT_ROOT / "gateway" / "tool-schemas" / f"{name}.json").read_text())
        ctl.create_gateway_target(
            gatewayIdentifier=gw_id, name=name, description=description,
            targetConfiguration={"mcp": {"lambda": {"lambdaArn": out[output_key], "toolSchema": {"inlinePayload": schema}}}},
            credentialProviderConfigurations=[{"credentialProviderType": "GATEWAY_IAM_ROLE"}],
        )
        print(f"  {name}: registered {len(schema)} tools -> {out[output_key].split(':')[-1]}")
        time.sleep(1)
    print("\nNext: set GATEWAY_URL for your agent to the gateway URL above.")


if __name__ == "__main__":
    main()
