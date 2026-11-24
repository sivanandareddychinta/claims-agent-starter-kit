# Agents (Tasks 2–4)

You build the six agents with the AgentCore CLI and Strands. This folder gives you
the parts: `main.py` (the multi-agent wiring, with TODOs), `prompts/` (system
prompts to tune) and `lab-config.json` (one model per agent).

## 1. Scaffold the project (Task 2, step 1)

```bash
npm install -g @aws/agentcore          # AgentCore CLI (Node.js 20+)
cd ~                                    # anywhere outside this kit
agentcore create                        # project ClaimsAgents, agent ClaimsSupervisor
```

In the wizard choose: **Agent → Strands Agents → Amazon Bedrock → short-term memory →
CodeZip**. (`agentcore create --help` shows the equivalent flags.)

The kit's `main.py` keeps each workflow stage stateless — the case record in DynamoDB
is the shared state — so it does not use the Memory resource. Wire Strands' AgentCore
Memory session manager into `invoke()` if you want multi-turn chats to remember
context (optional extension).

## 2. Add the Gateway (Task 1, step 3)

```bash
cd ClaimsAgents
agentcore add gateway                   # name it ClaimsGateway, authorizer: AWS IAM
agentcore deploy
python <kit>/scripts/register_gateway_targets.py --lab-id <labId> --gateway-name ClaimsGateway
```

The script registers the five tool Lambdas as targets. Tool names arrive in your
agent as `<target>___<tool>`, e.g. `intake___getPolicy`.

## 3. Drop in the kit's agent code

```bash
cp <kit>/agents/main.py           app/ClaimsSupervisor/main.py
cp -r <kit>/agents/prompts         app/ClaimsSupervisor/prompts
cp <kit>/agents/lab-config.json   app/ClaimsSupervisor/lab-config.json
```

Add the packages in `requirements.txt` to `app/ClaimsSupervisor/pyproject.toml`
(dependencies list), then set the gateway URL for local runs:

```bash
export GATEWAY_URL=<the gateway URL printed by register_gateway_targets.py>
agentcore dev                          # local server + agent inspector with traces
```

For the deployed Runtime, add `GATEWAY_URL` as an environment variable of the agent
in `agentcore/agentcore.json` (or the console) before `agentcore deploy`.

## 4. Fill the TODOs

| Task | Where in `main.py` | What |
|---|---|---|
| 2–3 | `TOOLSETS` | Give each specialist only the Gateway tools it needs (least privilege). |
| 2 | `document_agent`, `summary_agent` | Call `run_specialist(...)` for the role. `intake_agent` is the worked example. |
| 3 | `routing_agent`, `sla_comms_agent` | Same. |
| 4 | `invoke()` | Build the supervisor with the five specialists as tools and call it. |

Test each specialist with a case created by
`python scripts/submit_sample_claim.py --pack PACK-01 --no-workflow`, e.g. in the
agent inspector: *"Register case CLM-..., process its documents and save the summary."*

## 5. Deploy and connect the workflow (Task 4)

```bash
agentcore deploy
python <kit>/scripts/set_agent_runtime.py --lab-id <labId> --runtime-name ClaimsSupervisor --gateway-name ClaimsGateway
```

Set a short idle-session timeout on the Runtime (for example 120 seconds) — each
workflow stage is one call, so idle microVMs only cost money. Then follow traces with
`agentcore traces list` or in CloudWatch (GenAI Observability).

## Design notes

- **One Runtime, six agents.** Specialists are built fresh for each call inside the
  same Runtime session — no extra Runtimes to pay for or secure.
- **Shared state is the case record.** Agents never pass state to each other in
  memory; tools read and write DynamoDB, so every hand-off is in the audit trail.
- **No approval tool exists.** The Routing agent can only *request* approval; people
  decide in the portal.
- **Payload hygiene.** The entrypoint only passes a plain string prompt to the agent.
