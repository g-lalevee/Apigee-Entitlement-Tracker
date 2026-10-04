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
import datetime
import html
import json
import os
import pathlib
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
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
    DEFAULT_GOOGLE_CLIENT_ID,
    build_google_oidc_auth_url,
    clear_active_oidc_session,
    collect_all_organizations,
    discover_authorized_organizations,
    exchange_google_oidc_code,
    fetch_monitoring_pdu_timeseries,
    get_active_oidc_session,
    get_demo_dataset,
    get_gcp_user_auth_info,
    populate_contract_api_calls,
    recalculate_portfolio,
    set_active_oidc_session,
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

BASE_DIR = pathlib.Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
STATIC_DIR = BASE_DIR / "static"
DATA_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = DATA_DIR / "config.json"
PDU_LIMIT_PATH = BASE_DIR / "pdu_limit.json"
RAW_CACHE_PATH = DATA_DIR / "latest_raw.json"
HISTORY_PATH = DATA_DIR / "history.json"
DISCOVERED_ORGS_PATH = DATA_DIR / "discovered_orgs.json"


DEFAULT_CONFIG: Dict[str, Any] = {
    "entitlementPdu": 1500,          # Cross-organization manual PDU entitlement stored permanently in pdu_limit.json
    "entitlementEnvs": 20,           # Cross-organization Environment Units (Env × Regions) entitlement
    "entitlementYearlyCalls": 100_000_000,  # Cross-organization Yearly API Calls entitlement over Contract Period
    "contractStartDate": f"{datetime.datetime.now(datetime.timezone.utc).year}-01-01",
    "includeSharedFlows": True,
    "regionOverrides": {},           # Map of "org:env" -> region count
    "orgFilter": [],
    "gcpAccount": None,
}


def load_entitlement_limits() -> Dict[str, Any]:
    """Read the permanent Entitlement Limits & Contract Start Date from pdu_limit.json if present."""
    if PDU_LIMIT_PATH.exists():
        try:
            payload = json.loads(PDU_LIMIT_PATH.read_text(encoding="utf-8"))
            out: Dict[str, Any] = {}
            if int(payload.get("entitlementPdu", 0)) >= 1:
                out["entitlementPdu"] = int(payload["entitlementPdu"])
            if int(payload.get("entitlementEnvs", 0)) >= 1:
                out["entitlementEnvs"] = int(payload["entitlementEnvs"])
            if int(payload.get("entitlementYearlyCalls", 0)) >= 1:
                out["entitlementYearlyCalls"] = int(payload["entitlementYearlyCalls"])
            if payload.get("contractStartDate"):
                out["contractStartDate"] = str(payload["contractStartDate"]).strip()[:10]
            return out
        except Exception:
            pass
    return {}


def save_entitlement_limits(cfg: Dict[str, Any]) -> None:
    """Permanently store PDU, Environment Unit, and Yearly API Call limits + Contract Start Date in pdu_limit.json."""
    PDU_LIMIT_PATH.write_text(
        json.dumps(
            {
                "entitlementPdu": max(1, int(cfg.get("entitlementPdu", 1500))),
                "entitlementEnvs": max(1, int(cfg.get("entitlementEnvs", 20))),
                "entitlementYearlyCalls": max(1, int(cfg.get("entitlementYearlyCalls", 100_000_000))),
                "contractStartDate": str(
                    cfg.get("contractStartDate")
                    or f"{datetime.datetime.now(datetime.timezone.utc).year}-01-01"
                ).strip()[:10],
                "updatedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg.update(data)
        except Exception:
            pass

    perm_limits = load_entitlement_limits()
    if perm_limits:
        cfg.update(perm_limits)
    else:
        save_entitlement_limits(cfg)

    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    if "entitlementPdu" in cfg and cfg["entitlementPdu"] is not None:
        cfg["entitlementPdu"] = max(1, int(cfg["entitlementPdu"]))
    if "entitlementEnvs" in cfg and cfg["entitlementEnvs"] is not None:
        cfg["entitlementEnvs"] = max(1, int(cfg["entitlementEnvs"]))
    if "entitlementYearlyCalls" in cfg and cfg["entitlementYearlyCalls"] is not None:
        cfg["entitlementYearlyCalls"] = max(1, int(cfg["entitlementYearlyCalls"]))
    save_entitlement_limits(cfg)
    disk_cfg = {k: v for k, v in cfg.items() if k != "gcpAccount"}
    CONFIG_PATH.write_text(json.dumps(disk_cfg, indent=2), encoding="utf-8")


def load_raw_dataset() -> Dict[str, Any]:
    if RAW_CACHE_PATH.exists():
        try:
            return json.loads(RAW_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    demo = get_demo_dataset()
    save_raw_dataset(demo, merge_existing=False)
    return demo


def save_raw_dataset(raw_data: Dict[str, Any], merge_existing: bool = True) -> Dict[str, Any]:
    sanitized = dict(raw_data)
    sanitized.pop("account", None)
    if str(sanitized.get("authSource", "")).startswith("GCP User"):
        sanitized["authSource"] = "GCP User Account (OAuth2)"

    if (
        merge_existing
        and RAW_CACHE_PATH.exists()
        and sanitized.get("source") == "LIVE_APIGEE_API"
    ):
        try:
            existing = json.loads(RAW_CACHE_PATH.read_text(encoding="utf-8"))
            if (
                existing.get("source") == "LIVE_APIGEE_API"
                and existing.get("organizations")
                and sanitized.get("organizations")
            ):
                existing_map = {o["organization"]: o for o in existing.get("organizations", [])}
                for new_o in sanitized.get("organizations", []):
                    org_name = new_o["organization"]
                    old_o = existing_map.get(org_name)
                    if old_o and new_o.get("status") == "ERROR" and old_o.get("status") == "OK":
                        continue
                    if old_o and old_o.get("environments") and new_o.get("environments"):
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
                                new_e["yearlyApiCalls"] = old_e.get("yearlyApiCalls", 0)
                                new_e["callsSource"] = old_e.get("callsSource", "APIGEE_ANALYTICS")
                    existing_map[org_name] = new_o
                merged_orgs = list(existing_map.values())
                merged_orgs.sort(
                    key=lambda o: (
                        -sum(e.get("totalPdus", 0) for e in o.get("environments", [])),
                        o["organization"].lower(),
                    )
                )
                sanitized["organizations"] = merged_orgs
                sanitized["totalAuthorizedOrgs"] = len(merged_orgs)
        except Exception:
            pass

    RAW_CACHE_PATH.write_text(json.dumps(sanitized, indent=2), encoding="utf-8")
    return sanitized


def load_history() -> List[Dict[str, Any]]:
    if HISTORY_PATH.exists():
        try:
            return json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def append_history_snapshot(portfolio: Dict[str, Any]) -> List[Dict[str, Any]]:
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
        "entitlementEnvs": summary.get("entitlementEnvs", 20),
        "totalDeployedProxies": summary.get("totalDeployedProxies", 0),
        "totalDeployedSharedFlows": summary.get("totalDeployedSharedFlows", 0),
        "apigeeXPdus": summary.get("apigeeXPdus", 0),
        "apigeeHybridPdus": summary.get("apigeeHybridPdus", 0),
        "standardProxyPdus": summary.get("standardProxyPdus", 0),
        "extensibleProxyPdus": summary.get("extensibleProxyPdus", 0),
        "sharedFlowPdus": summary.get("sharedFlowPdus", 0),
        "totalPdus": summary.get("totalPdus", 0),
        "entitlementPdu": summary.get("entitlementPdu", 1500),
        "utilizationPct": summary.get("utilizationPct", 0),
        "remainingPdus": summary.get("remainingPdus", 0),
        "overagePdus": summary.get("overagePdus", 0),
        "quotaStatus": summary.get("quotaStatus", "HEALTHY"),
        "totalYearlyApiCalls": summary.get("totalYearlyApiCalls", 0),
        "entitlementYearlyCalls": summary.get("entitlementYearlyCalls", 100_000_000),
        "contractStartDate": summary.get("contractStartDate", ""),
    }
    history.insert(0, entry)
    history = history[:100]  # Keep last 100 snapshots
    HISTORY_PATH.write_text(json.dumps(history, indent=2), encoding="utf-8")
    return history


def get_current_portfolio() -> Dict[str, Any]:
    cfg = load_config()
    raw = load_raw_dataset()
    portfolio = recalculate_portfolio(
        raw,
        entitlement_pdu=cfg.get("entitlementPdu", 1500),
        include_shared_flows=cfg.get("includeSharedFlows", True),
        region_overrides=cfg.get("regionOverrides", {}),
        entitlement_envs=cfg.get("entitlementEnvs", 20),
        entitlement_yearly_calls=cfg.get("entitlementYearlyCalls", 100_000_000),
        contract_start_date=cfg.get("contractStartDate"),
    )
    history = load_history()
    if not history:
        history = append_history_snapshot(portfolio)
    portfolio["history"] = history
    portfolio["auth"] = get_gcp_user_auth_info()
    return portfolio


# --- FastAPI Application ---

app = FastAPI(
    title="Apigee Entitlement Tracker (Apigee X & Hybrid)",
    description="Cross-organization Proxy Deployment Unit (PDU), Environment Unit, and Yearly API Call monitoring dashboard and CSV/Sheets exporter.",
    version="1.2.0",
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class SettingsUpdateRequest(BaseModel):
    entitlementPdu: Optional[int] = None
    entitlementEnvs: Optional[int] = None
    entitlementYearlyCalls: Optional[int] = None
    contractStartDate: Optional[str] = None
    includeSharedFlows: Optional[bool] = None
    regionOverrides: Optional[Dict[str, int]] = None
    resetOverrides: Optional[bool] = False


class AuthLoginRequest(BaseModel):
    account: Optional[str] = None
    triggerBrowserLogin: bool = False
    accessToken: Optional[str] = None


class OidcTokenRequest(BaseModel):
    accessToken: str
    email: Optional[str] = None


class LiveScanRequest(BaseModel):
    account: Optional[str] = None
    accessToken: Optional[str] = None
    orgFilter: Optional[List[str]] = None


@app.middleware("http")
async def disable_static_and_auth_cache(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/") or path.startswith("/api/auth"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/")
async def index_page() -> FileResponse:
    return FileResponse(
        str(STATIC_DIR / "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


def extract_bearer_token(request: Request, explicit_token: Optional[str] = None) -> Optional[str]:
    """Extract a user OAuth access token from explicit arguments, Authorization header, query parameter, cookies, or active session."""
    if explicit_token and explicit_token.strip():
        return explicit_token.strip()
    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header.startswith("Bearer "):
        tok = auth_header[7:].strip()
        if tok and not tok.startswith("eyJ"):
            return tok
    q_tok = request.query_params.get("token", "").strip()
    if q_tok and not q_tok.startswith("eyJ"):
        return q_tok
    cookie_tok = request.cookies.get("apigee_access_token", "").strip()
    if cookie_tok and not cookie_tok.startswith("eyJ"):
        return cookie_tok
    oidc = get_active_oidc_session()
    if oidc.get("accessToken"):
        return oidc["accessToken"]
    return None



def get_request_user_account(request: Request, explicit_account: Optional[str] = None) -> Optional[str]:
    """Resolve the active user account email from explicit parameters, IAP headers, OIDC session, or config."""
    if explicit_account and explicit_account.strip():
        return explicit_account.strip()
    iap_user = request.headers.get("x-goog-authenticated-user-email") or request.headers.get("X-Goog-Authenticated-User-Email")
    if iap_user:
        return iap_user.split(":")[-1].strip() if ":" in iap_user else iap_user.strip()
    oidc = get_active_oidc_session()
    if oidc.get("email"):
        return oidc["email"]
    cfg = load_config()
    return cfg.get("gcpAccount")


@app.get("/api/auth-status")
async def check_auth_status(request: Request) -> JSONResponse:
    info = await asyncio.to_thread(get_gcp_user_auth_info)
    iap_user = request.headers.get("x-goog-authenticated-user-email") or request.headers.get("X-Goog-Authenticated-User-Email")
    if iap_user:
        clean_user = iap_user.split(":")[-1] if ":" in iap_user else iap_user
        clean_user = clean_user.strip()
        info["authenticated"] = True
        info["activeAccount"] = clean_user
        info["authSource"] = clean_user
        info["isIap"] = True
        accounts = info.get("accounts", [])
        if not any(a.get("account") == clean_user for a in accounts):
            accounts.insert(0, {"account": clean_user, "status": "ACTIVE"})
        info["accounts"] = accounts

    bearer_tok = extract_bearer_token(request)
    if bearer_tok and not info.get("hasUserToken"):
        try:
            tok_info = await asyncio.to_thread(validate_google_oauth_token, bearer_tok)
            info["hasUserToken"] = True
            if not info.get("activeAccount"):
                info["activeAccount"] = tok_info.get("email")
        except Exception:
            pass

    return JSONResponse(
        info,
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )



@app.get("/api/auth/oidc/auth-url")
async def api_oidc_auth_url(
    request: Request,
    account: Optional[str] = Query(default=None),
) -> JSONResponse:
    """Force Google re-authentication / account selection."""
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    base_url = f"{forwarded_proto}://{host}"
    redirect_uri = f"{base_url}/api/auth/oidc/callback"

    clear_active_oidc_session()

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

    # If OAuth credentials are not yet configured:
    # 1. If running on Cloud Run / behind IAP edge proxy, navigate directly to IAP's CLEAR_LOGIN_COOKIE
    iap_user = request.headers.get("x-goog-authenticated-user-email")
    if iap_user or os.environ.get("K_SERVICE"):
        clear_iap_url = "/_gcp_iap/clear_login_cookie"
        return JSONResponse(
            {
                "authUrl": clear_iap_url,
                "redirectUri": f"{base_url}/_gcp_iap/clear_login_cookie",
                "isOAuthConfigured": False,
                "notice": "OAuth 2.0 Client credentials not configured. Using IAP account switch.",
            }
        )

    # 2. If running locally, launch interactive browser login via gcloud
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


@app.get("/api/auth/oidc/callback")
async def api_oidc_callback(
    request: Request,
    code: Optional[str] = Query(default=None),
    error: Optional[str] = Query(default=None),
) -> Response:
    """Handle Google OIDC OAuth 2.0 redirect callback, exchange authorization code, and persist tokens."""
    if error or not code:
        return RedirectResponse(url=f"/?oidc_error={error or 'no_code'}")

    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    redirect_uri = f"{forwarded_proto}://{host}/api/auth/oidc/callback"

    try:
        session = await asyncio.to_thread(
            exchange_google_oidc_code,
            code=code,
            redirect_uri=redirect_uri,
        )
        email = session.get("email") or "google-user"
        access_token = session.get("accessToken") or ""
        refresh_token = session.get("refreshToken") or ""

        cfg = load_config()
        if email:
            cfg["gcpAccount"] = email
            save_config(cfg)

        safe_email = html.escape(email)
        html_content = f"""<!DOCTYPE html>
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
      sessionStorage.setItem('apigee_user_access_token', {json.dumps(access_token)});
      sessionStorage.setItem('apigee_user_email', {json.dumps(email)});
    }} catch (e) {{
      console.warn('sessionStorage error', e);
    }}
    window.location.replace('/?authenticated=1');
  </script>
</body>
</html>"""

        resp = HTMLResponse(content=html_content)
        if access_token:
            resp.set_cookie(
                key="apigee_access_token",
                value=access_token,
                max_age=3600,
                httponly=True,
                secure=True,
                samesite="lax",
            )
        if refresh_token:
            resp.set_cookie(
                key="apigee_refresh_token",
                value=refresh_token,
                max_age=30 * 86400,
                httponly=True,
                secure=True,
                samesite="lax",
            )
        return resp
    except Exception as exc:
        import urllib.parse
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
        cfg = load_config()
        if info.get("activeAccount"):
            cfg["gcpAccount"] = info.get("activeAccount")
            save_config(cfg)
        info["hasUserToken"] = True
        return JSONResponse(info)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/auth/oidc/logout")
async def api_oidc_logout() -> JSONResponse:
    """Clear the active Google OIDC session and cookies."""
    clear_active_oidc_session()
    resp = JSONResponse({"success": True, "message": "OIDC session cleared"})
    resp.delete_cookie("apigee_access_token")
    resp.delete_cookie("apigee_refresh_token")
    return resp



@app.post("/api/auth/login")
async def api_auth_login(request: Request, payload: AuthLoginRequest) -> JSONResponse:
    """Switch account or initiate Google OIDC login without requiring local gcloud CLI."""
    forwarded_proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host = request.headers.get("x-forwarded-host", request.headers.get("host", request.url.netloc))
    redirect_uri = f"{forwarded_proto}://{host}/api/auth/oidc/callback"

    user_token = extract_bearer_token(request, payload.accessToken)
    user_account = get_request_user_account(request, payload.account)

    if user_token:
        try:
            info = await asyncio.to_thread(
                switch_or_login_gcp_user,
                account=user_account,
                oidc_token=user_token,
            )
            cfg = load_config()
            cfg["gcpAccount"] = info.get("activeAccount")
            save_config(cfg)
            info["hasUserToken"] = True
            return JSONResponse(info)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    if user_account:
        oidc_sess = get_active_oidc_session()
        if oidc_sess.get("accessToken"):
            info = get_gcp_user_auth_info()
            info["hasUserToken"] = True
            return JSONResponse(info)
        info = get_gcp_user_auth_info()
        info["authenticated"] = True
        info["activeAccount"] = user_account
        info["hasUserToken"] = False
        info["needsUserToken"] = True
        return JSONResponse(info)

    try:
        info = await asyncio.to_thread(
            switch_or_login_gcp_user,
            account=payload.account,
            trigger_browser_login=payload.triggerBrowserLogin,
            oidc_token=payload.accessToken,
            redirect_uri=redirect_uri,
        )
        cfg = load_config()
        cfg["gcpAccount"] = info.get("activeAccount")
        save_config(cfg)
        return JSONResponse(info)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/organizations/discover")
async def api_discover_organizations(
    request: Request,
    refresh: bool = Query(default=True, description="Query live GET /v1/organizations"),
    account: Optional[str] = Query(default=None, description="Optional GCP user account email"),
) -> JSONResponse:
    user_token = extract_bearer_token(request)
    user_account = get_request_user_account(request, account)
    existing_orgs = []
    if DISCOVERED_ORGS_PATH.exists():
        try:
            cached = json.loads(DISCOVERED_ORGS_PATH.read_text(encoding="utf-8"))
            existing_orgs = cached.get("organizations", [])
            if not refresh:
                return JSONResponse(cached)
        except Exception:
            pass

    try:
        discovered = await discover_authorized_organizations(
            account=user_account,
            explicit_token=user_token,
        )

        if existing_orgs:
            known = {o["organization"]: o for o in existing_orgs}
            for o in discovered.get("organizations", []):
                known[o["organization"]] = o
            merged = sorted(list(known.values()), key=lambda x: x["organization"].lower())
            discovered["organizations"] = merged
            discovered["totalAuthorizedOrgs"] = len(merged)
        DISCOVERED_ORGS_PATH.write_text(json.dumps(discovered, indent=2), encoding="utf-8")
        return JSONResponse(discovered)
    except Exception as exc:
        if existing_orgs:
            return JSONResponse({"organizations": existing_orgs, "totalAuthorizedOrgs": len(existing_orgs)})
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/portfolio")
async def api_get_portfolio(request: Request) -> JSONResponse:
    portfolio = get_current_portfolio()
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


@app.get("/api/monitoring/timeseries")
async def api_get_monitoring_timeseries(
    request: Request,
    days: int = Query(default=14, ge=1, le=90, description="Lookback window in days"),
    dimension: str = Query(default="pdu", description="Dimension: pdu | environments | calls"),
    refresh: bool = Query(default=False, description="Force live query to Cloud Monitoring / Apigee Analytics API"),
) -> JSONResponse:
    cfg = load_config()
    dim = (dimension or "pdu").strip().lower()
    user_token = extract_bearer_token(request)
    user_account = get_request_user_account(request)
    contract_dt = str(cfg.get("contractStartDate") or "default").replace("-", "")
    cache_suffix = f"_{contract_dt}" if dim == "calls" else f"_{days}d"
    cache_file = DATA_DIR / f"monitoring_ts_{dim}{cache_suffix}.json"

    existing_cached: Optional[Dict[str, Any]] = None
    if cache_file.exists():
        try:
            existing_cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if not refresh:
                return JSONResponse(existing_cached)
        except Exception:
            existing_cached = None

    raw = load_raw_dataset()
    try:
        ts_data = await fetch_monitoring_pdu_timeseries(
            raw_data=raw,
            days=days,
            account=user_account,
            explicit_token=user_token,
            dimension=dim,
            contract_start_date=cfg.get("contractStartDate"),
        )

        # Merge existing multi-org series if live query only covered a subset of organizations
        if existing_cached and existing_cached.get("series") and raw.get("source") == "LIVE_APIGEE_API":
            merged_series_map = {
                (s.get("organization"), s.get("environment")): s
                for s in existing_cached.get("series", [])
            }
            for new_s in ts_data.get("series", []):
                if new_s.get("points"):
                    merged_series_map[(new_s.get("organization"), new_s.get("environment"))] = new_s
            merged_series = list(merged_series_map.values())
            ts_data["series"] = merged_series
            ts_data["seriesCount"] = len(merged_series)

        # If contract API calls were freshly populated into raw_data, persist raw_data too
        if dim == "calls":
            save_raw_dataset(raw)
        cache_file.write_text(json.dumps(ts_data, indent=2), encoding="utf-8")
        return JSONResponse(ts_data)
    except Exception as exc:
        if existing_cached:
            return JSONResponse(existing_cached)
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/settings")
async def api_update_settings(payload: SettingsUpdateRequest) -> JSONResponse:
    cfg = load_config()

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

    contract_date_changed = False
    if payload.contractStartDate is not None and payload.contractStartDate.strip():
        new_dt = payload.contractStartDate.strip()[:10]
        if new_dt != cfg.get("contractStartDate"):
            contract_date_changed = True
        cfg["contractStartDate"] = new_dt

    if payload.includeSharedFlows is not None:
        cfg["includeSharedFlows"] = bool(payload.includeSharedFlows)

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

    # Reload config after scan so any entitlement limit update made during the scan is preserved
    cfg = load_config()
    if payload.orgFilter is not None:
        cfg["orgFilter"] = payload.orgFilter
        save_config(cfg)

    raw_data = save_raw_dataset(raw_data)
    portfolio = recalculate_portfolio(
        raw_data,
        entitlement_pdu=cfg.get("entitlementPdu", 1500),
        include_shared_flows=cfg.get("includeSharedFlows", True),
        region_overrides=cfg.get("regionOverrides", {}),
        entitlement_envs=cfg.get("entitlementEnvs", 20),
        entitlement_yearly_calls=cfg.get("entitlementYearlyCalls", 100_000_000),
        contract_start_date=cfg.get("contractStartDate"),
    )
    history = append_history_snapshot(portfolio)
    portfolio["history"] = history
    portfolio["auth"] = get_gcp_user_auth_info()
    return JSONResponse(portfolio)


@app.get("/api/scan/stream")
@app.post("/api/scan/stream")
async def api_scan_stream(
    request: Request,
    account: Optional[str] = Query(default=None),
    token: Optional[str] = Query(default=None),
) -> StreamingResponse:
    """
    Real-time streaming endpoint for live Apigee scanning with progression bar support.
    Emits Server-Sent Events (SSE) as organizations, environments, and API call traffic
    are discovered, audited, and processed using the logged user's credentials.
    """
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    body_token = None
    target_account = account
    if request.method == "POST":
        try:
            body = await request.json()
            if isinstance(body, dict):
                if body.get("account"):
                    target_account = body["account"]
                if body.get("accessToken"):
                    body_token = body["accessToken"]
        except Exception:
            pass

    user_token = extract_bearer_token(request, token or body_token)
    final_account = get_request_user_account(request, target_account)

    cfg = load_config()

    def progress_callback(data: Dict[str, Any]):
        loop.call_soon_threadsafe(queue.put_nowait, {**data, "done": False})

    async def run_scan_worker():
        try:
            raw_data = await collect_all_organizations(
                explicit_token=user_token,
                account=final_account,
                contract_start_date=cfg.get("contractStartDate"),
                progress_callback=progress_callback,
            )


            raw_data = save_raw_dataset(raw_data)
            portfolio = recalculate_portfolio(
                raw_data,
                entitlement_pdu=cfg.get("entitlementPdu", 1500),
                include_shared_flows=cfg.get("includeSharedFlows", True),
                region_overrides=cfg.get("regionOverrides", {}),
                entitlement_envs=cfg.get("entitlementEnvs", 20),
                entitlement_yearly_calls=cfg.get("entitlementYearlyCalls", 100_000_000),
                contract_start_date=cfg.get("contractStartDate"),
            )
            history = append_history_snapshot(portfolio)
            portfolio["history"] = history
            portfolio["auth"] = get_gcp_user_auth_info()

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

    asyncio.create_task(run_scan_worker())

    async def event_generator():
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                yield f"data: {json.dumps(item)}\n\n"
        except asyncio.CancelledError:
            pass

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/demo")
async def api_load_demo_dataset() -> JSONResponse:
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
        entitlement_pdu=cfg.get("entitlementPdu", 1500),
        include_shared_flows=cfg.get("includeSharedFlows", True),
        region_overrides={},
        entitlement_envs=cfg.get("entitlementEnvs", 20),
        entitlement_yearly_calls=cfg.get("entitlementYearlyCalls", 100_000_000),
        contract_start_date=cfg.get("contractStartDate"),
    )
    history = append_history_snapshot(portfolio)
    portfolio["history"] = history
    portfolio["auth"] = get_gcp_user_auth_info()
    return JSONResponse(portfolio)


@app.get("/api/export/{report_type}")
async def api_export_report(
    report_type: str,
    org: Optional[str] = Query(
        default=None, description="Optional filter by organization(s), comma-separated"
    ),
    env: Optional[str] = Query(default=None, description="Optional filter by environment"),
    runtime: Optional[str] = Query(default=None, description="Optional filter by CLOUD or HYBRID"),
    hideZero: bool = Query(default=False, description="Hide 0-PDU environments in export"),
) -> Response:
    portfolio = get_current_portfolio()
    ts_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    selected_orgs = (
        {o.strip() for o in org.split(",") if o.strip() and o.strip() != "ALL"}
        if org
        else set()
    )

    # Apply optional filters if requested
    if selected_orgs or env or runtime:
        filtered_orgs = [
            o
            for o in portfolio["organizations"]
            if (not selected_orgs or o["organization"] in selected_orgs)
            and (not runtime or o["runtimeType"] == runtime)
        ]
        filtered_envs = [
            e
            for e in portfolio["environments"]
            if (not selected_orgs or e["organization"] in selected_orgs)
            and (not env or e["environment"] == env)
            and (not runtime or e["runtimeType"] == runtime)
        ]
        filtered_deps = [
            d
            for d in portfolio["deployments"]
            if (not selected_orgs or d["organization"] in selected_orgs)
            and (not env or d["environment"] == env)
            and (not runtime or d["runtimeType"] == runtime)
        ]
        portfolio = {
            **portfolio,
            "organizations": filtered_orgs,
            "environments": filtered_envs,
            "deployments": filtered_deps,
        }

    if report_type == "organizations":
        csv_text = generate_organizations_csv(portfolio)
        filename = f"apigee_pdu_organizations_summary_{ts_str}.csv"
        return Response(
            content=csv_text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    elif report_type == "environments":
        csv_text = generate_environments_csv(portfolio, hide_zero=hideZero)
        filename = f"apigee_entitlement_tracker_{ts_str}.csv"
        return Response(
            content=csv_text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    elif report_type == "deployments":
        csv_text = generate_deployments_csv(portfolio)
        filename = f"apigee_pdu_deployments_inventory_{ts_str}.csv"
        return Response(
            content=csv_text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    elif report_type == "history":
        csv_text = generate_history_csv(portfolio.get("history", []))
        filename = f"apigee_pdu_historical_log_{ts_str}.csv"
        return Response(
            content=csv_text,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    elif report_type == "bundle":
        zip_bytes = generate_zip_bundle(portfolio, portfolio.get("history", []))
        filename = f"apigee_pdu_sheets_bundle_{ts_str}.zip"
        return Response(
            content=zip_bytes,
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    else:
        raise HTTPException(
            status_code=400,
            detail="Invalid report_type. Choose from: organizations, environments, deployments, history, bundle.",
        )


def main() -> None:
    import threading
    import webbrowser

    is_cloud_run = bool(os.environ.get("K_SERVICE"))
    default_host = "0.0.0.0" if is_cloud_run else os.environ.get("HOST", "127.0.0.1")
    default_port = int(os.environ.get("PORT", "8080"))

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
    args = parser.parse_args()

    if args.clean:
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
        print(f"Cleaned {removed} local cache/export file(s). Ready for public publishing.")
        return

    if args.entitlement is not None:
        cfg = load_config()
        cfg["entitlementPdu"] = max(1, args.entitlement)
        save_config(cfg)

    if args.scan_live:
        auth_info = get_gcp_user_auth_info()
        acct = args.account or auth_info.get("activeAccount")
        print(f"Scanning live authorized Apigee organizations as GCP User: {acct} ...")

        def cli_progress(evt: Dict[str, Any]):
            pct = evt.get("percent", 0)
            msg = evt.get("message", "")
            print(f"[{pct:3d}%] {msg}")

        raw_data = asyncio.run(collect_all_organizations(account=acct, progress_callback=cli_progress))
        save_raw_dataset(raw_data)
        portfolio = get_current_portfolio()
        append_history_snapshot(portfolio)
        print(
            f"Scan complete: {portfolio['summary']['totalOrganizations']} authorized orgs, "
            f"{portfolio['summary']['totalPdus']:,} total PDUs."
        )

    if args.export_dir:
        out_dir = pathlib.Path(args.export_dir).resolve()
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
        print(f"Exported 4 CSV reports to {out_dir}")
        return

    url = f"http://{args.host}:{args.port}"
    print(f"Starting Apigee Entitlement Tracker Dashboard at {url}")
    if not args.no_open_browser and not is_cloud_run:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

