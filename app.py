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
from collections.abc import AsyncGenerator, Awaitable, Callable
import datetime
import html
import json
import logging
import os
import pathlib
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
    clear_user_session,
    collect_all_organizations,
    discover_authorized_organizations,
    exchange_google_oidc_code,
    fetch_monitoring_pdu_timeseries,
    generate_session_id,
    get_active_jurisdiction,
    get_apigee_base_url,
    get_demo_dataset,
    get_gcp_user_auth_info,
    get_user_session,
    populate_contract_api_calls,
    recalculate_portfolio,
    refresh_google_oauth_token,
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

BASE_DIR = pathlib.Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = DATA_DIR / "config.json"
PDU_LIMIT_PATH = BASE_DIR / "pdu_limit.json"
RAW_CACHE_PATH = DATA_DIR / "latest_raw.json"
HISTORY_PATH = DATA_DIR / "history.json"
DISCOVERED_ORGS_PATH = DATA_DIR / "discovered_orgs.json"


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


def load_entitlement_limits() -> dict[str, Any]:
    """Read the permanent Entitlement Limits & Contract Start Date from pdu_limit.json if present."""
    if PDU_LIMIT_PATH.exists():
        try:
            payload = json.loads(PDU_LIMIT_PATH.read_text(encoding="utf-8"))
            out: dict[str, Any] = {}
            if int(payload.get("entitlementPdu", 0)) >= 1:
                out["entitlementPdu"] = int(payload["entitlementPdu"])
            if int(payload.get("entitlementEnvs", 0)) >= 1:
                out["entitlementEnvs"] = int(payload["entitlementEnvs"])
            if int(payload.get("entitlementYearlyCalls", 0)) >= 1:
                out["entitlementYearlyCalls"] = int(payload["entitlementYearlyCalls"])
            if payload.get("contractStartDate"):
                out["contractStartDate"] = str(payload["contractStartDate"]).strip()[:10]
            return out
        except (OSError, ValueError, TypeError) as exc:
            logger.debug("Failed to read entitlement limits from %s: %s", PDU_LIMIT_PATH, exc)
    return {}


def save_entitlement_limits(cfg: dict[str, Any], *, updated: bool = True) -> None:
    """Permanently store PDU, Environment Unit, and Yearly API Call limits + Contract Start Date in pdu_limit.json."""
    PDU_LIMIT_PATH.write_text(
        json.dumps(
            {
                "entitlementPdu": max(1, int(cfg.get("entitlementPdu", 500))),
                "entitlementEnvs": max(1, int(cfg.get("entitlementEnvs", 6))),
                "entitlementYearlyCalls": max(1, int(cfg.get("entitlementYearlyCalls", 1_500_000_000))),
                "contractStartDate": str(
                    cfg.get("contractStartDate") or "2026-01-01"
                ).strip()[:10],
                "updatedAt": (
                    datetime.datetime.now(datetime.timezone.utc).isoformat()
                    if updated
                    else None
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def load_config() -> dict[str, Any]:
    """Load configuration from disk, applying defaults and permanent entitlement overrides."""
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update(data)
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Failed to read config from %s: %s", CONFIG_PATH, exc)

    perm_limits = load_entitlement_limits()
    if perm_limits:
        cfg.update(perm_limits)
    else:
        save_entitlement_limits(cfg, updated=False)

    if cfg.get("controlPlaneJurisdiction"):
        set_active_jurisdiction(cfg["controlPlaneJurisdiction"])

    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
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
    CONFIG_PATH.write_text(json.dumps(disk_cfg, indent=2), encoding="utf-8")


def get_raw_cache_path(jurisdiction_code: str | None = None) -> pathlib.Path:
    """Return the filesystem path for the raw cache of the specified or active jurisdiction."""
    jur = (jurisdiction_code or get_active_jurisdiction().get("code", "global")).strip().lower()
    return DATA_DIR / f"latest_raw_{jur}.json"


def get_discovered_orgs_path(jurisdiction_code: str | None = None) -> pathlib.Path:
    """Return the filesystem path for cached discovered organizations of the specified jurisdiction."""
    jur = (jurisdiction_code or get_active_jurisdiction().get("code", "global")).strip().lower()
    return DATA_DIR / f"discovered_orgs_{jur}.json"


def _resolve_cached_dataset_jurisdiction(data: dict[str, Any]) -> str:
    """Extract or infer the control plane jurisdiction from cached raw dataset content."""
    data_jur = str(data.get("controlPlaneJurisdiction") or "").strip().lower()
    if not data_jur and data.get("apiEndpoint"):
        endpoint_str = str(data["apiEndpoint"])
        for k, v in APIGEE_JURISDICTIONS.items():
            if v["endpoint"] in endpoint_str:
                return k
    return data_jur or "global"


def _read_cached_dataset_file(target_path: pathlib.Path, jur: str) -> dict[str, Any] | None:
    """Attempt reading and validating jurisdiction match of cached raw dataset."""
    if not target_path.exists():
        return None
    try:
        data = json.loads(target_path.read_text(encoding="utf-8"))
        data_jur = _resolve_cached_dataset_jurisdiction(data)
        if data_jur == jur:
            return data
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
    jur = (jurisdiction_code or get_active_jurisdiction().get("code", "global")).strip().lower()
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
    sanitized = dict(raw_data)
    sanitized.pop("account", None)
    if str(sanitized.get("authSource", "")).startswith("GCP User"):
        sanitized["authSource"] = "GCP User Account (OAuth2)"

    cur_jur_info = get_active_jurisdiction()
    jur = (
        jurisdiction_code
        or sanitized.get("controlPlaneJurisdiction")
        or cur_jur_info.get("code", "global")
    ).strip().lower()

    sanitized["controlPlaneJurisdiction"] = jur
    sanitized["jurisdiction"] = APIGEE_JURISDICTIONS.get(jur) or cur_jur_info
    target_path = get_raw_cache_path(jur)
    return sanitized, jur, target_path


def _merge_org_traffic_metrics(old_o: dict[str, Any], new_o: dict[str, Any]) -> None:
    """Preserve previous environment daily calls and traffic metrics when current scan lacked them."""
    if not (old_o.get("environments") and new_o.get("environments")):
        return
    old_envs = {e["environment"]: e for e in old_o.get("environments", [])}
    for new_e in new_o.get("environments", []):
        old_e = old_envs.get(new_e["environment"])
        if (
            old_e
            and not new_e.get("dailyCalls")
            and int(new_e.get("yearlyApiCalls", 0)) == 0
            and old_e.get("dailyCalls")
        ):
            new_e["dailyCalls"] = old_e["dailyCalls"]
            new_e["standardApiCalls"] = old_e.get("standardApiCalls", 0)
            new_e["extensibleApiCalls"] = old_e.get("extensibleApiCalls", 0)
            new_e["rawApiCalls"] = old_e.get("rawApiCalls", 0)
            new_e["yearlyApiCalls"] = old_e.get("yearlyApiCalls", 0)
            new_e["callsSource"] = old_e.get("callsSource", "APIGEE_ANALYTICS")


def _merge_raw_organizations(
    existing_orgs: list[dict[str, Any]],
    new_orgs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge newly scanned organizations into existing organization inventory."""
    existing_map = {o["organization"]: o for o in existing_orgs}
    for new_o in new_orgs:
        org_name = new_o["organization"]
        old_o = existing_map.get(org_name)
        if old_o and new_o.get("status") == "ERROR" and old_o.get("status") == "OK":
            continue
        if old_o:
            _merge_org_traffic_metrics(old_o, new_o)
        existing_map[org_name] = new_o

    merged = list(existing_map.values())
    merged.sort(
        key=lambda o: (
            -sum(e.get("totalPdus", 0) for e in o.get("environments", [])),
            o["organization"].lower(),
        )
    )
    return merged


def _try_merge_existing_dataset(
    target_path: pathlib.Path,
    sanitized: dict[str, Any],
    jur: str,
) -> None:
    """Attempt reading prior dataset on disk and merging live scan data within same jurisdiction."""
    if not target_path.exists():
        return
    try:
        existing = json.loads(target_path.read_text(encoding="utf-8"))
        existing_jur = _resolve_cached_dataset_jurisdiction(existing)
        if (
            existing.get("source") == "LIVE_APIGEE_API"
            and existing_jur == jur
            and existing.get("organizations")
            and sanitized.get("organizations")
        ):
            merged_orgs = _merge_raw_organizations(
                existing.get("organizations", []),
                sanitized.get("organizations", []),
            )
            sanitized["organizations"] = merged_orgs
            sanitized["totalAuthorizedOrgs"] = len(merged_orgs)
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        logger.debug("Failed to merge with existing raw dataset at %s: %s", target_path, exc)


def save_raw_dataset(
    raw_data: dict[str, Any],
    merge_existing: bool = True,
    jurisdiction_code: str | None = None,
) -> dict[str, Any]:
    """Persist raw scan dataset to disk, optionally merging live results for the active jurisdiction."""
    sanitized, jur, target_path = _prepare_sanitized_payload(raw_data, jurisdiction_code)
    if merge_existing and sanitized.get("source") == "LIVE_APIGEE_API":
        _try_merge_existing_dataset(target_path, sanitized, jur)

    target_path.write_text(json.dumps(sanitized, indent=2), encoding="utf-8")
    return sanitized


def load_history() -> list[dict[str, Any]]:
    """Load historical audit snapshots from disk."""
    if HISTORY_PATH.exists():
        try:
            return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("Failed to read history from %s: %s", HISTORY_PATH, exc)
    return []


def append_history_snapshot(portfolio: dict[str, Any]) -> list[dict[str, Any]]:
    """Record an audit summary snapshot into the rolling historical timeline."""
    history = load_history()
    summary = portfolio.get("summary", {})
    raw_src = str(portfolio.get("authSource") or portfolio.get("source", "DEMO_DATASET"))
    safe_src = "GCP User Account (OAuth2)" if raw_src.startswith("GCP User") else raw_src
    entry = {
        "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "source": safe_src,
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
    history.insert(0, entry)
    history = history[:100]  # Keep last 100 snapshots
    HISTORY_PATH.write_text(json.dumps(history, indent=2), encoding="utf-8")
    return history


def get_current_portfolio(jurisdiction_code: str | None = None) -> dict[str, Any]:
    """Assemble and calculate current entitlement portfolio across authorized organizations."""
    cfg = load_config()
    cur_jur_code = (jurisdiction_code or get_active_jurisdiction().get("code", "global")).strip().lower()
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
    history = load_history()
    if not history:
        history = append_history_snapshot(portfolio)
    portfolio["history"] = history
    portfolio["auth"] = get_gcp_user_auth_info()
    cur_jur = APIGEE_JURISDICTIONS.get(cur_jur_code) or get_active_jurisdiction()
    portfolio["jurisdiction"] = cur_jur
    portfolio["controlPlaneJurisdiction"] = cur_jur["code"]
    portfolio["controlPlaneEndpoint"] = cur_jur["endpoint"]
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


def _is_allowed_origin_host(origin: str, request_host: str) -> bool:
    """Verify whether the request Origin/Referer matches the host or trusted hosts."""
    try:
        parsed = urllib.parse.urlparse(origin)
        origin_host = parsed.netloc.split(":")[0].lower()
        clean_request_host = request_host.split(":")[0].lower()
        if origin_host == clean_request_host:
            return True
        if origin_host in {"localhost", "127.0.0.1", "testserver"}:
            return True
        for pattern in ALLOWED_HOSTS:
            if pattern.startswith("*.") and origin_host.endswith(pattern[1:]):
                return True
            if pattern == origin_host:
                return True
    except Exception:
        return False
    return False


@app.middleware("http")
async def protect_csrf_and_origin(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Protect state-changing endpoints against cross-origin and DNS rebinding attacks."""
    if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
        origin = request.headers.get("Origin") or request.headers.get("origin")
        referer = request.headers.get("Referer") or request.headers.get("referer")
        target = origin or referer
        if target:
            req_host = request.headers.get("host", request.url.netloc)
            if not _is_allowed_origin_host(target, req_host):
                return JSONResponse(
                    {"detail": "Forbidden: cross-origin request rejected"},
                    status_code=403,
                )
    return await call_next(request)


@app.middleware("http")
async def disable_static_and_auth_cache(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Disable browser caching for dashboard, static assets, and authentication endpoints."""
    response = await call_next(request)
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


def extract_bearer_token(request: Request, explicit_token: str | None = None) -> str | None:
    """Extract a user OAuth access token from explicit arguments, headers, cookies, or session."""
    if _is_clean_token(explicit_token):
        return explicit_token.strip() if explicit_token else None

    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header.startswith("Bearer "):
        candidate = auth_header[7:].strip()
        if _is_clean_token(candidate):
            return candidate

    cookie_tok = request.cookies.get("apigee_access_token", "").strip()
    if _is_clean_token(cookie_tok):
        return cookie_tok

    session_id = request.cookies.get("apigee_session_id", "").strip()
    if session_id:
        user_sess = get_user_session(session_id)
        sess_tok = str(user_sess.get("accessToken", "")).strip()
        if _is_clean_token(sess_tok):
            return sess_tok

    return None


def get_request_user_account(request: Request, explicit_account: str | None = None) -> str | None:
    """Resolve the active user account email from explicit parameters, IAP headers, OIDC session, or config."""
    if explicit_account and explicit_account.strip():
        return explicit_account.strip()
    iap_user = request.headers.get("x-goog-authenticated-user-email") or request.headers.get("X-Goog-Authenticated-User-Email")
    if iap_user:
        return iap_user.split(":")[-1].strip() if ":" in iap_user else iap_user.strip()
    session_id = request.cookies.get("apigee_session_id", "").strip()
    if session_id:
        user_sess = get_user_session(session_id)
        user_acct = user_sess.get("email") or user_sess.get("account")
        if user_acct:
            return user_acct
    if not os.environ.get("K_SERVICE"):
        cfg = load_config()
        return cfg.get("gcpAccount")
    return None


def _apply_iap_identity(request: Request, info: dict[str, Any]) -> None:
    """Detect and inject Google Cloud Identity-Aware Proxy (IAP) authenticated user identity."""
    iap_user = request.headers.get("x-goog-authenticated-user-email") or request.headers.get("X-Goog-Authenticated-User-Email")
    if not iap_user:
        return
    clean_user = iap_user.split(":")[-1].strip() if ":" in iap_user else iap_user.strip()
    info["authenticated"] = True
    info["activeAccount"] = clean_user
    info["authSource"] = clean_user
    info["isIap"] = True
    accounts = info.get("accounts", [])
    if not any(a.get("account") == clean_user for a in accounts):
        accounts.insert(0, {"account": clean_user, "status": "ACTIVE"})
    info["accounts"] = accounts


async def _verify_request_token(info: dict[str, Any], bearer_tok: str | None) -> None:
    """Validate bearer token online and populate token status flags in auth info dictionary."""
    if not bearer_tok:
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
        if not info.get("hasUserToken"):
            info["tokenValid"] = False
            info["tokenExpired"] = True
            info["hasUserToken"] = False


@app.get("/api/auth-status")
async def check_auth_status(request: Request) -> JSONResponse:
    """Return comprehensive authentication status, token validity, and active jurisdiction."""
    auth_tok = extract_bearer_token(request)
    session_id = request.cookies.get("apigee_session_id", "").strip()
    user_sess = get_user_session(session_id) if session_id else {}
    cookie_refresh_tok = request.cookies.get("apigee_refresh_token") or user_sess.get("refreshToken")
    cookie_access_tok = request.cookies.get("apigee_access_token") or user_sess.get("accessToken")
    req_account = get_request_user_account(request)
    info = await asyncio.to_thread(
        get_gcp_user_auth_info,
        explicit_token=auth_tok or cookie_access_tok,
        explicit_refresh_token=cookie_refresh_tok,
        explicit_account=req_account,
        user_session=user_sess if user_sess else None,
    )
    _apply_iap_identity(request, info)
    await _verify_request_token(info, auth_tok)

    info["tokenValid"] = bool(info.get("tokenValid", False) and info.get("hasUserToken", False))
    info["tokenExpired"] = bool(info.get("tokenExpired", False))
    cur_jur = get_active_jurisdiction()
    info["jurisdiction"] = cur_jur
    info["controlPlaneJurisdiction"] = cur_jur["code"]
    info["controlPlaneEndpoint"] = cur_jur["endpoint"]
    info["jurisdictions"] = list(APIGEE_JURISDICTIONS.values())

    return JSONResponse(
        info,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


def _resolve_redirect_endpoints(request: Request) -> tuple[str, str]:
    """Compute base URL and redirect URI for OAuth authentication flows."""
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    base_url = f"{forwarded_proto}://{host}"
    return base_url, f"{base_url}/api/auth/oidc/callback"


async def _perform_local_gcloud_browser_login(
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
        return JSONResponse(
            {
                "authUrl": "/?authenticated=1",
                "redirectUri": f"{base_url}/?authenticated=1",
                "activeAccount": info.get("activeAccount"),
                "isOAuthConfigured": False,
            }
        )
    return JSONResponse(
        {
            "authUrl": "/?authenticated=1",
            "redirectUri": redirect_uri,
            "isOAuthConfigured": False,
        }
    )


@app.get("/api/auth/oidc/auth-url")
async def api_oidc_auth_url(
    request: Request,
    account: str | None = Query(default=None),
) -> JSONResponse:
    """Force Google re-authentication / account selection."""
    base_url, redirect_uri = _resolve_redirect_endpoints(request)
    custom_client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    custom_client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    has_custom_web_oauth = bool(
        custom_client_id
        and custom_client_id != DEFAULT_GOOGLE_CLIENT_ID
        and custom_client_secret
    )

    if has_custom_web_oauth:
        auth_url = build_google_oidc_auth_url(
            redirect_uri=redirect_uri,
            prompt_account=account,
        )
        return JSONResponse(
            {
                "authUrl": auth_url,
                "redirectUri": redirect_uri,
                "clientId": custom_client_id,
                "isOAuthConfigured": True,
            }
        )

    iap_user = request.headers.get("x-goog-authenticated-user-email")
    if iap_user or os.environ.get("K_SERVICE"):
        return JSONResponse(
            {
                "authUrl": "/_gcp_iap/clear_login_cookie",
                "redirectUri": f"{base_url}/_gcp_iap/clear_login_cookie",
                "isOAuthConfigured": False,
                "notice": "OAuth 2.0 Client credentials not configured. Using IAP account switch.",
            }
        )

    return await _perform_local_gcloud_browser_login(account, redirect_uri, base_url)


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
  <script>
    try {{
      sessionStorage.setItem('apigee_user_access_token', {json_token});
      sessionStorage.setItem('apigee_user_email', {json_email});
      localStorage.setItem('apigee_user_access_token', {json_token});
      localStorage.setItem('apigee_user_email', {json_email});
    }} catch (e) {{
      console.warn('storage error', e);
    }}
    window.location.replace('/?authenticated=1');
  </script>
</body>
</html>"""


def _render_oidc_callback_html(email: str, access_token: str) -> str:
    """Render client-side token storage and redirect template for OAuth callback."""
    return _OIDC_CALLBACK_HTML_TEMPLATE.format(
        safe_email=html.escape(email),
        json_token=json.dumps(access_token),
        json_email=json.dumps(email),
    )


def _attach_oidc_cookies(
    response: Response,
    session_id: str,
    access_token: str,
    refresh_token: str,
    is_https: bool,
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
            max_age=3600,
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


def _build_oidc_response(
    request: Request,
    session: dict[str, Any],
) -> Response:
    """Create HTML response with cookies and client tokens from successful OIDC exchange."""
    email = session.get("email") or "google-user"
    access_token = session.get("accessToken", "")
    refresh_token = session.get("refreshToken", "")

    session_id = request.cookies.get("apigee_session_id") or generate_session_id()
    set_user_session(session_id, session)

    if email and not os.environ.get("K_SERVICE"):
        cfg = load_config()
        cfg["gcpAccount"] = email
        save_config(cfg)

    html_content = _render_oidc_callback_html(email, access_token)
    resp = HTMLResponse(content=html_content)
    is_https = (request.headers.get("x-forwarded-proto") == "https") or (request.url.scheme == "https")
    _attach_oidc_cookies(resp, session_id, access_token, refresh_token, is_https)
    return resp


@app.get("/api/auth/oidc/callback")
async def api_oidc_callback(
    request: Request,
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> Response:
    """Handle Google OIDC OAuth 2.0 redirect callback, exchange authorization code, and persist tokens."""
    if error or not code:
        err_val = error or "no_code"
        return RedirectResponse(url=f"/?oidc_error={err_val}")

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
        session_id = request.cookies.get("apigee_session_id") or generate_session_id()
        set_user_session(session_id, {
            "email": info.get("activeAccount") or payload.email,
            "accessToken": payload.accessToken,
            "authenticated": True,
        })
        if not os.environ.get("K_SERVICE"):
            cfg = load_config()
            if info.get("activeAccount"):
                cfg["gcpAccount"] = info.get("activeAccount")
                save_config(cfg)
        info["hasUserToken"] = True
        resp = JSONResponse(info)
        forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        is_https = (forwarded_proto == "https") or (request.url.scheme == "https")
        _attach_oidc_cookies(resp, session_id, payload.accessToken, "", is_https)
        return resp
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/auth/oidc/logout")
async def api_oidc_logout(request: Request) -> JSONResponse:
    """Clear the active Google OIDC session and cookies."""
    session_id = request.cookies.get("apigee_session_id")
    if session_id:
        clear_user_session(session_id)
    resp = JSONResponse({"success": True, "message": "OIDC session cleared"})
    resp.delete_cookie("apigee_session_id")
    resp.delete_cookie("apigee_access_token")
    resp.delete_cookie("apigee_refresh_token")
    return resp


@app.get("/api/jurisdictions")
async def api_get_jurisdictions() -> JSONResponse:
    """Return list of supported Apigee Control Plane Hosting Jurisdictions and the active one."""
    return JSONResponse({
        "current": get_active_jurisdiction(),
        "jurisdictions": list(APIGEE_JURISDICTIONS.values()),
    })


@app.post("/api/jurisdiction")
async def api_set_jurisdiction(payload: JurisdictionUpdateRequest) -> JSONResponse:
    """Set the active Apigee Control Plane Hosting Jurisdiction."""
    jur_info = set_active_jurisdiction(payload.jurisdiction)
    if jur_info is None:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid jurisdiction '{payload.jurisdiction}'. Must be one of: {', '.join(APIGEE_JURISDICTIONS.keys())}",
        )
    prev_jur = get_active_jurisdiction()["code"]
    cfg = load_config()
    cfg["controlPlaneJurisdiction"] = jur_info["code"]
    save_config(cfg)
    if prev_jur != jur_info["code"] and DISCOVERED_ORGS_PATH.exists():
        try:
            DISCOVERED_ORGS_PATH.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("Failed to remove cached discovered orgs: %s", exc)
    return JSONResponse({
        "status": "success",
        "jurisdiction": jur_info,
        "jurisdictions": list(APIGEE_JURISDICTIONS.values()),
    })


def _switch_jurisdiction_on_login(jurisdiction: str | None) -> None:
    """Update active jurisdiction and clear cached discovered organizations if jurisdiction changed."""
    if not jurisdiction:
        return
    jur_info = set_active_jurisdiction(jurisdiction)
    if jur_info is None:
        return
    prev_jur = get_active_jurisdiction()["code"]
    cfg = load_config()
    cfg["controlPlaneJurisdiction"] = jur_info["code"]
    save_config(cfg)
    if prev_jur != jur_info["code"] and DISCOVERED_ORGS_PATH.exists():
        try:
            DISCOVERED_ORGS_PATH.unlink(missing_ok=True)
        except OSError as exc:
            logger.debug("Failed to unlink cached discovered orgs: %s", exc)

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
        session_id = request.cookies.get("apigee_session_id") or generate_session_id()
        set_user_session(session_id, {
            "email": info.get("activeAccount") or user_account,
            "accessToken": user_token,
            "authenticated": True,
        })
        if not os.environ.get("K_SERVICE"):
            cfg = load_config()
            cfg["gcpAccount"] = info.get("activeAccount")
            save_config(cfg)
        info["hasUserToken"] = True
        resp = JSONResponse(info)
        is_https = (request.headers.get("x-forwarded-proto") == "https") or (request.url.scheme == "https")
        _attach_oidc_cookies(resp, session_id, user_token, "", is_https)
        return resp
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _handle_account_only_login(
    request: Request,
    user_account: str,
) -> JSONResponse:
    """Return auth info for a known user account, indicating if an access token is needed."""
    session_id = request.cookies.get("apigee_session_id", "").strip()
    user_sess = get_user_session(session_id) if session_id else {}
    info = get_gcp_user_auth_info(user_session=user_sess if user_sess else None)
    if user_sess.get("accessToken"):
        info["hasUserToken"] = True
        return JSONResponse(info)
    info["authenticated"] = True
    info["activeAccount"] = user_account
    info["hasUserToken"] = False
    info["needsUserToken"] = True
    return JSONResponse(info)


async def _handle_browser_or_default_login(
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
        if not os.environ.get("K_SERVICE"):
            cfg = load_config()
            cfg["gcpAccount"] = info.get("activeAccount")
            save_config(cfg)
        return JSONResponse(info)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/auth/login")
async def api_auth_login(request: Request, payload: AuthLoginRequest) -> JSONResponse:
    """Switch account or initiate Google OIDC login without requiring local gcloud CLI."""
    _switch_jurisdiction_on_login(payload.jurisdiction)
    _, redirect_uri = _resolve_redirect_endpoints(request)

    user_token = extract_bearer_token(request, payload.accessToken)
    user_account = get_request_user_account(request, payload.account)

    if user_token:
        return await _handle_token_login(request, user_token, user_account)
    if user_account:
        return _handle_account_only_login(request, user_account)
    return await _handle_browser_or_default_login(payload, redirect_uri)


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


def _merge_discovered_org_lists(
    existing_orgs: list[dict[str, Any]],
    newly_discovered: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge newly discovered organizations into existing list, deduplicating by org name."""
    if not existing_orgs:
        return newly_discovered
    known = {o["organization"]: o for o in existing_orgs}
    for o in newly_discovered:
        known[o["organization"]] = o
    return sorted(list(known.values()), key=lambda x: x["organization"].lower())


@app.get("/api/organizations/discover")
async def api_discover_organizations(
    request: Request,
    refresh: bool = Query(default=True, description="Query live GET /v1/organizations"),
    account: str | None = Query(default=None, description="Optional GCP user account email"),
    jurisdiction: str | None = Query(default=None, description="Control plane hosting jurisdiction"),
) -> JSONResponse:
    """Discover all authorized Apigee organizations for the current user and jurisdiction."""
    if jurisdiction:
        set_active_jurisdiction(jurisdiction)
    cur_jur_code = get_active_jurisdiction()["code"]
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
        )
        discovered["controlPlaneJurisdiction"] = cur_jur_code
        discovered["jurisdiction"] = get_active_jurisdiction()
        merged = _merge_discovered_org_lists(existing_orgs, discovered.get("organizations", []))
        discovered["organizations"] = merged
        discovered["totalAuthorizedOrgs"] = len(merged)
        disc_path.write_text(json.dumps(discovered, indent=2), encoding="utf-8")
        return JSONResponse(discovered)
    except Exception as exc:
        if existing_orgs:
            return JSONResponse({
                "organizations": existing_orgs,
                "totalAuthorizedOrgs": len(existing_orgs),
                "controlPlaneJurisdiction": cur_jur_code,
            })
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/portfolio")
async def api_get_portfolio(
    request: Request,
    jurisdiction: str | None = Query(default=None, description="Apigee control plane jurisdiction"),
) -> JSONResponse:
    """Retrieve recalculated portfolio summary, inventory breakdown, and current user auth info."""
    if jurisdiction:
        set_active_jurisdiction(jurisdiction)
    portfolio = get_current_portfolio(jurisdiction)
    user_account = get_request_user_account(request)
    user_token = extract_bearer_token(request)
    if "auth" not in portfolio or not isinstance(portfolio["auth"], dict):
        portfolio["auth"] = {}
    if user_account:
        portfolio["auth"]["activeAccount"] = user_account
        portfolio["auth"]["authSource"] = user_account
        portfolio["auth"]["authenticated"] = True
    portfolio["auth"]["hasUserToken"] = bool(user_token)
    return JSONResponse(portfolio)


def _compute_ts_cache_path(
    cur_jur_code: str,
    dim: str,
    days: int,
    contract_start_date: str | None,
) -> pathlib.Path:
    """Generate cache file path for Cloud Monitoring timeseries based on dimension and lookback."""
    contract_dt = str(contract_start_date or "default").replace("-", "")
    cache_suffix = f"_{contract_dt}" if dim == "calls" else f"_{days}d"
    return DATA_DIR / f"monitoring_ts_{cur_jur_code}_{dim}{cache_suffix}.json"


def _read_cached_timeseries(cache_file: pathlib.Path, cur_jur_code: str) -> dict[str, Any] | None:
    """Read cached timeseries data if present and matching current jurisdiction."""
    if not cache_file.exists():
        return None
    try:
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if cached.get("controlPlaneJurisdiction", "global") == cur_jur_code:
            return cached
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Failed to read timeseries cache at %s: %s", cache_file, exc)
    return None


def _merge_monitoring_series(
    existing: dict[str, Any] | None,
    ts_data: dict[str, Any],
    cur_jur_code: str,
    is_live_source: bool,
) -> None:
    """Merge historical series points when live query only returned a partial set of organizations."""
    if not (
        existing
        and existing.get("series")
        and existing.get("controlPlaneJurisdiction", "global") == cur_jur_code
        and is_live_source
    ):
        return
    merged_map = {
        (s.get("organization"), s.get("environment")): s
        for s in existing.get("series", [])
    }
    for new_s in ts_data.get("series", []):
        if new_s.get("points"):
            merged_map[(new_s.get("organization"), new_s.get("environment"))] = new_s
    merged = list(merged_map.values())
    ts_data["series"] = merged
    ts_data["seriesCount"] = len(merged)


@app.get("/api/monitoring/timeseries")
async def api_get_monitoring_timeseries(
    request: Request,
    days: int = Query(default=14, ge=1, le=90, description="Lookback window in days"),
    dimension: str = Query(default="pdu", description="Dimension: pdu | environments | calls"),
    refresh: bool = Query(default=False, description="Force live query to Cloud Monitoring / Apigee Analytics API"),
    jurisdiction: str | None = Query(default=None, description="Apigee control plane jurisdiction"),
) -> JSONResponse:
    """Query Cloud Monitoring and Apigee Analytics for PDU, Environment Unit, or Call timeseries."""
    if jurisdiction:
        set_active_jurisdiction(jurisdiction)
    cur_jur_code = get_active_jurisdiction()["code"]
    cfg = load_config()
    dim = (dimension or "pdu").strip().lower()
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
        )
        ts_data["controlPlaneJurisdiction"] = cur_jur_code
        _merge_monitoring_series(
            existing_cached,
            ts_data,
            cur_jur_code,
            raw.get("source") == "LIVE_APIGEE_API",
        )
        if dim == "calls":
            save_raw_dataset(raw, jurisdiction_code=cur_jur_code)
        cache_file.write_text(json.dumps(ts_data, indent=2), encoding="utf-8")
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


@app.post("/api/settings")
async def api_update_settings(payload: SettingsUpdateRequest) -> JSONResponse:
    """Update cross-organization entitlement parameters, contract date, and region overrides."""
    cfg = load_config()
    _validate_and_apply_entitlements(payload, cfg)

    contract_date_changed = False
    if payload.contractStartDate is not None and payload.contractStartDate.strip():
        new_dt = payload.contractStartDate.strip()[:10]
        if new_dt != cfg.get("contractStartDate"):
            contract_date_changed = True
        cfg["contractStartDate"] = new_dt

    if payload.includeSharedFlows is not None:
        cfg["includeSharedFlows"] = bool(payload.includeSharedFlows)

    _apply_region_override_updates(payload, cfg)
    save_config(cfg)

    if contract_date_changed:
        raw = load_raw_dataset()
        await populate_contract_api_calls(
            raw,
            contract_start_date=cfg.get("contractStartDate"),
            account=cfg.get("gcpAccount"),
        )
        save_raw_dataset(raw)
        for ts_cache in DATA_DIR.glob("monitoring_ts_calls*.json"):
            ts_cache.unlink(missing_ok=True)

    portfolio = get_current_portfolio()
    return JSONResponse(portfolio)


@app.post("/api/scan")
async def api_trigger_live_scan(request: Request, payload: LiveScanRequest) -> JSONResponse:
    """Trigger a live audit of all authorized Apigee organizations and environments."""
    if payload.jurisdiction:
        set_active_jurisdiction(payload.jurisdiction)
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
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    cfg = load_config()
    if payload.orgFilter is not None:
        cfg["orgFilter"] = payload.orgFilter
        save_config(cfg)

    raw_data = save_raw_dataset(raw_data, jurisdiction_code=payload.jurisdiction)
    portfolio = recalculate_portfolio(
        raw_data,
        entitlement_pdu=cfg.get("entitlementPdu", 500),
        include_shared_flows=cfg.get("includeSharedFlows", True),
        region_overrides=cfg.get("regionOverrides", {}),
        entitlement_envs=cfg.get("entitlementEnvs", 6),
        entitlement_yearly_calls=cfg.get("entitlementYearlyCalls", 1_500_000_000),
        contract_start_date=cfg.get("contractStartDate"),
    )
    history = append_history_snapshot(portfolio)
    portfolio["history"] = history
    portfolio["auth"] = get_gcp_user_auth_info()
    cur_jur = get_active_jurisdiction()
    portfolio["jurisdiction"] = cur_jur
    portfolio["controlPlaneJurisdiction"] = cur_jur["code"]
    portfolio["controlPlaneEndpoint"] = cur_jur["endpoint"]
    return JSONResponse(portfolio)


async def _extract_stream_request_params(
    request: Request,
    account: str | None,
    jurisdiction: str | None,
) -> tuple[str | None, str | None, str | None]:
    """Parse query parameters or JSON body parameters from streaming scan request."""
    target_account = account
    body_token = None
    requested_jur = jurisdiction
    if request.method == "POST":
        try:
            body = await request.json()
            if isinstance(body, dict):
                target_account = body.get("account") or target_account
                body_token = body.get("accessToken")
                requested_jur = body.get("jurisdiction") or requested_jur
        except (json.JSONDecodeError, ValueError) as exc:
            logger.debug("Failed to parse JSON body in scan stream: %s", exc)
    return target_account, body_token, requested_jur


def _build_stream_completion_portfolio(
    raw_data: dict[str, Any],
    requested_jur: str | None,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """Recalculate portfolio and append history snapshot upon successful stream scan."""
    persisted_raw = save_raw_dataset(raw_data, jurisdiction_code=requested_jur)
    portfolio = recalculate_portfolio(
        persisted_raw,
        entitlement_pdu=cfg.get("entitlementPdu", 500),
        include_shared_flows=cfg.get("includeSharedFlows", True),
        region_overrides=cfg.get("regionOverrides", {}),
        entitlement_envs=cfg.get("entitlementEnvs", 6),
        entitlement_yearly_calls=cfg.get("entitlementYearlyCalls", 1_500_000_000),
        contract_start_date=cfg.get("contractStartDate"),
    )
    portfolio["history"] = append_history_snapshot(portfolio)
    portfolio["auth"] = get_gcp_user_auth_info()
    cur_jur = get_active_jurisdiction()
    portfolio["jurisdiction"] = cur_jur
    portfolio["controlPlaneJurisdiction"] = cur_jur["code"]
    portfolio["controlPlaneEndpoint"] = cur_jur["endpoint"]
    return portfolio


async def _run_stream_scan_worker(
    user_token: str | None,
    final_account: str | None,
    requested_jur: str | None,
    cfg: dict[str, Any],
    queue: asyncio.Queue[dict[str, Any] | None],
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Execute live organization scan in the background and stream progress updates to the SSE queue."""
    def _progress_callback(data: dict[str, Any]) -> None:
        """Forward real-time scan event to async SSE queue."""
        loop.call_soon_threadsafe(queue.put_nowait, {**data, "done": False})

    try:
        raw_data = await collect_all_organizations(
            explicit_token=user_token,
            account=final_account,
            contract_start_date=cfg.get("contractStartDate"),
            progress_callback=_progress_callback,
        )
        portfolio = _build_stream_completion_portfolio(raw_data, requested_jur, cfg)
        total_pdus = portfolio.get("summary", {}).get("totalPdus", 0)
        total_orgs = portfolio.get("summary", {}).get("totalOrganizations", 0)
        msg = f"Scan complete! {total_pdus:,} PDUs across {total_orgs} organization(s)."

        loop.call_soon_threadsafe(
            queue.put_nowait,
            {
                "percent": 100,
                "stage": "COMPLETE",
                "message": msg,
                "detail": "Dashboard ready",
                "portfolio": portfolio,
                "done": True,
            },
        )
    except Exception as exc:
        loop.call_soon_threadsafe(
            queue.put_nowait,
            {
                "percent": 100,
                "stage": "ERROR",
                "message": f"Scan failed: {str(exc)}",
                "error": str(exc),
                "done": True,
            },
        )
    finally:
        loop.call_soon_threadsafe(queue.put_nowait, None)


async def _stream_queue_generator(
    queue: asyncio.Queue[dict[str, Any] | None],
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


@app.get("/api/scan/stream")
@app.post("/api/scan/stream")
async def api_scan_stream(
    request: Request,
    account: str | None = Query(default=None),
    jurisdiction: str | None = Query(default=None),
) -> StreamingResponse:
    """Real-time streaming endpoint emitting Server-Sent Events (SSE) during live organization scanning."""
    queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    target_account, body_token, requested_jur = await _extract_stream_request_params(
        request, account, jurisdiction
    )
    if requested_jur:
        set_active_jurisdiction(requested_jur)

    user_token = extract_bearer_token(request, body_token)
    final_account = get_request_user_account(request, target_account)
    cfg = load_config()

    asyncio.create_task(
        _run_stream_scan_worker(
            user_token=user_token,
            final_account=final_account,
            requested_jur=requested_jur,
            cfg=cfg,
            queue=queue,
            loop=loop,
        )
    )

    return StreamingResponse(
        _stream_queue_generator(queue),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/demo")
async def api_load_demo_dataset() -> JSONResponse:
    """Reset configuration overrides and load the bundled synthetic multi-organization demo dataset."""
    cfg = load_config()
    cfg["regionOverrides"] = {}
    save_config(cfg)

    demo_data = get_demo_dataset()
    await populate_contract_api_calls(
        demo_data,
        contract_start_date=cfg.get("contractStartDate"),
    )
    save_raw_dataset(demo_data, merge_existing=False)

    portfolio = recalculate_portfolio(
        demo_data,
        entitlement_pdu=cfg.get("entitlementPdu", 500),
        include_shared_flows=cfg.get("includeSharedFlows", True),
        region_overrides={},
        entitlement_envs=cfg.get("entitlementEnvs", 6),
        entitlement_yearly_calls=cfg.get("entitlementYearlyCalls", 1_500_000_000),
        contract_start_date=cfg.get("contractStartDate"),
    )
    history = append_history_snapshot(portfolio)
    portfolio["history"] = history
    portfolio["auth"] = get_gcp_user_auth_info()
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


def _filter_export_portfolio(
    portfolio: dict[str, Any],
    org: str | None,
    env: str | None,
    runtime: str | None,
) -> dict[str, Any]:
    """Filter portfolio organizations, environments, and deployments for CSV export."""
    selected_orgs = (
        {o.strip() for o in org.split(",") if o.strip() and o.strip() != "ALL"}
        if org
        else set()
    )
    if not (selected_orgs or env or runtime):
        return portfolio

    return {
        **portfolio,
        "organizations": [
            o for o in portfolio["organizations"]
            if _matches_export_filter(o, selected_orgs, None, runtime)
        ],
        "environments": [
            e for e in portfolio["environments"]
            if _matches_export_filter(e, selected_orgs, env, runtime)
        ],
        "deployments": [
            d for d in portfolio["deployments"]
            if _matches_export_filter(d, selected_orgs, env, runtime)
        ],
    }


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
    report_type: str,
    org: str | None = Query(default=None, description="Optional filter by organization(s), comma-separated"),
    env: str | None = Query(default=None, description="Optional filter by environment"),
    runtime: str | None = Query(default=None, description="Optional filter by CLOUD or HYBRID"),
    hideZero: bool = Query(default=False, description="Hide 0-PDU environments in export"),
) -> Response:
    """Generate and export CSV reports or a bundled ZIP archive for Google Sheets."""
    portfolio = get_current_portfolio()
    filtered_portfolio = _filter_export_portfolio(portfolio, org, env, runtime)
    ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return _generate_report_response(report_type, filtered_portfolio, ts_str, hideZero)


def _build_cli_arg_parser(default_host: str, default_port: int) -> argparse.ArgumentParser:
    """Construct CLI argument parser for server runtime flags and export options."""
    parser = argparse.ArgumentParser(
        description="Apigee Entitlement Tracker (X & Hybrid) — Web Dashboard & CSV Exporter"
    )
    parser.add_argument(
        "--host",
        default=default_host,
        help=f"Bind host (default: {default_host})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=default_port,
        help=f"Bind port (default: {default_port})",
    )
    parser.add_argument(
        "--entitlement",
        type=int,
        default=None,
        help="Set cross-organization PDU entitlement number",
    )
    parser.add_argument(
        "--account",
        type=str,
        default=None,
        help="GCP User Account email to authenticate with (defaults to active gcloud user account)",
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
    auth_info = get_gcp_user_auth_info()
    acct = account or auth_info.get("activeAccount")
    logger.info("Scanning live authorized Apigee organizations as GCP User: %s ...", acct)

    def _cli_progress(evt: dict[str, Any]) -> None:
        """Log real-time scan event percentages to console."""
        pct = evt.get("percent", 0)
        msg = evt.get("message", "")
        logger.info("[%3d%%] %s", pct, msg)

    raw_data = asyncio.run(collect_all_organizations(account=acct, progress_callback=_cli_progress))
    save_raw_dataset(raw_data)
    portfolio = get_current_portfolio()
    append_history_snapshot(portfolio)
    logger.info(
        "Scan complete: %s authorized orgs, %s total PDUs.",
        portfolio['summary']['totalOrganizations'],
        f"{portfolio['summary']['totalPdus']:,}",
    )


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


def main() -> None:
    """CLI entry point for running server, scanning live data, or generating exports."""
    import threading
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

    if args.entitlement is not None:
        cfg = load_config()
        cfg["entitlementPdu"] = max(1, args.entitlement)
        save_config(cfg)

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
