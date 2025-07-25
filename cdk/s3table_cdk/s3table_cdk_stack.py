from aws_cdk import (
    Duration,
    Stack,
    RemovalPolicy,
    CfnOutput,
    Aws,
    Size,
    aws_s3 as s3,
    aws_s3tables as s3tables,
    aws_sqs as sqs,
    aws_lambda as lambda_,
    aws_lambda_event_sources as lambda_event_sources,
    aws_s3_notifications as s3n,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_glue as glue,
    aws_lakeformation as lakeformation,
    aws_emrserverless as emrs,
    aws_s3_deployment as s3deploy,
    custom_resources as cr,
    CustomResource
)
from constructs import Construct
import time

class S3TableCdkStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # S3 存储桶 - 用于存储原始数据、处理后的数据和最终表数据
        data_bucket = s3.Bucket(
            self, "TelematicsDataUploadBucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            versioned=True
        )

        # SQS 队列 - 接收S3事件通知
        data_queue = sqs.Queue(
            self, "TelematicsDataDecodingQueue",
            visibility_timeout=Duration.seconds(300)
        )

        # 配置S3桶发送事件到SQS
        data_bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.SqsDestination(data_queue),
            s3.NotificationKeyFilter(prefix="raw/", suffix=".zip")
        )

        # 创建 lambda Layer - 仅支持ARM架构
        lambda_layer = lambda_.LayerVersion(
            self, "greptime layer",
            code=lambda_.Code.from_asset("lambda_layers/"),
            compatible_runtimes=[lambda_.Runtime.PYTHON_3_13],
            compatible_architectures=[lambda_.Architecture.ARM_64],  # 只支持ARM架构
            description="Layer containing greptime required packages for ARM64"
        )

        # Lambda函数 - 处理SQS消息
        processing_lambda = lambda_.Function(
            self, "ProcessingFunction",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="index.handler",
            code=lambda_.Code.from_asset("lambda"),
            timeout=Duration.seconds(300),
            memory_size=1024,
            layers=[lambda_layer],
            environment={
                "S3_BUCKET": data_bucket.bucket_name
            }
        )

        # 将SQS作为Lambda的事件源
        processing_lambda.add_event_source(
            lambda_event_sources.SqsEventSource(data_queue)
        )

        # 给Lambda授予S3读写权限
        data_bucket.grant_read_write(processing_lambda)

        # 添加HeadObject权限
        processing_lambda.add_to_role_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["s3:HeadObject"],
                resources=[data_bucket.arn_for_objects("*")]
            )
        )

        # EC2实例 - 生成数据并上传到S3
        vpc = ec2.Vpc(
            self, "DataGenerationVPC",
            max_azs=2,
            nat_gateways=1,  # 添加NAT网关
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="Public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24
                ),
                ec2.SubnetConfiguration(
                    name="Private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24
                )
            ]
        )

        # EC2 IAM角色与策略
        ec2_role = iam.Role(
            self, "EC2Role",
            assumed_by=iam.ServicePrincipal("ec2.amazonaws.com")
        )
        
        data_bucket.grant_read_write(ec2_role)

        # EC2实例
        instance = ec2.Instance(
            self, "DataGenerationInstance",
            vpc=vpc,
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE3, 
                ec2.InstanceSize.MEDIUM
            ),
            machine_image=ec2.AmazonLinuxImage(
                generation=ec2.AmazonLinuxGeneration.AMAZON_LINUX_2023
            ),
            role=ec2_role,
            vpc_subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PUBLIC  # 指定使用公有子网
            ),
            associate_public_ip_address=True  # 启用公网IP
        )

        # EMR Serverless应用程序
        emr_execution_role = iam.Role(
            self, "EMRServerlessExecutionRole",
            assumed_by=iam.ServicePrincipal("emr-serverless.amazonaws.com")
        )
        
        data_bucket.grant_read_write(emr_execution_role)
        
        # 添加Glue管理员权限
        emr_execution_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["glue:*"],  # 授予所有Glue权限
                resources=["*"]
            )
        )
        
        # 添加CloudWatch完整权限
        emr_execution_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("CloudWatchFullAccess")
        )
        
        # 添加管理员权限
        emr_execution_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("AdministratorAccess")
        )
        
        # 输出EMR执行角色的ARN
        CfnOutput(
            self, "EMRExecutionRoleArn",
            value=emr_execution_role.role_arn,
            description="ARN of the EMR Serverless execution role"
        )
        
        # 获取默认安全组
        default_security_group = ec2.SecurityGroup.from_security_group_id(
            self, "DefaultSecurityGroup",
            vpc.vpc_default_security_group
        )
        
        # 修改默认安全组，允许所有入站流量，目标是安全组自身
        default_security_group_rule = ec2.CfnSecurityGroupIngress(
            self, "DefaultSGIngressAll",
            ip_protocol="-1",  # -1 表示所有协议
            group_id=vpc.vpc_default_security_group,
            source_security_group_id=vpc.vpc_default_security_group  # 目标是安全组自身
        )
        
        # 确保有出站规则允许所有流量到任何地址
        default_security_group_egress = ec2.CfnSecurityGroupEgress(
            self, "DefaultSGEgressAll",
            ip_protocol="-1",  # -1 表示所有协议
            cidr_ip="0.0.0.0/0",  # 允许到任何地址
            group_id=vpc.vpc_default_security_group
        )
        
        # 获取私有子网IDs
        private_subnet_ids = [subnet.subnet_id for subnet in vpc.private_subnets]
        
        # 创建专用于S3 Tables VPC端点的安全组
        s3tables_endpoint_sg = ec2.SecurityGroup(
            self, "S3TablesEndpointSG",
            vpc=vpc,
            description="Security group for S3 Tables VPC endpoint",
            allow_all_outbound=True  # 允许所有出站流量
        )
        
        # 添加允许所有入站流量，目标是安全组自身
        s3tables_endpoint_sg.add_ingress_rule(
            peer=s3tables_endpoint_sg,
            connection=ec2.Port.all_traffic(),
            description="Allow all traffic from self"
        )
        
        # 为S3 Tables创建VPC接口端点
        s3tables_vpc_endpoint = ec2.InterfaceVpcEndpoint(
            self, "S3TablesVpcEndpoint",
            vpc=vpc,
            service=ec2.InterfaceVpcEndpointService(
                f"com.amazonaws.{Aws.REGION}.s3tables"
            ),
            subnets=ec2.SubnetSelection(
                subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS  # 使用与EMR相同的私有子网
            ),
            security_groups=[s3tables_endpoint_sg],  # 使用专用安全组
            private_dns_enabled=True  # 启用私有DNS
        )
        
        # 创建EMR Serverless应用程序，并配置VPC、子网和安全组
        emr_app = emrs.CfnApplication(
            self, "DataProcessingEMRApp",
            release_label="emr-7.7.0",
            type="SPARK",
            name="DataProcessingApp",
            initial_capacity=[
                emrs.CfnApplication.InitialCapacityConfigKeyValuePairProperty(
                    key="DRIVER",
                    value=emrs.CfnApplication.InitialCapacityConfigProperty(
                        worker_count=1,
                        worker_configuration=emrs.CfnApplication.WorkerConfigurationProperty(
                            cpu="4vCPU",
                            memory="16GB"
                        )
                    )
                ),
                emrs.CfnApplication.InitialCapacityConfigKeyValuePairProperty(
                    key="EXECUTOR",
                    value=emrs.CfnApplication.InitialCapacityConfigProperty(
                        worker_count=4,
                        worker_configuration=emrs.CfnApplication.WorkerConfigurationProperty(
                            cpu="4vCPU",
                            memory="16GB"
                        )
                    )
                )
            ],
            maximum_capacity={
                "cpu": "200vCPU",
                "memory": "800GB"
            },
            # 添加网络配置
            network_configuration=emrs.CfnApplication.NetworkConfigurationProperty(
                security_group_ids=[vpc.vpc_default_security_group, s3tables_endpoint_sg.security_group_id],
                subnet_ids=private_subnet_ids
            )
        )
        
        # 确保EMR应用程序在VPC端点创建后启动
        # 使用node.add_dependency代替add_depends_on，因为InterfaceVpcEndpoint不是CfnResource类型
        emr_app.node.add_dependency(s3tables_vpc_endpoint)

        # S3 Table Bucket
        cfn_table_bucket = s3tables.CfnTableBucket(
            self, "caredgedemo",
            table_bucket_name = "caredgedemo"
        )

        s3tables_lakeformation_role_policy = iam.PolicyStatement(
            effect=iam.Effect.ALLOW,
            principals=[iam.ServicePrincipal("lakeformation.amazonaws.com")],
            actions=[
                "sts:SetContext",
                "sts:SetSourceIdentity"
            ],
            conditions={
                "StringEquals": {
                    "aws:SourceAccount": Aws.ACCOUNT_ID
                }
            }
        )

        # 创建Lake Formation用于访问S3 Tables的IAM角色
        s3tables_lakeformation_role = iam.Role(
            self, "S3TablesRoleForLakeFormationDemo",
            role_name="S3TablesRoleForLakeFormationDemo",
            assumed_by=iam.ServicePrincipal("lakeformation.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name("AdministratorAccess")
            ]
        )

        s3tables_lakeformation_role.assume_role_policy.add_statements(
            s3tables_lakeformation_role_policy
        )

        # 添加S3 Tables列表权限 - 这是身份策略，不要指定principals
        s3tables_lakeformation_role.add_to_policy(
            iam.PolicyStatement(
                sid="LakeFormationPermissionsForS3ListTableBucket",
                effect=iam.Effect.ALLOW,
                actions=["s3tables:ListTableBuckets"],
                resources=["*"]  # 已经指定了资源
            )
        )

        # 添加S3 Tables数据访问权限 - 这是身份策略，不要指定principals
        s3tables_lakeformation_role.add_to_policy(
            iam.PolicyStatement(
                sid="LakeFormationDataAccessPermissionsForS3TableBucket",
                effect=iam.Effect.ALLOW,
                actions=[
                    "s3tables:CreateTableBucket",
                    "s3tables:GetTableBucket",
                    "s3tables:CreateNamespace",
                    "s3tables:GetNamespace",
                    "s3tables:ListNamespaces",
                    "s3tables:DeleteNamespace",
                    "s3tables:DeleteTableBucket",
                    "s3tables:CreateTable",
                    "s3tables:DeleteTable",
                    "s3tables:GetTable",
                    "s3tables:ListTables",
                    "s3tables:RenameTable",
                    "s3tables:UpdateTableMetadataLocation",
                    "s3tables:GetTableMetadataLocation",
                    "s3tables:GetTableData",
                    "s3tables:PutTableData"
                ],
                resources=[f"arn:aws:s3tables:{Aws.REGION}:{Aws.ACCOUNT_ID}:bucket/*"]  # 已经指定了资源
            )
        )

        # 创建Glue联邦目录连接到S3 Tables - 使用自定义资源
        glue_catalog_role = iam.Role(
            self, "GlueCatalogCustomResourceRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com")
        )
        
        # 添加Glue权限 - 只需要一处定义
        glue_catalog_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "*"
                ],
                resources=["*"]
            )
        )

                # 添加 Lake Formation 权限
        glue_catalog_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "lakeformation:GetDataAccess",
                    "lakeformation:GrantPermissions",
                    "lakeformation:GetCatalogResource",
                    "lakeformation:ListPermissions",
                    "lakeformation:GetDataLakeSettings"
                ],
                resources=["*"]
            )
        )
        
        # 添加基本的Lambda执行权限
        glue_catalog_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")
        )

        # 修改 Lambda 函数，确保使用正确的角色
        create_catalog_lambda = lambda_.Function(
            self, "CreateCatalogLambda",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="glue_catalog_handler.handler",
            code=lambda_.Code.from_asset("lambda"),
            timeout=Duration.seconds(300),
            memory_size=256,
            role=glue_catalog_role,
            environment={
                "BUCKET_NAME": data_bucket.bucket_name
            }
        )
        
        # 使用自定义资源调用Lambda
        s3tables_catalog = cr.Provider(
            self, "GlueCatalogProvider",
            on_event_handler=create_catalog_lambda
        )
        
        # 创建自定义资源来触发Lambda
        s3tables_catalog_resource = CustomResource(
            self, "S3TablesCatalogResource",
            service_token=s3tables_catalog.service_token,
            properties={
                "Region": Aws.REGION,
                "AccountId": Aws.ACCOUNT_ID,
                "Version": "1.2",  # 每次需要更新时递增此值
                "Timestamp": str(int(time.time()))  # 添加时间戳确保每次部署都不同
            }
        )

        # 分别上传脚本和JAR文件
        # 1. 上传Python脚本（小文件）
        script_deployment = s3deploy.BucketDeployment(
            self, "DeployProcessScript",
            sources=[s3deploy.Source.asset("emr_job", exclude=["jar/**"])],  # 排除jar目录
            destination_bucket=data_bucket,
            destination_key_prefix="scripts"
        )
        
        # 2. 单独上传JAR文件（大文件，需要更多内存）
        jar_deployment = s3deploy.BucketDeployment(
            self, "DeployJarFiles",
            sources=[s3deploy.Source.asset("emr_job/jar")],
            destination_bucket=data_bucket,
            destination_key_prefix="scripts/jar",
            memory_limit=3008,  # 最大内存 3GB
            ephemeral_storage_size=Size.gibibytes(10)  # 增加临时存储空间
        )

        # 创建一个自定义资源的IAM策略，允许PassRole操作
        emr_custom_resource_role = iam.Role(
            self, "EMRCustomResourceRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com")
        )

        # 添加PassRole权限
        emr_custom_resource_role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[emr_execution_role.role_arn],
                effect=iam.Effect.ALLOW
            )
        )

        # 添加EMR Serverless权限
        emr_custom_resource_role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "emr-serverless:StartJobRun",
                    "emr-serverless:GetJobRun",
                    "emr-serverless:CancelJobRun"
                ],
                resources=["*"],
                effect=iam.Effect.ALLOW
            )
        )

        # 添加基本的Lambda执行权限
        emr_custom_resource_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")
        )

        # 修改自定义资源，使用新创建的角色
        emr_job_custom_resource = cr.AwsCustomResource(
            self, "EMRServerlessJobRun",
            on_create={
                "service": "EMRServerless", 
                "action": "startJobRun",
                "parameters": {
                    "applicationId": emr_app.attr_application_id,
                    "executionRoleArn": emr_execution_role.role_arn,
                    "jobDriver": {
                        "sparkSubmit": {
                            "entryPoint": f"s3://{data_bucket.bucket_name}/scripts/process_data.py",
                            "entryPointArguments": [
                                data_bucket.bucket_name
                            ],
                            "sparkSubmitParameters": "--packages org.apache.iceberg:iceberg-spark-runtime-3.5_2.12:1.6.1,software.amazon.s3tables:s3-tables-catalog-for-iceberg-runtime:0.1.3 " +
                            "--conf spark.executor.cores=4 " +
                            "--conf spark.executor.memory=16g " +
                            f"--conf spark.sql.catalog.gpdemo=org.apache.iceberg.spark.SparkCatalog " +
                            f"--conf spark.sql.catalog.gpdemo.catalog-impl=software.amazon.s3tables.iceberg.S3TablesCatalog " +
                            f"--conf spark.sql.catalog.gpdemo.warehouse=arn:aws:s3tables:{Aws.REGION}:{Aws.ACCOUNT_ID}:bucket/caredgedemo " +
                            "--conf spark.sql.extensions=org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions " +
                            f"--conf spark.sql.catalog.defaultCatalog=gpdemo " +
                            f"--conf spark.sql.catalog.gpdemo.client.region={Aws.REGION} " +
                            f"--conf spark.jars=s3://{data_bucket.bucket_name}/jar/*"
                        }
                    },
                    "configurationOverrides": {
                        "applicationConfiguration": [{
                            "classification": "spark-defaults",
                            "properties": {
                                "spark.dynamicAllocation.enabled": "true"
                            }
                        }],
                        "monitoringConfiguration": {
                            "s3MonitoringConfiguration": {
                                "logUri": f"s3://{data_bucket.bucket_name}/logs/"
                            }
                        }
                    }
                },
                "physical_resource_id": cr.PhysicalResourceId.of("EMRServerlessJobRun")
            },
            policy=cr.AwsCustomResourcePolicy.from_statements([
                iam.PolicyStatement(
                    actions=["emr-serverless:StartJobRun"],
                    resources=["*"],
                    effect=iam.Effect.ALLOW
                ),
                iam.PolicyStatement(
                    actions=["iam:PassRole"],
                    resources=[emr_execution_role.role_arn],
                    effect=iam.Effect.ALLOW
                )
            ]),
            role=emr_custom_resource_role  # 使用新创建的角色
        )

        # 确保作业依赖于脚本和JAR文件部署
        emr_job_custom_resource.node.add_dependency(script_deployment)
        emr_job_custom_resource.node.add_dependency(jar_deployment)

        # 输出重要资源信息
        CfnOutput(self, "DataBucketName", value=data_bucket.bucket_name)
        CfnOutput(self, "SQSQueueUrl", value=data_queue.queue_url)
        CfnOutput(self, "LambdaFunction", value=processing_lambda.function_name)
        CfnOutput(self, "EMRServerlessAppId", value=emr_app.attr_application_id)
        CfnOutput(self, "EC2InstanceId", value=instance.instance_id)
        # 输出S3 Tables角色ARN
        CfnOutput(
            self, "S3TablesLakeFormationRoleArn", 
            value=s3tables_lakeformation_role.role_arn,
            description="ARN of the IAM role for Lake Formation to access S3 Tables"
        )
        
        # 输出S3 Tables VPC端点ID
        CfnOutput(
            self, "S3TablesVpcEndpointId",
            value=s3tables_vpc_endpoint.vpc_endpoint_id,
            description="ID of the S3 Tables VPC Endpoint"
        )
        
        # 输出S3 Tables端点安全组ID
        CfnOutput(
            self, "S3TablesEndpointSecurityGroupId",
            value=s3tables_endpoint_sg.security_group_id,
            description="ID of the S3 Tables Endpoint Security Group"
        )

        # 输出Glue联邦目录名称
        CfnOutput(
            self, "S3TablesGlueCatalogName", 
            value="s3tablescatalog",
            description="Name of the Glue Federated Catalog for S3 Tables"
        )

        # 创建 Lake Formation 权限管理的 Lambda 角色
        lakeformation_permissions_role = iam.Role(
            self, "LakeFormationPermissionsRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com")
        )

        # 添加 Lake Formation 权限
        lakeformation_permissions_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "lakeformation:GetDataLakeSettings",
                    "lakeformation:PutDataLakeSettings"
                ],
                resources=["*"]
            )
        )

        # 添加基本的 Lambda 执行权限
        lakeformation_permissions_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")
        )

        # 创建 Lake Formation 资源注册的 Lambda 角色
        lakeformation_resource_role = iam.Role(
            self, "LakeFormationResourceRole",
            assumed_by=iam.ServicePrincipal("lambda.amazonaws.com")
        )

        # 添加 Lake Formation 权限
        lakeformation_resource_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "lakeformation:RegisterResource",
                    "lakeformation:DeregisterResource"
                ],
                resources=["*"]
            )
        )

        # 添加 IAM PassRole 权限
        lakeformation_resource_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=["iam:PassRole"],
                resources=[s3tables_lakeformation_role.role_arn]  # 明确指定可以传递的角色
            )
        )

        # 添加基本的 Lambda 执行权限
        lakeformation_resource_role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSLambdaBasicExecutionRole")
        )

        # 在创建 lakeformation_resource_role 时添加 S3 权限
        lakeformation_resource_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "s3:PutObject"  # 允许向 S3 写入响应
                ],
                resources=["*"]  # 您可以限制到特定的 S3 bucket
            )
        )

        # 添加 CloudFormation 相关权限
        lakeformation_resource_role.add_to_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                actions=[
                    "cloudformation:SignalResource"  # 允许向 CloudFormation 发送信号
                ],
                resources=["*"]
            )
        )

        # 创建 Lake Formation 权限管理的 Lambda 函数
        lakeformation_permissions_lambda = lambda_.Function(
            self, "LakeFormationPermissionsLambda",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="lakeformation_permissions_handler.handler",
            code=lambda_.Code.from_asset("lambda"),
            timeout=Duration.seconds(300),
            memory_size=256,
            role=lakeformation_permissions_role
        )

        # 创建自定义资源提供者
        lakeformation_permissions_provider = cr.Provider(
            self, "LakeFormationPermissionsProvider",
            on_event_handler=lakeformation_permissions_lambda
        )

        # 创建自定义资源，设置 Lake Formation 权限
        lakeformation_permissions_resource = CustomResource(
            self, "LakeFormationPermissionsResource",
            service_token=lakeformation_permissions_provider.service_token,
            properties={
                "RoleArns": [
                    glue_catalog_role.role_arn,
                    emr_execution_role.role_arn,
                    lakeformation_resource_role.role_arn
                ],
                "Version": "1.0",
                "Timestamp": str(int(time.time()))
            }
        )

        # 设置 Lake Formation 权限资源依赖于 IAM 角色
        lakeformation_permissions_resource.node.add_dependency(glue_catalog_role)
        lakeformation_permissions_resource.node.add_dependency(emr_execution_role)
        lakeformation_permissions_resource.node.add_dependency(lakeformation_resource_role)


        # 重要：修改 Glue Catalog 资源的创建顺序，使其依赖于 Lake Formation 权限
        # 注意：这里需要修改之前的代码，将 s3tables_catalog_resource 的创建移到 lakeformation_permissions_resource 之后
        # 或者添加依赖关系
        s3tables_catalog_resource.node.add_dependency(lakeformation_permissions_resource)

        # EMR 应用程序依赖于 Lake Formation 权限
        emr_app.node.add_dependency(lakeformation_permissions_resource)

        # 如果有 EMR 作业自定义资源，也添加依赖
        if 'emr_job_custom_resource' in vars():
            emr_job_custom_resource.node.add_dependency(lakeformation_permissions_resource)

        # 创建 Lake Formation 资源注册的 Lambda 函数
        lakeformation_resource_lambda = lambda_.Function(
            self, "LakeFormationResourceLambda",
            runtime=lambda_.Runtime.PYTHON_3_13,
            handler="lakeformation_resource_handler.handler",
            code=lambda_.Code.from_asset("lambda"),
            timeout=Duration.seconds(300),
            memory_size=256,
            role=lakeformation_resource_role
        )

        # 创建自定义资源提供者
        lakeformation_resource_provider = cr.Provider(
            self, "LakeFormationResourceProvider",
            on_event_handler=lakeformation_resource_lambda
        )

        # 创建自定义资源，注册 S3 Tables 资源
        lakeformation_resource_registration = CustomResource(
            self, "LakeFormationResourceRegistration",
            service_token=lakeformation_resource_provider.service_token,
            properties={
                "ResourceArn": f"arn:aws:s3tables:{Aws.REGION}:{Aws.ACCOUNT_ID}:bucket/caredgedemo",
                "ResourceRoleArn": s3tables_lakeformation_role.role_arn,
                "Version": "1.0",
                "Timestamp": str(int(time.time()))
            }
        )

        # 设置依赖关系
        lakeformation_resource_registration.node.add_dependency(s3tables_lakeformation_role)
        lakeformation_resource_registration.node.add_dependency(lakeformation_permissions_resource)

        # 确保 Glue Catalog 资源依赖于资源注册
        s3tables_catalog_resource.node.add_dependency(lakeformation_resource_registration)
