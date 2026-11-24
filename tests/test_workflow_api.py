"""Workflow Lambdas, portal API, action history, evaluators and the workflow definitions."""
import json
import re

import boto3
import pytest

from api import portal_api
from conftest import KIT, FakeRuntime, FakeStepFunctions, make_case, patch_clients
from evaluation import claims_evaluators
from history import action_history
from shared import claims_common
from workflow import evaluate_summary, finalize_case, human_task, invoke_supervisor, sla_escalation

CID = "CLM-20261016-AAAA"


def set_status(aws, status, **extra):
    names = {"#s": "status"}
    values = {":s": status}
    expr = "SET #s = :s"
    for i, (k, v) in enumerate(extra.items()):
        names[f"#k{i}"], values[f":v{i}"] = k, v
        expr += f", #k{i} = :v{i}"
    aws.ddb.Table("Cases").update_item(Key={"caseId": CID}, UpdateExpression=expr,
                                       ExpressionAttributeNames=names, ExpressionAttributeValues=values)


def case(aws):
    return claims_common.from_ddb(aws.ddb.Table("Cases").get_item(Key={"caseId": CID})["Item"])


# --- invoke_supervisor ---------------------------------------------------------
@pytest.fixture()
def runtime_params(aws):
    ssm = boto3.client("ssm")
    ssm.put_parameter(Name="/claims-lab/agent/runtime-arn", Type="String",
                      Value="arn:aws:bedrock-agentcore:us-east-1:123456789012:runtime/claims-abc")
    ssm.put_parameter(Name="/claims-lab/agent/qualifier", Type="String", Value="prod")
    invoke_supervisor._config_cache.clear()


def test_invoke_supervisor_happy_path(aws, runtime_params, monkeypatch):
    make_case(aws, "PACK-01")
    fake = FakeRuntime(on_invoke=lambda kw: set_status(aws, "REGISTERED"), reply="Registered")
    patch_clients(monkeypatch, invoke_supervisor, **{"bedrock-agentcore": fake})
    out = invoke_supervisor.handler({"caseId": CID, "stage": "intake", "context": {"slaHours": 48}}, None)
    assert out["status"] == "REGISTERED" and "Registered" in out["agentReply"]
    call = fake.calls[0]
    assert len(call["runtimeSessionId"]) >= 33 and call["runtimeSessionId"].startswith(CID + "-intake-")
    assert call["qualifier"] == "prod"
    assert json.loads(call["payload"])["prompt"].startswith("Workflow stage: INTAKE")


def test_invoke_supervisor_stage_incomplete(aws, runtime_params, monkeypatch):
    make_case(aws, "PACK-01")
    patch_clients(monkeypatch, invoke_supervisor, **{"bedrock-agentcore": FakeRuntime()})
    with pytest.raises(invoke_supervisor.StageIncompleteError):
        invoke_supervisor.handler({"caseId": CID, "stage": "intake"}, None)


def test_invoke_supervisor_wraps_runtime_errors(aws, runtime_params, monkeypatch):
    make_case(aws, "PACK-01")

    class Boom:
        def invoke_agent_runtime(self, **_):
            raise RuntimeError("throttled")
    patch_clients(monkeypatch, invoke_supervisor, **{"bedrock-agentcore": Boom()})
    with pytest.raises(invoke_supervisor.AgentInvocationError):
        invoke_supervisor.handler({"caseId": CID, "stage": "intake"}, None)


def test_invoke_supervisor_needs_runtime_param(aws, monkeypatch):
    make_case(aws, "PACK-01")
    invoke_supervisor._config_cache.clear()
    with pytest.raises(invoke_supervisor.AgentInvocationError, match="set_agent_runtime"):
        invoke_supervisor.handler({"caseId": CID, "stage": "intake"}, None)


def test_more_info_prompt_is_quoted_safely():
    prompt = invoke_supervisor.build_prompt("more_info", CID, {"rationale": 'Need "repair invoice"', "slaHours": 24})
    assert "'repair invoice'" in prompt and "24-hour" in prompt


# --- human task / finalize / SLA ----------------------------------------------------
def test_human_task_stores_token_and_emails(aws):
    make_case(aws, "PACK-01", status="READY_FOR_APPROVAL")
    set_status(aws, "READY_FOR_APPROVAL", approver="APR-01")
    human_task.handler({"caseId": CID, "kind": "approval", "taskToken": "tok-1"}, None)
    c = case(aws)
    assert c["status"] == "PENDING_APPROVAL" and c["taskToken"] == "tok-1" and c["taskKind"] == "approval"
    with pytest.raises(ValueError):
        human_task.handler({"caseId": CID, "kind": "approval", "taskToken": "tok-2"}, None)


def test_finalize_and_manual_review(aws):
    make_case(aws, "PACK-01", status="DECIDED")
    set_status(aws, "DECIDED", assignee="ADJ-01", taskToken="t", taskKind="approval")
    finalize_case.handler({"caseId": CID, "outcome": "APPROVED"}, None)
    c = case(aws)
    assert c["status"] == "APPROVED" and "taskToken" not in c
    assert int(aws.ddb.Table("Adjusters").get_item(Key={"adjusterId": "ADJ-01"})["Item"]["openCases"]) == 2
    finalize_case.handler({"caseId": CID, "outcome": "MANUAL_REVIEW",
                           "error": {"Error": "StageIncompleteError", "Cause": json.dumps({"errorMessage": "agent stopped"})}}, None)
    assert case(aws)["manualReviewReason"] == "StageIncompleteError: agent stopped"


def test_sla_escalation_only_when_still_waiting(aws):
    make_case(aws, "PACK-01", status="PENDING_APPROVAL")
    out = sla_escalation.handler({"caseId": CID, "stage": "approval", "expectedStatuses": ["PENDING_APPROVAL"]}, None)
    assert out["escalated"] is True and out["notified"] == 1 and case(aws)["flags"]["escalated"] is True
    set_status(aws, "APPROVED")
    assert sla_escalation.handler({"caseId": CID, "stage": "approval", "expectedStatuses": ["PENDING_APPROVAL"]}, None)["escalated"] is False


# --- portal API ------------------------------------------------------------------
def req(route, user="approver1", groups="[approvers]", body=None, path=None, query=None):
    return {"routeKey": route, "pathParameters": path or {}, "queryStringParameters": query,
            "body": json.dumps(body) if body is not None else None,
            "requestContext": {"authorizer": {"jwt": {"claims": {"username": user, "cognito:groups": groups}}}}}


def test_api_create_submit_and_list(aws, monkeypatch):
    sfn = FakeStepFunctions()
    patch_clients(monkeypatch, portal_api, stepfunctions=sfn)
    body = {"policyNumber": "p-mot-1001", "claimType": "motor", "claimantName": "Diya Das",
            "claimantEmail": "d@example.com", "incidentDate": "2026-09-12", "claimedAmount": "48600",
            "description": "Bumper", "files": [{"fileName": "claim form.png", "contentType": "image/png"}]}
    res = portal_api.handler(req("POST /claims", user="adjuster1", groups="adjusters", body=body), None)
    assert res["statusCode"] == 201, res
    created = json.loads(res["body"])
    case_id = created["caseId"]
    assert re.match(r"^CLM-\d{8}-[A-Z0-9]{4}$", case_id) and "X-Amz-Signature" in created["uploads"][0]["uploadUrl"]
    early = portal_api.handler(req("POST /claims/{caseId}/submit", path={"caseId": case_id}), None)
    assert early["statusCode"] == 409 and "not been uploaded" in early["body"]
    item = claims_common.from_ddb(aws.ddb.Table("Cases").get_item(Key={"caseId": case_id})["Item"])
    aws.s3.put_object(Bucket="docs", Key=item["documents"][0]["s3Key"], Body=b"png")
    ok = portal_api.handler(req("POST /claims/{caseId}/submit", path={"caseId": case_id}), None)
    assert ok["statusCode"] == 202 and sfn.calls[0][0] == "start"
    assert json.loads(sfn.calls[0][1]["input"]) == {"caseId": case_id, "slaHours": 48}
    listed = json.loads(portal_api.handler(req("GET /claims", query={"status": "SUBMITTED"}), None)["body"])
    assert [c["caseId"] for c in listed["cases"]] == [case_id]
    me = json.loads(portal_api.handler(req("GET /me", user="approver1", groups="[approvers]"), None)["body"])
    assert me["adjusterId"] == "APR-01" and me["groups"] == ["approvers"]


def test_api_validation_errors(aws):
    bad = portal_api.handler(req("POST /claims", body={"claimType": "boat"}), None)
    assert bad["statusCode"] == 400
    assert portal_api.handler(req("GET /nope"), None)["statusCode"] == 404
    missing = portal_api.handler(req("GET /claims/{caseId}", path={"caseId": "CLM-20261016-ZZZZ"}), None)
    assert missing["statusCode"] == 404


def test_api_decision_rules(aws, monkeypatch):
    sfn = FakeStepFunctions()
    patch_clients(monkeypatch, portal_api, stepfunctions=sfn)
    make_case(aws, "PACK-01", status="PENDING_APPROVAL")
    set_status(aws, "PENDING_APPROVAL", taskToken="tok", taskKind="approval", approverUsername="approver1", approverName="Kavita Nair")
    path = {"caseId": CID}
    decision = {"decision": "APPROVE", "rationale": "All documents verified"}
    assert portal_api.handler(req("POST /claims/{caseId}/decision", user="adjuster1", groups="adjusters",
                                  body=decision, path=path), None)["statusCode"] == 403
    assert portal_api.handler(req("POST /claims/{caseId}/decision", user="approver2", body=decision, path=path), None)["statusCode"] == 403
    assert portal_api.handler(req("POST /claims/{caseId}/decision", body={"decision": "APPROVE", "rationale": ""},
                                  path=path), None)["statusCode"] == 400
    ok = portal_api.handler(req("POST /claims/{caseId}/decision", body=decision, path=path), None)
    assert ok["statusCode"] == 202, ok
    assert sfn.calls[0][1]["taskToken"] == "tok" and json.loads(sfn.calls[0][1]["output"])["decision"] == "APPROVE"
    c = case(aws)
    assert c["status"] == "DECIDED" and "taskToken" not in c and c["decision"]["decidedBy"] == "approver1"
    again = portal_api.handler(req("POST /claims/{caseId}/decision", body=decision, path=path), None)
    assert again["statusCode"] == 409


def test_api_more_documents_flow(aws, monkeypatch):
    sfn = FakeStepFunctions()
    patch_clients(monkeypatch, portal_api, stepfunctions=sfn)
    make_case(aws, "PACK-17", status="AWAITING_DOCUMENTS")
    set_status(aws, "AWAITING_DOCUMENTS", taskToken="doc-tok", taskKind="documents")
    res = portal_api.handler(req("POST /claims/{caseId}/documents", path={"caseId": CID},
                                 body={"files": [{"fileName": "photo.jpg", "contentType": "image/jpeg"}]}), None)
    assert res["statusCode"] == 201
    not_yet = portal_api.handler(req("POST /claims/{caseId}/documents/complete", path={"caseId": CID}), None)
    assert not_yet["statusCode"] == 409
    new_doc = case(aws)["documents"][-1]
    aws.s3.put_object(Bucket="docs", Key=new_doc["s3Key"], Body=b"jpg")
    done = portal_api.handler(req("POST /claims/{caseId}/documents/complete", path={"caseId": CID}), None)
    assert done["statusCode"] == 202 and sfn.calls[-1][1]["taskToken"] == "doc-tok"
    assert case(aws)["status"] == "DOCUMENTS_RECEIVED"


# --- action history ------------------------------------------------------------------
def test_action_history_writes_redacted_records(aws):
    from boto3.dynamodb.types import TypeSerializer
    ser = TypeSerializer()
    new = {"caseId": CID, "status": "PENDING_APPROVAL", "taskToken": "secret", "lastActor": "workflow",
           "documents": [{"docId": "D1", "text": "ocr text"}],
           "timeline": [{"at": "2026-10-16T10:00:00Z", "actor": "workflow", "action": "waitForApproval", "detail": "x"}]}
    record = {"eventName": "MODIFY", "eventID": "e1", "dynamodb": {
        "ApproximateCreationDateTime": 1792000000, "NewImage": {k: ser.serialize(v) for k, v in new.items()},
        "OldImage": {k: ser.serialize(v) for k, v in {"caseId": CID, "status": "READY_FOR_APPROVAL"}.items()}}}
    assert action_history.handler({"Records": [record]}, None) == {"written": 1}
    objs = aws.s3.list_objects_v2(Bucket="history", Prefix=f"history/caseId={CID}/")["Contents"]
    body = json.loads(aws.s3.get_object(Bucket="history", Key=objs[0]["Key"])["Body"].read())
    assert body["statusBefore"] == "READY_FOR_APPROVAL" and body["statusAfter"] == "PENDING_APPROVAL"
    assert "taskToken" not in body["newImage"] and "text" not in body["newImage"]["documents"][0]


# --- evaluators ----------------------------------------------------------------------
def test_field_accuracy_evaluator(aws):
    pack = make_case(aws, "PACK-01", with_fields=True)
    aws.s3.put_object(Bucket="docs", Key="sample-packs/PACK-01/pack.json", Body=json.dumps(pack).encode())
    spans = [{"attributes": {"gen_ai.prompt": f"Register case {CID}"}}]
    out = claims_evaluators.handler({"evaluatorName": "ClaimsFieldAccuracy", "evaluationInput": {"sessionSpans": spans}}, None)
    assert out["label"] == "PASS" and out["value"] == 1.0, out
    item = case(aws)
    item["documents"][0]["fields"]["POLICY_NUMBER"]["value"] = "P-MOT-9999"
    aws.ddb.Table("Cases").put_item(Item=claims_common.to_ddb(item))
    out = claims_evaluators.handler({"evaluatorName": "ClaimsFieldAccuracy", "evaluationInput": {"sessionSpans": spans}}, None)
    assert out["label"] == "FAIL" and "POLICY_NUMBER" in out["explanation"]


def test_no_agent_approval_evaluator(aws):
    make_case(aws, "PACK-01")
    spans = [{"name": "tool", "attributes": {"tool": "routing___requestApproval", "input": CID}}]
    ok = claims_evaluators.handler({"evaluatorName": "ClaimsNoAgentApproval", "evaluationInput": {"sessionSpans": spans}}, None)
    assert ok["label"] == "PASS", ok
    bad_spans = spans + [{"attributes": {"tool": "routing___approveClaim"}}]
    bad = claims_evaluators.handler({"evaluatorName": "ClaimsNoAgentApproval", "evaluationInput": {"sessionSpans": bad_spans}}, None)
    assert bad["label"] == "FAIL"
    assert claims_evaluators.handler({"evaluatorName": "x", "evaluationInput": {"sessionSpans": []}}, None)["errorCode"] == "NO_CASE_ID"


# --- workflow definitions ---------------------------------------------------------------
@pytest.mark.parametrize("path", ["workflow/claims_workflow.asl.json", "facilitator/solutions/workflow/claims_workflow.asl.json"])
def test_asl_is_consistent(path):
    file = KIT / path
    if not file.exists():
        pytest.skip(f"{path} not in this kit")
    asl = json.loads(file.read_text())
    states = asl["States"]
    assert asl["StartAt"] in states
    targets = []
    for st in states.values():
        targets += [st.get("Next"), st.get("Default")]
        targets += [c.get("Next") for c in st.get("Choices", []) + st.get("Catch", [])]
    missing = {t for t in targets if t and t not in states}
    assert not missing, missing
    placeholders = set(re.findall(r"\$\{(\w+)\}", file.read_text()))
    assert placeholders <= {"InvokeSupervisorArn", "HumanTaskArn", "FinalizeCaseArn"}, placeholders
    reachable, todo = set(), [asl["StartAt"]]
    while todo:
        s = todo.pop()
        if s in reachable:
            continue
        reachable.add(s)
        st = states[s]
        todo += [x for x in [st.get("Next"), st.get("Default")] + [c.get("Next") for c in st.get("Choices", []) + st.get("Catch", [])] if x]
    assert reachable == set(states), set(states) - reachable


# --- in-workflow LLM judge evaluation (Approach B) ---------------------------
def test_evaluate_summary_happy_path(aws):
    make_case(aws, "PACK-01", status="SUMMARIZED")
    set_status(aws, "SUMMARIZED", summary={
        "facts": "Vehicle collision on 2026-09-15 with front bumper damage. Repair invoice matches INR 48,600.",
        "evidence": ["claim_form", "repair_invoice", "photo_evidence"],
        "missingInformation": [],
        "risks": [],
        "nextAction": "Refer to approver for authorization within policy limits."
    })
    res = evaluate_summary.evaluate_case_summary(CID)
    assert res["passed"] is True
    assert res["score"] >= 0.80
    assert res["verdict"] == "PASS"
    assert res["summaryDiscrepancy"] is False

    c = case(aws)
    assert c["runtimeEvaluation"]["verdict"] == "PASS"
    assert c["runtimeEvaluation"]["score"] >= 0.80
    assert c["flags"].get("summaryDiscrepancy") is False
    assert any(e["action"] == "evaluate_summary" for e in c["timeline"])


def test_evaluate_summary_catches_out_of_policy_discrepancy(aws):
    make_case(aws, "PACK-25", status="SUMMARIZED")
    set_status(aws, "SUMMARIZED",
               flags={"outOfPolicy": True, "outOfPolicyReason": ["Incident date outside policy period"]},
               summary={
                   "facts": "Clean claim with bumper damage.",
                   "evidence": ["claim_form"],
                   "missingInformation": [],
                   "risks": [],
                   "nextAction": "Approve payout of INR 35,000 to claimant."
               })
    res = evaluate_summary.evaluate_case_summary(CID)
    assert res["passed"] is False
    assert res["score"] < 0.80
    assert res["verdict"] == "FAIL"
    assert res["summaryDiscrepancy"] is True

    c = case(aws)
    assert c["runtimeEvaluation"]["verdict"] == "FAIL"
    assert c["flags"]["summaryDiscrepancy"] is True
    assert c["flags"]["requiresSeniorAdjuster"] is True


def test_invoke_supervisor_routes_evaluate_summary_stage(aws):
    make_case(aws, "PACK-01", status="SUMMARIZED")
    set_status(aws, "SUMMARIZED", summary={
        "facts": "Hospitalization on 2026-09-12 for appendectomy.",
        "evidence": ["claim_form", "hospital_invoice"],
        "missingInformation": [],
        "risks": [],
        "nextAction": "Refer to approver for health reimbursement."
    })
    out = invoke_supervisor.handler({"caseId": CID, "stage": "evaluate_summary"}, None)
    assert out["passed"] is True
    assert out["score"] >= 0.80
    assert out["summaryDiscrepancy"] is False

