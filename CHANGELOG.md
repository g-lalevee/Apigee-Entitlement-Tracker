# Changelog

All notable changes to **Apigee Entitlement Tracker** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-11-26

### Fixed
- **Historical API Calls Accuracy (Pre-October / Multi-Month Retention):**
  - Removed Cloud Monitoring (`apigee.googleapis.com/proxy/response_count`, 6-week retention) fallback for API calls; all API call counts are now queried strictly from **Apigee Analytics** (14-month retention).
  - Isolated 90-day Analytics chunks with retries and non-overlapping `00:00`–`23:59` boundaries so empty early quarters, transient timeouts, or currently undeployed environments never wipe out or double-count earlier contract months.
  - Preserved organization PDU & Environment Unit inventories on partial Analytics errors (`PARTIAL` / `ERROR` status) instead of dropping organizations, and explicitly surfaced environments where the Apigee Analytics add-on is disabled (`NO ANALYTICS` / `ANALYTICS_DISABLED`).
- **OAuth Token Refresh & Multi-Session Stability:**
  - Fixed `/api/auth-status` refresh synchronization and Cloud Run cold-start refresh so OAuth sessions renew cleanly after 1 hour without lockout.
  - Added mid-scan `HTTP 401` session refresh and prevented failed live scans from silently opening the dashboard with synthetic demo or empty `0 PDU` data.
  - Eliminated duplicate 90-day Analytics scans when editing the Contract Starting Date and added sequence guards to prevent stale chart overwrites.
- **Excluded Organization Metadata & Cloud Run Deployment:**
  - Preserved rich `[PAYG]` and `[TRIAL]` metadata in `excludedBillingDetails` across cache reloads and partial merges while retaining both `SUBSCRIPTION_2021` and `SUBSCRIPTION_2024` organizations.
  - Paginated `/deployments` queries, deduplicated aligned Cloud Monitoring buckets, and fixed initial Cloud Run service creation in `scripts/setup_oauth_cloudrun.sh`.

### Added
- **Login Preloading:** Pre-fetches both Monthly API Calls and Cloud Monitoring PDU time-series during the login scan progress stream so the dashboard opens with charts ready immediately.
- **Excluded Organizations Dialog & Dismissible Warnings:** Added an upper-right excluded-organizations warning button opening a native modal dialog (`<dialog>`) and a close button on the `No Analytics` alert banner.
- **Light Theme & Accessibility Improvements:** Added WCAG-compliant Light Theme status contrast tokens, `Escape`-key modal dismissal, responsive narrow-viewport header wrapping, and full keyboard (`Enter`/`Space`) + ARIA state support on overview dimension cards.
- **Open-Source License Headers:** Added standard Apache 2.0 (`SPDX-License-Identifier: Apache-2.0`) headers across source files and deployment scripts.

### Changed
- **Streamlined Login & Header UX:** Removed the personal OAuth token paste shortcut from the login screen, gated the `Authenticate` / `Enter Dashboard` buttons until token status verification finishes, anchored utility actions to the top-right header corner, and simplified the user identity header.
- **Jurisdiction Switching:** Preserved the active session and theme when returning to the account/jurisdiction screen instead of forcing a full OAuth logout.
- **Cloud Run Cache & Memory Efficiency:** Added compact JSON serialization, bounded TTL/LRU `tmpfs` eviction, and `/tmp` fallback on read-only container filesystems.

### Security
- **Session Fixation & Auth Hardening:** Enforced session ID rotation on OAuth callback, token registration, and login boundaries; made `GET /api/auth-status` strictly read-only (never minting session cookies); restricted local workstation ADC auto-sessions to loopback `POST` logins.
- **CSRF & Multi-Tenant Host Protection:** Blocked multi-tenant wildcard suffixes (`*.run.app`, `*.appspot.com`, etc.) in `X-Forwarded-Host` validation and prevented unauthenticated `POST /api/jurisdiction` requests from mutating server-side config or purging cached discovery files.
- **Strict Content Security Policy (CSP):** Removed `'unsafe-inline'` from default `script-src` (`script-src 'self'`) and applied a per-response cryptographic `'nonce-...'` strictly to the OIDC callback response.

## [1.0.0] - 2026-11-16

### Added
- Initial public release of **Apigee Entitlement Tracker** for **Apigee X** and **Apigee Hybrid**.
- Cross-organization PDU, Environment Unit, and Yearly API Call tracking (including Standard `0.2×`, Extensible `1.0×`, and free Envoy Adapter `0×` Entitlement v2024 weighting).
- Support for Apigee Data Residency Zones (DRZ) and regional control plane jurisdictions.
- Cloud Run OAuth 2.0 Web Client delegation, headless CLI mode, and 16-column CSV audit exports.
