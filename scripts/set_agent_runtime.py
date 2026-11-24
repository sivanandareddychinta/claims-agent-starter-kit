#!/usr/bin/env python3
"""Point the workflow at your deployed supervisor and let the Runtime call the Gateway.

    python scripts/set_agent_runtime.py --lab-id lab --runtime-name ClaimsSupervisor --gateway-name ClaimsGateway
    python scripts/set_agent_runtime.py --lab-id lab --runtime-name ClaimsSupervisor --qualifier prod

Writes <ParamPrefix>/agent/runtime-arn and /agent/qualifier in Parameter Store
(read by the InvokeSupervisor Lambda) and, with --gateway-name, grants the
Runtime's role bedrock-agentcore:InvokeGateway on that gateway.
"""
import json
import sys

from kit import base_parser, outputs, session


def find_runtime(ctl, name: str) -> dict:
    token = None
    while True:
        page = ctl.list_agent_runtimes(**({"nextToken": token} if token else {}))
        for r in page.get("agentRuntimes", []):
            if name.lower() in r["agentRuntimeName"].lower() or name in r["agentRuntimeId"]:
                return ctl.get_agent_runtime(agentRuntimeId=r["agentRuntimeId"])
        token = page.get("nextToken")
        if not token:
            sys.exit(f"No AgentCore Runtime named '{name}'. Run `agentcore deploy` first, or check `agentcore status`.")


def main():
    p = base_parser(__doc__)
    p.add_argument("--runtime-name", required=True, help="AgentCore Runtime name or ID (see `agentcore status`)")
    p.add_argument("--qualifier", default="DEFAULT", help="endpoint the workflow calls: DEFAULT, prod, canary")
    p.add_argument("--gateway-name", help="grant the Runtime role permission to call this gateway")
    args = p.parse_args()
    sess = session(args.region)
    out = outputs(sess, args.lab_id)
    ctl = sess.client("bedrock-agentcore-control")
    rt = find_runtime(ctl, args.runtime_name)
    ssm = sess.client("ssm")
    prefix = out["ParamPrefix"]
    ssm.put_parameter(Name=f"{prefix}/agent/runtime-arn", Value=rt["agentRuntimeArn"], Type="String", Overwrite=True)
    ssm.put_parameter(Name=f"{prefix}/agent/qualifier", Value=args.qualifier, Type="String", Overwrite=True)
    print(f"Workflow now calls {rt['agentRuntimeArn']} (endpoint {args.qualifier})")

    if args.gateway_name:
        token = None
        gws = []
        while True:
            page = ctl.list_gateways(**({"nextToken": token} if token else {}))
            gws.extend(page.get("items", []))
            token = page.get("nextToken")
            if not token:
                break
        gw = next((g for g in gws if args.gateway_name.lower() in g.get("name", "").lower() and (not args.lab_id or args.lab_id.lower() in g.get("name", "").lower())), None)
        if not gw:
            sys.exit(f"No gateway named {args.gateway_name}")
        gw_arn = ctl.get_gateway(gatewayIdentifier=gw["gatewayId"])["gatewayArn"]
        role = rt["roleArn"].split("/")[-1]
        sess.client("iam").put_role_policy(RoleName=role, PolicyName="ClaimsLabInvokeGateway", PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [{"Effect": "Allow", "Action": "bedrock-agentcore:InvokeGateway", "Resource": gw_arn}]}))
        print(f"Runtime role {role} may now invoke {gw_arn}")


if __name__ == "__main__":
    main()
