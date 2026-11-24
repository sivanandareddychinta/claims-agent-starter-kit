"""Starter stack for the Claims & Case Management Agent lab.

Deploys everything except the agents themselves (learners build those with the
AgentCore CLI): data stores, document and history buckets, the tool Lambdas the
agents call through AgentCore Gateway, the Step Functions workflow, the portal
(S3 + CloudFront + Cognito) and its HTTP API.
"""
from __future__ import annotations

from pathlib import Path

from aws_cdk import (
    CfnOutput, Duration, RemovalPolicy, Stack,
    aws_apigatewayv2 as apigw,
    aws_apigatewayv2_authorizers as authorizers,
    aws_apigatewayv2_integrations as integrations,
    aws_cloudfront as cloudfront,
    aws_cloudfront_origins as origins,
    aws_cognito as cognito,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_lambda as lambda_,
    aws_lambda_event_sources as event_sources,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_scheduler as scheduler,
    aws_ssm as ssm,
    aws_stepfunctions as sfn,
)
from constructs import Construct

KIT_ROOT = Path(__file__).resolve().parents[2]


class ClaimsLabStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, lab_id: str, solutions: bool,
                 sender_email: str, notify_override_email: str, sla_minutes_override: str,
                 classifier_model_id: str, cognito_domain_prefix: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        solutions_root = KIT_ROOT / "facilitator" / "solutions"
        use_solutions = solutions and solutions_root.exists()
        lambdas_dir = (solutions_root if use_solutions else KIT_ROOT) / "lambdas"
        asl_file = (solutions_root if use_solutions else KIT_ROOT) / "workflow" / "claims_workflow.asl.json"
        param_prefix = f"/claims-lab/{lab_id}"
        schedule_group_name = f"claims-lab-{lab_id}-sla"

        # ------------------------------------------------------------------
        # Data stores
        # ------------------------------------------------------------------
        cases = dynamodb.Table(
            self, "Cases",
            partition_key=dynamodb.Attribute(name="caseId", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            stream=dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
            removal_policy=RemovalPolicy.DESTROY,
        )
        for name, pk, sk in (("status-index", "status", "createdAt"),
                             ("assignee-index", "assignee", "slaDueAt"),
                             ("policy-index", "policyNumber", "incidentDate")):
            cases.add_global_secondary_index(
                index_name=name,
                partition_key=dynamodb.Attribute(name=pk, type=dynamodb.AttributeType.STRING),
                sort_key=dynamodb.Attribute(name=sk, type=dynamodb.AttributeType.STRING),
            )
        policies = dynamodb.Table(
            self, "Policies",
            partition_key=dynamodb.Attribute(name="policyNumber", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST, removal_policy=RemovalPolicy.DESTROY,
        )
        adjusters = dynamodb.Table(
            self, "Adjusters",
            partition_key=dynamodb.Attribute(name="adjusterId", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST, removal_policy=RemovalPolicy.DESTROY,
        )

        # ------------------------------------------------------------------
        # Portal hosting (S3 + CloudFront) — created first so its URL can be
        # used for CORS, Cognito callbacks and links in emails.
        # ------------------------------------------------------------------
        portal_bucket = s3.Bucket(
            self, "PortalBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY, auto_delete_objects=True,
        )
        distribution = cloudfront.Distribution(
            self, "PortalDistribution",
            default_root_object="index.html",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(portal_bucket),
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
            ),
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
            comment=f"Claims lab portal ({lab_id})",
        )
        portal_url = f"https://{distribution.distribution_domain_name}"
        local_url = "http://localhost:5173"

        docs_bucket = s3.Bucket(
            self, "DocsBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            removal_policy=RemovalPolicy.DESTROY, auto_delete_objects=True,
            cors=[s3.CorsRule(allowed_methods=[s3.HttpMethods.PUT, s3.HttpMethods.GET],
                              allowed_origins=[portal_url, local_url], allowed_headers=["*"], max_age=3000)],
            lifecycle_rules=[s3.LifecycleRule(
                id="tier-case-files", prefix="cases/",
                transitions=[s3.Transition(storage_class=s3.StorageClass.INTELLIGENT_TIERING,
                                           transition_after=Duration.days(30))])],
        )
        history_bucket = s3.Bucket(
            self, "ActionHistoryBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True,
            encryption=s3.BucketEncryption.S3_MANAGED, versioned=True,
            object_lock_enabled=True,
            # Governance mode with 1-day retention keeps the lab account cleanable.
            # Production would use compliance mode and the regulator's retention period.
            object_lock_default_retention=s3.ObjectLockRetention.governance(Duration.days(1)),
            removal_policy=RemovalPolicy.RETAIN,
        )

        # ------------------------------------------------------------------
        # Cognito (portal sign-in)
        # ------------------------------------------------------------------
        user_pool = cognito.UserPool(
            self, "UserPool",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(username=True, email=True),
            password_policy=cognito.PasswordPolicy(min_length=10, require_symbols=False),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.DESTROY,
        )
        for group, desc in (("adjusters", "Work cases"), ("approvers", "Approve, reject or request more information"),
                            ("supervisors", "See everything; receive SLA escalations")):
            cognito.CfnUserPoolGroup(self, f"Group{group.capitalize()}", user_pool_id=user_pool.user_pool_id,
                                     group_name=group, description=desc)
        domain = user_pool.add_domain("Domain", cognito_domain=cognito.CognitoDomainOptions(
            domain_prefix=cognito_domain_prefix or f"claims-lab-{lab_id}-{self.account}"))
        app_client = user_pool.add_client(
            "PortalClient",
            generate_secret=False,
            auth_flows=cognito.AuthFlow(user_srp=True),
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
                callback_urls=[f"{portal_url}/", f"{local_url}/"],
                logout_urls=[f"{portal_url}/", f"{local_url}/"],
            ),
            access_token_validity=Duration.hours(1), id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(1),
            prevent_user_existence_errors=True,
        )

        # ------------------------------------------------------------------
        # Lambda functions
        # ------------------------------------------------------------------
        code = lambda_.Code.from_asset(str(lambdas_dir), exclude=["**/__pycache__", "**/*.pyc"])
        common_env = {
            "CASES_TABLE": cases.table_name,
            "POLICIES_TABLE": policies.table_name,
            "ADJUSTERS_TABLE": adjusters.table_name,
            "DOCS_BUCKET": docs_bucket.bucket_name,
            "PORTAL_URL": portal_url,
            "SENDER_EMAIL": sender_email,
            "NOTIFY_OVERRIDE_EMAIL": notify_override_email,
        }

        def fn(name: str, handler: str, timeout: int = 30, memory: int = 256, env: dict | None = None) -> lambda_.Function:
            log_group = logs.LogGroup(self, f"{name}Logs", retention=logs.RetentionDays.ONE_WEEK,
                                      removal_policy=RemovalPolicy.DESTROY)
            return lambda_.Function(
                self, name, runtime=lambda_.Runtime.PYTHON_3_13, architecture=lambda_.Architecture.ARM_64,
                handler=handler, code=code, timeout=Duration.seconds(timeout), memory_size=memory,
                environment={**common_env, **(env or {})}, log_group=log_group,
                description=f"Claims lab ({lab_id}): {name}",
            )

        sla_fn = fn("SlaEscalation", "workflow.sla_escalation.handler")
        scheduler_role = iam.Role(self, "SchedulerRole", assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"))
        sla_fn.grant_invoke(scheduler_role)
        scheduler.CfnScheduleGroup(self, "SlaScheduleGroup", name=schedule_group_name)

        intake_tools = fn("IntakeTools", "tools.intake_tools.handler")
        document_tools = fn("DocumentTools", "tools.document_tools.handler", timeout=60,
                            env={"CLASSIFIER_MODEL_ID": classifier_model_id})
        case_tools = fn("CaseTools", "tools.case_tools.handler")
        routing_tools = fn("RoutingTools", "tools.routing_tools.handler")
        comms_tools = fn("CommsTools", "tools.comms_tools.handler", env={
            "SCHEDULE_GROUP": schedule_group_name, "SLA_TARGET_ARN": sla_fn.function_arn,
            "SCHEDULER_ROLE_ARN": scheduler_role.role_arn, "SLA_MINUTES_OVERRIDE": sla_minutes_override})

        invoke_supervisor = fn("InvokeSupervisor", "workflow.invoke_supervisor.handler", timeout=900, env={
            "RUNTIME_ARN_PARAM": f"{param_prefix}/agent/runtime-arn",
            "QUALIFIER_PARAM": f"{param_prefix}/agent/qualifier"})
        human_task = fn("HumanTask", "workflow.human_task.handler")
        finalize_case = fn("FinalizeCase", "workflow.finalize_case.handler")
        portal_api = fn("PortalApi", "api.portal_api.handler")
        history_fn = fn("ActionHistory", "history.action_history.handler",
                        env={"HISTORY_BUCKET": history_bucket.bucket_name})
        evaluator_fn = fn("ClaimsEvaluators", "evaluation.claims_evaluators.handler", timeout=120)

        # Least-privilege grants, one role per function
        cases.grant_read_write_data(intake_tools); policies.grant_read_data(intake_tools)
        cases.grant_read_write_data(document_tools); policies.grant_read_data(document_tools)
        docs_bucket.grant_read(document_tools, "cases/*")
        document_tools.add_to_role_policy(iam.PolicyStatement(
            actions=["textract:DetectDocumentText", "textract:AnalyzeDocument"], resources=["*"]))
        document_tools.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=["arn:aws:bedrock:*::foundation-model/*",
                       f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*"]))
        cases.grant_read_write_data(case_tools)
        cases.grant_read_write_data(routing_tools); adjusters.grant_read_write_data(routing_tools)
        cases.grant_read_write_data(comms_tools); adjusters.grant_read_data(comms_tools)
        comms_tools.add_to_role_policy(iam.PolicyStatement(
            actions=["scheduler:CreateSchedule"],
            resources=[f"arn:aws:scheduler:{self.region}:{self.account}:schedule/{schedule_group_name}/*"]))
        scheduler_role.grant_pass_role(comms_tools)
        ses_send = iam.PolicyStatement(actions=["ses:SendEmail"],
                                       resources=[f"arn:aws:ses:{self.region}:{self.account}:identity/*"])
        for f in (comms_tools, human_task, sla_fn):
            f.add_to_role_policy(ses_send)

        cases.grant_read_write_data(invoke_supervisor)
        invoke_supervisor.add_to_role_policy(iam.PolicyStatement(
            actions=["ssm:GetParameter"],
            resources=[f"arn:aws:ssm:{self.region}:{self.account}:parameter{param_prefix}/agent/*"]))
        invoke_supervisor.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock-agentcore:InvokeAgentRuntime"],
            resources=[f"arn:aws:bedrock-agentcore:{self.region}:{self.account}:runtime/*"]))
        invoke_supervisor.add_to_role_policy(iam.PolicyStatement(
            actions=["bedrock:InvokeModel"],
            resources=["arn:aws:bedrock:*::foundation-model/*",
                       f"arn:aws:bedrock:{self.region}:{self.account}:inference-profile/*"]))
        cases.grant_read_write_data(human_task); adjusters.grant_read_data(human_task)
        cases.grant_read_write_data(finalize_case); adjusters.grant_read_write_data(finalize_case)
        cases.grant_read_write_data(sla_fn); adjusters.grant_read_data(sla_fn)
        cases.grant_read_write_data(portal_api); adjusters.grant_read_data(portal_api)
        docs_bucket.grant_read_write(portal_api, "cases/*")
        cases.grant_read_data(evaluator_fn); docs_bucket.grant_read(evaluator_fn, "sample-packs/*")

        history_bucket.grant_put(history_fn)
        history_fn.add_event_source(event_sources.DynamoEventSource(
            cases, starting_position=lambda_.StartingPosition.TRIM_HORIZON, batch_size=25,
            retry_attempts=3, bisect_batch_on_error=True))

        # Agent runtime pointer, filled in after `agentcore deploy` (scripts/set_agent_runtime.py)
        ssm.StringParameter(self, "RuntimeArnParam", parameter_name=f"{param_prefix}/agent/runtime-arn",
                            string_value="not-set", description="AgentCore Runtime ARN of the supervisor")
        ssm.StringParameter(self, "QualifierParam", parameter_name=f"{param_prefix}/agent/qualifier",
                            string_value="DEFAULT", description="Runtime endpoint the workflow calls (DEFAULT, prod, canary)")

        # ------------------------------------------------------------------
        # Step Functions workflow
        # ------------------------------------------------------------------
        sm_logs = logs.LogGroup(self, "WorkflowLogs", retention=logs.RetentionDays.ONE_WEEK,
                                removal_policy=RemovalPolicy.DESTROY)
        state_machine = sfn.StateMachine(
            self, "CaseWorkflow",
            definition_body=sfn.DefinitionBody.from_file(str(asl_file)),
            definition_substitutions={
                "InvokeSupervisorArn": invoke_supervisor.function_arn,
                "HumanTaskArn": human_task.function_arn,
                "FinalizeCaseArn": finalize_case.function_arn,
            },
            state_machine_type=sfn.StateMachineType.STANDARD,
            logs=sfn.LogOptions(destination=sm_logs, level=sfn.LogLevel.ERROR),
        )
        for f in (invoke_supervisor, human_task, finalize_case):
            f.grant_invoke(state_machine)
        portal_api.add_environment("STATE_MACHINE_ARN", state_machine.state_machine_arn)
        state_machine.grant_start_execution(portal_api)
        state_machine.grant_task_response(portal_api)

        # ------------------------------------------------------------------
        # HTTP API (Cognito JWT on every route)
        # ------------------------------------------------------------------
        api = apigw.HttpApi(
            self, "PortalHttpApi",
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=[portal_url, local_url],
                allow_methods=[apigw.CorsHttpMethod.GET, apigw.CorsHttpMethod.POST, apigw.CorsHttpMethod.OPTIONS],
                allow_headers=["authorization", "content-type"], max_age=Duration.hours(1)),
            default_authorizer=authorizers.HttpJwtAuthorizer(
                "CognitoAuthorizer",
                jwt_issuer=f"https://cognito-idp.{self.region}.amazonaws.com/{user_pool.user_pool_id}",
                jwt_audience=[app_client.user_pool_client_id]),
        )
        api_integration = integrations.HttpLambdaIntegration("PortalApiIntegration", portal_api)
        for method, path in (("GET", "/me"), ("GET", "/claims"), ("POST", "/claims"), ("GET", "/claims/{caseId}"),
                             ("POST", "/claims/{caseId}/submit"), ("POST", "/claims/{caseId}/documents"),
                             ("POST", "/claims/{caseId}/documents/complete"), ("POST", "/claims/{caseId}/decision")):
            api.add_routes(path=path, methods=[apigw.HttpMethod(method)], integration=api_integration)
        stage = api.default_stage.node.default_child
        stage.add_property_override("DefaultRouteSettings", {"ThrottlingBurstLimit": 40, "ThrottlingRateLimit": 20})

        # ------------------------------------------------------------------
        # Portal files + runtime config
        # ------------------------------------------------------------------
        cognito_domain_url = f"https://{domain.domain_name}.auth.{self.region}.amazoncognito.com"
        s3deploy.BucketDeployment(
            self, "PortalFiles",
            destination_bucket=portal_bucket, distribution=distribution, distribution_paths=["/*"],
            sources=[
                s3deploy.Source.asset(str(KIT_ROOT / "portal")),
                s3deploy.Source.json_data("config.json", {
                    "region": self.region, "apiUrl": api.api_endpoint, "userPoolId": user_pool.user_pool_id,
                    "clientId": app_client.user_pool_client_id, "cognitoDomain": cognito_domain_url,
                    "labId": lab_id}),
            ],
        )

        # ------------------------------------------------------------------
        # Gateway access: the AgentCore Gateway role needs to invoke the tools.
        # scripts/register_gateway_targets.py attaches this policy to that role.
        # ------------------------------------------------------------------
        tool_fns = {"intake": intake_tools, "document": document_tools, "case": case_tools,
                    "routing": routing_tools, "comms": comms_tools}
        gateway_policy = iam.ManagedPolicy(
            self, "GatewayInvokeToolsPolicy",
            description="Lets the claims lab AgentCore Gateway invoke the tool Lambdas",
            statements=[iam.PolicyStatement(actions=["lambda:InvokeFunction"],
                                            resources=[f.function_arn for f in tool_fns.values()])],
        )
        evaluator_fn.add_permission("AgentCoreEvaluations",
                                    principal=iam.ServicePrincipal("bedrock-agentcore.amazonaws.com"),
                                    source_account=self.account)

        # ------------------------------------------------------------------
        # Outputs (read by the scripts in /scripts)
        # ------------------------------------------------------------------
        outputs = {
            "PortalUrl": portal_url, "ApiUrl": api.api_endpoint, "UserPoolId": user_pool.user_pool_id,
            "UserPoolClientId": app_client.user_pool_client_id, "CognitoDomain": cognito_domain_url,
            "CasesTable": cases.table_name, "PoliciesTable": policies.table_name,
            "AdjustersTable": adjusters.table_name, "DocumentsBucket": docs_bucket.bucket_name,
            "HistoryBucket": history_bucket.bucket_name, "StateMachineArn": state_machine.state_machine_arn,
            "GatewayInvokePolicyArn": gateway_policy.managed_policy_arn,
            "EvaluatorFunctionArn": evaluator_fn.function_arn, "ParamPrefix": param_prefix,
            "KitVariant": "solutions" if use_solutions else "learner",
        }
        for key, f in tool_fns.items():
            outputs[f"{key.capitalize()}ToolsArn"] = f.function_arn
        for key, value in outputs.items():
            CfnOutput(self, key, value=value)
