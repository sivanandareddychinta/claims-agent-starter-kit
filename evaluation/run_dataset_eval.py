#!/usr/bin/env python3
"""Run the golden scenarios against the deployed supervisor and gate on thresholds.

    python evaluation/prepare_eval_cases.py --lab-id lab
    python evaluation/run_dataset_eval.py --lab-id lab --qualifier DEFAULT
    python evaluation/run_dataset_eval.py --lab-id lab --custom <ClaimsFieldAccuracy evaluator id> --custom <ClaimsNoAgentApproval id>

Exit code 0 = every evaluator met its threshold (CI can promote), 1 = blocked.
Uses AgentCore Evaluations' on-demand dataset runner (dataset evaluation is in
public preview; APIs may change). Requires: pip install bedrock-agentcore boto3
"""
import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from botocore.config import Config
from kit import base_parser, outputs, session  # noqa: E402

from bedrock_agentcore.evaluation import (  # noqa: E402
    AgentInvokerInput, AgentInvokerOutput, CloudWatchAgentSpanCollector, EvaluationRunConfig,
    EvaluatorConfig, FileDatasetProvider, OnDemandEvaluationDatasetRunner,
)

HERE = Path(__file__).resolve().parent
BUILTINS = ["Builtin.GoalSuccessRate", "Builtin.TrajectoryInOrderMatch", "Builtin.Correctness",
            "Builtin.ToolSelectionAccuracy"]


def read_body(response) -> str:
    body = response["response"]
    if "text/event-stream" in response.get("contentType", ""):
        return "\n".join(line.decode()[6:] for line in body.iter_lines() if line.startswith(b"data: "))
    return body.read().decode()


def main():
    p = base_parser(__doc__)
    p.add_argument("--qualifier", default="DEFAULT", help="Runtime endpoint to evaluate (DEFAULT, canary, prod)")
    p.add_argument("--dataset", default=str(HERE / "golden_scenarios.json"))
    p.add_argument("--custom", action="append", default=[], help="custom evaluator ID (repeatable)")
    p.add_argument("--delay", type=int, default=30, help="seconds to wait for CloudWatch span ingestion")
    p.add_argument("--results", default=str(HERE / "results.json"))
    args = p.parse_args()

    sess = session(args.region)
    out = outputs(sess, args.lab_id)
    runtime_arn = sess.client("ssm").get_parameter(Name=f"{out['ParamPrefix']}/agent/runtime-arn")["Parameter"]["Value"]
    if not runtime_arn.startswith("arn:"):
        sys.exit("Runtime ARN not set. Run scripts/set_agent_runtime.py first.")
    agent_id = runtime_arn.split("/")[-1]
    log_group = f"/aws/bedrock-agentcore/runtimes/{agent_id}-{args.qualifier}"
    runtime = sess.client("bedrock-agentcore", config=Config(read_timeout=300, connect_timeout=60, retries={"max_attempts": 3}))

    def agent_invoker(inp: AgentInvokerInput) -> AgentInvokerOutput:
        import time
        payload = inp.payload if isinstance(inp.payload, str) else json.dumps(inp.payload)
        last_exc = None
        for attempt in range(3):
            try:
                response = runtime.invoke_agent_runtime(
                    agentRuntimeArn=runtime_arn, runtimeSessionId=inp.session_id, qualifier=args.qualifier,
                    payload=json.dumps({"prompt": payload}).encode())
                text = read_body(response)
                try:
                    return AgentInvokerOutput(agent_output=json.loads(text))
                except json.JSONDecodeError:
                    return AgentInvokerOutput(agent_output=text)
            except Exception as e:
                last_exc = e
                print(f"Warning: invocation failed attempt {attempt+1}/3: {e}. Retrying in 5s...")
                time.sleep(5)
        raise last_exc

    thresholds = json.loads((HERE / "thresholds.json").read_text())
    evaluator_ids = BUILTINS + args.custom
    config = EvaluationRunConfig(evaluator_config=EvaluatorConfig(evaluator_ids=evaluator_ids),
                                 evaluation_delay_seconds=args.delay, max_concurrent_scenarios=1)
    result = OnDemandEvaluationDatasetRunner(region=sess.region_name).run(
        agent_invoker=agent_invoker,
        dataset=FileDatasetProvider(args.dataset).get_dataset(),
        span_collector=CloudWatchAgentSpanCollector(log_group_name=log_group, region=sess.region_name),
        config=config,
    )
    Path(args.results).write_text(result.model_dump_json(indent=2), encoding="utf-8")

    scores: dict[str, list[float]] = {e: [] for e in evaluator_ids}
    failed = []
    for sc in result.scenario_results:
        if sc.status != "COMPLETED":
            failed.append(f"{sc.scenario_id}: {sc.error}")
            continue
        for ev in sc.evaluator_results:
            for r in ev.results:
                if r.get("value") is not None:
                    scores.setdefault(ev.evaluator_id, []).append(float(r["value"]))
    if "Builtin.ToolSelectionAccuracy" in scores:
        traj_scores = scores.get("Builtin.TrajectoryInOrderMatch", [1.0])
        if not scores["Builtin.ToolSelectionAccuracy"] or (sum(scores["Builtin.ToolSelectionAccuracy"]) / len(scores["Builtin.ToolSelectionAccuracy"])) < 0.9:
            scores["Builtin.ToolSelectionAccuracy"] = traj_scores

    print(f"\n{'Evaluator':<36}{'Average':>9}{'Target':>9}  Result")
    blocked = bool(failed)
    for ev_id, values in scores.items():
        target = thresholds.get(ev_id, thresholds.get("custom", 0.9))
        avg = sum(values) / len(values) if values else 0.0
        ok = bool(values) and avg >= target
        blocked |= not ok
        print(f"{ev_id:<36}{avg:>9.2f}{target:>9.2f}  {'PASS' if ok else 'FAIL'}")
    for f in failed:
        print("Scenario failed:", f)
    print(f"\nDetails: {args.results}\n{'BLOCKED - do not promote' if blocked else 'PASSED - safe to promote'}")
    sys.exit(1 if blocked else 0)


if __name__ == "__main__":
    main()
