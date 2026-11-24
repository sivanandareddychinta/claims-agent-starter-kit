#!/usr/bin/env python3
"""CDK entry point for the Claims & Case Management Agent starter kit.

    cdk deploy -c labId=alice -c senderEmail=lab@example.com -c notifyOverrideEmail=me@example.com

See README.md for every context value.
"""
import os
import re

import aws_cdk as cdk

from starter_kit.stack import ClaimsLabStack

app = cdk.App()
ctx = app.node.try_get_context

lab_id = str(ctx("labId") or "lab").lower()
if not re.fullmatch(r"[a-z0-9-]{1,20}", lab_id):
    raise ValueError("labId must be 1-20 characters: lowercase letters, digits, hyphens")

ClaimsLabStack(
    app, f"ClaimsLab-{lab_id}",
    lab_id=lab_id,
    solutions=str(ctx("solutions") or "false").lower() == "true",
    sender_email=str(ctx("senderEmail") or ""),
    notify_override_email=str(ctx("notifyOverrideEmail") or ""),
    sla_minutes_override=str(ctx("slaMinutesOverride") or "0"),
    classifier_model_id=str(ctx("classifierModelId") or "us.amazon.nova-lite-v1:0"),
    cognito_domain_prefix=str(ctx("cognitoDomainPrefix") or ""),
    env=cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
                        region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1")),
    description="Claims & Case Management Agent lab - starter infrastructure",
)
cdk.Tags.of(app).add("project", "claims-agent-lab")
cdk.Tags.of(app).add("lab-id", lab_id)
app.synth()
