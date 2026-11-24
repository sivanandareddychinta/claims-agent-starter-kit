# Claims & Case Management Agent — starter kit

Starter kit for the **L300 (Developer): Intelligent Claims Processing & Case Management Agent** lab
(Wipro AWS FDE). It deploys everything around the agents, so your 4 hours go into the
agents, the workflow and the evaluation.

```
Portal (S3 + CloudFront + Cognito) ──► HTTP API ──► Step Functions case workflow
                                                     │  each stage invokes ▼
                                       Supervisor agent on AgentCore Runtime (you build)
                                         ├─ intake / document / summary / routing / sla_comms agents
                                         └─ tools via AgentCore Gateway ──► 5 tool Lambdas (this kit)
                                       DynamoDB case store · S3 documents · Textract · Bedrock
                                       Action history (DynamoDB Streams ► S3 Object Lock) · SES · Scheduler
```

## What is in the kit

| Path | What it is | You touch it in |
|---|---|---|
| `infra/` | AWS CDK app (Python) for all infrastructure | Task 1 (deploy) |
| `lambdas/tools/` | The 15 agent tools, grouped into 5 Gateway targets | Task 1 (2 TODOs) |
| `gateway/tool-schemas/` | Tool definitions registered on the Gateway | read only |
| `agents/` | Strands multi-agent `main.py`, prompts, model config | Tasks 2–4 |
| `workflow/claims_workflow.asl.json` | Step Functions definition (stages after Documents are TODO) | Task 5 |
| `lambdas/workflow/` | Workflow steps: invoke supervisor, human wait, finalize, SLA escalation | Task 5 (2 TODOs) |
| `lambdas/api/` | Portal API (Cognito-protected): claims, uploads, decisions | read only |
| `lambdas/history/` | Action history writer (Object Lock) | Task 6 (1 TODO) |
| `lambdas/evaluation/` | Code-based evaluators for AgentCore Evaluations | Task 7 (1 TODO) |
| `evaluation/` | Golden scenarios, thresholds, dataset runner (CI gate) | Task 7 |
| `portal/` | Adjuster portal (plain HTML/JS) | optional |
| `data/` | 28 policies, 9 staff, 30 labelled synthetic claim packs + generator | read only |
| `scripts/` | Seed data, users, Gateway targets, Runtime pointer, sample claims, endpoints | all tasks |
| `sql/` | Athena queries over the action history | Task 6 |
| `tests/` | Unit tests (moto) — they pass once your TODOs are done | every task |

Every TODO is marked `TODO(Task n)` and raises `NotImplementedError` until you fill it:

```bash
grep -rn "TODO(Task" lambdas agents workflow
```

## Prerequisites

- An AWS account and Region chosen by your facilitator (US East (N. Virginia) recommended).
- Python 3.10+, Node.js 20+, AWS CLI v2 with credentials, AWS CDK (`npm install -g aws-cdk`).
- AgentCore CLI: `npm install -g @aws/agentcore`.
- From your facilitator: your **labId**, the verified **sender email**, and confirmation that
  CloudWatch Transaction Search is enabled and CDK is bootstrapped.

## Task 1 — deploy and publish the tools (~25 min)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r infra/requirements.txt -r tests/requirements.txt

cd infra
cdk deploy -c labId=<labId> -c senderEmail=<verified sender> -c notifyOverrideEmail=<your verified inbox>
cd ..

python scripts/seed_data.py --lab-id <labId>
python scripts/create_lab_users.py --lab-id <labId> --email <you@company.com>
```

Open the `PortalUrl` output and sign in as `adjuster1` with the temporary password.

Now complete the two TODOs in `lambdas/tools/` (`extractDocument` Textract calls and the
`findSimilarCases` query), check them, and redeploy:

```bash
pytest -q tests/test_tools.py        # all green when your TODOs are right
cd infra && cdk deploy -c labId=<labId> ... && cd ..
```

Then create the Gateway and register the tools — see [`agents/README.md`](agents/README.md) §2.

## Tasks 2–4 — agents

Follow [`agents/README.md`](agents/README.md). Create test cases without the portal:

```bash
python scripts/submit_sample_claim.py --lab-id <labId> --pack PACK-01 --no-workflow
```

Each pack in `data/claim_packs/PACK-xx/pack.json` says what should happen (`expected`):

| Packs | Scenario |
|---|---|
| 01–12 | Clean motor, health and property claims |
| 13–15 | Poor scans (blurred, rotated) |
| 16 | Unreadable scan → low confidence → manual review |
| 17–19 | A required document is missing |
| 20–21 | Duplicates of packs 01 and 05 |
| 22–24 | High value → needs a senior approver |
| 25–27 | Out of policy (expired, wrong product, before start date) |
| 28 | Repair invoice contains a prompt-injection instruction |
| 29 | Invoice total ≠ claimed amount |
| 30 | Claimant is not the policy holder |

When the supervisor is deployed, point the workflow at it:

```bash
python scripts/set_agent_runtime.py --lab-id <labId> --runtime-name ClaimsSupervisor --gateway-name ClaimsGateway
```

## Task 5 — workflow and human approval (~45 min)

1. Fill the TODOs in `lambdas/workflow/invoke_supervisor.py` (session ID ≥ 33 characters,
   Runtime call).
2. Replace the three `TODO(Task 5)` Pass states in `workflow/claims_workflow.asl.json`.
   Use `${InvokeSupervisorArn}`, `${HumanTaskArn}` and `${FinalizeCaseArn}`; the CDK stack
   substitutes them. The approval wait uses `arn:aws:states:::lambda:invoke.waitForTaskToken`
   with `"taskToken.$": "$$.Task.Token"`.
3. `pytest -q tests/test_workflow_api.py`, redeploy, then run claims end to end:

```bash
python scripts/submit_sample_claim.py --lab-id <labId> --pack PACK-01
```

Sign in as `approver1` (limit INR 2 lakh) or `approver2` (INR 15 lakh) to decide.
`slaMinutesOverride` (default 10) makes SLAs expire in minutes so escalation is visible;
set it to `0` for real hours.

## Task 6 — govern and audit (~25 min)

- Fill the TODO in `lambdas/history/action_history.py`, redeploy, and query the history with
  `sql/action_history.sql` in Athena.
- Create a Bedrock Guardrail (PII masking, denied topics) and set `GUARDRAIL_ID` on the agent.
- Review IAM: one role per Lambda (see `infra/starter_kit/stack.py`), Gateway uses IAM auth.

## Task 7 — evaluate and release (~25 min)

```bash
pip install bedrock-agentcore
python evaluation/prepare_eval_cases.py --lab-id <labId>
python evaluation/run_dataset_eval.py --lab-id <labId>                     # built-in evaluators
```

Fill the TODO in `lambdas/evaluation/claims_evaluators.py`, redeploy, register the two
code-based evaluators (Lambda ARN = `EvaluatorFunctionArn` output), then gate on them too:

```bash
agentcore add evaluator --name ClaimsFieldAccuracy  --level SESSION --type code-based --lambda-arn <EvaluatorFunctionArn>
agentcore add evaluator --name ClaimsNoAgentApproval --level SESSION --type code-based --lambda-arn <EvaluatorFunctionArn>
agentcore deploy
python evaluation/run_dataset_eval.py --lab-id <labId> --custom <field-accuracy-id> --custom <no-agent-approval-id>
```

Release with versions and endpoints, and roll back by moving the pointer:

```bash
python scripts/promote_endpoint.py --runtime-name ClaimsSupervisor --list
python scripts/promote_endpoint.py --runtime-name ClaimsSupervisor --endpoint prod --version <n>
python scripts/set_agent_runtime.py --lab-id <labId> --runtime-name ClaimsSupervisor --qualifier prod
```

## Deploy-time settings (`cdk deploy -c key=value`)

| Key | Default | Meaning |
|---|---|---|
| `labId` | `lab` | Separates learners sharing an account (stack `ClaimsLab-<labId>`) |
| `senderEmail` | empty | SES-verified sender; empty = emails are skipped |
| `notifyOverrideEmail` | empty | Send every email to this verified inbox (SES sandbox) |
| `slaMinutesOverride` | `10` | SLAs expire after this many minutes; `0` = use real hours |
| `classifierModelId` | `us.amazon.nova-lite-v1:0` | Model used by `classifyDocument` |
| `cognitoDomainPrefix` | `claims-lab-<labId>-<account>` | Must be globally unique |

## Cost and clean-up

Everything is pay-per-use: idle cost is close to zero. Spend comes from model tokens,
Textract pages and Runtime active time, so prefer small models and single test packs
while developing.

```bash
cd infra && cdk destroy -c labId=<labId>
# in your AgentCore project: agentcore remove all && agentcore deploy
```

The action-history bucket is retained (Object Lock, governance mode, 1-day retention);
empty and delete it the next day.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `NotImplementedError: TODO(Task n)` | That TODO is still open — see the comment above it. |
| Case lands in `MANUAL_REVIEW` with `StageIncompleteError` | The agent finished without reaching the expected status. Read the reason on the case page and the trace (`agentcore traces list`). |
| `SSM parameter ... is not set` | Run `scripts/set_agent_runtime.py`. |
| Agent says Gateway tools not available | Run `register_gateway_targets.py`; check `GATEWAY_URL`; check the Runtime role can `InvokeGateway` (`set_agent_runtime.py --gateway-name`). |
| `AccessDeniedException` on a model | Check the model ID in `agents/lab-config.json` is available in your Region. |
| Upload fails in the portal | Files must be PNG, JPEG or single-page PDF; the upload link lasts 15 minutes. |
| No emails | Sender and recipient must be SES-verified while the account is in the SES sandbox. |
