"""Live AWS discovery via boto3.

Discovery is parallelized using `asyncio.to_thread` — boto3 is synchronous, but
the GIL is released during network I/O, so a thread pool gives near-linear
speedups on multi-resource workloads. We use this rather than aioboto3 to
avoid an extra dependency surface.

All paginated APIs are fully drained. Exponential backoff with jitter is
delegated to botocore's adaptive retry mode (see utils/auth.py).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable, Optional

import boto3

from sudiviz.discovery.models import (
    CloudProvider,
    DiscoveryResult,
    ECSCluster,
    ECSService,
    EKSCluster,
    EKSNodeGroup,
    HealthStatus,
    Instance,
    LambdaFunction,
    Listener,
    LoadBalancer,
    RDSInstance,
    S3Bucket,
    SecurityGroup,
    SecurityGroupRule,
    Target,
    TargetGroup,
)
from sudiviz.utils.auth import build_botocore_config, get_session, whoami

logger = logging.getLogger(__name__)


# Map ELBv2 TargetHealth state strings to our normalized HealthStatus.
_HEALTH_MAP = {
    "healthy": HealthStatus.HEALTHY,
    "unhealthy": HealthStatus.UNHEALTHY,
    "initial": HealthStatus.INITIAL,
    "draining": HealthStatus.DRAINING,
    "unused": HealthStatus.UNUSED,
    "unavailable": HealthStatus.UNKNOWN,
}


def _paginate(client: Any, op_name: str, **kwargs: Any) -> Iterable[dict]:
    """Drain a paginated boto3 operation. Yields each page dict."""
    paginator = client.get_paginator(op_name)
    yield from paginator.paginate(**kwargs)


def _matches_tags(resource_tags: list[dict], filter_tags: dict[str, str]) -> bool:
    """Check whether all key=value pairs in `filter_tags` are present."""
    if not filter_tags:
        return True
    have = {t["Key"]: t["Value"] for t in resource_tags or []}
    return all(have.get(k) == v for k, v in filter_tags.items())


def _tag_dict(tags: list[dict]) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in tags or []}


# ---------------------------------------------------------------------------
# Synchronous primitives (one per resource type) — wrapped in async fns below.
# ---------------------------------------------------------------------------


def _discover_albs_sync(
    session: boto3.Session,
    vpc_id: Optional[str],
    tag_filter: dict[str, str],
) -> list[LoadBalancer]:
    elbv2 = session.client("elbv2", config=build_botocore_config())
    lbs: list[LoadBalancer] = []
    arns: list[str] = []
    raw: dict[str, dict] = {}

    for page in _paginate(elbv2, "describe_load_balancers"):
        for lb in page["LoadBalancers"]:
            if vpc_id and lb.get("VpcId") != vpc_id:
                continue
            arns.append(lb["LoadBalancerArn"])
            raw[lb["LoadBalancerArn"]] = lb

    if not arns:
        return lbs

    # Tags must be fetched in batches of <=20.
    tags_by_arn: dict[str, list[dict]] = {}
    for i in range(0, len(arns), 20):
        chunk = arns[i : i + 20]
        resp = elbv2.describe_tags(ResourceArns=chunk)
        for td in resp["TagDescriptions"]:
            tags_by_arn[td["ResourceArn"]] = td.get("Tags", [])

    for arn, lb in raw.items():
        tags = tags_by_arn.get(arn, [])
        if not _matches_tags(tags, tag_filter):
            continue
        lbs.append(
            LoadBalancer(
                provider=CloudProvider.AWS,
                arn=arn,
                name=lb["LoadBalancerName"],
                dns_name=lb.get("DNSName"),
                scheme=lb.get("Scheme", "internet-facing"),
                type=lb.get("Type", "application"),
                vpc_id=lb.get("VpcId"),
                state=lb.get("State", {}).get("Code", "unknown"),
                security_group_ids=lb.get("SecurityGroups", []) or [],
                subnet_ids=[az["SubnetId"] for az in lb.get("AvailabilityZones", []) if "SubnetId" in az],
                tags=_tag_dict(tags),
            )
        )
    return lbs


def _discover_listeners_sync(session: boto3.Session, lb_arn: str) -> list[Listener]:
    """Discover listeners + their default actions + their rule actions.

    This is THE function for orphan detection: every listener (and every
    listener rule) gets unrolled into the set of target group ARNs it points
    to. A target group never appearing in this set is an orphan.
    """
    elbv2 = session.client("elbv2", config=build_botocore_config())
    listeners: list[Listener] = []

    for page in _paginate(elbv2, "describe_listeners", LoadBalancerArn=lb_arn):
        for raw in page["Listeners"]:
            default_arns = _extract_target_group_arns(raw.get("DefaultActions", []))
            rule_arns: list[str] = []
            for rpage in _paginate(elbv2, "describe_rules", ListenerArn=raw["ListenerArn"]):
                for rule in rpage["Rules"]:
                    rule_arns.extend(_extract_target_group_arns(rule.get("Actions", [])))
            listeners.append(
                Listener(
                    arn=raw["ListenerArn"],
                    lb_arn=lb_arn,
                    protocol=raw.get("Protocol", ""),
                    port=raw.get("Port", 0),
                    default_target_group_arns=default_arns,
                    rule_target_group_arns=rule_arns,
                )
            )
    return listeners


def _extract_target_group_arns(actions: list[dict]) -> list[str]:
    """Walk an Actions array (default or rule) and pull out target group ARNs."""
    arns: list[str] = []
    for action in actions:
        if "TargetGroupArn" in action:
            arns.append(action["TargetGroupArn"])
        forward = action.get("ForwardConfig") or {}
        for tg in forward.get("TargetGroups", []) or []:
            if "TargetGroupArn" in tg:
                arns.append(tg["TargetGroupArn"])
    return arns


def _discover_target_groups_sync(
    session: boto3.Session,
    vpc_id: Optional[str],
) -> list[TargetGroup]:
    elbv2 = session.client("elbv2", config=build_botocore_config())
    tgs: list[TargetGroup] = []
    arns: list[str] = []
    raw: dict[str, dict] = {}

    for page in _paginate(elbv2, "describe_target_groups"):
        for tg in page["TargetGroups"]:
            if vpc_id and tg.get("VpcId") != vpc_id:
                continue
            arns.append(tg["TargetGroupArn"])
            raw[tg["TargetGroupArn"]] = tg

    tags_by_arn: dict[str, list[dict]] = {}
    for i in range(0, len(arns), 20):
        chunk = arns[i : i + 20]
        resp = elbv2.describe_tags(ResourceArns=chunk)
        for td in resp["TagDescriptions"]:
            tags_by_arn[td["ResourceArn"]] = td.get("Tags", [])

    for arn, tg in raw.items():
        try:
            health = elbv2.describe_target_health(TargetGroupArn=arn).get("TargetHealthDescriptions", [])
        except Exception as exc:  # noqa: BLE001 — best-effort per TG
            logger.warning("describe_target_health failed for %s: %s", arn, exc)
            health = []

        targets = [
            Target(
                target_id=h["Target"]["Id"],
                target_type=tg.get("TargetType", "instance"),
                port=h["Target"].get("Port"),
                health=_HEALTH_MAP.get(h.get("TargetHealth", {}).get("State", "").lower(), HealthStatus.UNKNOWN),
                health_reason=h.get("TargetHealth", {}).get("Reason"),
                health_description=h.get("TargetHealth", {}).get("Description"),
            )
            for h in health
        ]

        tgs.append(
            TargetGroup(
                arn=arn,
                name=tg["TargetGroupName"],
                protocol=tg.get("Protocol", ""),
                port=tg.get("Port", 0),
                vpc_id=tg.get("VpcId"),
                targets=targets,
                associated_lb_arns=tg.get("LoadBalancerArns", []) or [],
                tags=_tag_dict(tags_by_arn.get(arn, [])),
            )
        )
    return tgs


def _discover_security_groups_sync(
    session: boto3.Session,
    vpc_id: Optional[str],
    only_ids: Optional[set[str]] = None,
) -> list[SecurityGroup]:
    ec2 = session.client("ec2", config=build_botocore_config())
    sgs: list[SecurityGroup] = []

    filters = []
    if vpc_id:
        filters.append({"Name": "vpc-id", "Values": [vpc_id]})

    kwargs: dict[str, Any] = {"Filters": filters} if filters else {}
    if only_ids:
        kwargs["GroupIds"] = list(only_ids)

    for page in _paginate(ec2, "describe_security_groups", **kwargs):
        for sg in page["SecurityGroups"]:
            rules = _flatten_sg_permissions(sg.get("IpPermissions", []), "ingress")
            rules += _flatten_sg_permissions(sg.get("IpPermissionsEgress", []), "egress")
            sgs.append(
                SecurityGroup(
                    sg_id=sg["GroupId"],
                    name=sg.get("GroupName", ""),
                    vpc_id=sg.get("VpcId"),
                    rules=rules,
                    tags=_tag_dict(sg.get("Tags", [])),
                )
            )

    # Determine attachments by scanning ENIs.
    attached: dict[str, set[str]] = {sg.sg_id: set() for sg in sgs}
    eni_filter = [{"Name": "vpc-id", "Values": [vpc_id]}] if vpc_id else []
    for page in _paginate(ec2, "describe_network_interfaces", Filters=eni_filter):
        for eni in page["NetworkInterfaces"]:
            for grp in eni.get("Groups", []):
                if grp["GroupId"] in attached:
                    attached[grp["GroupId"]].add(eni["NetworkInterfaceId"])
    for sg in sgs:
        sg.attached_to = sorted(attached.get(sg.sg_id, set()))
    return sgs


def _flatten_sg_permissions(perms: list[dict], direction: str) -> list[SecurityGroupRule]:
    rules: list[SecurityGroupRule] = []
    for p in perms:
        cidrs = [r["CidrIp"] for r in p.get("IpRanges", []) if "CidrIp" in r]
        cidrs += [r["CidrIpv6"] for r in p.get("Ipv6Ranges", []) if "CidrIpv6" in r]
        ref_sgs = [u["GroupId"] for u in p.get("UserIdGroupPairs", []) if "GroupId" in u]
        rules.append(
            SecurityGroupRule(
                direction=direction,
                protocol=p.get("IpProtocol", "-1"),
                from_port=p.get("FromPort"),
                to_port=p.get("ToPort"),
                cidr_ranges=cidrs,
                referenced_sg_ids=ref_sgs,
                description=(p.get("IpRanges") or [{}])[0].get("Description"),
            )
        )
    return rules


def _discover_instances_sync(
    session: boto3.Session,
    vpc_id: Optional[str],
    only_ids: Optional[set[str]] = None,
) -> list[Instance]:
    ec2 = session.client("ec2", config=build_botocore_config())
    instances: list[Instance] = []

    filters = []
    if vpc_id:
        filters.append({"Name": "vpc-id", "Values": [vpc_id]})

    kwargs: dict[str, Any] = {"Filters": filters} if filters else {}
    if only_ids:
        kwargs["InstanceIds"] = list(only_ids)

    for page in _paginate(ec2, "describe_instances", **kwargs):
        for reservation in page["Reservations"]:
            for inst in reservation["Instances"]:
                instances.append(
                    Instance(
                        instance_id=inst["InstanceId"],
                        instance_type=inst.get("InstanceType"),
                        state=inst.get("State", {}).get("Name", "unknown"),
                        private_ip=inst.get("PrivateIpAddress"),
                        public_ip=inst.get("PublicIpAddress"),
                        vpc_id=inst.get("VpcId"),
                        subnet_id=inst.get("SubnetId"),
                        security_group_ids=[sg["GroupId"] for sg in inst.get("SecurityGroups", [])],
                        tags=_tag_dict(inst.get("Tags", [])),
                    )
                )
    return instances


# ---------------------------------------------------------------------------
# ECS discovery
# ---------------------------------------------------------------------------


def _discover_ecs_sync(session: boto3.Session, vpc_id: Optional[str]) -> list[ECSCluster]:
    ecs = session.client("ecs", config=build_botocore_config())
    clusters: list[ECSCluster] = []

    cluster_arns: list[str] = []
    for page in _paginate(ecs, "list_clusters"):
        cluster_arns.extend(page.get("clusterArns", []))
    if not cluster_arns:
        return clusters

    for i in range(0, len(cluster_arns), 100):
        chunk = cluster_arns[i : i + 100]
        resp = ecs.describe_clusters(clusters=chunk, include=["TAGS"])
        for raw in resp.get("clusters", []):
            cluster = ECSCluster(
                arn=raw["clusterArn"],
                name=raw["clusterName"],
                status=raw.get("status", "ACTIVE"),
                active_services_count=raw.get("activeServicesCount", 0),
                running_tasks_count=raw.get("runningTasksCount", 0),
                pending_tasks_count=raw.get("pendingTasksCount", 0),
                container_instances_count=raw.get("registeredContainerInstancesCount", 0),
                capacity_providers=raw.get("capacityProviders", []),
                tags=_tag_dict(raw.get("tags", [])),
            )
            cluster.services = _discover_ecs_services_sync(ecs, cluster.arn, vpc_id)
            clusters.append(cluster)
    return clusters


def _discover_ecs_services_sync(
    ecs: Any, cluster_arn: str, vpc_id: Optional[str]
) -> list[ECSService]:
    services: list[ECSService] = []
    service_arns: list[str] = []
    for page in _paginate(ecs, "list_services", cluster=cluster_arn):
        service_arns.extend(page.get("serviceArns", []))
    if not service_arns:
        return services

    for i in range(0, len(service_arns), 10):
        chunk = service_arns[i : i + 10]
        resp = ecs.describe_services(cluster=cluster_arn, services=chunk, include=["TAGS"])
        for raw in resp.get("services", []):
            # Extract VPC config from network configuration.
            net = (raw.get("networkConfiguration") or {}).get("awsvpcConfiguration") or {}
            svc_vpc = None
            subnets = net.get("subnets") or []
            sgs = net.get("securityGroups") or []

            # If vpc_id filter is set, only keep services whose subnets belong to that VPC.
            # We can't cheaply get VPC ID from ECS, so we include all unless we have a mismatch.
            lb_arns: list[str] = []
            tg_arns: list[str] = []
            for lb_cfg in raw.get("loadBalancers") or []:
                if lb_cfg.get("targetGroupArn"):
                    tg_arns.append(lb_cfg["targetGroupArn"])
                if lb_cfg.get("loadBalancerName"):
                    lb_arns.append(lb_cfg["loadBalancerName"])

            services.append(
                ECSService(
                    arn=raw["serviceArn"],
                    name=raw["serviceName"],
                    cluster_arn=cluster_arn,
                    status=raw.get("status", "UNKNOWN"),
                    desired_count=raw.get("desiredCount", 0),
                    running_count=raw.get("runningCount", 0),
                    pending_count=raw.get("pendingCount", 0),
                    launch_type=raw.get("launchType", "EC2"),
                    task_definition=raw.get("taskDefinition"),
                    vpc_id=svc_vpc,
                    subnet_ids=subnets,
                    security_group_ids=sgs,
                    load_balancer_arns=lb_arns,
                    target_group_arns=tg_arns,
                    tags=_tag_dict(raw.get("tags", [])),
                )
            )
    return services


# ---------------------------------------------------------------------------
# EKS discovery
# ---------------------------------------------------------------------------


def _discover_eks_sync(session: boto3.Session, vpc_id: Optional[str]) -> list[EKSCluster]:
    eks = session.client("eks", config=build_botocore_config())
    clusters: list[EKSCluster] = []

    cluster_names: list[str] = []
    for page in _paginate(eks, "list_clusters"):
        cluster_names.extend(page.get("clusters", []))

    for name in cluster_names:
        try:
            raw = eks.describe_cluster(name=name)["cluster"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("EKS describe_cluster failed for %s: %s", name, exc)
            continue

        resources_vpc = raw.get("resourcesVpcConfig") or {}
        cluster_vpc = resources_vpc.get("vpcId")
        if vpc_id and cluster_vpc and cluster_vpc != vpc_id:
            continue

        node_groups = _discover_eks_nodegroups_sync(eks, name)
        clusters.append(
            EKSCluster(
                arn=raw.get("arn", f"arn:aws:eks:::{name}"),
                name=name,
                status=raw.get("status", "UNKNOWN"),
                version=raw.get("version"),
                endpoint=raw.get("endpoint"),
                vpc_id=cluster_vpc,
                subnet_ids=resources_vpc.get("subnetIds") or [],
                security_group_ids=resources_vpc.get("securityGroupIds") or [],
                node_groups=node_groups,
                tags=raw.get("tags") or {},
            )
        )
    return clusters


def _discover_eks_nodegroups_sync(eks: Any, cluster_name: str) -> list[EKSNodeGroup]:
    node_groups: list[EKSNodeGroup] = []
    ng_names: list[str] = []
    for page in _paginate(eks, "list_nodegroups", clusterName=cluster_name):
        ng_names.extend(page.get("nodegroups", []))

    for ng_name in ng_names:
        try:
            raw = eks.describe_nodegroup(clusterName=cluster_name, nodegroupName=ng_name)["nodegroup"]
        except Exception as exc:  # noqa: BLE001
            logger.warning("EKS describe_nodegroup failed for %s/%s: %s", cluster_name, ng_name, exc)
            continue
        scaling = raw.get("scalingConfig") or {}
        node_groups.append(
            EKSNodeGroup(
                arn=raw.get("nodegroupArn", f"arn:aws:eks:::{cluster_name}/{ng_name}"),
                name=ng_name,
                cluster_name=cluster_name,
                status=raw.get("status", "UNKNOWN"),
                capacity_type=raw.get("capacityType", "ON_DEMAND"),
                instance_types=raw.get("instanceTypes") or [],
                desired_size=scaling.get("desiredSize", 0),
                min_size=scaling.get("minSize", 0),
                max_size=scaling.get("maxSize", 0),
                tags=raw.get("tags") or {},
            )
        )
    return node_groups


# ---------------------------------------------------------------------------
# RDS discovery
# ---------------------------------------------------------------------------


def _discover_rds_sync(session: boto3.Session, vpc_id: Optional[str]) -> list[RDSInstance]:
    rds = session.client("rds", config=build_botocore_config())
    instances: list[RDSInstance] = []

    for page in _paginate(rds, "describe_db_instances"):
        for raw in page.get("DBInstances", []):
            db_vpc = raw.get("DBSubnetGroup", {}).get("VpcId")
            if vpc_id and db_vpc and db_vpc != vpc_id:
                continue
            endpoint = raw.get("Endpoint") or {}
            sg_ids = [sg["VpcSecurityGroupId"] for sg in raw.get("VpcSecurityGroups", []) if sg.get("VpcSecurityGroupId")]
            tags: dict[str, str] = {}
            try:
                tag_resp = rds.list_tags_for_resource(ResourceName=raw["DBInstanceArn"])
                tags = _tag_dict(tag_resp.get("TagList", []))
            except Exception:  # noqa: BLE001
                pass
            instances.append(
                RDSInstance(
                    arn=raw["DBInstanceArn"],
                    db_instance_id=raw["DBInstanceIdentifier"],
                    db_instance_class=raw.get("DBInstanceClass", ""),
                    engine=raw.get("Engine", ""),
                    engine_version=raw.get("EngineVersion"),
                    status=raw.get("DBInstanceStatus", "unknown"),
                    endpoint_address=endpoint.get("Address"),
                    endpoint_port=endpoint.get("Port"),
                    vpc_id=db_vpc,
                    subnet_group=raw.get("DBSubnetGroup", {}).get("DBSubnetGroupName"),
                    security_group_ids=sg_ids,
                    multi_az=raw.get("MultiAZ", False),
                    publicly_accessible=raw.get("PubliclyAccessible", False),
                    storage_encrypted=raw.get("StorageEncrypted", False),
                    tags=tags,
                )
            )
    return instances


# ---------------------------------------------------------------------------
# Lambda discovery
# ---------------------------------------------------------------------------


def _discover_lambda_sync(session: boto3.Session, vpc_id: Optional[str]) -> list[LambdaFunction]:
    lmb = session.client("lambda", config=build_botocore_config())
    functions: list[LambdaFunction] = []

    for page in _paginate(lmb, "list_functions"):
        for raw in page.get("Functions", []):
            vpc_cfg = raw.get("VpcConfig") or {}
            fn_vpc = vpc_cfg.get("VpcId")
            if vpc_id and fn_vpc and fn_vpc != vpc_id:
                continue
            state = "Unknown"
            try:
                config = lmb.get_function_configuration(FunctionName=raw["FunctionArn"])
                state_detail = config.get("State", "Unknown")
                state = state_detail
            except Exception:  # noqa: BLE001
                pass
            tags: dict[str, str] = {}
            try:
                tags = lmb.list_tags(Resource=raw["FunctionArn"]).get("Tags", {})
            except Exception:  # noqa: BLE001
                pass
            # Collect event source ARNs (e.g. from ALB target group or SQS)
            event_source_arns: list[str] = []
            try:
                for src_page in _paginate(lmb, "list_event_source_mappings", FunctionName=raw["FunctionArn"]):
                    for mapping in src_page.get("EventSourceMappings", []):
                        if mapping.get("EventSourceArn"):
                            event_source_arns.append(mapping["EventSourceArn"])
            except Exception:  # noqa: BLE001
                pass
            functions.append(
                LambdaFunction(
                    arn=raw["FunctionArn"],
                    name=raw["FunctionName"],
                    runtime=raw.get("Runtime"),
                    state=state,
                    last_modified=raw.get("LastModified"),
                    memory_size=raw.get("MemorySize", 128),
                    timeout=raw.get("Timeout", 3),
                    vpc_id=fn_vpc or None,
                    subnet_ids=vpc_cfg.get("SubnetIds") or [],
                    security_group_ids=vpc_cfg.get("SecurityGroupIds") or [],
                    event_source_arns=event_source_arns,
                    tags=tags,
                )
            )
    return functions


# ---------------------------------------------------------------------------
# S3 discovery
# ---------------------------------------------------------------------------


def _discover_s3_sync(session: boto3.Session) -> list[S3Bucket]:
    s3 = session.client("s3", config=build_botocore_config())
    # Only show buckets that belong to the session's region so the graph is
    # scoped to the selected region (S3 is global but buckets have a home region).
    session_region = session.region_name or "us-east-1"
    buckets: list[S3Bucket] = []

    resp = s3.list_buckets()
    for raw in resp.get("Buckets", []):
        name = raw["Name"]
        creation_date = raw.get("CreationDate")
        region = None
        versioning_enabled = False
        public_access_blocked = True
        encryption_enabled = False
        tags: dict[str, str] = {}

        try:
            region = s3.get_bucket_location(Bucket=name).get("LocationConstraint") or "us-east-1"
        except Exception:  # noqa: BLE001
            pass
        # Skip buckets that don't belong to the session's region.
        if region and region != session_region:
            continue
        try:
            v = s3.get_bucket_versioning(Bucket=name)
            versioning_enabled = v.get("Status") == "Enabled"
        except Exception:  # noqa: BLE001
            pass
        try:
            pub = s3.get_public_access_block(Bucket=name).get("PublicAccessBlockConfiguration", {})
            public_access_blocked = all([
                pub.get("BlockPublicAcls", False),
                pub.get("BlockPublicPolicy", False),
                pub.get("IgnorePublicAcls", False),
                pub.get("RestrictPublicBuckets", False),
            ])
        except Exception:  # noqa: BLE001
            pass
        try:
            enc = s3.get_bucket_encryption(Bucket=name)
            encryption_enabled = bool(enc.get("ServerSideEncryptionConfiguration"))
        except Exception:  # noqa: BLE001
            pass
        try:
            tag_resp = s3.get_bucket_tagging(Bucket=name)
            tags = _tag_dict(tag_resp.get("TagSet", []))
        except Exception:  # noqa: BLE001
            pass

        buckets.append(
            S3Bucket(
                name=name,
                arn=f"arn:aws:s3:::{name}",
                region=region,
                creation_date=str(creation_date) if creation_date else None,
                versioning_enabled=versioning_enabled,
                public_access_blocked=public_access_blocked,
                encryption_enabled=encryption_enabled,
                tags=tags,
            )
        )
    return buckets


# ---------------------------------------------------------------------------
# Public async wrappers for new resource types
# ---------------------------------------------------------------------------


async def discover_ecs_clusters(
    vpc_id: Optional[str] = None,
    session: Optional[boto3.Session] = None,
) -> list[ECSCluster]:
    sess = session or get_session()
    return await asyncio.to_thread(_discover_ecs_sync, sess, vpc_id)


async def discover_eks_clusters(
    vpc_id: Optional[str] = None,
    session: Optional[boto3.Session] = None,
) -> list[EKSCluster]:
    sess = session or get_session()
    return await asyncio.to_thread(_discover_eks_sync, sess, vpc_id)


async def discover_rds_instances(
    vpc_id: Optional[str] = None,
    session: Optional[boto3.Session] = None,
) -> list[RDSInstance]:
    sess = session or get_session()
    return await asyncio.to_thread(_discover_rds_sync, sess, vpc_id)


async def discover_lambda_functions(
    vpc_id: Optional[str] = None,
    session: Optional[boto3.Session] = None,
) -> list[LambdaFunction]:
    sess = session or get_session()
    return await asyncio.to_thread(_discover_lambda_sync, sess, vpc_id)


async def discover_s3_buckets(
    session: Optional[boto3.Session] = None,
) -> list[S3Bucket]:
    sess = session or get_session()
    return await asyncio.to_thread(_discover_s3_sync, sess)


# ---------------------------------------------------------------------------
# Public async API — top-level orchestrator that ties everything together.
# ---------------------------------------------------------------------------


async def discover_albs(
    vpc_id: Optional[str] = None,
    tags: Optional[dict[str, str]] = None,
    session: Optional[boto3.Session] = None,
) -> list[LoadBalancer]:
    """Discover ALB/NLB load balancers, optionally filtered by VPC + tags."""
    sess = session or get_session()
    return await asyncio.to_thread(_discover_albs_sync, sess, vpc_id, tags or {})


async def discover_listeners(lb_arn: str, session: Optional[boto3.Session] = None) -> list[Listener]:
    """Discover listeners (and their default+rule target group references)."""
    sess = session or get_session()
    return await asyncio.to_thread(_discover_listeners_sync, sess, lb_arn)


async def discover_target_groups(
    vpc_id: Optional[str] = None,
    session: Optional[boto3.Session] = None,
) -> list[TargetGroup]:
    """Discover target groups + their target health within a VPC."""
    sess = session or get_session()
    return await asyncio.to_thread(_discover_target_groups_sync, sess, vpc_id)


async def discover_security_groups(
    vpc_id: Optional[str] = None,
    resource_ids: Optional[Iterable[str]] = None,
    session: Optional[boto3.Session] = None,
) -> list[SecurityGroup]:
    """Discover security groups, their rules, and what they are attached to."""
    sess = session or get_session()
    only_ids = set(resource_ids) if resource_ids else None
    return await asyncio.to_thread(_discover_security_groups_sync, sess, vpc_id, only_ids)


async def discover_ec2_instances(
    vpc_id: Optional[str] = None,
    instance_ids: Optional[Iterable[str]] = None,
    session: Optional[boto3.Session] = None,
) -> list[Instance]:
    """Discover EC2 instance details."""
    sess = session or get_session()
    only_ids = set(instance_ids) if instance_ids else None
    return await asyncio.to_thread(_discover_instances_sync, sess, vpc_id, only_ids)


async def discover_all(
    vpc_id: Optional[str] = None,
    service_tag: Optional[str] = None,
    profile: Optional[str] = None,
    region: Optional[str] = None,
) -> DiscoveryResult:
    """Run the full discovery pipeline in parallel.

    The discovery happens in two phases:
      Phase 1: ALBs + target groups + security groups + instances (independent)
      Phase 2: listeners (one call per LB) — needed for orphan detection
              instance → target_group cross-reference (built locally)
    """
    vpc_id = vpc_id if isinstance(vpc_id, str) else None
    service_tag = service_tag if isinstance(service_tag, str) else None
    profile = profile if isinstance(profile, str) else None
    region = region if isinstance(region, str) else None
    sess = get_session(profile=profile, region=region)
    identity = await asyncio.to_thread(whoami, sess)
    tag_filter = _parse_service_tag(service_tag)

    albs, tgs, sgs, ecs_clusters, eks_clusters, rds_instances, lambda_functions, s3_buckets = (
        await asyncio.gather(
            discover_albs(vpc_id, tag_filter, sess),
            discover_target_groups(vpc_id, sess),
            discover_security_groups(vpc_id, session=sess),
            discover_ecs_clusters(vpc_id, sess),
            discover_eks_clusters(vpc_id, sess),
            discover_rds_instances(vpc_id, sess),
            discover_lambda_functions(vpc_id, sess),
            discover_s3_buckets(sess),
            return_exceptions=True,
        )
    )

    # If any discovery call raised, log it and substitute an empty list.
    def _unwrap(result: Any, label: str) -> list:
        if isinstance(result, BaseException):
            logger.warning("Discovery failed for %s: %s", label, result)
            return []
        return result  # type: ignore[return-value]

    albs = _unwrap(albs, "ALBs")
    tgs = _unwrap(tgs, "target_groups")
    sgs = _unwrap(sgs, "security_groups")
    ecs_clusters = _unwrap(ecs_clusters, "ECS")
    eks_clusters = _unwrap(eks_clusters, "EKS")
    rds_instances = _unwrap(rds_instances, "RDS")
    lambda_functions = _unwrap(lambda_functions, "Lambda")
    s3_buckets = _unwrap(s3_buckets, "S3")

    # Phase 2: listeners per ALB, in parallel.
    listener_groups = await asyncio.gather(*[discover_listeners(lb.arn, sess) for lb in albs])
    for lb, listeners in zip(albs, listener_groups):
        lb.listeners = listeners

    # Cross-link target_group -> lb via listener references (more reliable than
    # describe_target_groups' LoadBalancerArns field, which lags for new TGs).
    tg_to_lbs: dict[str, set[str]] = {tg.arn: set(tg.associated_lb_arns) for tg in tgs}
    for lb in albs:
        for lst in lb.listeners:
            for arn in (*lst.default_target_group_arns, *lst.rule_target_group_arns):
                tg_to_lbs.setdefault(arn, set()).add(lb.arn)
    for tg in tgs:
        tg.associated_lb_arns = sorted(tg_to_lbs.get(tg.arn, set()))

    # Pull instance IDs from healthy and unhealthy targets.
    target_instance_ids: set[str] = {
        t.target_id
        for tg in tgs
        for t in tg.targets
        if tg.targets and tg.targets[0].target_type == "instance"
    }
    instances = await discover_ec2_instances(vpc_id, instance_ids=target_instance_ids or None, session=sess)

    # If we filtered by tag, also pull any tagged instances even without target group membership.
    if tag_filter:
        tagged_instances = await discover_ec2_instances(vpc_id=vpc_id, session=sess)
        existing = {i.instance_id for i in instances}
        for inst in tagged_instances:
            matches = all(inst.tags.get(k) == v for k, v in tag_filter.items())
            if matches and inst.instance_id not in existing:
                instances.append(inst)

    # Reverse-link instance -> target group memberships.
    inst_to_tgs: dict[str, set[str]] = {}
    for tg in tgs:
        for t in tg.targets:
            if t.target_type == "instance":
                inst_to_tgs.setdefault(t.target_id, set()).add(tg.arn)
    for inst in instances:
        inst.target_group_arns = sorted(inst_to_tgs.get(inst.instance_id, set()))

    # Cross-link ECS services → target groups (via loadBalancers config).
    tg_arn_set = {tg.arn for tg in tgs}
    for cluster in ecs_clusters:
        for svc in cluster.services:
            svc.target_group_arns = [a for a in svc.target_group_arns if a in tg_arn_set]

    return DiscoveryResult(
        provider=CloudProvider.AWS,
        account_id=identity.account_id,
        region=identity.region,
        vpc_id=vpc_id,
        load_balancers=albs,
        target_groups=tgs,
        instances=instances,
        security_groups=sgs,
        ecs_clusters=ecs_clusters,
        eks_clusters=eks_clusters,
        rds_instances=rds_instances,
        lambda_functions=lambda_functions,
        s3_buckets=s3_buckets,
    )


def _parse_service_tag(service_tag: Optional[str]) -> dict[str, str]:
    """Parse `--service-tag Foo=Bar` style input.

    Accepts either a single 'k=v' string or 'k1=v1,k2=v2'. Returns {} if empty.
    """
    if not service_tag or not isinstance(service_tag, str):
        return {}
    out: dict[str, str] = {}
    for piece in service_tag.split(","):
        if "=" not in piece:
            continue
        k, v = piece.split("=", 1)
        out[k.strip()] = v.strip()
    return out
