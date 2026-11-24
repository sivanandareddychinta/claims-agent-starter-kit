"""Test fixtures: moto-backed DynamoDB/S3/SSM/SES/Scheduler plus small fakes for
Textract, Bedrock and AgentCore (not emulated by moto).

    pip install -r tests/requirements.txt
    pytest -q
"""
from __future__ import annotations

import io
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

KIT = Path(__file__).resolve().parents[1]
# Facilitators can test the reference answers: KIT_LAMBDAS=facilitator/solutions/lambdas pytest
sys.path.insert(0, str(KIT / os.environ.get("KIT_LAMBDAS", "lambdas")))

os.environ.update({
    "AWS_DEFAULT_REGION": "us-east-1", "AWS_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
    "CASES_TABLE": "Cases", "POLICIES_TABLE": "Policies", "ADJUSTERS_TABLE": "Adjusters",
    "DOCS_BUCKET": "docs", "HISTORY_BUCKET": "history", "PORTAL_URL": "https://portal.example",
    "SENDER_EMAIL": "lab@example.com", "NOTIFY_OVERRIDE_EMAIL": "",
    "SCHEDULE_GROUP": "claims-lab-test-sla", "SLA_TARGET_ARN": "arn:aws:lambda:us-east-1:123456789012:function:sla",
    "SCHEDULER_ROLE_ARN": "arn:aws:iam::123456789012:role/scheduler", "SLA_MINUTES_OVERRIDE": "10",
    "STATE_MACHINE_ARN": "arn:aws:states:us-east-1:123456789012:stateMachine:wf",
})

import boto3  # noqa: E402
from moto import mock_aws  # noqa: E402

from shared import claims_common  # noqa: E402


def _table(ddb, name, key, gsis=()):
    attrs = {key}
    for _, pk, sk in gsis:
        attrs |= {pk, sk}
    kwargs = dict(TableName=name, KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                  AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in sorted(attrs)],
                  BillingMode="PAY_PER_REQUEST")
    if gsis:
        kwargs["GlobalSecondaryIndexes"] = [{
            "IndexName": n, "KeySchema": [{"AttributeName": pk, "KeyType": "HASH"}, {"AttributeName": sk, "KeyType": "RANGE"}],
            "Projection": {"ProjectionType": "ALL"}} for n, pk, sk in gsis]
    ddb.create_table(**kwargs)


@pytest.fixture()
def aws():
    with mock_aws():
        claims_common.reset_clients()
        ddb = boto3.client("dynamodb")
        _table(ddb, "Cases", "caseId", [("status-index", "status", "createdAt"),
                                        ("assignee-index", "assignee", "slaDueAt"),
                                        ("policy-index", "policyNumber", "incidentDate")])
        _table(ddb, "Policies", "policyNumber")
        _table(ddb, "Adjusters", "adjusterId")
        res = boto3.resource("dynamodb")
        for name, file_name in (("Policies", "policies.json"), ("Adjusters", "adjusters.json")):
            for row in json.loads((KIT / "data" / file_name).read_text()):
                res.Table(name).put_item(Item=claims_common.to_ddb(row))
        s3 = boto3.client("s3")
        s3.create_bucket(Bucket="docs")
        s3.create_bucket(Bucket="history", ObjectLockEnabledForBucket=True)
        boto3.client("ses").verify_email_identity(EmailAddress="lab@example.com")
        boto3.client("scheduler").create_schedule_group(Name="claims-lab-test-sla")
        yield SimpleNamespace(ddb=res, s3=s3)
        claims_common.reset_clients()


def make_case(aws, pack_id: str, case_id: str = "CLM-20261016-AAAA", status: str = "SUBMITTED", with_fields=False):
    """Insert a case built from a sample pack (documents uploaded to moto S3)."""
    pack = json.loads((KIT / "data" / "claim_packs" / pack_id / "pack.json").read_text())
    docs = []
    for i, d in enumerate(pack["documents"], start=1):
        key = f"cases/{case_id}/DOC-{i:08d}-{d['file']}"
        aws.s3.put_object(Bucket="docs", Key=key, Body=(KIT / "data" / "claim_packs" / pack_id / d["file"]).read_bytes())
        doc = {"docId": f"DOC-{i:08d}", "fileName": d["file"], "s3Key": key}
        if with_fields:
            doc.update({"text": "x", "textConfidence": 99.0, "classification": {"docType": d["docType"], "confidence": 0.95},
                        "fields": {k: {"value": v, "confidence": 98.0} for k, v in d["fields"].items()}})
        docs.append(doc)
    c = pack["claim"]
    item = {"caseId": case_id, "status": status, "createdAt": "2026-10-16T10:00:00Z", "packId": pack_id,
            "policyNumber": c["policyNumber"], "claimType": c["claimType"], "claimantName": c["claimantName"],
            "claimantEmail": c["claimantEmail"], "incidentDate": c["incidentDate"], "claimedAmount": c["claimedAmount"],
            "description": c["description"], "documents": docs, "flags": {}}
    aws.ddb.Table("Cases").put_item(Item=claims_common.to_ddb(item))
    return pack


# ---------------------------------------------------------------------------
# Fakes for services moto does not emulate
# ---------------------------------------------------------------------------
class FakeTextract:
    """Returns the labelled values of the pack document as Textract answers."""

    def __init__(self, labels_by_key: dict, confidence: float = 97.0, text_by_key: dict | None = None):
        self.labels, self.confidence, self.text = labels_by_key, confidence, text_by_key or {}
        self.calls = []

    def detect_document_text(self, Document):
        key = Document["S3Object"]["Name"]
        self.calls.append(("detect", key))
        lines = self.text.get(key, "Sample document text\nLine two").split("\n")
        return {"Blocks": [{"BlockType": "LINE", "Text": t, "Confidence": self.confidence} for t in lines]}

    def analyze_document(self, Document, FeatureTypes, QueriesConfig):
        key = Document["S3Object"]["Name"]
        self.calls.append(("analyze", key))
        blocks, n = [], 0
        for q in QueriesConfig["Queries"]:
            n += 1
            answer = self.labels.get(key, {}).get(q["Alias"])
            qb = {"Id": f"q{n}", "BlockType": "QUERY", "Query": {"Text": q["Text"], "Alias": q["Alias"]}}
            if answer is not None:
                qb["Relationships"] = [{"Type": "ANSWER", "Ids": [f"a{n}"]}]
                blocks.append({"Id": f"a{n}", "BlockType": "QUERY_RESULT", "Text": answer, "Confidence": self.confidence})
            blocks.append(qb)
        return {"Blocks": blocks}


class FakeBedrock:
    def __init__(self, doc_type_by_marker: dict):
        self.map = doc_type_by_marker

    def converse(self, modelId, messages, inferenceConfig):
        text = messages[0]["content"][0]["text"]
        doc_type = next((t for marker, t in self.map.items() if marker in text), "other")
        return {"output": {"message": {"content": [{"text": json.dumps({"docType": doc_type, "confidence": 0.93, "reason": "test"})}]}}}


class FakeRuntime:
    """Fake bedrock-agentcore client; `on_invoke` lets a test mutate the case like an agent would."""

    def __init__(self, on_invoke=None, reply="done"):
        self.on_invoke, self.reply, self.calls = on_invoke, reply, []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        if self.on_invoke:
            self.on_invoke(kwargs)
        return {"contentType": "application/json", "response": io.BytesIO(json.dumps({"result": self.reply}).encode())}


class FakeStepFunctions:
    def __init__(self):
        self.calls = []

    def send_task_success(self, **kwargs):
        self.calls.append(("success", kwargs))
        return {}

    def start_execution(self, **kwargs):
        self.calls.append(("start", kwargs))
        return {"executionArn": f"arn:aws:states:us-east-1:123456789012:execution:wf:{kwargs['name']}"}


def patch_clients(monkeypatch, module, **fakes):
    real = claims_common.client

    def factory(name):
        return fakes[name] if name in fakes else real(name)

    monkeypatch.setattr(module, "client", factory)


class Ctx:
    def __init__(self, target: str, tool: str):
        self.client_context = SimpleNamespace(custom={"bedrockAgentCoreToolName": f"{target}___{tool}"})
