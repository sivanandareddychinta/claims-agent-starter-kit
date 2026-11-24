#!/usr/bin/env python3
"""Create (or reset) the EVAL-... cases that golden_scenarios.json refers to.

Run before every evaluation: agents change case state, so each run needs fresh
SUBMITTED cases. Also creates EVAL-20261001-PK01 (needed by the duplicate scenario).

    python evaluation/prepare_eval_cases.py --lab-id lab
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from kit import base_parser, create_case_from_pack, eval_case_id, outputs, session  # noqa: E402

HERE = Path(__file__).resolve().parent


def packs_in_dataset(path: Path) -> list[str]:
    scenarios = json.loads(path.read_text())["scenarios"]
    packs = {s["metadata"]["packId"] for s in scenarios}
    packs.add("PACK-01")
    return sorted(packs)


def main():
    p = base_parser(__doc__)
    p.add_argument("--dataset", default=str(HERE / "golden_scenarios.json"))
    args = p.parse_args()
    sess = session(args.region)
    out = outputs(sess, args.lab_id)
    for pack in packs_in_dataset(Path(args.dataset)):
        case_id = eval_case_id(pack)
        create_case_from_pack(sess, out, pack, case_id)
        print(f"{case_id} <- {pack} (SUBMITTED)")


if __name__ == "__main__":
    main()
