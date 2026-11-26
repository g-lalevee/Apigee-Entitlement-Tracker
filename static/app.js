/**
 * Copyright 2026 The Apigee Entitlement Tracker Authors
 * SPDX-License-Identifier: Apache-2.0
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Apigee Entitlement Tracker — Frontend Dashboard Controller
 */

let currentPortfolio = null;
let currentMonitoringTs = null;
let currentCallsTs = null;
let selectedMonitoringDays = 14;
let selectedGaugeDimension = 'pdu'; // 'pdu' | 'environments' | 'calls'
let selectedChartDimension = 'pdu'; // 'pdu' | 'environments'

function isLightTheme() {
  return document.body?.getAttribute('data-theme') === 'light';
}

function applyTheme(theme) {
  const mode = theme === 'light' ? 'light' : 'dark';
  const schemeMeta = document.querySelector('meta[name="color-scheme"]');
  if (mode === 'light') {
    document.documentElement.setAttribute('data-theme', 'light');
    document.body.setAttribute('data-theme', 'light');
    if (schemeMeta) schemeMeta.setAttribute('content', 'light');
  } else {
    document.documentElement.removeAttribute('data-theme');
    document.body.removeAttribute('data-theme');
    if (schemeMeta) schemeMeta.setAttribute('content', 'dark');
  }
  try {
    localStorage.setItem('apigee_pdu_theme', mode);
  } catch (_) {}

  document.querySelectorAll('.theme-toggle-btn').forEach((btn) => {
    const sunIcon = btn.querySelector('.icon-sun');
    const moonIcon = btn.querySelector('.icon-moon');
    const labelEl = btn.querySelector('.theme-toggle-text');
    if (mode === 'light') {
      if (sunIcon) sunIcon.style.display = 'none';
      if (moonIcon) moonIcon.style.display = 'inline-block';
      if (labelEl) labelEl.textContent = 'Dark Mode';
      btn.setAttribute('title', 'Switch to Dark Mode');
      btn.setAttribute('aria-label', 'Switch to Dark Mode');
    } else {
      if (sunIcon) sunIcon.style.display = 'inline-block';
      if (moonIcon) moonIcon.style.display = 'none';
      if (labelEl) labelEl.textContent = 'Light Mode';
      btn.setAttribute('title', 'Switch to Light Mode');
      btn.setAttribute('aria-label', 'Switch to Light Mode');
    }
  });

  if (currentPortfolio) {
    renderSimplifiedDashboard();
  }
}

// Multi-selection state for Organizations dropdown
let allOrgsSelected = true;
const selectedOrgs = new Set();

function isOrgSelected(orgName) {
  if (allOrgsSelected) return true;
  return selectedOrgs.has(orgName);
}

function getSelectedOrgsLabel(totalOrgsCount) {
  if (allOrgsSelected || (totalOrgsCount > 0 && selectedOrgs.size === totalOrgsCount)) {
    return `All Organizations (${totalOrgsCount || 0})`;
  }
  if (selectedOrgs.size === 0) {
    return '0 Organizations Selected';
  }
  const arr = Array.from(selectedOrgs).sort((a, b) =>
    String(a || '').localeCompare(String(b || ''), undefined, { sensitivity: 'base', numeric: true })
  );
  if (arr.length === 1) {
    return arr[0];
  }
  if (arr.length === 2) {
    return `${arr[0]}, ${arr[1]}`;
  }
  return `${arr.length} of ${totalOrgsCount} Orgs Selected`;
}

function formatCompactCalls(num) {
  const n = Number(num || 0);
  if (n >= 1_000_000_000) {
    return `${(n / 1_000_000_000).toFixed(n % 1_000_000_000 === 0 ? 0 : 2)}B`;
  }
  if (n >= 1_000_000) {
    return `${(n / 1_000_000).toFixed(n % 1_000_000 === 0 ? 0 : 1)}M`;
  }
  if (n >= 10_000) {
    return `${(n / 1_000).toFixed(1)}K`;
  }
  return n.toLocaleString();
}

function showToast(message) {
  const toast = document.getElementById('toast');
  if (!toast) return;
  toast.textContent = message;
  toast.classList.add('show');
  clearTimeout(window.__toastTimer);
  window.__toastTimer = setTimeout(() => {
    toast.classList.remove('show');
  }, 3000);
}

function escapeHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatFootprint(runtimeType) {
  if (runtimeType === 'HYBRID') {
    return '<span class="badge badge-hybrid">HYBRID</span>';
  }
  if (runtimeType === 'UNKNOWN') {
    return '<span class="calls-status-badge error" title="Organization metadata query failed">UNKNOWN</span>';
  }
  return '<span class="badge badge-x">APIGEE X</span>';
}

let inMemoryUserToken = '';

function purgeLegacyBrowserTokens() {
  try { sessionStorage.removeItem('apigee_user_access_token'); } catch (_) {}
  try { localStorage.removeItem('apigee_user_access_token'); } catch (_) {}
}

function getStoredUserToken() {
  return inMemoryUserToken;
}

function setStoredUserToken(token) {
  inMemoryUserToken = token && token.trim() ? token.trim() : '';
  purgeLegacyBrowserTokens();
}

async function apiFetch(url, options = {}) {
  const token = getStoredUserToken();
  const opts = { credentials: 'same-origin', ...options };
  opts.headers = { ...(options.headers || {}) };
  if (token && !opts.headers['Authorization']) {
    opts.headers['Authorization'] = `Bearer ${token}`;
  }
  return fetch(url, opts);
}

let activeSessionEmail = '';
let activeHasUserToken = false;
const dismissedAnalyticsWarnSet = new Set();
let currentAnalyticsWarnKeys = [];
const attemptedMismatchKeys = new Set();
let monitoringFetchSeq = 0;
let callsFetchSeq = 0;

function updateTokenStatusPill(hasToken, label = null) {
  const pill = document.getElementById('user-token-pill');

  activeHasUserToken = Boolean(hasToken);

  if (pill) {
    if (hasToken) {
      pill.textContent = label || 'User Token Active';
      pill.className = 'token-pill token-pill-active';
    } else {
      pill.textContent = label || 'Service Account Mode';
      pill.className = 'token-pill token-pill-required';
    }
  }
}

async function applyUserToken(token, feedbackEl = null) {
  if (!token || !token.trim()) {
    if (feedbackEl) {
      feedbackEl.textContent = 'Please provide an access token.';
      feedbackEl.className = 'token-feedback-msg token-feedback-error';
      feedbackEl.style.display = 'block';
    }
    return false;
  }

  let cleanTok = token.trim();
  if (cleanTok.startsWith('Bearer ')) {
    cleanTok = cleanTok.replace(/^Bearer\s+/i, '').trim();
  }

  if (feedbackEl) {
    feedbackEl.textContent = 'Validating token with Google APIs...';
    feedbackEl.className = 'token-feedback-msg';
    feedbackEl.style.display = 'block';
  }

  try {
    const res = await fetch('/api/auth/oidc/token', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${cleanTok}`,
      },
      body: JSON.stringify({
        accessToken: cleanTok,
        email: activeSessionEmail || undefined,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Token validation failed (HTTP ${res.status})`);
    }
    const info = await res.json();
    setStoredUserToken(cleanTok);
    const resolvedAcct = info.activeAccount || activeSessionEmail || '';
    if (resolvedAcct) {
      activeSessionEmail = resolvedAcct;
    }
    const enterBtn = document.getElementById('btn-enter-app');
    if (enterBtn) {
      enterBtn.disabled = false;
      enterBtn.classList.remove('btn-frozen');
      enterBtn.title = 'Enter Dashboard';
    }
    const enterBtnText = document.getElementById('btn-enter-app-text');
    if (enterBtnText) {
      const idLabel = resolvedAcct ? ` (${escapeHtml(resolvedAcct)})` : '';
      enterBtnText.innerHTML = `Enter Dashboard${idLabel} &rarr;`;
    }
    const authBtn = document.getElementById('btn-authenticate-account');
    if (authBtn) {
      authBtn.classList.remove('btn-mandatory');
      authBtn.hidden = false;
    }
    const authBtnText = document.getElementById('btn-authenticate-text');
    if (authBtnText) {
      authBtnText.textContent = 'Authenticate / Change Account';
    }
    const loginErrBox = document.getElementById('login-error-box');
    if (loginErrBox) {
      loginErrBox.style.display = 'none';
    }
    const checkingEl = document.getElementById('login-auth-checking');
    if (checkingEl) checkingEl.hidden = true;
    const actionsGroup = document.getElementById('login-actions-group');
    if (actionsGroup) actionsGroup.hidden = false;
    updateTokenStatusPill(true);
    if (feedbackEl) {
      feedbackEl.textContent = `Token valid for ${info.activeAccount || 'user'}!`;
      feedbackEl.className = 'token-feedback-msg token-feedback-success';
      feedbackEl.style.display = 'block';
    }
    showToast('User OAuth Token verified and active');
    // Hide SA banner if visible
    const saBanner = document.getElementById('dashboard-sa-notice');
    if (saBanner) saBanner.style.display = 'none';
    return true;
  } catch (err) {
    setStoredUserToken('');
    updateTokenStatusPill(false);
    if (feedbackEl) {
      feedbackEl.textContent = err.message;
      feedbackEl.className = 'token-feedback-msg token-feedback-error';
      feedbackEl.style.display = 'block';
    }
    return false;
  }
}

const JURISDICTION_ENDPOINTS = {
  global: { name: 'Global', endpoint: 'https://apigee.googleapis.com/v1' },
  us: { name: 'United States (us)', endpoint: 'https://apigee.us.rep.googleapis.com/v1' },
  ca: { name: 'Canada (ca)', endpoint: 'https://ca-apigee.googleapis.com/v1' },
  eu: { name: 'European Union (eu)', endpoint: 'https://apigee.eu.rep.googleapis.com/v1' },
  de: { name: 'Germany (de)', endpoint: 'https://de-apigee.googleapis.com/v1' },
  fr: { name: 'France (fr, europe-west9)', endpoint: 'https://fr-apigee.googleapis.com/v1' },
  ch: { name: 'Switzerland (ch, europe-west6)', endpoint: 'https://ch-apigee.googleapis.com/v1' },
  au: { name: 'Australia (au)', endpoint: 'https://au-apigee.googleapis.com/v1' },
  in: { name: 'India (in)', endpoint: 'https://apigee.in.rep.googleapis.com/v1' },
  jp: { name: 'Japan (jp)', endpoint: 'https://jp-apigee.googleapis.com/v1' },
  qa: { name: 'Qatar (qa, me-central1)', endpoint: 'https://qa-apigee.googleapis.com/v1' },
  sa: { name: 'Saudi Arabia (sa, me-central2)', endpoint: 'https://sa-apigee.googleapis.com/v1' },
  il: { name: 'Israel (il, me-west1)', endpoint: 'https://il-apigee.googleapis.com/v1' },
};

function getStoredJurisdiction() {
  return localStorage.getItem('apigee_control_plane_jurisdiction') || 'global';
}

function setStoredJurisdiction(val) {
  localStorage.setItem('apigee_control_plane_jurisdiction', val || 'global');
}

function updateJurisdictionHint(jurCode) {
  const descEl = document.getElementById('jurisdiction-endpoint-desc');
  const safeCode = String(jurCode || 'global').replace(/[^a-z0-9-]/gi, '').toLowerCase() || 'global';
  const info = JURISDICTION_ENDPOINTS[safeCode] || { endpoint: `https://${safeCode}-apigee.googleapis.com/v1` };
  if (descEl) {
    const codeEl = document.createElement('code');
    codeEl.textContent = info.endpoint;
    descEl.replaceChildren('Endpoint: ', codeEl);
  }
}

function showScanProgressModal(title = 'Discovering Apigee organizations...', detail = 'Querying Google Cloud Apigee API...', percent = 0) {
  const modal = document.getElementById('modal-scan-progress');
  const statusEl = document.getElementById('scan-modal-status');
  const detailEl = document.getElementById('scan-modal-detail');
  const percentEl = document.getElementById('scan-modal-percent');
  const fillEl = document.getElementById('scan-modal-progress-fill');

  if (statusEl) statusEl.textContent = title;
  if (detailEl) detailEl.textContent = detail;
  const pct = Math.max(0, Math.min(100, Math.round(percent || 0)));
  if (percentEl) percentEl.textContent = `${pct}%`;
  if (fillEl) fillEl.style.width = `${pct}%`;

  if (modal) modal.style.display = 'grid';
}

function updateScanProgressModal(evt) {
  const modal = document.getElementById('modal-scan-progress');
  const statusEl = document.getElementById('scan-modal-status');
  const detailEl = document.getElementById('scan-modal-detail');
  const percentEl = document.getElementById('scan-modal-percent');
  const fillEl = document.getElementById('scan-modal-progress-fill');

  if (modal && modal.style.display === 'none') {
    modal.style.display = 'grid';
  }

  const pct = Math.max(0, Math.min(100, Math.round(evt.percent || 0)));
  if (statusEl && evt.message) statusEl.textContent = evt.message;
  const subText = evt.organization || evt.detail;
  if (detailEl && subText !== undefined) detailEl.textContent = subText;
  if (percentEl) percentEl.textContent = `${pct}%`;
  if (fillEl) fillEl.style.width = `${pct}%`;
}

function hideScanProgressModal() {
  const modal = document.getElementById('modal-scan-progress');
  if (modal) {
    modal.style.display = 'none';
  }
}

let pendingTrafficRetryHandler = null;

function showTrafficErrorModal(errors, retryHandler = null) {
  const modal = document.getElementById('modal-traffic-error');
  const msgEl = document.getElementById('traffic-error-modal-message');
  const listEl = document.getElementById('traffic-error-modal-list');
  if (!modal || !listEl) return;

  const items = Array.isArray(errors) ? errors.filter(Boolean) : [String(errors || 'Unknown error')];
  if (items.length === 0) return;

  pendingTrafficRetryHandler = retryHandler;
  if (msgEl) {
    msgEl.textContent =
      items.length === 1
        ? 'An Apigee Analytics query failed. Organization PDUs and Environment Units were retained; affected API call counts are marked PARTIAL / ERROR:'
        : `${items.length} Apigee Analytics queries failed. Organization PDUs and Environment Units were retained; affected API call counts are marked PARTIAL / ERROR:`;
  }
  listEl.replaceChildren();
  items.slice(0, 20).forEach((entry) => {
    const li = document.createElement('li');
    li.textContent = String(entry);
    listEl.appendChild(li);
  });
  if (items.length > 20) {
    const moreLi = document.createElement('li');
    moreLi.textContent = `...and ${items.length - 20} more`;
    listEl.appendChild(moreLi);
  }
  modal.style.display = 'grid';
}

function hideTrafficErrorModal() {
  const modal = document.getElementById('modal-traffic-error');
  if (modal) {
    modal.style.display = 'none';
  }
  pendingTrafficRetryHandler = null;
}

async function initAuthScreen() {
  const enterBtn = document.getElementById('btn-enter-app');
  const enterBtnText = document.getElementById('btn-enter-app-text');
  const authBtn = document.getElementById('btn-authenticate-account');
  const authBtnText = document.getElementById('btn-authenticate-text');
  const checkingEl = document.getElementById('login-auth-checking');
  const actionsGroup = document.getElementById('login-actions-group');
  const errBox = document.getElementById('login-error-box');
  const jurSelect = document.getElementById('select-control-plane');

  activeSessionEmail = '';
  if (checkingEl) checkingEl.hidden = false;
  if (actionsGroup) actionsGroup.hidden = true;
  if (authBtn) authBtn.hidden = true;

  // Wire jurisdiction selector
  let userPickedJurDuringCheck = false;
  const activeJur = getStoredJurisdiction();
  if (jurSelect) {
    jurSelect.value = activeJur;
    updateJurisdictionHint(activeJur);
    if (!jurSelect.dataset.listenerBound) {
      jurSelect.dataset.listenerBound = 'true';
      jurSelect.addEventListener('change', async (e) => {
        userPickedJurDuringCheck = true;
        const val = e.target.value;
        setStoredJurisdiction(val);
        updateJurisdictionHint(val);
        currentPortfolio = null;
        allOrgsSelected = true;
        selectedOrgs.clear();
        currentMonitoringTs = null;
        currentCallsTs = null;
        try {
          await apiFetch('/api/jurisdiction', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ jurisdiction: val }),
          });
        } catch (_) {}
      });
    }
  }

  // Handle URL redirect query params from Google OIDC callback
  const urlParams = new URLSearchParams(window.location.search);
  const justAuthenticated = urlParams.get('authenticated') === '1' || urlParams.get('oidc_success') === '1';
  if (justAuthenticated) {
    showToast('Successfully authenticated with Google!');
    window.history.replaceState({}, document.title, window.location.pathname);
  }
  const rawOidcErr = urlParams.get('oidc_error');
  if (rawOidcErr) {
    if (errBox) {
      errBox.textContent = `Authentication Notice: ${rawOidcErr}`;
      errBox.style.display = 'block';
    }
    window.history.replaceState({}, document.title, window.location.pathname);
  }

  try {
    const res = await apiFetch('/api/auth-status', { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const auth = await res.json();

    const accounts = auth.accounts || [];
    const email = auth.activeAccount || (accounts.length > 0 ? accounts[0].account : '') || '';
    activeSessionEmail = email;

    if (!userPickedJurDuringCheck && auth.jurisdiction && auth.jurisdiction.code && jurSelect) {
      jurSelect.value = auth.jurisdiction.code;
      setStoredJurisdiction(auth.jurisdiction.code);
      updateJurisdictionHint(auth.jurisdiction.code);
    }

    const isTokenExpired = Boolean(auth.tokenExpired);
    const isTokenValid = Boolean(auth.tokenValid && auth.hasUserToken && !isTokenExpired);
    const isIapVerified = Boolean(auth.isIap && auth.authenticated);
    const canEnter = Boolean(isTokenValid || isIapVerified);

    updateTokenStatusPill(isTokenValid, isIapVerified && !isTokenValid ? 'IAP Verified' : null);

    // Rule: If authentication is required, freeze "Enter Dashboard" button.
    // When logged in, button displays: "Enter Dashboard (google id) →"
    if (!canEnter) {
      if (enterBtn) {
        enterBtn.disabled = true;
        enterBtn.classList.add('btn-frozen');
        enterBtn.title = 'Authentication mandatory: User token is expired or missing';
      }
      if (enterBtnText) {
        enterBtnText.innerHTML = '&#128274; Enter Dashboard';
      }
      if (authBtn) {
        authBtn.classList.add('btn-mandatory');
      }
      if (authBtnText) {
        authBtnText.textContent = isTokenExpired ? 'Token Expired — Authenticate with Google' : 'Authenticate with Google';
      }
    } else {
      if (enterBtn) {
        enterBtn.disabled = false;
        enterBtn.classList.remove('btn-frozen');
        enterBtn.title = 'Enter Dashboard';
      }
      if (enterBtnText) {
        const idLabel = email ? ` (${escapeHtml(email)})` : '';
        enterBtnText.innerHTML = `Enter Dashboard${idLabel} &rarr;`;
      }
      if (authBtn) {
        authBtn.classList.remove('btn-mandatory');
      }
      if (authBtnText) {
        authBtnText.textContent = isIapVerified && !isTokenValid ? 'Switch IAP Account' : 'Authenticate / Change Account';
      }
    }
  } catch (err) {
    activeSessionEmail = '';
    updateTokenStatusPill(false);
    if (enterBtn) {
      enterBtn.disabled = true;
      enterBtn.classList.add('btn-frozen');
      enterBtn.title = 'Authentication mandatory to enter dashboard';
    }
    if (enterBtnText) {
      enterBtnText.innerHTML = '&#128274; Enter Dashboard';
    }
    if (authBtn) {
      authBtn.classList.add('btn-mandatory');
    }
    if (authBtnText) {
      authBtnText.textContent = 'Authenticate with Google';
    }
  } finally {
    if (checkingEl) checkingEl.hidden = true;
    if (actionsGroup) actionsGroup.hidden = false;
    if (authBtn) authBtn.hidden = false;
  }
}


async function startLiveScanWithProgress({
  account = null,
  jurisdiction = null,
  onProgress = null,
} = {}) {
  const token = getStoredUserToken();
  const jur = jurisdiction || getStoredJurisdiction();
  const headers = { 'Content-Type': 'application/json' };
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }
  const response = await fetch('/api/scan/stream', {
    method: 'POST',
    headers,
    credentials: 'same-origin',
    body: JSON.stringify({
      account: account || undefined,
      jurisdiction: jur,
    }),
  });
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}: Failed to connect to scan stream`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split('\n');
    buffer = lines.pop(); // Keep incomplete chunk

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed.startsWith('data:')) continue;
      const jsonStr = trimmed.slice(5).trim();
      if (!jsonStr) continue;
      let event;
      try {
        event = JSON.parse(jsonStr);
      } catch (_) {
        continue;
      }
      if (event.done) {
        if (event.stage === 'ERROR') {
          throw new Error(event.error || event.message || 'Live scan failed');
        }
        const portfolio = event.portfolio || {};
        portfolio._prefetchedPduTs = event.pduTimeseries || null;
        portfolio._prefetchedCallsTs = event.callsTimeseries || null;
        return portfolio;
      }
      if (onProgress) onProgress(event);
    }
  }
  throw new Error('Scan stream ended unexpectedly without completion payload');
}

async function openDashboardForAccount(account, runLiveScan = true) {
  const enterBtn = document.getElementById('btn-enter-app');
  if (!account && !activeSessionEmail) {
    if (enterBtn) enterBtn.disabled = false;
    return;
  }

  const errBox = document.getElementById('login-error-box');

  if (enterBtn && enterBtn.classList.contains('btn-frozen')) {
    enterBtn.disabled = true;
    return;
  }

  if (errBox) errBox.style.display = 'none';

  // Keep Login screen visible during the entire login scan & chart prefetch phase
  showScanProgressModal('Discovering Apigee organizations...', 'Querying Google Cloud Apigee API...', 5);

  let usedCachedFallbackNotice = '';
  try {
    if (runLiveScan) {
      try {
        currentPortfolio = await startLiveScanWithProgress({
          account: account || null,
          jurisdiction: getStoredJurisdiction(),
          onProgress: updateScanProgressModal,
        });
      } catch (scanErr) {
        console.warn('Live scan encountered an issue, checking for genuine cached live snapshot:', scanErr);
        const targetJur = getStoredJurisdiction();
        const res = await apiFetch(`/api/portfolio?jurisdiction=${encodeURIComponent(targetJur)}`);
        if (!res.ok) throw scanErr;
        const fallback = await res.json();
        const fallbackJur = fallback.controlPlaneJurisdiction || fallback.jurisdiction?.code || targetJur;
        if (fallback.source !== 'LIVE_APIGEE_API' || fallbackJur !== targetJur) {
          throw scanErr;
        }
        currentPortfolio = fallback;
        usedCachedFallbackNotice = `Live scan failed (${scanErr.message}); showing cached snapshot from ${fallback.collectedAt || 'previous scan'}.`;
      }
    } else {
      updateScanProgressModal({ percent: 15, message: 'Loading Apigee Entitlements...', detail: 'Connecting to APIs' });

      const targetJur = getStoredJurisdiction();
      const fetchPromise = apiFetch(`/api/portfolio?jurisdiction=${encodeURIComponent(targetJur)}`);
      updateScanProgressModal({ percent: 35, message: 'Retrieving organization portfolio...', detail: 'Fetching audit data' });

      const res = await fetchPromise;
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      currentPortfolio = await res.json();

      const portfolioJur = currentPortfolio.controlPlaneJurisdiction || (currentPortfolio.jurisdiction && currentPortfolio.jurisdiction.code);
      if (currentPortfolio.source !== 'LIVE_APIGEE_API' || portfolioJur !== targetJur) {
        updateScanProgressModal({ percent: 45, message: 'Initiating live scan for account...', detail: 'Streaming live audit' });
        currentPortfolio = await startLiveScanWithProgress({
          account: account || null,
          jurisdiction: targetJur,
          onProgress: updateScanProgressModal,
        });
      }
    }

    // Complete API Calls & PDU Monitoring chart preloading DURING the login phase (no duplicate Analytics scan)
    if (currentPortfolio?._prefetchedCallsTs) {
      currentCallsTs = currentPortfolio._prefetchedCallsTs;
    } else {
      updateScanProgressModal({
        percent: 91,
        message: 'Preparing monthly API calls...',
        detail: 'Aggregating 90-day Apigee Analytics chunks',
      });
      await fetchMonthlyCallsSeries(Boolean(currentPortfolio?.callsPeriodMismatch), { skipRender: true });
    }

    if (
      currentPortfolio?._prefetchedPduTs &&
      selectedChartDimension === 'pdu' &&
      selectedMonitoringDays === 14
    ) {
      currentMonitoringTs = currentPortfolio._prefetchedPduTs;
    } else {
      updateScanProgressModal({
        percent: 95,
        message: 'Querying Cloud Monitoring PDU history...',
        detail: 'apigee.googleapis.com/proxy/details',
      });
      await fetchMonitoringTimeSeries(selectedMonitoringDays, false, selectedChartDimension, { skipRender: true });
    }

    updateScanProgressModal({ percent: 100, message: 'Dashboard Ready!', detail: 'Launching view' });

    // Always reset org selection so organizations from previously selected regions do NOT linger
    allOrgsSelected = true;
    selectedOrgs.clear();
    dismissedAnalyticsWarnSet.clear();

    // Switch to Dashboard screen BEFORE rendering SVG charts so container.clientWidth is exact
    document.getElementById('login-screen').style.display = 'none';
    document.getElementById('dashboard-screen').style.display = 'flex';

    populateOrgDropdown(currentPortfolio);
    populateEnvDropdown(currentPortfolio);
    renderSimplifiedDashboard();

    await new Promise((resolve) => requestAnimationFrame(() => resolve()));
    hideScanProgressModal();

    if (usedCachedFallbackNotice) {
      showToast(usedCachedFallbackNotice);
    }
    if (Array.isArray(currentPortfolio?.trafficErrors) && currentPortfolio.trafficErrors.length > 0) {
      showTrafficErrorModal(currentPortfolio.trafficErrors, () => {
        document.getElementById('btn-refresh-scan')?.click();
      });
    }
  } catch (err) {
    hideScanProgressModal();
    document.getElementById('dashboard-screen').style.display = 'none';
    document.getElementById('login-screen').style.display = '';
    if (errBox) {
      errBox.textContent = `Scan Error: ${err.message}`;
      errBox.style.display = 'block';
    }
  } finally {
    if (enterBtn) {
      enterBtn.disabled = false;
    }
  }
}

/* Cloud Monitoring Time-Series Chart */

async function fetchMonitoringTimeSeries(
  days = 14,
  refresh = false,
  dimension = selectedChartDimension,
  { skipRender = false } = {}
) {
  const reqId = ++monitoringFetchSeq;
  selectedMonitoringDays = days;
  selectedChartDimension = dimension === 'environments' ? 'environments' : 'pdu';

  const container = document.getElementById('chart-container');
  const subtitle = document.getElementById('chart-subtitle');
  const mainTitle = document.getElementById('chart-main-title');

  if (!skipRender && mainTitle) {
    mainTitle.textContent =
      selectedChartDimension === 'environments'
        ? 'Environment Units Across Time (Cloud Monitoring)'
        : 'PDUs Across Time (Cloud Monitoring)';
  }

  if (
    !skipRender &&
    container &&
    (!currentMonitoringTs || refresh || currentMonitoringTs.dimension !== selectedChartDimension)
  ) {
    container.innerHTML =
      '<div class="chart-empty">Querying Cloud Monitoring (apigee.googleapis.com/proxy/details)...</div>';
  }

  try {
    const jur = getStoredJurisdiction();
    const res = await apiFetch(
      `/api/monitoring/timeseries?days=${days}&dimension=${encodeURIComponent(
        selectedChartDimension
      )}&refresh=${refresh ? 'true' : 'false'}&jurisdiction=${encodeURIComponent(jur)}`
    );

    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const payload = await res.json();
    if (reqId !== monitoringFetchSeq) return;
    currentMonitoringTs = payload;
    if (!skipRender) {
      renderMonitoringChart();
    }
  } catch (err) {
    if (reqId !== monitoringFetchSeq) return;
    if (skipRender) {
      console.warn('Monitoring prefetch warning:', err);
      if (!currentMonitoringTs) {
        currentMonitoringTs = { dimension: selectedChartDimension, days, series: [], seriesCount: 0 };
      }
      return;
    }
    if (container) {
      container.innerHTML = `<div class="chart-empty">Could not load time-series: ${escapeHtml(err.message)}</div>`;
    }
    if (subtitle) {
      subtitle.innerHTML = `Error loading time-series data`;
    }
  }
}

function renderMonitoringChart() {
  const container = document.getElementById('chart-container');
  const subtitle = document.getElementById('chart-subtitle');
  const chartPill = document.getElementById('chart-limit-pill');
  const mainTitle = document.getElementById('chart-main-title');
  if (!container || !currentMonitoringTs) return;

  const dim = currentMonitoringTs.dimension === 'environments' ? 'environments' : 'pdu';
  if (mainTitle) {
    mainTitle.textContent =
      dim === 'environments'
        ? 'Environment Units Across Time (Cloud Monitoring)'
        : 'PDUs Across Time (Cloud Monitoring)';
  }
  const envFilter = document.getElementById('filter-env')?.value || 'ALL';
  const footprintFilter = document.getElementById('filter-footprint')?.value || 'ALL';
  const hideZero = document.getElementById('chk-hide-zero')?.checked ?? false;

  const zeroPduEnvKeys = new Set();
  if (hideZero && currentPortfolio?.environments) {
    for (const e of currentPortfolio.environments) {
      if ((e.totalPdus || 0) === 0) {
        zeroPduEnvKeys.add(`${e.organization}::${e.environment}`);
      }
    }
  }

  const summary = currentPortfolio?.summary || {};
  const limitVal =
    dim === 'environments'
      ? summary.entitlementEnvs || 6
      : summary.entitlementPdu || 500;

  const unitLabel = dim === 'environments' ? 'Env Units' : 'PDUs';
  const formatVal = (v) => Number(v || 0).toLocaleString();
  const totalOrgsCount = currentPortfolio?.organizations?.length || 0;

  const validPortfolioOrgs = Array.isArray(currentPortfolio?.organizations)
    ? new Set(currentPortfolio.organizations.map((o) => o.organization))
    : null;

  // Filter series by active Multi-Select Orgs / Env / Footprint / Hide 0-PDU Envs
  const matchingSeries = (currentMonitoringTs.series || []).filter((s) => {
    if (validPortfolioOrgs && !validPortfolioOrgs.has(s.organization)) return false;
    if (!isOrgSelected(s.organization)) return false;
    if (envFilter !== 'ALL' && s.environment !== envFilter) return false;
    if (footprintFilter !== 'ALL' && s.runtimeType !== footprintFilter) return false;
    if (hideZero && dim === 'pdu' && zeroPduEnvKeys.has(`${s.organization}::${s.environment}`)) return false;
    return true;
  });

  // 1. Determine alignment resolution (bucket interval in ms)
  const alignSeconds =
    currentMonitoringTs.alignmentPeriodSeconds ||
    (selectedMonitoringDays <= 1
      ? 3600
      : selectedMonitoringDays <= 7
      ? 14400
      : selectedMonitoringDays <= 14
      ? 21600
      : 43200);
  const bucketMs = alignSeconds * 1000;
  const monErrors = Array.isArray(currentMonitoringTs.monitoringErrors)
    ? currentMonitoringTs.monitoringErrors
    : [];
  const hybridOrgs = Array.isArray(currentMonitoringTs.unsupportedHybridOrganizations)
    ? currentMonitoringTs.unsupportedHybridOrganizations.filter((o) => isOrgSelected(o))
    : [];

  // 2. Gather all valid epoch timestamps across matching series
  const allEpochs = [];
  matchingSeries.forEach((s) => {
    (s.points || []).forEach((pt) => {
      const t = new Date(pt.timestamp).getTime();
      if (!isNaN(t)) allEpochs.push(t);
    });
  });

  if (!allEpochs.length) {
    const scopeHint =
      footprintFilter === 'HYBRID' || (hybridOrgs.length > 0 && matchingSeries.length === 0)
        ? ' Note: apigee.googleapis.com/proxy/details tracks Apigee X active proxy revisions only (not emitted by Apigee Hybrid or 0-deployment environments).'
        : '';
    const errHint = monErrors.length > 0 ? ` (${monErrors.length} query error(s): ${monErrors[0]})` : '';
    container.innerHTML = `<div class="chart-empty">No Cloud Monitoring telemetry found for the selected filter (${escapeHtml(
      unitLabel
    )}).${escapeHtml(scopeHint + errHint)}</div>`;
    if (subtitle) {
      subtitle.innerHTML = `Metric: <code>apigee.googleapis.com/proxy/details</code> (Apigee X active revisions) &bull; 0 active series for current filter`;
    }
    return;
  }

  // 3. Create regular canonical time buckets clamped to the requested lookback window
  const rawMinEpoch = Math.floor(Math.min(...allEpochs) / bucketMs) * bucketMs;
  const maxEpoch = Math.floor(Math.max(...allEpochs) / bucketMs) * bucketMs;
  const windowDays = Number(currentMonitoringTs.days || selectedMonitoringDays || 14);
  const windowStartEpoch = Math.floor((maxEpoch - windowDays * 86400 * 1000) / bucketMs) * bucketMs;
  const minEpoch = Math.max(rawMinEpoch, windowStartEpoch);

  const buckets = [];
  for (let t = minEpoch; t <= maxEpoch; t += bucketMs) {
    buckets.push(t);
  }

  // 4. Aggregate series across regular buckets with forward-fill (sample-and-hold for continuous gauge values)
  let points = [];
  if (buckets.length > 1) {
    const bucketTotals = new Array(buckets.length).fill(0);

    matchingSeries.forEach((s) => {
      const rawPts = (s.points || [])
        .map((p) => ({ time: new Date(p.timestamp).getTime(), val: Number(p.pdu || 0) }))
        .filter((p) => !isNaN(p.time))
        .sort((a, b) => a.time - b.time);

      if (!rawPts.length) return;

      const firstTime = rawPts[0].time;
      const firstVal = rawPts[0].val;
      let ptIdx = 0;
      const startsNearWindow = firstTime - minEpoch <= 2 * bucketMs;
      let currentVal = startsNearWindow ? firstVal : 0;
      let lastSampleTime = startsNearWindow ? minEpoch : -Infinity;

      buckets.forEach((bucketTime, bIdx) => {
        while (ptIdx < rawPts.length && rawPts[ptIdx].time <= bucketTime + bucketMs / 2) {
          currentVal = rawPts[ptIdx].val;
          lastSampleTime = rawPts[ptIdx].time;
          ptIdx++;
        }
        if (bucketTime - lastSampleTime > 2 * bucketMs) {
          currentVal = 0;
        }
        bucketTotals[bIdx] += currentVal;
      });
    });

    points = buckets.map((t, idx) => ({
      timestamp: new Date(t).toISOString(),
      date: new Date(t),
      pdu: bucketTotals[idx],
    }));
  } else {
    // Single point fallback
    const timeMap = new Map();
    matchingSeries.forEach((s) => {
      (s.points || []).forEach((pt) => {
        timeMap.set(pt.timestamp, (timeMap.get(pt.timestamp) || 0) + pt.pdu);
      });
    });
    const sortedTimestamps = Array.from(timeMap.keys()).sort();
    points = sortedTimestamps.map((ts) => ({
      timestamp: ts,
      date: new Date(ts),
      pdu: timeMap.get(ts),
    }));
  }

  if (!points.length) {
    container.innerHTML = `<div class="chart-empty">No Cloud Monitoring telemetry found for the selected filter (${escapeHtml(
      unitLabel
    )}).</div>`;
    if (subtitle) {
      subtitle.innerHTML = `Metric: <code>apigee.googleapis.com/proxy/details</code> &bull; 0 active series for current filter`;
    }
    return;
  }

  const values = points.map((p) => p.pdu);
  const minVal = Math.min(...values);
  const maxVal = Math.max(...values);
  const latestVal = points[points.length - 1].pdu;

  const isLatestAbove = latestVal > limitVal;
  if (chartPill) {
    if (isLatestAbove) {
      chartPill.className = 'chart-status-pill above';
      chartPill.textContent = `ABOVE LIMIT (+${formatVal(latestVal - limitVal)} ${unitLabel})`;
    } else {
      chartPill.className = 'chart-status-pill below';
      chartPill.textContent = `UNDER LIMIT (${formatVal(limitVal - latestVal)} AVAIL)`;
    }
  }

  const orgScopeText = getSelectedOrgsLabel(totalOrgsCount);
  const scopeDesc =
    allOrgsSelected && envFilter === 'ALL'
      ? `All Monitored Orgs (${matchingSeries.length} env series)`
      : `${orgScopeText}${envFilter !== 'ALL' ? ' / ' + envFilter : ''}`;

  if (subtitle) {
    const subLight = isLightTheme();
    const warnColor = subLight ? '#b45309' : '#fbbf24';
    const latestColor = isLatestAbove ? (subLight ? '#dc2626' : '#f87171') : (subLight ? '#059669' : '#34d399');
    const hybridNote =
      hybridOrgs.length > 0 ? ` &bull; <em>Excl. ${hybridOrgs.length} Hybrid org(s)</em>` : '';
    const monErrNote =
      monErrors.length > 0
        ? ` &bull; <span style="color:${warnColor};" title="${escapeHtml(monErrors.join(' | '))}">&#9888;&#65039; ${monErrors.length} Monitoring warning(s)</span>`
        : '';
    subtitle.innerHTML = `Metric: <code>apigee.googleapis.com/proxy/details</code> &bull; <strong>${escapeHtml(
      scopeDesc
    )}</strong> &bull; Min: <strong>${formatVal(minVal)}</strong> | Max: <strong>${formatVal(
      maxVal
    )}</strong> | Latest: <strong style="color:${latestColor};">${formatVal(latestVal)} ${unitLabel}</strong>${hybridNote}${monErrNote}`;
  }

  const width = container.clientWidth || 960;
  const height = 235;
  const padLeft = 58;
  const padRight = 24;
  const padTop = 18;
  const padBottom = 32;
  const plotW = Math.max(100, width - padLeft - padRight);
  const plotH = Math.max(80, height - padTop - padBottom);

  const showLimitLine =
    (allOrgsSelected && envFilter === 'ALL') || maxVal >= limitVal * 0.5;
  const yMaxRaw = showLimitLine ? Math.max(maxVal, limitVal) : maxVal;
  const yMax = Math.max(10, Math.ceil(yMaxRaw * 1.12));
  const yMin = 0;

  const xForIdx = (i) =>
    points.length === 1
      ? padLeft + plotW / 2
      : padLeft + (i / (points.length - 1)) * plotW;
  const yForVal = (v) => padTop + plotH - ((v - yMin) / (yMax - yMin)) * plotH;

  const coords = points.map((p, i) => ({
    x: xForIdx(i),
    y: yForVal(p.pdu),
    ...p,
  }));

  const isSingleBucket = coords.length === 1;
  const linePath = isSingleBucket
    ? `M${padLeft.toFixed(1)},${coords[0].y.toFixed(1)} L${(padLeft + plotW).toFixed(1)},${coords[0].y.toFixed(1)}`
    : coords.map((c, i) => `${i === 0 ? 'M' : 'L'}${c.x.toFixed(1)},${c.y.toFixed(1)}`).join(' ');
  const areaPath = isSingleBucket
    ? `${linePath} L${(padLeft + plotW).toFixed(1)},${(padTop + plotH).toFixed(1)} L${padLeft.toFixed(1)},${(padTop + plotH).toFixed(1)} Z`
    : `${linePath} L${coords[coords.length - 1].x.toFixed(1)},${(padTop + plotH).toFixed(1)} L${coords[0].x.toFixed(1)},${(padTop + plotH).toFixed(1)} Z`;

  const lightMode = isLightTheme();
  const COLOR_GREEN = lightMode ? '#059669' : '#34d399';
  const COLOR_RED = lightMode ? '#dc2626' : '#f87171';
  const gridStroke = lightMode ? 'rgba(15,23,42,0.12)' : 'rgba(255,255,255,0.1)';
  const axisTextColor = lightMode ? '#475569' : 'rgba(255,255,255,0.6)';

  const strokeStops = coords
    .map((c, i) => {
      const pct = coords.length === 1 ? 0 : ((i / (coords.length - 1)) * 100).toFixed(2);
      const col = c.pdu > limitVal ? COLOR_RED : COLOR_GREEN;
      return `<stop offset="${pct}%" stop-color="${col}" />`;
    })
    .join('');

  const fillStops = coords
    .map((c, i) => {
      const pct = coords.length === 1 ? 0 : ((i / (coords.length - 1)) * 100).toFixed(2);
      const col = c.pdu > limitVal ? COLOR_RED : COLOR_GREEN;
      return `<stop offset="${pct}%" stop-color="${col}" stop-opacity="0.32" />`;
    })
    .join('');

  const primaryColor = isLatestAbove ? COLOR_RED : COLOR_GREEN;

  const yTicks = [0, 0.33, 0.66, 1].map((t) => Math.round(yMin + t * (yMax - yMin)));
  const yGridHtml = yTicks
    .map((tv) => {
      const y = yForVal(tv).toFixed(1);
      return `
        <line x1="${padLeft}" y1="${y}" x2="${padLeft + plotW}" y2="${y}" stroke="${gridStroke}" stroke-dasharray="3,3" />
        <text x="${padLeft - 8}" y="${Number(y) + 4}" text-anchor="end" fill="${axisTextColor}" font-family="JetBrains Mono, monospace" font-size="11">${escapeHtml(formatVal(tv))}</text>
      `;
    })
    .join('');

  const xTickIndices = [];
  const stepCount = Math.min(6, points.length);
  for (let i = 0; i < stepCount; i++) {
    const idx = Math.round((i / Math.max(1, stepCount - 1)) * (points.length - 1));
    if (!xTickIndices.includes(idx)) xTickIndices.push(idx);
  }
  const xLabelsHtml = xTickIndices
    .map((idx) => {
      const c = coords[idx];
      const d = c.date;
      const lbl =
        selectedMonitoringDays <= 1
          ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
          : d.toLocaleDateString([], { month: 'short', day: 'numeric' });
      return `<text x="${c.x.toFixed(1)}" y="${height - 8}" text-anchor="middle" fill="${axisTextColor}" font-family="JetBrains Mono, monospace" font-size="11">${escapeHtml(lbl)}</text>`;
    })
    .join('');

  let limitLineHtml = '';
  if (limitVal <= yMax) {
    const ly = yForVal(limitVal).toFixed(1);
    const limitBadgeText = `Limit: ${formatVal(limitVal)} ${unitLabel}`;
    limitLineHtml = `
      <line x1="${padLeft}" y1="${ly}" x2="${padLeft + plotW}" y2="${ly}" stroke="${COLOR_RED}" stroke-width="1.5" stroke-dasharray="6,4" />
      <rect x="${padLeft + plotW - 156}" y="${Number(ly) - 19}" width="152" height="17" rx="6" fill="rgba(239,68,68,0.28)" />
      <text x="${padLeft + plotW - 80}" y="${Number(ly) - 7}" text-anchor="middle" fill="${lightMode ? '#991b1b' : '#fecaca'}" font-family="JetBrains Mono, monospace" font-size="10" font-weight="700">${escapeHtml(limitBadgeText)}</text>
    `;
  }

  container.innerHTML = `
    <svg width="100%" height="${height}" viewBox="0 0 ${width} ${height}" id="monitoring-svg" style="overflow: visible;">
      <defs>
        <linearGradient id="pduStrokeGrad" x1="0%" y1="0%" x2="100%" y2="0%">
          ${strokeStops}
        </linearGradient>
        <linearGradient id="pduAreaGrad" x1="0%" y1="0%" x2="100%" y2="0%">
          ${fillStops}
        </linearGradient>
        <linearGradient id="pduVerticalFade" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#ffffff" stop-opacity="1" />
          <stop offset="100%" stop-color="#ffffff" stop-opacity="0.04" />
        </linearGradient>
        <mask id="pduAreaMask">
          <rect x="0" y="0" width="${width}" height="${height}" fill="url(#pduVerticalFade)" />
        </mask>
      </defs>
      ${yGridHtml}
      <path d="${areaPath}" fill="${isSingleBucket ? primaryColor : 'url(#pduAreaGrad)'}" fill-opacity="${isSingleBucket ? '0.22' : '1'}" mask="url(#pduAreaMask)" />
      <path d="${linePath}" fill="none" stroke="${isSingleBucket ? primaryColor : 'url(#pduStrokeGrad)'}" stroke-width="2.75" stroke-linejoin="round" stroke-linecap="round" />
      ${isSingleBucket ? `<circle cx="${coords[0].x.toFixed(1)}" cy="${coords[0].y.toFixed(1)}" r="4.5" fill="${primaryColor}" stroke="#fff" stroke-width="1.5" />` : ''}
      ${limitLineHtml}
      ${xLabelsHtml}
      <line id="hover-vline" x1="0" y1="${padTop}" x2="0" y2="${padTop + plotH}" stroke="rgba(255,255,255,0.45)" stroke-width="1" stroke-dasharray="2,2" style="display:none;" />
      <circle id="hover-dot" cx="0" cy="0" r="5" fill="${primaryColor}" stroke="#fff" stroke-width="2" style="display:none;" />
      <rect id="hover-capture" x="${padLeft}" y="${padTop}" width="${plotW}" height="${plotH}" fill="transparent" style="cursor: crosshair;" />
    </svg>
    <div id="chart-tooltip" class="chart-tooltip"></div>
  `;

  const captureRect = document.getElementById('hover-capture');
  const vline = document.getElementById('hover-vline');
  const dot = document.getElementById('hover-dot');
  const tooltip = document.getElementById('chart-tooltip');

  captureRect?.addEventListener('mousemove', (ev) => {
    const rect = captureRect.getBoundingClientRect();
    const relX = Math.max(0, Math.min(plotW, ev.clientX - rect.left));
    const idx = Math.round((relX / plotW) * (coords.length - 1));
    const pt = coords[idx];
    if (!pt) return;

    const ptAbove = pt.pdu > limitVal;
    const ptColor = ptAbove ? COLOR_RED : COLOR_GREEN;

    vline.setAttribute('x1', pt.x);
    vline.setAttribute('x2', pt.x);
    vline.style.display = 'block';

    dot.setAttribute('cx', pt.x);
    dot.setAttribute('cy', pt.y);
    dot.setAttribute('fill', ptColor);
    dot.style.display = 'block';

    const statusStr = ptAbove
      ? `<span style="color:#f87171;font-weight:700;">ABOVE LIMIT (+${formatVal(pt.pdu - limitVal)})</span>`
      : `<span style="color:#34d399;font-weight:700;">UNDER LIMIT (${formatVal(limitVal - pt.pdu)} avail)</span>`;

    tooltip.innerHTML = `
      <div>${pt.date.toLocaleString()}</div>
      <div><strong>${pt.pdu.toLocaleString()} ${unitLabel}</strong> &bull; ${statusStr}</div>
    `;
    tooltip.style.display = 'block';
    const leftPos = Math.min(width - 240, Math.max(padLeft, pt.x - 85));
    tooltip.style.left = `${leftPos}px`;
    tooltip.style.top = `${Math.max(4, pt.y - 56)}px`;
  });

  captureRect?.addEventListener('mouseleave', () => {
    vline.style.display = 'none';
    dot.style.display = 'none';
    tooltip.style.display = 'none';
  });
}

/* Monthly API Calls Chart */

function updateCallsBreakdownBanner(stdCalls, envoyCalls, extCalls, weightedCalls, rawCalls) {
  const std = Number(stdCalls || 0);
  const envoy = Number(envoyCalls || 0);
  const ext = Number(extCalls || 0);
  const stdEntitlement = Math.round(std * 0.2);
  const extEntitlement = Math.round(ext * 1.0);
  const entitlementSum = stdEntitlement + extEntitlement;
  const hasBreakdown = std > 0 || ext > 0;
  const weighted = hasBreakdown ? entitlementSum : Number(weightedCalls ?? entitlementSum);
  const raw = hasBreakdown ? (std + ext) : Number(rawCalls ?? (std + ext));

  const extEl = document.getElementById('banner-calls-ext');
  if (extEl) {
    extEl.textContent = `${ext.toLocaleString()} (${formatCompactCalls(ext)})`;
  }
  const extWeightedEl = document.getElementById('banner-calls-ext-weighted');
  if (extWeightedEl) {
    extWeightedEl.innerHTML = `&rarr; <strong>${extEntitlement.toLocaleString()}</strong> ent. calls (1.0&times;)`;
  }

  const stdEl = document.getElementById('banner-calls-std');
  if (stdEl) {
    stdEl.textContent = `${std.toLocaleString()} (${formatCompactCalls(std)})`;
  }
  const stdWeightedEl = document.getElementById('banner-calls-std-weighted');
  if (stdWeightedEl) {
    stdWeightedEl.innerHTML = `&rarr; <strong>${stdEntitlement.toLocaleString()}</strong> ent. calls (0.2&times;)`;
  }

  const envoyEl = document.getElementById('banner-calls-envoy');
  if (envoyEl) {
    envoyEl.textContent = `${envoy.toLocaleString()} (${formatCompactCalls(envoy)})`;
  }
  const envoyWeightedEl = document.getElementById('banner-calls-envoy-weighted');
  if (envoyWeightedEl) {
    envoyWeightedEl.innerHTML = `&rarr; <strong>0</strong> ent. calls (Free / 0&times;)`;
  }

  const totalWeightedEl = document.getElementById('banner-calls-total-weighted');
  if (totalWeightedEl) {
    totalWeightedEl.textContent = `${weighted.toLocaleString()} (${formatCompactCalls(weighted)})`;
  }
  const rawTotalEl = document.getElementById('banner-calls-raw-total');
  if (rawTotalEl) {
    rawTotalEl.innerHTML = `- <strong>${formatCompactCalls(extEntitlement)}</strong> Ext + <strong>${formatCompactCalls(stdEntitlement)}</strong> Std Ent. &bull; Raw: ${formatCompactCalls(raw)}`;
  }
}

async function fetchMonthlyCallsSeries(refresh = false, { skipRender = false } = {}) {
  const reqId = ++callsFetchSeq;
  const container = document.getElementById('calls-monthly-chart-container');
  const subtitle = document.getElementById('calls-chart-subtitle');
  if (!skipRender && container && (!currentCallsTs || refresh)) {
    container.innerHTML =
      '<div class="chart-empty">Querying Apigee Analytics (90-day chunks) for API calls...</div>';
  }

  try {
    const jur = getStoredJurisdiction();
    const res = await apiFetch(`/api/monitoring/timeseries?dimension=calls&refresh=${refresh ? 'true' : 'false'}&jurisdiction=${encodeURIComponent(jur)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const payload = await res.json();
    if (reqId !== callsFetchSeq) return;
    currentCallsTs = payload;
    if (refresh || currentCallsTs?.callsRefreshed) {
      const portRes = await apiFetch(`/api/portfolio?jurisdiction=${encodeURIComponent(jur)}`);
      if (portRes.ok && reqId === callsFetchSeq) {
        currentPortfolio = await portRes.json();
        if (!skipRender) {
          renderSimplifiedDashboard();
        }
      }
    }
    if (!skipRender) {
      renderMonthlyCallsChart();
      if (Array.isArray(currentCallsTs?.trafficErrors) && currentCallsTs.trafficErrors.length > 0) {
        showTrafficErrorModal(currentCallsTs.trafficErrors, () => fetchMonthlyCallsSeries(true));
      }
    }
  } catch (err) {
    if (reqId !== callsFetchSeq) return;
    if (skipRender) {
      console.warn('Monthly calls prefetch warning:', err);
      if (!currentCallsTs) {
        currentCallsTs = { dimension: 'calls', series: [], seriesCount: 0 };
      }
      return;
    }
    if (container) {
      container.innerHTML = `<div class="chart-empty">Could not load monthly API calls: ${escapeHtml(err.message)}</div>`;
    }
    if (subtitle) {
      subtitle.textContent = 'Error loading monthly API calls';
    }
    showTrafficErrorModal([`Failed to load monthly API calls: ${err.message}`], () => fetchMonthlyCallsSeries(true));
  }
}

function renderMonthlyCallsChart() {
  const container = document.getElementById('calls-monthly-chart-container');
  const subtitle = document.getElementById('calls-chart-subtitle');
  const callsPill = document.getElementById('calls-chart-limit-pill');
  if (!container || !currentCallsTs) return;

  const envFilter = document.getElementById('filter-env')?.value || 'ALL';
  const footprintFilter = document.getElementById('filter-footprint')?.value || 'ALL';
  const summary = currentPortfolio?.summary || {};
  const yearlyLimit = summary.entitlementYearlyCalls || 1_500_000_000;
  const monthlyPaceLimit = Math.round(yearlyLimit / 12);

  const periodStartStr =
    currentCallsTs.periodStart ||
    summary.contractPeriodStart ||
    summary.contractStartDate ||
    '2026-01-01';
  const periodEndStr = currentCallsTs.periodEnd || summary.contractPeriodEnd || '';

  const validPortfolioOrgs = Array.isArray(currentPortfolio?.organizations)
    ? new Set(currentPortfolio.organizations.map((o) => o.organization))
    : null;

  // Filter series by active Multi-Select Orgs / Env / Footprint
  const matchingSeries = (currentCallsTs.series || []).filter((s) => {
    if (validPortfolioOrgs && !validPortfolioOrgs.has(s.organization)) return false;
    if (!isOrgSelected(s.organization)) return false;
    if (envFilter !== 'ALL' && s.environment !== envFilter) return false;
    if (footprintFilter !== 'ALL' && s.runtimeType !== footprintFilter) return false;
    return true;
  });

  // Build calendar monthly buckets spanning [periodStartStr, periodEndStr) (12 buckets on 1st-of-month, 13 on mid-month)
  const startParts = periodStartStr.split('-').map(Number);
  const startYear = startParts[0] || new Date().getUTCFullYear();
  const startMonth0 = (startParts[1] || 1) - 1;

  let bucketCount = 12;
  if (periodEndStr) {
    const endParts = periodEndStr.split('-').map(Number);
    if (endParts[0] && endParts[1]) {
      const endExclusiveUtc = Date.UTC(endParts[0], endParts[1] - 1, endParts[2] || 1);
      const lastInclusiveDt = new Date(endExclusiveUtc - 86400 * 1000);
      const monthsDiff =
        (lastInclusiveDt.getUTCFullYear() - startYear) * 12 +
        (lastInclusiveDt.getUTCMonth() - startMonth0) +
        1;
      bucketCount = Math.max(12, Math.min(13, monthsDiff));
    }
  }

  const monthBuckets = [];
  const monthKeyToIndex = new Map();
  for (let m = 0; m < bucketCount; m++) {
    const d = new Date(Date.UTC(startYear, startMonth0 + m, 1));
    const ymKey = `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}`;
    const shortLabel = d.toLocaleDateString('en-US', {
      month: 'short',
      year: '2-digit',
      timeZone: 'UTC',
    });
    const fullLabel = d.toLocaleDateString('en-US', {
      month: 'long',
      year: 'numeric',
      timeZone: 'UTC',
    });
    monthKeyToIndex.set(ymKey, m);
    monthBuckets.push({
      ymKey,
      shortLabel,
      fullLabel,
      stdCalls: 0,
      extCalls: 0,
      fallbackEntCalls: 0,
      entitlementCalls: 0,
      envoyCalls: 0,
      cumulativeCalls: 0,
    });
  }

  // Sum daily calls into active contract-period monthly buckets [periodStartStr, periodEndStr)
  matchingSeries.forEach((s) => {
    (s.points || []).forEach((pt) => {
      const dayStr = String(pt.timestamp || '').slice(0, 10);
      if (dayStr < periodStartStr) return;
      if (periodEndStr && dayStr >= periodEndStr) return;
      const ymKey = dayStr.slice(0, 7);
      const idx = monthKeyToIndex.get(ymKey);
      if (idx === undefined) return;
      const dStd = Number(pt.standardCalls ?? 0);
      const dExt = Number(pt.extensibleCalls ?? 0);
      const entVal = Number(pt.entitlementCalls ?? pt.dailyCalls ?? pt.pdu ?? 0);
      const envoyVal = Number(pt.envoyCalls ?? 0);
      if (dStd > 0 || dExt > 0) {
        monthBuckets[idx].stdCalls += dStd;
        monthBuckets[idx].extCalls += dExt;
      } else if (entVal > 0) {
        monthBuckets[idx].fallbackEntCalls += entVal;
      }
      if (envoyVal > 0) {
        monthBuckets[idx].envoyCalls += envoyVal;
      }
    });
  });

  // Compute exact monthly weighted entitlement calls and running cumulative total
  let runningTotal = 0;
  monthBuckets.forEach((b) => {
    b.entitlementCalls =
      b.stdCalls > 0 || b.extCalls > 0
        ? Math.round(b.stdCalls * 0.2) + Math.round(b.extCalls * 1.0) + b.fallbackEntCalls
        : b.fallbackEntCalls;
    runningTotal += b.entitlementCalls;
    b.cumulativeCalls = runningTotal;
  });

  const totalContractCalls = runningTotal;
  const isYearlyAbove = totalContractCalls > yearlyLimit;
  const pctYearly = yearlyLimit > 0 ? ((totalContractCalls / yearlyLimit) * 100).toFixed(1) : '0.0';

  const seriesStdCalls = matchingSeries.reduce((sum, s) => sum + (s.standardApiCalls || 0), 0);
  const seriesEnvoyCalls = matchingSeries.reduce((sum, s) => sum + (s.envoyApiCalls || 0), 0);
  const seriesExtCalls = matchingSeries.reduce((sum, s) => sum + (s.extensibleApiCalls || 0), 0);
  const seriesEntitlementCalls =
    Math.round(seriesStdCalls * 0.2) + Math.round(seriesExtCalls * 1.0);
  const seriesRawCalls = matchingSeries.reduce(
    (sum, s) =>
      sum +
      (s.rawApiCalls ??
        ((s.standardApiCalls || 0) + (s.extensibleApiCalls || 0))),
    0
  );
  updateCallsBreakdownBanner(
    seriesStdCalls,
    seriesEnvoyCalls,
    seriesExtCalls,
    seriesEntitlementCalls > 0 ? seriesEntitlementCalls : totalContractCalls,
    seriesRawCalls
  );

  if (callsPill) {
    if (isYearlyAbove) {
      callsPill.className = 'chart-status-pill above';
      callsPill.textContent = `ABOVE YEARLY LIMIT (+${formatCompactCalls(totalContractCalls - yearlyLimit)} CALLS)`;
    } else {
      callsPill.className = 'chart-status-pill below';
      callsPill.textContent = `UNDER YEARLY LIMIT (${formatCompactCalls(yearlyLimit - totalContractCalls)} AVAIL)`;
    }
  }

  if (subtitle) {
    const periodDisplay = periodEndStr
      ? `${escapeHtml(periodStartStr)} &rarr; ${escapeHtml(periodEndStr)}`
      : escapeHtml(periodStartStr);
    subtitle.innerHTML = `Contract Period: <code>${periodDisplay}</code>`;
  }

  const width = container.clientWidth || 960;
  const height = 235;
  const padLeft = 64;
  const padRight = 24;
  const padTop = 24;
  const padBottom = 34;
  const plotW = Math.max(120, width - padLeft - padRight);
  const plotH = Math.max(80, height - padTop - padBottom);

  const maxMonthCalls = Math.max(
    ...monthBuckets.map((b) => b.entitlementCalls + b.envoyCalls),
    0
  );
  const yMaxRaw = Math.max(maxMonthCalls, monthlyPaceLimit * 1.15, 100);
  const yMax = Math.ceil(yMaxRaw * 1.15);
  const yMin = 0;

  const yForVal = (v) => padTop + plotH - ((v - yMin) / (yMax - yMin)) * plotH;

  const lightMode = isLightTheme();
  const COLOR_GREEN = lightMode ? '#059669' : '#34d399';
  const COLOR_RED = lightMode ? '#dc2626' : '#f87171';
  const COLOR_ENVOY_GREY = lightMode ? '#94a3b8' : '#64748b';
  const gridStroke = lightMode ? 'rgba(15,23,42,0.12)' : 'rgba(255,255,255,0.1)';
  const axisTextColor = lightMode ? '#475569' : 'rgba(255,255,255,0.68)';

  // Y-axis grid lines
  const yTicks = [0, 0.33, 0.66, 1].map((t) => Math.round(yMin + t * (yMax - yMin)));
  const yGridHtml = yTicks
    .map((tv) => {
      const y = yForVal(tv).toFixed(1);
      return `
        <line x1="${padLeft}" y1="${y}" x2="${padLeft + plotW}" y2="${y}" stroke="${gridStroke}" stroke-dasharray="3,3" />
        <text x="${padLeft - 8}" y="${Number(y) + 4}" text-anchor="end" fill="${axisTextColor}" font-family="JetBrains Mono, monospace" font-size="11">${escapeHtml(
          formatCompactCalls(tv)
        )}</text>
      `;
    })
    .join('');

  // Monthly average pace reference line (Yearly Limit / 12)
  let paceLineHtml = '';
  if (monthlyPaceLimit <= yMax) {
    const py = yForVal(monthlyPaceLimit).toFixed(1);
    paceLineHtml = `
      <line x1="${padLeft}" y1="${py}" x2="${padLeft + plotW}" y2="${py}" stroke="${lightMode ? '#d97706' : 'rgba(251, 191, 36, 0.75)'}" stroke-width="1.5" stroke-dasharray="6,4" />
      <rect x="${padLeft + plotW - 184}" y="${Number(py) - 19}" width="180" height="17" rx="6" fill="rgba(245, 158, 11, 0.24)" />
      <text x="${padLeft + plotW - 94}" y="${Number(py) - 7}" text-anchor="middle" fill="${lightMode ? '#92400e' : '#fde68a'}" font-family="JetBrains Mono, monospace" font-size="10" font-weight="700">Monthly Pace: ${escapeHtml(
        formatCompactCalls(monthlyPaceLimit)
      )}/mo</text>
    `;
  }

  // Render monthly stacked bars (bottom: Entitlement = Std + Ext, top: Envoy Adapter in grey)
  const slotW = plotW / Math.max(1, monthBuckets.length);
  const barW = Math.max(16, Math.min(46, slotW * 0.58));
  const yBase = padTop + plotH;

  const barsHtml = monthBuckets
    .map((b, idx) => {
      const cx = padLeft + idx * slotW + slotW / 2;
      const bx = cx - barW / 2;
      const totalBarCalls = b.entitlementCalls + b.envoyCalls;
      const isBarAbove = b.cumulativeCalls > yearlyLimit || b.entitlementCalls > monthlyPaceLimit;
      const barColor = isBarAbove ? COLOR_RED : COLOR_GREEN;

      const yEnt = b.entitlementCalls > 0 ? yForVal(b.entitlementCalls) : yBase;
      const hEnt = Math.max(totalBarCalls > 0 ? 0 : 2, yBase - yEnt);
      const yTop = totalBarCalls > 0 ? yForVal(totalBarCalls) : yBase - 2;
      const hEnvoy = b.envoyCalls > 0 ? Math.max(2, yEnt - yTop) : 0;

      const entRectHtml =
        b.entitlementCalls > 0 || totalBarCalls === 0
          ? `<rect
            x="${bx.toFixed(1)}"
            y="${(totalBarCalls === 0 ? yBase - 2 : yEnt).toFixed(1)}"
            width="${barW.toFixed(1)}"
            height="${(totalBarCalls === 0 ? 2 : hEnt).toFixed(1)}"
            rx="${b.envoyCalls > 0 ? '2' : '6'}"
            fill="${barColor}"
            fill-opacity="${totalBarCalls > 0 ? '0.85' : '0.2'}"
            stroke="${barColor}"
            stroke-width="1"
          />`
          : '';

      const envoyRectHtml =
        b.envoyCalls > 0
          ? `<rect
            x="${bx.toFixed(1)}"
            y="${yTop.toFixed(1)}"
            width="${barW.toFixed(1)}"
            height="${hEnvoy.toFixed(1)}"
            rx="4"
            fill="${COLOR_ENVOY_GREY}"
            fill-opacity="0.82"
            stroke="${COLOR_ENVOY_GREY}"
            stroke-width="1"
          />`
          : '';

      const valLabel =
        totalBarCalls > 0
          ? `<text x="${cx.toFixed(1)}" y="${Math.max(12, yTop - 6).toFixed(
              1
            )}" text-anchor="middle" fill="${barColor}" font-family="JetBrains Mono, monospace" font-size="10" font-weight="700">${escapeHtml(
              formatCompactCalls(b.entitlementCalls)
            )}</text>`
          : '';

      return `
        <g class="month-bar-group" data-month-idx="${idx}">
          ${entRectHtml}
          ${envoyRectHtml}
          ${valLabel}
          <text x="${cx.toFixed(1)}" y="${height - 8}" text-anchor="middle" fill="${axisTextColor}" font-family="JetBrains Mono, monospace" font-size="10.5">${escapeHtml(
            b.shortLabel
          )}</text>
          <rect
            class="month-hover-zone"
            data-month-idx="${idx}"
            x="${(padLeft + idx * slotW).toFixed(1)}"
            y="${padTop}"
            width="${slotW.toFixed(1)}"
            height="${plotH}"
            fill="transparent"
            style="cursor: pointer;"
          />
        </g>
      `;
    })
    .join('');

  container.innerHTML = `
    <svg width="100%" height="${height}" viewBox="0 0 ${width} ${height}" id="calls-monthly-svg" style="overflow: visible;">
      ${yGridHtml}
      ${paceLineHtml}
      ${barsHtml}
    </svg>
    <div id="calls-chart-tooltip" class="chart-tooltip"></div>
  `;

  const tooltip = document.getElementById('calls-chart-tooltip');
  container.querySelectorAll('.month-hover-zone').forEach((zone) => {
    zone.addEventListener('mousemove', (ev) => {
      const idx = Number(zone.getAttribute('data-month-idx'));
      const b = monthBuckets[idx];
      if (!b || !tooltip) return;

      const cumPct = yearlyLimit > 0 ? ((b.cumulativeCalls / yearlyLimit) * 100).toFixed(1) : '0.0';
      const isCumAbove = b.cumulativeCalls > yearlyLimit;
      const statusHtml = isCumAbove
        ? `<span style="color:#f87171;font-weight:700;">ABOVE YEARLY LIMIT</span>`
        : `<span style="color:#34d399;font-weight:700;">${cumPct}% of Yearly Limit</span>`;

      tooltip.innerHTML = `
        <div><strong>${escapeHtml(b.fullLabel)}</strong></div>
        <div>Entitlement Calls (Std + Ext): <strong>${b.entitlementCalls.toLocaleString()} (${formatCompactCalls(b.entitlementCalls)})</strong></div>
        <div>Envoy Adapter Calls (Free): <strong>${b.envoyCalls.toLocaleString()} (${formatCompactCalls(b.envoyCalls)})</strong></div>
        <div>Cumulative YTD (Entitlement): <strong>${b.cumulativeCalls.toLocaleString()}</strong> / ${formatCompactCalls(yearlyLimit)} &bull; ${statusHtml}</div>
      `;
      tooltip.style.display = 'block';
      const cx = padLeft + idx * slotW + slotW / 2;
      const leftPos = Math.min(width - 260, Math.max(padLeft, cx - 110));
      tooltip.style.left = `${leftPos}px`;
      tooltip.style.top = `12px`;
    });

    zone.addEventListener('mouseleave', () => {
      if (tooltip) tooltip.style.display = 'none';
    });
  });
}

/* Dashboard Filtering & Rendering */

function populateOrgDropdown(portfolio) {
  const listContainer = document.getElementById('org-checkbox-list');
  const triggerLabel = document.getElementById('org-dropdown-label');
  const summaryBadge = document.getElementById('org-selection-summary');
  if (!listContainer || !portfolio) return;

  const hideZero = Boolean(document.getElementById('chk-hide-zero')?.checked);
  const allPortfolioOrgs = [...(portfolio.organizations || [])];
  const allPortfolioOrgSet = new Set(allPortfolioOrgs.map((o) => o.organization));
  const orgs = allPortfolioOrgs
    .filter((o) => !hideZero || (o.totalPdus || 0) > 0)
    .sort((a, b) =>
      String(a.organization || '').localeCompare(String(b.organization || ''), undefined, {
        sensitivity: 'base',
        numeric: true,
      })
    );

  Array.from(selectedOrgs).forEach((name) => {
    if (!allPortfolioOrgSet.has(name)) {
      selectedOrgs.delete(name);
    }
  });

  if (!allOrgsSelected && allPortfolioOrgs.length > 0 && selectedOrgs.size === allPortfolioOrgs.length) {
    allOrgsSelected = true;
    selectedOrgs.clear();
  }

  const visibleSelectedCount = orgs.filter((o) => allOrgsSelected || selectedOrgs.has(o.organization)).length;
  if (triggerLabel) {
    triggerLabel.textContent = getSelectedOrgsLabel(allPortfolioOrgs.length);
  }
  if (summaryBadge) {
    summaryBadge.textContent = allOrgsSelected
      ? `All Selected (${orgs.length})`
      : `${selectedOrgs.size} of ${allPortfolioOrgs.length} Selected`;
  }

  listContainer.replaceChildren();
  orgs.forEach((o) => {
    const checked = allOrgsSelected || selectedOrgs.has(o.organization);
    const labelEl = document.createElement('label');
    labelEl.className = 'org-checkbox-item';

    const leftEl = document.createElement('span');
    leftEl.className = 'org-checkbox-left';

    const inputEl = document.createElement('input');
    inputEl.type = 'checkbox';
    inputEl.className = 'org-checkbox-input';
    inputEl.value = String(o.organization || '');
    inputEl.checked = Boolean(checked);

    const nameEl = document.createElement('span');
    nameEl.className = 'org-checkbox-name';
    nameEl.title = String(o.organization || '');
    nameEl.textContent = String(o.organization || '');

    leftEl.append(inputEl, nameEl);

    const rightEl = document.createElement('span');
    rightEl.className = 'org-checkbox-right';

    const status = String(o.callsStatus || 'OK');
    if (status === 'ANALYTICS_DISABLED') {
      const badgeEl = document.createElement('span');
      badgeEl.className = 'calls-status-badge disabled';
      badgeEl.title = 'Apigee Analytics add-on is disabled on one or more environments in this organization';
      badgeEl.textContent = 'NO ANALYTICS';
      rightEl.appendChild(badgeEl);
    } else if (status === 'PARTIAL' || status === 'ERROR') {
      const badgeEl = document.createElement('span');
      badgeEl.className = `calls-status-badge ${status === 'ERROR' ? 'error' : 'partial'}`;
      badgeEl.title = (o.trafficErrors || []).join(' | ') || 'Apigee Analytics query issue';
      badgeEl.textContent = status;
      rightEl.appendChild(badgeEl);
    }

    const pduEl = document.createElement('span');
    pduEl.className = 'org-checkbox-pdu';
    pduEl.textContent = `${o.totalPdus ?? 0} PDU`;
    rightEl.appendChild(pduEl);

    labelEl.append(leftEl, rightEl);
    listContainer.appendChild(labelEl);
  });

  listContainer.querySelectorAll('.org-checkbox-input').forEach((input) => {
    input.addEventListener('change', (e) => {
      const orgName = e.target.value;
      const isChecked = e.target.checked;

      if (allOrgsSelected) {
        allOrgsSelected = false;
        selectedOrgs.clear();
        const seedPool = hideZero ? orgs : allPortfolioOrgs;
        seedPool.forEach((o) => {
          if (o.organization !== orgName) {
            selectedOrgs.add(o.organization);
          }
        });
      } else {
        if (isChecked) {
          selectedOrgs.add(orgName);
          const allVisibleChecked = orgs.length > 0 && orgs.every((o) => selectedOrgs.has(o.organization));
          if (allVisibleChecked && hideZero) {
            allOrgsSelected = true;
            selectedOrgs.clear();
          }
        } else {
          selectedOrgs.delete(orgName);
        }
        if (allPortfolioOrgs.length > 0 && selectedOrgs.size === allPortfolioOrgs.length) {
          allOrgsSelected = true;
          selectedOrgs.clear();
        }
      }

      populateOrgDropdown(currentPortfolio);
      populateEnvDropdown(currentPortfolio);
      renderSimplifiedDashboard();
    });
  });
}

function populateEnvDropdown(portfolio) {
  const envSelect = document.getElementById('filter-env');
  if (!envSelect || !portfolio) return;

  const hideZero = Boolean(document.getElementById('chk-hide-zero')?.checked);
  const prevEnv = envSelect.value || 'ALL';
  const envs = (portfolio.environments || []).filter((e) => {
    if (!isOrgSelected(e.organization)) return false;
    if (hideZero && (e.totalPdus || 0) <= 0) return false;
    return true;
  });

  const envTotals = new Map();
  envs.forEach((e) => {
    envTotals.set(e.environment, (envTotals.get(e.environment) || 0) + e.totalPdus);
  });

  const sortedEnvNames = Array.from(envTotals.keys()).sort((a, b) =>
    String(a || '').localeCompare(String(b || ''), undefined, {
      sensitivity: 'base',
      numeric: true,
    })
  );

  envSelect.innerHTML =
    `<option value="ALL">All Environments (${sortedEnvNames.length})</option>` +
    sortedEnvNames
      .map(
        (name) =>
          `<option value="${escapeHtml(name)}">${escapeHtml(name)} (${envTotals.get(name)} PDUs)</option>`
      )
      .join('');

  if (sortedEnvNames.includes(prevEnv)) {
    envSelect.value = prevEnv;
  } else {
    envSelect.value = 'ALL';
  }
}

function getNormalizedExcludedOrgs(portfolio) {
  if (!portfolio) return [];
  const byOrg = new Map();
  const details = Array.isArray(portfolio.excludedBillingDetails) ? portfolio.excludedBillingDetails : [];
  details.forEach((item) => {
    if (!item || typeof item !== 'object') return;
    const name = String(item.organization || '').trim();
    if (!name) return;
    byOrg.set(name, {
      organization: name,
      billingType: String(item.billingType || 'NON_SUBSCRIPTION').toUpperCase(),
      subscriptionPlan: String(item.subscriptionPlan || ''),
      runtimeType: String(item.runtimeType || ''),
    });
  });
  const names = Array.isArray(portfolio.excludedBillingOrganizations) ? portfolio.excludedBillingOrganizations : [];
  names.forEach((rawName) => {
    const name = String(rawName || '').trim();
    if (name && !byOrg.has(name)) {
      byOrg.set(name, {
        organization: name,
        billingType: 'PAYG / TRIAL',
        subscriptionPlan: '',
        runtimeType: '',
      });
    }
  });
  return Array.from(byOrg.values()).sort((a, b) =>
    a.organization.localeCompare(b.organization, undefined, { sensitivity: 'base', numeric: true })
  );
}

function renderScopeAndAnalyticsBanners(portfolio, baseMatchingEnvs) {
  const warnHeaderBtn = document.getElementById('btn-excluded-orgs-warning');
  const warnCountBadge = document.getElementById('excluded-orgs-badge-count');
  const modalIncludedEl = document.getElementById('excluded-orgs-modal-included');
  const modalSummaryEl = document.getElementById('excluded-orgs-modal-summary');
  const modalListEl = document.getElementById('excluded-orgs-modal-list');

  const excludedList = getNormalizedExcludedOrgs(portfolio);
  const subOrgs = Array.isArray(portfolio?.organizations) ? portfolio.organizations : [];

  if (warnHeaderBtn) {
    if (excludedList.length > 0) {
      const countsByType = new Map();
      excludedList.forEach((item) => {
        const label = item.billingType === 'EVALUATION' ? 'TRIAL' : item.billingType;
        countsByType.set(label, (countsByType.get(label) || 0) + 1);
      });
      const typeSummary = Array.from(countsByType.entries())
        .map(([k, c]) => `${c} ${k}`)
        .join(', ');

      const subCountsByPlan = new Map();
      subOrgs.forEach((o) => {
        const plan = String(o.subscriptionPlan || 'SUBSCRIPTION').trim() || 'SUBSCRIPTION';
        subCountsByPlan.set(plan, (subCountsByPlan.get(plan) || 0) + 1);
      });
      const subPlanSummary = Array.from(subCountsByPlan.entries())
        .map(([k, c]) => `${c} ${k}`)
        .join(', ');

      if (warnCountBadge) {
        warnCountBadge.textContent = String(excludedList.length);
      }
      warnHeaderBtn.title = `${excludedList.length} Excluded Organization(s) (${typeSummary}) — Click to view Scope`;
      warnHeaderBtn.setAttribute(
        'aria-label',
        `View ${excludedList.length} excluded non-subscription organizations`
      );
      warnHeaderBtn.style.display = 'inline-flex';

      if (modalIncludedEl) {
        modalIncludedEl.textContent = subPlanSummary
          ? `Included in Entitlement Audit: ${subOrgs.length} Subscription organization(s) (${subPlanSummary}).`
          : `Included in Entitlement Audit: ${subOrgs.length} Subscription organization(s).`;
      }
      if (modalSummaryEl) {
        modalSummaryEl.textContent = `Excluded from Entitlement Audit: ${excludedList.length} Non-Subscription organization(s) (${typeSummary}) — Trial and Pay-As-You-Go organizations are excluded:`;
      }
      if (modalListEl) {
        modalListEl.replaceChildren();
        excludedList.forEach((item) => {
          const chip = document.createElement('span');
          chip.className = 'excluded-org-chip';
          const nameSpan = document.createElement('span');
          nameSpan.textContent = item.organization;
          const typeSpan = document.createElement('span');
          typeSpan.className = 'excluded-org-type';
          typeSpan.textContent = item.billingType === 'EVALUATION' ? 'TRIAL' : item.billingType;
          chip.append(nameSpan, typeSpan);
          modalListEl.appendChild(chip);
        });
      }
    } else {
      warnHeaderBtn.style.display = 'none';
    }
  }

  const warnBanner = document.getElementById('dashboard-analytics-warning');
  const warnBadge = warnBanner?.querySelector('.analytics-warning-badge');
  const warnText = document.getElementById('analytics-warning-text');
  const warnChips = document.getElementById('analytics-warning-chips');

  if (warnBanner && warnText && warnChips) {
    const envsToCheck = Array.isArray(baseMatchingEnvs) ? baseMatchingEnvs : portfolio?.environments || [];
    const disabledEnvs = envsToCheck.filter(
      (e) => e.callsStatus === 'ANALYTICS_DISABLED' || e.callsSource === 'ANALYTICS_DISABLED'
    );
    const degradedEnvs = envsToCheck.filter(
      (e) => e.callsStatus === 'PARTIAL' || e.callsStatus === 'ERROR'
    );
    const allWarnEnvs = [...disabledEnvs, ...degradedEnvs];

    if (allWarnEnvs.length > 0) {
      currentAnalyticsWarnKeys = allWarnEnvs.map(
        (e) => `${e.organization}/${e.environment}:${e.callsStatus || e.callsSource}`
      );

      const allDismissed = currentAnalyticsWarnKeys.every((k) => dismissedAnalyticsWarnSet.has(k));
      if (allDismissed) {
        warnBanner.style.display = 'none';
        return;
      }

      if (warnBadge) {
        warnBadge.textContent =
          disabledEnvs.length > 0 && degradedEnvs.length > 0
            ? 'NO ANALYTICS & PARTIAL'
            : disabledEnvs.length > 0
            ? 'NO ANALYTICS'
            : 'ANALYTICS PARTIAL / ERROR';
      }

      const msgParts = [];
      if (disabledEnvs.length > 0) {
        msgParts.push(
          `Apigee Analytics add-on is disabled on ${disabledEnvs.length} environment(s) — PDUs and Environment Units are counted, while API calls are 0 (no Cloud Monitoring fallback)`
        );
      }
      if (degradedEnvs.length > 0) {
        msgParts.push(
          `Analytics call queries returned PARTIAL/ERROR on ${degradedEnvs.length} environment(s)`
        );
      }
      warnText.textContent = `${msgParts.join(' • ')}:`;

      warnChips.replaceChildren();
      allWarnEnvs.forEach((e) => {
        const chip = document.createElement('span');
        chip.className = 'analytics-warning-chip';
        const statusLabel =
          e.callsStatus === 'ANALYTICS_DISABLED' || e.callsSource === 'ANALYTICS_DISABLED'
            ? 'NO ANALYTICS'
            : String(e.callsStatus || 'PARTIAL');
        chip.textContent = `${e.organization} / ${e.environment} (${e.totalPdus ?? 0} PDUs • ${statusLabel})`;
        if (Array.isArray(e.trafficErrors) && e.trafficErrors.length > 0) {
          chip.title = e.trafficErrors.join(' | ');
        }
        warnChips.appendChild(chip);
      });
      warnBanner.style.display = 'flex';
    } else {
      currentAnalyticsWarnKeys = [];
      warnBanner.style.display = 'none';
    }
  }
}

function renderSimplifiedDashboard() {
  if (!currentPortfolio || !currentPortfolio.summary) return;
  const mismatchKey = `${getStoredJurisdiction()}:${currentPortfolio.summary.contractStartDate || ''}`;
  if (
    currentPortfolio.callsPeriodMismatch &&
    !window.__refreshingMismatchJur &&
    !attemptedMismatchKeys.has(mismatchKey)
  ) {
    attemptedMismatchKeys.add(mismatchKey);
    window.__refreshingMismatchJur = true;
    fetchMonthlyCallsSeries(true).finally(() => {
      window.__refreshingMismatchJur = false;
    });
  }

  const summary = currentPortfolio.summary;
  const limitPdu = summary.entitlementPdu || 500;
  const limitEnvs = summary.entitlementEnvs || 6;
  const limitCalls = summary.entitlementYearlyCalls || 1_500_000_000;
  const contractStartDate = summary.contractStartDate || '2026-01-01';
  const contractPeriodStart = summary.contractPeriodStart || contractStartDate;
  const contractPeriodEnd = summary.contractPeriodEnd || '';
  const daysElapsed = summary.daysElapsedInContract || 1;
  const daysInPeriod = summary.daysInContractPeriod || 365;

  const includeSf = Boolean(currentPortfolio.settings?.includeSharedFlows);
  const totalOrgsCount = summary.totalOrganizations || 0;

  const acct =
    currentPortfolio.auth?.activeAccount ||
    currentPortfolio.account ||
    activeSessionEmail ||
    'Authenticated';
  document.getElementById('header-account-subtitle').textContent = `User: ${acct}`;

  const jurBadge = document.getElementById('header-jurisdiction-badge');
  if (jurBadge) {
    const jurCode = currentPortfolio.controlPlaneJurisdiction || (currentPortfolio.jurisdiction && currentPortfolio.jurisdiction.code) || getStoredJurisdiction();
    const jurInfo = currentPortfolio.jurisdiction || JURISDICTION_ENDPOINTS[jurCode] || { name: jurCode.toUpperCase(), endpoint: '' };
    jurBadge.textContent = `Control Plane: ${jurInfo.name || jurCode.toUpperCase()}`;
    if (jurInfo.endpoint) {
      jurBadge.title = `Endpoint: ${jurInfo.endpoint}`;
    }
  }

  // Handle Service Account Notice Banner
  const saBanner = document.getElementById('dashboard-sa-notice');
  const isSaFallback = Boolean(currentPortfolio.auth?.isServiceAccountFallback);
  if (saBanner) {
    if (isSaFallback && !getStoredUserToken()) {
      saBanner.style.display = 'flex';
      const saText = document.getElementById('sa-banner-text');
      if (saText) {
        saText.textContent = `Viewing organizations with Cloud Run Service Account. To audit all organizations authorized for your personal user account (${acct}), add your personal user token.`;
      }
    } else {
      saBanner.style.display = 'none';
    }
  }

  // Sync Contract & Entitlement Parameters section inputs

  const pduInput = document.getElementById('input-pdu-limit');
  if (pduInput && document.activeElement !== pduInput) pduInput.value = limitPdu;

  const envInput = document.getElementById('input-env-limit');
  if (envInput && document.activeElement !== envInput) envInput.value = limitEnvs;

  const callsInput = document.getElementById('input-calls-limit');
  if (callsInput && document.activeElement !== callsInput) {
    callsInput.value = Math.round((limitCalls / 1000000) * 100) / 100;
  }

  const contractDateInput = document.getElementById('input-contract-date');
  if (contractDateInput && document.activeElement !== contractDateInput) {
    contractDateInput.value = contractStartDate;
  }

  const periodLbl = document.getElementById('lbl-active-contract-period');
  if (periodLbl) {
    periodLbl.innerHTML = `${escapeHtml(contractPeriodStart)} &rarr; ${escapeHtml(contractPeriodEnd)}`;
  }
  const callsHelpLbl = document.getElementById('lbl-calls-limit-formatted');
  if (callsHelpLbl) {
    callsHelpLbl.textContent = `Max API Calls per contract year in Millions (${formatCompactCalls(limitCalls)})`;
  }

  // Sync top retractable header summary pills
  const pillContractDate = document.getElementById('pill-summary-contract-date');
  if (pillContractDate) pillContractDate.textContent = contractStartDate;
  const pillPduLimit = document.getElementById('pill-summary-pdu-limit');
  if (pillPduLimit) pillPduLimit.textContent = limitPdu.toLocaleString();
  const pillEnvLimit = document.getElementById('pill-summary-env-limit');
  if (pillEnvLimit) pillEnvLimit.textContent = limitEnvs.toLocaleString();
  const pillCallsLimit = document.getElementById('pill-summary-calls-limit');
  if (pillCallsLimit) pillCallsLimit.textContent = formatCompactCalls(limitCalls);

  const chkSf = document.getElementById('chk-include-sf');
  if (chkSf) chkSf.checked = includeSf;

  document.getElementById('th-deployed-units').textContent = includeSf
    ? 'Deployed Units (Proxies + SF)'
    : 'Deployed Proxies';

  const envFilter = document.getElementById('filter-env')?.value || 'ALL';
  const footprintFilter = document.getElementById('filter-footprint')?.value || 'ALL';
  const hideZero = Boolean(document.getElementById('chk-hide-zero')?.checked);

  const exportParams = new URLSearchParams();
  if (!allOrgsSelected) {
    exportParams.set('org', selectedOrgs.size > 0 ? Array.from(selectedOrgs).join(',') : '__NONE__');
  }
  if (envFilter !== 'ALL') exportParams.set('env', envFilter);
  if (footprintFilter !== 'ALL') exportParams.set('runtime', footprintFilter);
  exportParams.set('hideZero', hideZero ? 'true' : 'false');
  exportParams.set('jurisdiction', getStoredJurisdiction());
  const qs = exportParams.toString();
  document.getElementById('btn-export-csv').href = `/api/export/environments${qs ? `?${qs}` : ''}`;

  const baseMatchingEnvs = (currentPortfolio.environments || []).filter((e) => {
    if (!isOrgSelected(e.organization)) return false;
    if (envFilter !== 'ALL' && e.environment !== envFilter) return false;
    if (footprintFilter !== 'ALL' && e.runtimeType !== footprintFilter) return false;
    return true;
  });

  renderScopeAndAnalyticsBanners(currentPortfolio, baseMatchingEnvs);

  const allMatchingEnvs = hideZero
    ? baseMatchingEnvs.filter((e) => (e.totalPdus || 0) > 0)
    : baseMatchingEnvs;

  // 1. PDU metrics for filtered scope
  const consumedPdus = allMatchingEnvs.reduce((sum, e) => sum + e.totalPdus, 0);
  const deployedUnits = allMatchingEnvs.reduce((sum, e) => sum + e.totalArtifacts, 0);
  const matchingOrgsCount = new Set(allMatchingEnvs.map((e) => e.organization)).size;
  const isPduAbove = consumedPdus > limitPdu;
  const diffPdus = Math.abs(limitPdu - consumedPdus);
  const pctPduUsed = limitPdu > 0 ? ((consumedPdus / limitPdu) * 100).toFixed(1) : '0.0';
  const isPduWarn = !isPduAbove && Number(pctPduUsed) >= 85;

  // 2. Environment Units (Env × Regions) metrics for filtered scope (include all matching envs, even 0-PDU)
  const consumedEnvUnits = baseMatchingEnvs.reduce((sum, e) => sum + (e.regionCount || 0), 0);
  const isEnvAbove = consumedEnvUnits > limitEnvs;
  const diffEnvs = Math.abs(limitEnvs - consumedEnvUnits);
  const pctEnvUsed = limitEnvs > 0 ? ((consumedEnvUnits / limitEnvs) * 100).toFixed(1) : '0.0';
  const isEnvWarn = !isEnvAbove && Number(pctEnvUsed) >= 85;

  // 3. Yearly API Calls (Contract Period) metrics for filtered scope (include all matching envs, even 0-PDU)
  const consumedStdCalls = baseMatchingEnvs.reduce((sum, e) => sum + (e.standardApiCalls || 0), 0);
  const consumedEnvoyCalls = baseMatchingEnvs.reduce((sum, e) => sum + (e.envoyApiCalls || 0), 0);
  const consumedExtCalls = baseMatchingEnvs.reduce((sum, e) => sum + (e.extensibleApiCalls || 0), 0);
  const consumedRawCalls = baseMatchingEnvs.reduce(
    (sum, e) =>
      sum +
      (e.rawApiCalls ??
        ((e.standardApiCalls || 0) + (e.extensibleApiCalls || 0))),
    0
  );
  const consumedCalls = baseMatchingEnvs.reduce((sum, e) => sum + (e.yearlyApiCalls || 0), 0);
  const projectedCalls = Math.round(consumedCalls * (daysInPeriod / Math.max(1, daysElapsed)));
  const isCallsAbove = consumedCalls > limitCalls;
  const isCallsProjectedAbove = !isCallsAbove && projectedCalls > limitCalls;
  const diffCalls = Math.abs(limitCalls - consumedCalls);
  const pctCallsUsed = limitCalls > 0 ? ((consumedCalls / limitCalls) * 100).toFixed(1) : '0.0';
  const isCallsWarn = !isCallsAbove && (Number(pctCallsUsed) >= 85 || isCallsProjectedAbove);

  updateCallsBreakdownBanner(
    consumedStdCalls,
    consumedEnvoyCalls,
    consumedExtCalls,
    consumedCalls,
    consumedRawCalls
  );

  const xPdus = allMatchingEnvs
    .filter((e) => e.runtimeType !== 'HYBRID' && e.runtimeType !== 'UNKNOWN')
    .reduce((sum, e) => sum + e.totalPdus, 0);
  const hybridPdus = allMatchingEnvs
    .filter((e) => e.runtimeType === 'HYBRID')
    .reduce((sum, e) => sum + e.totalPdus, 0);
  const activeEnvsCount = allMatchingEnvs.length;

  // Update Card 1 (PDUs)
  const consumedPduEl = document.getElementById('val-consumed-pdus');
  if (consumedPduEl) {
    consumedPduEl.textContent = consumedPdus.toLocaleString();
    consumedPduEl.className = isPduAbove ? 'above' : isPduWarn ? 'warning' : 'below';
  }
  document.getElementById('val-limit-pdus').textContent = limitPdu.toLocaleString();
  document.getElementById('val-consumed-formula').textContent = `${deployedUnits.toLocaleString()} deployed units (${matchingOrgsCount} orgs)`;
  document.getElementById('val-utilization-pct').textContent = `${pctPduUsed}% used`;
  document.getElementById('val-margin-pdus').textContent = isPduAbove
    ? `+${diffPdus.toLocaleString()}`
    : diffPdus.toLocaleString();
  document.getElementById('val-margin-label').textContent = isPduAbove ? 'overage' : 'available';
  const pillPdu = document.getElementById('pill-status-pdu');
  if (pillPdu) {
    pillPdu.className = `chart-status-pill ${isPduAbove ? 'above' : isPduWarn ? 'warning' : 'below'}`;
    pillPdu.textContent = isPduAbove
      ? `ABOVE (+${diffPdus.toLocaleString()})`
      : isPduWarn
      ? `WARNING (${pctPduUsed}%)`
      : 'BELOW LIMIT';
  }

  // Update Card 2 (Environment Units = Env × Regions)
  const consumedEnvEl = document.getElementById('val-consumed-envs');
  if (consumedEnvEl) {
    consumedEnvEl.textContent = consumedEnvUnits.toLocaleString();
    consumedEnvEl.className = isEnvAbove ? 'above' : isEnvWarn ? 'warning' : 'below';
  }
  document.getElementById('val-limit-envs').textContent = limitEnvs.toLocaleString();
  document.getElementById('val-envs-formula').innerHTML = `${consumedEnvUnits.toLocaleString()} regional env units across ${
    baseMatchingEnvs.length
  } logical envs &bull; <strong>${pctEnvUsed}% used</strong> (${
    isEnvAbove ? `+${diffEnvs} overage` : `${diffEnvs} available`
  })`;
  const pillEnvs = document.getElementById('pill-status-envs');
  if (pillEnvs) {
    pillEnvs.className = `chart-status-pill ${isEnvAbove ? 'above' : isEnvWarn ? 'warning' : 'below'}`;
    pillEnvs.textContent = isEnvAbove
      ? `ABOVE (+${diffEnvs})`
      : isEnvWarn
      ? `WARNING (${pctEnvUsed}%)`
      : 'BELOW LIMIT';
  }

  // Update Card 3 (Yearly API Calls over Contract Period)
  const consumedCallsEl = document.getElementById('val-consumed-calls');
  if (consumedCallsEl) {
    consumedCallsEl.textContent = formatCompactCalls(consumedCalls);
    consumedCallsEl.title = `${consumedCalls.toLocaleString()} calls`;
    consumedCallsEl.className = isCallsAbove ? 'above' : isCallsWarn ? 'warning' : 'below';
  }
  document.getElementById('val-limit-calls').textContent = formatCompactCalls(limitCalls);
  const chipStd = document.getElementById('chip-calls-std');
  if (chipStd) {
    chipStd.textContent = `Std: ${formatCompactCalls(consumedStdCalls)}`;
    chipStd.title = `Standard Calls: ${consumedStdCalls.toLocaleString()} (0.2× weighting)`;
  }
  const chipEnvoy = document.getElementById('chip-calls-envoy');
  if (chipEnvoy) {
    chipEnvoy.textContent = `Envoy: ${formatCompactCalls(consumedEnvoyCalls)}`;
    chipEnvoy.title = `Envoy Adapter Calls: ${consumedEnvoyCalls.toLocaleString()} (Free / 0× weighting)`;
  }
  const chipExt = document.getElementById('chip-calls-ext');
  if (chipExt) {
    chipExt.textContent = `Ext: ${formatCompactCalls(consumedExtCalls)}`;
    chipExt.title = `Extensible Calls: ${consumedExtCalls.toLocaleString()} (1.0× weighting)`;
  }
  const chipRaw = document.getElementById('chip-calls-raw');
  if (chipRaw) {
    chipRaw.textContent = `Raw: ${formatCompactCalls(consumedRawCalls)}`;
    chipRaw.title = `Raw Runtime API Calls (Ext + Std, excl. Envoy): ${consumedRawCalls.toLocaleString()}`;
  }

  const lightUi = isLightTheme();
  const projWarnColor = lightUi ? '#b45309' : '#fbbf24';
  document.getElementById('val-calls-formula').innerHTML = `Period: <code>${escapeHtml(
    contractPeriodStart
  )} &rarr; ${escapeHtml(contractPeriodEnd)}</code> &bull; <strong>${pctCallsUsed}% used</strong> &bull; Projected: <strong style="color:${
    projectedCalls > limitCalls ? projWarnColor : 'inherit'
  };">${escapeHtml(formatCompactCalls(projectedCalls))}/yr</strong>`;
  const pillCalls = document.getElementById('pill-status-calls');
  if (pillCalls) {
    pillCalls.className = `chart-status-pill ${isCallsAbove ? 'above' : isCallsWarn ? 'warning' : 'below'}`;
    pillCalls.textContent = isCallsAbove
      ? `ABOVE (+${formatCompactCalls(diffCalls)})`
      : isCallsProjectedAbove
      ? `PACE > LIMIT (${formatCompactCalls(projectedCalls)}/yr)`
      : isCallsWarn
      ? `WARNING (${pctCallsUsed}%)`
      : 'BELOW LIMIT';
  }

  // Update Scope label
  const scopeEl = document.getElementById('verdict-scope-label');
  const scopeParts = [];
  if (!allOrgsSelected) {
    scopeParts.push(`Orgs: ${getSelectedOrgsLabel(totalOrgsCount)}`);
  }
  if (envFilter !== 'ALL') scopeParts.push(`Env: ${envFilter}`);
  if (footprintFilter !== 'ALL') scopeParts.push(footprintFilter === 'HYBRID' ? 'Apigee Hybrid' : 'Apigee X');
  if (!hideZero) scopeParts.push('Incl. 0-PDU Envs');
  const errorOrgsCount = (currentPortfolio.organizations || []).filter(
    (o) => isOrgSelected(o.organization) && o.status === 'ERROR'
  ).length;
  if (errorOrgsCount > 0) {
    scopeParts.push(`⚠ ${errorOrgsCount} Org Error(s)`);
  }
  scopeEl.textContent = scopeParts.length
    ? `Filtered Scope (${scopeParts.join(' • ')})`
    : `All Authorized Organizations (${totalOrgsCount} Orgs)`;

  // Update 3-Dimension Summary Legend in the Right Card
  const colorForState = (above, warn) => {
    if (lightUi) {
      return above ? '#dc2626' : warn ? '#b45309' : '#059669';
    }
    return above ? '#f87171' : warn ? '#fbbf24' : '#34d399';
  };
  const legPdu = document.getElementById('legend-pdu-summary');
  if (legPdu) {
    legPdu.innerHTML = `<span style="color:${colorForState(isPduAbove, isPduWarn)}">${consumedPdus.toLocaleString()} / ${limitPdu.toLocaleString()} (${pctPduUsed}%)</span>`;
  }
  const legEnv = document.getElementById('legend-env-summary');
  if (legEnv) {
    legEnv.innerHTML = `<span style="color:${colorForState(isEnvAbove, isEnvWarn)}">${consumedEnvUnits.toLocaleString()} / ${limitEnvs.toLocaleString()} (${pctEnvUsed}%)</span>`;
  }
  const legCalls = document.getElementById('legend-calls-summary');
  if (legCalls) {
    legCalls.innerHTML = `<span style="color:${colorForState(isCallsAbove, isCallsWarn)}">${formatCompactCalls(
      consumedCalls
    )} / ${formatCompactCalls(limitCalls)} (${pctCallsUsed}%)</span>`;
  }

  const elX = document.getElementById('legend-x-pdus');
  const elHyb = document.getElementById('legend-hybrid-pdus');
  const elEnvs = document.getElementById('legend-active-envs');
  if (elX) elX.textContent = xPdus.toLocaleString();
  if (elHyb) elHyb.textContent = hybridPdus.toLocaleString();
  if (elEnvs) elEnvs.textContent = activeEnvsCount.toLocaleString();

  const elStdCalls = document.getElementById('legend-std-calls');
  const elExtCalls = document.getElementById('legend-ext-calls');
  const elEnvoyCalls = document.getElementById('legend-envoy-calls');
  const legCallsBreakdown = document.getElementById('legend-calls-breakdown-summary');
  if (elExtCalls) {
    elExtCalls.textContent = `Ext.: ${formatCompactCalls(consumedExtCalls)} (1.0×)`;
    elExtCalls.title = `${consumedExtCalls.toLocaleString()} extensible calls`;
  }
  if (elStdCalls) {
    elStdCalls.textContent = `Std.: ${formatCompactCalls(consumedStdCalls)} (0.2×)`;
    elStdCalls.title = `${consumedStdCalls.toLocaleString()} standard calls`;
  }
  if (elEnvoyCalls) {
    elEnvoyCalls.textContent = `Envoy: ${formatCompactCalls(consumedEnvoyCalls)} (Free)`;
    elEnvoyCalls.title = `${consumedEnvoyCalls.toLocaleString()} Envoy Adapter calls (Free / 0× weighting, excluded from entitlement)`;
  }
  if (legCallsBreakdown) {
    const stdEnt = Math.round(consumedStdCalls * 0.2);
    legCallsBreakdown.title = `Extensible: ${consumedExtCalls.toLocaleString()} calls (1.0×) • Standard: ${consumedStdCalls.toLocaleString()} calls (0.2× = ${stdEnt.toLocaleString()} ent. calls) • Entitlement Sum: ${(stdEnt + consumedExtCalls).toLocaleString()} (Envoy Free)`;
  }

  // Determine active Gauge Dimension metrics
  let gaugeConsumed = consumedPdus;
  let gaugeLimit = limitPdu;
  let gaugePct = pctPduUsed;
  let gaugeAbove = isPduAbove;
  let gaugeWarn = isPduWarn;
  let gaugeSubLabel = `${consumedPdus.toLocaleString()} / ${limitPdu.toLocaleString()} PDUs`;

  if (selectedGaugeDimension === 'environments') {
    gaugeConsumed = consumedEnvUnits;
    gaugeLimit = limitEnvs;
    gaugePct = pctEnvUsed;
    gaugeAbove = isEnvAbove;
    gaugeWarn = isEnvWarn;
    gaugeSubLabel = `${consumedEnvUnits.toLocaleString()} / ${limitEnvs.toLocaleString()} Env Units`;
  } else if (selectedGaugeDimension === 'calls') {
    gaugeConsumed = consumedCalls;
    gaugeLimit = limitCalls;
    gaugePct = pctCallsUsed;
    gaugeAbove = isCallsAbove;
    gaugeWarn = isCallsWarn;
    gaugeSubLabel = `${formatCompactCalls(consumedCalls)} / ${formatCompactCalls(limitCalls)} Calls`;
  }

  const anyAboveLimit = isPduAbove || isEnvAbove || isCallsAbove;
  const anyWarnLimit = !anyAboveLimit && (isPduWarn || isEnvWarn || isCallsWarn);
  const cardEl = document.getElementById('limit-validation-card');
  const badgeEl = document.getElementById('verdict-badge');
  const progressFillEl = document.getElementById('limit-progress-fill');

  if (anyAboveLimit) {
    cardEl.className = 'overview-bento above-limit';
    badgeEl.className = 'verdict-pill above';
    const breached = [];
    if (isPduAbove) breached.push('PDU');
    if (isEnvAbove) breached.push('ENV');
    if (isCallsAbove) breached.push('CALLS');
    badgeEl.textContent = `ABOVE LIMIT (${breached.join(' + ')})`;
  } else if (anyWarnLimit) {
    cardEl.className = 'overview-bento warning-limit';
    badgeEl.className = 'verdict-pill warning';
    const atRisk = [];
    if (isPduWarn) atRisk.push('PDU');
    if (isEnvWarn) atRisk.push('ENV');
    if (isCallsWarn) atRisk.push('CALLS');
    badgeEl.textContent = `OVERAGE RISK (${atRisk.join(' + ')})`;
  } else {
    cardEl.className = 'overview-bento below-limit';
    badgeEl.className = 'verdict-pill below';
    badgeEl.textContent = 'ALL 3 DIMENSIONS BELOW LIMIT';
  }

  if (progressFillEl) {
    progressFillEl.className = gaugeAbove
      ? 'limit-progress-fill above'
      : gaugeWarn
      ? 'limit-progress-fill warning'
      : 'limit-progress-fill';
    progressFillEl.style.width = `${Math.min(100, Number(gaugePct))}%`;
  }

  // Update Overview Donut Ring Gauge
  const donutRing = document.getElementById('donut-progress-ring');
  const donutPctText = document.getElementById('donut-pct-text');
  const donutSubText = document.getElementById('donut-sub-text');
  if (donutRing) {
    const circumference = 2 * Math.PI * 64; // 402.12
    const clampedRatio = Math.min(1, Math.max(0, gaugeConsumed / Math.max(1, gaugeLimit)));
    donutRing.style.strokeDashoffset = (circumference * (1 - clampedRatio)).toFixed(2);
    donutRing.setAttribute('stroke', colorForState(gaugeAbove, gaugeWarn));
  }
  if (donutPctText) donutPctText.textContent = `${gaugePct}%`;
  if (donutSubText) donutSubText.textContent = gaugeSubLabel;

  // Render Detail Report Table
  const visibleEnvs = [...allMatchingEnvs];
  const hiddenZeroCount = Math.max(0, baseMatchingEnvs.length - visibleEnvs.length);

  const reportSubEl = document.getElementById('table-report-subtitle');
  if (reportSubEl) {
    reportSubEl.textContent = hideZero
      ? `This list exclusively includes environments that feature proxy deployments (${visibleEnvs.length} active environments shown, ${hiddenZeroCount} 0-PDU environments hidden).`
      : `Showing all ${visibleEnvs.length} environments matching current filter (including ${baseMatchingEnvs.filter((e) => (e.totalPdus || 0) <= 0).length} environments with 0 PDUs).`;
  }

  visibleEnvs.sort(
    (a, b) =>
      String(a.organization || '').localeCompare(String(b.organization || '')) ||
      String(a.environment || '').localeCompare(String(b.environment || ''))
  );

  const tbody = document.getElementById('tbody-pdu');
  const tfoot = document.getElementById('tfoot-pdu');

  if (!visibleEnvs.length) {
    tbody.innerHTML = `
      <tr>
        <td colspan="14" style="text-align: center; padding: 1.5rem; color: var(--text-muted);">
          No environments match the current filter${hideZero ? ' (or all have 0 PDUs)' : ''}.
        </td>
      </tr>
    `;
  } else {
    tbody.innerHTML = visibleEnvs
      .map((e) => {
        const regionsHtml = (e.regions || [])
          .map((r) => `<span class="region-tag">${escapeHtml(r)}</span>`)
          .join('');
        const cautionBadgeHtml = e.regionCaution
          ? ` <span class="region-caution-badge" title="${escapeHtml(e.regionCaution)}" style="color:#f59e0b;cursor:help;font-size:0.75rem;font-weight:600;margin-left:0.25rem;white-space:nowrap;">&#9888;&#65039; Caution</span>`
          : '';
        const regionCount = Number(e.regionCount || 0);
        const envLimitPct = limitEnvs > 0 ? ((regionCount / limitEnvs) * 100).toFixed(2) : '0.00';
        const pduLimitPct = limitPdu > 0 ? ((e.totalPdus / limitPdu) * 100).toFixed(2) : '0.00';
        const unitsLabel = includeSf
          ? `${e.totalArtifacts} <span style="color:var(--text-muted);font-size:0.74rem;">(${e.totalProxies}P + ${e.sharedFlows}SF)</span>`
          : `${e.totalProxies}`;
        const stdCalls = Number(e.standardApiCalls || 0);
        const envoyCalls = Number(e.envoyApiCalls || 0);
        const extCalls = Number(e.extensibleApiCalls || 0);
        const envCalls = Number(e.yearlyApiCalls || 0);
        const callLimitPct = limitCalls > 0 ? ((envCalls / limitCalls) * 100).toFixed(2) : '0.00';
        const callsStatus = String(e.callsStatus || 'OK');
        const callsSource = String(e.callsSource || '');
        const errTooltip = escapeHtml((e.trafficErrors || []).join(' | ') || 'Apigee Analytics query issue');
        let callsBadgeHtml = '';
        if (callsSource === 'ANALYTICS_DISABLED' || callsStatus === 'ANALYTICS_DISABLED') {
          callsBadgeHtml = ' <span class="calls-status-badge disabled" title="Apigee Analytics add-on is not enabled on this environment">NO ANALYTICS</span>';
        } else if (callsStatus === 'PARTIAL') {
          callsBadgeHtml = ` <span class="calls-status-badge partial" title="${errTooltip}">PARTIAL</span>`;
        } else if (callsStatus === 'ERROR') {
          callsBadgeHtml = ` <span class="calls-status-badge error" title="${errTooltip}">ERROR</span>`;
        }

        return `
          <tr>
            <td class="mono-cell"><strong>${escapeHtml(e.organization)}</strong></td>
            <td class="footprint-col">${formatFootprint(e.runtimeType)}</td>
            <td class="mono-cell">${escapeHtml(e.environment)}</td>
            <td>${regionsHtml || '<span style="color:var(--text-muted);">Unattached (0 regions)</span>'}${cautionBadgeHtml}</td>
            <td class="num-cell" title="${e.regionCaution ? escapeHtml(e.regionCaution) : ''}">${regionCount}</td>
            <td class="num-cell">${envLimitPct}%</td>
            <td class="num-cell">${unitsLabel}</td>
            <td class="num-cell"><strong>${e.totalPdus.toLocaleString()}</strong></td>
            <td class="num-cell">${pduLimitPct}%</td>
            <td class="num-cell" title="${extCalls.toLocaleString()} extensible calls">${extCalls.toLocaleString()} <span style="color:var(--text-muted);font-size:0.74rem;">(${formatCompactCalls(extCalls)})</span></td>
            <td class="num-cell" title="${stdCalls.toLocaleString()} standard calls">${stdCalls.toLocaleString()} <span style="color:var(--text-muted);font-size:0.74rem;">(${formatCompactCalls(stdCalls)})</span></td>
            <td class="num-cell" title="${envCalls.toLocaleString()} entitlement calls (weighted)"><strong>${envCalls.toLocaleString()}</strong> <span style="color:var(--text-muted);font-size:0.74rem;">(${formatCompactCalls(envCalls)})</span>${callsBadgeHtml}</td>
            <td class="num-cell" title="${envoyCalls.toLocaleString()} Envoy Adapter calls (Free / 0× weighting)">${envoyCalls.toLocaleString()} <span style="color:var(--text-muted);font-size:0.74rem;">(${formatCompactCalls(envoyCalls)})</span></td>
            <td class="num-cell">${callLimitPct}%</td>
          </tr>
        `;
      })
      .join('');
  }

  const visibleUnits = visibleEnvs.reduce((s, e) => s + e.totalArtifacts, 0);
  const visiblePdus = visibleEnvs.reduce((s, e) => s + (e.totalPdus || 0), 0);
  const visibleEnvUnits = visibleEnvs.reduce((s, e) => s + (e.regionCount || 0), 0);
  const visibleStdCalls = visibleEnvs.reduce((s, e) => s + (e.standardApiCalls || 0), 0);
  const visibleEnvoyCalls = visibleEnvs.reduce((s, e) => s + (e.envoyApiCalls || 0), 0);
  const visibleExtCalls = visibleEnvs.reduce((s, e) => s + (e.extensibleApiCalls || 0), 0);
  const visibleCalls = visibleEnvs.reduce((s, e) => s + (e.yearlyApiCalls || 0), 0);
  const visibleEnvPct = limitEnvs > 0 ? ((visibleEnvUnits / limitEnvs) * 100).toFixed(1) : '0.0';
  const visiblePduPct = limitPdu > 0 ? ((visiblePdus / limitPdu) * 100).toFixed(1) : '0.0';
  const visibleCallsPct = limitCalls > 0 ? ((visibleCalls / limitCalls) * 100).toFixed(1) : '0.0';

  tfoot.innerHTML = `
    <tr>
      <td colspan="4">TOTAL (${visibleEnvs.length} Environments Displayed)</td>
      <td class="num-cell">${visibleEnvUnits.toLocaleString()} / ${limitEnvs.toLocaleString()}</td>
      <td class="num-cell">${visibleEnvPct}%</td>
      <td class="num-cell">${visibleUnits.toLocaleString()} units</td>
      <td class="num-cell">${visiblePdus.toLocaleString()} PDUs</td>
      <td class="num-cell">${visiblePduPct}%</td>
      <td class="num-cell" title="${visibleExtCalls.toLocaleString()} extensible calls">${formatCompactCalls(visibleExtCalls)}</td>
      <td class="num-cell" title="${visibleStdCalls.toLocaleString()} standard calls">${formatCompactCalls(visibleStdCalls)}</td>
      <td class="num-cell" title="${visibleCalls.toLocaleString()} entitlement calls">${formatCompactCalls(visibleCalls)} / ${formatCompactCalls(limitCalls)}</td>
      <td class="num-cell" title="${visibleEnvoyCalls.toLocaleString()} Envoy Adapter calls">${formatCompactCalls(visibleEnvoyCalls)}</td>
      <td class="num-cell">${visibleCallsPct}%</td>
    </tr>
  `;

  if (currentMonitoringTs) {
    renderMonitoringChart();
  }
  if (currentCallsTs) {
    renderMonthlyCallsChart();
  }
}

async function updateSettings(payload, toastMsg) {
  try {
    const jur = getStoredJurisdiction();
    const res = await apiFetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ jurisdiction: jur, ...payload }),
    });

    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    currentPortfolio = await res.json();
    populateOrgDropdown(currentPortfolio);
    populateEnvDropdown(currentPortfolio);
    renderSimplifiedDashboard();
    if (toastMsg) showToast(toastMsg);
  } catch (err) {
    showToast(`Failed to update settings: ${err.message}`);
  }
}

function setActiveDimensionUI(dim) {
  selectedGaugeDimension = dim;
  document.querySelectorAll('.dimension-card').forEach((card) => {
    const isActive = card.getAttribute('data-dim') === dim;
    card.classList.toggle('active', isActive);
    card.setAttribute('aria-pressed', isActive ? 'true' : 'false');
  });
  document.querySelectorAll('.gauge-dim-btn').forEach((btn) => {
    const isActive = btn.getAttribute('data-gauge-dim') === dim;
    btn.classList.toggle('active', isActive);
    btn.setAttribute('aria-pressed', isActive ? 'true' : 'false');
  });
  renderSimplifiedDashboard();
}

document.addEventListener('DOMContentLoaded', () => {
  purgeLegacyBrowserTokens();
  try {
    const savedTheme = localStorage.getItem('apigee_pdu_theme');
    if (savedTheme === 'light' || savedTheme === 'dark') {
      applyTheme(savedTheme);
    }
  } catch (_) {}

  document.querySelectorAll('.theme-toggle-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const nextTheme = isLightTheme() ? 'dark' : 'light';
      applyTheme(nextTheme);
      showToast(`Switched to ${nextTheme === 'light' ? 'Light' : 'Dark'} Theme`);
    });
  });

  initAuthScreen();

  // Retractable Contract & Entitlement Parameters Section at top of window
  const entitlementSection = document.getElementById('section-entitlement-config');
  const entitlementToggle = document.getElementById('entitlement-config-toggle');
  const entitlementToggleLbl = document.getElementById('lbl-entitlement-toggle');

  const setEntitlementCollapsed = (collapsed) => {
    if (!entitlementSection || !entitlementToggle) return;
    entitlementSection.classList.toggle('is-collapsed', collapsed);
    entitlementToggle.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
    if (entitlementToggleLbl) {
      entitlementToggleLbl.textContent = collapsed ? 'Edit Parameters' : 'Retract';
    }
  };

  entitlementToggle?.addEventListener('click', () => {
    const isCurrentlyCollapsed = entitlementSection?.classList.contains('is-collapsed');
    setEntitlementCollapsed(!isCurrentlyCollapsed);
  });

  entitlementToggle?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      const isCurrentlyCollapsed = entitlementSection?.classList.contains('is-collapsed');
      setEntitlementCollapsed(!isCurrentlyCollapsed);
    }
  });

  document.querySelector('a.dock-btn[href="#section-entitlement-config"]')?.addEventListener('click', () => {
    setEntitlementCollapsed(false);
  });

  // Multi-select Organization Dropdown Toggle & Outside Click
  const dropdownWrapper = document.getElementById('org-dropdown-wrapper');
  const dropdownBtn = document.getElementById('org-dropdown-btn');

  dropdownBtn?.addEventListener('click', (e) => {
    e.stopPropagation();
    const isOpen = dropdownWrapper.classList.toggle('open');
    dropdownBtn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
  });

  document.getElementById('org-dropdown-menu')?.addEventListener('click', (e) => {
    e.stopPropagation();
  });

  document.addEventListener('click', () => {
    if (dropdownWrapper?.classList.contains('open')) {
      dropdownWrapper.classList.remove('open');
      dropdownBtn?.setAttribute('aria-expanded', 'false');
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && dropdownWrapper?.classList.contains('open')) {
      dropdownWrapper.classList.remove('open');
      dropdownBtn?.setAttribute('aria-expanded', 'false');
    }
  });

  // Dimension Cards & Gauge Switcher Pills
  const activateDimensionCard = (card) => {
    const dim = card.getAttribute('data-dim') || 'pdu';
    setActiveDimensionUI(dim);
    if (dim === 'pdu' || dim === 'environments') {
      document.querySelectorAll('.dim-btn').forEach((b) => {
        b.classList.toggle('active', b.getAttribute('data-chart-dim') === dim);
      });
      fetchMonitoringTimeSeries(selectedMonitoringDays, false, dim);
    }
  };

  document.querySelectorAll('.dimension-card').forEach((card) => {
    card.addEventListener('click', () => activateDimensionCard(card));
    card.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        activateDimensionCard(card);
      } else if (e.key === ' ') {
        e.preventDefault();
      }
    });
    card.addEventListener('keyup', (e) => {
      if (e.key === ' ') {
        activateDimensionCard(card);
      }
    });
  });

  document.querySelectorAll('.gauge-dim-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const dim = btn.getAttribute('data-gauge-dim') || 'pdu';
      setActiveDimensionUI(dim);
    });
  });

  // Cloud Monitoring Chart Dimension Switcher Buttons (PDUs | Env Units)
  document.querySelectorAll('.dim-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.dim-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      const dim = btn.getAttribute('data-chart-dim') || 'pdu';
      setActiveDimensionUI(dim);
      fetchMonitoringTimeSeries(selectedMonitoringDays, false, dim);
    });
  });

  // Authenticate / Change Account Button Handler
  const handleAuthOrSwitch = async () => {
    const btn = document.getElementById('btn-authenticate-account') || document.getElementById('btn-google-oauth-login');
    const errBox = document.getElementById('login-error-box');
    if (errBox) errBox.style.display = 'none';

    let origHtml = 'Authenticate / Change Account';
    if (btn) {
      origHtml = btn.innerHTML;
      btn.disabled = true;
      btn.textContent = 'Redirecting...';
    }

    try {
      const res = await apiFetch('/api/auth/oidc/auth-url', { method: 'POST' });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        if (data.needsTokenModal) {
          const modal = document.getElementById('modal-token-manager');
          if (modal) modal.style.display = 'grid';
        }
        throw new Error(data.error || data.detail || `HTTP ${res.status}`);
      }
      if (data.authUrl) {
        window.location.href = data.authUrl;
        return;
      }
      throw new Error('Failed to retrieve authentication URL.');
    } catch (err) {
      if (errBox) {
        errBox.textContent = `Authentication Notice: ${err.message}`;
        errBox.style.display = 'block';
      }
      if (btn) {
        btn.disabled = false;
        btn.innerHTML = origHtml;
      }
    }
  };

  document.getElementById('btn-authenticate-account')?.addEventListener('click', handleAuthOrSwitch);
  document.getElementById('btn-google-oauth-login')?.addEventListener('click', handleAuthOrSwitch);

  // Enter Dashboard Button Handler
  const handleEnterDashboard = async () => {
    const btn = document.getElementById('btn-enter-app');
    if (btn && btn.classList.contains('btn-frozen')) {
      showToast('Authentication is mandatory to enter the dashboard.');
      return;
    }
    if (btn) {
      btn.disabled = true;
    }
    const errBox = document.getElementById('login-error-box');
    if (errBox) errBox.style.display = 'none';

    try {
      await apiFetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          account: activeSessionEmail || undefined,
          triggerBrowserLogin: false,
          jurisdiction: getStoredJurisdiction(),
        }),
      });
    } catch (_) {}
    await openDashboardForAccount(activeSessionEmail, true);
  };

  document.getElementById('btn-enter-app')?.addEventListener('click', handleEnterDashboard);
  document.getElementById('btn-continue-active-account')?.addEventListener('click', handleEnterDashboard);

  // Banner Add Token Button
  document.getElementById('btn-banner-add-token')?.addEventListener('click', () => {
    const modal = document.getElementById('modal-token-manager');
    if (modal) {
      modal.style.display = 'grid';
      const input = document.getElementById('input-modal-user-token');
      if (input) {
        input.value = '';
        input.focus();
      }
      const msg = document.getElementById('modal-token-msg');
      if (msg) msg.style.display = 'none';
    }
  });

  // Return to Login / Jurisdiction & Account Switcher (preserving active session for jurisdiction switch)
  document.getElementById('btn-switch-account')?.addEventListener('click', () => {
    hideTrafficErrorModal();
    const scopeDlg = document.getElementById('modal-excluded-orgs');
    if (scopeDlg?.open) scopeDlg.close();
    currentPortfolio = null;
    allOrgsSelected = true;
    selectedOrgs.clear();
    currentMonitoringTs = null;
    currentCallsTs = null;
    document.getElementById('dashboard-screen').style.display = 'none';
    document.getElementById('login-screen').style.display = '';
    initAuthScreen();
  });

  // Save Contract & Entitlement Parameters
  let isSavingEntitlements = false;
  const saveEntitlementsHandler = async () => {
    if (isSavingEntitlements) return;
    const pduVal = Number(document.getElementById('input-pdu-limit')?.value);
    const envVal = Number(document.getElementById('input-env-limit')?.value);
    const callsValMillion = Number(document.getElementById('input-calls-limit')?.value);
    const callsVal = callsValMillion > 0 ? Math.round(callsValMillion * 1000000) : 0;
    const contractDateVal = (document.getElementById('input-contract-date')?.value || '').trim();

    if (!pduVal || pduVal < 1 || !envVal || envVal < 1 || !callsVal || callsVal < 1) {
      showToast('Enter valid positive numbers for PDU, Environment, and Yearly Call limits (Calls in Millions)');
      return;
    }

    const prevContractDate = currentPortfolio?.summary?.contractStartDate;
    const contractChanged = Boolean(contractDateVal && contractDateVal !== prevContractDate);

    isSavingEntitlements = true;
    const saveBtn = document.getElementById('btn-apply-limit');
    if (saveBtn) {
      saveBtn.disabled = true;
      saveBtn.classList.add('is-refreshing');
      saveBtn.textContent = contractChanged ? 'Updating Contract & Analytics...' : 'Saving...';
    }
    try {
      const saveTargetLabel = currentPortfolio?.auth?.isCloudRun ? 'session settings' : 'pdu_limit.json';
      await updateSettings(
        {
          entitlementPdu: pduVal,
          entitlementEnvs: envVal,
          entitlementYearlyCalls: callsVal,
          contractStartDate: contractDateVal || null,
        },
        `Saved parameters (${pduVal.toLocaleString()} PDUs, ${envVal} Envs, ${formatCompactCalls(
          callsVal
        )} Calls, Start: ${contractDateVal}) to ${saveTargetLabel}`
      );
      if (contractChanged) {
        await fetchMonthlyCallsSeries(false);
      } else if (currentCallsTs) {
        renderMonthlyCallsChart();
      }
    } finally {
      isSavingEntitlements = false;
      if (saveBtn) {
        saveBtn.disabled = false;
        saveBtn.classList.remove('is-refreshing');
        saveBtn.textContent = 'Save Parameters';
      }
    }
  };

  document.getElementById('btn-apply-limit')?.addEventListener('click', saveEntitlementsHandler);

  ['input-pdu-limit', 'input-env-limit', 'input-calls-limit', 'input-contract-date'].forEach((id) => {
    const el = document.getElementById(id);
    el?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.isComposing) {
        saveEntitlementsHandler();
      }
    });
    if (id !== 'input-contract-date') {
      el?.addEventListener('change', () => {
        saveEntitlementsHandler();
      });
    }
  });

  document.getElementById('input-calls-limit')?.addEventListener('input', (e) => {
    const val = Number(e.target.value);
    const callsHelpLbl = document.getElementById('lbl-calls-limit-formatted');
    if (callsHelpLbl) {
      if (val > 0) {
        const rawCalls = Math.round(val * 1000000);
        callsHelpLbl.textContent = `Max API Calls per contract year (${formatCompactCalls(rawCalls)} = ${rawCalls.toLocaleString()} calls)`;
      } else {
        callsHelpLbl.textContent = 'Enter Yearly API Calls in Millions (e.g. 100 for 100M calls)';
      }
    }
  });

  // Refresh Live Scan
  document.getElementById('btn-refresh-scan')?.addEventListener('click', async () => {
    const btn = document.getElementById('btn-refresh-scan');
    const dProgress = document.getElementById('dashboard-scan-progress');
    const dPercent = document.getElementById('dashboard-progress-percent');
    const dFill = document.getElementById('dashboard-progress-bar-fill');
    const dStatus = document.getElementById('dashboard-progress-status');
    const dDetail = document.getElementById('dashboard-progress-detail');

    btn.disabled = true;
    btn.classList.add('is-refreshing');
    btn.setAttribute('title', 'Refreshing live scan in progress...');
    showScanProgressModal('Discovering Apigee organizations...', 'Querying Google Cloud Apigee API...', 5);

    function updateDashboardProgress(evt) {
      updateScanProgressModal(evt);
      const pct = Math.max(0, Math.min(100, evt.percent || 0));
      if (dPercent) dPercent.textContent = `${pct}%`;
      if (dFill) dFill.style.width = `${pct}%`;
      if (dStatus) dStatus.textContent = evt.message || 'Discovering Apigee organizations...';
      if (dDetail) dDetail.textContent = evt.organization || evt.detail || '';
    }

    try {
      const pduVal = Number(document.getElementById('input-pdu-limit')?.value);
      const envVal = Number(document.getElementById('input-env-limit')?.value);
      const callsValMillion = Number(document.getElementById('input-calls-limit')?.value);
      const callsVal = callsValMillion > 0 ? Math.round(callsValMillion * 1000000) : undefined;
      const contractDateVal = (document.getElementById('input-contract-date')?.value || '').trim();

      await apiFetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          jurisdiction: getStoredJurisdiction(),
          entitlementPdu: pduVal >= 1 ? pduVal : undefined,
          entitlementEnvs: envVal >= 1 ? envVal : undefined,
          entitlementYearlyCalls: callsVal >= 1 ? callsVal : undefined,
          contractStartDate: contractDateVal || undefined,
        }),
      });

      updateDashboardProgress({ percent: 5, message: 'Discovering Apigee organizations...', detail: 'Querying Google Cloud Apigee API...' });
      currentPortfolio = await startLiveScanWithProgress({
        account: null,
        jurisdiction: getStoredJurisdiction(),
        onProgress: updateDashboardProgress,
      });

      currentCallsTs = null;
      currentMonitoringTs = null;
      if (currentPortfolio?._prefetchedCallsTs) {
        currentCallsTs = currentPortfolio._prefetchedCallsTs;
      } else {
        await fetchMonthlyCallsSeries(false, { skipRender: true });
      }

      if (
        currentPortfolio?._prefetchedPduTs &&
        selectedChartDimension === 'pdu' &&
        selectedMonitoringDays === 14
      ) {
        currentMonitoringTs = currentPortfolio._prefetchedPduTs;
      } else {
        await fetchMonitoringTimeSeries(selectedMonitoringDays, true, selectedChartDimension, {
          skipRender: true,
        });
      }

      allOrgsSelected = true;
      selectedOrgs.clear();
      dismissedAnalyticsWarnSet.clear();
      populateOrgDropdown(currentPortfolio);
      populateEnvDropdown(currentPortfolio);
      renderSimplifiedDashboard();
      updateScanProgressModal({ percent: 100, message: 'Scan Complete!', detail: 'Live scan finished' });
      await new Promise((r) => setTimeout(r, 250));
      showToast(
        `Refreshed live scan (${currentPortfolio.summary.totalPdus.toLocaleString()} PDUs, ${
          currentPortfolio.summary.totalEnvironmentUnits
        } Env Units, ${formatCompactCalls(currentPortfolio.summary.totalYearlyApiCalls)} Calls)`
      );
      if (Array.isArray(currentPortfolio?.trafficErrors) && currentPortfolio.trafficErrors.length > 0) {
        showTrafficErrorModal(currentPortfolio.trafficErrors, () => {
          document.getElementById('btn-refresh-scan')?.click();
        });
      }
    } catch (err) {
      showToast(`Scan failed: ${err.message}`);
    } finally {
      hideScanProgressModal();
      if (dProgress) {
        setTimeout(() => {
          dProgress.style.display = 'none';
        }, 1200);
      }
      btn.classList.remove('is-refreshing');
      btn.disabled = false;
      btn.setAttribute('title', 'Refresh Live Scan');
    }
  });

  // Scope & Excluded Organizations Popup Dialog + Safari light-dismiss fallback
  const scopeDialog = document.getElementById('modal-excluded-orgs');
  document.getElementById('btn-excluded-orgs-warning')?.addEventListener('click', () => {
    if (scopeDialog && typeof scopeDialog.showModal === 'function' && !scopeDialog.open) {
      scopeDialog.showModal();
    }
  });

  if (scopeDialog && typeof HTMLDialogElement !== 'undefined' && !('closedBy' in HTMLDialogElement.prototype)) {
    scopeDialog.addEventListener('click', (event) => {
      if (event.target !== scopeDialog) return;
      const rect = scopeDialog.getBoundingClientRect();
      const isDialogContent =
        rect.top <= event.clientY &&
        event.clientY <= rect.top + rect.height &&
        rect.left <= event.clientX &&
        event.clientX <= rect.left + rect.width;
      if (!isDialogContent) {
        scopeDialog.close();
      }
    });
  }

  // Close "NO ANALYTICS" warning box
  document.getElementById('btn-close-analytics-warning')?.addEventListener('click', () => {
    currentAnalyticsWarnKeys.forEach((k) => dismissedAnalyticsWarnSet.add(k));
    const warnBanner = document.getElementById('dashboard-analytics-warning');
    if (warnBanner) warnBanner.style.display = 'none';
  });

  // Organization Dropdown & Filter Controls
  document.getElementById('btn-org-select-all')?.addEventListener('click', () => {
    allOrgsSelected = true;
    selectedOrgs.clear();
    populateOrgDropdown(currentPortfolio);
    populateEnvDropdown(currentPortfolio);
    renderSimplifiedDashboard();
  });

  document.getElementById('btn-org-clear')?.addEventListener('click', () => {
    allOrgsSelected = false;
    selectedOrgs.clear();
    populateOrgDropdown(currentPortfolio);
    populateEnvDropdown(currentPortfolio);
    renderSimplifiedDashboard();
  });

  document.getElementById('filter-env')?.addEventListener('change', () => {
    renderSimplifiedDashboard();
  });

  document.getElementById('filter-footprint')?.addEventListener('change', () => {
    renderSimplifiedDashboard();
  });

  document.getElementById('chk-hide-zero')?.addEventListener('change', (e) => {
    populateOrgDropdown(currentPortfolio);
    populateEnvDropdown(currentPortfolio);
    renderSimplifiedDashboard();
    showToast(
      e.target.checked
        ? 'Hiding 0-PDU environments across dashboard & filters'
        : 'Showing all environments (including 0-PDU environments)'
    );
  });

  document.getElementById('chk-include-sf')?.addEventListener('change', (e) => {
    updateSettings(
      { includeSharedFlows: e.target.checked },
      e.target.checked ? 'Included Shared Flows in PDU count' : 'Excluded Shared Flows from PDU count'
    );
  });

  document.getElementById('btn-reset-filters')?.addEventListener('click', () => {
    allOrgsSelected = true;
    selectedOrgs.clear();
    const hideZeroEl = document.getElementById('chk-hide-zero');
    if (hideZeroEl) hideZeroEl.checked = false;
    populateOrgDropdown(currentPortfolio);
    populateEnvDropdown(currentPortfolio);
    document.getElementById('filter-env').value = 'ALL';
    document.getElementById('filter-footprint').value = 'ALL';
    renderSimplifiedDashboard();
  });

  // Time Range Selector
  document.querySelectorAll('.range-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.range-btn').forEach((b) => b.classList.remove('active'));
      btn.classList.add('active');
      const days = Number(btn.getAttribute('data-days')) || 14;
      fetchMonitoringTimeSeries(days, false, selectedChartDimension);
    });
  });

  // Refresh Monitoring Chart
  document.getElementById('btn-refresh-chart')?.addEventListener('click', async () => {
    const btn = document.getElementById('btn-refresh-chart');
    if (btn) {
      btn.disabled = true;
      btn.classList.add('is-refreshing');
      btn.textContent = 'Refreshing...';
    }
    try {
      await fetchMonitoringTimeSeries(selectedMonitoringDays, true, selectedChartDimension);
      showToast('Refreshed Cloud Monitoring time-series');
    } finally {
      if (btn) {
        btn.classList.remove('is-refreshing');
        btn.disabled = false;
        btn.textContent = 'Refresh Monitoring';
      }
    }
  });

  // Refresh Monthly API Calls Chart
  document.getElementById('btn-refresh-calls-chart')?.addEventListener('click', async () => {
    const btn = document.getElementById('btn-refresh-calls-chart');
    if (btn) {
      btn.disabled = true;
      btn.classList.add('is-refreshing');
      btn.textContent = 'Refreshing Analytics...';
    }
    try {
      await fetchMonthlyCallsSeries(true);
      showToast('Refreshed monthly API calls from Apigee Analytics');
    } finally {
      if (btn) {
        btn.classList.remove('is-refreshing');
        btn.disabled = false;
        btn.textContent = 'Refresh Analytics Calls';
      }
    }
  });

  // Clickable weight info icons in Calls Breakdown Banner
  const closeAllCallInfoPopovers = () => {
    document.querySelectorAll('.call-info-popover').forEach((pop) => {
      pop.hidden = true;
    });
    document.querySelectorAll('.call-info-btn').forEach((b) => {
      b.setAttribute('aria-expanded', 'false');
    });
  };

  document.getElementById('calls-breakdown-banner')?.addEventListener('click', (e) => {
    const btn = e.target.closest('.call-info-btn');
    if (!btn) return;
    e.stopPropagation();
    const targetId = btn.getAttribute('data-info-target');
    const pop = targetId ? document.getElementById(targetId) : null;
    if (!pop) return;
    const isOpening = pop.hidden;
    closeAllCallInfoPopovers();
    if (isOpening) {
      pop.hidden = false;
      btn.setAttribute('aria-expanded', 'true');
    }
  });

  document.addEventListener('click', (e) => {
    if (!e.target.closest('.call-info-wrap')) {
      closeAllCallInfoPopovers();
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeAllCallInfoPopovers();
      const tokModal = document.getElementById('modal-token-manager');
      if (tokModal && tokModal.style.display !== 'none') {
        tokModal.style.display = 'none';
      }
      const trafficModal = document.getElementById('modal-traffic-error');
      if (trafficModal && trafficModal.style.display !== 'none') {
        hideTrafficErrorModal();
      }
    }
  });

  // Copy command helpers
  const copyCmdHandler = (btnId) => {
    const btn = document.getElementById(btnId);
    if (!btn) return;
    const textToCopy = 'gcloud auth application-default print-access-token';
    navigator.clipboard.writeText(textToCopy).then(() => {
      const orig = btn.textContent;
      btn.textContent = 'Copied!';
      setTimeout(() => {
        btn.textContent = orig;
      }, 2000);
    }).catch(() => {
      prompt('Copy token command:', textToCopy);
    });
  };

  document.getElementById('btn-copy-modal-token-cmd')?.addEventListener('click', () => copyCmdHandler('btn-copy-modal-token-cmd'));

  // Apply User Token (Modal)
  const submitModalTokenHandler = async () => {
    const input = document.getElementById('input-modal-user-token');
    const msg = document.getElementById('modal-token-msg');
    const ok = await applyUserToken(input?.value, msg);
    if (!ok) return;
    if (input) input.value = '';
    const dashboardActive = document.getElementById('dashboard-screen')?.style.display !== 'none';
    if (dashboardActive) {
      const modal = document.getElementById('modal-token-manager');
      if (modal) modal.style.display = 'none';
      if (msg) msg.style.display = 'none';
      document.getElementById('btn-refresh-scan')?.click();
    } else {
      setTimeout(() => {
        const modal = document.getElementById('modal-token-manager');
        if (modal) modal.style.display = 'none';
        if (msg) msg.style.display = 'none';
      }, 800);
    }
  };

  document.getElementById('btn-apply-modal-token')?.addEventListener('click', submitModalTokenHandler);

  document.getElementById('input-modal-user-token')?.addEventListener('keydown', async (e) => {
    if (e.key === 'Enter' && !e.isComposing) {
      await submitModalTokenHandler();
    }
  });

  document.getElementById('btn-close-token-modal')?.addEventListener('click', () => {
    const modal = document.getElementById('modal-token-manager');
    if (modal) modal.style.display = 'none';
  });

  document.getElementById('modal-token-manager')?.addEventListener('click', (e) => {
    if (e.target.id === 'modal-token-manager') {
      e.target.style.display = 'none';
    }
  });

  document.getElementById('btn-close-traffic-error-modal')?.addEventListener('click', hideTrafficErrorModal);
  document.getElementById('btn-dismiss-traffic-error')?.addEventListener('click', hideTrafficErrorModal);
  document.getElementById('modal-traffic-error')?.addEventListener('click', (e) => {
    if (e.target.id === 'modal-traffic-error') {
      hideTrafficErrorModal();
    }
  });
  document.getElementById('btn-retry-traffic-scan')?.addEventListener('click', () => {
    const retryFn = pendingTrafficRetryHandler;
    hideTrafficErrorModal();
    if (typeof retryFn === 'function') {
      retryFn();
    } else {
      document.getElementById('btn-refresh-scan')?.click();
    }
  });

  window.addEventListener('resize', () => {

    if (currentMonitoringTs) {
      renderMonitoringChart();
    }
    if (currentCallsTs) {
      renderMonthlyCallsChart();
    }
  });
});
