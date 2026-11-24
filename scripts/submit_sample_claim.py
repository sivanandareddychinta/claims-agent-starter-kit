#!/usr/bin/env python3
"""Create a case from a sample pack without using the portal.

    python scripts/submit_sample_claim.py --lab-id lab --pack PACK-01                 # create + start the workflow
    python scripts/submit_sample_claim.py --lab-id lab --pack PACK-01 --no-workflow   # create only (to test agents with `agentcore dev`)
"""
import json
import secrets
import time
from datetime import datetime, timezone

from kit import base_parser, create_case_from_pack, outputs, session


def main():
    p = base_parser(__doc__)
    p.add_argument("--pack", required=True, help="e.g. PACK-01 (see data/claim_packs)")
    p.add_argument("--no-workflow", action="store_true", help="only create the SUBMITTED case")
    p.add_argument("--sla-hours", type=float, default=48)
    args = p.parse_args()
    sess = session(args.region)
    out = outputs(sess, args.lab_id)
    case_id = f"CLM-{datetime.now(timezone.utc):%Y%m%d}-{secrets.token_hex(2).upper()}"
    item = create_case_from_pack(sess, out, args.pack, case_id)
    print(f"Created {case_id} from {args.pack} ({item['claimType']}, {item['claimedAmount']}) with {len(item['documents'])} documents")
    if args.no_workflow:
        print(f'Try it in `agentcore dev`: "Register case {case_id}, process its documents and summarise it."')
        return
    ex = sess.client("stepfunctions").start_execution(
        stateMachineArn=out["StateMachineArn"], name=f"{case_id}-{int(time.time())}",
        input=json.dumps({"caseId": case_id, "slaHours": args.sla_hours}))
    print(f"Workflow started: {ex['executionArn']}")
    print(f"Follow it in the portal: {out['PortalUrl']}/#/cases/{case_id}")


if __name__ == "__main__":
    main()
