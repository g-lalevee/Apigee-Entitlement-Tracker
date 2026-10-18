"""CSV & Google Sheets Ready Exporter for Apigee Entitlement Tracker.

Generates structured CSV reports designed for direct import into Google Sheets or Excel:
  1. `organizations` - Executive summary per Apigee Organization (X & Hybrid) + Cross-Org Entitlement
  2. `environments`  - Granular breakdown per (Organization, Environment) with region multipliers
  3. `deployments`   - Full inventory of deployed API Proxies (Standard/Extensible) & Shared Flows
  4. `history`       - Historical audit log of PDU scans over time
  5. `bundle`        - ZIP archive containing all CSV tabs ready for multi-tab Google Sheets import
"""

from __future__ import annotations

import csv
import datetime
import io
import zipfile
from collections.abc import Mapping, Sequence
from typing import Any

__all__ = [
    "DEPLOYMENTS_CSV_HEADER",
    "ENVIRONMENTS_CSV_HEADER",
    "HISTORY_CSV_HEADER",
    "ORGANIZATIONS_CSV_HEADER",
    "generate_deployments_csv",
    "generate_environments_csv",
    "generate_history_csv",
    "generate_organizations_csv",
    "generate_zip_bundle",
]

ORGANIZATIONS_CSV_HEADER: tuple[str, ...] = (
    "Organization",
    "GCP Project ID",
    "Footprint (X / Hybrid)",
    "Runtime Type",
    "Billing Type",
    "Subscription Plan",
    "Environments Count",
    "Active Regions Count",
    "Regions List",
    "Total Proxies Created",
    "Deployed API Proxies",
    "Deployed Shared Flows",
    "Standard Proxy PDUs",
    "Extensible Proxy PDUs",
    "Shared Flow PDUs",
    "Total Organization PDUs",
    "Cross-Org Entitlement Limit",
    "% of Global Entitlement",
    "% of Total Used PDUs",
    "Status",
)

ENVIRONMENTS_CSV_HEADER: tuple[str, ...] = (
    "Export date",
    "Organization",
    "Runtime",
    "Environment",
    "Regions",
    "Env Units (Regions)",
    "% of Env Limit",
    "Deployed Units (Proxies + SF)",
    "Consumed PDUs",
    "% of PDU Limit",
    "Standard Calls",
    "Extensible Calls",
    "Raw Total Calls",
    "Entitlement Calls (Weighted)",
    "% of call Limit",
)

DEPLOYMENTS_CSV_HEADER: tuple[str, ...] = (
    "Organization",
    "GCP Project ID",
    "Footprint (X / Hybrid)",
    "Environment",
    "Artifact Kind",
    "Proxy / Shared Flow Name",
    "Deployed Revision",
    "Deployment Type",
    "Regions Count (Multiplier)",
    "Regions / Instances",
    "Consumed PDUs",
    "Deployed Timestamp (UTC)",
    "Service Account",
)

HISTORY_CSV_HEADER: tuple[str, ...] = (
    "Snapshot Timestamp (UTC)",
    "Data Source",
    "Organizations Count",
    "Apigee X Orgs",
    "Apigee Hybrid Orgs",
    "Environments Count",
    "Deployed API Proxies",
    "Deployed Shared Flows",
    "Apigee X PDUs",
    "Apigee Hybrid PDUs",
    "Standard Proxy PDUs",
    "Extensible Proxy PDUs",
    "Shared Flow PDUs",
    "Total Cross-Org PDUs",
    "Cross-Org Entitlement Limit",
    "Utilization %",
    "Remaining PDUs",
    "Overage PDUs",
    "Status",
)


def _format_epoch_ms(ms_val: Any) -> str:
    """Convert epoch milliseconds string or int to ISO-8601 UTC timestamp string.

    Args:
        ms_val: Epoch timestamp in milliseconds (numeric or string representation).

    Returns:
        Formatted UTC timestamp string '%Y-%m-%d %H:%M:%S UTC' or empty string.
    """
    if not ms_val:
        return ""
    try:
        ts_sec = int(ms_val) / 1000.0
        return datetime.datetime.fromtimestamp(ts_sec, tz=datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    except (ValueError, TypeError, OverflowError):
        return str(ms_val)


def _build_org_summary_row(org: Mapping[str, Any], entitlement: int | float) -> list[Any]:
    """Build a CSV row representation for a single Apigee organization summary."""
    rt = org.get("runtimeType", "CLOUD")
    footprint_label = "Apigee Hybrid" if rt == "HYBRID" else "Apigee X (Cloud)"
    unique_regions: Sequence[str] = org.get("uniqueRegions", ())
    return [
        org.get("organization", ""),
        org.get("projectId", ""),
        footprint_label,
        rt,
        org.get("billingType", ""),
        org.get("subscriptionPlan", ""),
        org.get("environmentCount", 0),
        org.get("uniqueRegionsCount", 0),
        ", ".join(unique_regions),
        org.get("totalProxiesCreated", 0),
        org.get("deployedProxiesCount", 0),
        org.get("deployedSharedFlowsCount", 0),
        org.get("standardProxyPdus", 0),
        org.get("extensibleProxyPdus", 0),
        org.get("sharedFlowPdus", 0),
        org.get("totalPdus", 0),
        entitlement,
        f"{org.get('entitlementSharePct', 0)}%",
        f"{org.get('portfolioSharePct', 0)}%",
        org.get("status", "OK"),
    ]


def _build_portfolio_total_row(summary: Mapping[str, Any], entitlement: int | float) -> list[Any]:
    """Build the cross-organization global summary total row."""
    total_orgs = summary.get("totalOrganizations", 0)
    x_orgs = summary.get("apigeeXOrgs", 0)
    hybrid_orgs = summary.get("apigeeHybridOrgs", 0)
    overage = summary.get("overagePdus", 0)
    packs = summary.get("recommendedAddOnPacks50", 0)

    return [
        "TOTAL CROSS-ORGANIZATION",
        f"{total_orgs} Orgs ({x_orgs} X, {hybrid_orgs} Hybrid)",
        "All (X + Hybrid)",
        "ALL",
        "CROSS-ORG",
        f"Overage: {overage} PDUs ({packs} x 50-PDU Packs)",
        summary.get("totalEnvironments", 0),
        summary.get("totalActiveEnvRegionAttachments", 0),
        f"Apigee X PDUs: {summary.get('apigeeXPdus', 0)} | Hybrid PDUs: {summary.get('apigeeHybridPdus', 0)}",
        "",
        summary.get("totalDeployedProxies", 0),
        summary.get("totalDeployedSharedFlows", 0),
        summary.get("standardProxyPdus", 0),
        summary.get("extensibleProxyPdus", 0),
        summary.get("sharedFlowPdus", 0),
        summary.get("totalPdus", 0),
        entitlement,
        f"{summary.get('utilizationPct', 0)}%",
        "100.0%",
        summary.get("quotaStatus", "HEALTHY"),
    ]


def generate_organizations_csv(portfolio: Mapping[str, Any]) -> str:
    """Generate Executive Summary CSV (1 row per Apigee Organization + Portfolio Totals).

    Args:
        portfolio: Portfolio mapping containing 'summary' and 'organizations'.

    Returns:
        Complete CSV file contents as string.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)

    summary: Mapping[str, Any] = portfolio.get("summary", {})
    entitlement: int | float = summary.get("entitlementPdu", 500)

    writer.writerow(ORGANIZATIONS_CSV_HEADER)
    for org in portfolio.get("organizations", ()):
        writer.writerow(_build_org_summary_row(org, entitlement))

    writer.writerow([])
    writer.writerow(_build_portfolio_total_row(summary, entitlement))
    return buf.getvalue()


def _resolve_env_calls_data(
    e: Mapping[str, Any],
    limit_calls: int,
) -> tuple[int, int, int, int, str]:
    """Extract standard, extensible, raw, weighted calls, and limit percentage."""
    std_calls = int(e.get("standardApiCalls", 0))
    ext_calls = int(e.get("extensibleApiCalls", 0))
    raw_calls = int(e.get("rawApiCalls", std_calls + ext_calls))
    yearly_calls = int(e.get("yearlyApiCalls", 0))
    if yearly_calls == 0 and (std_calls > 0 or ext_calls > 0):
        yearly_calls = int(round(std_calls * 0.2 + ext_calls * 1.0))
    call_limit_pct = (
        f"{(yearly_calls / limit_calls) * 100:.2f}%" if limit_calls > 0 else "0.00%"
    )
    return std_calls, ext_calls, raw_calls, yearly_calls, call_limit_pct


def _format_env_unit_label(e: Mapping[str, Any], include_sf: bool) -> str:
    """Format the proxy and shared flow deployed units label."""
    total_proxies = int(e.get("totalProxies", 0))
    shared_flows = int(e.get("sharedFlows", 0))
    total_artifacts = int(e.get("totalArtifacts", total_proxies))
    if include_sf:
        return f"{total_artifacts} ({total_proxies}P + {shared_flows}SF)"
    return f"{total_proxies}"


def _build_env_row(
    e: Mapping[str, Any],
    *,
    limit_pdu: int,
    limit_envs: int,
    limit_calls: int,
    include_sf: bool,
    export_date: str,
) -> list[Any]:
    """Build a CSV row representation for a single environment entitlement entry."""
    rt = e.get("runtimeType", "CLOUD")
    runtime_label = "HYBRID" if rt == "HYBRID" else "APIGEE X"
    regions_list: Sequence[str] = e.get("regions") or ()
    regions_str = ", ".join(regions_list) if regions_list else "Unattached (0 regions)"
    region_count = int(e.get("regionCount", 0))
    env_limit_pct = f"{(region_count / limit_envs) * 100:.2f}%" if limit_envs > 0 else "0.00%"
    units_label = _format_env_unit_label(e, include_sf)
    total_pdus = int(e.get("totalPdus", 0))
    pdu_limit_pct = f"{(total_pdus / limit_pdu) * 100:.2f}%" if limit_pdu > 0 else "0.00%"
    std_calls, ext_calls, raw_calls, yearly_calls, call_limit_pct = (
        _resolve_env_calls_data(e, limit_calls)
    )

    return [
        export_date,
        e.get("organization", ""),
        runtime_label,
        e.get("environment", ""),
        regions_str,
        region_count,
        env_limit_pct,
        units_label,
        total_pdus,
        pdu_limit_pct,
        std_calls,
        ext_calls,
        raw_calls,
        yearly_calls,
        call_limit_pct,
    ]


def _filter_and_sort_environments(
    environments: Sequence[Mapping[str, Any]],
    *,
    hide_zero: bool,
) -> list[Mapping[str, Any]]:
    """Filter and sort environment entries by total PDUs descending."""
    filtered = (
        [e for e in environments if e.get("totalPdus", 0) > 0]
        if hide_zero
        else list(environments)
    )
    filtered.sort(
        key=lambda e: (e.get("totalPdus", 0), e.get("totalArtifacts", 0)),
        reverse=True,
    )
    return filtered


def generate_environments_csv(
    portfolio: Mapping[str, Any],
    *,
    hide_zero: bool = False,
) -> str:
    """Generate Entitlement Compliance CSV export per environment.

    Args:
        portfolio: Portfolio mapping containing 'summary', 'settings', and 'environments'.
        hide_zero: Whether to exclude environments with zero consumed PDUs.

    Returns:
        Complete CSV file contents as string.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)

    summary: Mapping[str, Any] = portfolio.get("summary", {})
    limit_pdu = int(summary.get("entitlementPdu", 500) or 500)
    limit_envs = int(summary.get("entitlementEnvs", 6) or 6)
    limit_calls = int(summary.get("entitlementYearlyCalls", 1_500_000_000) or 1_500_000_000)
    settings: Mapping[str, Any] = portfolio.get("settings", {})
    include_sf = bool(settings.get("includeSharedFlows", True))
    export_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y/%m/%d")

    writer.writerow(ENVIRONMENTS_CSV_HEADER)
    envs = _filter_and_sort_environments(portfolio.get("environments", ()), hide_zero=hide_zero)
    for env in envs:
        writer.writerow(
            _build_env_row(
                env,
                limit_pdu=limit_pdu,
                limit_envs=limit_envs,
                limit_calls=limit_calls,
                include_sf=include_sf,
                export_date=export_date,
            )
        )

    return buf.getvalue()


def _build_deployment_row(dep: Mapping[str, Any]) -> list[Any]:
    """Build a CSV row representation for a single deployment entry."""
    rt = dep.get("runtimeType", "CLOUD")
    footprint_label = "Apigee Hybrid" if rt == "HYBRID" else "Apigee X (Cloud)"
    regions: Sequence[str] = dep.get("regions", ())
    return [
        dep.get("organization", ""),
        dep.get("projectId", ""),
        footprint_label,
        dep.get("environment", ""),
        dep.get("artifactKind", "API_PROXY"),
        dep.get("name", ""),
        dep.get("revision", "1"),
        dep.get("proxyDeploymentType", "STANDARD"),
        dep.get("regionCount", 0),
        ", ".join(regions),
        dep.get("pdu", 0),
        _format_epoch_ms(dep.get("deployStartTime")),
        dep.get("serviceAccount", ""),
    ]


def generate_deployments_csv(portfolio: Mapping[str, Any]) -> str:
    """Generate Granular Deployment Inventory CSV (1 row per deployed Proxy/SF per Env).

    Args:
        portfolio: Portfolio mapping containing 'deployments'.

    Returns:
        Complete CSV file contents as string.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow(DEPLOYMENTS_CSV_HEADER)
    for dep in portfolio.get("deployments", ()):
        writer.writerow(_build_deployment_row(dep))

    return buf.getvalue()


def _build_history_row(item: Mapping[str, Any]) -> list[Any]:
    """Build a CSV row representation for a single history snapshot."""
    return [
        item.get("timestamp", ""),
        item.get("source", ""),
        item.get("totalOrganizations", 0),
        item.get("apigeeXOrgs", 0),
        item.get("apigeeHybridOrgs", 0),
        item.get("totalEnvironments", 0),
        item.get("totalDeployedProxies", 0),
        item.get("totalDeployedSharedFlows", 0),
        item.get("apigeeXPdus", 0),
        item.get("apigeeHybridPdus", 0),
        item.get("standardProxyPdus", 0),
        item.get("extensibleProxyPdus", 0),
        item.get("sharedFlowPdus", 0),
        item.get("totalPdus", 0),
        item.get("entitlementPdu", 500),
        f"{item.get('utilizationPct', 0)}%",
        item.get("remainingPdus", 0),
        item.get("overagePdus", 0),
        item.get("quotaStatus", "HEALTHY"),
    ]


def generate_history_csv(history_entries: Sequence[Mapping[str, Any]]) -> str:
    """Generate Historical Snapshot Log CSV for trend tracking in Google Sheets.

    Args:
        history_entries: Sequence of historical snapshot entries.

    Returns:
        Complete CSV file contents as string.
    """
    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow(HISTORY_CSV_HEADER)
    for item in history_entries:
        writer.writerow(_build_history_row(item))

    return buf.getvalue()


def generate_zip_bundle(
    portfolio: Mapping[str, Any],
    history_entries: Sequence[Mapping[str, Any]],
) -> bytes:
    """Create a ZIP bundle containing all 4 CSV reports ready for Google Sheets.

    Args:
        portfolio: Complete portfolio dictionary.
        history_entries: Sequence of historical snapshots.

    Returns:
        In-memory ZIP archive bytes.
    """
    mem_zip = io.BytesIO()
    with zipfile.ZipFile(mem_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "01_apigee_pdu_executive_summary.csv",
            generate_organizations_csv(portfolio),
        )
        zf.writestr(
            "02_apigee_pdu_environments_breakdown.csv",
            generate_environments_csv(portfolio),
        )
        zf.writestr(
            "03_apigee_pdu_deployments_inventory.csv",
            generate_deployments_csv(portfolio),
        )
        zf.writestr(
            "04_apigee_pdu_historical_log.csv",
            generate_history_csv(history_entries),
        )
    return mem_zip.getvalue()
