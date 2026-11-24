#!/usr/bin/env python3
"""Manage named Runtime endpoints for release and rollback (Task 7).

    python scripts/promote_endpoint.py --runtime-name ClaimsSupervisor --list
    python scripts/promote_endpoint.py --runtime-name ClaimsSupervisor --endpoint prod --version 3
    python scripts/promote_endpoint.py --runtime-name ClaimsSupervisor --endpoint prod --version 2   # rollback

Each `agentcore deploy` creates an immutable version; DEFAULT follows the latest.
The workflow calls the endpoint named in Parameter Store (set_agent_runtime.py --qualifier prod).
"""
import sys

from kit import base_parser, session
from set_agent_runtime import find_runtime


def main():
    p = base_parser(__doc__)
    p.add_argument("--runtime-name", required=True)
    p.add_argument("--endpoint", help="endpoint name, e.g. prod or canary")
    p.add_argument("--version", help="Runtime version the endpoint should serve")
    p.add_argument("--list", action="store_true", help="list versions and endpoints")
    args = p.parse_args()
    ctl = session(args.region).client("bedrock-agentcore-control")
    rt = find_runtime(ctl, args.runtime_name)
    rid = rt["agentRuntimeId"]
    endpoints = {e["name"]: e for e in ctl.list_agent_runtime_endpoints(agentRuntimeId=rid).get("runtimeEndpoints", [])}
    if args.list or not args.endpoint:
        versions = ctl.list_agent_runtime_versions(agentRuntimeId=rid).get("agentRuntimes", [])
        print("Versions:", ", ".join(f"{v['agentRuntimeVersion']} ({v['status']})" for v in versions))
        for name, e in endpoints.items():
            print(f"Endpoint {name}: live version {e.get('liveVersion')} target {e.get('targetVersion')} [{e.get('status')}]")
        return
    if not args.version:
        sys.exit("--version is required")
    if args.endpoint in endpoints:
        ctl.update_agent_runtime_endpoint(agentRuntimeId=rid, endpointName=args.endpoint, agentRuntimeVersion=args.version)
        print(f"Endpoint {args.endpoint} -> version {args.version} (updating)")
    else:
        ctl.create_agent_runtime_endpoint(agentRuntimeId=rid, name=args.endpoint, agentRuntimeVersion=args.version)
        print(f"Created endpoint {args.endpoint} on version {args.version}")


if __name__ == "__main__":
    main()
