"""Live GCP discovery via google-cloud client libraries.

Discovery is parallelized using ``asyncio.to_thread`` — the google-cloud
libraries are synchronous, but the GIL is released during network I/O, so a
thread pool gives near-linear speedups on multi-resource workloads.

All paginated APIs are fully drained. Retry/backoff is delegated to the
google-cloud libraries' built-in retry configuration.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from sudiviz.discovery.models import (
    CloudProvider,
    DiscoveryResult,
    EKSCluster,
    EKSNodeGroup,
    HealthStatus,
    Instance,
    LambdaFunction,
    LoadBalancer,
    RDSInstance,
    S3Bucket,
    SecurityGroup,
    SecurityGroupRule,
    TargetGroup,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Lazy-import guard — give a clear error when google-cloud SDKs are absent.
# ---------------------------------------------------------------------------

_GCP_AVAILABLE = True
_GCP_IMPORT_ERROR: Optional[str] = None

try:
    from google.cloud import compute_v1  # type: ignore[import-untyped]
    from google.cloud import container_v1  # type: ignore[import-untyped]
    from google.cloud import storage  # type: ignore[import-untyped]
    from google.cloud import functions_v2  # type: ignore[import-untyped]
except ImportError:
    _GCP_AVAILABLE = False
    _GCP_IMPORT_ERROR = (
        "google-cloud SDK is not installed. Install the GCP extras:\n"
        "  pip install sudiviz[gcp]"
    )

# Cloud SQL uses the API client rather than a dedicated SDK package.
try:
    from googleapiclient import discovery as gapi_discovery  # type: ignore[import-untyped]
except ImportError:
    gapi_discovery = None  # type: ignore[assignment]


def _require_gcp() -> None:
    if not _GCP_AVAILABLE:
        raise RuntimeError(_GCP_IMPORT_ERROR)


def _label_dict(labels: Optional[dict]) -> dict[str, str]:
    """Normalize GCP labels (already key:value dicts, but may be None)."""
    return dict(labels) if labels else {}


# ---------------------------------------------------------------------------
# Synchronous primitives (one per resource type) — wrapped in async below.
# ---------------------------------------------------------------------------


def _discover_compute_instances_sync(
    project: str,
    region: str,
    network: Optional[str],
) -> list[Instance]:
    """Discover Compute Engine VMs in all zones of the given region."""
    client = compute_v1.InstancesClient()
    instances: list[Instance] = []

    # AggregatedList returns instances across all zones; we filter to region.
    request = compute_v1.AggregatedListInstancesRequest(
        project=project,
        filter=f'zone:"{region}-*"' if region else None,
    )
    agg = client.aggregated_list(request=request)

    for zone_key, scoped_list in agg:
        if not scoped_list.instances:
            continue
        for inst in scoped_list.instances:
            # Network filter: check if any NIC is on the target network.
            if network:
                matched = any(
                    nic.network and nic.network.endswith(f"/{network}")
                    for nic in (inst.network_interfaces or [])
                )
                if not matched:
                    continue

            # Map GCP status to normalized state.
            state_map = {
                "RUNNING": "running",
                "STOPPED": "stopped",
                "TERMINATED": "terminated",
                "SUSPENDED": "suspended",
                "STAGING": "pending",
                "PROVISIONING": "pending",
            }
            state = state_map.get(inst.status, inst.status.lower() if inst.status else "unknown")

            # Extract IPs.
            private_ip: Optional[str] = None
            public_ip: Optional[str] = None
            sg_ids: list[str] = []
            network_name: Optional[str] = None
            subnet_name: Optional[str] = None

            for nic in inst.network_interfaces or []:
                if not private_ip:
                    private_ip = nic.network_i_p
                if not network_name and nic.network:
                    network_name = nic.network.rsplit("/", 1)[-1]
                if not subnet_name and nic.subnetwork:
                    subnet_name = nic.subnetwork.rsplit("/", 1)[-1]
                for ac in nic.access_configs or []:
                    if ac.nat_i_p:
                        public_ip = ac.nat_i_p
                        break

            # Machine type: extract the short name from the full URL.
            machine_type = inst.machine_type.rsplit("/", 1)[-1] if inst.machine_type else None

            instances.append(
                Instance(
                    provider=CloudProvider.GCP,
                    instance_id=str(inst.id),
                    instance_type=machine_type,
                    state=state,
                    private_ip=private_ip,
                    public_ip=public_ip,
                    vpc_id=network_name,
                    subnet_id=subnet_name,
                    security_group_ids=sg_ids,
                    tags=_label_dict(inst.labels),
                )
            )

    return instances


def _discover_load_balancers_sync(
    project: str,
    region: str,
) -> list[LoadBalancer]:
    """Discover Cloud Load Balancers via forwarding rules."""
    client = compute_v1.ForwardingRulesClient()
    lbs: list[LoadBalancer] = []

    # Global forwarding rules (external HTTP(S) LBs).
    try:
        global_client = compute_v1.GlobalForwardingRulesClient()
        request = compute_v1.ListGlobalForwardingRulesRequest(project=project)
        for rule in global_client.list(request=request):
            scheme = "internet-facing" if rule.load_balancing_scheme in (
                "EXTERNAL", "EXTERNAL_MANAGED",
            ) else "internal"
            lbs.append(
                LoadBalancer(
                    provider=CloudProvider.GCP,
                    arn=rule.self_link or f"projects/{project}/global/forwardingRules/{rule.name}",
                    name=rule.name,
                    dns_name=rule.i_p_address if rule.i_p_address else None,
                    scheme=scheme,
                    type="application",
                    vpc_id=rule.network.rsplit("/", 1)[-1] if rule.network else None,
                    state="active",
                    security_group_ids=[],
                    subnet_ids=[],
                    tags={},
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Global forwarding rules discovery failed: %s", exc)

    # Regional forwarding rules.
    try:
        request = compute_v1.ListForwardingRulesRequest(project=project, region=region)
        for rule in client.list(request=request):
            scheme = "internet-facing" if rule.load_balancing_scheme in (
                "EXTERNAL", "EXTERNAL_MANAGED",
            ) else "internal"
            lb_type = "network" if rule.load_balancing_scheme in (
                "EXTERNAL", "INTERNAL",
            ) else "application"
            lbs.append(
                LoadBalancer(
                    provider=CloudProvider.GCP,
                    arn=rule.self_link or f"projects/{project}/regions/{region}/forwardingRules/{rule.name}",
                    name=rule.name,
                    dns_name=rule.i_p_address if rule.i_p_address else None,
                    scheme=scheme,
                    type=lb_type,
                    vpc_id=rule.network.rsplit("/", 1)[-1] if rule.network else None,
                    state="active",
                    security_group_ids=[],
                    subnet_ids=[rule.subnetwork.rsplit("/", 1)[-1]] if rule.subnetwork else [],
                    tags={},
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Regional forwarding rules discovery failed: %s", exc)

    return lbs


def _discover_instance_groups_sync(
    project: str,
    region: str,
) -> list[TargetGroup]:
    """Discover managed and unmanaged instance groups as target groups."""
    tgs: list[TargetGroup] = []

    # Managed Instance Groups (MIGs) — regional.
    try:
        mig_client = compute_v1.RegionInstanceGroupManagersClient()
        request = compute_v1.ListRegionInstanceGroupManagersRequest(
            project=project, region=region,
        )
        for mig in mig_client.list(request=request):
            tgs.append(
                TargetGroup(
                    provider=CloudProvider.GCP,
                    arn=mig.self_link or f"projects/{project}/regions/{region}/instanceGroupManagers/{mig.name}",
                    name=mig.name,
                    protocol="TCP",
                    port=0,
                    vpc_id=None,
                    targets=[],
                    associated_lb_arns=[mig.target_pools[0]] if mig.target_pools else [],
                    tags={},
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Regional MIG discovery failed: %s", exc)

    # Zonal MIGs.
    try:
        zonal_client = compute_v1.InstanceGroupManagersClient()
        request = compute_v1.AggregatedListInstanceGroupManagersRequest(
            project=project,
            filter=f'zone:"{region}-*"' if region else None,
        )
        for zone_key, scoped_list in zonal_client.aggregated_list(request=request):
            for mig in scoped_list.instance_group_managers or []:
                tgs.append(
                    TargetGroup(
                        provider=CloudProvider.GCP,
                        arn=mig.self_link or f"{zone_key}/instanceGroupManagers/{mig.name}",
                        name=mig.name,
                        protocol="TCP",
                        port=0,
                        vpc_id=None,
                        targets=[],
                        associated_lb_arns=[mig.target_pools[0]] if mig.target_pools else [],
                        tags={},
                    )
                )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Zonal MIG discovery failed: %s", exc)

    return tgs


def _discover_firewall_rules_sync(
    project: str,
    network: Optional[str],
) -> list[SecurityGroup]:
    """Discover VPC firewall rules as security groups."""
    client = compute_v1.FirewallsClient()
    sgs: list[SecurityGroup] = []

    request = compute_v1.ListFirewallsRequest(project=project)
    for fw in client.list(request=request):
        # Filter by network if specified.
        fw_network = fw.network.rsplit("/", 1)[-1] if fw.network else None
        if network and fw_network != network:
            continue

        rules: list[SecurityGroupRule] = []

        # Ingress rules (allowed).
        if fw.direction == "INGRESS":
            for allowed in fw.allowed or []:
                ports = []
                for port_range in allowed.ports or []:
                    if "-" in port_range:
                        start, end = port_range.split("-", 1)
                        ports.append((int(start), int(end)))
                    else:
                        ports.append((int(port_range), int(port_range)))
                cidrs = list(fw.source_ranges or [])
                ref_sgs = list(fw.source_tags or [])
                if ports:
                    for from_port, to_port in ports:
                        rules.append(
                            SecurityGroupRule(
                                direction="ingress",
                                protocol=allowed.I_p_protocol or "-1",
                                from_port=from_port,
                                to_port=to_port,
                                cidr_ranges=cidrs,
                                referenced_sg_ids=ref_sgs,
                                description=fw.description,
                            )
                        )
                else:
                    rules.append(
                        SecurityGroupRule(
                            direction="ingress",
                            protocol=allowed.I_p_protocol or "-1",
                            from_port=None,
                            to_port=None,
                            cidr_ranges=cidrs,
                            referenced_sg_ids=ref_sgs,
                            description=fw.description,
                        )
                    )

        # Egress rules (allowed).
        if fw.direction == "EGRESS":
            for allowed in fw.allowed or []:
                ports = []
                for port_range in allowed.ports or []:
                    if "-" in port_range:
                        start, end = port_range.split("-", 1)
                        ports.append((int(start), int(end)))
                    else:
                        ports.append((int(port_range), int(port_range)))
                cidrs = list(fw.destination_ranges or [])
                if ports:
                    for from_port, to_port in ports:
                        rules.append(
                            SecurityGroupRule(
                                direction="egress",
                                protocol=allowed.I_p_protocol or "-1",
                                from_port=from_port,
                                to_port=to_port,
                                cidr_ranges=cidrs,
                                referenced_sg_ids=[],
                                description=fw.description,
                            )
                        )
                else:
                    rules.append(
                        SecurityGroupRule(
                            direction="egress",
                            protocol=allowed.I_p_protocol or "-1",
                            from_port=None,
                            to_port=None,
                            cidr_ranges=cidrs,
                            referenced_sg_ids=[],
                            description=fw.description,
                        )
                    )

        # Determine which instances this rule is attached to via target tags.
        attached = list(fw.target_tags or [])

        sgs.append(
            SecurityGroup(
                provider=CloudProvider.GCP,
                sg_id=fw.name,
                name=fw.name,
                vpc_id=fw_network,
                rules=rules,
                attached_to=attached,
                tags={},
            )
        )

    return sgs


def _discover_gke_sync(
    project: str,
    region: str,
) -> list[EKSCluster]:
    """Discover GKE clusters and their node pools."""
    client = container_v1.ClusterManagerClient()
    clusters: list[EKSCluster] = []

    # List clusters in the region (location = region for regional, zone for zonal).
    parent = f"projects/{project}/locations/{region}"
    try:
        response = client.list_clusters(parent=parent)
    except Exception as exc:  # noqa: BLE001
        logger.warning("GKE cluster listing failed for %s: %s", region, exc)
        # Also try "-" to list all locations and filter.
        try:
            response = client.list_clusters(parent=f"projects/{project}/locations/-")
        except Exception as exc2:  # noqa: BLE001
            logger.warning("GKE fallback listing failed: %s", exc2)
            return clusters

    for cluster in response.clusters or []:
        # Filter by region: cluster.location can be a zone (us-central1-a)
        # or a region (us-central1).
        if region and not cluster.location.startswith(region):
            continue

        status_map = {
            0: "UNKNOWN",      # STATUS_UNSPECIFIED
            1: "PROVISIONING", # PROVISIONING
            2: "ACTIVE",       # RUNNING
            3: "RECONCILING",  # RECONCILING
            4: "STOPPING",     # STOPPING
            5: "ERROR",        # ERROR
            6: "DEGRADED",     # DEGRADED
        }
        status = status_map.get(cluster.status, "UNKNOWN")

        node_groups: list[EKSNodeGroup] = []
        for pool in cluster.node_pools or []:
            pool_status_map = {
                0: "UNKNOWN",
                1: "PROVISIONING",
                2: "ACTIVE",
                3: "RECONCILING",
                4: "STOPPING",
                5: "ERROR",
                6: "RUNNING_WITH_ERROR",
            }
            pool_status = pool_status_map.get(pool.status, "UNKNOWN")
            autoscaling = pool.autoscaling
            node_groups.append(
                EKSNodeGroup(
                    provider=CloudProvider.GCP,
                    arn=pool.self_link or f"{parent}/clusters/{cluster.name}/nodePools/{pool.name}",
                    name=pool.name,
                    cluster_name=cluster.name,
                    status=pool_status,
                    capacity_type="PREEMPTIBLE" if (pool.config and pool.config.preemptible) else "ON_DEMAND",
                    instance_types=[pool.config.machine_type] if pool.config and pool.config.machine_type else [],
                    desired_size=pool.initial_node_count or 0,
                    min_size=autoscaling.min_node_count if autoscaling else 0,
                    max_size=autoscaling.max_node_count if autoscaling else 0,
                    tags=_label_dict(pool.config.labels if pool.config else None),
                )
            )

        clusters.append(
            EKSCluster(
                provider=CloudProvider.GCP,
                arn=cluster.self_link or f"{parent}/clusters/{cluster.name}",
                name=cluster.name,
                status=status,
                version=cluster.current_master_version,
                endpoint=cluster.endpoint,
                vpc_id=cluster.network,
                subnet_ids=[cluster.subnetwork] if cluster.subnetwork else [],
                security_group_ids=[],
                node_groups=node_groups,
                tags=_label_dict(cluster.resource_labels),
            )
        )

    return clusters


def _discover_cloud_sql_sync(project: str) -> list[RDSInstance]:
    """Discover Cloud SQL instances."""
    if gapi_discovery is None:
        logger.warning("googleapiclient not installed — skipping Cloud SQL discovery")
        return []

    instances: list[RDSInstance] = []
    try:
        service = gapi_discovery.build("sqladmin", "v1beta4")
        request = service.instances().list(project=project)
        while request is not None:
            response = request.execute()
            for inst in response.get("items", []):
                settings = inst.get("settings", {})
                ip_addresses = inst.get("ipAddresses", [])
                primary_ip = next(
                    (addr["ipAddress"] for addr in ip_addresses if addr.get("type") == "PRIMARY"),
                    None,
                )
                sg_ids: list[str] = []
                for net in settings.get("ipConfiguration", {}).get("authorizedNetworks", []):
                    if net.get("value"):
                        sg_ids.append(net["value"])

                instances.append(
                    RDSInstance(
                        provider=CloudProvider.GCP,
                        arn=inst.get("selfLink", f"projects/{project}/instances/{inst['name']}"),
                        db_instance_id=inst["name"],
                        db_instance_class=settings.get("tier", ""),
                        engine=inst.get("databaseVersion", ""),
                        engine_version=inst.get("databaseVersion"),
                        status=inst.get("state", "unknown").lower(),
                        endpoint_address=primary_ip,
                        endpoint_port=3306 if "MYSQL" in inst.get("databaseVersion", "").upper() else 5432,
                        vpc_id=settings.get("ipConfiguration", {}).get("privateNetwork", "").rsplit("/", 1)[-1] or None,
                        subnet_group=None,
                        security_group_ids=sg_ids,
                        multi_az=settings.get("availabilityType") == "REGIONAL",
                        publicly_accessible=settings.get("ipConfiguration", {}).get("ipv4Enabled", False),
                        storage_encrypted=settings.get("dataDiskType", "").startswith("PD_SSD"),
                        tags=_label_dict(settings.get("userLabels")),
                    )
                )
            request = service.instances().list_next(previous_request=request, previous_response=response)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cloud SQL discovery failed: %s", exc)

    return instances


def _discover_cloud_functions_sync(
    project: str,
    region: str,
) -> list[LambdaFunction]:
    """Discover Cloud Functions (v2)."""
    functions: list[LambdaFunction] = []
    try:
        client = functions_v2.FunctionServiceClient()
        parent = f"projects/{project}/locations/{region}"
        request = functions_v2.ListFunctionsRequest(parent=parent)

        for fn in client.list_functions(request=request):
            state_map = {
                0: "Unknown",     # STATE_UNSPECIFIED
                1: "Active",      # ACTIVE
                2: "Failed",      # FAILED
                3: "Deploying",   # DEPLOYING
                4: "Deleting",    # DELETING
                5: "Unknown",     # UNKNOWN
            }
            state = state_map.get(fn.state, "Unknown")

            # Extract runtime and config from service config.
            runtime = None
            memory_size = 256
            timeout = 60
            vpc_id = None
            subnet_ids: list[str] = []

            if fn.build_config:
                runtime = fn.build_config.runtime

            if fn.service_config:
                memory_mb = fn.service_config.available_memory
                if memory_mb:
                    # Memory comes as a string like "256M" or "256Mi"
                    try:
                        memory_size = int("".join(c for c in str(memory_mb) if c.isdigit()) or "256")
                    except (ValueError, TypeError):
                        memory_size = 256
                timeout_seconds = fn.service_config.timeout_seconds
                if timeout_seconds:
                    timeout = timeout_seconds
                if fn.service_config.vpc_connector:
                    vpc_id = fn.service_config.vpc_connector.rsplit("/", 1)[-1]

            functions.append(
                LambdaFunction(
                    provider=CloudProvider.GCP,
                    arn=fn.name or f"{parent}/functions/{fn.name}",
                    name=fn.name.rsplit("/", 1)[-1] if fn.name else "",
                    runtime=runtime,
                    state=state,
                    last_modified=fn.update_time.isoformat() if fn.update_time else None,
                    memory_size=memory_size,
                    timeout=timeout,
                    vpc_id=vpc_id,
                    subnet_ids=subnet_ids,
                    security_group_ids=[],
                    event_source_arns=[],
                    tags=_label_dict(fn.labels),
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cloud Functions discovery failed: %s", exc)

    return functions


def _discover_gcs_sync(project: str) -> list[S3Bucket]:
    """Discover Cloud Storage buckets."""
    buckets: list[S3Bucket] = []
    try:
        client = storage.Client(project=project)
        for bucket in client.list_buckets():
            versioning = bucket.versioning_enabled or False
            # GCS uses uniform bucket-level access (UBLA) as the equivalent of
            # public access block.
            iam_config = bucket.iam_configuration or {}
            ubla = getattr(iam_config, "uniform_bucket_level_access_enabled", False)
            # GCS buckets have default encryption; check for CMEK.
            encryption = bool(bucket.default_kms_key_name)

            buckets.append(
                S3Bucket(
                    provider=CloudProvider.GCP,
                    name=bucket.name,
                    arn=f"gs://{bucket.name}",
                    region=bucket.location.lower() if bucket.location else None,
                    creation_date=bucket.time_created.isoformat() if bucket.time_created else None,
                    versioning_enabled=versioning,
                    public_access_blocked=ubla,
                    encryption_enabled=encryption,
                    tags=_label_dict(bucket.labels),
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cloud Storage discovery failed: %s", exc)

    return buckets


# ---------------------------------------------------------------------------
# Public async wrappers
# ---------------------------------------------------------------------------


async def discover_gcp_instances(
    project: str,
    region: str,
    network: Optional[str] = None,
) -> list[Instance]:
    return await asyncio.to_thread(_discover_compute_instances_sync, project, region, network)


async def discover_gcp_load_balancers(
    project: str,
    region: str,
) -> list[LoadBalancer]:
    return await asyncio.to_thread(_discover_load_balancers_sync, project, region)


async def discover_gcp_instance_groups(
    project: str,
    region: str,
) -> list[TargetGroup]:
    return await asyncio.to_thread(_discover_instance_groups_sync, project, region)


async def discover_gcp_firewall_rules(
    project: str,
    network: Optional[str] = None,
) -> list[SecurityGroup]:
    return await asyncio.to_thread(_discover_firewall_rules_sync, project, network)


async def discover_gke_clusters(
    project: str,
    region: str,
) -> list[EKSCluster]:
    return await asyncio.to_thread(_discover_gke_sync, project, region)


async def discover_cloud_sql(project: str) -> list[RDSInstance]:
    return await asyncio.to_thread(_discover_cloud_sql_sync, project)


async def discover_cloud_functions(
    project: str,
    region: str,
) -> list[LambdaFunction]:
    return await asyncio.to_thread(_discover_cloud_functions_sync, project, region)


async def discover_gcs_buckets(project: str) -> list[S3Bucket]:
    return await asyncio.to_thread(_discover_gcs_sync, project)


# ---------------------------------------------------------------------------
# Top-level orchestrator
# ---------------------------------------------------------------------------


async def discover_all_gcp(
    project: Optional[str] = None,
    region: Optional[str] = None,
    vpc_network: Optional[str] = None,
    service_tag: Optional[str] = None,
) -> DiscoveryResult:
    """Run the full GCP discovery pipeline in parallel.

    Parameters
    ----------
    project : str, optional
        GCP project ID. Resolved from env/ADC if omitted.
    region : str, optional
        GCP region (e.g. ``us-central1``). Defaults to ``us-central1``.
    vpc_network : str, optional
        Filter resources to a specific VPC network name.
    service_tag : str, optional
        Label filter in ``key=value`` format.
    """
    _require_gcp()
    from sudiviz.utils.gcp_auth import gcp_whoami, get_gcp_project_id

    resolved_project = get_gcp_project_id(project)
    resolved_region = region or "us-central1"
    identity = gcp_whoami(project=resolved_project, region=resolved_region)

    (
        instances,
        load_balancers,
        instance_groups,
        firewall_rules,
        gke_clusters,
        cloud_sql_instances,
        cloud_functions,
        gcs_buckets,
    ) = await asyncio.gather(
        discover_gcp_instances(resolved_project, resolved_region, vpc_network),
        discover_gcp_load_balancers(resolved_project, resolved_region),
        discover_gcp_instance_groups(resolved_project, resolved_region),
        discover_gcp_firewall_rules(resolved_project, vpc_network),
        discover_gke_clusters(resolved_project, resolved_region),
        discover_cloud_sql(resolved_project),
        discover_cloud_functions(resolved_project, resolved_region),
        discover_gcs_buckets(resolved_project),
        return_exceptions=True,
    )

    # Unwrap exceptions — log and substitute empty lists.
    def _unwrap(result: Any, label: str) -> list:
        if isinstance(result, BaseException):
            logger.warning("GCP discovery failed for %s: %s", label, result)
            return []
        return result  # type: ignore[return-value]

    instances = _unwrap(instances, "Compute Engine")
    load_balancers = _unwrap(load_balancers, "Load Balancers")
    instance_groups = _unwrap(instance_groups, "Instance Groups")
    firewall_rules = _unwrap(firewall_rules, "Firewall Rules")
    gke_clusters = _unwrap(gke_clusters, "GKE")
    cloud_sql_instances = _unwrap(cloud_sql_instances, "Cloud SQL")
    cloud_functions = _unwrap(cloud_functions, "Cloud Functions")
    gcs_buckets = _unwrap(gcs_buckets, "Cloud Storage")

    # Apply label (tag) filter if specified.
    if service_tag:
        tag_filter = _parse_service_tag(service_tag)
        if tag_filter:
            instances = [i for i in instances if _matches_labels(i.tags, tag_filter)]
            load_balancers = [lb for lb in load_balancers if _matches_labels(lb.tags, tag_filter)]
            instance_groups = [tg for tg in instance_groups if _matches_labels(tg.tags, tag_filter)]
            gke_clusters = [c for c in gke_clusters if _matches_labels(c.tags, tag_filter)]
            cloud_sql_instances = [db for db in cloud_sql_instances if _matches_labels(db.tags, tag_filter)]
            cloud_functions = [fn for fn in cloud_functions if _matches_labels(fn.tags, tag_filter)]
            gcs_buckets = [b for b in gcs_buckets if _matches_labels(b.tags, tag_filter)]

    return DiscoveryResult(
        provider=CloudProvider.GCP,
        account_id=identity.account_email,
        project_id=identity.project_id,
        region=identity.region,
        vpc_id=vpc_network,
        load_balancers=load_balancers,
        target_groups=instance_groups,
        instances=instances,
        security_groups=firewall_rules,
        eks_clusters=gke_clusters,
        rds_instances=cloud_sql_instances,
        lambda_functions=cloud_functions,
        s3_buckets=gcs_buckets,
    )


def _parse_service_tag(service_tag: Optional[str]) -> dict[str, str]:
    """Parse ``--service-tag key=value,key2=value2``."""
    if not service_tag or not isinstance(service_tag, str):
        return {}
    out: dict[str, str] = {}
    for piece in service_tag.split(","):
        if "=" not in piece:
            continue
        k, v = piece.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _matches_labels(labels: dict[str, str], filter_labels: dict[str, str]) -> bool:
    """Check whether all key=value pairs in ``filter_labels`` are present."""
    if not filter_labels:
        return True
    return all(labels.get(k) == v for k, v in filter_labels.items())
