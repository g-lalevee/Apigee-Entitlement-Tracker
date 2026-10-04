"""
CSV & Google Sheets Ready Exporter for Apigee Entitlement Tracker.

Generates four structured CSV reports designed for direct import into Google Sheets or Excel:
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
from typing import Any, Dict, List


def _format_epoch_ms(ms_val: Any) -> str:
    """Convert epoch milliseconds string/int to ISO-8601 UTC timestamp string."""
    if not ms_val:
        return ""
    try:
        ts_sec = int(ms_val) / 1000.0
        return datetime.datetime.fromtimestamp(ts_sec, tz=datetime.timezone.utc).strftime(
            "%Y-%m-%d %H:%M:%S UTC"
        )
    except Exception:
        return str(ms_val)


def generate_organizations_csv(portfolio: Dict[str, Any]) -> str:
    """Generate Executive Summary CSV (1 row per Apigee Organization + Portfolio Totals)."""
    buf = io.StringIO()
    writer = csv.writer(buf)

    summary = portfolio.get("summary", {})
    entitlement = summary.get("entitlementPdu", 500)

    writer.writerow(
        [
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
        ]
    )

    for org in portfolio.get("organizations", []):
        rt = org.get("runtimeType", "CLOUD")
        footprint_label = "Apigee Hybrid" if rt == "HYBRID" else "Apigee X (Cloud)"
        writer.writerow(
            [
                org.get("organization", ""),
                org.get("projectId", ""),
                footprint_label,
                rt,
                org.get("billingType", ""),
                org.get("subscriptionPlan", ""),
                org.get("environmentCount", 0),
                org.get("uniqueRegionsCount", 0),
                ", ".join(org.get("uniqueRegions", [])),
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
        )

    # Blank separator row + Cross-Organization Global Summary Row
    writer.writerow([])
    writer.writerow(
        [
            "TOTAL CROSS-ORGANIZATION",
            f"{summary.get('totalOrganizations', 0)} Orgs ({summary.get('apigeeXOrgs', 0)} X, {summary.get('apigeeHybridOrgs', 0)} Hybrid)",
            "All (X + Hybrid)",
            "ALL",
            "CROSS-ORG",
            f"Overage: {summary.get('overagePdus', 0)} PDUs ({summary.get('recommendedAddOnPacks50', 0)} x 50-PDU Packs)",
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
    )

    return buf.getvalue()


def generate_environments_csv(portfolio: Dict[str, Any], hide_zero: bool = False) -> str:
    """Generate Entitlement Compliance CSV export per environment."""
    buf = io.StringIO()
    writer = csv.writer(buf)

    summary = portfolio.get("summary", {})
    limit_pdu = int(summary.get("entitlementPdu", 1500) or 1500)
    limit_envs = int(summary.get("entitlementEnvs", 20) or 20)
    limit_calls = int(summary.get("entitlementYearlyCalls", 100_000_000) or 100_000_000)
    include_sf = bool(portfolio.get("settings", {}).get("includeSharedFlows", True))
    export_date = datetime.datetime.now().strftime("%Y/%m/%d")

    writer.writerow(
        [
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
            "Number of call since period start",
            "% of call Limit",
        ]
    )

    all_matching_envs = list(portfolio.get("environments", []))
    visible_envs = (
        [e for e in all_matching_envs if e.get("totalPdus", 0) > 0]
        if hide_zero
        else all_matching_envs
    )

    visible_envs.sort(
        key=lambda e: (e.get("totalPdus", 0), e.get("totalArtifacts", 0)),
        reverse=True,
    )

    for e in visible_envs:
        rt = e.get("runtimeType", "CLOUD")
        runtime_label = "HYBRID" if rt == "HYBRID" else "APIGEE X"
        regions_list = e.get("regions") or []
        regions_str = (
            ", ".join(regions_list) if regions_list else "Unattached (0 regions)"
        )
        region_count = int(e.get("regionCount", 0))
        env_limit_pct = (
            f"{(region_count / limit_envs) * 100:.2f}%" if limit_envs > 0 else "0.00%"
        )
        total_proxies = int(e.get("totalProxies", 0))
        shared_flows = int(e.get("sharedFlows", 0))
        total_artifacts = int(e.get("totalArtifacts", total_proxies))
        units_label = (
            f"{total_artifacts} ({total_proxies}P + {shared_flows}SF)"
            if include_sf
            else f"{total_proxies}"
        )
        total_pdus = int(e.get("totalPdus", 0))
        pdu_limit_pct = (
            f"{(total_pdus / limit_pdu) * 100:.2f}%" if limit_pdu > 0 else "0.00%"
        )
        yearly_calls = int(e.get("yearlyApiCalls", 0))
        call_limit_pct = (
            f"{(yearly_calls / limit_calls) * 100:.2f}%" if limit_calls > 0 else "0.00%"
        )

        writer.writerow(
            [
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
                yearly_calls,
                call_limit_pct,
            ]
        )

    return buf.getvalue()


def generate_deployments_csv(portfolio: Dict[str, Any]) -> str:
    """Generate Granular Deployment Inventory CSV (1 row per deployed Proxy or Shared Flow per Env)."""
    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow(
        [
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
        ]
    )

    for dep in portfolio.get("deployments", []):
        rt = dep.get("runtimeType", "CLOUD")
        footprint_label = "Apigee Hybrid" if rt == "HYBRID" else "Apigee X (Cloud)"
        writer.writerow(
            [
                dep.get("organization", ""),
                dep.get("projectId", ""),
                footprint_label,
                dep.get("environment", ""),
                dep.get("artifactKind", "API_PROXY"),
                dep.get("name", ""),
                dep.get("revision", "1"),
                dep.get("proxyDeploymentType", "STANDARD"),
                dep.get("regionCount", 0),
                ", ".join(dep.get("regions", [])),
                dep.get("pdu", 0),
                _format_epoch_ms(dep.get("deployStartTime")),
                dep.get("serviceAccount", ""),
            ]
        )

    return buf.getvalue()


def generate_history_csv(history_entries: List[Dict[str, Any]]) -> str:
    """Generate Historical Snapshot Log CSV for trend tracking in Google Sheets."""
    buf = io.StringIO()
    writer = csv.writer(buf)

    writer.writerow(
        [
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
        ]
    )

    for item in history_entries:
        writer.writerow(
            [
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
        )

    return buf.getvalue()


def generate_zip_bundle(
    portfolio: Dict[str, Any],
    history_entries: List[Dict[str, Any]],
) -> bytes:
    """Create a ZIP bundle containing all 4 CSV reports ready for Google Sheets."""
    mem_zip = io.BytesIO()
    with zipfile.ZipFile(mem_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("01_apigee_pdu_executive_summary.csv", generate_organizations_csv(portfolio))
        zf.writestr("02_apigee_pdu_environments_breakdown.csv", generate_environments_csv(portfolio))
        zf.writestr("03_apigee_pdu_deployments_inventory.csv", generate_deployments_csv(portfolio))
        zf.writestr("04_apigee_pdu_historical_log.csv", generate_history_csv(history_entries))
    mem_zip.seek(0)
    return mem_zip.read()
