"""Pulumi program for the shared AgentCore Gateway infrastructure."""

from __future__ import annotations

from pathlib import Path

import pulumi
import pulumi_aws as aws
from mcp_targets import (
    RAPIDAPI_UPSTREAM,
    SECRET_CONFIG_NAMES,
    api_key_credential_provider_configuration,
    credential_provider_config_names,
    mcp_server_target_configuration,
    rapidapi_proxy_request_parameters,
    resolve_key_config_name,
    selected_rapidapi_specs,
    selected_remote_mcp_specs,
)

config = pulumi.Config()
region = aws.get_region().region
gateway_name = config.get("gateway_name") or "benchma-open-meteo"
gateway_role_name = config.get("gateway_role_name") or "AgentCoreGatewayExecutionRole"
lambda_name = config.get("lambda_function_name") or "benchma-open-meteo-tools"
lambda_role_name = config.get("lambda_role_name") or "benchma-open-meteo-lambda-role"
target_name = config.get("target_name") or "open-meteo-tools"
root = Path(__file__).resolve().parents[2]
enable_travel_rag = config.get_bool("enable_travel_rag") or False
# Optional: AgentCore Runtime MCP endpoint for opensource/mcp-tools (web search + OSM).
mcp_tools_endpoint = config.get("mcp_tools_endpoint")
mcp_tools_stack_name = config.get("mcp_tools_stack")
if mcp_tools_stack_name and not mcp_tools_endpoint:
    mcp_tools_endpoint = pulumi.StackReference(mcp_tools_stack_name).require_output("mcp_endpoint")

# Presence of secret config keys (not values) drives which optional targets exist.
_present_secret_keys = {name for name in SECRET_CONFIG_NAMES if config.get_secret(name) is not None}


def import_options(key: str, **kwargs) -> pulumi.ResourceOptions:
    return pulumi.ResourceOptions(import_=config.get(key), **kwargs)


gateway_assume_role = aws.iam.get_policy_document(
    statements=[
        {
            "effect": "Allow",
            "actions": ["sts:AssumeRole"],
            "principals": [
                {
                    "type": "Service",
                    "identifiers": ["bedrock-agentcore.amazonaws.com"],
                }
            ],
        }
    ]
)

gateway_role = aws.iam.Role(
    "gateway-role",
    name=gateway_role_name,
    description="Execution role for AgentCore Gateway",
    assume_role_policy=gateway_assume_role.json,
    opts=import_options("import_gateway_role_id"),
)

gateway_policy = aws.iam.RolePolicy(
    "gateway-policy",
    name="BedrockAgentCoreGatewayAccess",
    role=gateway_role.id,
    policy=pulumi.Output.json_dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "bedrock-agentcore:*",
                        "lambda:InvokeFunction",
                    ],
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": ["secretsmanager:GetSecretValue"],
                    "Resource": "*",
                },
            ],
        }
    ),
    opts=import_options("import_gateway_policy_id"),
)

lambda_assume_role = aws.iam.get_policy_document(
    statements=[
        {
            "effect": "Allow",
            "actions": ["sts:AssumeRole"],
            "principals": [{"type": "Service", "identifiers": ["lambda.amazonaws.com"]}],
        }
    ]
)

lambda_role = aws.iam.Role(
    "lambda-role",
    name=lambda_role_name,
    description="Execution role for Benchma Open-Meteo gateway Lambda",
    assume_role_policy=lambda_assume_role.json,
    opts=import_options("import_lambda_role_id"),
)

aws.iam.RolePolicyAttachment(
    "lambda-basic-execution",
    role=lambda_role.name,
    policy_arn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    opts=import_options("import_lambda_policy_attachment_id"),
)

lambda_import_id = config.get("import_lambda_id")
weather_lambda = aws.lambda_.Function(
    "open-meteo-tools",
    name=lambda_name,
    description="Open-Meteo proxy tools for Benchma AgentCore Gateway",
    role=lambda_role.arn,
    handler="lambda_function.lambda_handler",
    runtime=aws.lambda_.Runtime.PYTHON3D13,
    timeout=30,
    code=pulumi.AssetArchive(
        {"lambda_function.py": pulumi.FileAsset(str(root / "gateway" / "open_meteo_lambda.py"))}
    ),
    opts=pulumi.ResourceOptions(
        import_=lambda_import_id,
        depends_on=[lambda_role],
        # Adopt the existing function first; code updates begin after import config is removed.
        ignore_changes=["code"] if lambda_import_id else None,
    ),
)

gateway = aws.bedrock.AgentcoreGateway(
    "open-meteo-gateway",
    name=gateway_name,
    description=(
        "Benchma AgentCore Gateway: Open-Meteo Lambda plus optional MCP-server "
        "targets (mcp-tools, RapidAPI, Geoapify)"
    ),
    role_arn=gateway_role.arn,
    protocol_type="MCP",
    authorizer_type="AWS_IAM",
    region=region,
    opts=import_options("import_gateway_id", depends_on=[gateway_policy]),
)


def prop(
    name: str,
    type_: str,
    *,
    required: bool = False,
    description: str | None = None,
):
    value = {"name": name, "type": type_}
    if required:
        value["required"] = True
    if description:
        value["description"] = description
    return value


tool_schema = {
    "inline_payloads": [
        {
            "name": "getForecast",
            "description": (
                "Get current weather or forecast from Open-Meteo for latitude/longitude."
            ),
            "input_schema": {
                "type": "object",
                "properties": [
                    prop("current_weather", "boolean"),
                    prop("longitude", "number", required=True),
                    prop("forecast_days", "integer"),
                    prop("timezone", "string"),
                    prop("latitude", "number", required=True),
                    prop("daily", "string"),
                    prop("hourly", "string"),
                ],
            },
        },
        {
            "name": "searchLocation",
            "description": (
                "Resolve a city or place name to coordinates using Open-Meteo geocoding."
            ),
            "input_schema": {
                "type": "object",
                "properties": [
                    prop("name", "string", required=True),
                    prop("count", "integer"),
                    prop("language", "string"),
                ],
            },
        },
    ]
}

target = aws.bedrock.AgentcoreGatewayTarget(
    "open-meteo-target",
    name=target_name,
    gateway_identifier=gateway.gateway_id,
    description="Open-Meteo getForecast and searchLocation tools",
    credential_provider_configuration={"gateway_iam_role": {}},
    target_configuration={
        "mcp": {
            "lambda_": {
                "lambda_arn": weather_lambda.arn,
                "tool_schema": tool_schema,
            }
        }
    },
    region=region,
    opts=import_options(
        "import_gateway_target_id",
        depends_on=[gateway_policy, weather_lambda],
    ),
)

pulumi.export("gateway_id", gateway.gateway_id)
pulumi.export("gateway_arn", gateway.gateway_arn)
pulumi.export("gateway_url", gateway.gateway_url)
pulumi.export("gateway_role_arn", gateway_role.arn)
pulumi.export("lambda_arn", weather_lambda.arn)
pulumi.export("target_id", target.target_id)

# ---------------------------------------------------------------------------
# MCP-server targets (mcp-tools Runtime, RapidAPI, Geoapify)
# ---------------------------------------------------------------------------
credential_providers: dict[str, aws.bedrock.AgentcoreApiKeyCredentialProvider] = {}
for provider_name in credential_provider_config_names(_present_secret_keys):
    credential_providers[provider_name] = aws.bedrock.AgentcoreApiKeyCredentialProvider(
        f"api-key-{provider_name.replace('_', '-')}",
        name=f"benchma-{provider_name.replace('_', '-')}",
        api_key_wo=config.require_secret(provider_name),
        api_key_wo_version=config.get_int(f"{provider_name}_version") or 1,
        region=region,
    )
    pulumi.export(
        f"{provider_name}_credential_provider_arn",
        credential_providers[provider_name].credential_provider_arn,
    )

exported_target_ids: dict[str, pulumi.Output[str]] = {"open-meteo-tools": target.target_id}

# ---------------------------------------------------------------------------
# Optional Wikivoyage RAG target (Bedrock Knowledge Bases + Pinecone + S3)
# ---------------------------------------------------------------------------
if enable_travel_rag:
    account_id = aws.get_caller_identity().account_id
    pinecone_index_host = config.require("pinecone_index_host")
    pinecone_namespace = config.get("pinecone_namespace") or "wikivoyage-pilot-v1"
    source_prefix = f"wikivoyage/{pinecone_namespace}/kb-documents/"
    candidate_namespace = (config.get("travel_rag_candidate_namespace") or "").strip()
    candidate_active = config.get_bool("travel_rag_candidate_active") or False
    if candidate_active and not candidate_namespace:
        raise ValueError(
            "travel_rag_candidate_namespace is required when travel_rag_candidate_active is true"
        )
    candidate_source_prefix = (
        f"wikivoyage/{candidate_namespace}/kb-documents/" if candidate_namespace else None
    )
    evaluation_namespace = (
        config.get("travel_rag_evaluation_namespace") or ""
    ).strip()
    evaluation_1024_namespace = (
        config.get("travel_rag_evaluation_1024_namespace") or ""
    ).strip()
    evaluation_1024_pinecone_index_host = (
        config.get("travel_rag_evaluation_1024_pinecone_index_host") or ""
    ).strip()
    if bool(evaluation_1024_namespace) != bool(evaluation_1024_pinecone_index_host):
        raise ValueError(
            "travel_rag_evaluation_1024_namespace and "
            "travel_rag_evaluation_1024_pinecone_index_host must be configured together"
        )
    evaluation_source_prefix = (
        f"wikivoyage/{evaluation_namespace}/kb-documents/"
        if evaluation_namespace
        else None
    )
    evaluation_1024_source_prefix = (
        f"wikivoyage/{evaluation_1024_namespace}/kb-documents/"
        if evaluation_1024_namespace
        else None
    )
    structured_namespace = (config.get("travel_rag_structured_namespace") or "").strip()
    structured_active = config.get_bool("travel_rag_structured_active") or False
    hybrid_collection_name = (
        config.get("travel_rag_hybrid_collection_name") or ""
    ).strip()
    hybrid_index_name = (
        config.get("travel_rag_hybrid_index_name") or "travel-rag-hybrid-v1"
    ).strip()
    hybrid_embedding_dimensions = (
        config.get_int("travel_rag_hybrid_embedding_dimensions") or 512
    )
    if hybrid_embedding_dimensions not in {256, 512, 1024}:
        raise ValueError(
            "travel_rag_hybrid_embedding_dimensions must be 256, 512, or 1024"
        )
    hybrid_index_ready = config.get_bool("travel_rag_hybrid_index_ready") or False
    hybrid_active = config.get_bool("travel_rag_hybrid_active") or False
    active_candidates = sum((candidate_active, structured_active, hybrid_active))
    if active_candidates > 1:
        raise ValueError(
            "Only one travel RAG candidate can be active at a time"
        )
    if structured_active and not structured_namespace:
        raise ValueError(
            "travel_rag_structured_namespace is required when travel_rag_structured_active is true"
        )
    if hybrid_index_ready and not hybrid_collection_name:
        raise ValueError(
            "travel_rag_hybrid_collection_name is required when the hybrid index is ready"
        )
    if hybrid_active and not hybrid_index_ready:
        raise ValueError(
            "travel_rag_hybrid_index_ready must be true before hybrid activation"
        )
    structured_source_prefix = (
        f"datatourisme/{structured_namespace}/kb-documents/"
        if structured_namespace
        else None
    )
    source_prefixes = [source_prefix]
    if candidate_source_prefix:
        source_prefixes.append(candidate_source_prefix)
    if evaluation_source_prefix:
        source_prefixes.append(evaluation_source_prefix)
    if evaluation_1024_source_prefix and evaluation_1024_source_prefix not in source_prefixes:
        source_prefixes.append(evaluation_1024_source_prefix)
    if structured_source_prefix:
        source_prefixes.append(structured_source_prefix)
    rag_bucket_name = (
        config.get("travel_rag_bucket_name")
        or f"agentic-blueprint-travel-rag-{account_id}-{region}"
    )
    rag_lambda_name = config.get("travel_rag_lambda_name") or "benchma-travel-rag"
    rag_lambda_role_name = (
        config.get("travel_rag_lambda_role_name") or "benchma-travel-rag-lambda-role"
    )
    knowledge_base_name = config.get("travel_rag_knowledge_base_name") or "benchma-travel-rag"
    knowledge_base_role_name = (
        config.get("travel_rag_knowledge_base_role_name") or "benchma-travel-rag-kb-role"
    )
    reranking_model_id = (config.get("travel_rag_reranking_model_id") or "").strip()
    reranking_model_arn = (
        f"arn:aws:bedrock:{region}::foundation-model/{reranking_model_id}"
        if reranking_model_id
        else None
    )
    rerank_candidates = config.get_int("travel_rag_rerank_candidates") or 20
    if reranking_model_id and not 1 <= rerank_candidates <= 100:
        raise ValueError("travel_rag_rerank_candidates must be between 1 and 100")

    rag_bucket = aws.s3.BucketV2(
        "travel-rag-chunks",
        bucket=rag_bucket_name,
        force_destroy=False,
    )
    aws.s3.BucketPublicAccessBlock(
        "travel-rag-public-access-block",
        bucket=rag_bucket.id,
        block_public_acls=True,
        block_public_policy=True,
        ignore_public_acls=True,
        restrict_public_buckets=True,
    )
    aws.s3.BucketServerSideEncryptionConfigurationV2(
        "travel-rag-encryption",
        bucket=rag_bucket.id,
        rules=[
            {
                "apply_server_side_encryption_by_default": {
                    "sse_algorithm": "AES256",
                }
            }
        ],
    )
    aws.s3.BucketVersioningV2(
        "travel-rag-versioning",
        bucket=rag_bucket.id,
        versioning_configuration={"status": "Enabled"},
    )

    pinecone_secret_arn = config.get("pinecone_secret_arn")
    pinecone_secret_dependencies: list[pulumi.Resource] = []
    if not pinecone_secret_arn:
        pinecone_secret = aws.secretsmanager.Secret(
            "travel-rag-pinecone-secret",
            name=config.get("pinecone_secret_name") or "agentic-blueprint/pinecone/travel-rag",
            description="Pinecone API key used only by Bedrock Knowledge Bases",
            recovery_window_in_days=7,
        )
        pinecone_secret_version = aws.secretsmanager.SecretVersion(
            "travel-rag-pinecone-secret-version",
            secret_id=pinecone_secret.id,
            secret_string=pulumi.Output.json_dumps(
                {"apiKey": config.require_secret("pinecone_api_key")}
            ),
        )
        pinecone_secret_arn = pinecone_secret.arn
        pinecone_secret_dependencies.append(pinecone_secret_version)

    knowledge_base_assume_role = aws.iam.get_policy_document(
        statements=[
            {
                "effect": "Allow",
                "actions": ["sts:AssumeRole"],
                "principals": [
                    {
                        "type": "Service",
                        "identifiers": ["bedrock.amazonaws.com"],
                    }
                ],
                "conditions": [
                    {
                        "test": "StringEquals",
                        "variable": "aws:SourceAccount",
                        "values": [account_id],
                    },
                    {
                        "test": "ArnLike",
                        "variable": "aws:SourceArn",
                        "values": [f"arn:aws:bedrock:{region}:{account_id}:knowledge-base/*"],
                    },
                ],
            }
        ]
    )
    knowledge_base_role = aws.iam.Role(
        "travel-rag-knowledge-base-role",
        name=knowledge_base_role_name,
        description="Service role for the Wikivoyage Bedrock Knowledge Base",
        assume_role_policy=knowledge_base_assume_role.json,
    )

    hybrid_encryption_policy = None
    hybrid_network_policy = None
    hybrid_collection = None
    hybrid_data_access_policy = None
    if hybrid_collection_name:
        hybrid_admin_principal_arn = config.require(
            "travel_rag_hybrid_admin_principal_arn"
        )
        hybrid_admin_session_arn = (
            config.get("travel_rag_hybrid_admin_session_arn") or ""
        ).strip()
        hybrid_admin_principals = [
            hybrid_admin_session_arn or hybrid_admin_principal_arn
        ]
        hybrid_encryption_policy = aws.opensearch.ServerlessSecurityPolicy(
            "travel-rag-hybrid-encryption-policy",
            name="travel-rag-hybrid-encryption",
            type="encryption",
            description="Encryption for the isolated travel RAG hybrid collection",
            policy=pulumi.Output.json_dumps(
                {
                    "Rules": [
                        {
                            "Resource": [f"collection/{hybrid_collection_name}"],
                            "ResourceType": "collection",
                        }
                    ],
                    "AWSOwnedKey": True,
                }
            ),
            region=region,
        )
        hybrid_network_policy = aws.opensearch.ServerlessSecurityPolicy(
            "travel-rag-hybrid-network-policy",
            name="travel-rag-hybrid-network",
            type="network",
            description="Signed public API access for Bedrock and index bootstrap",
            policy=pulumi.Output.json_dumps(
                [
                    {
                        "Description": "Travel RAG hybrid collection API access",
                        "Rules": [
                            {
                                "ResourceType": "collection",
                                "Resource": [f"collection/{hybrid_collection_name}"],
                            }
                        ],
                        "AllowFromPublic": True,
                    }
                ]
            ),
            region=region,
        )
        hybrid_collection = aws.opensearch.ServerlessCollection(
            "travel-rag-hybrid-collection",
            name=hybrid_collection_name,
            description="BM25 and dense-vector retrieval candidate for travel RAG",
            type="VECTORSEARCH",
            standby_replicas="DISABLED",
            region=region,
            opts=pulumi.ResourceOptions(
                depends_on=[hybrid_encryption_policy, hybrid_network_policy]
            ),
        )
        hybrid_data_access_policy = aws.opensearch.ServerlessAccessPolicy(
            "travel-rag-hybrid-data-access-policy",
            name="travel-rag-hybrid-access",
            type="data",
            description="Bedrock and bootstrap access to the travel RAG hybrid index",
            policy=pulumi.Output.json_dumps(
                [
                    {
                        "Rules": [
                            {
                                "ResourceType": "index",
                                "Resource": [f"index/{hybrid_collection_name}/*"],
                                "Permission": [
                                    "aoss:CreateIndex",
                                    "aoss:DescribeIndex",
                                    "aoss:UpdateIndex",
                                    "aoss:ReadDocument",
                                    "aoss:WriteDocument",
                                ],
                            },
                            {
                                "ResourceType": "collection",
                                "Resource": [f"collection/{hybrid_collection_name}"],
                                "Permission": [
                                    "aoss:DescribeCollectionItems",
                                    "aoss:CreateCollectionItems",
                                    "aoss:UpdateCollectionItems",
                                ],
                            },
                        ],
                        "Principal": [
                            *hybrid_admin_principals,
                            knowledge_base_role.arn,
                        ],
                    }
                ]
            ),
            region=region,
        )

    knowledge_base_policy_statements = [
        {
            "Sid": "ReadSourceDocuments",
            "Effect": "Allow",
            "Action": ["s3:GetObject"],
            "Resource": rag_bucket.arn.apply(
                lambda arn: [f"{arn}/{prefix}*" for prefix in source_prefixes]
            ),
        },
        {
            "Sid": "ListSourceDocuments",
            "Effect": "Allow",
            "Action": ["s3:ListBucket"],
            "Resource": rag_bucket.arn,
            "Condition": {
                "StringLike": {"s3:prefix": [f"{prefix}*" for prefix in source_prefixes]}
            },
        },
        {
            "Sid": "ReadPineconeCredentials",
            "Effect": "Allow",
            "Action": ["secretsmanager:GetSecretValue"],
            "Resource": pinecone_secret_arn,
        },
        {
            "Sid": "AssociatePineconeCredentials",
            "Effect": "Allow",
            "Action": ["bedrock:AssociateThirdPartyKnowledgeBase"],
            "Resource": "*",
            "Condition": {
                "StringEquals": {
                    "bedrock:ThirdPartyKnowledgeBaseCredentialsSecretArn": pinecone_secret_arn
                }
            },
        },
        {
            "Sid": "CreateEmbeddings",
            "Effect": "Allow",
            "Action": ["bedrock:InvokeModel"],
            "Resource": (
                f"arn:aws:bedrock:{region}::foundation-model/"
                "amazon.titan-embed-text-v2:0"
            ),
        },
    ]
    if hybrid_collection:
        knowledge_base_policy_statements.append(
            {
                "Sid": "AccessHybridOpenSearchCollection",
                "Effect": "Allow",
                "Action": ["aoss:APIAccessAll"],
                "Resource": hybrid_collection.arn,
            }
        )
    knowledge_base_policy = aws.iam.RolePolicy(
        "travel-rag-knowledge-base-policy",
        name="TravelRagKnowledgeBaseAccess",
        role=knowledge_base_role.id,
        policy=pulumi.Output.json_dumps(
            {
                "Version": "2012-10-17",
                "Statement": knowledge_base_policy_statements,
            }
        ),
    )

    knowledge_base = aws.bedrock.AgentKnowledgeBase(
        "travel-rag-knowledge-base",
        name=knowledge_base_name,
        description="Wikivoyage semantic retrieval stored in Pinecone",
        role_arn=knowledge_base_role.arn,
        knowledge_base_configuration={
            "type": "VECTOR",
            "vector_knowledge_base_configuration": {
                "embedding_model_arn": (
                    f"arn:aws:bedrock:{region}::foundation-model/amazon.titan-embed-text-v2:0"
                ),
                "embedding_model_configuration": {
                    "bedrock_embedding_model_configuration": {
                        "dimensions": 512,
                        "embedding_data_type": "FLOAT32",
                    }
                },
            },
        },
        storage_configuration={
            "type": "PINECONE",
            "pinecone_configuration": {
                "connection_string": pinecone_index_host,
                "credentials_secret_arn": pinecone_secret_arn,
                "namespace": pinecone_namespace,
                "field_mapping": {
                    "text_field": "text",
                    "metadata_field": "metadata",
                },
            },
        },
        region=region,
        opts=pulumi.ResourceOptions(
            depends_on=[knowledge_base_policy, *pinecone_secret_dependencies]
        ),
    )

    data_source = aws.bedrock.AgentDataSource(
        "travel-rag-data-source",
        knowledge_base_id=knowledge_base.id,
        name="wikivoyage",
        description="Versioned Wikivoyage text chunks with citation metadata",
        data_deletion_policy="RETAIN",
        data_source_configuration={
            "type": "S3",
            "s3_configuration": {
                "bucket_arn": rag_bucket.arn,
                "bucket_owner_account_id": account_id,
                "inclusion_prefixes": [source_prefix],
            },
        },
        vector_ingestion_configuration={"chunking_configuration": {"chunking_strategy": "NONE"}},
        region=region,
        opts=pulumi.ResourceOptions(depends_on=[knowledge_base]),
    )

    candidate_knowledge_base = None
    candidate_data_source = None
    if candidate_namespace and candidate_source_prefix:
        candidate_knowledge_base = aws.bedrock.AgentKnowledgeBase(
            "travel-rag-candidate-knowledge-base",
            name=f"{knowledge_base_name}-candidate",
            description="Candidate Wikivoyage retrieval corpus stored in Pinecone",
            role_arn=knowledge_base_role.arn,
            knowledge_base_configuration={
                "type": "VECTOR",
                "vector_knowledge_base_configuration": {
                    "embedding_model_arn": (
                        f"arn:aws:bedrock:{region}::foundation-model/amazon.titan-embed-text-v2:0"
                    ),
                    "embedding_model_configuration": {
                        "bedrock_embedding_model_configuration": {
                            "dimensions": 512,
                            "embedding_data_type": "FLOAT32",
                        }
                    },
                },
            },
            storage_configuration={
                "type": "PINECONE",
                "pinecone_configuration": {
                    "connection_string": pinecone_index_host,
                    "credentials_secret_arn": pinecone_secret_arn,
                    "namespace": candidate_namespace,
                    "field_mapping": {
                        "text_field": "text",
                        "metadata_field": "metadata",
                    },
                },
            },
            region=region,
            opts=pulumi.ResourceOptions(
                depends_on=[knowledge_base_policy, *pinecone_secret_dependencies]
            ),
        )
        candidate_data_source = aws.bedrock.AgentDataSource(
            "travel-rag-candidate-data-source",
            knowledge_base_id=candidate_knowledge_base.id,
            name="wikivoyage-candidate",
            description="Candidate listing-aware Wikivoyage chunks with citation metadata",
            data_deletion_policy="RETAIN",
            data_source_configuration={
                "type": "S3",
                "s3_configuration": {
                    "bucket_arn": rag_bucket.arn,
                    "bucket_owner_account_id": account_id,
                    "inclusion_prefixes": [candidate_source_prefix],
                },
            },
            vector_ingestion_configuration={
                "chunking_configuration": {"chunking_strategy": "NONE"}
            },
            region=region,
            opts=pulumi.ResourceOptions(depends_on=[candidate_knowledge_base]),
        )

    evaluation_knowledge_base = None
    evaluation_data_source = None
    if evaluation_namespace and evaluation_source_prefix:
        evaluation_knowledge_base = aws.bedrock.AgentKnowledgeBase(
            "travel-rag-evaluation-knowledge-base",
            name=f"{knowledge_base_name}-evaluation",
            description="Isolated Wikivoyage retrieval evaluation stored in Pinecone",
            role_arn=knowledge_base_role.arn,
            knowledge_base_configuration={
                "type": "VECTOR",
                "vector_knowledge_base_configuration": {
                    "embedding_model_arn": (
                        f"arn:aws:bedrock:{region}::foundation-model/"
                        "amazon.titan-embed-text-v2:0"
                    ),
                    "embedding_model_configuration": {
                        "bedrock_embedding_model_configuration": {
                            "dimensions": 512,
                            "embedding_data_type": "FLOAT32",
                        }
                    },
                },
            },
            storage_configuration={
                "type": "PINECONE",
                "pinecone_configuration": {
                    "connection_string": pinecone_index_host,
                    "credentials_secret_arn": pinecone_secret_arn,
                    "namespace": evaluation_namespace,
                    "field_mapping": {
                        "text_field": "text",
                        "metadata_field": "metadata",
                    },
                },
            },
            region=region,
            opts=pulumi.ResourceOptions(
                depends_on=[knowledge_base_policy, *pinecone_secret_dependencies]
            ),
        )
        evaluation_data_source = aws.bedrock.AgentDataSource(
            "travel-rag-evaluation-data-source",
            knowledge_base_id=evaluation_knowledge_base.id,
            name="wikivoyage-evaluation",
            description="Evaluation-only listing-aware Wikivoyage chunks",
            data_deletion_policy="RETAIN",
            data_source_configuration={
                "type": "S3",
                "s3_configuration": {
                    "bucket_arn": rag_bucket.arn,
                    "bucket_owner_account_id": account_id,
                    "inclusion_prefixes": [evaluation_source_prefix],
                },
            },
            vector_ingestion_configuration={
                "chunking_configuration": {"chunking_strategy": "NONE"}
            },
            region=region,
            opts=pulumi.ResourceOptions(depends_on=[evaluation_knowledge_base]),
        )

    evaluation_1024_knowledge_base = None
    evaluation_1024_data_source = None
    if evaluation_1024_namespace and evaluation_1024_source_prefix:
        evaluation_1024_knowledge_base = aws.bedrock.AgentKnowledgeBase(
            "travel-rag-evaluation-1024-knowledge-base",
            name=f"{knowledge_base_name}-evaluation-1024",
            description="Titan 1024 Wikivoyage evaluation stored in Pinecone",
            role_arn=knowledge_base_role.arn,
            knowledge_base_configuration={
                "type": "VECTOR",
                "vector_knowledge_base_configuration": {
                    "embedding_model_arn": (
                        f"arn:aws:bedrock:{region}::foundation-model/"
                        "amazon.titan-embed-text-v2:0"
                    ),
                    "embedding_model_configuration": {
                        "bedrock_embedding_model_configuration": {
                            "dimensions": 1024,
                            "embedding_data_type": "FLOAT32",
                        }
                    },
                },
            },
            storage_configuration={
                "type": "PINECONE",
                "pinecone_configuration": {
                    "connection_string": evaluation_1024_pinecone_index_host,
                    "credentials_secret_arn": pinecone_secret_arn,
                    "namespace": evaluation_1024_namespace,
                    "field_mapping": {
                        "text_field": "text",
                        "metadata_field": "metadata",
                    },
                },
            },
            region=region,
            opts=pulumi.ResourceOptions(
                depends_on=[knowledge_base_policy, *pinecone_secret_dependencies]
            ),
        )
        evaluation_1024_data_source = aws.bedrock.AgentDataSource(
            "travel-rag-evaluation-1024-data-source",
            knowledge_base_id=evaluation_1024_knowledge_base.id,
            name="wikivoyage-evaluation-1024",
            description="Evaluation-only Wikivoyage chunks embedded by Titan at 1024 dimensions",
            data_deletion_policy="RETAIN",
            data_source_configuration={
                "type": "S3",
                "s3_configuration": {
                    "bucket_arn": rag_bucket.arn,
                    "bucket_owner_account_id": account_id,
                    "inclusion_prefixes": [evaluation_1024_source_prefix],
                },
            },
            vector_ingestion_configuration={
                "chunking_configuration": {"chunking_strategy": "NONE"}
            },
            region=region,
            opts=pulumi.ResourceOptions(depends_on=[evaluation_1024_knowledge_base]),
        )

    structured_knowledge_base = None
    structured_data_source = None
    if structured_namespace and structured_source_prefix:
        structured_knowledge_base = aws.bedrock.AgentKnowledgeBase(
            "travel-rag-structured-knowledge-base",
            name=f"{knowledge_base_name}-structured",
            description="Structured DATAtourisme retrieval experiment stored in Pinecone",
            role_arn=knowledge_base_role.arn,
            knowledge_base_configuration={
                "type": "VECTOR",
                "vector_knowledge_base_configuration": {
                    "embedding_model_arn": (
                        f"arn:aws:bedrock:{region}::foundation-model/amazon.titan-embed-text-v2:0"
                    ),
                    "embedding_model_configuration": {
                        "bedrock_embedding_model_configuration": {
                            "dimensions": 512,
                            "embedding_data_type": "FLOAT32",
                        }
                    },
                },
            },
            storage_configuration={
                "type": "PINECONE",
                "pinecone_configuration": {
                    "connection_string": pinecone_index_host,
                    "credentials_secret_arn": pinecone_secret_arn,
                    "namespace": structured_namespace,
                    "field_mapping": {
                        "text_field": "text",
                        "metadata_field": "metadata",
                    },
                },
            },
            region=region,
            opts=pulumi.ResourceOptions(
                depends_on=[knowledge_base_policy, *pinecone_secret_dependencies]
            ),
        )
        structured_data_source = aws.bedrock.AgentDataSource(
            "travel-rag-structured-data-source",
            knowledge_base_id=structured_knowledge_base.id,
            name="datatourisme-paris",
            description="One structured DATAtourisme Paris POI per embedding document",
            data_deletion_policy="RETAIN",
            data_source_configuration={
                "type": "S3",
                "s3_configuration": {
                    "bucket_arn": rag_bucket.arn,
                    "bucket_owner_account_id": account_id,
                    "inclusion_prefixes": [structured_source_prefix],
                },
            },
            vector_ingestion_configuration={
                "chunking_configuration": {"chunking_strategy": "NONE"}
            },
            region=region,
            opts=pulumi.ResourceOptions(depends_on=[structured_knowledge_base]),
        )

    hybrid_knowledge_base = None
    hybrid_data_source = None
    if (
        hybrid_index_ready
        and hybrid_collection
        and hybrid_data_access_policy
        and evaluation_source_prefix
    ):
        hybrid_knowledge_base = aws.bedrock.AgentKnowledgeBase(
            "travel-rag-hybrid-knowledge-base",
            name=f"{knowledge_base_name}-hybrid-v2",
            description="Evaluation-only Wikivoyage retrieval with BM25 and dense vectors",
            role_arn=knowledge_base_role.arn,
            knowledge_base_configuration={
                "type": "VECTOR",
                "vector_knowledge_base_configuration": {
                    "embedding_model_arn": (
                        f"arn:aws:bedrock:{region}::foundation-model/"
                        "amazon.titan-embed-text-v2:0"
                    ),
                    "embedding_model_configuration": {
                        "bedrock_embedding_model_configuration": {
                            "dimensions": hybrid_embedding_dimensions,
                            "embedding_data_type": "FLOAT32",
                        }
                    },
                },
            },
            storage_configuration={
                "type": "OPENSEARCH_SERVERLESS",
                "opensearch_serverless_configuration": {
                    "collection_arn": hybrid_collection.arn,
                    "vector_index_name": hybrid_index_name,
                    "field_mapping": {
                        "vector_field": "bedrock-knowledge-base-default-vector",
                        "text_field": "AMAZON_BEDROCK_TEXT_CHUNK",
                        "metadata_field": "AMAZON_BEDROCK_METADATA",
                    },
                },
            },
            region=region,
            opts=pulumi.ResourceOptions(
                depends_on=[
                    knowledge_base_policy,
                    hybrid_collection,
                    hybrid_data_access_policy,
                ]
            ),
        )
        hybrid_data_source = aws.bedrock.AgentDataSource(
            "travel-rag-hybrid-data-source",
            knowledge_base_id=hybrid_knowledge_base.id,
            name="wikivoyage-evaluation-hybrid",
            description="Evaluation-only Wikivoyage chunks indexed for BM25 and dense retrieval",
            data_deletion_policy="RETAIN",
            data_source_configuration={
                "type": "S3",
                "s3_configuration": {
                    "bucket_arn": rag_bucket.arn,
                    "bucket_owner_account_id": account_id,
                    "inclusion_prefixes": [evaluation_source_prefix],
                },
            },
            vector_ingestion_configuration={
                "chunking_configuration": {"chunking_strategy": "NONE"}
            },
            region=region,
            opts=pulumi.ResourceOptions(
                depends_on=[hybrid_knowledge_base],
                replace_on_changes=["knowledgeBaseId"],
            ),
        )

    rag_lambda_role = aws.iam.Role(
        "travel-rag-lambda-role",
        name=rag_lambda_role_name,
        description="Execution role for the Bedrock Knowledge Base retrieval adapter",
        assume_role_policy=lambda_assume_role.json,
    )
    aws.iam.RolePolicyAttachment(
        "travel-rag-lambda-basic-execution",
        role=rag_lambda_role.name,
        policy_arn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    )
    if hybrid_active and hybrid_knowledge_base:
        runtime_knowledge_base = hybrid_knowledge_base
        runtime_namespace = f"{structured_namespace}-hybrid"
        runtime_search_type = "HYBRID"
    elif structured_active and structured_knowledge_base:
        runtime_knowledge_base = structured_knowledge_base
        runtime_namespace = structured_namespace
        runtime_search_type = "SEMANTIC"
    elif candidate_active and candidate_knowledge_base:
        runtime_knowledge_base = candidate_knowledge_base
        runtime_namespace = candidate_namespace
        runtime_search_type = "SEMANTIC"
    else:
        runtime_knowledge_base = knowledge_base
        runtime_namespace = pinecone_namespace
        runtime_search_type = "SEMANTIC"
    rag_lambda_policy_statements = [
        {
            "Sid": "RetrieveTravelEvidence",
            "Effect": "Allow",
            "Action": ["bedrock:Retrieve"],
            "Resource": runtime_knowledge_base.arn,
        }
    ]
    rag_lambda_environment = {
        "BEDROCK_KNOWLEDGE_BASE_ID": runtime_knowledge_base.id,
        "RAG_CORPUS_VERSION": runtime_namespace,
        "RAG_SEARCH_TYPE": runtime_search_type,
    }
    if candidate_active or structured_active or hybrid_active:
        rag_lambda_environment["RAG_CATEGORY_FILTER_KEY"] = "category_key"
    if reranking_model_arn:
        rag_lambda_policy_statements.extend(
            [
                {
                    "Sid": "RerankTravelEvidence",
                    "Effect": "Allow",
                    "Action": ["bedrock:Rerank"],
                    "Resource": "*",
                },
                {
                    "Sid": "InvokeTravelReranker",
                    "Effect": "Allow",
                    "Action": ["bedrock:InvokeModel"],
                    "Resource": reranking_model_arn,
                },
            ]
        )
        rag_lambda_environment.update(
            {
                "BEDROCK_RERANKING_MODEL_ARN": reranking_model_arn,
                "RAG_RERANK_CANDIDATES": str(rerank_candidates),
            }
        )

    rag_lambda_policy = aws.iam.RolePolicy(
        "travel-rag-lambda-policy",
        name="TravelRagRetrieve",
        role=rag_lambda_role.id,
        policy=pulumi.Output.json_dumps(
            {
                "Version": "2012-10-17",
                "Statement": rag_lambda_policy_statements,
            }
        ),
    )

    rag_lambda = aws.lambda_.Function(
        "travel-rag-retrieval",
        name=rag_lambda_name,
        description="Retrieve Wikivoyage evidence through Bedrock Knowledge Bases",
        role=rag_lambda_role.arn,
        handler="lambda_function.lambda_handler",
        runtime=aws.lambda_.Runtime.PYTHON3D13,
        timeout=30,
        memory_size=512,
        code=pulumi.AssetArchive(
            {"lambda_function.py": pulumi.FileAsset(str(root / "gateway" / "travel_rag_lambda.py"))}
        ),
        environment={"variables": rag_lambda_environment},
        opts=pulumi.ResourceOptions(depends_on=[rag_lambda_policy]),
    )

    rag_tool_schema = {
        "inline_payloads": [
            {
                "name": "search_travel_guides",
                "description": (
                    "Search a dated Wikivoyage corpus for source-backed destination "
                    "background. Use live or official sources for volatile information."
                ),
                "input_schema": {
                    "type": "object",
                    "properties": [
                        prop(
                            "query",
                            "string",
                            required=True,
                            description="Search question, maximum 500 characters.",
                        ),
                        prop(
                            "destination",
                            "string",
                            required=True,
                            description="Required destination, maximum 200 characters.",
                        ),
                        prop(
                            "country",
                            "string",
                            description="Optional country, maximum 200 characters.",
                        ),
                        prop(
                            "top_k",
                            "integer",
                            description="Results to request, from 1 to 10.",
                        ),
                    ],
                },
            }
        ]
    }
    rag_target = aws.bedrock.AgentcoreGatewayTarget(
        "travel-rag-target",
        name="travel-rag",
        gateway_identifier=gateway.gateway_id,
        description="Wikivoyage retrieval through Bedrock Knowledge Bases and Pinecone",
        credential_provider_configuration={"gateway_iam_role": {}},
        target_configuration={
            "mcp": {
                "lambda_": {
                    "lambda_arn": rag_lambda.arn,
                    "tool_schema": rag_tool_schema,
                }
            }
        },
        region=region,
        opts=pulumi.ResourceOptions(depends_on=[gateway_policy, rag_lambda]),
    )
    exported_target_ids["travel-rag"] = rag_target.target_id
    pulumi.export("travel_rag_bucket", rag_bucket.bucket)
    pulumi.export("travel_rag_lambda_arn", rag_lambda.arn)
    pulumi.export("travel_rag_target_id", rag_target.target_id)
    pulumi.export("travel_rag_namespace", pinecone_namespace)
    pulumi.export("travel_rag_source_prefix", source_prefix)
    pulumi.export("travel_rag_knowledge_base_id", knowledge_base.id)
    pulumi.export("travel_rag_data_source_id", data_source.data_source_id)
    pulumi.export("travel_rag_pinecone_secret_arn", pinecone_secret_arn)
    if candidate_knowledge_base and candidate_data_source:
        pulumi.export("travel_rag_candidate_namespace", candidate_namespace)
        pulumi.export("travel_rag_candidate_source_prefix", candidate_source_prefix)
        pulumi.export("travel_rag_candidate_knowledge_base_id", candidate_knowledge_base.id)
        pulumi.export("travel_rag_candidate_data_source_id", candidate_data_source.data_source_id)
    if evaluation_knowledge_base and evaluation_data_source:
        pulumi.export("travel_rag_evaluation_namespace", evaluation_namespace)
        pulumi.export("travel_rag_evaluation_source_prefix", evaluation_source_prefix)
        pulumi.export("travel_rag_evaluation_knowledge_base_id", evaluation_knowledge_base.id)
        pulumi.export(
            "travel_rag_evaluation_data_source_id",
            evaluation_data_source.data_source_id,
        )
    if evaluation_1024_knowledge_base and evaluation_1024_data_source:
        pulumi.export("travel_rag_evaluation_1024_namespace", evaluation_1024_namespace)
        pulumi.export(
            "travel_rag_evaluation_1024_source_prefix",
            evaluation_1024_source_prefix,
        )
        pulumi.export(
            "travel_rag_evaluation_1024_knowledge_base_id",
            evaluation_1024_knowledge_base.id,
        )
        pulumi.export(
            "travel_rag_evaluation_1024_data_source_id",
            evaluation_1024_data_source.data_source_id,
        )
    if structured_knowledge_base and structured_data_source:
        pulumi.export("travel_rag_structured_namespace", structured_namespace)
        pulumi.export("travel_rag_structured_source_prefix", structured_source_prefix)
        pulumi.export("travel_rag_structured_knowledge_base_id", structured_knowledge_base.id)
        pulumi.export(
            "travel_rag_structured_data_source_id", structured_data_source.data_source_id
        )
    if hybrid_collection:
        pulumi.export("travel_rag_hybrid_collection_arn", hybrid_collection.arn)
        pulumi.export("travel_rag_hybrid_collection_endpoint", hybrid_collection.collection_endpoint)
        pulumi.export("travel_rag_hybrid_index_name", hybrid_index_name)
    if hybrid_knowledge_base and hybrid_data_source:
        pulumi.export("travel_rag_hybrid_knowledge_base_id", hybrid_knowledge_base.id)
        pulumi.export("travel_rag_hybrid_data_source_id", hybrid_data_source.data_source_id)

if mcp_tools_endpoint is not None:
    if isinstance(mcp_tools_endpoint, pulumi.Output):
        mcp_tools_target_configuration = mcp_tools_endpoint.apply(mcp_server_target_configuration)
    else:
        mcp_tools_target_configuration = mcp_server_target_configuration(mcp_tools_endpoint)
    mcp_tools_target = aws.bedrock.AgentcoreGatewayTarget(
        "mcp-tools-target",
        name="mcp-tools",
        gateway_identifier=gateway.gateway_id,
        description="Open-source web search and OpenStreetMap MCP tools",
        # AgentCore Runtime MCP endpoint; Gateway signs with its service role.
        credential_provider_configuration={"gateway_iam_role": {}},
        target_configuration=mcp_tools_target_configuration,
        region=region,
        opts=pulumi.ResourceOptions(depends_on=[gateway_policy]),
    )
    exported_target_ids["mcp-tools"] = mcp_tools_target.target_id

# RapidAPI: API Gateway HTTP proxy injects x-api-host; Gateway injects x-api-key.
for spec in selected_rapidapi_specs(_present_secret_keys):
    key_name = resolve_key_config_name(
        _present_secret_keys, spec.key_config_name, spec.fallback_key_config_names
    )
    assert key_name is not None  # guarded by selected_rapidapi_specs
    provider = credential_providers[key_name]
    api = aws.apigatewayv2.Api(
        f"rapidapi-proxy-{spec.name}",
        name=f"benchma-rapidapi-{spec.name}",
        protocol_type="HTTP",
        description=f"Injects {spec.host} for RapidAPI MCP ({spec.name})",
    )
    integration = aws.apigatewayv2.Integration(
        f"rapidapi-integration-{spec.name}",
        api_id=api.id,
        integration_type="HTTP_PROXY",
        integration_method="ANY",
        integration_uri=RAPIDAPI_UPSTREAM,
        payload_format_version="1.0",
        request_parameters=rapidapi_proxy_request_parameters(spec.host),
    )
    # $default catches every method/path the Gateway MCP client may use.
    route = aws.apigatewayv2.Route(
        f"rapidapi-route-{spec.name}",
        api_id=api.id,
        route_key="$default",
        target=integration.id.apply(lambda integration_id: f"integrations/{integration_id}"),
    )
    stage = aws.apigatewayv2.Stage(
        f"rapidapi-stage-{spec.name}",
        api_id=api.id,
        name="$default",
        auto_deploy=True,
        opts=pulumi.ResourceOptions(depends_on=[route]),
    )
    rapidapi_target = aws.bedrock.AgentcoreGatewayTarget(
        f"rapidapi-target-{spec.name}",
        name=spec.name,
        gateway_identifier=gateway.gateway_id,
        description=spec.description,
        credential_provider_configuration=api_key_credential_provider_configuration(
            provider.credential_provider_arn
        ),
        target_configuration=api.api_endpoint.apply(mcp_server_target_configuration),
        region=region,
        opts=pulumi.ResourceOptions(depends_on=[gateway_policy, stage, provider]),
    )
    exported_target_ids[spec.name] = rapidapi_target.target_id
    pulumi.export(f"{spec.name}_proxy_endpoint", api.api_endpoint)

for spec in selected_remote_mcp_specs(_present_secret_keys):
    provider = credential_providers[spec.key_config_name]
    remote_target = aws.bedrock.AgentcoreGatewayTarget(
        f"remote-mcp-{spec.name}",
        name=spec.name,
        gateway_identifier=gateway.gateway_id,
        description=spec.description,
        credential_provider_configuration=api_key_credential_provider_configuration(
            provider.credential_provider_arn,
            parameter_name=spec.credential_parameter_name,
        ),
        target_configuration=mcp_server_target_configuration(spec.endpoint),
        region=region,
        opts=pulumi.ResourceOptions(depends_on=[gateway_policy, provider]),
    )
    exported_target_ids[spec.name] = remote_target.target_id

pulumi.export("mcp_target_ids", exported_target_ids)
pulumi.export("enabled_mcp_targets", sorted(exported_target_ids))
