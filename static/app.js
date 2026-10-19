/**
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
  if (mode === 'light') {
    document.body.setAttribute('data-theme', 'light');
  } else {
    document.body.removeAttribute('data-theme');
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
    .replace(/"/g, '&quot;');
}

function formatFootprint(runtimeType) {
  if (runtimeType === 'HYBRID') {
    return '<span class="badge badge-hybrid">HYBRID</span>';
  }
  return '<span class="badge badge-x">APIGEE X</span>';
}

function getStoredUserToken() {
  try {
    return sessionStorage.getItem('apigee_user_access_token') || localStorage.getItem('apigee_user_access_token') || '';
  } catch (_) {
    return '';
  }
}

function setStoredUserToken(token) {
  if (token && token.trim()) {
    try { sessionStorage.setItem('apigee_user_access_token', token.trim()); } catch (_) {}
    try { localStorage.setItem('apigee_user_access_token', token.trim()); } catch (_) {}
  } else {
    try { sessionStorage.removeItem('apigee_user_access_token'); } catch (_) {}
    try { localStorage.removeItem('apigee_user_access_token'); } catch (_) {}
  }
}

async function apiFetch(url, options = {}) {
  const token = getStoredUserToken();
  const opts = { ...options };
  opts.headers = { ...(options.headers || {}) };
  if (token && !opts.headers['Authorization']) {
    opts.headers['Authorization'] = `Bearer ${token}`;
  }
  return fetch(url, opts);
}

let activeSessionEmail = '';
let activeHasUserToken = false;

function updateTokenStatusPill(hasToken, label = null) {
  const pill = document.getElementById('user-token-pill');
  const tokenSection = document.getElementById('user-token-section');
  const headerBadge = document.getElementById('header-token-badge');
  const enterBtn = document.getElementById('btn-enter-app') || document.getElementById('btn-continue-active-account');

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


  if (headerBadge) {
    if (hasToken) {
      headerBadge.textContent = 'User Token Active';
      headerBadge.className = 'token-pill token-pill-active';
      headerBadge.style.display = 'inline-flex';
    } else {
      headerBadge.textContent = 'Service Account Mode';
      headerBadge.className = 'token-pill token-pill-required';
      headerBadge.style.display = 'inline-flex';
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
    if (info.activeAccount) {
      activeSessionEmail = info.activeAccount;
      const enterBtnText = document.getElementById('btn-enter-app-text');
      if (enterBtnText) {
        enterBtnText.innerHTML = `Enter Dashboard (${escapeHtml(info.activeAccount)}) &rarr;`;
      }
    }
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
  const info = JURISDICTION_ENDPOINTS[jurCode] || { endpoint: `https://${jurCode}-apigee.googleapis.com/v1` };
  if (descEl) {
    descEl.innerHTML = `Endpoint: <code>${info.endpoint}</code>`;
  }
}

function showScanProgressModal(title = 'Scanning Apigee Entitlements...', detail = 'Connecting to APIs...', percent = 0) {
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
  if (detailEl && evt.detail !== undefined) detailEl.textContent = evt.detail;
  if (percentEl) percentEl.textContent = `${pct}%`;
  if (fillEl) fillEl.style.width = `${pct}%`;
}

function hideScanProgressModal() {
  const modal = document.getElementById('modal-scan-progress');
  if (modal) {
    modal.style.display = 'none';
  }
}

async function initAuthScreen() {
  const enterBtn = document.getElementById('btn-enter-app');
  const enterBtnText = document.getElementById('btn-enter-app-text');
  const authBtn = document.getElementById('btn-authenticate-account');
  const authBtnText = document.getElementById('btn-authenticate-text');
  const errBox = document.getElementById('login-error-box');
  const jurSelect = document.getElementById('select-control-plane');

  activeSessionEmail = '';

  // Wire jurisdiction selector
  const activeJur = getStoredJurisdiction();
  if (jurSelect) {
    jurSelect.value = activeJur;
    updateJurisdictionHint(activeJur);
    if (!jurSelect.dataset.listenerBound) {
      jurSelect.dataset.listenerBound = 'true';
      jurSelect.addEventListener('change', async (e) => {
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
  if (urlParams.get('oidc_error')) {
    if (errBox) {
      errBox.textContent = `Authentication Notice: ${decodeURIComponent(urlParams.get('oidc_error'))}`;
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

    if (auth.jurisdiction && auth.jurisdiction.code && jurSelect) {
      jurSelect.value = auth.jurisdiction.code;
      setStoredJurisdiction(auth.jurisdiction.code);
      updateJurisdictionHint(auth.jurisdiction.code);
    }

    const isTokenExpired = Boolean(auth.tokenExpired);
    const isTokenValid = Boolean(auth.tokenValid && auth.hasUserToken && !isTokenExpired);

    updateTokenStatusPill(isTokenValid);

    // Rule: If authentication is required, freeze "Enter Dashboard" button.
    // When logged in, button displays: "Enter Dashboard (google id) →"
    if (!isTokenValid) {
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
        authBtnText.textContent = 'Authenticate / Change Account';
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
  }
}


async function startLiveScanWithProgress({
  account = null,
  jurisdiction = null,
  onProgress = null,
} = {}) {
  const token = getStoredUserToken();
  const jur = jurisdiction || getStoredJurisdiction();
  const url = `/api/scan/stream?account=${encodeURIComponent(account || '')}&jurisdiction=${encodeURIComponent(jur)}`;
  const headers = {};
  if (token) {
    headers['Authorization'] = `Bearer ${token}`;
  }
  const response = await fetch(url, { headers });
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
      try {
        const event = JSON.parse(jsonStr);
        if (event.done) {
          if (event.stage === 'ERROR') {
            throw new Error(event.error || event.message);
          }
          return event.portfolio;
        }
        if (onProgress) onProgress(event);
      } catch (err) {
        if (err.message && !err.message.includes('JSON')) throw err;
      }
    }
  }
  throw new Error('Scan stream ended unexpectedly without completion payload');
}

async function openDashboardForAccount(account, runLiveScan = true) {
  if (!account && !activeSessionEmail) return;

  const errBox = document.getElementById('login-error-box');
  const enterBtn = document.getElementById('btn-enter-app');

  if (enterBtn && enterBtn.classList.contains('btn-frozen')) {
    return;
  }

  if (errBox) errBox.style.display = 'none';

  // Immediately transition to Home Page / Dashboard screen
  document.getElementById('login-screen').style.display = 'none';
  document.getElementById('dashboard-screen').style.display = 'flex';

  // Display scan progress modal popup on the home page
  showScanProgressModal('Connecting to Apigee APIs...', 'Initializing scan session', 5);

  try {
    if (runLiveScan) {
      showScanProgressModal('Connecting to Google Cloud Apigee APIs...', 'Scanning organizations and environments', 10);
      try {
        currentPortfolio = await startLiveScanWithProgress({
          account: account || null,
          jurisdiction: getStoredJurisdiction(),
          onProgress: updateScanProgressModal,
        });
      } catch (scanErr) {
        console.warn('Live scan encountered an issue, attempting cached fallback:', scanErr);
        const targetJur = getStoredJurisdiction();
        const res = await apiFetch(`/api/portfolio?jurisdiction=${encodeURIComponent(targetJur)}`);
        if (!res.ok) throw scanErr;
        currentPortfolio = await res.json();
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
        try {
          currentPortfolio = await startLiveScanWithProgress({
            account: account || null,
            jurisdiction: targetJur,
            onProgress: updateScanProgressModal,
          });
        } catch (scanErr) {
          console.warn('Initial live scan fallback encountered an issue, continuing with cached portfolio:', scanErr);
        }
      }
    }

    updateScanProgressModal({ percent: 90, message: 'Finalizing compliance metrics...', detail: 'Preparing dashboard views' });

    // Always reset org selection so organizations from previously selected regions do NOT linger
    allOrgsSelected = true;
    selectedOrgs.clear();

    // Populate dashboard UI
    populateOrgDropdown(currentPortfolio);
    populateEnvDropdown(currentPortfolio);
    renderSimplifiedDashboard();

    updateScanProgressModal({ percent: 100, message: 'Dashboard Ready!', detail: 'Launching view' });

    // Smooth transition: brief delay before dismissing modal popup
    await new Promise((resolve) => setTimeout(resolve, 300));
    hideScanProgressModal();

    fetchMonitoringTimeSeries(selectedMonitoringDays, runLiveScan, selectedChartDimension);
    fetchMonthlyCallsSeries(runLiveScan);
  } catch (err) {
    hideScanProgressModal();
    // Return back to login screen with error notice
    document.getElementById('dashboard-screen').style.display = 'none';
    document.getElementById('login-screen').style.display = 'flex';
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

async function fetchMonitoringTimeSeries(days = 14, refresh = false, dimension = selectedChartDimension) {
  selectedMonitoringDays = days;
  selectedChartDimension = dimension === 'environments' ? 'environments' : 'pdu';

  const container = document.getElementById('chart-container');
  const subtitle = document.getElementById('chart-subtitle');
  const mainTitle = document.getElementById('chart-main-title');

  if (mainTitle) {
    mainTitle.textContent =
      selectedChartDimension === 'environments'
        ? 'Environment Units Across Time (Cloud Monitoring)'
        : 'PDUs Across Time (Cloud Monitoring)';
  }

  if (container && (!currentMonitoringTs || refresh || currentMonitoringTs.dimension !== selectedChartDimension)) {
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
    currentMonitoringTs = await res.json();
    renderMonitoringChart();
  } catch (err) {
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
  if (!container || !currentMonitoringTs) return;

  const dim = currentMonitoringTs.dimension === 'environments' ? 'environments' : 'pdu';
  const envFilter = document.getElementById('filter-env')?.value || 'ALL';
  const footprintFilter = document.getElementById('filter-footprint')?.value || 'ALL';
  const hideZero = document.getElementById('chk-hide-zero')?.checked ?? true;

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

  // Filter series by active Multi-Select Orgs / Env / Footprint / Hide 0-PDU Envs
  const matchingSeries = (currentMonitoringTs.series || []).filter((s) => {
    if (!isOrgSelected(s.organization)) return false;
    if (envFilter !== 'ALL' && s.environment !== envFilter) return false;
    if (footprintFilter !== 'ALL' && s.runtimeType !== footprintFilter) return false;
    if (hideZero && zeroPduEnvKeys.has(`${s.organization}::${s.environment}`)) return false;
    return true;
  });

  // 1. Determine alignment resolution (bucket interval in ms)
  const alignSeconds =
    currentMonitoringTs.alignmentPeriodSeconds ||
    (selectedChartDays <= 1
      ? 3600
      : selectedChartDays <= 7
      ? 14400
      : selectedChartDays <= 14
      ? 21600
      : 43200);
  const bucketMs = alignSeconds * 1000;

  // 2. Gather all valid epoch timestamps across matching series
  const allEpochs = [];
  matchingSeries.forEach((s) => {
    (s.points || []).forEach((pt) => {
      const t = new Date(pt.timestamp).getTime();
      if (!isNaN(t)) allEpochs.push(t);
    });
  });

  if (!allEpochs.length) {
    container.innerHTML = `<div class="chart-empty">No Cloud Monitoring telemetry found for the selected filter (${escapeHtml(
      unitLabel
    )}).</div>`;
    if (subtitle) {
      subtitle.innerHTML = `Metric: <code>apigee.googleapis.com/proxy/details</code> &bull; 0 active series for current filter`;
    }
    return;
  }

  // 3. Create regular canonical time buckets from min to max
  const minEpoch = Math.floor(Math.min(...allEpochs) / bucketMs) * bucketMs;
  const maxEpoch = Math.floor(Math.max(...allEpochs) / bucketMs) * bucketMs;

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
      // If the series starts within 2 buckets of minEpoch, use first point's value as baseline
      let currentVal = firstTime - minEpoch <= 2 * bucketMs ? firstVal : 0;

      buckets.forEach((bucketTime, bIdx) => {
        while (ptIdx < rawPts.length && rawPts[ptIdx].time <= bucketTime + bucketMs / 2) {
          currentVal = rawPts[ptIdx].val;
          ptIdx++;
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

  // Drop trailing bucket if it's a partial final alignment window with fewer reporting orgs
  if (points.length > 3) {
    const prevVal = points[points.length - 2].pdu;
    const lastVal = points[points.length - 1].pdu;
    if (lastVal < prevVal * 0.7) {
      points = points.slice(0, points.length - 1);
    }
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
    subtitle.innerHTML = `Metric: <code>apigee.googleapis.com/proxy/details</code> &bull; <strong>${escapeHtml(
      scopeDesc
    )}</strong> &bull; Min: <strong>${formatVal(minVal)}</strong> | Max: <strong>${formatVal(
      maxVal
    )}</strong> | Latest: <strong style="color:${
      isLatestAbove ? '#f87171' : '#34d399'
    };">${formatVal(latestVal)} ${unitLabel}</strong>`;
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

  const linePath = coords
    .map((c, i) => `${i === 0 ? 'M' : 'L'}${c.x.toFixed(1)},${c.y.toFixed(1)}`)
    .join(' ');
  const areaPath = `${linePath} L${coords[coords.length - 1].x.toFixed(1)},${(padTop + plotH).toFixed(1)} L${coords[0].x.toFixed(1)},${(padTop + plotH).toFixed(1)} Z`;

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
      <path d="${areaPath}" fill="url(#pduAreaGrad)" mask="url(#pduAreaMask)" />
      <path d="${linePath}" fill="none" stroke="url(#pduStrokeGrad)" stroke-width="2.75" stroke-linejoin="round" stroke-linecap="round" />
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

function updateCallsBreakdownBanner(stdCalls, extCalls, weightedCalls, rawCalls) {
  const std = Number(stdCalls || 0);
  const ext = Number(extCalls || 0);
  const stdEntitlement = Math.round(std * 0.2);
  const extEntitlement = Math.round(ext * 1.0);
  const entitlementSum = stdEntitlement + extEntitlement;
  const weighted = (std > 0 || ext > 0) ? entitlementSum : Number(weightedCalls ?? entitlementSum);
  const raw = (std > 0 || ext > 0) ? (std + ext) : Number(rawCalls ?? (std + ext));

  const stdEl = document.getElementById('banner-calls-std');
  if (stdEl) {
    stdEl.textContent = `${std.toLocaleString()} (${formatCompactCalls(std)})`;
    stdEl.title = `${std.toLocaleString()} standard calls`;
  }
  const stdWeightedEl = document.getElementById('banner-calls-std-weighted');
  if (stdWeightedEl) {
    stdWeightedEl.innerHTML = `&rarr; <strong>${stdEntitlement.toLocaleString()}</strong> ent. calls (0.2&times;)`;
  }

  const extEl = document.getElementById('banner-calls-ext');
  if (extEl) {
    extEl.textContent = `${ext.toLocaleString()} (${formatCompactCalls(ext)})`;
    extEl.title = `${ext.toLocaleString()} extensible calls`;
  }
  const extWeightedEl = document.getElementById('banner-calls-ext-weighted');
  if (extWeightedEl) {
    extWeightedEl.innerHTML = `&rarr; <strong>${extEntitlement.toLocaleString()}</strong> ent. calls (1.0&times;)`;
  }

  const totalWeightedEl = document.getElementById('banner-calls-total-weighted');
  if (totalWeightedEl) {
    totalWeightedEl.textContent = `${weighted.toLocaleString()} (${formatCompactCalls(weighted)})`;
    totalWeightedEl.title = `Entitlement Calls = ${ext.toLocaleString()} Extensible + (${std.toLocaleString()} Standard × 0.2 = ${stdEntitlement.toLocaleString()}) = ${weighted.toLocaleString()}`;
  }
  const rawTotalEl = document.getElementById('banner-calls-raw-total');
  if (rawTotalEl) {
    rawTotalEl.innerHTML = `- <strong>${formatCompactCalls(extEntitlement)}</strong> Ext + <strong>${formatCompactCalls(stdEntitlement)}</strong> Std Ent. &bull; Raw: ${formatCompactCalls(raw)}`;
    rawTotalEl.title = `Entitlement Sum = Extensible (${ext.toLocaleString()}) + 0.2 × Standard (${std.toLocaleString()} = ${stdEntitlement.toLocaleString()}) = ${weighted.toLocaleString()} | Raw Total: ${raw.toLocaleString()}`;
  }
}

async function fetchMonthlyCallsSeries(refresh = false) {
  const container = document.getElementById('calls-monthly-chart-container');
  const subtitle = document.getElementById('calls-chart-subtitle');
  if (container && (!currentCallsTs || refresh)) {
    container.innerHTML =
      '<div class="chart-empty">Querying Apigee Analytics (14m retention) &amp; Cloud Monitoring for monthly API calls...</div>';
  }

  try {
    const jur = getStoredJurisdiction();
    const res = await apiFetch(`/api/monitoring/timeseries?dimension=calls&refresh=${refresh ? 'true' : 'false'}&jurisdiction=${encodeURIComponent(jur)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    currentCallsTs = await res.json();
    renderMonthlyCallsChart();
  } catch (err) {

    if (container) {
      container.innerHTML = `<div class="chart-empty">Could not load monthly API calls: ${escapeHtml(err.message)}</div>`;
    }
    if (subtitle) {
      subtitle.textContent = 'Error loading monthly API calls';
    }
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

  // Filter series by active Multi-Select Orgs / Env / Footprint
  const matchingSeries = (currentCallsTs.series || []).filter((s) => {
    if (!isOrgSelected(s.organization)) return false;
    if (envFilter !== 'ALL' && s.environment !== envFilter) return false;
    if (footprintFilter !== 'ALL' && s.runtimeType !== footprintFilter) return false;
    return true;
  });

  // Build 12 monthly buckets starting from the Contract Starting Date's month
  const startParts = periodStartStr.split('-').map(Number);
  const startYear = startParts[0] || new Date().getUTCFullYear();
  const startMonth0 = (startParts[1] || 1) - 1; // 0-indexed month

  const monthBuckets = [];
  const monthKeyToIndex = new Map();
  for (let m = 0; m < 12; m++) {
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
      calls: 0,
      cumulativeCalls: 0,
    });
  }

  // Sum daily calls into the 12 contract-year monthly buckets (only on or after periodStartStr)
  matchingSeries.forEach((s) => {
    (s.points || []).forEach((pt) => {
      const dayStr = String(pt.timestamp || '').slice(0, 10);
      if (dayStr < periodStartStr) return;
      const ymKey = dayStr.slice(0, 7);
      const idx = monthKeyToIndex.get(ymKey);
      const val = Number(pt.dailyCalls ?? pt.pdu ?? 0);
      if (idx !== undefined && val > 0) {
        monthBuckets[idx].calls += val;
      }
    });
  });

  // Compute running cumulative total across the 12 months
  let runningTotal = 0;
  monthBuckets.forEach((b) => {
    runningTotal += b.calls;
    b.cumulativeCalls = runningTotal;
  });

  const totalContractCalls = runningTotal;
  const isYearlyAbove = totalContractCalls > yearlyLimit;
  const pctYearly = yearlyLimit > 0 ? ((totalContractCalls / yearlyLimit) * 100).toFixed(1) : '0.0';

  const seriesStdCalls = matchingSeries.reduce((sum, s) => sum + (s.standardApiCalls || 0), 0);
  const seriesExtCalls = matchingSeries.reduce((sum, s) => sum + (s.extensibleApiCalls || 0), 0);
  const seriesEntitlementCalls = Math.round(seriesStdCalls * 0.2 + seriesExtCalls * 1.0);
  const seriesRawCalls = matchingSeries.reduce(
    (sum, s) => sum + (s.rawApiCalls || (s.standardApiCalls || 0) + (s.extensibleApiCalls || 0)),
    0
  );
  updateCallsBreakdownBanner(
    seriesStdCalls,
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

  const maxMonthCalls = Math.max(...monthBuckets.map((b) => b.calls), 0);
  const yMaxRaw = Math.max(maxMonthCalls, monthlyPaceLimit * 1.15, 100);
  const yMax = Math.ceil(yMaxRaw * 1.15);
  const yMin = 0;

  const yForVal = (v) => padTop + plotH - ((v - yMin) / (yMax - yMin)) * plotH;

  const lightMode = isLightTheme();
  const COLOR_GREEN = lightMode ? '#059669' : '#34d399';
  const COLOR_RED = lightMode ? '#dc2626' : '#f87171';
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

  // Render 12 monthly bars
  const slotW = plotW / 12;
  const barW = Math.max(16, Math.min(46, slotW * 0.58));

  const barsHtml = monthBuckets
    .map((b, idx) => {
      const cx = padLeft + idx * slotW + slotW / 2;
      const bx = cx - barW / 2;
      const by = b.calls > 0 ? yForVal(b.calls) : padTop + plotH - 2;
      const bh = Math.max(2, padTop + plotH - by);

      // Bar is red if cumulative calls exceed the yearly limit OR this month's calls exceed the monthly pace
      const isBarAbove = b.cumulativeCalls > yearlyLimit || b.calls > monthlyPaceLimit;
      const barColor = isBarAbove ? COLOR_RED : COLOR_GREEN;
      const barOpacity = b.calls > 0 ? '0.85' : '0.2';

      const valLabel =
        b.calls > 0
          ? `<text x="${cx.toFixed(1)}" y="${Math.max(12, by - 6).toFixed(
              1
            )}" text-anchor="middle" fill="${barColor}" font-family="JetBrains Mono, monospace" font-size="10" font-weight="700">${escapeHtml(
              formatCompactCalls(b.calls)
            )}</text>`
          : '';

      return `
        <g class="month-bar-group" data-month-idx="${idx}">
          <rect
            x="${bx.toFixed(1)}"
            y="${by.toFixed(1)}"
            width="${barW.toFixed(1)}"
            height="${bh.toFixed(1)}"
            rx="6"
            fill="${barColor}"
            fill-opacity="${barOpacity}"
            stroke="${barColor}"
            stroke-width="1"
          />
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
        <div>Monthly Calls: <strong>${b.calls.toLocaleString()} (${formatCompactCalls(b.calls)})</strong></div>
        <div>Cumulative YTD: <strong>${b.cumulativeCalls.toLocaleString()}</strong> / ${formatCompactCalls(yearlyLimit)} &bull; ${statusHtml}</div>
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
  const orgs = [...(portfolio.organizations || [])]
    .filter((o) => !hideZero || (o.totalPdus || 0) > 0)
    .sort((a, b) =>
      String(a.organization || '').localeCompare(String(b.organization || ''), undefined, {
        sensitivity: 'base',
        numeric: true,
      })
    );
  const validOrgSet = new Set(orgs.map((o) => o.organization));

  Array.from(selectedOrgs).forEach((name) => {
    if (!validOrgSet.has(name)) {
      selectedOrgs.delete(name);
    }
  });

  if (!allOrgsSelected && orgs.length > 0 && selectedOrgs.size === orgs.length) {
    allOrgsSelected = true;
    selectedOrgs.clear();
  }

  const activeCount = allOrgsSelected ? orgs.length : selectedOrgs.size;
  if (triggerLabel) {
    triggerLabel.textContent = getSelectedOrgsLabel(orgs.length);
  }
  if (summaryBadge) {
    summaryBadge.textContent = allOrgsSelected
      ? `All Selected (${orgs.length})`
      : `${activeCount} of ${orgs.length} Selected`;
  }

  listContainer.innerHTML = orgs
    .map((o) => {
      const checked = allOrgsSelected || selectedOrgs.has(o.organization);
      return `
        <label class="org-checkbox-item">
          <span class="org-checkbox-left">
            <input
              type="checkbox"
              class="org-checkbox-input"
              value="${escapeHtml(o.organization)}"
              ${checked ? 'checked' : ''}
            />
            <span class="org-checkbox-name" title="${escapeHtml(o.organization)}">${escapeHtml(o.organization)}</span>
          </span>
          <span class="org-checkbox-pdu">${o.totalPdus} PDU</span>
        </label>
      `;
    })
    .join('');

  listContainer.querySelectorAll('.org-checkbox-input').forEach((input) => {
    input.addEventListener('change', (e) => {
      const orgName = e.target.value;
      const isChecked = e.target.checked;

      if (allOrgsSelected) {
        allOrgsSelected = false;
        selectedOrgs.clear();
        orgs.forEach((o) => {
          if (o.organization !== orgName) {
            selectedOrgs.add(o.organization);
          }
        });
      } else {
        if (isChecked) {
          selectedOrgs.add(orgName);
        } else {
          selectedOrgs.delete(orgName);
        }
        if (orgs.length > 0 && selectedOrgs.size === orgs.length) {
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

function renderSimplifiedDashboard() {
  if (!currentPortfolio || !currentPortfolio.summary) return;

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
  document.getElementById('header-account-subtitle').textContent = `User: ${acct} • ${totalOrgsCount} Authorized Orgs`;

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
  const qs = exportParams.toString();
  document.getElementById('btn-export-csv').href = `/api/export/environments${qs ? `?${qs}` : ''}`;

  const baseMatchingEnvs = (currentPortfolio.environments || []).filter((e) => {
    if (!isOrgSelected(e.organization)) return false;
    if (envFilter !== 'ALL' && e.environment !== envFilter) return false;
    if (footprintFilter !== 'ALL' && e.runtimeType !== footprintFilter) return false;
    return true;
  });

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

  // 2. Environment Units (Env × Regions) metrics for filtered scope
  const consumedEnvUnits = allMatchingEnvs.reduce((sum, e) => sum + (e.regionCount || 0), 0);
  const isEnvAbove = consumedEnvUnits > limitEnvs;
  const diffEnvs = Math.abs(limitEnvs - consumedEnvUnits);
  const pctEnvUsed = limitEnvs > 0 ? ((consumedEnvUnits / limitEnvs) * 100).toFixed(1) : '0.0';

  // 3. Yearly API Calls (Contract Period) metrics for filtered scope
  const consumedStdCalls = allMatchingEnvs.reduce((sum, e) => sum + (e.standardApiCalls || 0), 0);
  const consumedExtCalls = allMatchingEnvs.reduce((sum, e) => sum + (e.extensibleApiCalls || 0), 0);
  const consumedRawCalls = allMatchingEnvs.reduce(
    (sum, e) => sum + (e.rawApiCalls ?? ((e.standardApiCalls || 0) + (e.extensibleApiCalls || 0))),
    0
  );
  const consumedCalls = allMatchingEnvs.reduce((sum, e) => sum + (e.yearlyApiCalls || 0), 0);
  const projectedCalls = Math.round(consumedCalls * (daysInPeriod / Math.max(1, daysElapsed)));
  const isCallsAbove = consumedCalls > limitCalls;
  const diffCalls = Math.abs(limitCalls - consumedCalls);
  const pctCallsUsed = limitCalls > 0 ? ((consumedCalls / limitCalls) * 100).toFixed(1) : '0.0';

  updateCallsBreakdownBanner(consumedStdCalls, consumedExtCalls, consumedCalls, consumedRawCalls);

  const xPdus = allMatchingEnvs
    .filter((e) => e.runtimeType !== 'HYBRID')
    .reduce((sum, e) => sum + e.totalPdus, 0);
  const hybridPdus = allMatchingEnvs
    .filter((e) => e.runtimeType === 'HYBRID')
    .reduce((sum, e) => sum + e.totalPdus, 0);
  const activeEnvsCount = allMatchingEnvs.length;

  // Update Card 1 (PDUs)
  const consumedPduEl = document.getElementById('val-consumed-pdus');
  if (consumedPduEl) {
    consumedPduEl.textContent = consumedPdus.toLocaleString();
    consumedPduEl.className = isPduAbove ? 'above' : 'below';
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
    pillPdu.className = `chart-status-pill ${isPduAbove ? 'above' : 'below'}`;
    pillPdu.textContent = isPduAbove ? `ABOVE (+${diffPdus.toLocaleString()})` : 'BELOW LIMIT';
  }

  // Update Card 2 (Environment Units = Env × Regions)
  const consumedEnvEl = document.getElementById('val-consumed-envs');
  if (consumedEnvEl) {
    consumedEnvEl.textContent = consumedEnvUnits.toLocaleString();
    consumedEnvEl.className = isEnvAbove ? 'above' : 'below';
  }
  document.getElementById('val-limit-envs').textContent = limitEnvs.toLocaleString();
  document.getElementById('val-envs-formula').innerHTML = `${consumedEnvUnits.toLocaleString()} regional env units across ${
    allMatchingEnvs.length
  } logical envs &bull; <strong>${pctEnvUsed}% used</strong> (${
    isEnvAbove ? `+${diffEnvs} overage` : `${diffEnvs} available`
  })`;
  const pillEnvs = document.getElementById('pill-status-envs');
  if (pillEnvs) {
    pillEnvs.className = `chart-status-pill ${isEnvAbove ? 'above' : 'below'}`;
    pillEnvs.textContent = isEnvAbove ? `ABOVE (+${diffEnvs})` : 'BELOW LIMIT';
  }

  // Update Card 3 (Yearly API Calls over Contract Period)
  const consumedCallsEl = document.getElementById('val-consumed-calls');
  if (consumedCallsEl) {
    consumedCallsEl.textContent = formatCompactCalls(consumedCalls);
    consumedCallsEl.title = `${consumedCalls.toLocaleString()} calls`;
    consumedCallsEl.className = isCallsAbove ? 'above' : 'below';
  }
  document.getElementById('val-limit-calls').textContent = formatCompactCalls(limitCalls);
  const chipStd = document.getElementById('chip-calls-std');
  if (chipStd) {
    chipStd.textContent = `Std: ${formatCompactCalls(consumedStdCalls)}`;
    chipStd.title = `Standard Calls: ${consumedStdCalls.toLocaleString()} (0.2× weighting)`;
  }
  const chipExt = document.getElementById('chip-calls-ext');
  if (chipExt) {
    chipExt.textContent = `Ext: ${formatCompactCalls(consumedExtCalls)}`;
    chipExt.title = `Extensible Calls: ${consumedExtCalls.toLocaleString()} (1.0× weighting)`;
  }
  const chipRaw = document.getElementById('chip-calls-raw');
  if (chipRaw) {
    chipRaw.textContent = `Raw: ${formatCompactCalls(consumedRawCalls)}`;
    chipRaw.title = `Raw API Calls: ${consumedRawCalls.toLocaleString()}`;
  }

  document.getElementById('val-calls-formula').innerHTML = `Period: <code>${escapeHtml(
    contractPeriodStart
  )} &rarr; ${escapeHtml(contractPeriodEnd)}</code> (${daysElapsed}d elapsed) &bull; <strong>${pctCallsUsed}% used</strong> &bull; Run-rate: <strong>${formatCompactCalls(
    projectedCalls
  )}/yr</strong>`;
  const pillCalls = document.getElementById('pill-status-calls');
  if (pillCalls) {
    pillCalls.className = `chart-status-pill ${isCallsAbove ? 'above' : 'below'}`;
    pillCalls.textContent = isCallsAbove
      ? `ABOVE (+${formatCompactCalls(diffCalls)})`
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
  scopeEl.textContent = scopeParts.length
    ? `Filtered Scope (${scopeParts.join(' • ')})`
    : `All Authorized Organizations (${totalOrgsCount} Orgs)`;

  // Update 3-Dimension Summary Legend in the Right Card
  const legPdu = document.getElementById('legend-pdu-summary');
  if (legPdu) {
    legPdu.innerHTML = `<span style="color:${isPduAbove ? '#f87171' : '#34d399'}">${consumedPdus.toLocaleString()} / ${limitPdu.toLocaleString()} (${pctPduUsed}%)</span>`;
  }
  const legEnv = document.getElementById('legend-env-summary');
  if (legEnv) {
    legEnv.innerHTML = `<span style="color:${isEnvAbove ? '#f87171' : '#34d399'}">${consumedEnvUnits.toLocaleString()} / ${limitEnvs.toLocaleString()} (${pctEnvUsed}%)</span>`;
  }
  const legCalls = document.getElementById('legend-calls-summary');
  if (legCalls) {
    legCalls.innerHTML = `<span style="color:${isCallsAbove ? '#f87171' : '#34d399'}">${formatCompactCalls(
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
  const legCallsBreakdown = document.getElementById('legend-calls-breakdown-summary');
  if (elStdCalls) {
    elStdCalls.textContent = `${formatCompactCalls(consumedStdCalls)} Std (0.2×)`;
    elStdCalls.title = `${consumedStdCalls.toLocaleString()} standard calls`;
  }
  if (elExtCalls) {
    elExtCalls.textContent = `${formatCompactCalls(consumedExtCalls)} Ext (1.0×)`;
    elExtCalls.title = `${consumedExtCalls.toLocaleString()} extensible calls`;
  }
  if (legCallsBreakdown) {
    const stdEnt = Math.round(consumedStdCalls * 0.2);
    legCallsBreakdown.title = `Standard: ${consumedStdCalls.toLocaleString()} calls (0.2× = ${stdEnt.toLocaleString()} ent. calls) • Extensible: ${consumedExtCalls.toLocaleString()} calls (1.0×) • Entitlement Sum: ${(stdEnt + consumedExtCalls).toLocaleString()}`;
  }

  // Determine active Gauge Dimension metrics
  let gaugeConsumed = consumedPdus;
  let gaugeLimit = limitPdu;
  let gaugePct = pctPduUsed;
  let gaugeAbove = isPduAbove;
  let gaugeSubLabel = `${consumedPdus.toLocaleString()} / ${limitPdu.toLocaleString()} PDUs`;

  if (selectedGaugeDimension === 'environments') {
    gaugeConsumed = consumedEnvUnits;
    gaugeLimit = limitEnvs;
    gaugePct = pctEnvUsed;
    gaugeAbove = isEnvAbove;
    gaugeSubLabel = `${consumedEnvUnits.toLocaleString()} / ${limitEnvs.toLocaleString()} Env Units`;
  } else if (selectedGaugeDimension === 'calls') {
    gaugeConsumed = consumedCalls;
    gaugeLimit = limitCalls;
    gaugePct = pctCallsUsed;
    gaugeAbove = isCallsAbove;
    gaugeSubLabel = `${formatCompactCalls(consumedCalls)} / ${formatCompactCalls(limitCalls)} Calls`;
  }

  const anyAboveLimit = isPduAbove || isEnvAbove || isCallsAbove;
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
  } else {
    cardEl.className = 'overview-bento below-limit';
    badgeEl.className = 'verdict-pill below';
    badgeEl.textContent = 'ALL 3 DIMENSIONS BELOW LIMIT';
  }

  if (progressFillEl) {
    progressFillEl.className = gaugeAbove ? 'limit-progress-fill above' : 'limit-progress-fill';
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
    donutRing.setAttribute('stroke', gaugeAbove ? '#f87171' : '#34d399');
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

  visibleEnvs.sort((a, b) => b.totalPdus - a.totalPdus || b.totalArtifacts - a.totalArtifacts);

  const tbody = document.getElementById('tbody-pdu');
  const tfoot = document.getElementById('tfoot-pdu');

  if (!visibleEnvs.length) {
    tbody.innerHTML = `
      <tr>
        <td colspan="13" style="text-align: center; padding: 1.5rem; color: var(--text-muted);">
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
        const regionCount = Number(e.regionCount || 0);
        const envLimitPct = limitEnvs > 0 ? ((regionCount / limitEnvs) * 100).toFixed(2) : '0.00';
        const pduLimitPct = limitPdu > 0 ? ((e.totalPdus / limitPdu) * 100).toFixed(2) : '0.00';
        const unitsLabel = includeSf
          ? `${e.totalArtifacts} <span style="color:var(--text-muted);font-size:0.74rem;">(${e.totalProxies}P + ${e.sharedFlows}SF)</span>`
          : `${e.totalProxies}`;
        const stdCalls = Number(e.standardApiCalls || 0);
        const extCalls = Number(e.extensibleApiCalls || 0);
        const envCalls = Number(e.yearlyApiCalls || 0);
        const callLimitPct = limitCalls > 0 ? ((envCalls / limitCalls) * 100).toFixed(2) : '0.00';

        return `
          <tr>
            <td class="mono-cell"><strong>${escapeHtml(e.organization)}</strong></td>
            <td class="footprint-col">${formatFootprint(e.runtimeType)}</td>
            <td class="mono-cell">${escapeHtml(e.environment)}</td>
            <td>${regionsHtml || '<span style="color:var(--text-muted);">Unattached (0 regions)</span>'}</td>
            <td class="num-cell">${regionCount}</td>
            <td class="num-cell">${envLimitPct}%</td>
            <td class="num-cell">${unitsLabel}</td>
            <td class="num-cell"><strong>${e.totalPdus.toLocaleString()}</strong></td>
            <td class="num-cell">${pduLimitPct}%</td>
            <td class="num-cell" title="${stdCalls.toLocaleString()} standard calls">${stdCalls.toLocaleString()} <span style="color:var(--text-muted);font-size:0.74rem;">(${formatCompactCalls(stdCalls)})</span></td>
            <td class="num-cell" title="${extCalls.toLocaleString()} extensible calls">${extCalls.toLocaleString()} <span style="color:var(--text-muted);font-size:0.74rem;">(${formatCompactCalls(extCalls)})</span></td>
            <td class="num-cell" title="${envCalls.toLocaleString()} entitlement calls (weighted)"><strong>${envCalls.toLocaleString()}</strong> <span style="color:var(--text-muted);font-size:0.74rem;">(${formatCompactCalls(envCalls)})</span></td>
            <td class="num-cell">${callLimitPct}%</td>
          </tr>
        `;
      })
      .join('');
  }

  const visibleUnits = visibleEnvs.reduce((s, e) => s + e.totalArtifacts, 0);
  const visibleEnvUnits = visibleEnvs.reduce((s, e) => s + (e.regionCount || 0), 0);
  const visibleStdCalls = visibleEnvs.reduce((s, e) => s + (e.standardApiCalls || 0), 0);
  const visibleExtCalls = visibleEnvs.reduce((s, e) => s + (e.extensibleApiCalls || 0), 0);
  const visibleCalls = visibleEnvs.reduce((s, e) => s + (e.yearlyApiCalls || 0), 0);

  tfoot.innerHTML = `
    <tr>
      <td colspan="4">TOTAL (${visibleEnvs.length} Environments Displayed)</td>
      <td class="num-cell">${visibleEnvUnits.toLocaleString()} / ${limitEnvs.toLocaleString()}</td>
      <td class="num-cell">${pctEnvUsed}%</td>
      <td class="num-cell">${visibleUnits.toLocaleString()} units</td>
      <td class="num-cell">${consumedPdus.toLocaleString()} PDUs</td>
      <td class="num-cell">${pctPduUsed}%</td>
      <td class="num-cell" title="${visibleStdCalls.toLocaleString()} standard calls">${formatCompactCalls(visibleStdCalls)}</td>
      <td class="num-cell" title="${visibleExtCalls.toLocaleString()} extensible calls">${formatCompactCalls(visibleExtCalls)}</td>
      <td class="num-cell" title="${visibleCalls.toLocaleString()} entitlement calls">${formatCompactCalls(visibleCalls)} / ${formatCompactCalls(limitCalls)}</td>
      <td class="num-cell">${pctCallsUsed}%</td>
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
    const res = await apiFetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
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
    card.classList.toggle('active', card.getAttribute('data-dim') === dim);
  });
  document.querySelectorAll('.gauge-dim-btn').forEach((btn) => {
    btn.classList.toggle('active', btn.getAttribute('data-gauge-dim') === dim);
  });
  renderSimplifiedDashboard();
}

document.addEventListener('DOMContentLoaded', () => {
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
  document.querySelectorAll('.dimension-card').forEach((card) => {
    card.addEventListener('click', () => {
      const dim = card.getAttribute('data-dim') || 'pdu';
      setActiveDimensionUI(dim);
      if (dim === 'pdu' || dim === 'environments') {
        document.querySelectorAll('.dim-btn').forEach((b) => {
          b.classList.toggle('active', b.getAttribute('data-chart-dim') === dim);
        });
        fetchMonitoringTimeSeries(selectedMonitoringDays, false, dim);
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
      const res = await fetch('/api/auth/oidc/auth-url');
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
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
      await fetch('/api/auth/login', {
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

  // Switch GCP Account

  document.getElementById('btn-switch-account')?.addEventListener('click', () => {
    currentPortfolio = null;
    allOrgsSelected = true;
    selectedOrgs.clear();
    currentMonitoringTs = null;
    currentCallsTs = null;
    document.getElementById('dashboard-screen').style.display = 'none';
    document.getElementById('login-screen').style.display = 'grid';
    initAuthScreen();
  });

  // Save Contract & Entitlement Parameters
  const saveEntitlementsHandler = async () => {
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

    const saveBtn = document.getElementById('btn-apply-limit');
    if (saveBtn) {
      saveBtn.classList.add('is-refreshing');
      saveBtn.textContent = contractChanged ? 'Updating Contract & Analytics...' : 'Saving...';
    }
    try {
      await updateSettings(
        {
          entitlementPdu: pduVal,
          entitlementEnvs: envVal,
          entitlementYearlyCalls: callsVal,
          contractStartDate: contractDateVal || null,
        },
        `Saved parameters (${pduVal.toLocaleString()} PDUs, ${envVal} Envs, ${formatCompactCalls(
          callsVal
        )} Calls, Start: ${contractDateVal}) to pdu_limit.json`
      );
      if (contractChanged) {
        await fetchMonthlyCallsSeries(true);
      } else if (currentCallsTs) {
        renderMonthlyCallsChart();
      }
    } finally {
      if (saveBtn) {
        saveBtn.classList.remove('is-refreshing');
        saveBtn.textContent = 'Save Parameters';
      }
    }
  };

  document.getElementById('btn-apply-limit')?.addEventListener('click', saveEntitlementsHandler);

  ['input-pdu-limit', 'input-env-limit', 'input-calls-limit', 'input-contract-date'].forEach((id) => {
    const el = document.getElementById(id);
    el?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        saveEntitlementsHandler();
      }
    });
    el?.addEventListener('change', () => {
      saveEntitlementsHandler();
    });
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
    showScanProgressModal('Refreshing Live Scan...', 'Connecting to Apigee APIs...', 5);

    function updateDashboardProgress(evt) {
      updateScanProgressModal(evt);
      const pct = Math.max(0, Math.min(100, evt.percent || 0));
      if (dPercent) dPercent.textContent = `${pct}%`;
      if (dFill) dFill.style.width = `${pct}%`;
      if (dStatus) dStatus.textContent = evt.message || 'Auditing live Apigee organizations...';
      if (dDetail) dDetail.textContent = evt.detail || '';
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
          entitlementPdu: pduVal >= 1 ? pduVal : undefined,
          entitlementEnvs: envVal >= 1 ? envVal : undefined,
          entitlementYearlyCalls: callsVal >= 1 ? callsVal : undefined,
          contractStartDate: contractDateVal || undefined,
        }),
      });

      updateDashboardProgress({ percent: 5, message: 'Initiating live Apigee scan...' });
      currentPortfolio = await startLiveScanWithProgress({
        account: null,
        jurisdiction: getStoredJurisdiction(),
        onProgress: updateDashboardProgress,
      });

      allOrgsSelected = true;
      selectedOrgs.clear();
      populateOrgDropdown(currentPortfolio);
      populateEnvDropdown(currentPortfolio);
      renderSimplifiedDashboard();
      await Promise.all([
        fetchMonitoringTimeSeries(selectedMonitoringDays, true, selectedChartDimension),
        fetchMonthlyCallsSeries(true),
      ]);
      updateScanProgressModal({ percent: 100, message: 'Scan Complete!', detail: 'Live scan finished' });
      await new Promise((r) => setTimeout(r, 250));
      showToast(
        `Refreshed live scan (${currentPortfolio.summary.totalPdus.toLocaleString()} PDUs, ${
          currentPortfolio.summary.totalEnvironmentUnits
        } Env Units, ${formatCompactCalls(currentPortfolio.summary.totalYearlyApiCalls)} Calls)`
      );
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
    if (hideZeroEl) hideZeroEl.checked = true;
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

  document.getElementById('btn-copy-token-cmd')?.addEventListener('click', () => copyCmdHandler('btn-copy-token-cmd'));
  document.getElementById('btn-copy-modal-token-cmd')?.addEventListener('click', () => copyCmdHandler('btn-copy-modal-token-cmd'));

  // Apply User Token (Login Card)
  document.getElementById('btn-apply-user-token')?.addEventListener('click', async () => {
    const input = document.getElementById('input-user-token');
    const msg = document.getElementById('token-validation-msg');
    const ok = await applyUserToken(input?.value, msg);
    if (ok && input) input.value = '';
  });

  document.getElementById('input-user-token')?.addEventListener('keydown', async (e) => {
    if (e.key === 'Enter') {
      const input = document.getElementById('input-user-token');
      const msg = document.getElementById('token-validation-msg');
      const ok = await applyUserToken(input?.value, msg);
      if (ok && input) input.value = '';
    }
  });

  // Apply User Token (Modal)
  document.getElementById('btn-apply-modal-token')?.addEventListener('click', async () => {
    const input = document.getElementById('input-modal-user-token');
    const msg = document.getElementById('modal-token-msg');
    const ok = await applyUserToken(input?.value, msg);
    if (ok) {
      if (input) input.value = '';
      setTimeout(() => {
        const modal = document.getElementById('modal-token-manager');
        if (modal) modal.style.display = 'none';
        if (msg) msg.style.display = 'none';
      }, 1000);
    }
  });

  document.getElementById('input-modal-user-token')?.addEventListener('keydown', async (e) => {
    if (e.key === 'Enter') {
      const input = document.getElementById('input-modal-user-token');
      const msg = document.getElementById('modal-token-msg');
      const ok = await applyUserToken(input?.value, msg);
      if (ok) {
        if (input) input.value = '';
        setTimeout(() => {
          const modal = document.getElementById('modal-token-manager');
          if (modal) modal.style.display = 'none';
          if (msg) msg.style.display = 'none';
        }, 1000);
      }
    }
  });

  // Modal Open & Close
  document.getElementById('btn-token-dialog')?.addEventListener('click', () => {
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

  document.getElementById('btn-close-token-modal')?.addEventListener('click', () => {
    const modal = document.getElementById('modal-token-manager');
    if (modal) modal.style.display = 'none';
  });

  document.getElementById('modal-token-manager')?.addEventListener('click', (e) => {
    if (e.target.id === 'modal-token-manager') {
      e.target.style.display = 'none';
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
