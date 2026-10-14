"""Apigee Entitlement Tracker — Multi-Organization Collector Engine.

Authenticates using Google Cloud credentials (`google-auth` Application Default Credentials,
OAuth2 access tokens, or `gcloud auth`) against the Apigee Management API
(`https://apigee.googleapis.com/v1`) and Cloud Monitoring API (`https://monitoring.googleapis.com/v3`).

Retrieves authorized Apigee organizations (`GET /v1/organizations`) and calculates consumption
across Apigee X (`CLOUD`) and Apigee Hybrid (`HYBRID`):
  Total Org PDUs = Sum over Environments [ (Deployed Proxies + Deployed Shared Flows) * Attached Regions ]
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import datetime
import json
import logging
import math
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Any
import urllib.parse

import google.auth
import google.auth.exceptions
import google.auth.transport.requests
import requests

logger = logging.getLogger(__name__)

__all__ = [
    "ADC_LOGIN_SCOPES",
    "APIGEE_BASE_URL",
    "APIGEE_JURISDICTIONS",
    "CLOUD_PLATFORM_SCOPE",
    "DEFAULT_GOOGLE_CLIENT_ID",
    "GOOGLE_OIDC_AUTH_URL",
    "GOOGLE_OIDC_TOKEN_URL",
    "GOOGLE_OIDC_USERINFO_URL",
    "GOOGLE_TOKENINFO_URL",
    "MONITORING_BASE_URL",
    "build_google_oidc_auth_url",
    "clear_active_oidc_session",
    "collect_all_organizations",
    "collect_all_organizations_sync",
    "discover_authorized_organizations",
    "discover_authorized_organizations_sync",
    "exchange_google_oidc_code",
    "fetch_monitoring_pdu_timeseries",
    "fetch_monitoring_pdu_timeseries_sync",
    "get_active_jurisdiction",
    "get_active_oidc_session",
    "get_apigee_base_url",
    "get_demo_dataset",
    "get_gcp_user_auth_info",
    "list_available_jurisdictions",
    "populate_contract_api_calls",
    "populate_contract_api_calls_sync",
    "recalculate_portfolio",
    "refresh_google_oauth_token",
    "resolve_contract_period",
    "set_active_jurisdiction",
    "set_active_oidc_session",
    "switch_or_login_gcp_user",
    "time",
    "validate_google_oauth_token",
]

APIGEE_BASE_URL = "https://apigee.googleapis.com/v1"
MONITORING_BASE_URL = "https://monitoring.googleapis.com/v3"

# Official Apigee API Control Plane Hosting Jurisdictions
# Reference: https://docs.cloud.google.com/apigee/docs/locations#available-apigee-api-control-plane-hosting-jurisdictions
APIGEE_JURISDICTIONS: dict[str, dict[str, str]] = {
    "global": {
        "code": "global",
        "region": "Global",
        "name": "Global (Default)",
        "description": "Global (Default) — apigee.googleapis.com",
        "endpoint": "apigee.googleapis.com",
        "baseUrl": "https://apigee.googleapis.com/v1",
    },
    "us": {
        "code": "us",
        "region": "Americas",
        "name": "United States",
        "description": "United States (us - multiple regions) — apigee.us.rep.googleapis.com",
        "endpoint": "apigee.us.rep.googleapis.com",
        "baseUrl": "https://apigee.us.rep.googleapis.com/v1",
    },
    "ca": {
        "code": "ca",
        "region": "Americas",
        "name": "Canada",
        "description": "Canada (ca - multiple regions) — ca-apigee.googleapis.com",
        "endpoint": "ca-apigee.googleapis.com",
        "baseUrl": "https://ca-apigee.googleapis.com/v1",
    },
    "eu": {
        "code": "eu",
        "region": "Europe",
        "name": "European Union",
        "description": "European Union (eu - multiple regions) — apigee.eu.rep.googleapis.com",
        "endpoint": "apigee.eu.rep.googleapis.com",
        "baseUrl": "https://apigee.eu.rep.googleapis.com/v1",
    },
    "de": {
        "code": "de",
        "region": "Europe",
        "name": "Germany",
        "description": "Germany (de - multiple regions) — de-apigee.googleapis.com",
        "endpoint": "de-apigee.googleapis.com",
        "baseUrl": "https://de-apigee.googleapis.com/v1",
    },
    "fr": {
        "code": "fr",
        "region": "Europe",
        "name": "France",
        "description": "France (fr - europe-west9) — fr-apigee.googleapis.com",
        "endpoint": "fr-apigee.googleapis.com",
        "baseUrl": "https://fr-apigee.googleapis.com/v1",
    },
    "ch": {
        "code": "ch",
        "region": "Europe",
        "name": "Switzerland",
        "description": "Switzerland (ch - europe-west6) — ch-apigee.googleapis.com",
        "endpoint": "ch-apigee.googleapis.com",
        "baseUrl": "https://ch-apigee.googleapis.com/v1",
    },
    "au": {
        "code": "au",
        "region": "Asia-Pacific",
        "name": "Australia",
        "description": "Australia (au - multiple regions) — au-apigee.googleapis.com",
        "endpoint": "au-apigee.googleapis.com",
        "baseUrl": "https://au-apigee.googleapis.com/v1",
    },
    "in": {
        "code": "in",
        "region": "Asia-Pacific",
        "name": "India",
        "description": "India (in - multiple regions) — apigee.in.rep.googleapis.com",
        "endpoint": "apigee.in.rep.googleapis.com",
        "baseUrl": "https://apigee.in.rep.googleapis.com/v1",
    },
    "jp": {
        "code": "jp",
        "region": "Asia-Pacific",
        "name": "Japan",
        "description": "Japan (jp - multiple regions) — jp-apigee.googleapis.com",
        "endpoint": "jp-apigee.googleapis.com",
        "baseUrl": "https://jp-apigee.googleapis.com/v1",
    },
    "qa": {
        "code": "qa",
        "region": "Middle East",
        "name": "Qatar",
        "description": "Qatar (qa - me-central1) — qa-apigee.googleapis.com",
        "endpoint": "qa-apigee.googleapis.com",
        "baseUrl": "https://qa-apigee.googleapis.com/v1",
    },
    "sa": {
        "code": "sa",
        "region": "Middle East",
        "name": "Saudi Arabia",
        "description": "Saudi Arabia (sa - me-central2) — sa-apigee.googleapis.com",
        "endpoint": "sa-apigee.googleapis.com",
        "baseUrl": "https://sa-apigee.googleapis.com/v1",
    },
    "il": {
        "code": "il",
        "region": "Middle East",
        "name": "Israel",
        "description": "Israel (il - me-west1) — il-apigee.googleapis.com",
        "endpoint": "il-apigee.googleapis.com",
        "baseUrl": "https://il-apigee.googleapis.com/v1",
    },
}

_ACTIVE_JURISDICTION = "global"


def get_active_jurisdiction() -> dict[str, str]:
    """Return the currently selected Apigee control plane jurisdiction configuration."""
    global _ACTIVE_JURISDICTION
    return APIGEE_JURISDICTIONS.get(_ACTIVE_JURISDICTION, APIGEE_JURISDICTIONS["global"])


def get_apigee_base_url() -> str:
    """Return the base URL of the active Apigee control plane jurisdiction."""
    return get_active_jurisdiction()["baseUrl"]


def list_available_jurisdictions() -> list[dict[str, str]]:
    """Return a list of all configured Apigee control plane jurisdictions."""
    return list(APIGEE_JURISDICTIONS.values())


def _register_custom_jurisdiction(url_str: str) -> dict[str, str]:
    """Parse and configure a direct custom Apigee endpoint URL."""
    global _ACTIVE_JURISDICTION
    custom_base = url_str.rstrip("/")
    if not custom_base.endswith("/v1"):
        custom_base += "/v1"
    endpoint = custom_base.replace("https://", "").replace("http://", "").replace("/v1", "")
    info = {
        "code": "custom",
        "region": "Custom",
        "name": "Custom",
        "description": f"Custom Endpoint ({custom_base})",
        "endpoint": endpoint,
        "baseUrl": custom_base,
    }
    APIGEE_JURISDICTIONS["custom"] = info
    _ACTIVE_JURISDICTION = "custom"
    if hasattr(_thread_local, "session"):
        delattr(_thread_local, "session")
    return info


def set_active_jurisdiction(code_or_url: str | None) -> dict[str, str]:
    """Set active jurisdiction by code ('global', 'eu', 'us', etc.) or hostname/URL."""
    global _ACTIVE_JURISDICTION
    if not code_or_url:
        return get_active_jurisdiction()
    clean = str(code_or_url).strip().lower()
    for code, info in APIGEE_JURISDICTIONS.items():
        if clean in (code, info["endpoint"].lower(), info["baseUrl"].lower(), info["name"].lower()):
            _ACTIVE_JURISDICTION = code
            if hasattr(_thread_local, "session"):
                delattr(_thread_local, "session")
            return info
    if clean.startswith("http://") or clean.startswith("https://"):
        return _register_custom_jurisdiction(clean)
    return get_active_jurisdiction()


# Google OIDC & OAuth 2.0 endpoints
GOOGLE_OIDC_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OIDC_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_OIDC_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"

DEFAULT_GOOGLE_CLIENT_ID = ""

ADC_LOGIN_SCOPES: list[str] = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/cloud-platform",
]
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

_thread_local = threading.local()

_ACTIVE_OIDC_SESSION: dict[str, Any] = {}
_OIDC_LOCK = threading.Lock()
_SESSION_CACHE_FILE = Path(__file__).resolve().parent / "data" / ".session.json"


def _save_session_to_disk(sess: dict[str, Any]) -> None:
    """Safely persist active session dictionary to disk with secure permissions."""
    try:
        _SESSION_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_file = _SESSION_CACHE_FILE.with_suffix(".tmp")
        tmp_file.write_text(json.dumps(sess, indent=2), encoding="utf-8")
        try:
            os.chmod(tmp_file, 0o600)
        except OSError as exc:
            logger.debug("Failed setting file permissions on session cache: %s", exc)
        tmp_file.replace(_SESSION_CACHE_FILE)
    except OSError as exc:
        logger.debug("Failed persisting session cache to disk: %s", exc)


def _load_session_from_disk() -> dict[str, Any]:
    """Load cached session dictionary from disk if available."""
    try:
        if _SESSION_CACHE_FILE.exists():
            content = _SESSION_CACHE_FILE.read_text(encoding="utf-8")
            if content.strip():
                return json.loads(content)
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Failed loading session cache from disk: %s", exc)
    return {}


def set_active_oidc_session(session: dict[str, Any]) -> None:
    """Set the active user session and persist to disk cache."""
    global _ACTIVE_OIDC_SESSION
    with _OIDC_LOCK:
        _ACTIVE_OIDC_SESSION = dict(session)
    if hasattr(_thread_local, "session"):
        delattr(_thread_local, "session")
    _save_session_to_disk(session)


def get_active_oidc_session() -> dict[str, Any]:
    """Retrieve the current active user session, restoring from disk cache if needed."""
    global _ACTIVE_OIDC_SESSION
    with _OIDC_LOCK:
        if not _ACTIVE_OIDC_SESSION:
            disk_sess = _load_session_from_disk()
            if disk_sess:
                _ACTIVE_OIDC_SESSION = disk_sess
        return dict(_ACTIVE_OIDC_SESSION)


def clear_active_oidc_session() -> None:
    """Clear the active user session and delete disk cache."""
    global _ACTIVE_OIDC_SESSION
    with _OIDC_LOCK:
        _ACTIVE_OIDC_SESSION = {}
    if hasattr(_thread_local, "session"):
        delattr(_thread_local, "session")
    try:
        if _SESSION_CACHE_FILE.exists():
            _SESSION_CACHE_FILE.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("Failed unlinking session cache: %s", exc)


def _validate_tokeninfo(clean_token: str) -> dict[str, Any] | None:
    """Attempt token validation via Google OAuth2 tokeninfo endpoint."""
    try:
        resp = requests.get(
            f"{GOOGLE_TOKENINFO_URL}?access_token={clean_token}",
            timeout=8.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            email = data.get("email") or data.get("sub", "")
            scope = data.get("scope", "")
            if CLOUD_PLATFORM_SCOPE not in scope:
                raise ValueError(f"Token lacks required scope: {CLOUD_PLATFORM_SCOPE}")
            return {
                "valid": True,
                "email": email,
                "scope": scope,
                "expiresIn": data.get("expires_in"),
                "authMethod": "GOOGLE_OIDC_TOKENINFO",
            }
        err_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        err_desc = err_data.get("error_description") or f"HTTP {resp.status_code}"
        logger.debug("Tokeninfo validation failed: %s", err_desc)
    except requests.RequestException as exc:
        logger.debug("Tokeninfo request error: %s", exc)
    return None


def _validate_userinfo(clean_token: str) -> dict[str, Any] | None:
    """Attempt token validation via Google OIDC userinfo endpoint."""
    try:
        ui_resp = requests.get(
            GOOGLE_OIDC_USERINFO_URL,
            headers={"Authorization": f"Bearer {clean_token}"},
            timeout=8.0,
        )
        if ui_resp.status_code == 200:
            ui_data = ui_resp.json()
            return {
                "valid": True,
                "email": ui_data.get("email"),
                "name": ui_data.get("name"),
                "sub": ui_data.get("sub"),
                "authMethod": "GOOGLE_OIDC_USERINFO",
            }
    except requests.RequestException as exc:
        logger.debug("Userinfo request error: %s", exc)
    return None


def validate_google_oauth_token(token: str) -> dict[str, Any]:
    """Validate a Google OAuth2 access token or ID token via Google's OAuth2 endpoints."""
    if not token or not token.strip():
        raise ValueError("OAuth2 token is required")

    clean_token = token.strip()
    info = _validate_tokeninfo(clean_token)
    if info is not None:
        return info

    user_info = _validate_userinfo(clean_token)
    if user_info is not None:
        return user_info

    raise ValueError("Invalid Google OAuth2 token. Failed tokeninfo and userinfo verification.")


def build_google_oidc_auth_url(
    redirect_uri: str,
    client_id: str | None = None,
    prompt_account: str | None = None,
    state: str | None = None,
) -> str:
    """Build a Google OAuth 2.0 / OIDC authorization URL for browser redirect."""
    cid = client_id or os.environ.get("GOOGLE_CLIENT_ID", DEFAULT_GOOGLE_CLIENT_ID)
    scopes = " ".join(ADC_LOGIN_SCOPES)
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scopes,
        "access_type": "offline",
        "prompt": "consent select_account",
        "include_granted_scopes": "true",
    }
    if state:
        params["state"] = state
    if prompt_account:
        params["login_hint"] = prompt_account

    return f"{GOOGLE_OIDC_AUTH_URL}?{urllib.parse.urlencode(params)}"


def _fetch_userinfo_for_token(access_token: str) -> dict[str, Any]:
    """Retrieve profile metadata for a verified OAuth2 access token."""
    try:
        ui_resp = requests.get(
            GOOGLE_OIDC_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=8.0,
        )
        if ui_resp.status_code == 200:
            return ui_resp.json()
    except requests.RequestException as exc:
        logger.debug("Failed retrieving userinfo for access token: %s", exc)
    return {}


def exchange_google_oidc_code(
    code: str,
    redirect_uri: str,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict[str, Any]:
    """Exchange a Google OIDC authorization code for access and ID tokens."""
    cid = client_id or os.environ.get("GOOGLE_CLIENT_ID", DEFAULT_GOOGLE_CLIENT_ID)
    sec = client_secret or os.environ.get("GOOGLE_CLIENT_SECRET")

    payload = {
        "code": code,
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    if sec:
        payload["client_secret"] = sec

    resp = requests.post(GOOGLE_OIDC_TOKEN_URL, data=payload, timeout=12.0)
    if resp.status_code != 200:
        raise RuntimeError(f"Google OIDC token exchange failed (HTTP {resp.status_code}): {resp.text}")

    token_data = resp.json()
    access_token = token_data.get("access_token")
    id_token = token_data.get("id_token")
    user_info = _fetch_userinfo_for_token(access_token) if access_token else {}

    email = user_info.get("email") or "google-user"
    expires_in = token_data.get("expires_in")
    expires_at = (time.time() + float(expires_in)) if expires_in else None
    session = {
        "email": email,
        "name": user_info.get("name", email),
        "picture": user_info.get("picture"),
        "accessToken": access_token,
        "refreshToken": token_data.get("refresh_token") or get_active_oidc_session().get("refreshToken"),
        "idToken": id_token,
        "expiresIn": expires_in,
        "expiresAt": expires_at,
        "authMethod": "GOOGLE_OIDC_WEB_FLOW",
    }
    set_active_oidc_session(session)
    return session


def refresh_google_oauth_token(
    refresh_token: str,
    client_id: str | None = None,
    client_secret: str | None = None,
) -> dict[str, Any] | None:
    """Refresh an expired Google OAuth access token using an OAuth2 refresh token."""
    if not refresh_token or not str(refresh_token).strip():
        return None
    cid = client_id or os.environ.get("GOOGLE_CLIENT_ID", DEFAULT_GOOGLE_CLIENT_ID)
    sec = client_secret or os.environ.get("GOOGLE_CLIENT_SECRET")
    payload = {
        "client_id": cid,
        "grant_type": "refresh_token",
        "refresh_token": str(refresh_token).strip(),
    }
    if sec:
        payload["client_secret"] = sec
    try:
        resp = requests.post(GOOGLE_OIDC_TOKEN_URL, data=payload, timeout=10.0)
        if resp.status_code == 200:
            data = resp.json()
            new_access_token = data.get("access_token")
            expires_in = data.get("expires_in", 3600)
            if new_access_token:
                curr_sess = get_active_oidc_session()
                if curr_sess:
                    curr_sess.update(
                        {
                            "accessToken": new_access_token,
                            "expiresIn": expires_in,
                            "expiresAt": time.time() + float(expires_in),
                            "authMethod": "GOOGLE_OIDC_REFRESH",
                        }
                    )
                    set_active_oidc_session(curr_sess)
            return data
    except requests.RequestException as exc:
        logger.debug("Failed refreshing OAuth2 token: %s", exc)
    return None


def _find_gcloud_bin() -> str | None:
    """Locate the gcloud CLI binary on the system if installed."""
    candidates = [
        shutil.which("gcloud"),
        str(Path.home() / "google-cloud-sdk/bin/gcloud"),
        "/usr/local/bin/gcloud",
        "/opt/homebrew/bin/gcloud",
    ]
    for path_str in candidates:
        if path_str and Path(path_str).is_file():
            return path_str
    return None


def _is_service_account_credentials(creds: Any) -> bool:
    """Check if credentials represent a Service Account or Cloud Run/GCE metadata identity."""
    if creds is None:
        return False
    sa_email = str(getattr(creds, "service_account_email", "") or "")
    if sa_email and sa_email != "default" and sa_email.endswith(".gserviceaccount.com"):
        return True
    qual = f"{type(creds).__module__}.{type(creds).__qualname__}".lower()
    return "service_account" in qual or "compute_engine" in qual or "serviceaccount" in qual


def _is_user_credentials(creds: Any) -> bool:
    """Check if credentials represent genuine end-user credentials (such as user ADC)."""
    if creds is None or _is_service_account_credentials(creds):
        return False
    try:
        import google.oauth2.credentials
        if isinstance(creds, google.oauth2.credentials.Credentials):
            return True
    except (ImportError, AttributeError):
        pass
    return False


def _should_refresh_explicit_token(token: str | None, oidc_sess: dict[str, Any]) -> bool:
    """Determine whether an incoming explicit token requires updating the session."""
    if not token:
        return False
    if not oidc_sess.get("accessToken"):
        return True
    exp = oidc_sess.get("expiresAt")
    return bool(exp and time.time() >= float(exp))


def _build_explicit_session(
    token: str,
    refresh_token: str | None,
    explicit_account: str | None,
    fallback_refresh: str | None,
) -> dict[str, Any] | None:
    """Validate and construct an explicit OIDC session dictionary."""
    try:
        tok_info = validate_google_oauth_token(token)
        email = tok_info.get("email") or explicit_account or "google-user"
        exp_in = float(tok_info.get("expiresIn", 3600))
        return {
            "email": email,
            "name": email,
            "accessToken": token,
            "refreshToken": refresh_token or fallback_refresh,
            "expiresIn": exp_in,
            "expiresAt": time.time() + exp_in,
            "authMethod": "REQUEST_BEARER_TOKEN",
        }
    except ValueError as exc:
        logger.debug("Failed validating explicit request token: %s", exc)
        return None


def _restore_explicit_token(
    explicit_token: str | None,
    explicit_refresh_token: str | None,
    explicit_account: str | None,
    oidc_sess: dict[str, Any],
) -> dict[str, Any]:
    """Restore or update OIDC session from explicit request parameters."""
    if explicit_token and _should_refresh_explicit_token(explicit_token, oidc_sess):
        new_sess = _build_explicit_session(
            explicit_token,
            explicit_refresh_token,
            explicit_account,
            oidc_sess.get("refreshToken"),
        )
        if new_sess:
            set_active_oidc_session(new_sess)
            return new_sess

    if explicit_refresh_token and not oidc_sess.get("refreshToken"):
        oidc_sess["refreshToken"] = explicit_refresh_token
        set_active_oidc_session(oidc_sess)
    return oidc_sess


def _attempt_token_refresh(refresh_tok: str | None) -> tuple[bool, bool, int | None]:
    """Attempt refreshing token and return standard liveness tuple."""
    if refresh_tok and refresh_google_oauth_token(refresh_tok):
        return True, False, 3600
    return False, True, 0


def _evaluate_token_liveness(
    token: str | None,
    refresh_tok: str | None,
    expires_at_val: Any,
) -> tuple[bool, bool, int | None]:
    """Determine whether an OAuth2 token is valid, expired, and its remaining seconds."""
    if not token:
        return _attempt_token_refresh(refresh_tok) if refresh_tok else (False, False, None)

    if not expires_at_val:
        return True, False, 3600

    now_ts = time.time()
    expires_at = float(expires_at_val)

    if now_ts >= expires_at:
        return _attempt_token_refresh(refresh_tok)

    if now_ts >= (expires_at - 300) and refresh_tok and refresh_google_oauth_token(refresh_tok):
        return True, False, 3600

    return True, False, max(0, int(expires_at - now_ts))


def _probe_gcloud_auth_list(gcloud_bin: str) -> tuple[str | None, list[dict[str, str]]]:
    """Inspect gcloud auth list for active account and accounts list."""
    active_account: str | None = None
    all_accounts: list[dict[str, str]] = []
    try:
        res = subprocess.run(
            [gcloud_bin, "auth", "list", "--format=json"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            for item in json.loads(res.stdout):
                acct = item.get("account", "")
                status = item.get("status", "")
                if acct:
                    all_accounts.append({"account": acct, "status": status})
                    if status == "ACTIVE":
                        active_account = acct
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as exc:
        logger.debug("gcloud auth list probe failed: %s", exc)
    return active_account, all_accounts


def _probe_gcloud_config_list(gcloud_bin: str) -> tuple[str | None, str | None]:
    """Inspect gcloud config list for fallback account and project ID."""
    try:
        res = subprocess.run(
            [gcloud_bin, "config", "list", "--format=json"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            cfg = json.loads(res.stdout)
            core = cfg.get("core", {})
            return core.get("account"), core.get("project")
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError) as exc:
        logger.debug("gcloud config list probe failed: %s", exc)
    return None, None


def _probe_gcloud_active_account(gcloud_bin: str) -> tuple[str | None, list[dict[str, str]], str | None]:
    """Inspect gcloud CLI configuration for active user and project."""
    active_account, all_accounts = _probe_gcloud_auth_list(gcloud_bin)
    cfg_account, project_id = _probe_gcloud_config_list(gcloud_bin)
    return active_account or cfg_account, all_accounts, project_id


def _try_load_local_gcloud_token(gcloud_bin: str, active_account: str | None) -> dict[str, Any] | None:
    """Attempt reading local user ADC token directly from gcloud binary."""
    try:
        res_tok = subprocess.run(
            [gcloud_bin, "auth", "application-default", "print-access-token"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if res_tok.returncode == 0 and res_tok.stdout.strip():
            tok = res_tok.stdout.strip()
            tok_info = validate_google_oauth_token(tok)
            tok_email = tok_info.get("email") or active_account
            if tok_email:
                sess_data = {
                    "email": tok_email,
                    "name": tok_email,
                    "accessToken": tok,
                    "expiresIn": tok_info.get("expiresIn"),
                    "expiresAt": time.time() + float(tok_info.get("expiresIn", 3600)),
                    "authMethod": "LOCAL_GCLOUD_ADC",
                }
                set_active_oidc_session(sess_data)
                return sess_data
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        logger.debug("Local gcloud ADC token read failed: %s", exc)
    return None


def _build_oidc_user_auth_payload(
    oidc_sess: dict[str, Any],
    token_valid: bool,
    token_expired: bool,
    expires_in: int | None,
    cur_jur: dict[str, Any],
    all_jurisdictions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Construct auth status payload when active OIDC user session is present."""
    rem_sec = (
        max(0, int(float(oidc_sess["expiresAt"]) - time.time()))
        if oidc_sess.get("expiresAt")
        else (int(expires_in) if expires_in else 3600)
    )
    email = str(oidc_sess.get("email", ""))
    return {
        "authenticated": token_valid,
        "hasUserToken": token_valid,
        "tokenValid": token_valid,
        "tokenExpired": token_expired,
        "expiresIn": rem_sec if token_valid else 0,
        "isServiceAccountFallback": False,
        "activeAccount": email,
        "accounts": [{"account": email, "status": "ACTIVE" if token_valid else "EXPIRED"}],
        "projectId": None,
        "apigeeBaseUrl": get_apigee_base_url(),
        "controlPlaneJurisdiction": cur_jur["code"],
        "controlPlaneEndpoint": cur_jur["endpoint"],
        "jurisdiction": cur_jur,
        "jurisdictions": all_jurisdictions,
        "authSource": email,
        "authMethod": oidc_sess.get("authMethod", "USER_TOKEN"),
        "userInfo": {
            "email": oidc_sess.get("email"),
            "name": oidc_sess.get("name"),
            "picture": oidc_sess.get("picture"),
        },
        "gcloudPath": _find_gcloud_bin(),
        "isCloudRun": bool(os.environ.get("K_SERVICE")),
    }


def _build_local_gcloud_auth_payload(
    local_sess: dict[str, Any],
    all_accounts: list[dict[str, str]],
    project_id: str | None,
    gcloud_bin: str,
    cur_jur: dict[str, Any],
    all_jurisdictions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Construct auth status payload when local gcloud ADC session is active."""
    email = str(local_sess.get("email", ""))
    return {
        "authenticated": True,
        "hasUserToken": True,
        "tokenValid": True,
        "tokenExpired": False,
        "expiresIn": local_sess.get("expiresIn", 3600),
        "activeAccount": email,
        "accounts": all_accounts or [{"account": email, "status": "ACTIVE"}],
        "projectId": project_id,
        "apigeeBaseUrl": get_apigee_base_url(),
        "controlPlaneJurisdiction": cur_jur["code"],
        "controlPlaneEndpoint": cur_jur["endpoint"],
        "jurisdiction": cur_jur,
        "jurisdictions": all_jurisdictions,
        "authSource": email,
        "authMethod": "LOCAL_GCLOUD_ADC",
        "gcloudPath": gcloud_bin,
        "isCloudRun": False,
    }


def _probe_adc_fallback(
    active_account: str | None,
    project_id: str | None,
    all_accounts: list[dict[str, str]],
) -> tuple[str | None, list[dict[str, str]], str | None, str | None, bool]:
    """Inspect environment ADC when no local active gcloud account is configured."""
    if active_account:
        return active_account, all_accounts, project_id, None, False
    try:
        creds, default_proj = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
        proj = project_id or default_proj
        if _is_user_credentials(creds):
            acct = "Application Default Credentials"
            return acct, all_accounts + [{"account": acct, "status": "ACTIVE"}], proj, None, True
        return active_account, all_accounts, proj, getattr(creds, "service_account_email", None), False
    except (google.auth.exceptions.GoogleAuthError, OSError) as exc:
        logger.debug("User ADC discovery failed: %s", exc)
        return active_account, all_accounts, project_id, None, False


@dataclass(frozen=True)
class FallbackAuthContext:
    """Context holding resolved credentials and environment auth parameters."""
    active_account: str | None
    all_accounts: list[dict[str, str]]
    project_id: str | None
    runtime_sa: str | None
    has_token: bool
    token_valid: bool
    token_expired: bool
    expires_in: int | None
    gcloud_bin: str | None


def _build_fallback_auth_payload(
    ctx: FallbackAuthContext,
    cur_jur: dict[str, Any],
    all_jurisdictions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Construct fallback auth payload when no direct user session is active."""
    is_authenticated = bool(ctx.active_account and (ctx.has_token or ctx.token_valid))
    return {
        "authenticated": is_authenticated,
        "hasUserToken": ctx.has_token,
        "tokenValid": ctx.token_valid,
        "tokenExpired": ctx.token_expired,
        "expiresIn": ctx.expires_in,
        "isServiceAccountFallback": bool(ctx.runtime_sa and not ctx.has_token),
        "activeAccount": ctx.active_account,
        "accounts": ctx.all_accounts,
        "projectId": ctx.project_id,
        "apigeeBaseUrl": get_apigee_base_url(),
        "controlPlaneJurisdiction": cur_jur["code"],
        "controlPlaneEndpoint": cur_jur["endpoint"],
        "jurisdiction": cur_jur,
        "jurisdictions": all_jurisdictions,
        "authSource": ctx.active_account or "No active GCP User Account",
        "authMethod": "LOCAL_ADC" if ctx.gcloud_bin else "NONE",
        "gcloudPath": ctx.gcloud_bin,
        "isCloudRun": bool(os.environ.get("K_SERVICE")),
        "runtimeServiceAccount": ctx.runtime_sa,
    }


def get_gcp_user_auth_info(
    explicit_token: str | None = None,
    explicit_refresh_token: str | None = None,
    explicit_account: str | None = None,
) -> dict[str, Any]:
    """Inspect the active Google Cloud user or credentials and token expiration status."""
    oidc_sess = _restore_explicit_token(
        explicit_token,
        explicit_refresh_token,
        explicit_account,
        get_active_oidc_session(),
    )
    cur_jur = get_active_jurisdiction()
    all_jurisdictions = list(APIGEE_JURISDICTIONS.values())

    token = oidc_sess.get("accessToken")
    refresh_tok = oidc_sess.get("refreshToken") or explicit_refresh_token
    token_valid, token_expired, expires_in = _evaluate_token_liveness(token, refresh_tok, oidc_sess.get("expiresAt"))

    if oidc_sess.get("email") and (token_valid or token_expired):
        return _build_oidc_user_auth_payload(
            oidc_sess, token_valid, token_expired, expires_in, cur_jur, all_jurisdictions
        )

    return _resolve_non_oidc_auth(
        oidc_sess, token_valid, token_expired, expires_in, cur_jur, all_jurisdictions
    )


def _resolve_non_oidc_auth(
    oidc_sess: dict[str, Any],
    token_valid: bool,
    token_expired: bool,
    expires_in: int | None,
    cur_jur: dict[str, Any],
    all_jurisdictions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Resolve authentication state via local gcloud CLI or Application Default Credentials."""
    gcloud_bin = _find_gcloud_bin()
    active_account, all_accounts, project_id = (
        _probe_gcloud_active_account(gcloud_bin) if gcloud_bin else (None, [], None)
    )

    if gcloud_bin and not oidc_sess.get("accessToken"):
        local_sess = _try_load_local_gcloud_token(gcloud_bin, active_account)
        if local_sess:
            return _build_local_gcloud_auth_payload(
                local_sess, all_accounts, project_id, gcloud_bin, cur_jur, all_jurisdictions
            )

    active_account, all_accounts, project_id, runtime_sa, has_token = _probe_adc_fallback(
        active_account, project_id, all_accounts
    )
    fallback_ctx = FallbackAuthContext(
        active_account=active_account,
        all_accounts=all_accounts,
        project_id=project_id,
        runtime_sa=runtime_sa,
        has_token=has_token,
        token_valid=token_valid,
        token_expired=token_expired,
        expires_in=expires_in,
        gcloud_bin=gcloud_bin,
    )
    return _build_fallback_auth_payload(fallback_ctx, cur_jur, all_jurisdictions)


def _login_via_gcloud_interactive(gcloud_bin: str, account: str | None) -> bool:
    """Execute interactive gcloud auth application-default login."""
    try:
        cmd = [
            gcloud_bin,
            "auth",
            "application-default",
            "login",
            f"--scopes={','.join(ADC_LOGIN_SCOPES)}",
            "--quiet",
        ]
        if account:
            cmd.append(account)
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=180, check=False)
        if res.returncode == 0:
            tok_res = subprocess.run(
                [gcloud_bin, "auth", "application-default", "print-access-token"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if tok_res.returncode == 0 and tok_res.stdout.strip():
                tok = tok_res.stdout.strip()
                tok_info = validate_google_oauth_token(tok)
                if tok_info.get("email"):
                    set_active_oidc_session({
                        "email": tok_info["email"],
                        "accessToken": tok,
                        "name": tok_info["email"],
                        "authMethod": "OIDC",
                    })
                    return True
    except (subprocess.SubprocessError, OSError, ValueError) as exc:
        logger.debug("Interactive gcloud login failed: %s", exc)
    return False


def _validate_gcp_account(account: str | None) -> str | None:
    """Validate GCP account string ensuring no dangerous command injection characters."""
    if not account:
        return None
    cleaned = account.strip()
    if cleaned.startswith("-") or any(c in cleaned for c in " \t\r\n;&|`$"):
        raise ValueError("Invalid GCP account format")
    return cleaned


def _handle_direct_oidc_token(oidc_token: str, account: str | None) -> dict[str, Any]:
    """Store direct OIDC token into active session and return updated auth info."""
    token_info = validate_google_oauth_token(oidc_token.strip())
    email = token_info.get("email") or account or "google-user"
    set_active_oidc_session({
        "email": email,
        "accessToken": oidc_token.strip(),
        "name": email,
        "authMethod": "GOOGLE_OIDC_TOKEN",
    })
    return get_gcp_user_auth_info()


def _handle_browser_login(
    gcloud_bin: str | None,
    account: str | None,
    redirect_uri: str | None,
) -> dict[str, Any]:
    """Execute interactive gcloud login or generate OAuth2 redirect URL."""
    clear_active_oidc_session()
    if gcloud_bin and _login_via_gcloud_interactive(gcloud_bin, account):
        return get_gcp_user_auth_info()

    r_uri = redirect_uri or "http://localhost:8080/api/auth/oidc/callback"
    auth_url = build_google_oidc_auth_url(redirect_uri=r_uri, prompt_account=account)
    info = get_gcp_user_auth_info()
    info["oidcAuthUrl"] = auth_url
    info["authMethod"] = "OIDC_REDIRECT_REQUIRED"
    return info


def _apply_gcp_account_switch(account: str, gcloud_bin: str | None) -> None:
    """Update active OIDC session and local gcloud CLI config for selected account."""
    oidc_sess = get_active_oidc_session()
    if oidc_sess:
        oidc_sess["email"] = account
        set_active_oidc_session(oidc_sess)
    if gcloud_bin:
        try:
            subprocess.run(
                [gcloud_bin, "config", "set", "account", account],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            logger.debug("Failed setting gcloud account config: %s", exc)


def switch_or_login_gcp_user(
    account: str | None = None,
    trigger_browser_login: bool = False,
    oidc_token: str | None = None,
    redirect_uri: str | None = None,
) -> dict[str, Any]:
    """Switch or authenticate GCP user account using Google OIDC / REST APIs or local gcloud."""
    clean_account = _validate_gcp_account(account)

    if hasattr(_thread_local, "session"):
        delattr(_thread_local, "session")

    if oidc_token and oidc_token.strip():
        return _handle_direct_oidc_token(oidc_token, clean_account)

    gcloud_bin = _find_gcloud_bin()
    if trigger_browser_login:
        return _handle_browser_login(gcloud_bin, clean_account, redirect_uri)

    if clean_account:
        _apply_gcp_account_switch(clean_account, gcloud_bin)

    return get_gcp_user_auth_info()


def _try_gcloud_token_session(gcloud_bin: str, target_account: str | None) -> tuple[requests.Session, str, str] | None:
    """Build a requests.Session by printing an access token from local gcloud CLI."""
    subcmds = [
        [gcloud_bin, "auth", "application-default", "print-access-token"],
        [gcloud_bin, "auth", "print-access-token"] + (["--account", target_account] if target_account else []),
    ]
    for subcmd in subcmds:
        try:
            res = subprocess.run(subcmd, capture_output=True, text=True, timeout=8, check=False)
            if res.returncode == 0 and res.stdout.strip():
                sess = requests.Session()
                sess.headers.update({"Authorization": f"Bearer {res.stdout.strip()}", "Accept": "application/json"})
                return sess, get_apigee_base_url(), f"GCP User: {target_account or 'Local User'} (Local ADC)"
        except (subprocess.SubprocessError, OSError) as exc:
            logger.debug("Command %s execution error: %s", subcmd, exc)
    return None


def _create_token_session(token: str, user_label: str) -> tuple[requests.Session, str, str]:
    """Create requests.Session configured with explicit Bearer token."""
    sess = requests.Session()
    sess.headers.update({"Authorization": f"Bearer {token.strip()}", "Accept": "application/json"})
    return sess, get_apigee_base_url(), f"GCP User Token: {user_label} (OAuth2 API)"


def _try_user_adc_session(target_account: str | None) -> tuple[requests.Session, str, str] | None:
    """Attempt creating AuthorizedSession using user Application Default Credentials."""
    try:
        creds, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
        if _is_user_credentials(creds):
            sess = google.auth.transport.requests.AuthorizedSession(creds)
            sess.headers.update({"Accept": "application/json"})
            return sess, get_apigee_base_url(), f"Application Default Credentials: {target_account or 'ADC'} (User)"
    except (google.auth.exceptions.GoogleAuthError, OSError) as exc:
        logger.debug("User ADC session build error: %s", exc)
    return None


def _try_service_account_session(target_account: str | None) -> tuple[requests.Session, str, str]:
    """Attempt creating AuthorizedSession using service account credentials."""
    try:
        creds, _ = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
        sess = google.auth.transport.requests.AuthorizedSession(creds)
        sess.headers.update({"Accept": "application/json"})
        sa_email = getattr(creds, "service_account_email", None) or "Service Account"
        return sess, get_apigee_base_url(), f"Service Account: {sa_email} (Logged User: {target_account or 'User'})"
    except Exception as exc:
        raise RuntimeError(
            f"No valid Google credentials available to query Apigee APIs: {exc}. "
            "Please provide your personal user access token or authenticate with Google."
        ) from exc


def _create_authenticated_session(
    account: str | None = None,
    explicit_token: str | None = None,
) -> tuple[requests.Session, str, str]:
    """Build an authenticated requests.Session for Apigee API queries."""
    auth_info = get_gcp_user_auth_info()
    target_account = account or auth_info.get("activeAccount")

    oidc_sess = get_active_oidc_session()
    token_to_use = (explicit_token.strip() if explicit_token else None) or oidc_sess.get("accessToken")
    if token_to_use:
        user_label = oidc_sess.get("email") or target_account or "User"
        return _create_token_session(token_to_use, user_label)

    gcloud_bin = _find_gcloud_bin()
    if gcloud_bin:
        gcloud_sess = _try_gcloud_token_session(gcloud_bin, target_account)
        if gcloud_sess:
            return gcloud_sess

    adc_sess = _try_user_adc_session(target_account)
    if adc_sess:
        return adc_sess

    return _try_service_account_session(target_account)


def _get_thread_session(
    account: str | None = None,
    explicit_token: str | None = None,
) -> tuple[requests.Session, str, str]:
    """Get or create a thread-local authenticated requests.Session for worker threads."""
    tok_fingerprint = (explicit_token.strip()[-12:] if explicit_token else "") or get_active_oidc_session().get("accessToken", "")[-12:]
    cur_jur = get_active_jurisdiction()["code"]
    cache_key = f"{account or 'default'}:{tok_fingerprint}:{cur_jur}"
    if not hasattr(_thread_local, "session") or getattr(_thread_local, "cache_key", None) != cache_key:
        sess, base_url, auth_desc = _create_authenticated_session(
            account=account,
            explicit_token=explicit_token,
        )
        _thread_local.session = sess
        _thread_local.base_url = base_url
        _thread_local.auth_desc = auth_desc
        _thread_local.cache_key = cache_key
    return _thread_local.session, _thread_local.base_url, _thread_local.auth_desc


def _get_json_sync(
    sess: requests.Session,
    url: str,
    params: dict[str, Any] | None = None,
    timeout: float = 15.0,
) -> tuple[dict[str, Any] | None, str | None]:
    """Perform a synchronous GET request returning parsed JSON and error string."""
    try:
        resp = sess.get(url, params=params, timeout=timeout)
        if resp.status_code == 200:
            if not resp.text or not resp.text.strip():
                return {}, None
            return resp.json(), None
        return None, f"HTTP {resp.status_code}: {resp.text[:250]}"
    except requests.RequestException as exc:
        return None, str(exc)


def discover_authorized_organizations_sync(
    account: str | None = None,
    explicit_token: str | None = None,
) -> dict[str, Any]:
    """Retrieve all Apigee organizations authorized for the user via GET /v1/organizations."""
    sess, base_url, auth_desc = _create_authenticated_session(
        account=account,
        explicit_token=explicit_token,
    )
    orgs_url = f"{base_url}/organizations"
    data, err = _get_json_sync(sess, orgs_url, timeout=20.0)
    if err or data is None:
        raise RuntimeError(f"Failed to list authorized Apigee organizations from {orgs_url}: {err}")

    raw_orgs = data.get("organizations", [])
    organizations: list[dict[str, Any]] = []
    for item in raw_orgs:
        org_name = item.get("organization")
        proj_ids = item.get("projectIds") or ([item.get("projectId")] if item.get("projectId") else [org_name])
        if org_name:
            organizations.append(
                {
                    "organization": org_name,
                    "projectId": proj_ids[0] if proj_ids else org_name,
                    "projectIds": proj_ids,
                }
            )

    organizations.sort(key=lambda x: x["organization"].lower())
    auth_info = get_gcp_user_auth_info()

    return {
        "discoveredAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "account": account or auth_info.get("activeAccount"),
        "authSource": auth_desc,
        "apiEndpoint": orgs_url,
        "totalAuthorizedOrgs": len(organizations),
        "organizations": organizations,
    }


async def discover_authorized_organizations(
    account: str | None = None,
    explicit_token: str | None = None,
) -> dict[str, Any]:
    """Async wrapper for dynamic organization discovery."""
    return await asyncio.to_thread(
        discover_authorized_organizations_sync,
        account,
        explicit_token,
    )


def _resolve_org_runtime_type(
    raw_runtime_type: str,
    org_name: str,
    instances: list[dict[str, Any]],
    props_list: list[dict[str, Any]],
) -> str:
    """Classify runtime type as CLOUD (Apigee X) or HYBRID."""
    props_map = {p.get("name"): p.get("value") for p in props_list if isinstance(p, dict)}
    hybrid_feature_enabled = str(props_map.get("features.hybrid.enabled", "")).lower() == "true"
    if raw_runtime_type == "HYBRID" or (
        len(instances) == 0 and (hybrid_feature_enabled or "hybrid" in org_name.lower())
    ):
        return "HYBRID"
    return raw_runtime_type


def _map_cloud_env_regions(
    instances: list[dict[str, Any]],
    env_names: list[str],
    sess: requests.Session,
    org_base: str,
) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    """Map Cloud environments to their attached regional instances."""
    env_to_regions: dict[str, list[str]] = {e: [] for e in env_names}
    instances_summary: list[dict[str, Any]] = []

    for inst in instances:
        inst_name = inst.get("name", "")
        inst_loc = inst.get("location", inst_name)
        att_data, _ = _get_json_sync(sess, f"{org_base}/instances/{inst_name}/attachments")
        attached_envs: list[str] = []
        for att in (att_data or {}).get("attachments", []):
            env_ref = att.get("environment")
            if env_ref:
                attached_envs.append(env_ref)
                env_to_regions.setdefault(env_ref, [])
                region_label = f"{inst_loc} ({inst_name})" if inst_name != inst_loc else inst_loc
                if region_label not in env_to_regions[env_ref]:
                    env_to_regions[env_ref].append(region_label)

        instances_summary.append(
            {
                "name": inst_name,
                "location": inst_loc,
                "state": inst.get("state", "ACTIVE"),
                "runtimeVersion": inst.get("runtimeVersion", ""),
                "attachedEnvironments": attached_envs,
            }
        )
    return env_to_regions, instances_summary


def _map_hybrid_env_regions(
    env_names: list[str],
    org_name: str,
    hybrid_region_overrides: dict[str, int],
) -> dict[str, list[str]]:
    """Map Hybrid environments to their regional clusters or user overrides."""
    env_to_regions: dict[str, list[str]] = {}
    for env_name in env_names:
        override_count = hybrid_region_overrides.get(
            f"{org_name}:{env_name}",
            hybrid_region_overrides.get(org_name, 0),
        )
        if override_count and override_count > 0:
            env_to_regions[env_name] = [f"hybrid-region-{i + 1}" for i in range(override_count)]
        else:
            env_to_regions[env_name] = ["hybrid-k8s-cluster"]
    return env_to_regions


@dataclass(frozen=True)
class OrgEnvRecordContext:
    """Context holding organization, project, and runtime metadata for record generation."""
    org_name: str
    project_id: str
    runtime_type: str


def _build_proxy_deployments(
    ctx: OrgEnvRecordContext,
    env_name: str,
    env_proxies: list[dict[str, Any]],
    regions: list[str],
) -> tuple[list[dict[str, Any]], int, int]:
    """Build deployment records for proxies and count standard vs extensible proxies."""
    region_count = len(regions)
    deployments: list[dict[str, Any]] = []
    std_proxies, ext_proxies = 0, 0

    for p in env_proxies:
        p_type = p.get("proxyDeploymentType", "EXTENSIBLE" if ctx.runtime_type == "HYBRID" else "STANDARD")
        if p_type not in ("STANDARD", "EXTENSIBLE"):
            p_type = "STANDARD"
        if p_type == "STANDARD":
            std_proxies += 1
        else:
            ext_proxies += 1

        deployments.append({
            "organization": ctx.org_name,
            "projectId": ctx.project_id,
            "runtimeType": ctx.runtime_type,
            "environment": env_name,
            "artifactKind": "API_PROXY",
            "name": p.get("apiProxy", "unknown-proxy"),
            "revision": str(p.get("revision", "1")),
            "proxyDeploymentType": p_type,
            "regionCount": region_count,
            "regions": regions,
            "pdu": region_count,
            "deployStartTime": p.get("deployStartTime"),
            "serviceAccount": p.get("serviceAccount", ""),
        })

    return deployments, std_proxies, ext_proxies


def _build_sf_deployments(
    ctx: OrgEnvRecordContext,
    env_name: str,
    env_sfs: list[dict[str, Any]],
    regions: list[str],
) -> list[dict[str, Any]]:
    """Build deployment records for shared flows in an environment."""
    region_count = len(regions)
    return [
        {
            "organization": ctx.org_name,
            "projectId": ctx.project_id,
            "runtimeType": ctx.runtime_type,
            "environment": env_name,
            "artifactKind": "SHARED_FLOW",
            "name": sf.get("apiProxy", "unknown-sharedflow"),
            "revision": str(sf.get("revision", "1")),
            "proxyDeploymentType": "SHARED_FLOW",
            "regionCount": region_count,
            "regions": regions,
            "pdu": region_count,
            "deployStartTime": sf.get("deployStartTime"),
            "serviceAccount": sf.get("serviceAccount", ""),
        }
        for sf in env_sfs
    ]


def _build_env_summary_record(
    ctx: OrgEnvRecordContext,
    env_name: str,
    regions: list[str],
    std_proxies: int,
    ext_proxies: int,
    shared_flows_count: int,
) -> dict[str, Any]:
    """Build summary dictionary for an environment."""
    region_count = len(regions)
    total_proxies_env = std_proxies + ext_proxies
    total_artifacts_env = total_proxies_env + shared_flows_count

    return {
        "organization": ctx.org_name,
        "runtimeType": ctx.runtime_type,
        "environment": env_name,
        "envType": "COMPREHENSIVE",
        "regions": regions,
        "regionCount": region_count,
        "environmentUnits": region_count,
        "standardProxies": std_proxies,
        "extensibleProxies": ext_proxies,
        "totalProxies": total_proxies_env,
        "sharedFlows": shared_flows_count,
        "totalArtifacts": total_artifacts_env,
        "standardProxyPdus": std_proxies * region_count,
        "extensibleProxyPdus": ext_proxies * region_count,
        "proxyOnlyPdus": total_proxies_env * region_count,
        "sharedFlowPdus": shared_flows_count * region_count,
        "totalPdus": total_artifacts_env * region_count,
        "yearlyApiCalls": 0,
        "callsSource": "PENDING",
        "dailyCalls": {},
    }


def _build_env_and_deployment_records(
    env_names: list[str],
    env_to_regions: dict[str, list[str]],
    proxies_by_env: dict[str, list[dict[str, Any]]],
    sfs_by_env: dict[str, list[dict[str, Any]]],
    ctx: OrgEnvRecordContext,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Construct environments and deployments summary list for an organization."""
    environments_list: list[dict[str, Any]] = []
    deployments_list: list[dict[str, Any]] = []

    for env_name in env_names:
        regions = env_to_regions.get(env_name, [])
        env_proxies = proxies_by_env.get(env_name, [])
        env_sfs = sfs_by_env.get(env_name, [])

        proxy_deps, std_proxies, ext_proxies = _build_proxy_deployments(ctx, env_name, env_proxies, regions)
        sf_deps = _build_sf_deployments(ctx, env_name, env_sfs, regions)

        deployments_list.extend(proxy_deps)
        deployments_list.extend(sf_deps)
        environments_list.append(
            _build_env_summary_record(ctx, env_name, regions, std_proxies, ext_proxies, len(env_sfs))
        )

    return environments_list, deployments_list


def _build_error_org_result(org_name: str, project_id: str, error: str) -> dict[str, Any]:
    """Return error record when organization fetch fails."""
    return {
        "organization": org_name,
        "projectId": project_id,
        "status": "ERROR",
        "error": error,
        "runtimeType": "UNKNOWN",
        "billingType": "UNKNOWN",
        "environments": [],
        "deployments": [],
    }


def _resolve_instances_and_runtime(
    sess: requests.Session,
    org_base: str,
    org_name: str,
    org_meta: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    """Retrieve instances and resolve runtime type for an organization."""
    raw_runtime_type = org_meta.get("runtimeType", "CLOUD")
    instances: list[dict[str, Any]] = []
    if raw_runtime_type == "CLOUD":
        inst_data, _ = _get_json_sync(sess, f"{org_base}/instances")
        instances = (inst_data or {}).get("instances", [])

    props_list = org_meta.get("properties", {}).get("property", [])
    runtime_type = _resolve_org_runtime_type(raw_runtime_type, org_name, instances, props_list)
    return runtime_type, instances


def _group_deployments_by_env(
    deps: list[dict[str, Any]],
    env_names: list[str],
    env_to_regions: dict[str, list[str]],
    runtime_type: str,
) -> dict[str, list[dict[str, Any]]]:
    """Group deployments by environment, registering unlisted environments on the fly."""
    grouped: dict[str, list[dict[str, Any]]] = {e: [] for e in env_names}
    fallback_region = ["hybrid-k8s-cluster"] if runtime_type == "HYBRID" else []
    for d in deps:
        e_name = d.get("environment")
        if not e_name:
            continue
        if e_name not in env_names:
            env_names.append(e_name)
            env_to_regions.setdefault(e_name, fallback_region)
        grouped.setdefault(e_name, []).append(d)
    return grouped


@dataclass(frozen=True)
class OrgDiscoveryContext:
    """Session and API endpoints for an organization."""
    sess: requests.Session
    org_base: str
    org_name: str
    instances: list[dict[str, Any]]


def _resolve_regions_for_runtime(
    runtime_type: str,
    env_names: list[str],
    overrides: dict[str, int],
    ctx: OrgDiscoveryContext,
) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    """Map environments to regions based on Cloud or Hybrid deployment model."""
    if runtime_type == "CLOUD":
        return _map_cloud_env_regions(ctx.instances, env_names, ctx.sess, ctx.org_base)
    return _map_hybrid_env_regions(env_names, ctx.org_name, overrides), []


def _resolve_org_plans(org_meta: dict[str, Any]) -> tuple[str, str]:
    """Resolve billing type and subscription plan with standard defaults."""
    billing_type = org_meta.get("billingType") or org_meta.get("subscriptionType") or "SUBSCRIPTION"
    subscription_plan = org_meta.get("subscriptionPlan") or org_meta.get("subscriptionType") or billing_type
    return billing_type, subscription_plan


def _fetch_deployments_grouped(
    sess: requests.Session,
    org_base: str,
    env_names: list[str],
    env_to_regions: dict[str, list[str]],
    runtime_type: str,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]]]:
    """Fetch deployments and shared flows from Apigee API grouped by environment."""
    proxy_dep_data, _ = _get_json_sync(sess, f"{org_base}/deployments")
    sf_dep_data, _ = _get_json_sync(sess, f"{org_base}/deployments", params={"sharedFlows": "true"})
    all_proxy_deps = (proxy_dep_data or {}).get("deployments", [])
    all_sf_deps = (sf_dep_data or {}).get("deployments", [])

    proxies_by_env = _group_deployments_by_env(all_proxy_deps, env_names, env_to_regions, runtime_type)
    sfs_by_env = _group_deployments_by_env(all_sf_deps, env_names, env_to_regions, runtime_type)
    return proxies_by_env, sfs_by_env


def _count_unique_artifacts(deployments: list[dict[str, Any]], kind: str) -> int:
    """Count unique artifact names for a given kind in deployment records."""
    return len({d["name"] for d in deployments if d.get("artifactKind") == kind})


def _fetch_single_organization_sync(
    org_name: str,
    project_ids: list[str],
    account: str | None = None,
    explicit_token: str | None = None,
    hybrid_region_overrides: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Fetch complete PDU data for a single Apigee Organization (X or Hybrid)."""
    hybrid_overrides = hybrid_region_overrides or {}
    project_id = project_ids[0] if project_ids else org_name
    sess, base_url, _ = _get_thread_session(account=account, explicit_token=explicit_token)
    org_base = f"{base_url}/organizations/{org_name}"

    org_meta, org_err = _get_json_sync(sess, org_base)
    if org_err or not org_meta:
        return _build_error_org_result(org_name, project_id, org_err or "Failed to fetch organization metadata")

    billing_type, subscription_plan = _resolve_org_plans(org_meta)
    env_names: list[str] = [str(e) for e in org_meta.get("environments", [])]

    runtime_type, instances = _resolve_instances_and_runtime(sess, org_base, org_name, org_meta)
    discovery_ctx = OrgDiscoveryContext(sess=sess, org_base=org_base, org_name=org_name, instances=instances)
    env_to_regions, instances_summary = _resolve_regions_for_runtime(
        runtime_type, env_names, hybrid_overrides, discovery_ctx
    )
    proxies_by_env, sfs_by_env = _fetch_deployments_grouped(
        sess, org_base, env_names, env_to_regions, runtime_type
    )

    ctx = OrgEnvRecordContext(org_name=org_name, project_id=project_id, runtime_type=runtime_type)
    environments_list, deployments_list = _build_env_and_deployment_records(
        env_names, env_to_regions, proxies_by_env, sfs_by_env, ctx
    )

    return {
        "organization": org_name,
        "projectId": project_id,
        "status": "OK",
        "error": None,
        "runtimeType": runtime_type,
        "billingType": billing_type,
        "subscriptionPlan": subscription_plan,
        "state": org_meta.get("state", "ACTIVE"),
        "totalProxiesCreated": _count_unique_artifacts(deployments_list, "API_PROXY"),
        "totalSharedFlowsCreated": _count_unique_artifacts(deployments_list, "SHARED_FLOW"),
        "instances": instances_summary,
        "environments": environments_list,
        "deployments": deployments_list,
    }


def _add_years(dt: datetime.datetime, years: int) -> datetime.datetime:
    """Safely increment a datetime by whole years handling leap day."""
    try:
        return dt.replace(year=dt.year + years)
    except ValueError:
        return dt + datetime.timedelta(days=365 * years)


def resolve_contract_period(contract_start_date: str | None = None) -> dict[str, Any]:
    """Resolve the active yearly contract period from a user-provided start date."""
    now = datetime.datetime.now(datetime.timezone.utc)
    default_start = datetime.datetime(now.year, 1, 1, tzinfo=datetime.timezone.utc)
    parsed_start = default_start

    if contract_start_date and str(contract_start_date).strip():
        raw_str = str(contract_start_date).strip()[:10]
        try:
            parsed_start = datetime.datetime.strptime(raw_str, "%Y-%m-%d").replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            parsed_start = default_start

    period_start = parsed_start
    max_lookback_dt = now - datetime.timedelta(days=420)
    while (period_start + datetime.timedelta(days=365)) <= now and period_start < max_lookback_dt:
        period_start = _add_years(period_start, 1)

    period_end = _add_years(period_start, 1)
    query_start = max(period_start, max_lookback_dt)
    query_end = now if now > query_start else query_start + datetime.timedelta(days=1)
    days_elapsed = max(1, (now - period_start).days + 1) if now >= period_start else 1

    return {
        "contractStartDate": parsed_start.strftime("%Y-%m-%d"),
        "periodStart": period_start.strftime("%Y-%m-%d"),
        "periodEnd": period_end.strftime("%Y-%m-%d"),
        "queryStartDt": query_start,
        "queryEndDt": query_end,
        "daysElapsed": days_elapsed,
        "daysInPeriod": max(1, (period_end - period_start).days),
    }


def _query_env_analytics_chunks(
    sess: requests.Session,
    base_url: str,
    org_name: str,
    env_name: str,
    chunks: list[tuple[datetime.datetime, datetime.datetime]],
) -> tuple[dict[str, int], bool]:
    """Query Apigee Analytics endpoint in 90-day time chunks."""
    daily_map: dict[str, int] = {}
    for c_start, c_end in chunks:
        tr_str = f"{c_start.strftime('%m/%d/%Y %H:%M')}~{c_end.strftime('%m/%d/%Y %H:%M')}"
        stats_url = f"{base_url}/organizations/{org_name}/environments/{env_name}/stats/"
        params = {"select": "sum(message_count)", "timeRange": tr_str, "timeUnit": "day"}
        data, err = _get_json_sync(sess, stats_url, params=params, timeout=15.0)
        if err or not data:
            return daily_map, False
        for env_block in data.get("environments", []):
            for metric_block in env_block.get("metrics", []):
                for val_item in metric_block.get("values", []):
                    ts_ms = val_item.get("timestamp")
                    try:
                        cnt = int(round(float(val_item.get("value", 0))))
                    except (ValueError, TypeError):
                        cnt = 0
                    if ts_ms and cnt > 0:
                        day_key = datetime.datetime.fromtimestamp(int(ts_ms) / 1000.0, tz=datetime.timezone.utc).strftime("%Y-%m-%d")
                        daily_map[day_key] = daily_map.get(day_key, 0) + cnt
    return daily_map, True


def _process_monitoring_point(pt: dict[str, Any], env_data: dict[str, Any]) -> None:
    """Update environment daily calls if a higher value is found in Cloud Monitoring."""
    t_str = pt.get("interval", {}).get("endTime") or pt.get("interval", {}).get("startTime")
    val = int(pt.get("value", {}).get("int64Value", 0))
    if t_str and val > 0:
        day_key = t_str[:10]
        prev = env_data["daily"].get(day_key, 0)
        if val > prev:
            env_data["daily"][day_key] = val
            if not env_data["analytics_ok"]:
                env_data["source"] = "CLOUD_MONITORING"


def _query_monitoring_response_counts(
    sess: requests.Session,
    proj_id: str,
    org_name: str,
    q_start: datetime.datetime,
    q_end: datetime.datetime,
    env_results: dict[str, dict[str, Any]],
) -> None:
    """Query Cloud Monitoring metric for supplementary proxy response counts."""
    mon_url = f"{MONITORING_BASE_URL}/projects/{proj_id}/timeSeries"
    mon_params = {
        "filter": f'metric.type="apigee.googleapis.com/proxy/response_count" AND resource.labels.org="{org_name}"',
        "interval.startTime": q_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "interval.endTime": q_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "aggregation.alignmentPeriod": "86400s",
        "aggregation.perSeriesAligner": "ALIGN_SUM",
        "aggregation.crossSeriesReducer": "REDUCE_SUM",
        "aggregation.groupByFields": ["resource.label.org", "resource.label.env"],
    }
    mon_data, mon_err = _get_json_sync(sess, mon_url, params=mon_params, timeout=18.0)
    if mon_err or not mon_data:
        return
    for ts in mon_data.get("timeSeries", []):
        env_name = ts.get("resource", {}).get("labels", {}).get("env")
        if not env_name or env_name not in env_results:
            continue
        for pt in ts.get("points", []):
            _process_monitoring_point(pt, env_results[env_name])


def _fetch_single_org_contract_calls_sync(
    org_obj: dict[str, Any],
    query_start: datetime.datetime,
    query_end: datetime.datetime,
    account: str | None = None,
    explicit_token: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Fetch daily and total API call counts per environment for an organization."""
    org_name = org_obj["organization"]
    proj_id = org_obj.get("projectId", org_name)
    envs = org_obj.get("environments", [])
    if not envs:
        return {}

    sess, apigee_base_url, _ = _get_thread_session(account=account, explicit_token=explicit_token)

    chunks: list[tuple[datetime.datetime, datetime.datetime]] = []
    cur = query_start
    while cur < query_end:
        nxt = min(query_end, cur + datetime.timedelta(days=90))
        chunks.append((cur, nxt))
        cur = nxt

    env_results: dict[str, dict[str, Any]] = {
        e["environment"]: {"daily": {}, "source": "APIGEE_ANALYTICS", "analytics_ok": False}
        for e in envs
    }

    for e in envs:
        env_name = e["environment"]
        if int(e.get("totalArtifacts", 0)) <= 0 and int(e.get("totalPdus", 0)) <= 0:
            continue
        daily_calls, ok = _query_env_analytics_chunks(sess, apigee_base_url, org_name, env_name, chunks)
        if ok:
            env_results[env_name]["daily"] = daily_calls
            env_results[env_name]["analytics_ok"] = True
        else:
            env_results[env_name]["source"] = "CLOUD_MONITORING"

    _query_monitoring_response_counts(sess, proj_id, org_name, query_start, query_end, env_results)
    return env_results


def _generate_demo_calls(
    raw_data: dict[str, Any],
    q_start: datetime.datetime,
    q_end: datetime.datetime,
    period_info: dict[str, Any],
) -> dict[str, Any]:
    """Populate deterministic daily call history for demo simulation dataset."""
    num_days = max(1, (q_end - q_start).days)
    for o in raw_data.get("organizations", []):
        for e in o.get("environments", []):
            base_annual = int(e.get("demoAnnualCallsBase", e.get("totalPdus", 0) * 420_000))
            daily_avg = base_annual / 365.0
            daily_map: dict[str, int] = {}
            total_c = 0
            for d_idx in range(num_days + 1):
                day_dt = q_start + datetime.timedelta(days=d_idx)
                if day_dt > q_end:
                    break
                wave = 0.85 + 0.30 * math.sin((d_idx + len(e["environment"])) * 0.4)
                c_val = max(0, int(round(daily_avg * wave)))
                daily_map[day_dt.strftime("%Y-%m-%d")] = c_val
                total_c += c_val
            e["yearlyApiCalls"] = total_c
            e["callsSource"] = "APIGEE_ANALYTICS_DEMO"
            e["dailyCalls"] = daily_map

    raw_data["contractPeriod"] = {
        "contractStartDate": period_info["contractStartDate"],
        "periodStart": period_info["periodStart"],
        "periodEnd": period_info["periodEnd"],
        "daysElapsed": period_info["daysElapsed"],
        "daysInPeriod": period_info["daysInPeriod"],
    }
    return raw_data


def _apply_org_traffic_result(org_obj: dict[str, Any], org_res: dict[str, Any]) -> None:
    """Update organization environments with collected traffic data."""
    for e in org_obj.get("environments", []):
        e_info = org_res.get(e["environment"], {})
        daily_map = e_info.get("daily", {})
        e["yearlyApiCalls"] = sum(daily_map.values())
        e["callsSource"] = e_info.get("source", "APIGEE_ANALYTICS")
        e["dailyCalls"] = daily_map


def _notify_traffic_progress(
    callback: Callable[[dict[str, Any]], None],
    org_name: str,
    completed: int,
    total: int,
    env_count: int,
) -> None:
    """Send traffic collection progress notification to callback."""
    pct = 68 + int((completed / total) * 28)
    try:
        callback({
            "percent": pct,
            "message": f"Audited API traffic for {org_name} ({completed}/{total})",
            "stage": "TRAFFIC",
            "current": completed,
            "total": total,
            "detail": f"{env_count} envs queried across contract period",
        })
    except Exception as exc:
        logger.debug("Traffic progress callback failed: %s", exc)


def _collect_traffic_for_active_orgs(
    active_orgs: list[dict[str, Any]],
    q_start: datetime.datetime,
    q_end: datetime.datetime,
    account: str | None,
    explicit_token: str | None,
    progress_callback: Callable[[dict[str, Any]], None] | None,
) -> None:
    """Execute concurrent queries for org traffic across active environments."""
    if not active_orgs:
        return
    max_workers = min(10, max(1, len(active_orgs)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_fetch_single_org_contract_calls_sync, o, q_start, q_end, account, explicit_token): o
            for o in active_orgs
        }
        traffic_done = 0
        for fut in as_completed(futures):
            o = futures[fut]
            _apply_org_traffic_result(o, fut.result())
            traffic_done += 1
            if progress_callback:
                _notify_traffic_progress(
                    progress_callback, o["organization"], traffic_done, len(active_orgs), len(o.get("environments", []))
                )


def populate_contract_api_calls_sync(
    raw_data: dict[str, Any],
    contract_start_date: str | None = None,
    account: str | None = None,
    explicit_token: str | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Populate API calls across all environments for the active contract period."""
    period_info = resolve_contract_period(contract_start_date)
    q_start, q_end = period_info["queryStartDt"], period_info["queryEndDt"]

    if raw_data.get("source") == "DEMO_DATASET":
        return _generate_demo_calls(raw_data, q_start, q_end, period_info)

    active_orgs = [o for o in raw_data.get("organizations", []) if len(o.get("environments", [])) > 0]
    _collect_traffic_for_active_orgs(active_orgs, q_start, q_end, account, explicit_token, progress_callback)

    raw_data["contractPeriod"] = {
        "contractStartDate": period_info["contractStartDate"],
        "periodStart": period_info["periodStart"],
        "periodEnd": period_info["periodEnd"],
        "daysElapsed": period_info["daysElapsed"],
        "daysInPeriod": period_info["daysInPeriod"],
    }
    return raw_data


async def populate_contract_api_calls(
    raw_data: dict[str, Any],
    contract_start_date: str | None = None,
    account: str | None = None,
    explicit_token: str | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Async wrapper for populating contract-period API calls."""
    return await asyncio.to_thread(
        populate_contract_api_calls_sync,
        raw_data,
        contract_start_date,
        account,
        explicit_token,
        progress_callback,
    )


def _collect_targets_concurrently(
    selected_targets: list[tuple[str, list[str]]],
    account: str | None,
    explicit_token: str | None,
    hybrid_region_overrides: dict[str, int] | None,
    notify: Callable[..., None],
) -> list[dict[str, Any]]:
    """Query organization details concurrently across target organizations."""
    max_workers = min(10, max(1, len(selected_targets)))
    organizations: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _fetch_single_organization_sync,
                org_name,
                project_ids,
                account=account,
                explicit_token=explicit_token,
                hybrid_region_overrides=hybrid_region_overrides,
            ): org_name
            for org_name, project_ids in selected_targets
        }
        completed_count = 0
        for future in as_completed(futures):
            org_name = futures[future]
            completed_count += 1
            org_res = future.result()
            organizations.append(org_res)
            pct = 10 + int((completed_count / len(selected_targets)) * 58)
            notify(
                pct,
                f"Audited {org_name} ({completed_count}/{len(selected_targets)})",
                stage="ORGANIZATIONS",
                current=completed_count,
                total=len(selected_targets),
                detail=f"{len(org_res.get('environments', []))} envs, {len(org_res.get('deployments', []))} deployments",
            )
    return organizations


def _notify_scan_progress(
    callback: Callable[[dict[str, Any]], None] | None,
    percent: int,
    message: str,
    stage: str = "SCAN",
    **meta: Any,
) -> None:
    """Forward progress update to caller callback safely."""
    if not callback:
        return
    try:
        callback({
            "percent": max(0, min(100, percent)),
            "message": message,
            "stage": stage,
            "current": int(meta.get("current", 0)),
            "total": int(meta.get("total", 0)),
            "detail": str(meta.get("detail", "")),
        })
    except Exception as exc:
        logger.debug("Progress callback error: %s", exc)


def _resolve_selected_targets(
    discovery_orgs: list[dict[str, Any]],
    org_filter: list[str] | None,
) -> list[tuple[str, list[str]]]:
    """Filter discovery organizations according to optional org filter."""
    filter_set = {o.strip() for o in (org_filter or []) if o and o.strip()}
    selected_targets = [
        (item["organization"], item.get("projectIds", [item.get("projectId", item["organization"])]))
        for item in discovery_orgs
        if not filter_set or item["organization"] in filter_set
    ]
    if filter_set and not selected_targets:
        selected_targets = [(o, [o]) for o in sorted(filter_set)]
    return selected_targets


def _build_empty_collection_payload(auth_source: str, account: str | None, api_endpoint: str) -> dict[str, Any]:
    """Return empty dataset when no authorized organizations are found."""
    return {
        "collectedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": "LIVE_APIGEE_API",
        "authSource": auth_source,
        "account": account,
        "apiEndpoint": api_endpoint,
        "totalAuthorizedOrgs": 0,
        "organizations": [],
    }


def collect_all_organizations_sync(
    explicit_token: str | None = None,
    org_filter: list[str] | None = None,
    hybrid_region_overrides: dict[str, int] | None = None,
    account: str | None = None,
    contract_start_date: str | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Discover authorized Apigee organizations and collect live metrics in parallel."""
    _notify_scan_progress(progress_callback, 5, "Discovering authorized Apigee organizations via Google Cloud API...", stage="DISCOVERY")
    discovery = discover_authorized_organizations_sync(account=account, explicit_token=explicit_token)
    selected_targets = _resolve_selected_targets(discovery["organizations"], org_filter)

    if not selected_targets:
        _notify_scan_progress(progress_callback, 100, "No authorized Apigee organizations found for this account.", stage="COMPLETE")
        return _build_empty_collection_payload(discovery["authSource"], account, discovery["apiEndpoint"])

    notify_cb = lambda p, m, **kw: _notify_scan_progress(progress_callback, p, m, **kw)
    organizations = _collect_targets_concurrently(selected_targets, account, explicit_token, hybrid_region_overrides, notify_cb)
    organizations.sort(key=lambda o: o["organization"].lower())
    raw_payload: dict[str, Any] = {
        "collectedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": "LIVE_APIGEE_API",
        "authSource": discovery["authSource"],
        "account": account,
        "apiEndpoint": discovery["apiEndpoint"],
        "totalAuthorizedOrgs": len(discovery["organizations"]),
        "organizations": organizations,
    }

    _notify_scan_progress(progress_callback, 68, "Auditing annual API traffic over active contract period...", stage="TRAFFIC")
    populate_contract_api_calls_sync(
        raw_payload,
        contract_start_date=contract_start_date,
        account=account,
        explicit_token=explicit_token,
        progress_callback=progress_callback,
    )
    _notify_scan_progress(progress_callback, 100, "Collection completed successfully.", stage="COMPLETE")
    return raw_payload


async def collect_all_organizations(
    explicit_token: str | None = None,
    org_filter: list[str] | None = None,
    hybrid_region_overrides: dict[str, int] | None = None,
    account: str | None = None,
    contract_start_date: str | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Async wrapper for dynamic multi-organization collection."""
    return await asyncio.to_thread(
        collect_all_organizations_sync,
        explicit_token,
        org_filter,
        hybrid_region_overrides,
        account,
        contract_start_date,
        progress_callback,
    )


def _needs_calls_population(raw_data: dict[str, Any], period_start_date: str) -> bool:
    """Check if traffic calls need to be fetched or refreshed."""
    current_cp = (raw_data.get("contractPeriod") or {}).get("contractStartDate")
    if current_cp != period_start_date:
        return True
    orgs_list = raw_data.get("organizations", [])
    return not any("dailyCalls" in e for o in orgs_list for e in o.get("environments", []))


def _extract_env_calls_series(
    org_name: str,
    runtime_type: str,
    env: dict[str, Any],
) -> dict[str, Any] | None:
    """Format single environment daily calls as time-series entry."""
    daily_map: dict[str, int] = env.get("dailyCalls") or {}
    if not daily_map and int(env.get("totalPdus", 0)) <= 0:
        return None
    pts = [
        {"timestamp": f"{k}T00:00:00Z", "pdu": int(v), "dailyCalls": int(v)}
        for k, v in sorted(daily_map.items())
    ]
    return {
        "organization": org_name,
        "environment": env["environment"],
        "runtimeType": runtime_type,
        "source": env.get("callsSource", "APIGEE_ANALYTICS"),
        "points": pts,
    }


def _build_calls_dimension_series(
    raw_data: dict[str, Any],
    contract_start_date: str | None,
    account: str | None,
    explicit_token: str | None,
) -> dict[str, Any]:
    """Build historical time-series for the yearly API calls dimension."""
    period_info = resolve_contract_period(
        contract_start_date or (raw_data.get("contractPeriod") or {}).get("contractStartDate")
    )
    if _needs_calls_population(raw_data, period_info["contractStartDate"]):
        populate_contract_api_calls_sync(
            raw_data,
            contract_start_date=period_info["contractStartDate"],
            account=account,
            explicit_token=explicit_token,
        )

    calls_series: list[dict[str, Any]] = []
    for o in raw_data.get("organizations", []):
        r_type = o.get("runtimeType", "CLOUD")
        for e in o.get("environments", []):
            series = _extract_env_calls_series(o["organization"], r_type, e)
            if series:
                calls_series.append(series)

    return {
        "fetchedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "dimension": "calls",
        "metricType": "Apigee Analytics sum(message_count) + apigee.googleapis.com/proxy/response_count",
        "contractStartDate": period_info["contractStartDate"],
        "periodStart": period_info["periodStart"],
        "periodEnd": period_info["periodEnd"],
        "days": period_info["daysElapsed"],
        "alignmentPeriodSeconds": 86400,
        "seriesCount": len(calls_series),
        "series": calls_series,
    }


def _resolve_alignment_seconds(days: int) -> int:
    """Choose Cloud Monitoring alignment interval matching query duration."""
    if days <= 1:
        return 3600
    if days <= 7:
        return 14400
    if days <= 14:
        return 21600
    return 43200


def _parse_monitoring_points_environments(
    time_series_items: Sequence[dict[str, Any]],
    align_seconds: int,
    org_name: str,
    runtime_type: str,
) -> list[dict[str, Any]]:
    """Parse environment unit time series counting distinct regional locations."""
    env_ts_regions: dict[str, dict[str, int]] = {}
    for ts in time_series_items:
        env_name = ts.get("resource", {}).get("labels", {}).get("env", "default")
        for p in ts.get("points", []):
            t_str = p.get("interval", {}).get("endTime") or p.get("interval", {}).get("startTime")
            val = int(p.get("value", {}).get("int64Value", 0))
            if t_str and val > 0:
                try:
                    dt = datetime.datetime.fromisoformat(t_str.replace("Z", "+00:00"))
                    epoch_snap = int(round(dt.timestamp() / align_seconds)) * align_seconds
                    t_clean = datetime.datetime.fromtimestamp(epoch_snap, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                except (ValueError, TypeError):
                    t_clean = t_str
                env_ts_regions.setdefault(env_name, {})[t_clean] = env_ts_regions.get(env_name, {}).get(t_clean, 0) + 1

    return [
        {
            "organization": org_name,
            "environment": env_name,
            "runtimeType": runtime_type,
            "source": "CLOUD_MONITORING",
            "points": [{"timestamp": t_str, "pdu": r_cnt} for t_str, r_cnt in sorted(ts_map.items())],
        }
        for env_name, ts_map in env_ts_regions.items()
    ]


def _parse_monitoring_points_pdus(
    time_series_items: Sequence[dict[str, Any]],
    align_seconds: int,
    org_name: str,
    runtime_type: str,
) -> list[dict[str, Any]]:
    """Parse proxy detail time series mapping peak PDU values."""
    series_out: list[dict[str, Any]] = []
    for ts in time_series_items:
        env_name = ts.get("resource", {}).get("labels", {}).get("env", "default")
        pts_by_bucket: dict[str, int] = {}
        for p in ts.get("points", []):
            t_str = p.get("interval", {}).get("endTime") or p.get("interval", {}).get("startTime")
            val = int(p.get("value", {}).get("int64Value", 0))
            if t_str:
                try:
                    dt = datetime.datetime.fromisoformat(t_str.replace("Z", "+00:00"))
                    epoch_snap = int(round(dt.timestamp() / align_seconds)) * align_seconds
                    t_clean = datetime.datetime.fromtimestamp(epoch_snap, tz=datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    pts_by_bucket[t_clean] = max(pts_by_bucket.get(t_clean, 0), val)
                except (ValueError, TypeError):
                    pts_by_bucket[t_str] = max(pts_by_bucket.get(t_str, 0), val)

        series_out.append({
            "organization": org_name,
            "environment": env_name,
            "runtimeType": runtime_type,
            "source": "CLOUD_MONITORING",
            "points": [{"timestamp": k, "pdu": v} for k, v in sorted(pts_by_bucket.items())],
        })
    return series_out


@dataclass(frozen=True)
class MonitoringQueryConfig:
    """Parameters for Cloud Monitoring proxy details queries."""
    dim: str
    align_seconds: int
    start_str: str
    end_str: str
    account: str | None = None
    explicit_token: str | None = None


def _query_single_org_monitoring(
    target: tuple[str, str, dict[str, Any]],
    cfg: MonitoringQueryConfig,
) -> list[dict[str, Any]]:
    """Query Cloud Monitoring proxy details for a single target organization."""
    org_name, proj_id, org_obj = target
    sess, _, _ = _get_thread_session(account=cfg.account, explicit_token=cfg.explicit_token)
    url = f"{MONITORING_BASE_URL}/projects/{proj_id}/timeSeries"
    group_fields = (
        ["resource.label.org", "resource.label.env", "resource.label.location"]
        if cfg.dim == "environments"
        else ["resource.label.org", "resource.label.env"]
    )
    params = {
        "filter": f'metric.type="apigee.googleapis.com/proxy/details" AND resource.labels.org="{org_name}"',
        "interval.startTime": cfg.start_str,
        "interval.endTime": cfg.end_str,
        "aggregation.alignmentPeriod": f"{cfg.align_seconds}s",
        "aggregation.perSeriesAligner": "ALIGN_MAX",
        "aggregation.crossSeriesReducer": "REDUCE_COUNT",
        "aggregation.groupByFields": group_fields,
    }
    data, err = _get_json_sync(sess, url, params=params, timeout=18.0)
    if err or not data or not data.get("timeSeries"):
        return []
    ts_items = data.get("timeSeries", [])
    r_type = org_obj.get("runtimeType", "CLOUD")
    if cfg.dim == "environments":
        return _parse_monitoring_points_environments(ts_items, cfg.align_seconds, org_name, r_type)
    return _parse_monitoring_points_pdus(ts_items, cfg.align_seconds, org_name, r_type)


def _execute_monitoring_queries(
    active_targets: list[tuple[str, str, dict[str, Any]]],
    cfg: MonitoringQueryConfig,
) -> list[dict[str, Any]]:
    """Execute time-series queries concurrently across active target organizations."""
    if not active_targets:
        return []
    all_series: list[dict[str, Any]] = []
    max_workers = min(10, max(1, len(active_targets)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_query_single_org_monitoring, t, cfg) for t in active_targets]
        for f in futures:
            all_series.extend(f.result())
    return all_series


def _build_monitoring_time_window(days: int, align_seconds: int) -> tuple[str, str]:
    """Calculate aligned start and end ISO timestamps for monitoring queries."""
    now = datetime.datetime.now(datetime.timezone.utc)
    aligned_end_epoch = (int(now.timestamp()) // align_seconds) * align_seconds
    aligned_end = datetime.datetime.fromtimestamp(aligned_end_epoch, tz=datetime.timezone.utc)
    aligned_start = aligned_end - datetime.timedelta(days=days)
    return aligned_start.strftime("%Y-%m-%dT%H:%M:%SZ"), aligned_end.strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_monitoring_pdu_timeseries_sync(
    raw_data: dict[str, Any],
    days: int = 14,
    account: str | None = None,
    explicit_token: str | None = None,
    dimension: str = "pdu",
    contract_start_date: str | None = None,
) -> dict[str, Any]:
    """Retrieve historical time-series across all active Apigee organizations."""
    dim = (dimension or "pdu").strip().lower()
    if dim == "calls":
        return _build_calls_dimension_series(raw_data, contract_start_date, account, explicit_token)

    days = max(1, min(90, int(days)))
    align_seconds = _resolve_alignment_seconds(days)
    start_str, end_str = _build_monitoring_time_window(days, align_seconds)
    cfg = MonitoringQueryConfig(
        dim=dim,
        align_seconds=align_seconds,
        start_str=start_str,
        end_str=end_str,
        account=account,
        explicit_token=explicit_token,
    )

    orgs_list = raw_data.get("organizations", [])
    active_targets = [
        (o["organization"], o.get("projectId", o["organization"]), o)
        for o in orgs_list
        if len(o.get("deployments", [])) > 0 or any(e.get("totalPdus", 0) > 0 for e in o.get("environments", []))
    ]
    all_series = _execute_monitoring_queries(active_targets, cfg)

    return {
        "fetchedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "dimension": dim,
        "metricType": "apigee.googleapis.com/proxy/details",
        "days": days,
        "alignmentPeriodSeconds": align_seconds,
        "seriesCount": len(all_series),
        "series": all_series,
    }


async def fetch_monitoring_pdu_timeseries(
    raw_data: dict[str, Any],
    days: int = 14,
    account: str | None = None,
    explicit_token: str | None = None,
    dimension: str = "pdu",
    contract_start_date: str | None = None,
) -> dict[str, Any]:
    """Async wrapper for multi-dimension time-series collection."""
    return await asyncio.to_thread(
        fetch_monitoring_pdu_timeseries_sync,
        raw_data=raw_data,
        days=days,
        account=account,
        dimension=dimension,
        contract_start_date=contract_start_date,
        explicit_token=explicit_token,
    )


@dataclass(frozen=True)
class RecalculateConfig:
    """Configuration thresholds and parameters for portfolio recalculation."""
    entitlement_pdu: int = 1500
    entitlement_envs: int = 20
    entitlement_yearly_calls: int = 100_000_000
    include_shared_flows: bool = True
    region_overrides: dict[str, int] | None = None


@dataclass(frozen=True)
class OrgRecalculateMeta:
    """Metadata of an organization being recalculated."""
    org_name: str
    runtime_type: str
    billing_type: str
    project_id: str


def _resolve_effective_regions(
    env_name: str,
    orig_regions: list[str],
    org_meta: OrgRecalculateMeta,
    region_overrides: dict[str, int],
) -> tuple[list[str], int, bool]:
    """Determine active regions for an environment considering manual overrides."""
    override_key = f"{org_meta.org_name}:{env_name}"
    if override_key not in region_overrides:
        return orig_regions, len(orig_regions), False

    r_count = max(0, int(region_overrides[override_key]))
    if r_count == len(orig_regions):
        return orig_regions, r_count, True
    if r_count < len(orig_regions):
        return orig_regions[:r_count], r_count, True
    prefix = "hybrid" if org_meta.runtime_type == "HYBRID" else "region"
    synthetic = [f"{prefix}-{i + 1}" for i in range(len(orig_regions), r_count)]
    return orig_regions + synthetic, r_count, True


def _build_env_recalculated_record(
    env: dict[str, Any],
    meta: OrgRecalculateMeta,
    cfg: RecalculateConfig,
    effective_regions: list[str],
    orig_regions: list[str],
    is_overridden: bool,
) -> dict[str, Any]:
    """Assemble final recalculated environment metrics record."""
    r_count = len(effective_regions)
    std_p = int(env.get("standardProxies", 0))
    ext_p = int(env.get("extensibleProxies", 0))
    tot_p = std_p + ext_p
    sf_p = int(env.get("sharedFlows", 0))
    env_total_pdus = (tot_p * r_count) + ((sf_p * r_count) if cfg.include_shared_flows else 0)
    env_yearly_calls = int(env.get("yearlyApiCalls", 0))

    return {
        "organization": meta.org_name,
        "projectId": meta.project_id,
        "runtimeType": meta.runtime_type,
        "billingType": meta.billing_type,
        "environment": env["environment"],
        "envType": env.get("envType", "COMPREHENSIVE"),
        "regions": effective_regions,
        "originalRegionCount": len(orig_regions),
        "regionCount": r_count,
        "environmentUnits": r_count,
        "isOverridden": is_overridden,
        "standardProxies": std_p,
        "extensibleProxies": ext_p,
        "totalProxies": tot_p,
        "sharedFlows": sf_p,
        "totalArtifacts": tot_p + (sf_p if cfg.include_shared_flows else 0),
        "standardProxyPdus": std_p * r_count,
        "extensibleProxyPdus": ext_p * r_count,
        "proxyOnlyPdus": tot_p * r_count,
        "sharedFlowPdus": sf_p * r_count,
        "totalPdus": env_total_pdus,
        "yearlyApiCalls": env_yearly_calls,
        "callsSource": env.get("callsSource", "APIGEE_ANALYTICS"),
        "formulaText": (
            f"({tot_p} proxies{' + ' + str(sf_p) + ' shared flows' if cfg.include_shared_flows else ''}) "
            f"× {r_count} region{'s' if r_count != 1 else ''} = {env_total_pdus} PDUs"
        ),
        "entitlementSharePct": round((env_total_pdus / cfg.entitlement_pdu) * 100, 2),
        "envEntitlementSharePct": round((r_count / cfg.entitlement_envs) * 100, 2),
        "callsEntitlementSharePct": round((env_yearly_calls / cfg.entitlement_yearly_calls) * 100, 2),
    }


def _recalculate_environment(
    env: dict[str, Any],
    meta: OrgRecalculateMeta,
    cfg: RecalculateConfig,
) -> tuple[dict[str, Any], list[str]]:
    """Compute recalculation metrics for a single environment."""
    env_name = env["environment"]
    orig_regions = list(env.get("regions", []))
    overrides = cfg.region_overrides or {}
    effective_regions, _, is_overridden = _resolve_effective_regions(
        env_name, orig_regions, meta, overrides
    )
    env_record = _build_env_recalculated_record(
        env, meta, cfg, effective_regions, orig_regions, is_overridden
    )
    return env_record, effective_regions


def _recalculate_org_environments(
    envs: list[dict[str, Any]],
    meta: OrgRecalculateMeta,
    cfg: RecalculateConfig,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Recalculate all environments for an organization."""
    org_envs_out: list[dict[str, Any]] = []
    org_regions_set: set[str] = set()
    for env in envs:
        env_record, eff_regions = _recalculate_environment(env, meta, cfg)
        org_envs_out.append(env_record)
        org_regions_set.update(eff_regions)
    return org_envs_out, org_regions_set


def _recalculate_org_deployments(
    deployments: list[dict[str, Any]],
    env_map: dict[str, dict[str, Any]],
    include_shared_flows: bool,
) -> list[dict[str, Any]]:
    """Recompute deployment metrics based on effective environment region counts."""
    org_deployments_out: list[dict[str, Any]] = []
    for dep in deployments:
        env_info = env_map.get(dep["environment"])
        eff_r_count = env_info["regionCount"] if env_info else dep.get("regionCount", 1)
        eff_regions = env_info["regions"] if env_info else dep.get("regions", [])
        is_sf = dep.get("artifactKind") == "SHARED_FLOW"
        eff_pdu = eff_r_count if (not is_sf or include_shared_flows) else 0

        org_deployments_out.append({
            **dep,
            "regionCount": eff_r_count,
            "regions": eff_regions,
            "pdu": eff_pdu,
        })
    return org_deployments_out


def _build_recalculated_org_record(
    org: dict[str, Any],
    meta: OrgRecalculateMeta,
    org_envs_out: list[dict[str, Any]],
    org_regions_set: set[str],
    entitlement_pdu: int,
) -> dict[str, Any]:
    """Construct summary dictionary for a recalculated organization."""
    org_deployed_proxies = sum(e["totalProxies"] for e in org_envs_out)
    org_deployed_sfs = sum(e["sharedFlows"] for e in org_envs_out)
    org_total_pdus = sum(e["totalPdus"] for e in org_envs_out)

    return {
        "organization": meta.org_name,
        "projectId": meta.project_id,
        "status": org.get("status", "OK"),
        "error": org.get("error"),
        "runtimeType": meta.runtime_type,
        "billingType": meta.billing_type,
        "subscriptionPlan": org.get("subscriptionPlan", "Enterprise"),
        "state": org.get("state", "ACTIVE"),
        "totalProxiesCreated": org.get("totalProxiesCreated", org_deployed_proxies),
        "totalSharedFlowsCreated": org.get("totalSharedFlowsCreated", org_deployed_sfs),
        "environmentCount": len(org_envs_out),
        "environmentUnits": sum(e["environmentUnits"] for e in org_envs_out),
        "uniqueRegionsCount": len(org_regions_set),
        "uniqueRegions": sorted(org_regions_set),
        "deployedProxiesCount": org_deployed_proxies,
        "deployedSharedFlowsCount": org_deployed_sfs,
        "standardProxyPdus": sum(e["standardProxyPdus"] for e in org_envs_out),
        "extensibleProxyPdus": sum(e["extensibleProxyPdus"] for e in org_envs_out),
        "proxyOnlyPdus": sum(e["proxyOnlyPdus"] for e in org_envs_out),
        "sharedFlowPdus": sum(e["sharedFlowPdus"] for e in org_envs_out),
        "totalPdus": org_total_pdus,
        "yearlyApiCalls": sum(e["yearlyApiCalls"] for e in org_envs_out),
        "entitlementSharePct": round((org_total_pdus / entitlement_pdu) * 100, 2),
        "environments": org_envs_out,
    }


def _recalculate_organization(
    org: dict[str, Any],
    cfg: RecalculateConfig,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Recalculate an entire organization, its environments, and its deployments."""
    meta = OrgRecalculateMeta(
        org_name=org["organization"],
        runtime_type=org.get("runtimeType", "CLOUD"),
        billing_type=org.get("billingType", "SUBSCRIPTION"),
        project_id=org.get("projectId", org["organization"]),
    )
    org_envs_out, org_regions_set = _recalculate_org_environments(
        org.get("environments", []), meta, cfg
    )
    for inst in org.get("instances", []):
        if inst.get("location"):
            org_regions_set.add(inst["location"])

    env_map = {e["environment"]: e for e in org_envs_out}
    org_deployments_out = _recalculate_org_deployments(
        org.get("deployments", []), env_map, cfg.include_shared_flows
    )
    org_record = _build_recalculated_org_record(
        org, meta, org_envs_out, org_regions_set, cfg.entitlement_pdu
    )
    return org_record, org_envs_out, org_deployments_out


def _compute_quota_summary(
    portfolio_totals: dict[str, int],
    period_info: dict[str, Any],
    entitlement_pdu: int,
    entitlement_envs: int,
    entitlement_yearly_calls: int,
) -> dict[str, Any]:
    """Compute overall quota health and utilization statistics across dimensions."""
    total_pdus = portfolio_totals["totalPdus"]
    total_env_units = portfolio_totals["totalEnvironmentUnits"]
    total_calls = portfolio_totals["yearlyApiCalls"]

    util_pct = round((total_pdus / entitlement_pdu) * 100, 1)
    env_util_pct = round((total_env_units / entitlement_envs) * 100, 1)
    calls_util_pct = round((total_calls / entitlement_yearly_calls) * 100, 1)

    overage_pdus = max(0, total_pdus - entitlement_pdu)
    days_elapsed = period_info["daysElapsed"]
    days_in_period = period_info["daysInPeriod"]
    projected_calls = int(round(total_calls * (days_in_period / max(1, days_elapsed))))

    return {
        "entitlementPdu": entitlement_pdu,
        "utilizationPct": util_pct,
        "remainingPdus": max(0, entitlement_pdu - total_pdus),
        "overagePdus": overage_pdus,
        "recommendedAddOnPacks50": math.ceil(overage_pdus / 50) if overage_pdus > 0 else 0,
        "quotaStatus": "OVER_LIMIT" if util_pct > 100 else ("WARNING" if util_pct >= 85 else "HEALTHY"),
        "entitlementEnvs": entitlement_envs,
        "envUtilizationPct": env_util_pct,
        "remainingEnvs": max(0, entitlement_envs - total_env_units),
        "overageEnvs": max(0, total_env_units - entitlement_envs),
        "envQuotaStatus": "OVER_LIMIT" if total_env_units > entitlement_envs else ("WARNING" if env_util_pct >= 85 else "HEALTHY"),
        "totalYearlyApiCalls": total_calls,
        "projectedYearlyApiCalls": projected_calls,
        "entitlementYearlyCalls": entitlement_yearly_calls,
        "callsUtilizationPct": calls_util_pct,
        "remainingYearlyCalls": max(0, entitlement_yearly_calls - total_calls),
        "overageYearlyCalls": max(0, total_calls - entitlement_yearly_calls),
        "callsQuotaStatus": "OVER_LIMIT" if total_calls > entitlement_yearly_calls else ("WARNING" if calls_util_pct >= 85 else "HEALTHY"),
        "contractStartDate": period_info["contractStartDate"],
        "contractPeriodStart": period_info["periodStart"],
        "contractPeriodEnd": period_info["periodEnd"],
        "daysElapsedInContract": days_elapsed,
        "daysInContractPeriod": days_in_period,
    }


def _aggregate_org_metrics(organizations_out: list[dict[str, Any]]) -> dict[str, int]:
    """Aggregate high-level metric counts across organizations in a single pass."""
    metrics = {
        "apigeeXOrgs": 0,
        "apigeeHybridOrgs": 0,
        "apigeeXPdus": 0,
        "apigeeHybridPdus": 0,
        "totalDeployedProxies": 0,
        "totalDeployedSharedFlows": 0,
        "standardProxyPdus": 0,
        "extensibleProxyPdus": 0,
        "proxyOnlyPdus": 0,
        "sharedFlowPdus": 0,
    }
    for o in organizations_out:
        is_hybrid = o.get("runtimeType") == "HYBRID"
        if is_hybrid:
            metrics["apigeeHybridOrgs"] += 1
            metrics["apigeeHybridPdus"] += o.get("totalPdus", 0)
        else:
            metrics["apigeeXOrgs"] += 1
            metrics["apigeeXPdus"] += o.get("totalPdus", 0)
        metrics["totalDeployedProxies"] += o.get("deployedProxiesCount", 0)
        metrics["totalDeployedSharedFlows"] += o.get("deployedSharedFlowsCount", 0)
        metrics["standardProxyPdus"] += o.get("standardProxyPdus", 0)
        metrics["extensibleProxyPdus"] += o.get("extensibleProxyPdus", 0)
        metrics["proxyOnlyPdus"] += o.get("proxyOnlyPdus", 0)
        metrics["sharedFlowPdus"] += o.get("sharedFlowPdus", 0)
    return metrics


def _build_portfolio_summary(
    organizations_out: list[dict[str, Any]],
    all_envs_out: list[dict[str, Any]],
    portfolio_totals: dict[str, int],
    quota_summary: dict[str, Any],
) -> dict[str, Any]:
    """Build high-level portfolio summary metrics dictionary."""
    agg = _aggregate_org_metrics(organizations_out)
    return {
        "totalOrganizations": len(organizations_out),
        "totalEnvironments": len(all_envs_out),
        "totalEnvironmentUnits": portfolio_totals["totalEnvironmentUnits"],
        "totalActiveEnvRegionAttachments": portfolio_totals["totalEnvironmentUnits"],
        "totalPdus": portfolio_totals["totalPdus"],
        **agg,
        **quota_summary,
    }


def _recalculate_all_organizations(
    orgs_data: list[dict[str, Any]],
    cfg: RecalculateConfig,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Recalculate metrics across all organizations and flatten environments/deployments."""
    organizations_out: list[dict[str, Any]] = []
    all_envs_out: list[dict[str, Any]] = []
    all_deps_out: list[dict[str, Any]] = []

    for org in orgs_data:
        org_rec, org_envs, org_deps = _recalculate_organization(org, cfg)
        organizations_out.append(org_rec)
        all_envs_out.extend(org_envs)
        all_deps_out.extend(org_deps)

    organizations_out.sort(key=lambda o: (-o["totalPdus"], -o["deployedProxiesCount"], o["organization"].lower()))
    total_pdus = sum(o["totalPdus"] for o in organizations_out)
    for o in organizations_out:
        o["portfolioSharePct"] = round((o["totalPdus"] / total_pdus) * 100, 1) if total_pdus > 0 else 0.0

    return organizations_out, all_envs_out, all_deps_out


def _build_recalculated_portfolio_payload(
    raw_data: dict[str, Any],
    cfg: RecalculateConfig,
    contract_start: str | None,
    summary: dict[str, Any],
    org_results: tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]],
) -> dict[str, Any]:
    """Assemble final recalculated portfolio dictionary."""
    organizations_out, all_envs_out, all_deps_out = org_results
    return {
        "collectedAt": raw_data.get("collectedAt"),
        "source": raw_data.get("source", "DEMO_DATASET"),
        "authSource": raw_data.get("authSource", "Simulation Dataset (X & Hybrid)"),
        "account": raw_data.get("account"),
        "apiEndpoint": raw_data.get("apiEndpoint"),
        "totalAuthorizedOrgs": raw_data.get("totalAuthorizedOrgs", len(organizations_out)),
        "settings": {
            "entitlementPdu": cfg.entitlement_pdu,
            "entitlementEnvs": cfg.entitlement_envs,
            "entitlementYearlyCalls": cfg.entitlement_yearly_calls,
            "contractStartDate": contract_start,
            "includeSharedFlows": cfg.include_shared_flows,
            "regionOverrides": cfg.region_overrides,
        },
        "summary": summary,
        "organizations": organizations_out,
        "environments": all_envs_out,
        "deployments": all_deps_out,
    }


def recalculate_portfolio(
    raw_data: dict[str, Any],
    entitlement_pdu: int = 1500,
    include_shared_flows: bool = True,
    region_overrides: dict[str, int] | None = None,
    entitlement_envs: int = 20,
    **extra_settings: Any,
) -> dict[str, Any]:
    """Recalculate environment, organization, and cross-organization entitlement metrics."""
    ent_calls = int(extra_settings.get("entitlement_yearly_calls", 100_000_000))
    contract_start = extra_settings.get("contract_start_date")

    cfg = RecalculateConfig(
        entitlement_pdu=max(1, int(entitlement_pdu)),
        entitlement_envs=max(1, int(entitlement_envs)),
        entitlement_yearly_calls=max(1, ent_calls),
        include_shared_flows=include_shared_flows,
        region_overrides=region_overrides or {},
    )
    period_info = resolve_contract_period(
        contract_start or (raw_data.get("contractPeriod") or {}).get("contractStartDate")
    )
    org_results = _recalculate_all_organizations(raw_data.get("organizations", []), cfg)
    organizations_out, all_envs_out, _ = org_results

    portfolio_totals = {
        "totalPdus": sum(o["totalPdus"] for o in organizations_out),
        "totalEnvironmentUnits": sum(o["environmentUnits"] for o in organizations_out),
        "yearlyApiCalls": sum(o["yearlyApiCalls"] for o in organizations_out),
    }
    quota_summary = _compute_quota_summary(
        portfolio_totals, period_info, cfg.entitlement_pdu, cfg.entitlement_envs, cfg.entitlement_yearly_calls
    )
    summary = _build_portfolio_summary(organizations_out, all_envs_out, portfolio_totals, quota_summary)
    return _build_recalculated_portfolio_payload(
        raw_data, cfg, period_info["contractStartDate"], summary, org_results
    )


_DEMO_SPECS: tuple[dict[str, Any], ...] = (
    {
        "organization": "acme-global-prod-x",
        "projectId": "gcp-acme-apigee-prod-01",
        "runtimeType": "CLOUD",
        "billingType": "SUBSCRIPTION",
        "subscriptionPlan": "Subscription 2024 (Enterprise Plus)",
        "totalProxiesCreated": 94,
        "totalSharedFlowsCreated": 14,
        "envs": [
            {
                "name": "prod-eu",
                "envType": "COMPREHENSIVE",
                "regions": ["europe-west1", "europe-west9", "europe-west3"],
                "std": [
                    "customer-profile-v2", "order-tracking-v1", "catalog-search-v3",
                    "inventory-lookup-v2", "store-locator-v1", "loyalty-points-v2",
                    "shipping-rates-v1", "currency-fx-v1", "notifications-push-v2",
                    "invoice-download-v1", "cart-session-v3", "product-reviews-v1",
                    "wishlist-sync-v1", "promo-codes-v2", "partner-webhook-v1",
                    "address-validation-v1", "tax-calculator-v2", "giftcard-balance-v1",
                ],
                "ext": [
                    "payments-checkout-v3", "oauth2-token-issuer-v2", "openbanking-aisp-v2",
                    "openbanking-pisp-v2", "fraud-scoring-callout-v1", "b2b-mtls-gateway-v2",
                    "graphql-federation-v1", "pci-tokenization-v2", "sap-erp-connector-v1",
                    "salesforce-crm-sync-v2",
                ],
                "sf": [
                    "sf-common-security-headers", "sf-jwt-validation", "sf-cloud-logging-splunk",
                    "sf-fault-handling-standard", "sf-spike-arrest-tiered", "sf-cors-policy",
                ],
            },
            {
                "name": "prod-us",
                "envType": "COMPREHENSIVE",
                "regions": ["us-central1", "us-east1"],
                "std": [
                    "customer-profile-v2", "order-tracking-v1", "catalog-search-v3",
                    "inventory-lookup-v2", "store-locator-v1", "loyalty-points-v2",
                    "shipping-rates-v1", "cart-session-v3", "product-reviews-v1",
                    "promo-codes-v2", "address-validation-v1", "tax-calculator-v2",
                ],
                "ext": [
                    "payments-checkout-v3", "oauth2-token-issuer-v2", "fraud-scoring-callout-v1",
                    "b2b-mtls-gateway-v2", "pci-tokenization-v2", "salesforce-crm-sync-v2",
                ],
                "sf": [
                    "sf-common-security-headers", "sf-jwt-validation", "sf-cloud-logging-splunk",
                    "sf-fault-handling-standard", "sf-spike-arrest-tiered",
                ],
            },
            {
                "name": "staging-global",
                "envType": "COMPREHENSIVE",
                "regions": ["europe-west1", "us-central1"],
                "std": [
                    "customer-profile-v2", "order-tracking-v1", "catalog-search-v3",
                    "inventory-lookup-v2", "loyalty-points-v2", "cart-session-v3",
                    "promo-codes-v2", "ai-shopping-assistant-v1",
                ],
                "ext": [
                    "payments-checkout-v3", "oauth2-token-issuer-v2", "openbanking-aisp-v2",
                    "sap-erp-connector-v1", "vertex-ai-agent-gateway-v1",
                ],
                "sf": [
                    "sf-common-security-headers", "sf-jwt-validation", "sf-cloud-logging-splunk",
                    "sf-fault-handling-standard",
                ],
            },
        ],
    },
    {
        "organization": "acme-retail-nonprod-x",
        "projectId": "gcp-acme-apigee-nonprod-02",
        "runtimeType": "CLOUD",
        "billingType": "SUBSCRIPTION",
        "subscriptionPlan": "Subscription 2024 (Enterprise Plus)",
        "totalProxiesCreated": 68,
        "totalSharedFlowsCreated": 10,
        "envs": [
            {
                "name": "dev",
                "envType": "BASE",
                "regions": ["europe-west1"],
                "std": [
                    "customer-profile-v2", "customer-profile-v3-beta", "order-tracking-v1",
                    "catalog-search-v3", "inventory-lookup-v2", "store-locator-v1",
                    "loyalty-points-v2", "shipping-rates-v1", "currency-fx-v1",
                    "cart-session-v3", "product-reviews-v1", "promo-codes-v2",
                    "sandbox-mock-api-v1", "experimental-rag-search-v1",
                ],
                "ext": [
                    "payments-checkout-v3", "oauth2-token-issuer-v2", "openbanking-aisp-v2",
                    "fraud-scoring-callout-v1", "graphql-federation-v1", "vertex-ai-agent-gateway-v1",
                ],
                "sf": [
                    "sf-common-security-headers", "sf-jwt-validation", "sf-cloud-logging-splunk",
                    "sf-fault-handling-standard", "sf-cors-policy",
                ],
            },
            {
                "name": "uat",
                "envType": "INTERMEDIATE",
                "regions": ["europe-west1", "europe-west9"],
                "std": [
                    "customer-profile-v2", "order-tracking-v1", "catalog-search-v3",
                    "inventory-lookup-v2", "loyalty-points-v2", "cart-session-v3",
                    "shipping-rates-v1", "promo-codes-v2", "tax-calculator-v2",
                    "invoice-download-v1",
                ],
                "ext": [
                    "payments-checkout-v3", "oauth2-token-issuer-v2", "openbanking-aisp-v2",
                    "b2b-mtls-gateway-v2", "pci-tokenization-v2",
                ],
                "sf": [
                    "sf-common-security-headers", "sf-jwt-validation", "sf-cloud-logging-splunk",
                    "sf-fault-handling-standard",
                ],
            },
        ],
    },
    {
        "organization": "acme-banking-emea-hybrid",
        "projectId": "gcp-acme-hybrid-emea-03",
        "runtimeType": "HYBRID",
        "billingType": "SUBSCRIPTION",
        "subscriptionPlan": "Subscription 2024 (Hybrid Multi-DC)",
        "totalProxiesCreated": 52,
        "totalSharedFlowsCreated": 9,
        "envs": [
            {
                "name": "hybrid-prod-dc",
                "envType": "COMPREHENSIVE",
                "regions": ["onprem-paris-dc1", "onprem-frankfurt-dc2"],
                "std": [
                    "core-ledger-inquiry-v1", "iban-validation-v2", "sepa-instant-transfer-v1",
                    "swift-mt103-gateway-v1", "account-statements-pdf-v2", "branch-atm-locator-v1",
                    "fx-spot-rates-v1", "standing-orders-v1", "direct-debit-mandates-v2",
                    "card-controls-lock-v1",
                ],
                "ext": [
                    "psd2-aisp-consents-v3", "psd2-pisp-payments-v3", "psd2-caf-funds-v2",
                    "hsm-pkcs11-signer-v1", "mainframe-cics-bridge-v2", "kyc-aml-screening-v1",
                    "strong-customer-auth-v2", "corporate-treasury-ebics-v1",
                ],
                "sf": [
                    "sf-mtls-cert-thumbprint", "sf-psd2-qseal-verify", "sf-siem-syslog-forwarder",
                    "sf-fault-handling-banking", "sf-iso20022-schema-guard",
                ],
            },
            {
                "name": "hybrid-stage-dc",
                "envType": "COMPREHENSIVE",
                "regions": ["onprem-paris-dc1"],
                "std": [
                    "core-ledger-inquiry-v1", "iban-validation-v2", "sepa-instant-transfer-v1",
                    "account-statements-pdf-v2", "fx-spot-rates-v1", "card-controls-lock-v1",
                    "standing-orders-v1",
                ],
                "ext": [
                    "psd2-aisp-consents-v3", "psd2-pisp-payments-v3", "mainframe-cics-bridge-v2",
                    "kyc-aml-screening-v1", "strong-customer-auth-v2",
                ],
                "sf": [
                    "sf-mtls-cert-thumbprint", "sf-psd2-qseal-verify", "sf-siem-syslog-forwarder",
                    "sf-fault-handling-banking",
                ],
            },
        ],
    },
    {
        "organization": "acme-manufacturing-edge-hybrid",
        "projectId": "gcp-acme-hybrid-iot-04",
        "runtimeType": "HYBRID",
        "billingType": "SUBSCRIPTION",
        "subscriptionPlan": "Subscription 2024 (Hybrid Anthos/GKE)",
        "totalProxiesCreated": 31,
        "totalSharedFlowsCreated": 6,
        "envs": [
            {
                "name": "factory-edge-prod",
                "envType": "COMPREHENSIVE",
                "regions": ["anthos-munich-plant1", "anthos-lyon-plant2"],
                "std": [
                    "scada-telemetry-ingest-v2", "plc-command-dispatch-v1", "predictive-maint-alerts-v1",
                    "warehouse-agv-routing-v1", "supply-chain-asn-v2", "quality-vision-inspect-v1",
                    "energy-grid-meter-v1", "worker-safety-beacon-v1",
                ],
                "ext": [
                    "opc-ua-protocol-adapter-v1", "mqtt-http-bridge-v2", "sap-mes-production-order-v1",
                    "digital-twin-sync-v1",
                ],
                "sf": [
                    "sf-device-apikey-verify", "sf-edge-rate-limiter", "sf-otel-trace-context",
                ],
            },
            {
                "name": "factory-edge-dev",
                "envType": "BASE",
                "regions": ["anthos-munich-plant1"],
                "std": [
                    "scada-telemetry-ingest-v2", "plc-command-dispatch-v1", "warehouse-agv-routing-v1",
                    "simulator-sensor-stream-v1", "quality-vision-inspect-v1",
                ],
                "ext": [
                    "opc-ua-protocol-adapter-v1", "mqtt-http-bridge-v2", "digital-twin-sync-v1",
                ],
                "sf": [
                    "sf-device-apikey-verify", "sf-edge-rate-limiter",
                ],
            },
        ],
    },
)


def _build_proxy_demo_deps(
    names: list[str],
    dep_type: str,
    e_spec: dict[str, Any],
    org_name: str,
    proj_id: str,
    r_type: str,
) -> list[dict[str, Any]]:
    """Generate simulated proxy deployment records."""
    e_name = e_spec["name"]
    regs = e_spec["regions"]
    r_cnt = len(regs)
    is_ext = dep_type == "EXTENSIBLE"
    sa_suffix = f"sa-apigee-{e_name}@{proj_id}.iam.gserviceaccount.com" if is_ext else ""
    return [
        {
            "organization": org_name,
            "projectId": proj_id,
            "runtimeType": r_type,
            "environment": e_name,
            "artifactKind": "API_PROXY",
            "name": p_name,
            "revision": str((idx % (7 if is_ext else 5)) + (2 if is_ext else 1)),
            "proxyDeploymentType": dep_type,
            "regionCount": r_cnt,
            "regions": regs,
            "pdu": r_cnt,
            "deployStartTime": "1711850000000" if is_ext else "1711800000000",
            "serviceAccount": sa_suffix,
        }
        for idx, p_name in enumerate(names)
    ]


def _build_sf_demo_deps(
    names: list[str],
    e_spec: dict[str, Any],
    org_name: str,
    proj_id: str,
    r_type: str,
) -> list[dict[str, Any]]:
    """Generate simulated shared flow deployment records."""
    e_name = e_spec["name"]
    regs = e_spec["regions"]
    r_cnt = len(regs)
    return [
        {
            "organization": org_name,
            "projectId": proj_id,
            "runtimeType": r_type,
            "environment": e_name,
            "artifactKind": "SHARED_FLOW",
            "name": sf_name,
            "revision": str((idx % 3) + 1),
            "proxyDeploymentType": "SHARED_FLOW",
            "regionCount": r_cnt,
            "regions": regs,
            "pdu": r_cnt,
            "deployStartTime": "1711700000000",
            "serviceAccount": "",
        }
        for idx, sf_name in enumerate(names)
    ]


def _build_demo_deployments(
    e_spec: dict[str, Any],
    org_name: str,
    proj_id: str,
    r_type: str,
) -> list[dict[str, Any]]:
    """Build simulated deployments for a demo environment."""
    deps: list[dict[str, Any]] = []
    deps.extend(_build_proxy_demo_deps(e_spec["std"], "STANDARD", e_spec, org_name, proj_id, r_type))
    deps.extend(_build_proxy_demo_deps(e_spec["ext"], "EXTENSIBLE", e_spec, org_name, proj_id, r_type))
    deps.extend(_build_sf_demo_deps(e_spec["sf"], e_spec, org_name, proj_id, r_type))
    return deps


def _build_demo_env(
    e_spec: dict[str, Any],
    org_name: str,
    proj_id: str,
    r_type: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Build simulated environment summary and its associated deployments."""
    deps = _build_demo_deployments(e_spec, org_name, proj_id, r_type)
    e_name = e_spec["name"]
    regs = e_spec["regions"]
    r_cnt = len(regs)
    std_c = len(e_spec["std"])
    ext_c = len(e_spec["ext"])
    sf_c = len(e_spec["sf"])
    tot_p = std_c + ext_c
    tot_art = tot_p + sf_c

    env_record = {
        "organization": org_name,
        "runtimeType": r_type,
        "environment": e_name,
        "envType": e_spec["envType"],
        "regions": regs,
        "regionCount": r_cnt,
        "environmentUnits": r_cnt,
        "standardProxies": std_c,
        "extensibleProxies": ext_c,
        "totalProxies": tot_p,
        "sharedFlows": sf_c,
        "totalArtifacts": tot_art,
        "standardProxyPdus": std_c * r_cnt,
        "extensibleProxyPdus": ext_c * r_cnt,
        "proxyOnlyPdus": tot_p * r_cnt,
        "sharedFlowPdus": sf_c * r_cnt,
        "totalPdus": tot_art * r_cnt,
        "demoAnnualCallsBase": (tot_art * r_cnt) * 350_000,
    }
    return env_record, deps


def _build_demo_org(org_spec: dict[str, Any]) -> dict[str, Any]:
    """Build a complete simulated organization record from spec definition."""
    org_name = org_spec["organization"]
    proj_id = org_spec["projectId"]
    r_type = org_spec["runtimeType"]
    envs_list: list[dict[str, Any]] = []
    deps_list: list[dict[str, Any]] = []

    for e_spec in org_spec["envs"]:
        env_rec, env_deps = _build_demo_env(e_spec, org_name, proj_id, r_type)
        envs_list.append(env_rec)
        deps_list.extend(env_deps)

    return {
        "organization": org_name,
        "projectId": proj_id,
        "status": "OK",
        "error": None,
        "runtimeType": r_type,
        "billingType": org_spec["billingType"],
        "subscriptionPlan": org_spec["subscriptionPlan"],
        "state": "ACTIVE",
        "totalProxiesCreated": org_spec["totalProxiesCreated"],
        "totalSharedFlowsCreated": org_spec["totalSharedFlowsCreated"],
        "instances": [],
        "environments": envs_list,
        "deployments": deps_list,
    }


def get_demo_dataset() -> dict[str, Any]:
    """Generates a realistic multi-organization Apigee X + Apigee Hybrid dataset."""
    organizations = [_build_demo_org(spec) for spec in _DEMO_SPECS]
    demo_payload: dict[str, Any] = {
        "collectedAt": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": "DEMO_DATASET",
        "authSource": "Sample Multi-Org Dataset (2 Apigee X + 2 Apigee Hybrid)",
        "organizations": organizations,
    }
    populate_contract_api_calls_sync(demo_payload)
    return demo_payload
