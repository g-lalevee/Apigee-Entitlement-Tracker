# Copyright 2026 The Apigee Entitlement Tracker Authors
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
FastAPI Backend & CLI for Apigee Entitlement Tracker.

Monitors PDU, Environment Unit, and Yearly API Call usage across all authorized Apigee X (CLOUD)
and Apigee Hybrid (HYBRID) organizations using standard Google Cloud credentials (`gcloud auth`
OAuth2 / `google-auth`), tracks consumption against configured Cross-Organization Entitlements,
serves an interactive web dashboard, and exports Google-Sheets-ready CSV reports.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping, Sequence
import contextvars
import datetime
import hashlib
import html
import json
import logging
import os
import pathlib
import secrets
import threading
import time
import urllib.parse
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from collector import (
    APIGEE_JURISDICTIONS,
    DEFAULT_GOOGLE_CLIENT_ID,
    build_google_oidc_auth_url,
    clear_active_oidc_session,
    clear_user_session,
    collect_all_organizations,
    discover_authorized_organizations,
    exchange_google_oidc_code,
    extract_excluded_billing_details,
    fetch_monitoring_pdu_timeseries,
    filter_subscription_organizations,
    generate_session_id,
    get_active_jurisdiction,
    get_demo_dataset,
    get_gcp_user_auth_info,
    get_session_for_active_token,
    get_user_session,
    is_token_in_active_session,
    populate_contract_api_calls,
    recalculate_portfolio,
    refresh_google_oauth_token,
    resolve_jurisdiction_info,
    resolve_verified_subject_identity,
    set_active_jurisdiction,
    set_user_session,
    switch_or_login_gcp_user,
    validate_google_oauth_token,
)
from exporter import (
    generate_deployments_csv,
    generate_environments_csv,
    generate_history_csv,
    generate_organizations_csv,
    generate_zip_bundle,
)

logger = logging.getLogger("apigee_monitor.app")

def _resolve_initial_data_dir(base_dir: pathlib.Path) -> pathlib.Path:
    """Resolve writable data directory, falling back to /tmp when rootfs is read-only."""
    env_override = (os.environ.get("APIGEE_DATA_DIR") or "").strip()
    preferred = pathlib.Path(env_override) if env_override else (base_dir / "data")
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        if os.access(preferred, os.W_OK):
            return preferred
    except OSError:
        pass
    fallback = pathlib.Path("/tmp/apigee-licence-monitor/data")
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


BASE_DIR = pathlib.Path(__file__).resolve().parent
DATA_DIR = _resolve_initial_data_dir(BASE_DIR)
STATIC_DIR = BASE_DIR / "static"

CONFIG_PATH = DATA_DIR / "config.json"
PDU_LIMIT_PATH = BASE_DIR / "pdu_limit.json"
RAW_CACHE_PATH = DATA_DIR / "latest_raw.json"
HISTORY_PATH = DATA_DIR / "history.json"
DISCOVERED_ORGS_PATH = DATA_DIR / "discovered_orgs.json"

_FILE_IO_LOCK = threading.RLock()
_CACHE_USER_SCOPE: contextvars.ContextVar[str] = contextvars.ContextVar("cache_user_scope", default="")


DEFAULT_CONFIG: dict[str, Any] = {
    "entitlementPdu": 500,           # Cross-organization manual PDU entitlement stored permanently in pdu_limit.json
    "entitlementEnvs": 6,            # Cross-organization Environment Units (Env × Regions) entitlement
    "entitlementYearlyCalls": 1_500_000_000,  # Cross-organization Yearly API Calls entitlement over Contract Period
    "contractStartDate": "2026-01-01",
    "includeSharedFlows": True,
    "regionOverrides": {},           # Map of "org:env" -> region count
    "orgFilter": [],
    "gcpAccount": None,
    "controlPlaneJurisdiction": "global",
}


_CLOUD_RUN_CACHE_MAX_FILES = 60
_CLOUD_RUN_CACHE_TTL_SECONDS = 6 * 3600
_TS_CACHE_TTL_SECONDS = 6 * 3600


def _evict_stale_cloud_run_cache_files_unlocked() -> None:
    """Bound per-user tmpfs cache files on Cloud Run by TTL and LRU mtime."""
    if not os.environ.get("K_SERVICE") or not DATA_DIR.exists():
        return
    now_ts = time.time()
    candidates: list[tuple[float, pathlib.Path]] = []
    for p in DATA_DIR.glob("*.json"):
        try:
            mtime = p.stat().st_mtime
            if (now_ts - mtime) > _CLOUD_RUN_CACHE_TTL_SECONDS:
                p.unlink(missing_ok=True)
            else:
                candidates.append((mtime, p))
        except OSError:
            continue
    if len(candidates) > _CLOUD_RUN_CACHE_MAX_FILES:
        candidates.sort(key=lambda pair: pair[0])
        for _, victim in candidates[: len(candidates) - _CLOUD_RUN_CACHE_MAX_FILES]:
            victim.unlink(missing_ok=True)


def _atomic_write_json(path: pathlib.Path, payload: Any) -> None:
    """Atomically write JSON payload to disk using a sibling temp file and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = (
        json.dumps(payload, separators=(",", ":"))
        if os.environ.get("K_SERVICE")
        else json.dumps(payload, indent=2)
    )
    with _FILE_IO_LOCK:
        tmp_path = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
        try:
            tmp_path.write_text(serialized, encoding="utf-8")
            os.replace(tmp_path, path)
            _evict_stale_cloud_run_cache_files_unlocked()
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)


def _get_cache_user_suffix() -> str:
    """Return per-user cache suffix when running in multi-user Cloud Run mode."""
    return _CACHE_USER_SCOPE.get() if os.environ.get("K_SERVICE") else ""


def get_config_path() -> pathlib.Path:
    """Return configuration JSON path, scoped per user on multi-tenant Cloud Run."""
    suffix = _get_cache_user_suffix()
    return DATA_DIR / f"config{suffix}.json" if suffix else CONFIG_PATH


def get_history_path() -> pathlib.Path:
    """Return historical audit log JSON path, scoped per user on multi-tenant Cloud Run."""
    suffix = _get_cache_user_suffix()
    return DATA_DIR / f"history{suffix}.json" if suffix else HISTORY_PATH


def _parse_entitlement_limits_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract validated entitlement thresholds and contract start date from JSON dict."""
    out: dict[str, Any] = {}
    for key in ("entitlementPdu", "entitlementEnvs", "entitlementYearlyCalls"):
        if int(payload.get(key, 0) or 0) >= 1:
            out[key] = int(payload[key])
    if payload.get("contractStartDate"):
        out["contractStartDate"] = str(payload["contractStartDate"]).strip()[:10]
    return out


def load_entitlement_limits() -> dict[str, Any]:
    """Read the permanent Entitlement Limits & Contract Start Date from pdu_limit.json if present."""
    if PDU_LIMIT_PATH.exists():
        try:
            payload = json.loads(PDU_LIMIT_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return _parse_entitlement_limits_payload(payload)
        except (OSError, ValueError, TypeError) as exc:
            logger.debug("Failed to read entitlement limits from %s: %s", PDU_LIMIT_PATH, exc)
    return {}


def save_entitlement_limits(cfg: dict[str, Any], *, updated: bool = True) -> None:
    """Permanently store PDU, Environment Unit, and Yearly API Call limits + Contract Start Date in pdu_limit.json."""
    if os.environ.get("K_SERVICE"):
        return
    _atomic_write_json(
        PDU_LIMIT_PATH,
        {
            "entitlementPdu": max(1, int(cfg.get("entitlementPdu", 500))),
            "entitlementEnvs": max(1, int(cfg.get("entitlementEnvs", 6))),
            "entitlementYearlyCalls": max(1, int(cfg.get("entitlementYearlyCalls", 1_500_000_000))),
            "contractStartDate": str(cfg.get("contractStartDate") or "2026-01-01").strip()[:10],
            "updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat() if updated else None,
        },
    )


def _read_config_json(cfg_path: pathlib.Path) -> tuple[dict[str, Any], bool]:
    """Read optional configuration JSON dict from disk and report whether the file existed."""
    if not cfg_path.exists():
        return {}, False
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        return (data if isinstance(data, dict) else {}), True
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Failed to read config from %s: %s", cfg_path, exc)
        return {}, True


def load_config() -> dict[str, Any]:
    """Load configuration from disk, avoiding unbounded tmpfs writes on Cloud Run."""
    cfg = dict(DEFAULT_CONFIG)
    cfg_path = get_config_path()
    disk_data, has_user_cfg = _read_config_json(cfg_path)
    cfg.update(disk_data)
    is_cloud_run = bool(os.environ.get("K_SERVICE"))

    perm_limits = load_entitlement_limits()
    if perm_limits and (not is_cloud_run or not has_user_cfg):
        cfg.update(perm_limits)
    elif not perm_limits and not is_cloud_run:
        save_entitlement_limits(cfg, updated=False)

    if cfg.get("controlPlaneJurisdiction") and not is_cloud_run:
        set_active_jurisdiction(cfg["controlPlaneJurisdiction"])
    if not has_user_cfg and not is_cloud_run:
        _atomic_write_json(cfg_path, cfg)
    return cfg


def save_config(cfg: dict[str, Any]) -> None:
    """Persist runtime configuration to disk, excluding ephemeral account credentials."""
    if "entitlementPdu" in cfg and cfg["entitlementPdu"] is not None:
        cfg["entitlementPdu"] = max(1, int(cfg["entitlementPdu"]))
    if "entitlementEnvs" in cfg and cfg["entitlementEnvs"] is not None:
        cfg["entitlementEnvs"] = max(1, int(cfg["entitlementEnvs"]))
    if "entitlementYearlyCalls" in cfg and cfg["entitlementYearlyCalls"] is not None:
        cfg["entitlementYearlyCalls"] = max(1, int(cfg["entitlementYearlyCalls"]))
    save_entitlement_limits(cfg)
    disk_cfg = {k: v for k, v in cfg.items() if k != "gcpAccount"}
    _atomic_write_json(get_config_path(), disk_cfg)


def _normalize_valid_jurisdiction(jurisdiction_code: str | None = None) -> str:
    """Resolve and strictly whitelist jurisdiction code against APIGEE_JURISDICTIONS."""
    candidate = (jurisdiction_code or get_active_jurisdiction().get("code", "global")).strip().lower()
    if candidate in APIGEE_JURISDICTIONS:
        return candidate
    active_code = str(get_active_jurisdiction().get("code", "global")).strip().lower()
    return active_code if active_code in APIGEE_JURISDICTIONS else "global"


def get_raw_cache_path(jurisdiction_code: str | None = None) -> pathlib.Path:
    """Return the filesystem path for the raw cache of the specified or active jurisdiction."""
    jur = _normalize_valid_jurisdiction(jurisdiction_code)
    return DATA_DIR / f"latest_raw_{jur}{_get_cache_user_suffix()}.json"


def get_discovered_orgs_path(jurisdiction_code: str | None = None) -> pathlib.Path:
    """Return the filesystem path for cached discovered organizations of the specified jurisdiction."""
    jur = _normalize_valid_jurisdiction(jurisdiction_code)
    return DATA_DIR / f"discovered_orgs_{jur}{_get_cache_user_suffix()}.json"


def _resolve_cached_dataset_jurisdiction(data: dict[str, Any]) -> str:
    """Extract or infer the control plane jurisdiction from cached raw dataset content."""
    data_jur = str(data.get("controlPlaneJurisdiction") or "").strip().lower()
    if not data_jur and data.get("apiEndpoint"):
        endpoint_str = str(data["apiEndpoint"])
        for k, v in APIGEE_JURISDICTIONS.items():
            if v["endpoint"] in endpoint_str:
                return k
    return data_jur or "global"


def _strip_non_subscription_orgs_from_raw(data: dict[str, Any]) -> dict[str, Any]:
    """Filter out Trial/Evaluation and Pay-As-You-Go organizations while preserving excludedBillingDetails."""
    raw_orgs = data.get("organizations")
    if not isinstance(raw_orgs, list):
        return data
    kept, newly_excluded = filter_subscription_organizations(raw_orgs)
    merged_excl = sorted(
        set(data.get("excludedBillingOrganizations") or []) | set(newly_excluded),
        key=str.lower,
    )
    if not newly_excluded and not merged_excl:
        return data
    merged_details = extract_excluded_billing_details(
        raw_orgs, data.get("excludedBillingDetails"), merged_excl
    )
    updated = {
        **data,
        "organizations": kept,
        "excludedBillingOrganizations": merged_excl,
        "excludedBillingDetails": merged_details,
    }
    if len(kept) != len(raw_orgs):
        updated["totalAuthorizedOrgs"] = len(kept)
    return updated


def _read_cached_dataset_file(target_path: pathlib.Path, jur: str) -> dict[str, Any] | None:
    """Attempt reading and validating jurisdiction match of cached raw dataset."""
    if not target_path.exists():
        return None
    try:
        data = json.loads(target_path.read_text(encoding="utf-8"))
        data_jur = _resolve_cached_dataset_jurisdiction(data)
        if data_jur == jur:
            return _strip_non_subscription_orgs_from_raw(data)
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Failed to read raw dataset from %s: %s", target_path, exc)
    return None


def _build_empty_jurisdiction_dataset(jur: str) -> dict[str, Any]:
    """Construct an initial empty dataset structure for a newly selected jurisdiction."""
    cur_jur_info = APIGEE_JURISDICTIONS.get(jur) or get_active_jurisdiction()
    return {
        "source": "NEW",
        "collectedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "controlPlaneJurisdiction": jur,
        "jurisdiction": cur_jur_info,
        "apiEndpoint": cur_jur_info.get("endpoint", "") + "/organizations",
        "totalAuthorizedOrgs": 0,
        "organizations": [],
    }


def load_raw_dataset(jurisdiction_code: str | None = None) -> dict[str, Any]:
    """Load the latest raw dataset for the active or given jurisdiction, falling back to demo data."""
    jur = _normalize_valid_jurisdiction(jurisdiction_code)
    target_path = get_raw_cache_path(jur)
    if not target_path.exists() and jur == "global" and RAW_CACHE_PATH.exists():
        target_path = RAW_CACHE_PATH

    cached_data = _read_cached_dataset_file(target_path, jur)
    if cached_data is not None:
        return cached_data

    if jur != "global":
        return _build_empty_jurisdiction_dataset(jur)

    return get_demo_dataset()


def _prepare_sanitized_payload(
    raw_data: dict[str, Any],
    jurisdiction_code: str | None,
) -> tuple[dict[str, Any], str, pathlib.Path]:
    """Sanitize and attach jurisdiction metadata to raw dataset before storage."""
    sanitized = _strip_non_subscription_orgs_from_raw(dict(raw_data))
    sanitized.pop("account", None)
    if str(sanitized.get("authSource", "")).startswith("GCP User"):
        sanitized["authSource"] = "GCP User Account (OAuth2)"

    cur_jur_info = get_active_jurisdiction()
    jur = _normalize_valid_jurisdiction(
        jurisdiction_code or sanitized.get("controlPlaneJurisdiction")
    )

    sanitized["controlPlaneJurisdiction"] = jur
    sanitized["jurisdiction"] = APIGEE_JURISDICTIONS.get(jur) or cur_jur_info
    target_path = get_raw_cache_path(jur)
    return sanitized, jur, target_path


def _merge_org_traffic_metrics(old_o: dict[str, Any], new_o: dict[str, Any]) -> None:
    """Preserve prior environment daily calls only when traffic was not queried in the new pass."""
    if not (old_o.get("environments") and new_o.get("environments")):
        return
    old_envs = {e["environment"]: e for e in old_o.get("environments", [])}
    for new_e in new_o.get("environments", []):
        old_e = old_envs.get(new_e["environment"])
        if old_e and new_e.get("callsSource") == "PENDING" and old_e.get("dailyCalls"):
            new_e.update({
                "dailyCalls": old_e["dailyCalls"],
                "dailyStandardCalls": old_e.get("dailyStandardCalls", {}),
                "dailyExtensibleCalls": old_e.get("dailyExtensibleCalls", {}),
                "dailyEntitlementCalls": old_e.get("dailyEntitlementCalls", {}),
                "dailyEnvoyCalls": old_e.get("dailyEnvoyCalls", {}),
                "standardApiCalls": old_e.get("standardApiCalls", 0),
                "envoyApiCalls": old_e.get("envoyApiCalls", 0),
                "extensibleApiCalls": old_e.get("extensibleApiCalls", 0),
                "rawApiCalls": old_e.get("rawApiCalls", 0),
                "yearlyApiCalls": old_e.get("yearlyApiCalls", 0),
                "callsSource": old_e.get("callsSource", "APIGEE_ANALYTICS"),
                "callsStatus": old_e.get("callsStatus", "OK"),
            })


def _sort_raw_organizations(orgs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort raw organization records descending by total PDUs then alphabetically."""
    orgs.sort(
        key=lambda o: (
            -sum(e.get("totalPdus", 0) for e in o.get("environments", [])),
            o["organization"].lower(),
        )
    )
    return orgs


def _merge_raw_organizations(
    existing_orgs: list[dict[str, Any]],
    new_orgs: list[dict[str, Any]],
    *,
    authoritative_replace: bool = False,
) -> list[dict[str, Any]]:
    """Merge or authoritatively replace organization inventory while preserving PENDING call caches."""
    existing_map = {o["organization"]: o for o in existing_orgs}
    target_map: dict[str, dict[str, Any]] = {} if authoritative_replace else dict(existing_map)
    for new_o in new_orgs:
        org_name = new_o["organization"]
        old_o = existing_map.get(org_name)
        if old_o and new_o.get("status") == "ERROR" and old_o.get("status") == "OK":
            target_map[org_name] = {**old_o, "lastScanError": new_o.get("error")}
            continue
        if old_o:
            _merge_org_traffic_metrics(old_o, new_o)
        target_map[org_name] = new_o
    return _sort_raw_organizations(list(target_map.values()))


def _merge_subscription_org_inventories(
    existing_orgs: list[dict[str, Any]],
    sanitized: dict[str, Any],
) -> list[dict[str, Any]]:
    """Reconcile existing and incoming subscription organizations according to partial/full scan scope."""
    excluded_set = set(sanitized.get("excludedOrganizations") or []) | set(
        sanitized.get("excludedBillingOrganizations") or []
    )
    existing_sub, _ = filter_subscription_organizations(existing_orgs)
    incoming_sub, _ = filter_subscription_organizations(sanitized.get("organizations", []))
    existing_clean = [o for o in existing_sub if o.get("organization") not in excluded_set]
    is_partial = bool(sanitized.get("isPartialScan"))
    merged_orgs = _merge_raw_organizations(
        existing_clean, incoming_sub, authoritative_replace=not is_partial
    )
    return [o for o in merged_orgs if o.get("organization") not in excluded_set]


def _merge_excluded_detail_list(
    existing_details: Sequence[Any],
    incoming_details: Sequence[Any],
    active_sub_names: set[str],
) -> list[dict[str, Any]]:
    """Deduplicate and filter excluded billing detail records against active subscription orgs."""
    by_name: dict[str, dict[str, Any]] = {}
    for entry in list(existing_details) + list(incoming_details):
        if isinstance(entry, Mapping):
            org_name = str(entry.get("organization") or "").strip()
            if org_name and org_name not in active_sub_names:
                by_name[org_name] = dict(entry)
    return [by_name[k] for k in sorted(by_name, key=str.lower)]


def _merge_excluded_billing_orgs(existing: dict[str, Any], sanitized: dict[str, Any]) -> None:
    """Preserve excluded Trial/PAYG organizations and details across partial scans."""
    if not sanitized.get("isPartialScan"):
        return
    active_sub_names = {str(o.get("organization") or "") for o in sanitized.get("organizations", [])}
    prior_excl = set(existing.get("excludedBillingOrganizations") or [])
    curr_excl = set(sanitized.get("excludedBillingOrganizations") or [])
    sanitized["excludedBillingOrganizations"] = sorted((prior_excl | curr_excl) - active_sub_names)
    sanitized["excludedBillingDetails"] = _merge_excluded_detail_list(
        existing.get("excludedBillingDetails") or (),
        sanitized.get("excludedBillingDetails") or (),
        active_sub_names,
    )


def _try_merge_existing_dataset(
    target_path: pathlib.Path,
    sanitized: dict[str, Any],
    jur: str,
) -> None:
    """Attempt reading prior dataset on disk and reconciling live scan data within same jurisdiction."""
    if not target_path.exists():
        return
    try:
        existing_raw = json.loads(target_path.read_text(encoding="utf-8"))
        is_same_live_jur = (
            existing_raw.get("source") == "LIVE_APIGEE_API"
            and _resolve_cached_dataset_jurisdiction(existing_raw) == jur
        )
        if is_same_live_jur and "organizations" in sanitized:
            existing = _strip_non_subscription_orgs_from_raw(existing_raw)
            if existing.get("organizations"):
                merged_orgs = _merge_subscription_org_inventories(existing["organizations"], sanitized)
                sanitized["organizations"] = merged_orgs
                sanitized["totalAuthorizedOrgs"] = len(merged_orgs)
            _merge_excluded_billing_orgs(existing, sanitized)
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        logger.debug("Failed to merge with existing raw dataset at %s: %s", target_path, exc)


def save_raw_dataset(
    raw_data: dict[str, Any],
    merge_existing: bool = True,
    jurisdiction_code: str | None = None,
) -> dict[str, Any]:
    """Persist raw scan dataset to disk, reconciling live results for the target jurisdiction."""
    with _FILE_IO_LOCK:
        sanitized, jur, target_path = _prepare_sanitized_payload(raw_data, jurisdiction_code)
        if merge_existing and sanitized.get("source") == "LIVE_APIGEE_API":
            _try_merge_existing_dataset(target_path, sanitized, jur)
        _atomic_write_json(target_path, sanitized)
        return sanitized


def load_history() -> list[dict[str, Any]]:
    """Load historical audit snapshots from disk."""
    hist_path = get_history_path()
    if hist_path.exists():
        try:
            data = json.loads(hist_path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Failed to read history from %s: %s", hist_path, exc)
    return []


def append_history_snapshot(portfolio: dict[str, Any]) -> list[dict[str, Any]]:
    """Record an audit summary snapshot into the rolling historical timeline."""
    summary = portfolio.get("summary", {})
    raw_src = str(portfolio.get("authSource") or portfolio.get("source", "DEMO_DATASET"))
    safe_src = "GCP User Account (OAuth2)" if raw_src.startswith("GCP User") else raw_src
    entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "source": safe_src,
        "jurisdiction": portfolio.get("controlPlaneJurisdiction", "global"),
        "totalOrganizations": summary.get("totalOrganizations", 0),
        "apigeeXOrgs": summary.get("apigeeXOrgs", 0),
        "apigeeHybridOrgs": summary.get("apigeeHybridOrgs", 0),
        "totalEnvironments": summary.get("totalEnvironments", 0),
        "totalEnvironmentUnits": summary.get("totalEnvironmentUnits", 0),
        "entitlementEnvs": summary.get("entitlementEnvs", 6),
        "totalDeployedProxies": summary.get("totalDeployedProxies", 0),
        "totalDeployedSharedFlows": summary.get("totalDeployedSharedFlows", 0),
        "apigeeXPdus": summary.get("apigeeXPdus", 0),
        "apigeeHybridPdus": summary.get("apigeeHybridPdus", 0),
        "standardProxyPdus": summary.get("standardProxyPdus", 0),
        "extensibleProxyPdus": summary.get("extensibleProxyPdus", 0),
        "sharedFlowPdus": summary.get("sharedFlowPdus", 0),
        "totalPdus": summary.get("totalPdus", 0),
        "entitlementPdu": summary.get("entitlementPdu", 500),
        "utilizationPct": summary.get("utilizationPct", 0),
        "remainingPdus": summary.get("remainingPdus", 0),
        "overagePdus": summary.get("overagePdus", 0),
        "quotaStatus": summary.get("quotaStatus", "HEALTHY"),
        "totalYearlyApiCalls": summary.get("totalYearlyApiCalls", 0),
        "entitlementYearlyCalls": summary.get("entitlementYearlyCalls", 1_500_000_000),
        "contractStartDate": summary.get("contractStartDate", ""),
    }
    with _FILE_IO_LOCK:
        history = load_history()
        history.insert(0, entry)
        history = history[:100]
        _atomic_write_json(get_history_path(), history)
        return history


def _has_cached_calls_period_mismatch(raw: Mapping[str, Any], cfg_contract_date: str | None) -> bool:
    """Return True when cached live dataset's contractPeriod differs from active contractStartDate."""
    if raw.get("source") != "LIVE_APIGEE_API" or not raw.get("organizations"):
        return False
    raw_cp = str((raw.get("contractPeriod") or {}).get("contractStartDate") or "").strip()[:10]
    cfg_cp = str(cfg_contract_date or "").strip()[:10]
    return bool(raw_cp and cfg_cp and raw_cp != cfg_cp)


def get_current_portfolio(
    jurisdiction_code: str | None = None,
    auth_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble and calculate current entitlement portfolio across authorized organizations."""
    cfg = load_config()
    cur_jur_code = _normalize_valid_jurisdiction(
        jurisdiction_code or cfg.get("controlPlaneJurisdiction")
    )
    raw = load_raw_dataset(cur_jur_code)
    portfolio = recalculate_portfolio(
        raw,
        entitlement_pdu=cfg.get("entitlementPdu", 500),
        include_shared_flows=cfg.get("includeSharedFlows", True),
        region_overrides=cfg.get("regionOverrides", {}),
        entitlement_envs=cfg.get("entitlementEnvs", 6),
        entitlement_yearly_calls=cfg.get("entitlementYearlyCalls", 1_500_000_000),
        contract_start_date=cfg.get("contractStartDate"),
    )
    portfolio["callsPeriodMismatch"] = _has_cached_calls_period_mismatch(raw, cfg.get("contractStartDate"))
    cur_jur = APIGEE_JURISDICTIONS.get(cur_jur_code) or get_active_jurisdiction()
    portfolio["jurisdiction"] = cur_jur
    portfolio["controlPlaneJurisdiction"] = cur_jur["code"]
    portfolio["controlPlaneEndpoint"] = cur_jur["endpoint"]
    history = load_history()
    if not history and (not os.environ.get("K_SERVICE") or _get_cache_user_suffix()):
        history = append_history_snapshot(portfolio)
    portfolio["history"] = history
    portfolio["auth"] = (
        auth_override
        if auth_override is not None
        else get_gcp_user_auth_info(jurisdiction=cur_jur_code)
    )
    return portfolio


# --- FastAPI Application ---

app = FastAPI(
    title="Apigee Entitlement Tracker (Apigee X & Hybrid)",
    description="Cross-organization Proxy Deployment Unit (PDU), Environment Unit, and Yearly API Call monitoring dashboard and CSV/Sheets exporter.",
    version="1.2.0",
)

_RAW_ALLOWED_HOSTS = os.environ.get(
    "ALLOWED_HOSTS",
    "localhost,127.0.0.1,[::1],testserver,*.run.app,*.appspot.com,*.cloudshell.dev",
)
ALLOWED_HOSTS = [h.strip() for h in _RAW_ALLOWED_HOSTS.split(",") if h.strip()]
if not ALLOWED_HOSTS:
    ALLOWED_HOSTS = ["*"]

app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class SettingsUpdateRequest(BaseModel):
    """Payload schema for updating runtime entitlement parameters and overrides."""

    entitlementPdu: int | None = None
    entitlementEnvs: int | None = None
    entitlementYearlyCalls: int | None = None
    contractStartDate: str | None = None
    includeSharedFlows: bool | None = None
    regionOverrides: dict[str, int] | None = None
    resetOverrides: bool = False
    jurisdiction: str | None = None


class AuthLoginRequest(BaseModel):
    """Payload schema for user authentication or account switching."""

    account: str | None = None
    triggerBrowserLogin: bool = False
    accessToken: str | None = None
    jurisdiction: str | None = None


class JurisdictionUpdateRequest(BaseModel):
    """Payload schema for updating the active Apigee Control Plane Jurisdiction."""

    jurisdiction: str


class OidcTokenRequest(BaseModel):
    """Payload schema for registering an explicit Google OAuth 2.0 access token."""

    accessToken: str
    email: str | None = None


class LiveScanRequest(BaseModel):
    """Payload schema for triggering a live Apigee organization inventory scan."""

    account: str | None = None
    accessToken: str | None = None
    orgFilter: list[str] | None = None
    jurisdiction: str | None = None


_MULTI_TENANT_PUBLIC_DOMAINS = {
    "run.app",
    "appspot.com",
    "cloudshell.dev",
    "azurewebsites.net",
    "herokuapp.com",
    "firebaseapp.com",
    "web.app",
    "github.io",
}


def _is_multi_tenant_domain(domain: str) -> bool:
    """Check if domain is a known multi-tenant public cloud or hosting domain."""
    clean = domain.lower().strip(".")
    return any(clean == pub or clean.endswith("." + pub) for pub in _MULTI_TENANT_PUBLIC_DOMAINS)


def _extract_hostname(url_or_host: str) -> str:
    """Extract lowercase hostname from a URL or Host header string."""
    if not url_or_host or not isinstance(url_or_host, str):
        return ""
    raw = url_or_host.strip()
    if not raw or raw.lower() == "null":
        return ""
    with_scheme = raw if "://" in raw or raw.startswith("//") else f"//{raw}"
    parsed = urllib.parse.urlsplit(with_scheme)
    return (parsed.hostname or raw.split(":")[0]).lower()


def _matches_allowed_host_pattern(origin_host: str, pattern: str) -> bool:
    """Return True if origin_host matches a single non-wildcard, single-tenant ALLOWED_HOSTS pattern."""
    clean_pat = pattern.strip().lower()
    if not clean_pat or clean_pat == "*":
        return False
    if clean_pat.startswith("*."):
        base = clean_pat[2:].strip(".")
        if not base or _is_multi_tenant_domain(base):
            return False
        return origin_host == base or origin_host.endswith("." + base)
    return clean_pat.strip("[]") == origin_host


def _is_allowed_origin_host(origin: str, request_host: str) -> bool:
    """Verify whether the request Origin/Referer matches the host or trusted hosts."""
    origin_host = _extract_hostname(origin)
    if not origin_host:
        return False

    clean_request_host = _extract_hostname(request_host)
    if clean_request_host and origin_host == clean_request_host:
        return True

    local_hosts = {"localhost", "127.0.0.1", "::1", "testserver"}
    if origin_host in local_hosts and clean_request_host in local_hosts:
        return True

    return any(_matches_allowed_host_pattern(origin_host, p) for p in ALLOWED_HOSTS)


def _get_header_case_insensitive(request: Request, name: str) -> str:
    """Return header value by name regardless of casing."""
    return request.headers.get(name) or request.headers.get(name.lower()) or ""


def _host_matches_trusted_hosts(candidate_host: str) -> bool:
    """Verify candidate hostname against configured non-wildcard, single-tenant ALLOWED_HOSTS patterns."""
    clean_host = _extract_hostname(candidate_host)
    if not clean_host:
        return False
    return any(_matches_allowed_host_pattern(clean_host, p) for p in ALLOWED_HOSTS)


def _is_trusted_proxy_env() -> bool:
    """Return True only when deployed behind a trusted reverse proxy (Cloud Run or explicit opt-in)."""
    return bool(os.environ.get("K_SERVICE") or os.environ.get("TRUST_PROXY_HEADERS") == "1")


def _resolve_trusted_host_and_proto(request: Request) -> tuple[str, str]:
    """Resolve validated scheme and host, ignoring untrusted X-Forwarded-* headers."""
    scheme = request.url.scheme or "http"
    host = request.headers.get("host") or request.url.netloc
    if _is_trusted_proxy_env():
        fwd_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        if fwd_proto in {"http", "https"}:
            scheme = fwd_proto
        fwd_host = request.headers.get("x-forwarded-host", "").split(",")[0].strip()
        if fwd_host and _host_matches_trusted_hosts(fwd_host):
            host = fwd_host
    return scheme, host


def _is_mutative_request_origin_valid(request: Request) -> bool:
    """Validate Origin and Referer headers on state-changing HTTP requests."""
    origin = _get_header_case_insensitive(request, "Origin")
    referer = _get_header_case_insensitive(request, "Referer")
    if not origin and not referer:
        return False
    _, req_host = _resolve_trusted_host_and_proto(request)
    if origin and not _is_allowed_origin_host(origin, req_host):
        return False
    return not (referer and not _is_allowed_origin_host(referer, req_host))


def _hash_user_scope_ident(ident: str) -> str:
    """Hash a verified user identity into a 12-char cache suffix."""
    return f"_{hashlib.sha256(ident.encode('utf-8')).hexdigest()[:12]}"


def _set_verified_cloud_run_scope(ident: str | None) -> None:
    """Bind the current request's Cloud Run cache scope to a cryptographically verified identity."""
    if os.environ.get("K_SERVICE") and ident and ident.strip():
        _CACHE_USER_SCOPE.set(_hash_user_scope_ident(ident.strip()))


def _compute_cloud_run_user_scope(request: Request) -> str:
    """Derive a per-user cache file suffix strictly from verified session or IAP identity."""
    verified_iap = _get_verified_iap_email(request)
    if verified_iap:
        return _hash_user_scope_ident(f"iap:{verified_iap}")
    session_id = request.cookies.get("apigee_session_id", "").strip()
    if session_id:
        user_sess = get_user_session(session_id)
        if user_sess:
            ident = str(user_sess.get("email") or user_sess.get("account") or f"sid:{session_id}")
            return _hash_user_scope_ident(ident)
    return ""


def _build_csp_policy(script_nonce: str | None = None) -> str:
    """Build Content-Security-Policy header value with optional per-response script nonce."""
    script_src = f"'self' 'nonce-{script_nonce}'" if script_nonce else "'self'"
    return (
        "default-src 'self'; "
        f"script-src {script_src}; "
        "object-src 'none'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com data:; "
        "img-src 'self' data: https:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'"
    )


_DEFAULT_CSP_POLICY = _build_csp_policy()


@app.middleware("http")
async def protect_csrf_and_origin(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Protect state-changing endpoints against cross-origin and DNS rebinding attacks."""
    is_mutative_method = request.method in {"POST", "PUT", "DELETE", "PATCH"}
    is_scan_stream = request.url.path.rstrip("/") == "/api/scan/stream"
    if (is_mutative_method or is_scan_stream) and not _is_mutative_request_origin_valid(request):
        return JSONResponse(
            {"detail": "Forbidden: cross-origin request rejected"},
            status_code=403,
        )
    scope_token: contextvars.Token[str] | None = None
    if os.environ.get("K_SERVICE"):
        scope_token = _CACHE_USER_SCOPE.set(_compute_cloud_run_user_scope(request))
    try:
        return await call_next(request)
    finally:
        if scope_token is not None:
            _CACHE_USER_SCOPE.reset(scope_token)


@app.middleware("http")
async def disable_static_and_auth_cache(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Attach security headers and disable browser caching for dashboard and auth endpoints."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=()"
    if "Content-Security-Policy" not in response.headers:
        response.headers["Content-Security-Policy"] = _DEFAULT_CSP_POLICY
    path = request.url.path
    if path == "/" or path.startswith("/static/") or path.startswith("/api/auth"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/")
async def index_page() -> FileResponse:
    """Serve the single-page dashboard HTML application."""
    return FileResponse(
        str(STATIC_DIR / "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


def _is_clean_token(tok: str | None) -> bool:
    """Return True if token string is non-empty and not a JWT ID token."""
    return bool(tok and tok.strip() and not tok.strip().startswith("eyJ"))


def _extract_live_session_token(request: Request) -> str | None:
    """Return non-expired accessToken from active apigee_session_id if present."""
    session_id = request.cookies.get("apigee_session_id", "").strip()
    if not _is_live_user_session(session_id):
        return None
    sess_tok = str(get_user_session(session_id).get("accessToken", "")).strip()
    return sess_tok if _is_clean_token(sess_tok) else None


def _resolve_header_vs_live_token(request: Request, live_sess_tok: str | None) -> str | None:
    """Resolve Authorization Bearer header token, preferring refreshed live_sess_tok when header is stale."""
    auth_header = request.headers.get("Authorization", "").strip()
    if not auth_header.startswith("Bearer "):
        return None
    candidate = auth_header[7:].strip()
    if not _is_clean_token(candidate):
        return None
    if live_sess_tok and live_sess_tok != candidate and not is_token_in_active_session(candidate):
        return live_sess_tok
    return candidate


def extract_bearer_token(request: Request, explicit_token: str | None = None) -> str | None:
    """Extract a user OAuth access token preferring refreshed live-session over stale header."""
    if _is_clean_token(explicit_token):
        return explicit_token.strip() if explicit_token else None

    live_sess_tok = _extract_live_session_token(request)
    header_resolved = _resolve_header_vs_live_token(request, live_sess_tok)
    if header_resolved:
        return header_resolved
    if live_sess_tok:
        return live_sess_tok

    session_id = request.cookies.get("apigee_session_id", "").strip()
    sess_tok = str(get_user_session(session_id).get("accessToken", "")).strip() if session_id else ""
    if _is_clean_token(sess_tok):
        return sess_tok

    cookie_tok = request.cookies.get("apigee_access_token", "").strip()
    return cookie_tok if _is_clean_token(cookie_tok) else None


def _get_verified_iap_email(request: Request) -> str | None:
    """Return verified IAP user email only when cryptographic JWT assertion is valid."""
    iap_user = request.headers.get("x-goog-authenticated-user-email") or request.headers.get(
        "X-Goog-Authenticated-User-Email"
    )
    iap_jwt = request.headers.get("x-goog-iap-jwt-assertion") or request.headers.get(
        "X-Goog-IAP-JWT-Assertion"
    )
    if iap_user and _validate_iap_jwt(iap_jwt, iap_user):
        return iap_user.split(":")[-1].strip() if ":" in iap_user else iap_user.strip()
    return None


def get_request_user_account(request: Request, explicit_account: str | None = None) -> str | None:
    """Resolve active user account email without trusting caller-supplied parameters on Cloud Run."""
    verified_iap = _get_verified_iap_email(request)
    if verified_iap:
        return verified_iap
    session_id = request.cookies.get("apigee_session_id", "").strip()
    if session_id:
        user_sess = get_user_session(session_id)
        user_acct = user_sess.get("email") or user_sess.get("account")
        if user_acct:
            return str(user_acct)
    if not os.environ.get("K_SERVICE"):
        if explicit_account and explicit_account.strip():
            return explicit_account.strip()
        return load_config().get("gcpAccount")
    return None


def _apply_iap_identity(request: Request, info: dict[str, Any]) -> None:
    """Detect and inject cryptographically verified Google Cloud IAP user identity."""
    clean_user = _get_verified_iap_email(request)
    if not clean_user:
        return
    info["authenticated"] = True
    info["activeAccount"] = clean_user
    info["authSource"] = clean_user
    info["isIap"] = True
    accounts = info.get("accounts", [])
    if not any(a.get("account") == clean_user for a in accounts):
        accounts.insert(0, {"account": clean_user, "status": "ACTIVE"})
    info["accounts"] = accounts


def _is_live_user_session(session_id: str | None) -> bool:
    """Return True when session_id exists in _USER_SESSIONS and has not expired."""
    if not session_id:
        return False
    sess = get_user_session(session_id)
    if not sess:
        return False
    exp = sess.get("expiresAt")
    return exp is None or time.time() < float(exp)


async def _verify_request_token(info: dict[str, Any], bearer_tok: str | None) -> None:
    """Validate bearer token online when not already backed by a live session."""
    if not bearer_tok or is_token_in_active_session(bearer_tok):
        return
    try:
        tok_info = await asyncio.to_thread(validate_google_oauth_token, bearer_tok)
        info["hasUserToken"] = True
        info["tokenValid"] = True
        info["tokenExpired"] = False
        info["expiresIn"] = tok_info.get("expiresIn")
        if not info.get("activeAccount"):
            info["activeAccount"] = tok_info.get("email")
            info["authenticated"] = True
    except (ValueError, OSError, RuntimeError) as exc:
        logger.debug("Token validation failed: %s", exc)
        info["tokenValid"] = False
        info["tokenExpired"] = True
        info["hasUserToken"] = False
        if not info.get("isIap"):
            info["authenticated"] = False


_LOOPBACK_CLIENTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost", "testserver"})


def _is_loopback_workstation_request(request: Request) -> bool:
    """Return True when the HTTP request originates from a local loopback client and host."""
    if os.environ.get("ALLOW_REMOTE_WORKSTATION_AUTH") == "1":
        return True
    client_host = (request.client.host if request.client else "").strip().lower()
    req_host = _extract_hostname(request.headers.get("host") or request.url.netloc)
    return client_host in _LOOPBACK_CLIENTS and req_host in _LOOPBACK_HOSTS


def _rotate_session_id(request: Request) -> tuple[str, dict[str, Any]]:
    """Invalidate any pre-existing session cookie ID and mint a fresh cryptographic session ID."""
    prior_sid = request.cookies.get("apigee_session_id", "").strip()
    prior_sess = dict(get_user_session(prior_sid)) if prior_sid else {}
    if prior_sid:
        clear_user_session(prior_sid)
    return generate_session_id(), prior_sess


def _attach_local_workstation_session_cookie(
    request: Request,
    response: Response,
    info: dict[str, Any],
) -> None:
    """Mint a fresh HttpOnly session cookie on explicit POST login from a local loopback workstation."""
    existing_sid = request.cookies.get("apigee_session_id", "").strip()
    if os.environ.get("K_SERVICE") or not _is_loopback_workstation_request(request):
        return
    if _is_live_user_session(existing_sid):
        return
    if not (info.get("authenticated") and info.get("hasUserToken")):
        return
    local_sess_id, _ = _rotate_session_id(request)
    exp_sec = float(info.get("expiresIn") or 3600)
    set_user_session(
        local_sess_id,
        {
            "email": info.get("activeAccount") or "local-gcloud-user",
            "authenticated": True,
            "authMethod": info.get("authMethod", "LOCAL_GCLOUD_ADC"),
            "expiresIn": exp_sec,
            "expiresAt": time.time() + max(300.0, exp_sec),
        },
    )
    scheme, _ = _resolve_trusted_host_and_proto(request)
    response.set_cookie(
        key="apigee_session_id",
        value=local_sess_id,
        max_age=86400,
        httponly=True,
        secure=(scheme == "https"),
        samesite="lax",
    )


def _validate_explicit_jurisdiction_param(jurisdiction: str | None) -> str | None:
    """Validate an explicit jurisdiction query/body string and raise HTTP 400 if unrecognized."""
    if jurisdiction is None or not jurisdiction.strip():
        return None
    jur_info = resolve_jurisdiction_info(jurisdiction)
    if jur_info is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid jurisdiction '{jurisdiction}'. Must be one of: "
                f"{', '.join(APIGEE_JURISDICTIONS.keys())}"
            ),
        )
    return str(jur_info["code"])


def _resolve_request_jurisdiction(
    request: Request | None = None,
    jurisdiction: str | None = None,
) -> str:
    """Validate explicit jurisdiction or resolve per-request cookie/config jurisdiction."""
    explicit_code = _validate_explicit_jurisdiction_param(jurisdiction)
    if explicit_code is not None:
        return explicit_code

    if request is not None:
        raw_cookie = request.cookies.get("apigee_jurisdiction", "").strip()
        if raw_cookie:
            cookie_jur = resolve_jurisdiction_info(raw_cookie)
            if cookie_jur is not None:
                return str(cookie_jur["code"])

    raw_cfg = str(load_config().get("controlPlaneJurisdiction") or "").strip()
    if raw_cfg:
        cfg_jur = resolve_jurisdiction_info(raw_cfg)
        if cfg_jur is not None:
            return str(cfg_jur["code"])
    return str(get_active_jurisdiction()["code"])


def _sync_refreshed_session_memory(
    session_id: str,
    prior_tok: str,
    user_sess_mut: dict[str, Any] | None,
    info: dict[str, Any],
) -> tuple[str, str] | None:
    """Persist refreshed OAuth session in memory and return (eff_sid, new_tok) when rotated."""
    if not user_sess_mut or not info.get("tokenValid"):
        return None
    new_tok = str(user_sess_mut.get("accessToken") or "").strip()
    if not new_tok or new_tok == prior_tok:
        return None
    known_sess = bool(session_id and get_user_session(session_id))
    eff_sid = session_id if known_sess else generate_session_id()
    set_user_session(eff_sid, user_sess_mut)
    return eff_sid, new_tok


def _populate_auth_status_jurisdiction(info: dict[str, Any], cur_jur_code: str) -> None:
    """Normalize boolean token flags and attach jurisdiction metadata to auth-status payload."""
    info["tokenValid"] = bool(info.get("tokenValid", False) and info.get("hasUserToken", False))
    info["tokenExpired"] = bool(info.get("tokenExpired", False))
    cur_jur = resolve_jurisdiction_info(cur_jur_code) or get_active_jurisdiction()
    info["jurisdiction"] = cur_jur
    info["controlPlaneJurisdiction"] = cur_jur["code"]
    info["controlPlaneEndpoint"] = cur_jur["endpoint"]
    info["jurisdictions"] = list(APIGEE_JURISDICTIONS.values())


def _extract_auth_status_inputs(
    request: Request,
) -> tuple[str | None, str, str, str | None, dict[str, Any] | None]:
    """Extract header token, session ID, prior token, refresh token, and mutable session copy."""
    header_tok = extract_bearer_token(request)
    session_id = request.cookies.get("apigee_session_id", "").strip()
    user_sess = get_user_session(session_id) if session_id else {}
    prior_tok = str(user_sess.get("accessToken") or "").strip()
    cookie_refresh = request.cookies.get("apigee_refresh_token") or user_sess.get("refreshToken")
    sess_mut = dict(user_sess) if (user_sess or cookie_refresh) else None
    explicit_tok = header_tok or request.cookies.get("apigee_access_token") or prior_tok
    return explicit_tok, session_id, prior_tok, cookie_refresh, sess_mut


@app.get("/api/auth-status")
async def check_auth_status(request: Request) -> JSONResponse:
    """Return read-only authentication status, token validity, and active jurisdiction."""
    cur_jur_code = _resolve_request_jurisdiction(request)
    header_tok = extract_bearer_token(request)
    explicit_tok, session_id, prior_tok, cookie_refresh_tok, user_sess_mut = _extract_auth_status_inputs(request)
    info = await asyncio.to_thread(
        get_gcp_user_auth_info,
        explicit_token=explicit_tok,
        explicit_refresh_token=cookie_refresh_tok,
        explicit_account=get_request_user_account(request),
        user_session=user_sess_mut,
        jurisdiction=cur_jur_code,
    )
    rotated = _sync_refreshed_session_memory(session_id, prior_tok, user_sess_mut, info)
    _apply_iap_identity(request, info)
    await _verify_request_token(info, rotated[1] if rotated else header_tok)
    _populate_auth_status_jurisdiction(info, cur_jur_code)

    resp = JSONResponse(info, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    if rotated is not None:
        scheme, _ = _resolve_trusted_host_and_proto(request)
        _attach_oidc_cookies(resp, rotated[0], rotated[1], "", scheme == "https", int(info.get("expiresIn") or 3600))
    return resp


def _resolve_redirect_endpoints(request: Request) -> tuple[str, str]:
    """Compute validated base URL and redirect URI for OAuth authentication flows."""
    scheme, host = _resolve_trusted_host_and_proto(request)
    base_url = f"{scheme}://{host}"
    return base_url, f"{base_url}/api/auth/oidc/callback"


async def _perform_local_gcloud_browser_login(
    request: Request,
    account: str | None,
    redirect_uri: str,
    base_url: str,
) -> JSONResponse:
    """Launch interactive local gcloud browser login flow."""
    info = await asyncio.to_thread(
        switch_or_login_gcp_user,
        account=account,
        trigger_browser_login=True,
        redirect_uri=redirect_uri,
    )
    if info.get("authenticated"):
        cfg = load_config()
        cfg["gcpAccount"] = info.get("activeAccount")
        save_config(cfg)
        resp = JSONResponse(
            {
                "authUrl": "/?authenticated=1",
                "redirectUri": f"{base_url}/?authenticated=1",
                "activeAccount": info.get("activeAccount"),
                "isOAuthConfigured": False,
            }
        )
        _attach_local_workstation_session_cookie(request, resp, info)
        return resp
    return JSONResponse(
        {
            "error": (
                "Interactive gcloud login did not complete or gcloud CLI is not available. "
                "Paste a personal OAuth access token to sign in."
            ),
            "redirectUri": redirect_uri,
            "isOAuthConfigured": False,
            "needsTokenModal": True,
        },
        status_code=400,
    )


def _build_custom_oauth_redirect_response(
    request: Request,
    redirect_uri: str,
    account: str | None,
    custom_client_id: str,
) -> JSONResponse:
    """Create JSONResponse with OAuth URL and CSRF state cookie."""
    state_token = generate_session_id()
    auth_url = build_google_oidc_auth_url(
        redirect_uri=redirect_uri, prompt_account=account, state=state_token
    )
    scheme, _ = _resolve_trusted_host_and_proto(request)
    resp = JSONResponse(
        {
            "authUrl": auth_url,
            "redirectUri": redirect_uri,
            "clientId": custom_client_id,
            "isOAuthConfigured": True,
        }
    )
    resp.set_cookie(
        key="apigee_oidc_state", value=state_token, max_age=600, httponly=True, secure=(scheme == "https"), samesite="lax"
    )
    return resp


def _verify_auth_url_request_origin(request: Request, *, require_origin: bool) -> None:
    """Reject cross-site auth-url requests and require same-origin headers for local side-effect login."""
    sec_site = request.headers.get("sec-fetch-site", "").strip().lower()
    if sec_site == "cross-site":
        raise HTTPException(status_code=403, detail="Forbidden: cross-site request rejected")
    has_origin_or_ref = bool(request.headers.get("Origin") or request.headers.get("Referer"))
    if (require_origin or has_origin_or_ref) and not _is_mutative_request_origin_valid(request):
        raise HTTPException(status_code=403, detail="Forbidden: cross-origin request rejected")


@app.get("/api/auth/oidc/auth-url")
@app.post("/api/auth/oidc/auth-url")
async def api_oidc_auth_url(
    request: Request,
    account: str | None = Query(default=None),
) -> JSONResponse:
    """Force Google re-authentication / account selection."""
    _verify_auth_url_request_origin(request, require_origin=False)
    base_url, redirect_uri = _resolve_redirect_endpoints(request)
    custom_client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    custom_client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    if custom_client_id and custom_client_id != DEFAULT_GOOGLE_CLIENT_ID and custom_client_secret:
        return _build_custom_oauth_redirect_response(request, redirect_uri, account, custom_client_id)

    if _get_verified_iap_email(request):
        return JSONResponse(
            {
                "authUrl": "/_gcp_iap/clear_login_cookie",
                "redirectUri": f"{base_url}/_gcp_iap/clear_login_cookie",
                "isOAuthConfigured": False,
                "isIap": True,
                "notice": "OAuth 2.0 Client credentials not configured. Using IAP account switch.",
            }
        )
    if os.environ.get("K_SERVICE"):
        return JSONResponse(
            {
                "error": (
                    "OAuth 2.0 Web Client credentials (GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET) "
                    "are not configured on this Cloud Run service. Run ./scripts/setup_oauth_cloudrun.sh "
                    "or paste your personal OAuth access token in the dialog."
                ),
                "isOAuthConfigured": False,
                "needsTokenModal": True,
            },
            status_code=400,
        )

    _verify_auth_url_request_origin(request, require_origin=True)
    return await _perform_local_gcloud_browser_login(request, account, redirect_uri, base_url)


_OIDC_CALLBACK_HTML_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>Authentication Successful</title>
  <style>
    body {{
      margin: 0;
      background: #0b0f19;
      color: #e2e8f0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      display: grid;
      place-items: center;
      min-height: 100vh;
    }}
    .auth-card {{
      text-align: center;
      padding: 2.5rem 2rem;
      border-radius: 20px;
      background: rgba(30, 41, 59, 0.85);
      border: 1px solid rgba(255, 255, 255, 0.12);
      box-shadow: 0 12px 32px rgba(0, 0, 0, 0.5);
      max-width: 420px;
    }}
    .spinner {{
      width: 42px;
      height: 42px;
      border: 3px solid rgba(59, 130, 246, 0.2);
      border-top-color: #3b82f6;
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
      margin: 0 auto 1.25rem;
    }}
    @keyframes spin {{
      to {{ transform: rotate(360deg); }}
    }}
    h3 {{ margin: 0 0 0.5rem; font-size: 1.25rem; }}
    p {{ margin: 0; color: #94a3b8; font-size: 0.95rem; line-height: 1.4; }}
  </style>
</head>
<body>
  <div class="auth-card">
    <div class="spinner"></div>
    <h3>Signed in as {safe_email}</h3>
    <p>Connecting to your Apigee organizations...</p>
  </div>
  <script nonce="{script_nonce}">
    try {{
      localStorage.removeItem('apigee_user_access_token');
      sessionStorage.removeItem('apigee_user_access_token');
    }} catch (e) {{
      console.warn('storage error', e);
    }}
    window.location.replace('/?authenticated=1');
  </script>
</body>
</html>"""


def _render_oidc_callback_html(email: str, script_nonce: str) -> str:
    """Render secure HttpOnly-cookie redirect template for OAuth callback without exposing tokens to JS."""
    return _OIDC_CALLBACK_HTML_TEMPLATE.format(
        safe_email=html.escape(email),
        script_nonce=html.escape(script_nonce, quote=True),
    )


def _attach_oidc_cookies(
    response: Response,
    session_id: str,
    access_token: str,
    refresh_token: str,
    is_https: bool,
    access_max_age: int = 3600,
) -> None:
    """Attach secure HTTP-only session cookies to OAuth redirect response."""
    if session_id:
        response.set_cookie(
            key="apigee_session_id",
            value=session_id,
            max_age=180 * 86400,
            httponly=True,
            secure=is_https,
            samesite="lax",
        )
    if access_token:
        response.set_cookie(
            key="apigee_access_token",
            value=access_token,
            max_age=max(60, int(access_max_age)),
            httponly=True,
            secure=is_https,
            samesite="lax",
        )
    if refresh_token:
        response.set_cookie(
            key="apigee_refresh_token",
            value=refresh_token,
            max_age=180 * 86400,
            httponly=True,
            secure=is_https,
            samesite="lax",
        )
    response.delete_cookie(key="apigee_oidc_state")


def _build_oidc_response(
    request: Request,
    session: dict[str, Any],
) -> Response:
    """Create HTML response with rotated HttpOnly session cookies from successful OIDC exchange."""
    email = str(session.get("email") or f"anon-{secrets.token_hex(6)}")
    access_token = str(session.get("accessToken") or "")
    refresh_token = str(session.get("refreshToken") or "")
    exp_sec = int(float(session.get("expiresIn") or 3600))

    session_id, _ = _rotate_session_id(request)
    set_user_session(session_id, session)
    _set_verified_cloud_run_scope(email)

    if email and not os.environ.get("K_SERVICE"):
        cfg = load_config()
        cfg["gcpAccount"] = email
        save_config(cfg)

    script_nonce = secrets.token_urlsafe(16)
    resp = HTMLResponse(
        content=_render_oidc_callback_html(email, script_nonce),
        headers={"Content-Security-Policy": _build_csp_policy(script_nonce)},
    )
    scheme, _ = _resolve_trusted_host_and_proto(request)
    _attach_oidc_cookies(resp, session_id, access_token, refresh_token, scheme == "https", exp_sec)
    return resp


@app.get("/api/auth/oidc/callback")
async def api_oidc_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> Response:
    """Handle Google OIDC OAuth 2.0 redirect callback, exchange authorization code, and persist tokens."""
    if error or not code:
        err_val = urllib.parse.quote(str(error or "no_code"), safe="")
        return RedirectResponse(url=f"/?oidc_error={err_val}")

    expected_state = request.cookies.get("apigee_oidc_state")
    if not state or not expected_state or not secrets.compare_digest(state, expected_state):
        return RedirectResponse(url="/?oidc_error=invalid_state")

    _, redirect_uri = _resolve_redirect_endpoints(request)
    try:
        session = await asyncio.to_thread(
            exchange_google_oidc_code,
            code=code,
            redirect_uri=redirect_uri,
        )
        return _build_oidc_response(request, session)
    except Exception as exc:
        logger.warning("OIDC code exchange failed: %s", exc)
        err_msg = urllib.parse.quote(str(exc))
        return RedirectResponse(url=f"/?oidc_error={err_msg}")


def _purge_discovered_orgs_cache(prev_jur: str, new_jur: str) -> None:
    """Remove cached discovered organization files when switching jurisdictions."""
    if prev_jur == new_jur:
        return
    for candidate in (DISCOVERED_ORGS_PATH, get_discovered_orgs_path(prev_jur), get_discovered_orgs_path(new_jur)):
        try:
            candidate.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("Failed to remove cached discovered orgs %s: %s", candidate, exc)


@app.post("/api/auth/oidc/token")
async def api_oidc_set_token(request: Request, payload: OidcTokenRequest) -> JSONResponse:
    """Register and validate a Google OAuth 2.0 access token via Google REST APIs (zero CLI)."""
    target_account = get_request_user_account(request, payload.email)
    try:
        info = await asyncio.to_thread(
            switch_or_login_gcp_user,
            account=target_account,
            oidc_token=payload.accessToken,
        )
        session_id, prior_sess = _rotate_session_id(request)
        exp_sec = float(info.get("expiresIn") or 3600)
        resolved_email = str(info.get("activeAccount") or f"anon-{secrets.token_hex(6)}")
        prior_refresh = str(prior_sess.get("refreshToken") or "") if prior_sess.get("email") == resolved_email else ""
        set_user_session(session_id, {
            "email": resolved_email,
            "accessToken": payload.accessToken.strip(),
            "refreshToken": prior_refresh,
            "expiresIn": exp_sec,
            "expiresAt": time.time() + max(60.0, exp_sec),
            "authenticated": True,
        })
        _set_verified_cloud_run_scope(resolved_email)
        if not os.environ.get("K_SERVICE"):
            cfg = load_config()
            if info.get("activeAccount"):
                cfg["gcpAccount"] = info.get("activeAccount")
                save_config(cfg)
        info["hasUserToken"] = True
        resp = JSONResponse(info)
        scheme, _ = _resolve_trusted_host_and_proto(request)
        _attach_oidc_cookies(resp, session_id, payload.accessToken.strip(), "", scheme == "https", int(exp_sec))
        return resp
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _purge_user_cloud_run_caches() -> None:
    """Remove per-user tmpfs cache files on Cloud Run when a user logs out."""
    suffix = _get_cache_user_suffix()
    if not suffix or not DATA_DIR.exists():
        return
    for p in DATA_DIR.glob(f"*{suffix}.json"):
        p.unlink(missing_ok=True)


@app.post("/api/auth/oidc/logout")
async def api_oidc_logout(request: Request) -> JSONResponse:
    """Clear the active Google OIDC session and cookies."""
    session_id = request.cookies.get("apigee_session_id")
    if os.environ.get("K_SERVICE"):
        _purge_user_cloud_run_caches()
    if session_id:
        clear_user_session(session_id)
    if not os.environ.get("K_SERVICE"):
        clear_active_oidc_session()
    resp = JSONResponse(
        {"success": True, "message": "OIDC session cleared"},
        headers={"Clear-Site-Data": '"cookies"'},
    )
    resp.delete_cookie("apigee_session_id")
    resp.delete_cookie("apigee_access_token")
    resp.delete_cookie("apigee_refresh_token")
    return resp


@app.get("/api/jurisdictions")
async def api_get_jurisdictions(request: Request) -> JSONResponse:
    """Return list of supported Apigee Control Plane Hosting Jurisdictions and the active one."""
    cur_code = _resolve_request_jurisdiction(request)
    return JSONResponse({
        "current": resolve_jurisdiction_info(cur_code) or get_active_jurisdiction(),
        "jurisdictions": list(APIGEE_JURISDICTIONS.values()),
    })


async def _is_request_authenticated(request: Request) -> bool:
    """Return True when the incoming HTTP request carries verified authentication credentials."""
    try:
        await _verify_request_authenticated(request)
        return True
    except HTTPException:
        return False


def _persist_authenticated_jurisdiction(prev_jur: str, new_code: str) -> None:
    """Persist jurisdiction to config and purge stale discovered-org cache for an authenticated user."""
    if not os.environ.get("K_SERVICE"):
        set_active_jurisdiction(new_code)
    elif not _get_cache_user_suffix():
        return
    cfg = load_config()
    cfg["controlPlaneJurisdiction"] = new_code
    save_config(cfg)
    _purge_discovered_orgs_cache(prev_jur, new_code)


@app.post("/api/jurisdiction")
async def api_set_jurisdiction(request: Request, payload: JurisdictionUpdateRequest) -> JSONResponse:
    """Set the active Apigee Control Plane Hosting Jurisdiction."""
    jur_info = resolve_jurisdiction_info(payload.jurisdiction)
    if jur_info is None:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid jurisdiction '{payload.jurisdiction}'. Must be one of: {', '.join(APIGEE_JURISDICTIONS.keys())}",
        )
    if await _is_request_authenticated(request):
        prev_jur = _resolve_request_jurisdiction(request)
        _persist_authenticated_jurisdiction(prev_jur, str(jur_info["code"]))

    resp = JSONResponse({
        "status": "success",
        "jurisdiction": jur_info,
        "jurisdictions": list(APIGEE_JURISDICTIONS.values()),
    })
    scheme, _ = _resolve_trusted_host_and_proto(request)
    resp.set_cookie(
        key="apigee_jurisdiction",
        value=jur_info["code"],
        max_age=180 * 86400,
        httponly=False,
        secure=(scheme == "https"),
        samesite="lax",
    )
    return resp


def _switch_jurisdiction_on_login(jurisdiction: str | None) -> None:
    """Update active jurisdiction and clear cached discovered organizations after authenticated login."""
    if not jurisdiction or os.environ.get("K_SERVICE"):
        return
    prev_jur = get_active_jurisdiction()["code"]
    jur_info = set_active_jurisdiction(jurisdiction)
    if jur_info is None:
        return
    cfg = load_config()
    cfg["controlPlaneJurisdiction"] = jur_info["code"]
    save_config(cfg)
    _purge_discovered_orgs_cache(prev_jur, jur_info["code"])


async def _handle_token_login(
    request: Request,
    user_token: str,
    user_account: str | None,
) -> JSONResponse:
    """Authenticate or switch session using an explicit OAuth bearer token."""
    try:
        info = await asyncio.to_thread(
            switch_or_login_gcp_user,
            account=user_account,
            oidc_token=user_token,
        )
        session_id, prior_sess = _rotate_session_id(request)
        exp_sec = float(info.get("expiresIn") or 3600)
        resolved_email = str(info.get("activeAccount") or f"anon-{secrets.token_hex(6)}")
        keep_refresh = (
            str(prior_sess.get("refreshToken") or request.cookies.get("apigee_refresh_token") or "")
            if (not prior_sess.get("email") or prior_sess.get("email") == resolved_email)
            else ""
        )
        set_user_session(session_id, {
            "email": resolved_email,
            "accessToken": user_token.strip(),
            "refreshToken": keep_refresh,
            "expiresIn": exp_sec,
            "expiresAt": time.time() + max(60.0, exp_sec),
            "authenticated": True,
        })
        _set_verified_cloud_run_scope(resolved_email)
        if not os.environ.get("K_SERVICE"):
            cfg = load_config()
            cfg["gcpAccount"] = info.get("activeAccount")
            save_config(cfg)
        info["hasUserToken"] = True
        resp = JSONResponse(info)
        scheme, _ = _resolve_trusted_host_and_proto(request)
        _attach_oidc_cookies(resp, session_id, user_token.strip(), keep_refresh, scheme == "https", int(exp_sec))
        return resp
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


async def _handle_existing_live_session_login(request: Request, session_id: str) -> JSONResponse:
    """Return active OIDC session info without overwriting refreshToken or corrupting expiresAt."""
    user_sess = get_user_session(session_id)
    cur_jur = _resolve_request_jurisdiction(request)
    info = await asyncio.to_thread(get_gcp_user_auth_info, user_session=user_sess, jurisdiction=cur_jur)
    _apply_iap_identity(request, info)
    info["hasUserToken"] = bool(user_sess.get("accessToken") or info.get("hasUserToken"))
    return JSONResponse(info)


def _handle_account_only_login(
    request: Request,
    user_account: str,
) -> JSONResponse:
    """Return auth info for a known user account, honoring IAP or local workstation sessions."""
    session_id = request.cookies.get("apigee_session_id", "").strip()
    user_sess = get_user_session(session_id) if session_id else {}
    cur_jur = _resolve_request_jurisdiction(request)
    info = get_gcp_user_auth_info(user_session=user_sess if user_sess else None, jurisdiction=cur_jur)
    _apply_iap_identity(request, info)
    if user_sess.get("accessToken"):
        info["hasUserToken"] = True
        return JSONResponse(info)
    if not os.environ.get("K_SERVICE") and _is_loopback_workstation_request(request) and info.get("authenticated") and info.get("hasUserToken"):
        resp = JSONResponse(info)
        _attach_local_workstation_session_cookie(request, resp, info)
        return resp
    if info.get("isIap"):
        return JSONResponse(info)
    info["authenticated"] = True
    info["activeAccount"] = user_account
    info["hasUserToken"] = False
    info["needsUserToken"] = True
    return JSONResponse(info)


async def _handle_browser_or_default_login(
    request: Request,
    payload: AuthLoginRequest,
    redirect_uri: str,
) -> JSONResponse:
    """Execute standard gcloud/OIDC login or interactive browser authentication."""
    try:
        info = await asyncio.to_thread(
            switch_or_login_gcp_user,
            account=payload.account,
            trigger_browser_login=payload.triggerBrowserLogin,
            oidc_token=payload.accessToken,
            redirect_uri=redirect_uri,
        )
        if not os.environ.get("K_SERVICE") and _is_loopback_workstation_request(request):
            cfg = load_config()
            cfg["gcpAccount"] = info.get("activeAccount")
            save_config(cfg)
        resp = JSONResponse(info)
        _attach_local_workstation_session_cookie(request, resp, info)
        return resp
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


async def _dispatch_auth_login(
    request: Request,
    payload: AuthLoginRequest,
    redirect_uri: str,
) -> JSONResponse:
    """Dispatch POST /api/auth/login to live-session, token, account, or workstation handler."""
    session_id = request.cookies.get("apigee_session_id", "").strip()
    if (
        not payload.accessToken
        and not payload.triggerBrowserLogin
        and _is_live_user_session(session_id)
        and get_user_session(session_id).get("accessToken")
    ):
        return await _handle_existing_live_session_login(request, session_id)

    user_token = extract_bearer_token(request, payload.accessToken)
    user_account = get_request_user_account(request, payload.account)

    if user_token:
        return await _handle_token_login(request, user_token, user_account)
    if user_account:
        return await asyncio.to_thread(_handle_account_only_login, request, user_account)
    return await _handle_browser_or_default_login(request, payload, redirect_uri)


@app.post("/api/auth/login")
async def api_auth_login(request: Request, payload: AuthLoginRequest) -> JSONResponse:
    """Switch account or initiate Google OIDC login without requiring local gcloud CLI."""
    _validate_explicit_jurisdiction_param(payload.jurisdiction)
    _, redirect_uri = _resolve_redirect_endpoints(request)
    resp = await _dispatch_auth_login(request, payload, redirect_uri)
    minted_cookie = "apigee_session_id=" in ";".join(resp.headers.getlist("set-cookie"))
    if minted_cookie or await _is_request_authenticated(request):
        _switch_jurisdiction_on_login(payload.jurisdiction)
    return resp


def _read_cached_discovered_orgs(
    disc_path: pathlib.Path,
    cur_jur_code: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Read cached discovered organizations file for the specified jurisdiction."""
    if not disc_path.exists():
        return None, []
    try:
        cached = json.loads(disc_path.read_text(encoding="utf-8"))
        if cached.get("controlPlaneJurisdiction") == cur_jur_code:
            return cached, cached.get("organizations", [])
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Failed to load cached discovered orgs from %s: %s", disc_path, exc)
    return None, []


@app.get("/api/organizations/discover")
async def api_discover_organizations(
    request: Request,
    refresh: bool = Query(default=True, description="Query live GET /v1/organizations"),
    account: str | None = Query(default=None, description="Optional GCP user account email"),
    jurisdiction: str | None = Query(default=None, description="Control plane hosting jurisdiction"),
) -> JSONResponse:
    """Discover all authorized Apigee organizations for the current user and jurisdiction."""
    _validate_explicit_jurisdiction_param(jurisdiction)
    await _verify_request_authenticated(request)
    cur_jur_code = _resolve_request_jurisdiction(request, jurisdiction)
    disc_path = get_discovered_orgs_path(cur_jur_code)
    cached, existing_orgs = _read_cached_discovered_orgs(disc_path, cur_jur_code)
    if cached and not refresh:
        return JSONResponse(cached)

    user_token = extract_bearer_token(request)
    user_account = get_request_user_account(request, account)
    try:
        discovered = await discover_authorized_organizations(
            account=user_account,
            explicit_token=user_token,
            jurisdiction=cur_jur_code,
        )
        discovered["controlPlaneJurisdiction"] = cur_jur_code
        discovered["jurisdiction"] = resolve_jurisdiction_info(cur_jur_code) or get_active_jurisdiction()
        live_orgs = sorted(discovered.get("organizations", []), key=lambda x: x["organization"].lower())
        discovered["organizations"] = live_orgs
        discovered["totalAuthorizedOrgs"] = len(live_orgs)
        _atomic_write_json(disc_path, discovered)
        return JSONResponse(discovered)
    except Exception as exc:
        if existing_orgs:
            return JSONResponse({
                "organizations": existing_orgs,
                "totalAuthorizedOrgs": len(existing_orgs),
                "controlPlaneJurisdiction": cur_jur_code,
            })
        raise HTTPException(status_code=400, detail=str(exc))


async def _build_request_auth_snapshot(request: Request, cur_jur_code: str) -> dict[str, Any]:
    """Assemble authenticated user identity snapshot scoped to the current HTTP request."""
    session_id = request.cookies.get("apigee_session_id", "").strip()
    user_sess = get_user_session(session_id) if session_id else {}
    user_token = extract_bearer_token(request)
    user_account = get_request_user_account(request)
    auth_info = await asyncio.to_thread(
        get_gcp_user_auth_info,
        explicit_token=user_token,
        explicit_account=user_account,
        user_session=user_sess if user_sess else None,
        jurisdiction=cur_jur_code,
    )
    if user_account:
        auth_info["activeAccount"] = user_account
        auth_info["authSource"] = user_account
        auth_info["authenticated"] = True
    auth_info["hasUserToken"] = bool(user_token or auth_info.get("hasUserToken"))
    return auth_info


@app.get("/api/portfolio")
async def api_get_portfolio(
    request: Request,
    jurisdiction: str | None = Query(default=None, description="Apigee control plane jurisdiction"),
) -> JSONResponse:
    """Retrieve recalculated portfolio summary, inventory breakdown, and current user auth info."""
    _validate_explicit_jurisdiction_param(jurisdiction)
    await _verify_request_authenticated(request)
    cur_jur_code = _resolve_request_jurisdiction(request, jurisdiction)
    auth_snapshot = await _build_request_auth_snapshot(request, cur_jur_code)
    portfolio = get_current_portfolio(cur_jur_code, auth_override=auth_snapshot)
    return JSONResponse(portfolio)


def _compute_ts_cache_path(
    cur_jur_code: str,
    dim: str,
    days: int,
    contract_start_date: str | None,
) -> pathlib.Path:
    """Generate cache file path for Cloud Monitoring timeseries based on dimension and lookback."""
    safe_jur = _normalize_valid_jurisdiction(cur_jur_code)
    raw_dt = str(contract_start_date or "default")
    contract_dt = "".join(ch for ch in raw_dt if ch.isalnum()) or "default"
    cache_suffix = f"_{contract_dt}" if dim == "calls" else f"_{int(days)}d"
    return DATA_DIR / f"monitoring_ts_{safe_jur}_{dim}{cache_suffix}{_get_cache_user_suffix()}.json"


def _resolve_raw_file_for_jur(cur_jur_code: str) -> pathlib.Path | None:
    """Locate existing raw cache file path for a jurisdiction, if present on disk."""
    raw_path = get_raw_cache_path(cur_jur_code)
    if raw_path.exists():
        return raw_path
    if cur_jur_code == "global" and RAW_CACHE_PATH.exists():
        return RAW_CACHE_PATH
    return None


def _get_excluded_billing_orgs_for_jur(cur_jur_code: str) -> set[str]:
    """Return set of excluded Trial/PAYG organization names from raw cache for jurisdiction."""
    raw_path = _resolve_raw_file_for_jur(cur_jur_code)
    if raw_path is None:
        return set()
    try:
        raw_obj = json.loads(raw_path.read_text(encoding="utf-8"))
        _, newly_excluded = filter_subscription_organizations(raw_obj.get("organizations") or [])
        return set(raw_obj.get("excludedBillingOrganizations") or []) | set(newly_excluded)
    except (OSError, json.JSONDecodeError):
        return set()


def _purge_jurisdiction_ts_caches(cur_jur_code: str | None = None, *, calls_only: bool = False) -> None:
    """Delete cached timeseries JSON files for a jurisdiction (or all jurisdictions when cur_jur_code is None)."""
    if not DATA_DIR.exists():
        return
    suffix = _get_cache_user_suffix()
    jur_tok = _normalize_valid_jurisdiction(cur_jur_code) if cur_jur_code else "*"
    dim_tok = "calls" if calls_only else "*"
    pattern = f"monitoring_ts_{jur_tok}_{dim_tok}*{suffix}.json" if suffix else f"monitoring_ts_{jur_tok}_{dim_tok}*.json"
    for ts_cache in DATA_DIR.glob(pattern):
        ts_cache.unlink(missing_ok=True)


def _is_ts_cache_fresh(cached: Mapping[str, Any]) -> bool:
    """Return False if cached timeseries has exceeded _TS_CACHE_TTL_SECONDS."""
    fetched_raw = str(cached.get("fetchedAt") or "").strip()
    if not fetched_raw:
        return True
    try:
        dt = datetime.datetime.fromisoformat(fetched_raw.replace("Z", "+00:00"))
        age = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds()
        return age <= _TS_CACHE_TTL_SECONDS
    except ValueError:
        return True


def _read_cached_timeseries(cache_file: pathlib.Path, cur_jur_code: str) -> dict[str, Any] | None:
    """Read cached timeseries data if present, fresh, and matching current jurisdiction."""
    if not cache_file.exists():
        return None
    try:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if cached.get("controlPlaneJurisdiction", "global") == cur_jur_code and _is_ts_cache_fresh(cached):
            excluded_orgs = _get_excluded_billing_orgs_for_jur(cur_jur_code)
            if excluded_orgs and isinstance(cached.get("series"), list):
                filtered_series = [
                    s for s in cached["series"] if s.get("organization") not in excluded_orgs
                ]
                cached = {**cached, "series": filtered_series, "seriesCount": len(filtered_series)}
            return cached
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Failed to read timeseries cache at %s: %s", cache_file, exc)
    return None


@app.get("/api/monitoring/timeseries")
async def api_get_monitoring_timeseries(
    request: Request,
    days: int = Query(default=14, ge=1, le=90, description="Lookback window in days"),
    dimension: str = Query(default="pdu", description="Dimension: pdu | environments | calls"),
    refresh: bool = Query(default=False, description="Force live query to Cloud Monitoring / Apigee Analytics API"),
    jurisdiction: str | None = Query(default=None, description="Apigee control plane jurisdiction"),
) -> JSONResponse:
    """Query Cloud Monitoring and Apigee Analytics for PDU, Environment Unit, or Call timeseries."""
    _validate_explicit_jurisdiction_param(jurisdiction)
    dim = (dimension or "pdu").strip().lower()
    if dim not in {"pdu", "environments", "calls"}:
        raise HTTPException(
            status_code=400,
            detail="Invalid dimension. Must be one of: pdu, environments, calls",
        )
    await _verify_request_authenticated(request)
    cur_jur_code = _resolve_request_jurisdiction(request, jurisdiction)
    cfg = load_config()
    cache_file = _compute_ts_cache_path(cur_jur_code, dim, days, cfg.get("contractStartDate"))
    existing_cached = _read_cached_timeseries(cache_file, cur_jur_code)
    if existing_cached and not refresh:
        return JSONResponse(existing_cached)

    raw = load_raw_dataset(cur_jur_code)
    user_token = extract_bearer_token(request)
    user_account = get_request_user_account(request)
    try:
        ts_data = await fetch_monitoring_pdu_timeseries(
            raw_data=raw,
            days=days,
            account=user_account,
            explicit_token=user_token,
            dimension=dim,
            contract_start_date=cfg.get("contractStartDate"),
            jurisdiction=cur_jur_code,
            force_refresh=refresh,
        )
        ts_data["controlPlaneJurisdiction"] = cur_jur_code
        if dim == "calls" and (refresh or ts_data.get("callsRefreshed")):
            save_raw_dataset(raw, jurisdiction_code=cur_jur_code)
        _atomic_write_json(cache_file, ts_data)
        return JSONResponse(ts_data)
    except Exception as exc:
        if existing_cached:
            return JSONResponse(existing_cached)
        raise HTTPException(status_code=400, detail=str(exc))


def _validate_and_apply_entitlements(
    payload: SettingsUpdateRequest,
    cfg: dict[str, Any],
) -> None:
    """Validate entitlement thresholds and update configuration values."""
    if payload.entitlementPdu is not None:
        if payload.entitlementPdu < 1:
            raise HTTPException(status_code=400, detail="Entitlement PDU must be >= 1")
        cfg["entitlementPdu"] = int(payload.entitlementPdu)

    if payload.entitlementEnvs is not None:
        if payload.entitlementEnvs < 1:
            raise HTTPException(status_code=400, detail="Entitlement Environments must be >= 1")
        cfg["entitlementEnvs"] = int(payload.entitlementEnvs)

    if payload.entitlementYearlyCalls is not None:
        if payload.entitlementYearlyCalls < 1:
            raise HTTPException(status_code=400, detail="Entitlement Yearly API Calls must be >= 1")
        cfg["entitlementYearlyCalls"] = int(payload.entitlementYearlyCalls)


def _apply_region_override_updates(
    payload: SettingsUpdateRequest,
    cfg: dict[str, Any],
) -> None:
    """Update region overrides dictionary according to reset or selective update flags."""
    if payload.resetOverrides:
        cfg["regionOverrides"] = {}
    elif payload.regionOverrides is not None:
        merged = dict(cfg.get("regionOverrides", {}))
        for k, v in payload.regionOverrides.items():
            if v is None or v < 0:
                merged.pop(k, None)
            else:
                merged[k] = int(v)
        cfg["regionOverrides"] = merged


_IAP_MISSING_AUD_WARNED: list[bool] = []


def _log_missing_iap_audience_once() -> None:
    """Log missing IAP_AUDIENCE notice once per process instead of spamming every static request."""
    if not _IAP_MISSING_AUD_WARNED:
        _IAP_MISSING_AUD_WARNED.append(True)
        logger.info("IAP headers detected without IAP_AUDIENCE configured; falling back to OAuth session auth.")


def _validate_iap_jwt(iap_jwt: str | None, expected_email: str | None = None) -> bool:
    """Cryptographically validate Google Cloud IAP JWT assertion with mandatory audience."""
    if not iap_jwt or not iap_jwt.strip():
        return False
    expected_aud = (os.environ.get("IAP_AUDIENCE") or "").strip()
    if not expected_aud:
        _log_missing_iap_audience_once()
        return False
    clean_jwt = iap_jwt.strip()
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        decoded = id_token.verify_token(
            clean_jwt,
            google_requests.Request(),
            audience=expected_aud,
            certs_url="https://www.gstatic.com/iap/verify/public_key-jwk",
        )
        if expected_email and expected_email.strip():
            jwt_email = str(decoded.get("email", "")).strip().lower()
            clean_expected = (
                expected_email.split(":")[-1].strip().lower()
                if ":" in expected_email
                else expected_email.strip().lower()
            )
            if jwt_email != clean_expected:
                return False
        return True
    except Exception as exc:
        logger.debug("IAP JWT validation failed: %s", exc)
        return False


def _try_refresh_expired_session(session_id: str, user_sess: dict[str, Any]) -> bool:
    """Attempt refreshing an expired session using its stored OAuth refresh token."""
    refresh_tok = user_sess.get("refreshToken")
    if not refresh_tok:
        return False
    try:
        refreshed_data = refresh_google_oauth_token(str(refresh_tok))
        if refreshed_data and refreshed_data.get("access_token"):
            user_sess["accessToken"] = refreshed_data["access_token"]
            exp_in = float(refreshed_data.get("expires_in", 3600))
            user_sess["expiresIn"] = exp_in
            user_sess["expiresAt"] = time.time() + exp_in
            set_user_session(session_id, user_sess)
            return True
    except Exception as exc:
        logger.debug("Failed refreshing session token: %s", exc)
    return False


def _rehydrate_session_via_refresh_cookie(session_id: str, refresh_tok: str) -> str | None:
    """Attempt rehydrating a missing/expired session from an HttpOnly refresh token cookie."""
    if not refresh_tok:
        return None
    refreshed = refresh_google_oauth_token(refresh_tok)
    new_tok = str((refreshed or {}).get("access_token") or "").strip()
    if not new_tok:
        return None
    exp_sec = float((refreshed or {}).get("expires_in") or 3600)
    try:
        tok_info = validate_google_oauth_token(new_tok)
        ident = resolve_verified_subject_identity(tok_info, new_tok)
    except ValueError:
        ident = resolve_verified_subject_identity({}, new_tok)
    set_user_session(session_id, {
        "email": ident,
        "accessToken": new_tok,
        "refreshToken": refresh_tok,
        "expiresIn": exp_sec,
        "expiresAt": time.time() + max(60.0, exp_sec),
        "authenticated": True,
    })
    return ident


def _verify_and_resolve_token_identity(candidate_token: str, session_id: str = "", refresh_tok: str = "") -> str:
    """Validate an explicit Bearer or cookie OAuth access token and return verified identity."""
    matched_sess = get_session_for_active_token(candidate_token)
    if matched_sess is not None:
        ident = str(
            matched_sess.get("email")
            or matched_sess.get("account")
            or resolve_verified_subject_identity({}, candidate_token)
        )
        if session_id:
            rebound = dict(matched_sess)
            rebound["email"] = ident
            if refresh_tok:
                rebound["refreshToken"] = refresh_tok
            set_user_session(session_id, rebound)
        return ident
    try:
        tok_info = validate_google_oauth_token(candidate_token)
    except Exception as exc:
        logger.debug("Bearer token validation failed: %s", exc)
        raise HTTPException(
            status_code=401,
            detail="Authentication required: invalid or expired authentication token",
        ) from exc
    exp_sec = float(tok_info.get("expiresIn") or 3600)
    ident = resolve_verified_subject_identity(tok_info, candidate_token)
    if session_id:
        set_user_session(session_id, {
            "email": ident,
            "accessToken": candidate_token,
            "refreshToken": refresh_tok,
            "expiresIn": exp_sec,
            "expiresAt": time.time() + max(60.0, exp_sec),
            "authenticated": True,
        })
    return ident


def _resolve_active_session_ident(session_id: str, user_sess: dict[str, Any]) -> str | None:
    """Return verified session identity if non-expired or refreshable in-place."""
    if not user_sess:
        return None
    expires_at = user_sess.get("expiresAt")
    is_fresh = not expires_at or time.time() < float(expires_at)
    if is_fresh or _try_refresh_expired_session(session_id, user_sess):
        return str(user_sess.get("email") or user_sess.get("account") or f"sid:{session_id}")
    return None


def _verify_or_rehydrate_session_sync(
    session_id: str,
    candidate_token: str,
    cookie_refresh: str,
) -> str:
    """Validate an apigee_session_id cookie or rehydrate it on scale-out from valid tokens."""
    user_sess = get_user_session(session_id)
    live_ident = _resolve_active_session_ident(session_id, user_sess)
    if live_ident:
        return live_ident

    rehydrated = _rehydrate_session_via_refresh_cookie(session_id, cookie_refresh)
    if rehydrated:
        return rehydrated
    if candidate_token and candidate_token != str(user_sess.get("accessToken") or ""):
        return _verify_and_resolve_token_identity(candidate_token, session_id=session_id, refresh_tok=cookie_refresh)

    detail = (
        "Authentication session expired. Please re-authenticate."
        if user_sess
        else "Authentication required: invalid or forged session"
    )
    raise HTTPException(status_code=401, detail=detail)


def _extract_iap_headers(request: Request) -> tuple[str | None, str | None]:
    """Extract IAP email and JWT assertion headers case-insensitively."""
    iap_user = request.headers.get("x-goog-authenticated-user-email") or request.headers.get(
        "X-Goog-Authenticated-User-Email"
    )
    iap_jwt = request.headers.get("x-goog-iap-jwt-assertion") or request.headers.get(
        "X-Goog-IAP-JWT-Assertion"
    )
    return iap_user, iap_jwt


async def _try_verify_iap_headers(iap_user: str | None, iap_jwt: str | None) -> bool:
    """Verify IAP JWT assertion off the event loop and set Cloud Run scope when valid."""
    if not await asyncio.to_thread(_validate_iap_jwt, iap_jwt, iap_user):
        return False
    raw_email = iap_user or ""
    clean_iap = raw_email.split(":")[-1].strip() if ":" in raw_email else raw_email.strip()
    _set_verified_cloud_run_scope(f"iap:{clean_iap}")
    return True


async def _verify_request_authenticated(request: Request) -> None:
    """Verify caller authentication via Google IAP, OAuth Bearer token, or valid/rehydrated session."""
    iap_user, iap_jwt = _extract_iap_headers(request)
    has_iap_headers = bool(iap_user or iap_jwt)
    if has_iap_headers and await _try_verify_iap_headers(iap_user, iap_jwt):
        return

    auth_header = request.headers.get("Authorization", "").strip()
    bearer_tok = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
    candidate_token = bearer_tok or request.cookies.get("apigee_access_token", "").strip()
    cookie_refresh = request.cookies.get("apigee_refresh_token", "").strip()
    session_id = request.cookies.get("apigee_session_id", "").strip()

    if session_id:
        ident = await asyncio.to_thread(
            _verify_or_rehydrate_session_sync, session_id, candidate_token, cookie_refresh
        )
        _set_verified_cloud_run_scope(ident)
        return

    if candidate_token:
        ident = await asyncio.to_thread(_verify_and_resolve_token_identity, candidate_token)
        _set_verified_cloud_run_scope(ident)
        return

    detail = (
        "Authentication required: invalid or unverified IAP credentials"
        if has_iap_headers
        else "Authentication required: missing authentication credentials"
    )
    raise HTTPException(status_code=401, detail=detail)


def _validate_iso_date_str(raw_date: str) -> str:
    """Validate strict YYYY-MM-DD calendar date string or raise HTTP 400."""
    cleaned = raw_date.strip()
    if len(cleaned) != 10:
        raise HTTPException(status_code=400, detail="Invalid contractStartDate format. Expected YYYY-MM-DD.")
    try:
        return datetime.date.fromisoformat(cleaned).isoformat()
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail="Invalid contractStartDate calendar date. Expected YYYY-MM-DD."
        ) from exc


async def _refresh_calls_for_contract_date(
    request: Request,
    cfg: dict[str, Any],
    cur_jur_code: str,
) -> None:
    """Re-query Apigee Analytics for the target jurisdiction when contractStartDate changes."""
    raw = load_raw_dataset(cur_jur_code)
    user_token = extract_bearer_token(request)
    user_account = get_request_user_account(request) or cfg.get("gcpAccount")
    await populate_contract_api_calls(
        raw,
        contract_start_date=cfg.get("contractStartDate"),
        account=user_account,
        explicit_token=user_token,
        jurisdiction=cur_jur_code,
    )
    save_raw_dataset(raw, jurisdiction_code=cur_jur_code)
    _purge_jurisdiction_ts_caches(None, calls_only=True)


@app.post("/api/settings")
async def api_update_settings(
    request: Request,
    payload: SettingsUpdateRequest,
    jurisdiction: str | None = Query(default=None),
) -> JSONResponse:
    """Update cross-organization entitlement parameters, contract date, and region overrides."""
    _validate_explicit_jurisdiction_param(payload.jurisdiction or jurisdiction)
    await _verify_request_authenticated(request)
    cur_jur_code = _resolve_request_jurisdiction(request, payload.jurisdiction or jurisdiction)
    cfg = load_config()
    _validate_and_apply_entitlements(payload, cfg)

    contract_date_changed = False
    if payload.contractStartDate is not None and payload.contractStartDate.strip():
        new_dt = _validate_iso_date_str(payload.contractStartDate)
        contract_date_changed = new_dt != cfg.get("contractStartDate")
        cfg["contractStartDate"] = new_dt

    if payload.includeSharedFlows is not None:
        cfg["includeSharedFlows"] = bool(payload.includeSharedFlows)

    _apply_region_override_updates(payload, cfg)
    save_config(cfg)

    if contract_date_changed:
        await _refresh_calls_for_contract_date(request, cfg, cur_jur_code)

    auth_snapshot = await _build_request_auth_snapshot(request, cur_jur_code)
    portfolio = get_current_portfolio(cur_jur_code, auth_override=auth_snapshot)
    return JSONResponse(portfolio)


@app.post("/api/scan")
async def api_trigger_live_scan(request: Request, payload: LiveScanRequest) -> JSONResponse:
    """Trigger a live audit of all authorized Apigee organizations and environments."""
    _validate_explicit_jurisdiction_param(payload.jurisdiction)
    await _verify_request_authenticated(request)
    cur_jur_code = _resolve_request_jurisdiction(request, payload.jurisdiction)
    cfg = load_config()
    user_token = extract_bearer_token(request, payload.accessToken)
    user_account = get_request_user_account(request, payload.account)
    try:
        raw_data = await collect_all_organizations(
            explicit_token=user_token,
            org_filter=payload.orgFilter,
            hybrid_region_overrides=cfg.get("regionOverrides", {}),
            account=user_account,
            contract_start_date=cfg.get("contractStartDate"),
            jurisdiction=cur_jur_code,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    cfg = load_config()
    if payload.orgFilter is not None:
        cfg["orgFilter"] = payload.orgFilter
        save_config(cfg)

    raw_data = save_raw_dataset(raw_data, jurisdiction_code=cur_jur_code)
    _purge_jurisdiction_ts_caches(cur_jur_code)
    portfolio = recalculate_portfolio(
        raw_data,
        entitlement_pdu=cfg.get("entitlementPdu", 500),
        include_shared_flows=cfg.get("includeSharedFlows", True),
        region_overrides=cfg.get("regionOverrides", {}),
        entitlement_envs=cfg.get("entitlementEnvs", 6),
        entitlement_yearly_calls=cfg.get("entitlementYearlyCalls", 1_500_000_000),
        contract_start_date=cfg.get("contractStartDate"),
    )
    cur_jur = resolve_jurisdiction_info(cur_jur_code) or get_active_jurisdiction()
    portfolio["jurisdiction"] = cur_jur
    portfolio["controlPlaneJurisdiction"] = cur_jur["code"]
    portfolio["controlPlaneEndpoint"] = cur_jur["endpoint"]
    portfolio["history"] = append_history_snapshot(portfolio)
    portfolio["auth"] = await _build_request_auth_snapshot(request, cur_jur_code)
    return JSONResponse(portfolio)


async def _extract_stream_request_params(
    request: Request,
    account: str | None,
    jurisdiction: str | None,
) -> tuple[str | None, str | None, str | None, list[str] | None]:
    """Parse query parameters or JSON body parameters from streaming scan request."""
    target_account = account
    body_token = None
    requested_jur = jurisdiction
    body_org_filter: list[str] | None = None
    if request.method == "POST":
        try:
            body = await request.json()
            if isinstance(body, dict):
                target_account = body.get("account") or target_account
                body_token = body.get("accessToken")
                requested_jur = body.get("jurisdiction") or requested_jur
                if isinstance(body.get("orgFilter"), list):
                    body_org_filter = body["orgFilter"]
        except (json.JSONDecodeError, ValueError) as exc:
            logger.debug("Failed to parse JSON body in scan stream: %s", exc)
    return target_account, body_token, requested_jur, body_org_filter


def _build_stream_completion_portfolio(
    raw_data: dict[str, Any],
    cur_jur_code: str,
    cfg: dict[str, Any],
    auth_snapshot: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Recalculate portfolio with fresh configuration and return (portfolio, persisted_raw)."""
    _ = cfg
    latest_cfg = load_config()
    persisted_raw = save_raw_dataset(raw_data, jurisdiction_code=cur_jur_code)
    _purge_jurisdiction_ts_caches(cur_jur_code)
    portfolio = recalculate_portfolio(
        persisted_raw,
        entitlement_pdu=latest_cfg.get("entitlementPdu", 500),
        include_shared_flows=latest_cfg.get("includeSharedFlows", True),
        region_overrides=latest_cfg.get("regionOverrides", {}),
        entitlement_envs=latest_cfg.get("entitlementEnvs", 6),
        entitlement_yearly_calls=latest_cfg.get("entitlementYearlyCalls", 1_500_000_000),
        contract_start_date=latest_cfg.get("contractStartDate"),
    )
    cur_jur = resolve_jurisdiction_info(cur_jur_code) or get_active_jurisdiction()
    portfolio["jurisdiction"] = cur_jur
    portfolio["controlPlaneJurisdiction"] = cur_jur["code"]
    portfolio["controlPlaneEndpoint"] = cur_jur["endpoint"]
    portfolio["history"] = append_history_snapshot(portfolio)
    portfolio["auth"] = auth_snapshot
    return portfolio, persisted_raw


async def _prefetch_stream_timeseries(
    persisted_raw: dict[str, Any],
    cur_jur_code: str,
    user_account: str | None,
    user_token: str | None,
    progress_cb: Callable[[dict[str, Any]], None],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build Calls series from scanned raw data and query Cloud Monitoring PDU history during login scan."""
    latest_cfg = load_config()
    c_start = latest_cfg.get("contractStartDate")
    progress_cb({"percent": 88, "stage": "TIMESERIES", "message": "Preparing monthly API calls...", "detail": "Aggregating 90-day Analytics chunks"})
    try:
        calls_ts = await fetch_monitoring_pdu_timeseries(
            raw_data=persisted_raw,
            days=14,
            account=user_account,
            explicit_token=user_token,
            dimension="calls",
            contract_start_date=c_start,
            jurisdiction=cur_jur_code,
            force_refresh=False,
        )
        calls_ts["controlPlaneJurisdiction"] = cur_jur_code
        if calls_ts.get("callsRefreshed"):
            save_raw_dataset(persisted_raw, jurisdiction_code=cur_jur_code)
        _atomic_write_json(_compute_ts_cache_path(cur_jur_code, "calls", 14, c_start), calls_ts)
    except Exception as exc:
        logger.debug("Prefetch Calls timeseries failed during stream scan: %s", exc)
        calls_ts = {"dimension": "calls", "series": [], "seriesCount": 0, "trafficErrors": [str(exc)]}

    progress_cb({"percent": 93, "stage": "MONITORING", "message": "Querying Cloud Monitoring PDU history...", "detail": "apigee.googleapis.com/proxy/details (14d)"})
    try:
        pdu_ts = await fetch_monitoring_pdu_timeseries(
            raw_data=persisted_raw,
            days=14,
            account=user_account,
            explicit_token=user_token,
            dimension="pdu",
            contract_start_date=c_start,
            jurisdiction=cur_jur_code,
        )
        pdu_ts["controlPlaneJurisdiction"] = cur_jur_code
        _atomic_write_json(_compute_ts_cache_path(cur_jur_code, "pdu", 14, c_start), pdu_ts)
    except Exception as exc:
        logger.debug("Prefetch PDU timeseries failed during stream scan: %s", exc)
        pdu_ts = {"dimension": "pdu", "days": 14, "series": [], "seriesCount": 0, "monitoringErrors": [str(exc)]}
    return pdu_ts, calls_ts


def _build_stream_complete_event(
    portfolio: dict[str, Any],
    pdu_ts: dict[str, Any],
    calls_ts: dict[str, Any],
) -> dict[str, Any]:
    """Construct the final COMPLETE SSE payload with prefetched timeseries."""
    total_pdus = portfolio.get("summary", {}).get("totalPdus", 0)
    total_orgs = portfolio.get("summary", {}).get("totalOrganizations", 0)
    return {
        "percent": 100,
        "stage": "COMPLETE",
        "message": f"Scan complete! {total_pdus:,} PDUs across {total_orgs} organization(s).",
        "detail": "Dashboard ready",
        "portfolio": portfolio,
        "pduTimeseries": pdu_ts,
        "callsTimeseries": calls_ts,
        "done": True,
    }


async def _run_stream_scan_worker(
    user_token: str | None,
    final_account: str | None,
    cur_jur_code: str,
    cfg: dict[str, Any],
    queue: asyncio.Queue[dict[str, Any] | None],
    loop: asyncio.AbstractEventLoop,
    **extra: Any,
) -> None:
    """Execute live organization scan in the background and stream progress updates to the SSE queue."""
    auth_snapshot: dict[str, Any] = extra.get("auth_snapshot") or {}
    org_filter: list[str] | None = extra.get("org_filter")
    progress_cb = lambda data: loop.call_soon_threadsafe(queue.put_nowait, {**data, "done": False})
    try:
        raw_data = await collect_all_organizations(
            explicit_token=user_token,
            org_filter=org_filter,
            hybrid_region_overrides=cfg.get("regionOverrides", {}),
            account=final_account,
            contract_start_date=cfg.get("contractStartDate"),
            progress_callback=progress_cb,
            jurisdiction=cur_jur_code,
            completion_percent=86,
        )
        portfolio, persisted_raw = _build_stream_completion_portfolio(raw_data, cur_jur_code, cfg, auth_snapshot)
        pdu_ts, calls_ts = await _prefetch_stream_timeseries(
            persisted_raw, cur_jur_code, final_account, user_token, progress_cb
        )
        loop.call_soon_threadsafe(queue.put_nowait, _build_stream_complete_event(portfolio, pdu_ts, calls_ts))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        err_evt = {"percent": 100, "stage": "ERROR", "message": f"Scan failed: {exc}", "error": str(exc), "done": True}
        loop.call_soon_threadsafe(queue.put_nowait, err_evt)
    finally:
        loop.call_soon_threadsafe(queue.put_nowait, None)


async def _stream_queue_generator(
    queue: asyncio.Queue[dict[str, Any] | None],
    worker_task: asyncio.Task[None] | None = None,
) -> AsyncGenerator[str, None]:
    """Asynchronously consume items from the scan queue and format as Server-Sent Events."""
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield f"data: {json.dumps(item)}\n\n"
    except asyncio.CancelledError:
        pass
    finally:
        if worker_task is not None and not worker_task.done():
            worker_task.cancel()


@app.get("/api/scan/stream")
@app.post("/api/scan/stream")
async def api_scan_stream(
    request: Request,
    account: str | None = Query(default=None),
    jurisdiction: str | None = Query(default=None),
) -> StreamingResponse:
    """Real-time streaming endpoint emitting Server-Sent Events (SSE) during live organization scanning."""
    target_account, body_token, requested_jur, body_org_filter = await _extract_stream_request_params(
        request, account, jurisdiction
    )
    _validate_explicit_jurisdiction_param(requested_jur)
    await _verify_request_authenticated(request)
    cur_jur_code = _resolve_request_jurisdiction(request, requested_jur)

    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    user_token = extract_bearer_token(request, body_token)
    final_account = get_request_user_account(request, target_account)
    cfg = load_config()
    eff_org_filter = body_org_filter if body_org_filter is not None else (cfg.get("orgFilter") or None)
    auth_snapshot = await _build_request_auth_snapshot(request, cur_jur_code)

    worker_task = asyncio.create_task(
        _run_stream_scan_worker(
            user_token=user_token,
            final_account=final_account,
            cur_jur_code=cur_jur_code,
            cfg=cfg,
            queue=queue,
            loop=loop,
            auth_snapshot=auth_snapshot,
            org_filter=eff_org_filter,
        )
    )

    return StreamingResponse(
        _stream_queue_generator(queue, worker_task),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/demo")
async def api_load_demo_dataset(request: Request) -> JSONResponse:
    """Reset configuration overrides and load the bundled synthetic multi-organization demo dataset."""
    await _verify_request_authenticated(request)
    cur_jur_code = _resolve_request_jurisdiction(request, "global")
    cfg = load_config()
    cfg["regionOverrides"] = {}
    save_config(cfg)

    demo_data = get_demo_dataset(populate_calls=False)
    await populate_contract_api_calls(
        demo_data,
        contract_start_date=cfg.get("contractStartDate"),
    )
    save_raw_dataset(demo_data, merge_existing=False, jurisdiction_code=cur_jur_code)
    _purge_jurisdiction_ts_caches(cur_jur_code)

    portfolio = recalculate_portfolio(
        demo_data,
        entitlement_pdu=cfg.get("entitlementPdu", 500),
        include_shared_flows=cfg.get("includeSharedFlows", True),
        region_overrides={},
        entitlement_envs=cfg.get("entitlementEnvs", 6),
        entitlement_yearly_calls=cfg.get("entitlementYearlyCalls", 1_500_000_000),
        contract_start_date=cfg.get("contractStartDate"),
    )
    cur_jur = resolve_jurisdiction_info(cur_jur_code) or get_active_jurisdiction()
    portfolio["jurisdiction"] = cur_jur
    portfolio["controlPlaneJurisdiction"] = cur_jur["code"]
    portfolio["controlPlaneEndpoint"] = cur_jur["endpoint"]
    portfolio["history"] = append_history_snapshot(portfolio)
    portfolio["auth"] = await _build_request_auth_snapshot(request, cur_jur_code)
    return JSONResponse(portfolio)


def _matches_export_filter(
    item: dict[str, Any],
    selected_orgs: set[str],
    env: str | None,
    runtime: str | None,
) -> bool:
    """Check if an organization, environment, or deployment record passes export filters."""
    if selected_orgs and item.get("organization") not in selected_orgs:
        return False
    if env and item.get("environment") != env:
        return False
    if runtime and item.get("runtimeType") != runtime:
        return False
    return True


def _filter_single_org_for_export(
    org_obj: dict[str, Any],
    all_deps: list[dict[str, Any]],
    env: str | None,
) -> dict[str, Any] | None:
    """Filter a single organization's environments and deployments by optional env name."""
    org_name = org_obj["organization"]
    matching_envs = [e for e in org_obj.get("environments", []) if not env or e.get("environment") == env]
    if env and not matching_envs:
        return None
    matching_deps = [
        d for d in all_deps
        if d.get("organization") == org_name and (not env or d.get("environment") == env)
    ]
    instances = [] if env else [{"location": r} for r in org_obj.get("uniqueRegions", [])]
    return {**org_obj, "environments": matching_envs, "deployments": matching_deps, "instances": instances}


def _build_filtered_raw_orgs_for_export(
    portfolio: dict[str, Any],
    selected_orgs: set[str],
    env: str | None,
    runtime: str | None,
) -> list[dict[str, Any]]:
    """Construct filtered organization records with matching environments and deployments."""
    all_deps = portfolio.get("deployments", [])
    filtered_orgs: list[dict[str, Any]] = []
    for o in portfolio.get("organizations", []):
        if not _matches_export_filter(o, selected_orgs, None, runtime):
            continue
        filtered_o = _filter_single_org_for_export(o, all_deps, env)
        if filtered_o is not None:
            filtered_orgs.append(filtered_o)
    return filtered_orgs


def _filter_export_portfolio(
    portfolio: dict[str, Any],
    org: str | None,
    env: str | None,
    runtime: str | None,
) -> dict[str, Any]:
    """Filter portfolio and recalculate summary/totals so exported totals match filtered rows."""
    selected_orgs = (
        {o.strip() for o in org.split(",") if o.strip() and o.strip() != "ALL"}
        if org
        else set()
    )
    if not (selected_orgs or env or runtime):
        return portfolio

    filtered_raw_orgs = _build_filtered_raw_orgs_for_export(portfolio, selected_orgs, env, runtime)
    settings = portfolio.get("settings", {})
    recalc = recalculate_portfolio(
        {
            "collectedAt": portfolio.get("collectedAt"),
            "source": portfolio.get("source", "LIVE_APIGEE_API"),
            "authSource": portfolio.get("authSource", ""),
            "apiEndpoint": portfolio.get("apiEndpoint"),
            "totalAuthorizedOrgs": len(filtered_raw_orgs),
            "organizations": filtered_raw_orgs,
        },
        entitlement_pdu=int(settings.get("entitlementPdu", 500)),
        include_shared_flows=bool(settings.get("includeSharedFlows", True)),
        region_overrides={},
        entitlement_envs=int(settings.get("entitlementEnvs", 6)),
        entitlement_yearly_calls=int(settings.get("entitlementYearlyCalls", 1_500_000_000)),
        contract_start_date=settings.get("contractStartDate"),
    )
    recalc["history"] = portfolio.get("history", [])
    return recalc


def _generate_report_response(
    report_type: str,
    portfolio: dict[str, Any],
    ts_str: str,
    hide_zero: bool,
) -> Response:
    """Generate HTTP file attachment response for the requested report format."""
    if report_type == "organizations":
        csv_text = generate_organizations_csv(portfolio)
        return Response(
            content=csv_text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="apigee_pdu_organizations_summary_{ts_str}.csv"'},
        )
    if report_type == "environments":
        csv_text = generate_environments_csv(portfolio, hide_zero=hide_zero)
        return Response(
            content=csv_text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="apigee_entitlement_tracker_{ts_str}.csv"'},
        )
    if report_type == "deployments":
        csv_text = generate_deployments_csv(portfolio)
        return Response(
            content=csv_text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="apigee_pdu_deployments_inventory_{ts_str}.csv"'},
        )
    if report_type == "history":
        csv_text = generate_history_csv(portfolio.get("history", []))
        return Response(
            content=csv_text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="apigee_pdu_historical_log_{ts_str}.csv"'},
        )
    if report_type == "bundle":
        zip_bytes = generate_zip_bundle(portfolio, portfolio.get("history", []))
        return Response(
            content=zip_bytes,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="apigee_pdu_sheets_bundle_{ts_str}.zip"'},
        )
    raise HTTPException(
        status_code=400,
        detail="Invalid report_type. Choose from: organizations, environments, deployments, history, bundle.",
    )


@app.get("/api/export/{report_type}")
async def api_export_report(
    request: Request,
    report_type: str,
    org: str | None = Query(default=None, description="Optional filter by organization(s), comma-separated"),
    env: str | None = Query(default=None, description="Optional filter by environment"),
    runtime: str | None = Query(default=None, description="Optional filter by CLOUD or HYBRID"),
    hideZero: bool = Query(default=False, description="Hide 0-PDU environments in export"),
) -> Response:
    """Generate and export CSV reports or a bundled ZIP archive for Google Sheets."""
    raw_jur = request.query_params.get("jurisdiction")
    _validate_explicit_jurisdiction_param(raw_jur)
    await _verify_request_authenticated(request)
    cur_jur_code = _resolve_request_jurisdiction(request, raw_jur)
    auth_snapshot = await _build_request_auth_snapshot(request, cur_jur_code)
    portfolio = get_current_portfolio(cur_jur_code, auth_override=auth_snapshot)
    filtered_portfolio = _filter_export_portfolio(portfolio, org, env, runtime)
    ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return _generate_report_response(report_type, filtered_portfolio, ts_str, hideZero)


def _build_cli_arg_parser(default_host: str, default_port: int) -> argparse.ArgumentParser:
    """Construct CLI argument parser for server runtime flags and export options."""
    valid_jur_codes = ", ".join(APIGEE_JURISDICTIONS.keys())
    parser = argparse.ArgumentParser(
        description="Apigee Entitlement Tracker (X & Hybrid) — Web Dashboard & CSV Exporter"
    )
    parser.add_argument("--host", default=default_host, help=f"Bind host (default: {default_host})")
    parser.add_argument("--port", type=int, default=default_port, help=f"Bind port (default: {default_port})")
    parser.add_argument("--entitlement", type=int, default=None, help="Set cross-organization PDU entitlement number")
    parser.add_argument(
        "--account",
        type=str,
        default=None,
        help="GCP User Account email to authenticate with (defaults to active gcloud user account)",
    )
    parser.add_argument(
        "--jurisdiction",
        type=str,
        choices=tuple(APIGEE_JURISDICTIONS.keys()),
        default=None,
        help=f"Apigee Control Plane hosting jurisdiction ({valid_jur_codes})",
    )
    parser.add_argument(
        "--scan-live",
        action="store_true",
        help="Run a live Apigee API scan across authorized organizations before starting or exporting",
    )
    parser.add_argument(
        "--export-dir",
        type=str,
        default=None,
        help="Export all CSV files to the specified folder and exit (CLI mode)",
    )
    parser.add_argument(
        "--no-open-browser",
        action="store_true",
        help="Do not automatically open the web browser on startup",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove all local cached scan data (data/*.json) and exports (exports/*) before publishing and exit",
    )
    return parser


def _clean_cached_data_and_exports() -> None:
    """Remove local cached scan data and generated export files."""
    removed = 0
    for p in DATA_DIR.glob("*.json"):
        p.unlink(missing_ok=True)
        removed += 1
    exports_dir = BASE_DIR / "exports"
    if exports_dir.exists():
        for p in exports_dir.glob("*"):
            if p.is_file():
                p.unlink(missing_ok=True)
                removed += 1
    logger.info("Cleaned %d local cache/export file(s). Ready for public publishing.", removed)


def _run_cli_live_scan(account: str | None) -> None:
    """Execute live organization scan from CLI with progress logging."""
    cfg = load_config()
    jur_code = str(cfg.get("controlPlaneJurisdiction") or get_active_jurisdiction()["code"])
    auth_info = get_gcp_user_auth_info(jurisdiction=jur_code)
    acct = account or auth_info.get("activeAccount")
    logger.info("Scanning live authorized Apigee organizations as GCP User: %s ...", acct)

    def _cli_progress(evt: dict[str, Any]) -> None:
        """Log real-time scan event percentages to console."""
        pct = evt.get("percent", 0)
        msg = evt.get("message", "")
        detail = evt.get("organization") or evt.get("detail") or ""
        logger.info("[%3d%%] %s%s", pct, msg, f" — {detail}" if detail else "")

    raw_data = asyncio.run(
        collect_all_organizations(
            org_filter=cfg.get("orgFilter") or None,
            hybrid_region_overrides=cfg.get("regionOverrides", {}),
            account=acct,
            contract_start_date=cfg.get("contractStartDate"),
            progress_callback=_cli_progress,
            jurisdiction=jur_code,
        )
    )
    save_raw_dataset(raw_data, jurisdiction_code=jur_code)
    portfolio = get_current_portfolio(jur_code)
    append_history_snapshot(portfolio)
    summary = portfolio["summary"]
    logger.info("Scan complete: %s authorized orgs, %s total PDUs.", summary["totalOrganizations"], f"{summary['totalPdus']:,}")
    if summary.get("errorOrgs"):
        logger.warning("WARNING: %d organization(s) failed metadata/deployment inspection.", summary["errorOrgs"])
    if portfolio.get("trafficErrors"):
        logger.warning("WARNING: %d Apigee Analytics warning(s); affected call counts marked PARTIAL/ERROR.", len(portfolio["trafficErrors"]))


def _run_cli_export(export_dir: str) -> None:
    """Export all four CSV report files to the target directory."""
    out_dir = pathlib.Path(export_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    portfolio = get_current_portfolio()
    (out_dir / "01_apigee_pdu_executive_summary.csv").write_text(
        generate_organizations_csv(portfolio), encoding="utf-8"
    )
    (out_dir / "02_apigee_pdu_environments_breakdown.csv").write_text(
        generate_environments_csv(portfolio), encoding="utf-8"
    )
    (out_dir / "03_apigee_pdu_deployments_inventory.csv").write_text(
        generate_deployments_csv(portfolio), encoding="utf-8"
    )
    (out_dir / "04_apigee_pdu_historical_log.csv").write_text(
        generate_history_csv(portfolio.get("history", [])), encoding="utf-8"
    )
    logger.info("Exported 4 CSV reports to %s", out_dir)


def _apply_cli_overrides(args: argparse.Namespace) -> None:
    """Apply optional CLI jurisdiction and entitlement overrides to runtime configuration."""
    if args.jurisdiction:
        jur_info = set_active_jurisdiction(args.jurisdiction)
        if jur_info:
            cfg = load_config()
            cfg["controlPlaneJurisdiction"] = jur_info["code"]
            save_config(cfg)
    if args.entitlement is not None:
        cfg = load_config()
        cfg["entitlementPdu"] = max(1, args.entitlement)
        save_config(cfg)


def main() -> None:
    """CLI entry point for running server, scanning live data, or generating exports."""
    import webbrowser

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    is_cloud_run = bool(os.environ.get("K_SERVICE"))
    default_host = "0.0.0.0" if is_cloud_run else os.environ.get("HOST", "127.0.0.1")
    default_port = int(os.environ.get("PORT", "8080"))

    parser = _build_cli_arg_parser(default_host, default_port)
    args = parser.parse_args()

    if args.clean:
        _clean_cached_data_and_exports()
        return

    _apply_cli_overrides(args)

    if args.scan_live:
        _run_cli_live_scan(args.account)

    if args.export_dir:
        _run_cli_export(args.export_dir)
        return

    url = f"http://{args.host}:{args.port}"
    logger.info("Starting Apigee Entitlement Tracker Dashboard at %s", url)
    if not args.no_open_browser and not is_cloud_run:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
