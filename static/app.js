'use strict';

let _importCount = 0;   // tracks how many times Import has been successfully used

/* ── Freemium tier helpers ────────────────────────────────── */

// These mirror the server's constants in app.py — they only shape the copy the
// user reads. Every limit is enforced server-side against Firestore; nothing here
// grants anything.
const FREE_HAND_LIMIT            = 30;   // hands shown per table
const FREE_HISTORY_DAYS          = 7;
const FREE_IMPORTS_PER_DAY       = 3;

const _SESSION_KEY         = 'pppha_session_id';
// An import made while signed out lives on the server for an hour; this is the
// claim ticket for it. sessionStorage, not localStorage: it belongs to this tab's
// visit, and a stale ticket in another tab would claim someone else's import.
const _PENDING_SESSION_KEY = 'pppha_pending_session';

// ── Tier-gated UI element lists ────────────────────────────────────────────────
// Add/remove IDs here to control which elements are shown per tier.
// FREE_ONLY_ELS  → visible to non-Pro users; hidden for Pro.
// PRO_ONLY_ELS   → visible to Pro users; hidden for non-Pro (note: sections managed
//                  by _loadHistory() are NOT listed here — they handle their own state).
const FREE_ONLY_ELS = [
];
const PRO_ONLY_ELS = [
  // (tournament sections managed separately by _loadHistory)
];
// Visible only to anon (signed-out) visitors — signed-in free users have already
// made the sign-in decision and don't need the marketing pitch again.
const ANON_ONLY_ELS = [
  'tier-compare',       // Free vs Pro marketing cards
];

// Firebase handles — populated by _initFirebase()
let _analytics   = null;
let _db          = null;
let _auth        = null;
let _currentUser = null;  // firebase.User or null

// In-memory freemium state — loaded from Firestore on auth change.
// Defaults to free tier until Firestore responds (safe fallback). Only is_pro
// lives here now: every counter is server-side, in users/{uid}.quota.
let _userState = { is_pro: false };

function getSessionId() {
  let id = localStorage.getItem(_SESSION_KEY);
  if (!id) {
    id = (typeof crypto !== 'undefined' && crypto.randomUUID)
      ? crypto.randomUUID()
      : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
          const r = Math.random() * 16 | 0;
          return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
        });
    localStorage.setItem(_SESSION_KEY, id);
  }
  return id;
}

function isPro() {
  return _userState.is_pro === true;
}

function isSignedIn() {
  return !!_currentUser;
}

/** 'anon' | 'free' | 'pro' — the same three tiers the server resolves. */
function currentTier() {
  if (!isSignedIn()) return 'anon';
  return isPro() ? 'pro' : 'free';
}

// Daily counters used to live here, in localStorage and a client-written
// Firestore field. They are server-side now (users/{uid}.quota): a counter the
// browser owns is a counter the browser can reset.
const _UPGRADE_REASONS = {
  export:        "You've used your exports for today. Upgrade to Pro for unlimited daily exports.",
  hands:         `Free accounts see only the last ${FREE_HAND_LIMIT} hands. Upgrade to Pro for unlimited history.`,
  tourney:       'Tournament exports are limited on the free plan.',
  import_quota:  `That's all ${FREE_IMPORTS_PER_DAY} imports for today. Upgrade to Pro for unlimited imports.`,
  // Fallbacks only — _handleExportFailure normally passes the live limit as
  // customText, sourced from the server's response body (see _quotaReasonText).
  hand_quota:    "You've used your hand exports for today. Upgrade to Pro for unlimited exports.",
  tourney_quota: "You've used your tournament exports for today. Upgrade to Pro for unlimited exports.",
  full_session:  'Exporting a whole session in one file is a Pro feature.',
  history:       `Free accounts keep ${FREE_HISTORY_DAYS} days of history. Upgrade to Pro to keep everything.`,
};

// hand_quota/tourney_quota copy depends on the admin-configured hard limit, which
// arrives per-request in the quota_exceeded response body (app.py's `limit` field) —
// building it from a module-level constant here would drift the moment an admin
// changes the config without a redeploy.
function _quotaReasonText(kind, limit) {
  // Tourney exports moved from a daily cap to lifetime-free-once + N/week
  // (see _tourney_export_gate in app.py) — this copy only fires once the
  // lifetime freebie is spent and the weekly limit is hit, so "a week" is
  // accurate here even though the export itself may have been free the very
  // first time.
  return kind === 'tourney'
    ? `Free accounts get ${limit} tournament export${limit === 1 ? '' : 's'} a week after your first free one. Upgrade to Pro for unlimited exports.`
    : `That's all ${limit} hand export${limit === 1 ? '' : 's'} for today. Upgrade to Pro for unlimited exports.`;
}

function showUpgradeModal(reason, customText) {
  const reasonEl = document.getElementById('pro-modal-reason');
  if (reasonEl) reasonEl.textContent = customText || _UPGRADE_REASONS[reason] || '';
  // Reset coming-soon banner and button
  const cs  = document.getElementById('pro-coming-soon');
  const btn = document.getElementById('pro-upgrade-btn');
  if (cs)  cs.classList.add('d-none');
  if (btn) btn.disabled = false;
  // Show dev hatch only when ?dev=1
  const devHatch = document.getElementById('pro-dev-hatch');
  if (devHatch) {
    devHatch.classList.toggle('d-none', new URLSearchParams(location.search).get('dev') !== '1');
  }
  _trackEvent('pro_modal_shown', { reason });
  const modal = bootstrap.Modal.getOrCreateInstance(document.getElementById('pro-upgrade-modal'));
  modal.show();
}

function handleUpgradeClick(tier = 'pro') {
  _trackEvent('pro_upgrade_clicked');
  const btn = document.getElementById('pro-upgrade-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Redirecting…'; }
  const fail = (msg) => {
    // Must match the button's initial label in index.html, or a failed checkout
    // silently relabels the CTA and drops the price from it.
    if (btn) { btn.disabled = false; btn.textContent = _pricingCtaLong(); }
    const cs = document.getElementById('pro-coming-soon');
    if (cs) { cs.textContent = msg; cs.classList.remove('d-none'); }
  };
  // uid/email are derived server-side from this token and are no longer sent in
  // the body — the server refuses the call without it.
  if (!_currentUser) { fail('Please sign in before upgrading.'); return; }
  _currentUser.getIdToken()
    .then(token => fetch('/api/create-checkout-session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${token}` },
      body: JSON.stringify({ tier }),
    }))
    .then(r => r.json())
    .then(d => {
      if (d.url) { window.location.href = d.url; }
      else throw new Error(d.error || 'Could not start checkout');
    })
    .catch(err => fail(err.message));
}

// activateProDev()/deactivateProDev() removed: they wrote is_pro straight from
// the client, which the Firestore rules now reject (see firestore.rules — a
// client-writable is_pro meant any signed-in user could self-grant Pro).

/** Open the sign-in modal, optionally explaining why it appeared. */
function showSignInModal(note) {
  const el = document.getElementById('auth-gate-note');
  if (el) {
    el.textContent = note || '';
    el.classList.toggle('d-none', !note);
  }
  _trackEvent('signin_modal_shown', { reason: note ? 'export' : 'manual' });
  bootstrap.Modal.getOrCreateInstance(document.getElementById('modal-auth')).show();
}

/* ── Export gate responses ───────────────────────────────── */

/**
 * Turn a refused export into the right prompt, and return the message to show
 * on the button.
 *
 * The server owns every one of these decisions; this only decides which door to
 * open. `retry` is re-run verbatim once the user earns their unlock, so callers
 * must pass a closure that repeats the exact same export.
 */
async function _handleExportFailure(res, kind, retry) {
  const body = await res.json().catch(() => ({}));
  const err  = body.error || '';

  if (res.status === 401 || err === 'login_required') {
    showSignInModal('Sign in to export your hands — it takes a few seconds and it stays free.');
    return 'Sign in to export';
  }
  if (err === 'upgrade_required') {
    showUpgradeModal(body.feature === 'full_session_export' ? 'full_session' : 'export');
    return 'Pro feature';
  }
  if (err === 'history_expired') {
    showUpgradeModal('history');
    return `Outside your ${FREE_HISTORY_DAYS}-day history`;
  }
  if (err === 'quota_exceeded') {
    showUpgradeModal(
      kind === 'tourney' ? 'tourney_quota' : 'hand_quota',
      typeof body.limit === 'number' ? _quotaReasonText(kind, body.limit) : null
    );
    return "That's your last one for today";
  }
  if (err === 'survey_required') {
    // Tourney exports still go through CPX; hand exports were switched to the
    // gate-stub modal (see _hand_export_gate in app.py) while ayeT/Wannads
    // rewarded-video approval is shelved.
    if (kind === 'tourney') {
      openSurveyModal(kind, retry);
      return 'Unlock with a quick survey';
    }
    _openGateStub('hand_export', retry, 'hand_quota');
    return 'Watch to unlock';
  }
  return err || 'Export failed';
}

/**
 * Run an export request, handling every tier refusal in one place.
 * Returns {blob, filename} on success; throws with a user-facing message
 * otherwise (the gate prompts have already been opened by then).
 */
async function _runExport(url, { kind, body, headers, fallbackName }) {
  const token = _currentUser ? await _currentUser.getIdToken().catch(() => null) : null;
  const h = Object.assign({ 'Content-Type': 'application/json' }, headers || {});
  if (token) h['Authorization'] = `Bearer ${token}`;

  const res = await fetch(url, { method: 'POST', headers: h, body: JSON.stringify(body || {}) });
  if (!res.ok) {
    const retry = () => _downloadExport(url, { kind, body, headers, fallbackName });
    throw new Error(await _handleExportFailure(res, kind, retry));
  }
  const cd = res.headers.get('Content-Disposition') || '';
  const m  = cd.match(/filename[^;=\n]*=([^;\n]*)/);
  return { blob: await res.blob(),
           filename: m ? m[1].replace(/['"]/g, '').trim() : fallbackName };
}

/** _runExport plus the browser download and the status toast. */
async function _downloadExport(url, opts, btn) {
  _rowExportStatus(btn, 'loading', opts.loadingText || 'Exporting…');
  try {
    const { blob, filename } = await _runExport(url, opts);
    _triggerDownload(blob, filename);
    _rowExportStatus(btn, 'ok', `Saved as ${filename}`, 5000);
    return true;
  } catch (err) {
    _rowExportStatus(btn, 'err', err.message, 6000);
    return false;
  }
}

/* ── Survey unlocks ──────────────────────────────────────── */
// A free user earns an export by completing a survey. CPX Research is the paid
// provider; Tally is the fallback for when CPX has nothing eligible — no revenue,
// but the user still gets moving and we get product research out of it.
//
// The server grants the credit from the provider's server-to-server callback, so
// the browser's only job is to open the survey and then watch /api/credits until
// the balance moves.

const _SURVEY_POLL_MS      = 1000;
const _SURVEY_POLL_TIMEOUT = 60000;

let _surveyState = { kind: null, retry: null, baseline: 0, timer: null, deadline: 0 };

function _surveyStatus(msg, tone) {
  const el = document.getElementById('survey-status');
  if (!el) return;
  el.textContent = msg || '';
  el.style.color = tone === 'err' ? 'var(--red)'
                 : tone === 'ok'  ? 'var(--green)' : 'var(--muted)';
}

async function openSurveyModal(kind, retry) {
  if (!_currentUser) { showSignInModal('Sign in to unlock exports.'); return; }
  _surveyState = { kind, retry, baseline: 0, timer: null, deadline: 0 };

  const modalEl = document.getElementById('survey-modal');
  if (!modalEl) return;
  const label = document.getElementById('survey-kind-label');
  if (label) {
    label.textContent = kind === 'tourney'
      ? 'one tournament export' : 'one more hand export';
  }
  _surveyStatus('Loading a survey…');
  const frame = document.getElementById('survey-frame');
  if (frame) frame.removeAttribute('src');
  const fallbackBtn = document.getElementById('survey-fallback-btn');
  if (fallbackBtn) fallbackBtn.classList.add('d-none');

  _trackEvent('survey_modal_shown', { kind });
  bootstrap.Modal.getOrCreateInstance(modalEl).show();

  let cfg = {};
  try {
    const token = await _currentUser.getIdToken();
    const res   = await fetch('/api/survey-config', { headers: { Authorization: `Bearer ${token}` } });
    cfg = res.ok ? await res.json() : {};
    _surveyState.baseline = (await _fetchCredits())[kind] || 0;
  } catch (e) {
    console.warn('survey config failed', e);
  }
  _surveyState.config = cfg;

  if (cfg.cpx && cfg.cpx.app_id) {
    _surveyShowCpx(cfg.cpx, kind);
  } else if (cfg.tally_form_url) {
    _surveyShowTally(cfg.tally_form_url, kind);
  } else {
    _surveyStatus('No surveys are available right now — please try again later.', 'err');
    return;
  }
  if (cfg.tally_form_url && fallbackBtn) fallbackBtn.classList.remove('d-none');
  _surveyStartPolling();
}

/**
 * Point the modal's iframe at CPX Research.
 *
 * TODO(cpx): confirm the widget path and parameter names against the CPX
 * dashboard's "Website Script" page before launch. ext_user_id is the Firebase
 * uid and subid_1 carries which unlock the user is chasing, so the postback in
 * app.py knows which credit to grant. secure_hash is computed server-side in
 * /api/survey-config — the app secret must never reach the browser.
 */
function _surveyShowCpx(cpx, kind) {
  const frame = document.getElementById('survey-frame');
  if (!frame) return;
  const params = new URLSearchParams({
    app_id:      cpx.app_id,
    ext_user_id: cpx.ext_user_id,
    subid_1:     kind,
  });
  if (cpx.secure_hash) params.set('secure_hash', cpx.secure_hash);
  frame.src = `https://offers.cpx-research.com/index.php?${params.toString()}`;
  _surveyStatus('Complete the survey to unlock your export.');
  _trackEvent('survey_provider_shown', { provider: 'cpx', kind });
}

/**
 * Swap to the Tally fallback form.
 *
 * TODO(tally): the form must define hidden fields named `uid` and `kind` and have
 * its webhook pointed at /api/tally/callback with the signing secret set to
 * TALLY_SIGNING_SECRET. Tally maps URL query params onto hidden fields of the
 * same name, which is how the two values below reach the webhook payload.
 */
function _surveyShowTally(formUrl, kind) {
  const frame = document.getElementById('survey-frame');
  if (!frame) return;
  const sep = formUrl.includes('?') ? '&' : '?';
  frame.src = `${formUrl}${sep}uid=${encodeURIComponent(_currentUser.uid)}&kind=${encodeURIComponent(kind)}`;
  _surveyStatus('Answer a few quick questions to unlock your export.');
  _trackEvent('survey_provider_shown', { provider: 'tally', kind });
}

function useSurveyFallback() {
  const url = (_surveyState.config || {}).tally_form_url;
  if (url) _surveyShowTally(url, _surveyState.kind);
}

async function _fetchCredits() {
  if (!_currentUser) return { hand: 0, tourney: 0 };
  try {
    const token = await _currentUser.getIdToken();
    const res = await fetch('/api/credits', { headers: { Authorization: `Bearer ${token}` } });
    return res.ok ? await res.json() : { hand: 0, tourney: 0 };
  } catch (e) {
    return { hand: 0, tourney: 0 };
  }
}

/**
 * Watch /api/credits until the balance for this kind goes up, then retry the
 * export that was refused. Polling (rather than trusting a postMessage from the
 * provider's iframe) is deliberate: the credit is only real once the provider's
 * server-to-server callback has landed, and a message from a third-party frame
 * is not evidence that it has.
 */
function _surveyStartPolling() {
  clearTimeout(_surveyState.timer);
  _surveyState.deadline = Date.now() + _SURVEY_POLL_TIMEOUT;

  const tick = async () => {
    if (!_surveyState.kind) return;
    const credits = await _fetchCredits();
    if ((credits[_surveyState.kind] || 0) > _surveyState.baseline) {
      _surveyEarned();
      return;
    }
    if (Date.now() > _surveyState.deadline) {
      _surveyStatus('Still waiting on the survey provider. Close this and try the '
                    + 'export again in a minute — your unlock is not lost.', 'err');
      return;
    }
    _surveyState.timer = setTimeout(tick, _SURVEY_POLL_MS);
  };
  _surveyState.timer = setTimeout(tick, _SURVEY_POLL_MS);
}

function _surveyEarned() {
  const retry = _surveyState.retry;
  const kind  = _surveyState.kind;
  clearTimeout(_surveyState.timer);
  _surveyStatus('Unlocked — starting your export…', 'ok');
  _trackEvent('survey_completed', { kind });
  setTimeout(() => {
    closeSurveyModal();
    if (retry) retry();
  }, 900);
}

function closeSurveyModal() {
  clearTimeout(_surveyState.timer);
  _surveyState = { kind: null, retry: null, baseline: 0, timer: null, deadline: 0 };
  const frame = document.getElementById('survey-frame');
  if (frame) frame.removeAttribute('src');   // stop the provider's page running
  const modalEl = document.getElementById('survey-modal');
  if (modalEl) {
    const modal = bootstrap.Modal.getInstance(modalEl);
    if (modal) modal.hide();
  }
}

/* ── Gate stub modal ("watch to unlock" ad stand-in) ─────── */
// ayeT-Studios/Wannads rewarded-video approval is pending, so this is a
// self-hosted stand-in with the same completion contract a real ad would have:
// 30s of forced user-visible attention, an OK button that only enables once
// the timer runs out, and a server-side signal (POST /api/gate/stub-completion)
// that records the completion for a future gate check to consume.
//
// The modal body doubles as a Pro upsell — forced attention is a conversion
// opportunity while there's no ad revenue at stake yet.
//
// When ayeT/Wannads approval lands (separate follow-up Feature), only this
// function gets swapped for the real SDK — the completion contract (endpoint
// shape, gate-check code, admin config) stays the same.
//
// NOT wired to any gate check yet (that's Task 6) — this is just the reusable
// modal component + its completion endpoint, callable the same way
// openSurveyModal()/_surveyShowCpx() are today.

const _GATE_STUB_SECONDS = 30;   // keep in sync with the "wait 30 seconds" copy
                                  // in templates/index.html's gate-stub-modal
let _gateStubState = {
  kind: null, onComplete: null, remaining: 0, timer: null,
  completionId: null, posted: false,
};

function _gateStubKindCopy(kind) {
  const I = window.I18N_GATE_STUB || {};
  return kind === 'import'
    ? { kindLabel: I.kindLabelImport, feature: I.featureImport }
    : { kindLabel: I.kindLabelHandExport, feature: I.featureHandExport };
}

function _gateStubNewId() {
  return (window.crypto && crypto.randomUUID)
    ? crypto.randomUUID()
    : `gs_${Date.now()}_${Math.random().toString(36).slice(2)}`;
}

/**
 * The gate-stub dispatch point for imports and hand exports (AC6/AC8 of the
 * wire-three-gates task): open the stub modal when GATE_STUB_MODAL_ENABLED,
 * otherwise fall through to the upgrade prompt instead of silently letting
 * the gated action through. This is deliberately a thin wrapper around
 * _showGateStubModal rather than relying on that function's own internal
 * "flag off → call onComplete() for free" fallback — that fallback exists so
 * a caller that forgets to check the flag doesn't hard-block on a missing
 * modal element, but at this call site (the actual gate dispatch) a
 * disabled flag must mean "no unlock mechanism available", not "let it
 * through for free". When a real rewarded-video SDK replaces the stub, only
 * this function's branch needs to change — the gate-check code in app.py
 * does not.
 */
function _openGateStub(stubKind, retry, upgradeReason) {
  if (window.GATE_STUB_MODAL_ENABLED === false) {
    showUpgradeModal(upgradeReason);
    return;
  }
  _showGateStubModal(stubKind, retry);
}

/**
 * Show the "watch to unlock" stub modal. onComplete fires exactly once, after
 * the 30s timer has elapsed AND the (by-then-enabled) OK button is clicked.
 * kind is 'import' or 'hand_export' — it only drives copy, never the timer.
 */
function _showGateStubModal(kind, onComplete) {
  if (window.GATE_STUB_MODAL_ENABLED === false) {
    // GATE_STUB_MODAL_ENABLED=false server-side — nothing stands in for the ad,
    // so don't block whatever called this.
    if (onComplete) onComplete();
    return;
  }
  const modalEl = document.getElementById('gate-stub-modal');
  if (!modalEl) { if (onComplete) onComplete(); return; }

  clearInterval(_gateStubState.timer);
  _gateStubState = {
    kind, onComplete, remaining: _GATE_STUB_SECONDS, timer: null,
    completionId: _gateStubNewId(), posted: false,
  };

  const copy = _gateStubKindCopy(kind);
  const kindLabelEl    = document.getElementById('gate-stub-kind-label');
  const featureLabelEl = document.getElementById('gate-stub-feature-label');
  if (kindLabelEl)    kindLabelEl.textContent = copy.kindLabel || '';
  if (featureLabelEl) featureLabelEl.textContent = copy.feature || '';

  const proBtn = document.getElementById('gate-stub-pro-btn');
  if (proBtn) {
    proBtn.onclick = () => {
      closeGateStubModal();
      showUpgradeModal(kind === 'import' ? 'import_quota' : 'hand_quota');
    };
  }

  const okBtn = document.getElementById('gate-stub-ok-btn');
  if (okBtn) {
    okBtn.disabled = true;
    okBtn.onclick = _gateStubOkClicked;
  }
  _gateStubRenderCountdown();

  _trackEvent('gate_stub_modal_shown', { kind });
  bootstrap.Modal.getOrCreateInstance(modalEl).show();

  _gateStubState.timer = setInterval(() => {
    _gateStubState.remaining -= 1;
    _gateStubRenderCountdown();
    if (_gateStubState.remaining <= 0) {
      clearInterval(_gateStubState.timer);
      _gateStubState.timer = null;
    }
  }, 1000);
}

function _gateStubRenderCountdown() {
  const okBtn = document.getElementById('gate-stub-ok-btn');
  if (!okBtn) return;
  const remaining = Math.max(_gateStubState.remaining, 0);
  const I = window.I18N_GATE_STUB || {};
  okBtn.disabled = remaining > 0;
  okBtn.textContent = remaining > 0
    ? (I.unlockCountdown || 'Unlock in __SECONDS__ seconds…').replace('__SECONDS__', String(remaining))
    : (I.unlockReady || 'Unlock');
}

/** OK button handler — only meaningful once the button is enabled (t=0). */
function _gateStubOkClicked() {
  // Guards double-click / double-fire: once posted stays true, a second click
  // (even one queued before the first click's synchronous disable took effect)
  // is a no-op instead of a second completion POST.
  if (_gateStubState.remaining > 0 || _gateStubState.posted) return;
  _gateStubState.posted = true;
  const okBtn = document.getElementById('gate-stub-ok-btn');
  if (okBtn) okBtn.disabled = true;

  const { kind, onComplete, completionId } = _gateStubState;
  _trackEvent('gate_stub_completed', { kind });
  _postGateStubCompletion(kind, completionId)
    .catch(err => console.warn('gate stub completion POST failed', err))
    .then(() => {
      closeGateStubModal();
      if (onComplete) onComplete();
    });
}

async function _postGateStubCompletion(kind, completionId) {
  if (!_currentUser) return;
  const token = await _currentUser.getIdToken();
  await fetch('/api/gate/stub-completion', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
    body: JSON.stringify({ kind, ts: Date.now(), completion_id: completionId }),
  });
}

function closeGateStubModal() {
  clearInterval(_gateStubState.timer);
  _gateStubState = {
    kind: null, onComplete: null, remaining: 0, timer: null,
    completionId: null, posted: false,
  };
  const modalEl = document.getElementById('gate-stub-modal');
  if (modalEl) {
    const modal = bootstrap.Modal.getInstance(modalEl);
    if (modal) modal.hide();
  }
}

/* ── Helpers ─────────────────────────────────────────────── */

function fmtChips(n) {
  if (n == null) return 'N/A';
  return Math.abs(n).toLocaleString();
}

function fmtProfitHtml(n) {
  if (n === 0) return '<span class="profit-zero">0</span>';
  const abs = Math.abs(n).toLocaleString();
  return n > 0
    ? `<span class="profit-pos">+${abs}</span>`
    : `<span class="profit-neg">−${abs}</span>`;
}

function fmtProfitPlain(n) {
  if (n === 0) return '0';
  const abs = Math.abs(n).toLocaleString();
  return n > 0 ? `+${abs}` : `−${abs}`;
}

function renderCard(card) {
  return `<span class="playing-card ${card.suit_class}">
    <span class="card-rank">${card.rank}</span><span class="card-suit">${card.suit}</span>
  </span>`;
}

function resultBadge(result) {
  if (result === 'Won')  return '<span class="badge-won">Won</span>';
  if (result === 'Lost') return '<span class="badge-lost">Lost</span>';
  return '<span class="badge-break">Break even</span>';
}

function posBadge(pos) {
  if (!pos || pos === '?') return '<span class="pos-badge pos-bl">?</span>';
  const cls = pos === 'BTN' ? 'pos-btn' : (pos === 'SB' || pos === 'BB') ? 'pos-bl' : '';
  return `<span class="pos-badge ${cls}">${pos}</span>`;
}

// Street chips share .pos-badge's shape but carry their own fixed width, so the
// two columns line up within themselves without one padding out to the other's
// longest label.
const _STREET_BADGES = {
  'Pre':      ['pos-bl',        'Pre'],
  'Pre VPIP': ['street-vpip',   'Pre VPIP'],
  'Flop':     ['street-flop',   'Flop'],
  'Turn':     ['street-turn',   'Turn'],
  'River':    ['street-river',  'River'],
  'SD':       ['street-sd',     'Showdown'],
};

function streetBadge(s) {
  const [cls, label] = _STREET_BADGES[s] || ['pos-bl', s || '—'];
  return `<span class="pos-badge street-badge ${cls}">${label}</span>`;
}

function fmtProfitBB(profit, bigBlind) {
  if (!bigBlind || profit == null) return '<span class="profit-zero">—</span>';
  const bb = profit / bigBlind;
  if (bb === 0) return '<span class="profit-zero">0</span>';
  const abs = Math.abs(bb).toFixed(2);
  return bb > 0
    ? `<span class="profit-pos">+${abs}</span>`
    : `<span class="profit-neg">−${abs}</span>`;
}

function shortHandNum(gameid) {
  const parts = (gameid || '').split('-');
  return parts[2] ? String(parseInt(parts[2], 10)) : (gameid || '—');
}

/* ── Timezone helpers ────────────────────────────────────── */

// The display zone is shared with the Tournaments page through this localStorage key, so a
// choice made on either page carries across on the next load.
function _savedTz() { return localStorage.getItem('pppha_tz') || 'Australia/Adelaide'; }

function currentTz() {
  const sel = document.getElementById('tz-select');
  return sel ? sel.value : _savedTz();
}

/** Format parts of a date/time into a plain object keyed by part type. */
function _tzParts(ts, tz, opts) {
  const out = {};
  new Intl.DateTimeFormat('en-AU', Object.assign({ timeZone: tz }, opts))
    .formatToParts(new Date(ts * 1000))
    .forEach(function (p) { out[p.type] = p.value; });
  return out;
}

/* ── Hand id copy ─────────────────────────────────────── */

function copyHandId(btn) {
  const handNum = btn.dataset.handNum;
  navigator.clipboard.writeText(handNum).catch(() => {});
  btn.innerHTML = `<svg viewBox="0 0 24 24"><polyline points="20 6 9 17 4 12"/></svg>`;
  btn.classList.add('copied');
  setTimeout(() => {
    btn.classList.remove('copied');
    btn.innerHTML = `<svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>`;
  }, 1500);
}

/** "8 Jun 26, 14:30" — on mobile collapses to "8 Jun, 14:30" */
function fmtHandDateTime(ts, tz) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const dateFull = new Intl.DateTimeFormat('en-GB', {
    timeZone: tz, day: 'numeric', month: 'short', year: '2-digit',
  }).format(d);
  const dateShort = new Intl.DateTimeFormat('en-GB', {
    timeZone: tz, day: 'numeric', month: 'short',
  }).format(d);
  const time = new Intl.DateTimeFormat('en-GB', {
    timeZone: tz, hour: '2-digit', minute: '2-digit', hour12: false,
  }).format(d);
  return `<span class="d-none d-md-inline">${dateFull}, ${time}</span>`
       + `<span class="d-md-none">${dateShort}<br>${time}</span>`;
}

/** "8 Jun 26" */
function fmtDate(ts, tz) {
  if (!ts) return '—';
  return new Intl.DateTimeFormat('en-GB', {
    timeZone: tz, day: 'numeric', month: 'short', year: '2-digit',
  }).format(new Date(ts * 1000));
}

/** "2:30 PM" */
function fmtTime(ts, tz) {
  if (!ts) return '—';
  return new Intl.DateTimeFormat('en-US', {
    timeZone: tz, hour: 'numeric', minute: '2-digit', hour12: true,
  }).format(new Date(ts * 1000));
}

/** "ACDT", "UTC", "EST", etc. */
function getTzAbbr(ts, tz) {
  const parts = new Intl.DateTimeFormat('en-AU', {
    timeZone: tz, timeZoneName: 'short',
  }).formatToParts(new Date(ts * 1000));
  return (parts.find(function (p) { return p.type === 'timeZoneName'; }) || {}).value || tz;
}

/** Refresh every [data-tz-label] column header to show the current abbreviation. */
function updateTzHeaders() {
  const data  = window._lastData;
  const refTs = (data && data.tournaments && data.tournaments[0] &&
                 data.tournaments[0].earliest_ts)
                 || Date.now() / 1000;
  const abbr = getTzAbbr(refTs, currentTz());
  document.querySelectorAll('[data-tz-label]').forEach(function (th) {
    th.textContent = th.dataset.tzLabel + ' (' + abbr + ')';
  });
}

/* ── UI helpers ──────────────────────────────────────────── */

function showError(msg) {
  const el = document.getElementById('error-msg');
  el.textContent = msg;
  el.classList.remove('d-none');
}

function clearError() {
  const el = document.getElementById('error-msg');
  el.classList.add('d-none');
  el.textContent = '';
}

function setLoading(on) {
  const box      = document.getElementById('loading-msg');
  const spinner  = document.getElementById('loading-spinner');
  const text     = document.getElementById('loading-text');
  if (on) {
    spinner.classList.remove('d-none');
    text.style.color = 'var(--green)';
    text.textContent = 'Fetching hand history… this may take a few seconds';
    box.classList.remove('d-none');
  } else {
    box.classList.add('d-none');
  }
  document.getElementById('import-btn').disabled = on;
}

function showImportSuccess(data) {
  const box     = document.getElementById('loading-msg');
  const spinner = document.getElementById('loading-spinner');
  const text    = document.getElementById('loading-text');
  const name    = data.player?.name || 'Player';
  const newHands  = data.new_hands ?? 0;
  const total     = data.total_fetched ?? 0;
  const tourCount = data.new_tourney_count || (data.tournaments?.length || 0);
  spinner.classList.add('d-none');
  text.style.color = 'var(--green)';
  _importCount++;
  const tourFrag = tourCount
    ? ` across <strong>${tourCount}</strong> tournament${tourCount !== 1 ? 's' : ''}`
    : '';

  let html;
  if (data.tier === 'anon') {
    html =
      `✓ Analysed <strong>${total}</strong> hands${tourFrag}. ` +
      `<button class="btn-link-inline" onclick="showSignInModal('Sign in to save your hands.')">Sign in to save them.</button>`;
  } else if (newHands > 0) {
    html =
      `✓ Welcome, <strong>${name}</strong>! ` +
      `<strong>${newHands}</strong> new hands loaded${tourFrag}.`;
  } else {
    html =
      `✓ Welcome back, <strong>${name}</strong>! ` +
      `<strong>${total}</strong> hands re-analysed (all already saved).`;
  }
  text.innerHTML = html;
  box.classList.remove('d-none');
}

/* ── Gamification ────────────────────────────────────────── */

// This file interpolates values straight into template literals everywhere else, which is
// fine for numbers and server-generated labels. Badge names and leaderboard display names
// are the first strings here that originate from user-controlled data, so they get escaped.
function _esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function _fmtNum(n) {
  return Number(n || 0).toLocaleString('en-AU');
}

// TODO(Caio): add real promotional image URLs here (recommended ~160x600
// vertical "skyscraper" crop). Empty by default — the side banner slot
// stays hidden until at least one is set (see _initSideBanner).
const SIDE_BANNER_IMAGES = [];
const SIDE_BANNER_INTERVAL_MS = 6000; // within the AC's 5-8s default range

/** Vertical auto-rotating side banner. No-op (stays hidden) with 0 images;
    shows statically with 1; rotates with 2+. */
function _initSideBanner() {
  const el  = document.getElementById('side-banner');
  const img = document.getElementById('side-banner-img');
  if (!el || !img || !SIDE_BANNER_IMAGES.length) return;

  let i = 0;
  const show = idx => {
    img.style.opacity = 0;
    setTimeout(() => {
      img.src = SIDE_BANNER_IMAGES[idx];
      img.style.opacity = 1;
    }, 200);
  };
  show(0);
  el.classList.remove('d-none');

  if (SIDE_BANNER_IMAGES.length > 1) {
    setInterval(() => {
      i = (i + 1) % SIDE_BANNER_IMAGES.length;
      show(i);
    }, SIDE_BANNER_INTERVAL_MS);
  }
}

// TODO(Caio): set a real promotional image URL here (a wide, short crop
// works best — the slot caps at 180px tall). Empty by default — the
// mid-page banner stays hidden until this is set (see _initMidBanner).
const MID_BANNER_IMAGE = '';

/** Static horizontal mid-page banner. No-op (stays hidden) with no image configured. */
function _initMidBanner() {
  const el  = document.getElementById('mid-banner');
  const img = document.getElementById('mid-banner-img');
  if (!el || !img || !MID_BANNER_IMAGE) return;

  img.src = MID_BANNER_IMAGE;
  el.classList.remove('d-none');
}

/** Simple network connectivity indicator, driven by navigator.onLine + online/offline events. */
function _initConnStatus() {
  const el    = document.getElementById('conn-status');
  const label = document.getElementById('conn-status-label');
  if (!el || !label) return;
  const onlineText  = el.dataset.onlineLabel  || 'Online';
  const offlineText = el.dataset.offlineLabel || 'Offline';
  const update = () => {
    const online = navigator.onLine;
    el.classList.toggle('conn-offline', !online);
    el.classList.toggle('conn-online', online);
    label.textContent = online ? onlineText : offlineText;
  };
  window.addEventListener('online', update);
  window.addEventListener('offline', update);
  update();
}

/** Refresh the two rewards blocks flanking the title. No-op when signed out — they stay hidden. */
function _loadGamification() {
  const left  = document.getElementById('gam-block-left');
  const right = document.getElementById('gam-block-right');
  if (!left || !right) return;
  if (!_currentUser) { left.classList.add('d-none'); right.classList.add('d-none'); return; }

  _currentUser.getIdToken()
    .then(token => fetch('/api/gamification', { headers: { Authorization: `Bearer ${token}` } }))
    .then(r => (r.ok ? r.json() : null))
    .then(g => {
      if (!g) return;
      document.getElementById('gam-points').textContent = _fmtNum(g.points_total);
      document.getElementById('gam-streak').textContent =
        g.streak_days ? `${g.streak_days}` : '—';
      document.getElementById('gam-rank').textContent =
        g.rank ? `#${g.rank}` : '—';

      // Newest badges first — the most recent unlock is the interesting one.
      const badges = (g.badges || []).slice().sort((a, b) => (b.ts || 0) - (a.ts || 0));
      const shown  = badges.slice(0, 4);
      let html = shown.map(b =>
        `<span class="gam-badge" title="${_esc(b.name)} — ${_esc(b.title)}">${_esc(b.title)}</span>`
      ).join('');
      if (badges.length > shown.length) {
        html += `<span class="gam-badge gam-badge-more">+${badges.length - shown.length}</span>`;
      }
      document.getElementById('gam-badges').innerHTML = html;

      const next = g.next_badge;
      document.getElementById('gam-next').innerHTML = next
        ? `<strong>${_fmtNum(next.remaining)}</strong> hands to ${_esc(next.title)}`
        : '';

      left.classList.remove('d-none');
      right.classList.remove('d-none');
    })
    .catch(() => { /* the rewards blocks are decoration — never surface a failure here */ });
}

/** Floating summary of what an import just earned. */
function showGamificationToast(g) {
  const toast = document.getElementById('gam-toast');
  if (!toast || !g || (!g.points && !(g.badges || []).length)) return;
  clearTimeout(toast._hideTimer);

  const rows = (g.awards || [])
    .map(a => `<div class="gam-toast-row"><span>${_esc(a.label)}</span><span>+${_fmtNum(a.points)}</span></div>`)
    .join('');
  const unlocked = (g.badges || [])
    .map(b => `<span class="gam-toast-unlock" title="${_esc(b.name)}">🏅 ${_esc(b.title)}</span>`)
    .join('');

  toast.innerHTML =
    `<div class="gam-toast-head">+${_fmtNum(g.points)} points</div>` +
    (rows ? `<div class="gam-toast-rows">${rows}</div>` : '') +
    (unlocked ? `<div class="gam-toast-badges">${unlocked}</div>` : '');

  toast.classList.add('gam-visible');
  // Badge unlocks are worth reading, so they linger a little longer.
  toast._hideTimer = setTimeout(() => toast.classList.remove('gam-visible'),
                                unlocked ? 9000 : 6000);
}

/* ── Import handler ──────────────────────────────────────── */

function handleImport() {
  const url = (document.getElementById('url-input').value || '').trim();
  clearError();
  document.getElementById('results-section').classList.add('d-none');

  if (!url) {
    showError('Please enter a PPPoker Hand Review URL.');
    return;
  }

  setLoading(true);

  const _doFetch = (idToken) => {
    const headers = { 'Content-Type': 'application/json' };
    if (idToken) headers['Authorization'] = `Bearer ${idToken}`;
    fetch('/api/analyze', {
      method: 'POST',
      headers,
      // No session_id: the server used to key an in-process cache of the
      // imported hands off it. That cache is gone — exports read from the
      // player's own persisted tournaments instead.
      body: JSON.stringify({ url }),
    })
      .then(r => r.json().then(data => ({ ok: r.ok, status: r.status, data })))
      .then(({ ok, data }) => {
        setLoading(false);
        if (!ok && data.error === 'quota_exceeded') {
          showUpgradeModal('import_quota');
          showError(`You've used all ${data.limit ?? FREE_IMPORTS_PER_DAY} imports for today. `
                    + 'Imports reset at midnight UTC.');
          return;
        }
        if (!ok && data.error === 'survey_required') {
          // Free import beyond the ungated allowance — watch-to-unlock via the
          // gate-stub modal (imports never used CPX; see _import_gate in app.py).
          _openGateStub('import', () => { setLoading(true); _doFetch(idToken); },
                       'import_quota');
          return;
        }
        if (data.error) { showError(data.error); return; }
        _rememberPendingSession(data.session_token);
        applyImportResult(data);
      })
      .catch(err => {
        setLoading(false);
        showError('Network error: ' + err.message);
      });
  };

  if (_currentUser) {
    _currentUser.getIdToken().then(_doFetch).catch(() => _doFetch(null));
  } else {
    _doFetch(null);
  }
}

/** Render one import (fresh or claimed) and refresh everything that follows it. */
function applyImportResult(data) {
  renderResults(data);
  showImportSuccess(data);
  if (data.gamification) {
    showGamificationToast(data.gamification);
    _loadGamification();
  }
  if (data.saved) {
    _startImportHighlights((data.tournaments || []).map(t => t.tourney_id).filter(Boolean));
    _loadHistory();
  }
}

/* ── Signed-out import → sign-in handoff ─────────────────── */

/**
 * Hold on to the claim ticket for an import made while signed out.
 *
 * The hands themselves stay on the server for an hour; this is only the signed
 * token that proves which parked import is ours. Signing in during that hour
 * adopts it into the account instead of making the user paste the link again.
 */
function _rememberPendingSession(token) {
  try {
    if (token) sessionStorage.setItem(_PENDING_SESSION_KEY, token);
  } catch (e) { /* private mode — the user just re-imports after signing in */ }
}

function _pendingSession() {
  try { return sessionStorage.getItem(_PENDING_SESSION_KEY) || ''; }
  catch (e) { return ''; }
}

function _clearPendingSession() {
  try { sessionStorage.removeItem(_PENDING_SESSION_KEY); } catch (e) {}
}

/** Adopt a signed-out import into the account that just signed in. */
async function _claimPendingSession() {
  const token = _pendingSession();
  if (!token || !_currentUser) return;
  try {
    const idToken = await _currentUser.getIdToken();
    const res = await fetch('/api/analyze/claim', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${idToken}` },
      body: JSON.stringify({ session_token: token }),
    });
    const data = await res.json().catch(() => ({}));

    if (res.ok && data.claimed) {
      _clearPendingSession();
      window._anonGraphs = null;         // the persisted endpoints serve them now
      applyImportResult(data);
      _trackEvent('anon_session_claimed', { hands: data.total_fetched || 0 });
      return;
    }
    if (data.error === 'quota_exceeded') {
      // Deliberately keeps the ticket: the import is still parked server-side and
      // can be claimed tomorrow, or straight away after upgrading.
      showUpgradeModal('import_quota');
      return;
    }
    if (data.error === 'survey_required') {
      // Deliberately keeps the ticket too — same reasoning as quota_exceeded
      // above, just an unlock instead of a wait.
      _openGateStub('import', () => { _claimPendingSession(); }, 'import_quota');
      return;
    }
    _clearPendingSession();             // expired or already claimed
  } catch (e) {
    console.warn('claim failed', e);
  }
}

/* ── Render all ──────────────────────────────────────────── */

function renderResults(data) {
  window._lastData = data;   // persisted so tz changes can re-render
  _trackEvent('hands_imported', { total: data.total_fetched || 0 });

  // A signed-out import gets its per-tournament graph data inline, because the
  // detail endpoint that normally serves it needs an account. Keyed by id so a
  // row click can render the graph with no further round trip.
  window._anonGraphs = null;
  if (Array.isArray(data.tournament_graphs) && data.tournament_graphs.length) {
    window._anonGraphs = {};
    data.tournament_graphs.forEach(g => {
      if (g && g.tourney_id) window._anonGraphs[g.tourney_id] = g;
    });
  }

  // Derive date span from newly-imported hands only
  const _spanStr = (() => {
    if (!data.new_ts_min) return null;
    const tz   = currentTz();
    const opts = { day: 'numeric', month: 'short', year: '2-digit', timeZone: tz };
    const dMin = new Date(data.new_ts_min * 1000).toLocaleDateString('en-GB', opts);
    const dMax = new Date(data.new_ts_max * 1000).toLocaleDateString('en-GB', opts);
    return dMin === dMax ? dMax : `${dMin} – ${dMax}`;
  })();

  // Player avatar initials
  const _initials = (data.player.name || '').replace(/[^A-Za-z0-9]/g, '').slice(0, 2).toUpperCase() || '??';
  const _avatarEl = document.getElementById('player-avatar-text');
  if (_avatarEl) _avatarEl.textContent = _initials;

  document.getElementById('player-info').innerHTML =
    `<strong>${data.player.name}</strong>` +
    `<span style="color:var(--muted);font-size:.8rem">&nbsp;&nbsp;UID: ${data.player.uid}</span>` +
    (_spanStr ? `<span style="color:var(--muted);font-size:.78rem">&nbsp;&nbsp;${_spanStr}</span>` : '') +
    // fetch_failed, not (available - fetched): on a free account the difference
    // also contains hands pruned by the history window, which did not fail.
    (data.fetch_failed
      ? `&nbsp;&nbsp;<span class="text-warning" style="font-size:.8rem">(${data.fetch_failed} hands failed to load)</span>`
      : '');

  renderHandStats(data);
  renderRecentHands(data.recent_hands || []);
  renderRecentWonHands(data.recent_won_hands || []);
  // Signed-in players get the persisted cross-session Tournament History from
  // the independent top-level sections below (populated by _loadHistory(), which
  // runs whether or not an import happened this session). The card inside
  // #results-section is the signed-out view: this import's tournaments only,
  // since nothing was saved.
  const freeCard = document.getElementById('free-tournament-history-card');
  if (data.saved) {
    if (freeCard) freeCard.classList.add('d-none');
  } else {
    if (freeCard) freeCard.classList.remove('d-none');
    renderTournaments(data.tournaments || []);
    _resetTournamentDetails();
  }
  if (data.history_expired_tournaments) {
    _showHistoryCapNotice(data.history_expired_tournaments);
  }
  updateTzHeaders();
  _updateExportGates();
  _renderPlayerExportAll();

  document.getElementById('results-section').classList.remove('d-none');
  document.getElementById('results-section').scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/* ── Shared hand table renderer ──────────────────────────── */

function renderHandsTable(hands, tbodyId, options = {}) {
  if (!isPro() && hands.length > FREE_HAND_LIMIT) {
    hands = hands.slice(-FREE_HAND_LIMIT);
  }
  const tbody = document.getElementById(tbodyId);
  if (!hands.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">No hands available</td></tr>';
    return;
  }

  const tz = currentTz();
  const showExport = !!options.showExport;
  const exportTid  = options.exportTid || '';   // if set, route export through storage endpoints

  tbody.innerHTML = hands.map(h => {
    const cards = (h.hole_cards || []).map(renderCard).join('');
    const copyBtn = h.hand_num
      ? `<button class="copy-hand-btn" onclick="copyHandId(this)" data-hand-num="${h.hand_num}" title="Hand ID: ${h.hand_num}">
           <svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
         </button>`
      : '';

    // Cols 5+6 differ between normal tables (Result, Net P/L) and export tables (Net P/L, Export)
    let cols56;
    if (showExport) {
      const hn = h.hand_num;
      const tid = exportTid;
      const exportBtns = !hn
        ? '<span class="text-muted">—</span>'
        // Signed out, every export needs an account first — so say that instead
        // of offering four buttons that all answer 401.
        : !isSignedIn()
        ? `<button class="btn export-icon-btn signin-export-btn" title="Sign in to export"
                   onclick="showSignInModal('Sign in to export your hands — it stays free.')">
             <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="var(--yellow)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
             <span>Sign in to export</span>
           </button>`
        : `<div class="d-flex gap-1 flex-wrap justify-content-center">
            <button class="btn export-icon-btn" data-platform="PokerTracker" title="Export PT4" onclick="exportHandFromRow('${hn}','PokerTracker',this,'${tid}')">
              <img src="https://www.google.com/s2/favicons?domain=pokertracker.com&sz=64" width="16" height="16" alt="PT">
            </button>
            <button class="btn export-icon-btn" data-platform="DriveHUD" title="Export DriveHUD" onclick="exportHandFromRow('${hn}','DriveHUD',this,'${tid}')">
              <img src="https://www.google.com/s2/favicons?domain=drivehud.com&sz=64" width="16" height="16" alt="DH">
            </button>
            <button class="btn export-icon-btn" data-platform="GTOWizard" title="Export GTO Wizard" onclick="exportHandFromRow('${hn}','GTOWizard',this,'${tid}')">
              <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 32 32"><rect width="32" height="32" rx="5" fill="#0f0f10"/><polyline points="4,8 9,24 16,13 23,24 28,8" fill="none" stroke="#3dff7a" stroke-width="3.2" stroke-linejoin="round" stroke-linecap="round"/></svg>
            </button>
            <button class="btn export-icon-btn" title="Export JSON" onclick="exportHandFromRow('${hn}','',this,'${tid}')">
              <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
            </button>
          </div>`;
      // Export mode: Net P/L (BB) | Export
      cols56 = `<td>${fmtProfitBB(h.profit, h.big_blind)}</td>
      <td class="text-center d-none d-lg-table-cell">${exportBtns}</td>`;
    } else {
      // Normal mode: Result | Net P/L (BB)
      cols56 = `<td class="d-none d-lg-table-cell">${resultBadge(h.result)}</td>
      <td>${fmtProfitBB(h.profit, h.big_blind)}</td>`;
    }

    // Column visibility mirrors the header priority in index.html: cards and
    // net p/l always survive, then position and street, and when/export/replay
    // drop off as the viewport narrows.
    return `<tr${h.hand_num ? ` data-hand-num="${h.hand_num}"` : ''}>
      <td class="d-none d-md-table-cell"><span class="hand-when-cell">${fmtHandDateTime(h.ts, tz)}${copyBtn}</span></td>
      <td class="no-wrap">${cards || '—'}</td>
      <td>${posBadge(h.position)}</td>
      <td>${streetBadge(h.last_street)}</td>
      ${cols56}
      <td class="d-none d-xl-table-cell">
        ${h.replay_url && h.replay_url !== '#'
          ? `<a class="replay-link" href="${h.replay_url}" target="_blank" rel="noopener" title="Watch replay">▶</a>`
          : '<span class="text-muted">—</span>'}
      </td>
    </tr>`;
  }).join('');
}

function renderRecentHands(hands)    { renderHandsTable(hands, 'recent-hands-tbody',  { showExport: true }); }
function renderRecentWonHands(hands) { renderHandsTable(hands, 'recent-won-tbody',     { showExport: true }); }

/* ── Table 2: Stats summary (preserved, not called by default) ── */

function statCard(label, value, colorClass) {
  return `<div class="col-6 col-sm-4 col-md-3 col-xl-2">
    <div class="stat-card">
      <div class="stat-value ${colorClass || ''}">${value}</div>
      <div class="stat-label">${label}</div>
    </div>
  </div>`;
}

function renderStats(s) {
  const grid = document.getElementById('stats-grid');
  const net  = s.net_profit   || 0;
  const bb   = s.bb_100       || 0;
  const win  = s.biggest_win  || 0;
  const loss = s.biggest_loss || 0;

  grid.innerHTML = [
    statCard('Hands Played',  s.total_hands || 0),
    statCard('VPIP',          (s.vpip_pct || 0) + '%'),
    statCard('PFR',           (s.pfr_pct  || 0) + '%'),
    statCard('Aggr. Factor',  s.af        || 0),
    statCard('WTSD',          (s.wtsd_pct || 0) + '%'),
    statCard('W$SD',          (s.wsd_pct  || 0) + '%'),
    statCard('BB / 100',      bb.toFixed(2),              bb  < 0 ? 'neg' : ''),
    statCard('Net Profit',    fmtProfitPlain(net),        net < 0 ? 'neg' : ''),
    statCard('Biggest Win',   '+' + win.toLocaleString(), ''),
    statCard('Biggest Loss',  '−' + Math.abs(loss).toLocaleString(), 'neg'),
  ].join('');
}

/* ── Game categorisation ─────────────────────────────────── */
// Mirrors hand_parser.classify_game on the server: only a real-money MTT is a
// tracked tournament. Play-money games (no club room name) and single-table
// games — cash tables and sit-and-gos — all belong to Cash & Play Money.
// `category` is computed server-side on every read; the is_mtt fallback only
// covers a response from a server that predates the split.
function _isTourneyGame(t) {
  return t && t.category ? t.category === 'tournament' : !!(t && t.is_mtt);
}

function _isCashOrPlayGame(t) {
  return !_isTourneyGame(t);
}

// Badge for one game: distinguishes why a game sits where it does, so a
// play-money MTT in the Cash & Play Money table doesn't read as a cash session.
function _gameTypeBadge(t) {
  if (_isTourneyGame(t))  return '<span class="badge bg-primary">MTT</span>';
  if (t && t.is_play_money) {
    return t.is_mtt
      ? '<span class="badge bg-secondary">Play MTT</span>'
      : '<span class="badge bg-secondary">Play</span>';
  }
  return '<span class="badge bg-secondary">Cash</span>';
}

/* ── Table 3: Tournaments ────────────────────────────────── */

function renderTournaments(tournaments) {
  const tbody = document.getElementById('tournaments-tbody');

  // Populate tourney strip
  const strip = document.getElementById('tourney-strip');
  if (strip) {
    const mttCount  = tournaments.filter(_isTourneyGame).length;
    const playCount = tournaments.filter(t => t.is_play_money).length;
    const satCount  = tournaments.filter(t => (t.room_name || '').toLowerCase().includes('sat')).length;
    const wonCount  = tournaments.filter(t => (t.net || 0) > 0).length;
    const items = [
      ['Games',      tournaments.length],
      ['MTT',        mttCount],
      ['Play money', playCount],
      ['Satellite',  satCount],
      ['Won',        wonCount],
    ];
    strip.innerHTML = items.map(([label, value]) =>
      `<span class="val-pill"><strong>${value}</strong><span class="val-pill-label">${label}</span></span>`
    ).join('<span class="val-sep">·</span>');
  }

  if (!tournaments.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">No tournaments detected</td></tr>';
    return;
  }

  const tz = currentTz();
  tbody.innerHTML = tournaments.map(t => {
    const typeBadge = _gameTypeBadge(t);
    const hasGraph  = !!(window._anonGraphs && window._anonGraphs[t.tourney_id]);
    // Signed out, the row itself is the way into the tournament's graph — the
    // data came down with the import, so there is nothing to fetch.
    const rowAttrs = hasGraph
      ? ` class="anon-tourney-row" role="button" tabindex="0"` +
        ` title="View this tournament's graph"` +
        ` onclick="_selectAnonTourneyDetail('${t.tourney_id}', this)"` +
        ` onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();_selectAnonTourneyDetail('${t.tourney_id}', this);}"`
      : '';

    return `<tr${rowAttrs}>
      <td style="white-space:nowrap"><small>${fmtDate(t.earliest_ts, tz)}</small></td>
      <td class="d-none d-sm-table-cell"><small>${t.room_name || '—'}</small></td>
      <td class="d-none d-lg-table-cell"><small class="text-muted">${fmtTime(t.earliest_ts, tz)}</small></td>
      <td class="d-none d-lg-table-cell"><small class="text-muted">${t.time_played || '—'}</small></td>
      <td class="d-none">${typeBadge}</td>
      <td class="text-center">${t.hands}</td>
      <td class="text-center export-col" style="vertical-align:middle">
        ${isSignedIn()
          ? `<div class="d-flex gap-2 flex-wrap justify-content-center">
              <button class="btn export-icon-btn" data-platform="PokerTracker" title="Export for PokerTracker" onclick="event.stopPropagation();exportTournament('${t.tourney_id}', this)">
                <img src="https://www.google.com/s2/favicons?domain=pokertracker.com&sz=64" width="22" height="22" alt="PT">
              </button>
              <button class="btn export-icon-btn" data-platform="DriveHUD" title="Export for DriveHUD" onclick="event.stopPropagation();exportTournament('${t.tourney_id}', this)">
                <img src="https://www.google.com/s2/favicons?domain=drivehud.com&sz=64" width="22" height="22" alt="DH">
              </button>
              <button class="btn export-icon-btn" data-platform="GTOWizard" title="Export for GTO Wizard" onclick="event.stopPropagation();exportTournament('${t.tourney_id}', this)">
                <svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 32 32"><rect width="32" height="32" rx="5" fill="#0f0f10"/><polyline points="4,8 9,24 16,13 23,24 28,8" fill="none" stroke="#3dff7a" stroke-width="3.2" stroke-linejoin="round" stroke-linecap="round"/></svg>
              </button>
              <button class="btn export-icon-btn" title="Export as JSON file" onclick="event.stopPropagation();exportTournamentJson('${t.tourney_id}', this)">
                <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
              </button>
            </div>`
          : _SIGNIN_TO_EXPORT_GATE
        }
      </td>
    </tr>`;
  }).join('');

}

// Signed-out export column: the buttons are shown but inert, because the offer
// is "sign in and these work", not "pay us".
const _SIGNIN_TO_EXPORT_GATE =
  `<div class="tourney-gate-wrap">
    <div class="tourney-gate-blur" aria-hidden="true">
      <div class="d-flex gap-2 flex-wrap justify-content-center">
        <button class="btn export-icon-btn" tabindex="-1" disabled>
          <img src="https://www.google.com/s2/favicons?domain=pokertracker.com&sz=64" width="22" height="22" alt="">
        </button>
        <button class="btn export-icon-btn" tabindex="-1" disabled>
          <img src="https://www.google.com/s2/favicons?domain=drivehud.com&sz=64" width="22" height="22" alt="">
        </button>
        <button class="btn export-icon-btn" tabindex="-1" disabled>
          <svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 32 32"><rect width="32" height="32" rx="5" fill="#0f0f10"/><polyline points="4,8 9,24 16,13 23,24 28,8" fill="none" stroke="#3dff7a" stroke-width="3.2" stroke-linejoin="round" stroke-linecap="round"/></svg>
        </button>
        <button class="btn export-icon-btn" tabindex="-1" disabled>
          <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
        </button>
      </div>
    </div>
    <div class="tourney-gate-overlay">
      <svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="var(--yellow)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
      <span class="tourney-gate-label">Sign in to export</span>
      <button class="tourney-gate-btn" onclick="event.stopPropagation();showSignInModal('Sign in to export your hands — it stays free.')">Sign in</button>
    </div>
  </div>`;

/** Banner for tournaments the free tier's 7-day window dropped from an import. */
function _showHistoryCapNotice(count) {
  const box = document.getElementById('history-cap-notice');
  if (!box) return;
  box.innerHTML =
    `${count} tournament${count === 1 ? '' : 's'} from this link ` +
    `${count === 1 ? 'is' : 'are'} older than ${FREE_HISTORY_DAYS} days and ` +
    `${count === 1 ? "wasn't" : "weren't"} saved. ` +
    `<button class="btn-link-inline" onclick="showUpgradeModal('history')">Upgrade to keep everything</button>`;
  box.classList.remove('d-none');
}

/* ── Export gate renderers ───────────────────────────────── */

const _EXPORT_ALL_BTNS_HTML =
  `<div class="export-btn-grid">` +
  `<button class="export-grid-btn" data-platform="PokerTracker" title="Export for PokerTracker" onclick="exportAllHands(this)">` +
  `<img src="https://www.google.com/s2/favicons?domain=pokertracker.com&sz=64" width="28" height="28" alt="PT"><span>Poker Tracker</span></button>` +
  `<button class="export-grid-btn" data-platform="DriveHUD" title="Export for DriveHUD" onclick="exportAllHands(this)">` +
  `<img src="https://www.google.com/s2/favicons?domain=drivehud.com&sz=64" width="28" height="28" alt="DH"><span>DriveHUD</span></button>` +
  `<button class="export-grid-btn" data-platform="GTOWizard" title="Export for GTO Wizard" onclick="exportAllHands(this)">` +
  `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 32 32"><rect width="32" height="32" rx="5" fill="#0f0f10"/><polyline points="4,8 9,24 16,13 23,24 28,8" fill="none" stroke="#3dff7a" stroke-width="3.2" stroke-linejoin="round" stroke-linecap="round"/></svg>` +
  `<span>GTO Wizard</span></button>` +
  `<button class="export-grid-btn" title="Export as JSON" onclick="exportAllHandsJson(this)">` +
  `<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>` +
  `<span>JSON File</span></button>` +
  `</div>`;

const _LOCK_ICON_SVG =
  `<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" ` +
  `stroke="var(--yellow)" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">` +
  `<rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>`;

// Signed-in-wall for the Export All Hands section — same visual treatment as the
// Pro-only wall, but the pitch is "sign in" rather than "upgrade".
const _SIGNIN_EXPORT_ALL_WRAP_HTML =
  `<div class="export-gate-wrap signin-export-all-wrap">` +
  `<div class="tourney-gate-blur" aria-hidden="true">${_EXPORT_ALL_BTNS_HTML}</div>` +
  `<div class="tourney-gate-overlay">` +
  _LOCK_ICON_SVG +
  `<span class="tourney-gate-label">Sign in to export</span>` +
  `<button class="tourney-gate-btn" onclick="showSignInModal('Sign in to export your hands — it stays free.')">Sign in</button>` +
  `</div></div>`;

/** Renders the Export All Hands container — real buttons for Pro, sign-in wall for anon, Pro upsell for signed-in free. */
function _renderExportAllSection() {
  const el = document.getElementById('export-all-container');
  if (!el) return;
  if (isPro()) {
    el.innerHTML = _EXPORT_ALL_BTNS_HTML;
  } else if (isSignedIn()) {
    el.innerHTML =
      `<div class="export-gate-wrap">` +
      `<div class="tourney-gate-blur" aria-hidden="true">${_EXPORT_ALL_BTNS_HTML}</div>` +
      `<div class="tourney-gate-overlay">` +
      _LOCK_ICON_SVG +
      `<span class="tourney-gate-label">Export All Hands — Pro only</span>` +
      `<button class="tourney-gate-btn" onclick="showUpgradeModal('export')">${_pricingCta()}</button>` +
      `</div></div>`;
  } else {
    el.innerHTML = _SIGNIN_EXPORT_ALL_WRAP_HTML;
  }
}

// The per-hand export counter that used to live here is gone with the
// client-side quota: the server owns the count, and the honest place to learn
// you're out is the response to the export you just asked for.

/**
 * Show/hide tier-gated elements based on current Pro status.
 *
 * FREE_ONLY_ELS ship with .tier-pending in the HTML so they are invisible at
 * first paint — otherwise a Pro user sees the Free-vs-Pro upsell flash on every
 * load until Firestore reports their tier. Clearing .tier-pending here is what
 * finally reveals them, so this must run on every auth/tier resolution path,
 * including the failure ones (see _resolveTierUI).
 */
function _applyTierVisibility() {
  const pro  = isPro();
  const anon = currentTier() === 'anon';
  FREE_ONLY_ELS.forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.style.display = pro ? 'none' : '';
    el.classList.remove('tier-pending');
  });
  PRO_ONLY_ELS.forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.style.display = pro ? '' : 'none';
    el.classList.remove('tier-pending');
  });
  ANON_ONLY_ELS.forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.style.display = anon ? '' : 'none';
    el.classList.remove('tier-pending');
  });
}

/**
 * Terminal "we know the tier now (or never will)" hook. Safe to call more than
 * once, and called from every Firebase bail-out path plus a watchdog timer so a
 * blocked SDK / failed config fetch can't leave the free-tier UI hidden forever.
 */
let _tierResolveTimer = setTimeout(() => _resolveTierUI(), 4000);
function _resolveTierUI() {
  if (_tierResolveTimer) { clearTimeout(_tierResolveTimer); _tierResolveTimer = null; }
  _updateExportGates();
}

/** Call after any state change that could affect export UI. */
function _updateExportGates() {
  _applyTierVisibility();
  _renderPlayerExportAll();
}

/* ── Tournament Summary ──────────────────────────────────── */

// tourney_ids touched by the most recent import — used to briefly highlight
// the corresponding rows in Tournament Summary / History after _loadHistory() renders.
let _justImportedTourneyIds = new Set();
let _importHighlightTimer = null;
const IMPORT_HIGHLIGHT_MS = 60000;

function _startImportHighlights(tourneyIds) {
  _justImportedTourneyIds = new Set(tourneyIds || []);
  if (_importHighlightTimer) clearTimeout(_importHighlightTimer);
  _importHighlightTimer = setTimeout(() => {
    _justImportedTourneyIds = new Set();
    _importHighlightTimer = null;
    document.querySelectorAll('.row-flash, .card-flash').forEach(el => {
      el.classList.remove('row-flash', 'card-flash');
    });
  }, IMPORT_HIGHLIGHT_MS);
}

function _dismissImportHighlight(el) {
  if (!el) return;
  el.classList.remove('row-flash', 'card-flash');
}

// ── TS / CGS filter + sort state ─────────────────────────────────────────────
let _allTournaments     = [];          // cached from _loadHistory
let _tsFilter           = 'week';      // 'all' | 'today' | 'week' | 'month'
let _tsSortCol          = 'last';      // null = default order
let _tsSortDir          = 'asc';

let _cgsFilter          = 'all';
let _cgsSortCol         = null;
let _cgsSortDir         = 'desc';

// ── Date-filter helper ────────────────────────────────────────────────────────
function _filterTournamentsByDate(tournaments, filter) {
  if (filter === 'all') return tournaments;
  const tz  = currentTz();
  const now = new Date();
  const todayStr = now.toLocaleDateString('en-CA', { timeZone: tz });

  if (filter === 'today') {
    return tournaments.filter(t => {
      if (!t.earliest_ts) return false;
      return new Date(t.earliest_ts * 1000).toLocaleDateString('en-CA', { timeZone: tz }) === todayStr;
    });
  }
  if (filter === 'week') {
    const cutoff = (now.getTime() - 7 * 86400000) / 1000;
    return tournaments.filter(t => (t.earliest_ts || 0) >= cutoff);
  }
  if (filter === 'month') {
    const cutoff = (now.getTime() - 30 * 86400000) / 1000;
    return tournaments.filter(t => (t.earliest_ts || 0) >= cutoff);
  }
  return tournaments;
}

// ── TS filter / sort ──────────────────────────────────────────────────────────
function _setTsFilter(f) {
  _tsFilter = f;
  const sel = document.getElementById('ts-filter-select');
  if (sel) sel.value = f;
  _renderTournamentSummary(_allTournaments);
}

function _sortTs(col) {
  if (_tsSortCol === col) {
    _tsSortDir = _tsSortDir === 'asc' ? 'desc' : 'asc';
  } else {
    _tsSortCol = col;
    _tsSortDir = 'desc';
  }
  _renderTournamentSummary(_allTournaments);
}

function _updateTsSortIcons(col, dir) {
  document.querySelectorAll('#tournament-summary-section .sort-th').forEach(th => {
    const icon = th.querySelector('.sort-icon');
    if (!icon) return;
    icon.textContent = th.dataset.col === col ? (dir === 'asc' ? ' ▲' : ' ▼') : '';
  });
}

// ── CGS filter / sort ─────────────────────────────────────────────────────────
function _setCgsFilter(f) {
  _cgsFilter = f;
  const sel = document.getElementById('cgs-filter-select');
  if (sel) sel.value = f;
  _renderCashGamesSummary(_allTournaments);
}

function _sortCgs(col) {
  if (_cgsSortCol === col) {
    _cgsSortDir = _cgsSortDir === 'asc' ? 'desc' : 'asc';
  } else {
    _cgsSortCol = col;
    _cgsSortDir = 'desc';
  }
  _renderCashGamesSummary(_allTournaments);
}

function _updateCgsSortIcons(col, dir) {
  document.querySelectorAll('#cash-games-summary-section .sort-th').forEach(th => {
    const icon = th.querySelector('.sort-icon');
    if (!icon) return;
    icon.textContent = th.dataset.col === col ? (dir === 'asc' ? ' ▲' : ' ▼') : '';
  });
}


// ── Group sort helper ─────────────────────────────────────────────────────────
function _sortGroups(rows, col, dir) {
  if (!col) return rows;
  const sign = dir === 'asc' ? 1 : -1;
  return [...rows].sort((a, b) => {
    let av, bv;
    switch (col) {
      case 'name':     av = a[0].toLowerCase(); bv = b[0].toLowerCase(); break;
      case 'events':   av = a[1].length; bv = b[1].length; break;
      case 'type':     av = a[1].some(_isTourneyGame) ? 1 : 0; bv = b[1].some(_isTourneyGame) ? 1 : 0; break;
      case 'avgHands': av = a[1].reduce((s, t) => s + (t.hands || 0), 0) / a[1].length;
                       bv = b[1].reduce((s, t) => s + (t.hands || 0), 0) / b[1].length; break;
      case 'avgDur':   av = a[1].reduce((s, t) => s + (t.duration_secs || 0), 0) / a[1].length;
                       bv = b[1].reduce((s, t) => s + (t.duration_secs || 0), 0) / b[1].length; break;
      case 'handsHr': {
        const aDurHr = a[1].reduce((s, t) => s + (t.duration_secs || 0), 0) / 3600;
        const bDurHr = b[1].reduce((s, t) => s + (t.duration_secs || 0), 0) / 3600;
        av = aDurHr > 0 ? a[1].reduce((s, t) => s + (t.hands || 0), 0) / aDurHr : 0;
        bv = bDurHr > 0 ? b[1].reduce((s, t) => s + (t.hands || 0), 0) / bDurHr : 0;
        break;
      }
      case 'last':     av = Math.max(...a[1].map(t => t.earliest_ts || 0));
                       bv = Math.max(...b[1].map(t => t.earliest_ts || 0)); break;
      default: return 0;
    }
    if (av < bv) return -sign;
    if (av > bv) return  sign;
    return 0;
  });
}

// ── Cash & Play Money Summary ─────────────────────────────────────────────────
// Play-money games carry no room name, so grouping them by room_name would pile
// every one of them into a single "(Unknown)" row. Split them by table format
// instead, which is the only distinction that survives: play-money MTTs and
// play-money sit-and-gos get a row each.
function _cashGroupName(t) {
  if (t && t.is_play_money) {
    return t.is_mtt ? 'Play Money — MTT' : 'Play Money — Sit & Go';
  }
  return (t && t.room_name) || '(Unknown)';
}

function _renderCashGamesSummary(tournaments) {
  const cashOnly = (tournaments || []).filter(_isCashOrPlayGame);
  const filtered = _filterTournamentsByDate(cashOnly, _cgsFilter);

  const cgsSection = document.getElementById('cash-games-summary-section');
  if (cgsSection) cgsSection.classList.remove('d-none');
  const cgsdSection = document.getElementById('cash-game-detail-section');
  // Hide session detail when filter produces no results (no sessions to click)
  if (cgsdSection) cgsdSection.classList.toggle('d-none', !filtered.length);

  // Stats strip
  const strip = document.getElementById('cgs-strip');
  if (strip) {
    const items = [['Sessions', filtered.length], ['Total Hands', filtered.reduce((s, t) => s + (t.hands || 0), 0)]];
    strip.innerHTML = items.map(([label, value]) =>
      `<span class="val-pill"><strong>${value}</strong><span class="val-pill-label">${label}</span></span>`
    ).join('<span class="val-sep">·</span>');
  }

  const tbody = document.getElementById('cgs-summary-tbody');
  if (!tbody) return;

  const byRoom = {};
  for (const t of filtered) {
    const key = _cashGroupName(t);
    if (!byRoom[key]) byRoom[key] = [];
    byRoom[key].push(t);
  }

  let rows = Object.entries(byRoom).sort((a, b) => b[1].length - a[1].length);
  rows = _sortGroups(rows, _cgsSortCol, _cgsSortDir);
  _updateCgsSortIcons(_cgsSortCol, _cgsSortDir);

  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">No cash or play money data in selected range.</td></tr>';
    return;
  }

  const tz = currentTz();
  tbody.innerHTML = rows.map(([name, entries], idx) => {
    const rowId       = `cgs-${idx}`;
    const count       = entries.length;
    const totalHands  = entries.reduce((s, t) => s + (t.hands || 0), 0);
    const totalDurHr  = entries.reduce((s, t) => s + (t.duration_secs || 0), 0) / 3600;
    const handsPerHr  = totalDurHr > 0 ? (totalHands / totalDurHr) : 0;
    const avgHands    = Math.round(totalHands / count);
    const avgDurSecs  = entries.reduce((s, t) => s + (t.duration_secs || 0), 0) / count;
    const avgVpip     = (entries.reduce((s, t) => s + (t.vpip_pct || 0), 0) / count).toFixed(1);
    const avgPfr      = (entries.reduce((s, t) => s + (t.pfr_pct  || 0), 0) / count).toFixed(1);
    const lastTs      = Math.max(...entries.map(t => t.earliest_ts || 0));
    const lastDate    = lastTs ? new Date(lastTs * 1000).toLocaleDateString('en-GB',
        { day: 'numeric', month: 'short', year: '2-digit', timeZone: tz }) : '—';

    const sortedEntries = [...entries].sort((a, b) => (b.earliest_ts || 0) - (a.earliest_ts || 0));
    const isRowNew = entries.some(t => _justImportedTourneyIds.has(t.tourney_id));
    const eventCards = sortedEntries.map(t => {
      const d = t.earliest_ts ? new Date(t.earliest_ts * 1000).toLocaleDateString('en-GB',
          { day: 'numeric', month: 'short', year: '2-digit', timeZone: tz }) : '—';
      const evDurHr = (t.duration_secs || 0) / 3600;
      const evPerHr = evDurHr > 0 ? (t.hands || 0) / evDurHr : 0;
      const isCardNew = _justImportedTourneyIds.has(t.tourney_id);
      return `<div class="tsum-event-card${isCardNew ? ' card-flash' : ''}" data-tid="${t.tourney_id}" onclick="event.stopPropagation();_dismissImportHighlight(this);_selectCgsdDetail('${t.tourney_id}', this)">
        <div class="tsum-event-top">
          <span class="tsum-event-date">${d}</span>
          <span class="tsum-stat-pill">${fmtTime(t.earliest_ts, tz)}</span>
          <span class="tsum-stat-pill">${_fmtDuration(t.duration_secs)}</span>
        </div>
        <div class="tsum-event-stats">${_tsumStatPills(t.hands || 0, t.vpip_pct || 0, t.pfr_pct || 0, evPerHr)}</div>
        <div class="tsum-event-actions">${_TSUM_EXPORT_ICONS(t.tourney_id)}</div>
      </div>`;
    }).join('');

    return `<tr class="tsum-summary-row${isRowNew ? ' row-flash' : ''}" onclick="_dismissImportHighlight(this);_toggleCgsDetail('${rowId}')">
      <td>
        <svg class="tsum-chevron" id="${rowId}-chevron" xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
        <small>${name}</small>
      </td>
      <td class="text-center">${count}</td>
      <td class="text-center d-none d-md-table-cell">${Math.max(...entries.map(t => t.max_players || 0)) || '—'}</td>
      <td class="text-center d-none d-md-table-cell">${avgHands}</td>
      <td class="text-center d-none d-md-table-cell">${_fmtDuration(avgDurSecs)}</td>
      <td class="text-center d-none d-lg-table-cell">${handsPerHr.toFixed(1)}</td>
      <td class="text-center"><small>${lastDate}</small></td>
    </tr>
    <tr class="tsum-detail-row">
      <td colspan="7">
        <div class="tsum-detail-wrap" id="${rowId}-detail">
          <div class="tsum-event-grid">${eventCards}</div>
        </div>
      </td>
    </tr>`;
  }).join('');
}

function _toggleCgsDetail(rowId) {
  _toggleTsumDetail('cgs-summary-tbody', rowId);
}

// ── Cash & Play Money Session Details ─────────────────────────────────────────
let _selectedCgsId = null;

function _resetCgsd() {
  const tbody = document.getElementById('cgsd-tbody');
  if (tbody) tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">Select a cash or play money session above to view its hands.</td></tr>';
  const hint = document.getElementById('cgsd-hint');
  if (hint) hint.textContent = '';
  document.querySelectorAll('.tsum-event-card.selected').forEach(c => c.classList.remove('selected'));
  _selectedCgsId = null;
}

async function _selectCgsdDetail(tid, cardEl) {
  if (cardEl && cardEl.classList.contains('selected')) {
    _resetCgsd();
    return;
  }
  document.querySelectorAll('#cash-games-summary-section .tsum-event-card.selected').forEach(c => c.classList.remove('selected'));
  if (cardEl) cardEl.classList.add('selected');
  _selectedCgsId = tid;

  const tbody   = document.getElementById('cgsd-tbody');
  const hint    = document.getElementById('cgsd-hint');
  const section = document.getElementById('cash-game-detail-section');
  if (tbody) tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">Loading hands…</td></tr>';
  if (section) section.classList.remove('d-none');

  if (!_currentUser) return;
  const token = await _currentUser.getIdToken().catch(() => null);
  if (!token) { _resetCgsd(); return; }

  try {
    const r = await fetch(`/api/tournaments/${tid}/hands`, { headers: { 'Authorization': `Bearer ${token}` } });
    if (_selectedCgsId !== tid) return;
    if (!r.ok) {
      if (tbody) tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">Could not load hands.</td></tr>';
      return;
    }
    const data  = await r.json();
    const hands = data.hands || [];
    renderHandsTable(hands, 'cgsd-tbody', { showExport: true, exportTid: tid });
    if (hint) {
      const dateLabel = cardEl ? (cardEl.querySelector('.tsum-event-date')?.textContent || '') : '';
      hint.textContent = `${hands.length} hand${hands.length !== 1 ? 's' : ''}${dateLabel ? ' · ' + dateLabel : ''}`;
    }
    if (section) section.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    if (tbody) tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">Could not load hands.</td></tr>';
  }
}

// ── Per-row hand export (TD + CGSD) ──────────────────────────────────────────
function exportHandFromRow(handNum, platform, btn, tid) {
  if (!_currentUser) {
    showSignInModal('Sign in to export this hand — it takes a few seconds and it stays free.');
    return;
  }
  const isJson = !platform;
  // Every hand export goes through the tournament it belongs to: the server no
  // longer keeps the imported session in memory. The id is in the gameid when
  // the caller didn't pass one (rows rendered from a fresh import).
  const tourneyId = tid || _tidFromHandId(handNum);
  if (!tourneyId) {
    _rowExportStatus(btn, 'err', 'Could not tell which tournament this hand belongs to', 6000);
    return;
  }
  const endpoint = isJson
    ? `/api/tournaments/${tourneyId}/export/json/hand`
    : `/api/tournaments/${tourneyId}/export/hand`;
  _downloadExport(endpoint, {
    kind: 'hand',
    body: { hand_id: handNum, platform },
    fallbackName: `hand_export.${isJson ? 'json' : 'txt'}`,
  }, btn);
}

// ── Player badge: Export All (moves here from removed section) ────────────────
function _renderPlayerExportAll() {
  const wrap = document.getElementById('player-export-all');
  const btns = document.getElementById('player-export-all-btns');
  if (!wrap || !btns) return;
  if (!window._lastData) { wrap.classList.add('d-none'); return; }
  if (isPro()) {
    btns.innerHTML = _EXPORT_ALL_BTNS_HTML;
  } else if (isSignedIn()) {
    btns.innerHTML =
      `<div class="export-gate-wrap" style="min-height:auto">` +
      `<div class="tourney-gate-blur" aria-hidden="true">${_EXPORT_ALL_BTNS_HTML}</div>` +
      `<div class="tourney-gate-overlay">` +
      _LOCK_ICON_SVG +
      `<span class="tourney-gate-label">Pro only</span>` +
      `<button class="tourney-gate-btn" onclick="showUpgradeModal('export')">${_pricingCta()}</button>` +
      `</div></div>`;
  } else {
    btns.innerHTML = _SIGNIN_EXPORT_ALL_WRAP_HTML;
  }
  wrap.classList.remove('d-none');
}

/**
 * Drop tournaments outside a free account's history window.
 *
 * A tournament with no earliest_ts is kept — matching the server, where "we
 * can't date it" is not treated as "it's old".
 */
function _withinHistoryWindow(tournaments) {
  if (isPro()) return tournaments;
  const cutoff = (Date.now() - FREE_HISTORY_DAYS * 86400000) / 1000;
  return (tournaments || []).filter(t => t.earliest_ts == null || t.earliest_ts >= cutoff);
}

function _fmtDuration(secs) {
  if (!secs || secs < 0) return '—';
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

/**
 * Persisted history for any signed-in player.
 *
 * Free accounts see the last FREE_HISTORY_DAYS days of it — the server already
 * filters, and the filter below is the belt to that pair of braces: it keeps an
 * expired tournament off the page even if a stale response or a cached payload
 * still carries one.
 */
async function _loadHistory() {
  if (!_currentUser) return;
  const token = await _currentUser.getIdToken().catch(() => null);
  if (!token) return;
  const tsSection  = document.getElementById('tournament-summary-section');
  const tdSection  = document.getElementById('tournament-history-pro-section');
  const cgsSection = document.getElementById('cash-games-summary-section');
  const cgsdSection= document.getElementById('cash-game-detail-section');
  try {
    const r = await fetch('/api/tournaments', { headers: { 'Authorization': `Bearer ${token}` } });
    if (!r.ok) return;
    const data = await r.json();
    const tournaments = _withinHistoryWindow(data.tournaments || []);
    _allTournaments = tournaments;

    if (!tournaments.length) {
      [tsSection, tdSection, cgsSection, cgsdSection].forEach(el => el && el.classList.add('d-none'));
      return;
    }

    const hasMtt  = tournaments.some(_isTourneyGame);
    const hasCash = tournaments.some(_isCashOrPlayGame);

    if (hasMtt) {
      _renderTournamentSummary(tournaments);
      _resetTournamentDetails();
      if (tsSection) tsSection.classList.remove('d-none');
      if (tdSection) tdSection.classList.remove('d-none');
    } else {
      if (tsSection) tsSection.classList.add('d-none');
      if (tdSection) tdSection.classList.add('d-none');
    }

    if (hasCash) {
      _renderCashGamesSummary(tournaments);
      _resetCgsd();
    } else {
      if (cgsSection) cgsSection.classList.add('d-none');
      if (cgsdSection) cgsdSection.classList.add('d-none');
    }
  } catch (e) { console.warn('History load failed:', e); }
}

const _TSUM_ICON_HANDS   = `<svg class="tsum-stat-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="6" width="11" height="15" rx="1.5" transform="rotate(-12 8.5 13.5)"/><rect x="10" y="4" width="11" height="15" rx="1.5"/></svg>`;
const _TSUM_ICON_PERCENT = `<svg class="tsum-stat-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="6" cy="6" r="3"/><circle cx="18" cy="18" r="3"/><line x1="19" y1="5" x2="5" y2="19"/></svg>`;
const _TSUM_ICON_CLOCK    = `<svg class="tsum-stat-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15.5 14"/></svg>`;

function _tsumStatPills(hands, vpip, pfr, perHr) {
  const vpipR = Math.round(vpip);
  const pfrR  = Math.round(pfr);
  return `
    <span class="tsum-stat-item" title="Total hands">
      ${_TSUM_ICON_HANDS}
      <span class="tsum-stat-pill tsum-stat-stacked">
        <span class="tsum-stat-full">${hands} hands</span>
        <span class="tsum-stat-short">${hands}</span>
      </span>
    </span>
    <span class="tsum-stat-item" title="VPIP / PFR">
      ${_TSUM_ICON_PERCENT}
      <span class="tsum-stat-pill tsum-stat-stacked">
        <span class="tsum-stat-full">${vpip.toFixed(1)}% / ${pfr.toFixed(1)}%</span>
        <span class="tsum-stat-short">${vpipR}/${pfrR}</span>
      </span>
    </span>
    <span class="tsum-stat-item" title="Hands per hour">
      ${_TSUM_ICON_CLOCK}
      <span class="tsum-stat-pill tsum-stat-stacked">
        <span class="tsum-stat-full">${perHr.toFixed(1)}/hr</span>
        <span class="tsum-stat-short">${perHr.toFixed(1)}</span>
      </span>
    </span>`;
}

const _TSUM_EXPORT_ICONS = (tourneyId) => `
  <button class="btn export-icon-btn" data-platform="PokerTracker" title="Export for PokerTracker" onclick="event.stopPropagation();exportPersistedTournament('${tourneyId}', this)">
    <img src="https://www.google.com/s2/favicons?domain=pokertracker.com&sz=64" width="16" height="16" alt="PT">
  </button>
  <button class="btn export-icon-btn" data-platform="DriveHUD" title="Export for DriveHUD" onclick="event.stopPropagation();exportPersistedTournament('${tourneyId}', this)">
    <img src="https://www.google.com/s2/favicons?domain=drivehud.com&sz=64" width="16" height="16" alt="DH">
  </button>
  <button class="btn export-icon-btn" data-platform="GTOWizard" title="Export for GTO Wizard" onclick="event.stopPropagation();exportPersistedTournament('${tourneyId}', this)">
    <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 32 32"><rect width="32" height="32" rx="5" fill="#0f0f10"/><polyline points="4,8 9,24 16,13 23,24 28,8" fill="none" stroke="#3dff7a" stroke-width="3.2" stroke-linejoin="round" stroke-linecap="round"/></svg>
  </button>
  <button class="btn export-icon-btn" title="Export as JSON file" onclick="event.stopPropagation();exportPersistedTournamentJson('${tourneyId}', this)">
    <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/></svg>
  </button>`;

function _renderTournamentSummary(tournaments) {
  const tbody = document.getElementById('tourney-summary-tbody');
  if (!tbody) return;

  // Real-money MTTs only (cash, sit-and-gos and play money live in the
  // Cash & Play Money table), then apply date filter
  const mttOnly = (tournaments || []).filter(_isTourneyGame);
  const filtered = _filterTournamentsByDate(mttOnly, _tsFilter);

  // Pills strip
  const strip = document.getElementById('tourney-strip-pro');
  if (strip) {
    const satCount = filtered.filter(t => (t.room_name || '').toLowerCase().includes('sat')).length;
    const pkoCount = filtered.filter(t => { const r = (t.room_name || '').toLowerCase(); return (r.includes('pko') || r.includes('mko')) && !r.includes('sat'); }).length;
    const items = [['Tourneys', filtered.length], ['Satellite', satCount], ['PKO', pkoCount]];
    strip.innerHTML = items.map(([label, value]) =>
      `<span class="val-pill"><strong>${value}</strong><span class="val-pill-label">${label}</span></span>`
    ).join('<span class="val-sep">·</span>');
  }

  const byRoom = {};
  for (const t of filtered) {
    const key = t.room_name || '(Unknown)';
    if (!byRoom[key]) byRoom[key] = [];
    byRoom[key].push(t);
  }

  let rows = Object.entries(byRoom).sort((a, b) => b[1].length - a[1].length);
  rows = _sortGroups(rows, _tsSortCol, _tsSortDir);
  _updateTsSortIcons(_tsSortCol, _tsSortDir);

  if (!rows.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="text-center py-4 text-muted">No tournament data in selected range.</td></tr>';
    return;
  }

  const tz = currentTz();
  tbody.innerHTML = rows.map(([name, entries], idx) => {
    const rowId       = `tsum-${idx}`;
    const count       = entries.length;
    const isMtt       = entries.some(_isTourneyGame);
    const totalHands  = entries.reduce((s, t) => s + (t.hands || 0), 0);
    const totalDurHr  = entries.reduce((s, t) => s + (t.duration_secs || 0), 0) / 3600;
    const handsPerHr  = totalDurHr > 0 ? (totalHands / totalDurHr) : 0;
    const avgHands    = Math.round(totalHands / count);
    const avgDurSecs  = entries.reduce((s, t) => s + (t.duration_secs || 0), 0) / count;
    const avgVpip     = (entries.reduce((s, t) => s + (t.vpip_pct || 0), 0) / count).toFixed(1);
    const avgPfr      = (entries.reduce((s, t) => s + (t.pfr_pct  || 0), 0) / count).toFixed(1);
    const lastTs      = Math.max(...entries.map(t => t.earliest_ts || 0));
    const lastDate    = lastTs ? new Date(lastTs * 1000).toLocaleDateString('en-GB',
        { day: 'numeric', month: 'short', year: '2-digit', timeZone: tz }) : '—';

    const sortedEntries = [...entries].sort((a, b) => (a.earliest_ts || 0) - (b.earliest_ts || 0));
    const isRowNew = entries.some(t => _justImportedTourneyIds.has(t.tourney_id));
    const eventCards = sortedEntries.map(t => {
      const d = t.earliest_ts ? new Date(t.earliest_ts * 1000).toLocaleDateString('en-GB',
          { day: 'numeric', month: 'short', year: '2-digit', timeZone: tz }) : '—';
      const evDurHr = (t.duration_secs || 0) / 3600;
      const evPerHr = evDurHr > 0 ? (t.hands || 0) / evDurHr : 0;
      const isCardNew = _justImportedTourneyIds.has(t.tourney_id);
      return `<div class="tsum-event-card${isCardNew ? ' card-flash' : ''}" data-tid="${t.tourney_id}" onclick="event.stopPropagation();_dismissImportHighlight(this);_selectTourneyDetail('${t.tourney_id}', this)">
        <div class="tsum-event-top">
          <span class="tsum-event-date">${d}</span>
          <span class="tsum-stat-pill" title="Sit down time">${fmtTime(t.earliest_ts, tz)}</span>
          <span class="tsum-stat-pill" title="Time played">${_fmtDuration(t.duration_secs)}</span>
        </div>
        <div class="tsum-event-stats">${_tsumStatPills(t.hands || 0, t.vpip_pct || 0, t.pfr_pct || 0, evPerHr)}</div>
        <div class="tsum-event-actions">${_TSUM_EXPORT_ICONS(t.tourney_id)}</div>
      </div>`;
    }).join('');

    return `<tr class="tsum-summary-row${isRowNew ? ' row-flash' : ''}" onclick="_dismissImportHighlight(this);_toggleTourneyDetail('${rowId}')">
      <td>
        <svg class="tsum-chevron" id="${rowId}-chevron" xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
        <small>${name}</small>
      </td>
      <td class="text-center">${count}</td>
      <td class="text-center d-none">${isMtt ? '<span class="badge bg-primary">MTT</span>' : '<span class="badge bg-secondary">MTT</span>'}</td>
      <td class="text-center d-none d-md-table-cell">${Math.max(...entries.map(t => t.max_players || 0)) || '—'}</td>
      <td class="text-center d-none d-md-table-cell">${avgHands}</td>
      <td class="text-center d-none d-md-table-cell">${_fmtDuration(avgDurSecs)}</td>
      <td class="text-center d-none d-lg-table-cell">${handsPerHr.toFixed(1)}</td>
      <td class="text-center"><small>${lastDate}</small></td>
    </tr>
    <tr class="tsum-detail-row">
      <td colspan="8">
        <div class="tsum-detail-wrap" id="${rowId}-detail">
          <div class="tsum-event-grid">${eventCards}</div>
        </div>
      </td>
    </tr>`;
  }).join('');
}

function _toggleTourneyDetail(rowId) {
  _toggleTsumDetail('tourney-summary-tbody', rowId);
}

// Accordion toggle shared by the Tournament Summary and Cash & Play Money
// tables: opening a row's detail closes any other open row in the same tbody.
function _toggleTsumDetail(tbodyId, rowId) {
  const tbody   = document.getElementById(tbodyId);
  const detail  = document.getElementById(`${rowId}-detail`);
  const chevron = document.getElementById(`${rowId}-chevron`);
  if (!detail) return;
  const opening = !detail.classList.contains('tsum-detail-open');
  if (tbody && opening) {
    tbody.querySelectorAll('.tsum-detail-wrap.tsum-detail-open').forEach(el => {
      if (el !== detail) el.classList.remove('tsum-detail-open');
    });
    tbody.querySelectorAll('.tsum-chevron-open').forEach(el => {
      if (el !== chevron) el.classList.remove('tsum-chevron-open');
    });
  }
  detail.classList.toggle('tsum-detail-open');
  if (chevron) chevron.classList.toggle('tsum-chevron-open');
}

/* ── Tournament Progress Graph ──────────────────────────────────────────── */

let _tgChart      = null;
let _tgYMode      = 'both';   // 'both' | 'chips' | 'bb'
let _tgXMode      = 'time';   // 'time' | 'level'
let _tgPlayedOnly = false;    // when true, crop to played hands only
let _tgState      = null;     // computed chart state shared across toggle updates
let _tgPinned     = null;     // dataset index of the hand pinned in the hover card
let _tgCardBox    = null;     // last placed card rect, for drawing its connector

// Hero voluntarily put chips in preflop. `last_street` already encodes this:
// 'Pre' is a pure fold, every other value means hero played the hand.
const _tgIsVpip = h => !!h && h.last_street !== 'Pre';

// Played hands losing less than this many BB are dropped from the graph — they
// are blind-ish folds, not decisions, and they crowd out the spots worth
// finding. (`_is_vpip` over-counts action types 12/13, see
// docs/pppoker-action-model.md, so a chunk of these are -0.5BB SB folds that
// were never really voluntary.) Hovering one still gets you a full card; it
// just no longer earns a dot or a place in the snap/step order.
const _TG_DEAD_PL_BB = -0.65;
function _tgIsNotable(h) {
  if (!_tgIsVpip(h)) return false;
  if (!h.big_blind || !h.profit) return true;   // no P/L to judge, or break-even
  const bb = h.profit / h.big_blind;
  return !(bb < 0 && bb > _TG_DEAD_PL_BB);
}

// How far (px) the cursor may sit from a played hand and still snap to it.
// A 5h graph packs hands 1.5-4px apart, but folds are ~70% of them — snapping
// to the played ones only is what makes a single hand reachable with a mouse.
const TG_SNAP_PX = 14;

// Vertical dead-zone (px): folds only fire the hand-card when the cursor is
// this close to the curve. Keeps the Stage line at the top of the plot readable
// without stripping the "scrub anywhere for a stack reading" feature — snapped
// played hands ignore this and always show, since aiming at a marker is intent.
const TG_VDEAD_PX = 50;

// Tournament configs: ITM bubble / expected-end hours from tournament start
const _TG_CFGS = {
  'DEEP FREEZE':  { itmH: 4.0, endH: 5.5, lateRegLevels: 14, levelDurRebuyMin: null, levelDurMin: 12 },
  'CRAZY 2':      { itmH: 3.0, endH: 4.0, lateRegLevels: 12, levelDurRebuyMin: 12,   levelDurMin: 10 },
  'LUCKY DAY':    { itmH: 3.0, endH: 4.0, lateRegLevels: 11, levelDurRebuyMin: 10,   levelDurMin:  8 },
  'EAST PKO SAT': { itmH: 3.0, endH: 4.0, lateRegLevels: 10, levelDurRebuyMin:  6,   levelDurMin:  5 },
  'TEXAS':        { itmH: 3.0, endH: 4.0, lateRegLevels: 11, levelDurRebuyMin: 10,   levelDurMin: 10 },
  'MINI':         { itmH: 4.0, endH: 5.5, lateRegLevels: 12, levelDurRebuyMin:  6,   levelDurMin:  6 },
};
const _TG_CFG_DEFAULT = { itmH: 4.0, endH: 5.5, lateRegLevels: 12, levelDurRebuyMin: 12, levelDurMin: 10 };

// BB → Level lookup (base levels 1-51 + Crazy 2 / Deep Freeze extra 52-68)
const _TG_BB_LVL = (() => {
  const pairs = [
    [1,50],[2,100],[3,200],[4,300],[5,400],[6,500],[7,600],[8,800],
    [9,1000],[10,1200],[11,1600],[12,2000],[13,2400],[14,3000],[15,4000],
    [16,5000],[17,6000],[18,8000],[19,10000],[20,12000],[21,16000],[22,20000],
    [23,24000],[24,30000],[25,40000],[26,50000],[27,60000],[28,80000],
    [29,100000],[30,120000],[31,160000],[32,200000],[33,240000],[34,300000],
    [35,400000],[36,500000],[37,600000],[38,800000],[39,1000000],[40,1200000],
    [41,1600000],[42,2000000],[43,3000000],[44,4000000],[45,5000000],
    [46,6000000],[47,8000000],[48,10000000],[49,12000000],[50,16000000],
    [51,20000000],
    [52,30000000],[53,40000000],[54,60000000],[55,80000000],[56,100000000],
    [57,120000000],[58,160000000],[59,200000000],[60,300000000],[61,400000000],
    [62,600000000],[63,800000000],[64,1000000000],[65,1200000000],
    [66,1600000000],[67,2000000000],[68,3000000000],
  ];
  const m = {};
  for (const [lvl, bb] of pairs) m[bb] = lvl;
  return m;
})();

// Lucky Day BB → Level (60-level override structure)
const _TG_LUCKY_BB_LVL = (() => {
  const bbs = [
    100,200,300,400,600,800,1000,1200,1600,2000,2400,3000,4000,5000,
    6000,8000,10000,12000,16000,20000,24000,30000,40000,50000,60000,
    80000,100000,120000,160000,200000,240000,300000,400000,500000,
    600000,800000,1000000,1200000,1600000,2000000,3000000,4000000,
    5000000,6000000,8000000,10000000,12000000,16000000,20000000,
    24000000,30000000,40000000,50000000,60000000,80000000,100000000,
    120000000,160000000,200000000,240000000,
  ];
  const m = {};
  bbs.forEach((bb, i) => { m[bb] = i + 1; });
  return m;
})();

function _tgGetCfg(roomName) {
  const up = (roomName || '').toUpperCase();
  for (const [key, cfg] of Object.entries(_TG_CFGS)) {
    if (up.includes(key)) return cfg;
  }
  return _TG_CFG_DEFAULT;
}

function _tgInferLevel(bigBlind, roomName, chipStack) {
  if (!bigBlind) return null;
  const isLucky = (roomName || '').toUpperCase().includes('LUCKY DAY');
  const map = isLucky ? _TG_LUCKY_BB_LVL : _TG_BB_LVL;
  const direct = map[bigBlind] || null;
  const scaled = map[Math.round(bigBlind / 100)] || null;
  // Both candidates can exist (the table contains values 100x apart); disambiguate
  // using the BB count implied by the hero's stack — a real tournament BB count
  // is almost always in the 1-300 range, so whichever candidate lands there wins.
  if (direct && scaled && chipStack) {
    const bbDirect = chipStack / bigBlind;
    const bbScaled = chipStack / (bigBlind / 100);
    const plausible = v => v >= 1 && v <= 300;
    if (plausible(bbDirect) && !plausible(bbScaled)) return direct;
    if (plausible(bbScaled) && !plausible(bbDirect)) return scaled;
  }
  return direct || scaled || null;
}

function _tgFmtElapsed(secs) {
  if (secs < 0) return '-' + _tgFmtElapsed(-secs);
  const h = Math.floor(secs / 3600);
  const m = Math.floor((secs % 3600) / 60);
  return h > 0 ? `${h}h ${String(m).padStart(2, '0')}m` : `${m}m`;
}

function _tgFmtK(n) {
  if (n == null) return '—';
  const a = Math.abs(n);
  if (a >= 1e9)  return (n / 1e9).toFixed(2).replace(/\.?0+$/, '') + 'B';
  if (a >= 1e6)  return (n / 1e6).toFixed(2).replace(/\.?0+$/, '') + 'M';
  if (a >= 1000) return (n / 1000).toFixed(1).replace(/\.?0+$/, '') + 'K';
  return n.toLocaleString();
}

// Custom Chart.js plugin — draws dashed vertical reference lines; labels show on hover
const _TG_REFLINES_PLUGIN = {
  id: 'tgRefLines',
  // Track cursor position so labels only render for the line being hovered.
  afterEvent(chart, args) {
    const e = args.event;
    const prev = chart._tgHoverX;
    if (e.type === 'mousemove')      chart._tgHoverX = e.x;
    else if (e.type === 'mouseout')  chart._tgHoverX = null;
    if (chart._tgHoverX !== prev) args.changed = true;
  },
  afterDraw(chart) {
    const lines = chart.config.options.tgRefLines;
    if (!lines || !lines.length) return;
    const { ctx, chartArea: ca, scales: { x } } = chart;
    if (!x) return;
    const hoverX = chart._tgHoverX;
    ctx.save();
    ctx.font = "10px 'Exo 2', sans-serif";
    for (const { secs, color, label } of lines) {
      const px = x.getPixelForValue(secs);
      if (px < ca.left || px > ca.right) continue;
      ctx.beginPath();
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.setLineDash([5, 4]);
      ctx.moveTo(px, ca.top);
      ctx.lineTo(px, ca.bottom);
      ctx.stroke();
      ctx.setLineDash([]);
      // Only draw the label box when the cursor is near this line.
      if (hoverX == null || Math.abs(hoverX - px) > 8) continue;
      const parts = label.split('\n');
      const tw = Math.max(...parts.map(l => ctx.measureText(l).width));
      const bh = parts.length * 14 + 6;
      const bw = tw + 10;
      const bx = Math.min(px + 4, ca.right - bw - 2);
      const by = ca.top + 5;
      ctx.fillStyle = 'rgba(13,17,23,0.88)';
      if (ctx.roundRect) { ctx.beginPath(); ctx.roundRect(bx, by, bw, bh, 3); ctx.fill(); }
      else { ctx.fillRect(bx, by, bw, bh); }
      ctx.fillStyle = color;
      parts.forEach((l, i) => ctx.fillText(l, bx + 5, by + 13 + i * 14));
    }
    ctx.restore();
  },
};

// Is a scale currently drawn? Chart.js exposes this as options.display — a bare
// `scale.display` is undefined, which silently disables anything guarded on it.
const _tgScaleOn = sc => !!(sc && (sc._isVisible ? sc._isVisible() : sc.options?.display));

// Custom Chart.js plugin — draws ring + icon markers for biggest win/loss/bust
const _TG_MARKERS_PLUGIN = {
  id: 'tgMarkers',
  afterDatasetsDraw(chart) {
    const markers = chart.config.options.tgMarkers;
    if (!markers || !markers.length) return;
    const { ctx, scales: { x, yLeft, yRight } } = chart;
    ctx.save();
    for (const { secs, chipY, bbY, color, icon } of markers) {
      const px = x.getPixelForValue(secs);
      let py;
      if (_tgScaleOn(yLeft) && chipY != null) py = yLeft.getPixelForValue(chipY);
      else if (_tgScaleOn(yRight) && bbY != null) py = yRight.getPixelForValue(bbY);
      if (py == null) continue;
      ctx.fillStyle   = color + '22';
      ctx.strokeStyle = color;
      ctx.lineWidth   = 2;
      ctx.beginPath();
      ctx.arc(px, py, 9, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      ctx.fillStyle     = color;
      ctx.font          = 'bold 9px sans-serif';
      ctx.textAlign     = 'center';
      ctx.textBaseline  = 'middle';
      ctx.fillText(icon, px, py);
    }
    ctx.textAlign    = 'left';
    ctx.textBaseline = 'alphabetic';
    ctx.restore();
  },
};

// Pixel position of a dataset index, resolved against whichever y-axis is
// currently on show — same approach as _TG_MARKERS_PLUGIN above.
function _tgPointPixels(chart, idx) {
  const s = _tgState;
  if (!s || !chart) return null;
  const cp = s.chipDataset[idx];
  const bp = s.bbDataset[idx];
  if (!cp) return null;
  const { x, yLeft, yRight } = chart.scales;
  let py = null;
  if (_tgScaleOn(yLeft) && cp.y != null)        py = yLeft.getPixelForValue(cp.y);
  else if (_tgScaleOn(yRight) && bp?.y != null) py = yRight.getPixelForValue(bp.y);
  if (py == null) return null;
  return { x: x.getPixelForValue(cp.x), y: py };
}

// Custom Chart.js plugin — rings the pinned hand and drops a faint guide down
// to the axis, so the card stays visually tied to its point while you read it.
const _TG_PIN_PLUGIN = {
  id: 'tgPin',
  afterDatasetsDraw(chart) {
    const idx = chart.config.options.tgPinnedIndex;
    if (idx == null) return;
    const p = _tgPointPixels(chart, idx);
    if (!p) return;
    const { ctx, chartArea: ca } = chart;
    ctx.save();
    ctx.strokeStyle = 'rgba(0,230,118,0.35)';
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(p.x, p.y);
    ctx.lineTo(p.x, ca.bottom);
    ctx.stroke();
    // The card gets pushed into clear space, which can leave it well away from
    // its hand — run a leader to the nearest edge of it so the pairing reads.
    const box = _tgCardBox;
    if (box) {
      const bx = Math.max(box.x, Math.min(p.x, box.x + box.w));
      const by = Math.max(box.y, Math.min(p.y, box.y + box.h));
      if (Math.hypot(bx - p.x, by - p.y) > 14) {
        ctx.beginPath();
        ctx.moveTo(p.x, p.y);
        ctx.lineTo(bx, by);
        ctx.stroke();
      }
    }
    ctx.setLineDash([]);
    ctx.strokeStyle = '#00e676';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.arc(p.x, p.y, 7, 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
  },
};

// Nearest played hand to a cursor x, in pixels. vpipIdx is ascending in x, so
// the distance is unimodal and the scan can stop as soon as it starts growing.
function _tgNearestVpip(chart, xPx) {
  const s = _tgState;
  if (!s || !s.vpipIdx.length) return null;
  const xs = chart.scales.x;
  let best = null, bestD = Infinity;
  for (const i of s.vpipIdx) {
    const d = Math.abs(xs.getPixelForValue(s.chipDataset[i].x) - xPx);
    if (d < bestD) { bestD = d; best = i; }
    else if (d > bestD) break;
  }
  return bestD <= TG_SNAP_PX ? best : null;
}

// Custom Chart.js interaction mode: behaves like the built-in 'index' mode, but
// when a played hand sits within TG_SNAP_PX of the cursor it wins. Folds stay
// hoverable so scrubbing the line for a stack reading still works anywhere.
function _tgVpipMode(chart, e, options, useFinalPosition) {
  const items = Chart.Interaction.modes.index(chart, e, { ...options, intersect: false }, useFinalPosition);
  if (!_tgState || !_tgState.vpipIdx.length) return items;
  // Hover arrives as a normalised ChartEvent (canvas-relative x); clicks come
  // through getElementsAtEventForMode as a raw MouseEvent, whose .x is clientX.
  // getRelativePosition tells them apart — don't read e.x directly.
  const pos = Chart.helpers?.getRelativePosition
    ? Chart.helpers.getRelativePosition(e, chart)
    : ('native' in e ? e : { x: e.offsetX, y: e.offsetY });
  const snap = _tgNearestVpip(chart, pos.x);
  // Snapped played hands always win — the user has aimed at that marker.
  if (snap != null && !(items.length && items[0].index === snap)) {
    const out = [];
    chart.data.datasets.forEach((ds, di) => {
      if (ds.hidden) return;
      const el = chart.getDatasetMeta(di).data?.[snap];
      if (el) out.push({ element: el, datasetIndex: di, index: snap });
    });
    if (out.length) return out;
  }
  // No snap: only surface the fold-hover if the cursor is near the curve.
  if (snap == null && items.length && pos.y != null) {
    const idx = items[0].index;
    const el = chart.getDatasetMeta(0).data?.[idx];
    if (el && Math.abs(pos.y - el.y) > TG_VDEAD_PX) return [];
  }
  return items;
}

function _tgLevelFromElapsed(elapsedSecs, cfg) {
  const rebuyDur = (cfg.levelDurRebuyMin || cfg.levelDurMin) * 60;
  const mainDur  = cfg.levelDurMin * 60;
  const rebuyEnd = cfg.lateRegLevels * rebuyDur;
  let lvl;
  if (elapsedSecs <= 0) lvl = 1;
  else if (elapsedSecs <= rebuyEnd) lvl = Math.ceil(elapsedSecs / rebuyDur);
  else lvl = cfg.lateRegLevels + Math.ceil((elapsedSecs - rebuyEnd) / mainDur);
  return cfg.maxBlinds ? Math.min(lvl, cfg.maxBlinds) : lvl;
}

// Inverse of _tgLevelFromElapsed: seconds from tournament start to the START of
// a given level. Used to anchor a hero who late-registers at (say) level 6 at
// the real tournament time that level began, instead of at t=0 — so the data
// lines up with the fixed Late Reg / ITM / End reference lines.
function _tgLevelStartSecs(level, cfg) {
  if (!level || level <= 1) return 0;
  const rebuyDur = (cfg.levelDurRebuyMin || cfg.levelDurMin) * 60;
  const mainDur  = cfg.levelDurMin * 60;
  const lr       = cfg.lateRegLevels;
  const completed = level - 1;            // levels fully elapsed before this one
  if (completed <= lr) return completed * rebuyDur;
  return lr * rebuyDur + (completed - lr) * mainDur;
}

function _renderTournamentChart(hands, meta) {
  const wrap = document.getElementById('tourney-graph-wrap');
  if (!wrap) return;
  if (_tgChart) { _tgChart.destroy(); _tgChart = null; }
  _tgState = null;
  _tgSetGraphWarning('');

  if (!hands || !hands.length) { wrap.classList.add('d-none'); return; }

  const sorted = [...hands].filter(h => h.ts).sort((a, b) => a.ts - b.ts);
  if (!sorted.length) { wrap.classList.add('d-none'); return; }

  if (meta && meta.graph_ready === false) {
    wrap.classList.add('d-none');
    const warnEl = document.getElementById('tourney-graph-warning');
    _tgSetGraphWarning(meta.graph_warning || (warnEl && warnEl.dataset.fallback) || '');
    return;
  }

  const roomName       = (meta && meta.room_name) || '';
  const isFinishBusted = !!(meta && meta.finish_busted);
  // Prefer DB-resolved config from the backend (meta); the hardcoded table is
  // only a fallback for fields the API didn't provide.
  const cfg            = { ..._tgGetCfg(roomName) };
  if (meta) {
    if (meta.itm_h != null)              cfg.itmH          = meta.itm_h;
    if (meta.end_h != null)              cfg.endH          = meta.end_h;
    if (meta.ft_h  != null)              cfg.ftH           = meta.ft_h;
    if (meta.late_reg_level != null)     cfg.lateRegLevels = meta.late_reg_level;
    if (meta.level_duration_min != null) cfg.levelDurMin   = meta.level_duration_min;
    if (meta.max_blinds != null)         cfg.maxBlinds     = meta.max_blinds;
    // rebuy-phase duration: DB truth wins, including a null "no rebuy period"
    if ('level_duration_rebuy_min' in meta) cfg.levelDurRebuyMin = meta.level_duration_rebuy_min;
  }
  if (cfg.ftH == null) cfg.ftH = 3.5;  // final-table default so the ref line still draws

  // Anchor x=0 at the REAL tournament start. The backend reconciles each hand's
  // actual blind level from the DB ladder, so a hero who late-registers at
  // (say) level 6 gets placed at the real time that level began rather than at
  // t=0 — keeping the data aligned with the fixed Late Reg / ITM / End lines.
  // `entryOffset` is the seconds from tournament start to the first hand's
  // level; hero wall-clock deltas are added on top. Falls back to 0 when the
  // level is unknown (behaves like the old first-hand anchoring).
  const firstHand   = sorted[0];
  const tournStart  = firstHand.ts;
  const entryOffset = _tgLevelStartSecs(firstHand.level, cfg);
  const elapsedOf   = ts => (ts - tournStart) + entryOffset;

  const titleEl = document.getElementById('tourney-graph-title');
  if (titleEl) {
    const dateStr = new Date(tournStart * 1000).toLocaleDateString('en-GB',
        { day: 'numeric', month: 'short', year: 'numeric', timeZone: currentTz() });
    titleEl.textContent = roomName ? `${roomName} — ${dateStr}` : dateStr;
  }

  // Seconds from tournament start to key milestones
  const lateRegSecs = (cfg.levelDurRebuyMin || cfg.levelDurMin) * cfg.lateRegLevels * 60;
  const itmSecs     = cfg.itmH * 3600;
  const endSecs     = cfg.endH * 3600;
  const ftSecs      = cfg.ftH * 3600;
  const lastHandElapsed = elapsedOf(sorted[sorted.length - 1].ts);
  const axisSecs    = Math.max(endSecs + 30 * 60, lastHandElapsed + 15 * 60);

  // Build {x: elapsedSecs, y: value} datasets. When two consecutive hands are
  // separated by a long real-time pause (busted & later re-entered), insert a
  // null point so Chart.js breaks the line instead of drawing a diagonal
  // connector across the gap.
  const GAP_BREAK_SECS = 15 * 60;
  const rebuyTsSet = new Set(((meta && meta.rebuys) || []).map(r => r.ts));
  const chipDataset = [];
  const bbDataset   = [];
  const pointList   = [];  // parallel to dataset for tooltip lookup

  for (let i = 0; i < sorted.length; i++) {
    const h = sorted[i];
    const elapsed = elapsedOf(h.ts);

    if (i > 0 && rebuyTsSet.has(h.ts)) {
      // Backend detected a bust-and-rebuy right before this hand: the prior
      // hand's stack actually went to zero, not just to whatever its
      // pre-hand chip_stack shows. Plant an explicit zero point there so the
      // line visibly busts before the break, instead of just jumping to the
      // new buy-in's stack.
      const prevElapsed = elapsedOf(sorted[i - 1].ts);
      chipDataset.push({ x: prevElapsed + 1, y: 0 });
      bbDataset.push({ x: prevElapsed + 1, y: 0 });
      pointList.push(null);
      const midX = (prevElapsed + elapsed) / 2;
      chipDataset.push({ x: midX, y: null });
      bbDataset.push({ x: midX, y: null });
      pointList.push(null);
    } else if (i > 0 && (h.ts - sorted[i - 1].ts) > GAP_BREAK_SECS) {
      const midX = (elapsedOf(sorted[i - 1].ts) + elapsed) / 2;
      chipDataset.push({ x: midX, y: null });
      bbDataset.push({ x: midX, y: null });
      pointList.push(null);
    }

    const chips = h.chip_stack != null ? h.chip_stack : null;
    const bb    = chips != null && h.big_blind ? parseFloat((chips / h.big_blind).toFixed(1)) : null;
    chipDataset.push({ x: elapsed, y: chips });
    bbDataset.push({ x: elapsed, y: bb });
    pointList.push(h);
  }

  // If the hero busted out on the final hand, add a terminal point at y=0
  // so the line visibly touches bottom instead of stopping mid-air.
  let bustPoint = null;
  if (isFinishBusted) {
    const lastHand = sorted[sorted.length - 1];
    bustPoint = { x: elapsedOf(lastHand.ts) + 5, y: 0 };
    chipDataset.push(bustPoint);
    bbDataset.push({ x: bustPoint.x, y: 0 });
    pointList.push(null);
  }

  // Lookups for the hover card, built once so its per-mousemove render stays
  // O(1): the display hand number at each dataset index, and the indices of
  // hands the hero actually played. `vpipIdx` is what the magnetic snap and the
  // arrow-key stepping walk over; it is ascending in x, like the datasets.
  const handNums = [];
  const vpipIdx  = [];
  let handNo = 0;
  for (let i = 0; i < pointList.length; i++) {
    const h = pointList[i];
    if (!h) { handNums.push(null); continue; }
    handNums.push(++handNo);
    if (_tgIsNotable(h)) vpipIdx.push(i);
  }

  // Track played range for "played only" zoom
  const playedMinSecs = elapsedOf(sorted[0].ts);
  const playedMaxSecs = bustPoint ? bustPoint.x : elapsedOf(sorted[sorted.length - 1].ts);

  // Reference lines at fixed second positions
  const refLines = [
    { secs: lateRegSecs, color: 'rgba(204,204,0,0.9)',   label: `Late Reg\nL${cfg.lateRegLevels}` },
    { secs: ftSecs,      color: 'rgba(179,136,255,0.9)', label: `Final Table\n${cfg.ftH}h`         },
    { secs: itmSecs,     color: 'rgba(255,152,0,0.9)',   label: `ITM Bubble\n${cfg.itmH}h`         },
    { secs: endSecs,     color: 'rgba(255,82,82,0.9)',   label: `Exp. End\n${cfg.endH}h`            },
  ];

  // Critical point markers — store secs + y-values for both axes
  let bigWinH = null, bigWinVal = -Infinity;
  let bigLossH = null, bigLossVal = Infinity;
  let lastH = null;
  for (const h of sorted) {
    if ((h.profit || 0) > bigWinVal)  { bigWinVal  = h.profit; bigWinH  = h; }
    if ((h.profit || 0) < bigLossVal) { bigLossVal = h.profit; bigLossH = h; }
    lastH = h;
  }
  function _mkMarker(h, color, icon) {
    const secs  = elapsedOf(h.ts);
    const chips = h.chip_stack != null ? h.chip_stack : null;
    const bb    = chips != null && h.big_blind ? parseFloat((chips / h.big_blind).toFixed(1)) : null;
    return { secs, chipY: chips, bbY: bb, color, icon };
  }
  const markers = [];
  if (bigWinH  && bigWinVal  > 0) markers.push(_mkMarker(bigWinH,  '#00e676', '▲'));
  if (bigLossH && bigLossVal < 0) markers.push(_mkMarker(bigLossH, '#ff5252', '▼'));
  // No ✕ on the final bust: the losing hand already carries the ▼ a few pixels
  // away and the line visibly drops to zero, so the ring on the zero line was
  // a second mark saying the same thing. Mid-tournament busts keep theirs —
  // those pair with a rebuy and would otherwise read as a stack that healed
  // itself. (Legend below the chart names both.)

  // Rebuy / add-on spots detected by the backend analyser (marked at the hand
  // where the chip injection was observed).
  const _findHandByTs = ts => sorted.find(x => x.ts === ts);
  for (const rb of (meta && meta.rebuys) || []) {
    const h = _findHandByTs(rb.ts);
    if (!h) continue;
    markers.push(_mkMarker(h, '#ffb300', 'R'));
    // Also flag the bust that preceded this rebuy, at the zero point planted
    // in the dataset above.
    const idx = sorted.indexOf(h);
    if (idx > 0) {
      const prevElapsed = elapsedOf(sorted[idx - 1].ts);
      markers.push({ secs: prevElapsed + 1, chipY: 0, bbY: 0, color: '#ff5252', icon: '✕' });
    }
  }
  for (const ad of (meta && meta.addons) || []) {
    const h = _findHandByTs(ad.ts);
    if (h) markers.push(_mkMarker(h, '#40c4ff', 'A'));
  }

  _tgState = {
    chipDataset, bbDataset, pointList, handNums, vpipIdx,
    refLines, markers,
    tournStart, entryOffset, axisSecs,
    playedMinSecs, playedMaxSecs,
    roomName, cfg,
  };

  _tgPinned = null;   // a different tournament invalidates any pinned index
  _tgBuildChart();
  _tgRenderLegend(markers);
  wrap.classList.remove('d-none');
}

// Icon → [dataset key, colour], matching what _TG_MARKERS_PLUGIN paints. The
// label text itself is server-translated and read from #tourney-graph-legend's
// data-* attributes (see _tgRenderLegend) rather than hardcoded here.
const _TG_MARK_LEGEND = {
  '▲': ['biggestWin',  '#00e676'],
  '▼': ['biggestLoss', '#ff5252'],
  'R': ['rebuy',        '#ffb300'],
  'A': ['addon',        '#40c4ff'],
  '✕': ['busted',       '#ff5252'],
};

// The dots are a deliberately sparse read of the run — only hands hero played,
// and not the blind-ish ones (see _tgIsNotable) — which is not guessable from
// the chart. Marks are drawn only when they occur, so the legend lists only the
// ones this tournament actually has.
function _tgRenderLegend(markers) {
  const el = document.getElementById('tourney-graph-legend');
  if (!el) return;
  const t = el.dataset;
  const items = [
    `<span class="tg-lg"><i class="tg-lg-dot won"></i>${t.handWon}</span>`,
    `<span class="tg-lg"><i class="tg-lg-dot lost"></i>${t.handLost}</span>`,
    `<span class="tg-lg"><i class="tg-lg-dot sd"></i>${t.reachedShowdown}</span>`,
  ];
  const drawn = new Set((markers || []).map(m => m.icon));
  for (const [icon, [key, color]] of Object.entries(_TG_MARK_LEGEND)) {
    if (!drawn.has(icon)) continue;
    items.push(`<span class="tg-lg"><i class="tg-lg-mark" style="color:${color};`
      + `border-color:${color};background:${color}22">${icon}</i>${t[key]}</span>`);
  }
  items.push(`<span class="tg-lg"><i class="tg-lg-line"></i>${t.stage}</span>`);
  items.push(`<span class="tg-lg tg-lg-note">${t.note}</span>`);
  el.innerHTML = items.join('');
}

function _tgSetGraphWarning(message) {
  const el = document.getElementById('tourney-graph-warning');
  if (!el) return;
  el.textContent = message || '';
  el.classList.toggle('d-none', !message);
}

function _tgBuildChart() {
  if (!_tgState) return;
  const s = _tgState;
  const { chipDataset, bbDataset, pointList, refLines, markers, tournStart, entryOffset, axisSecs,
          playedMinSecs, playedMaxSecs, roomName, cfg } = s;
  const canvas = document.getElementById('tourney-graph-canvas');
  if (!canvas) return;

  const chipColor = 'rgba(64,196,255,1)';
  const bbColor   = 'rgba(0,230,118,1)';
  const showLeft  = _tgYMode !== 'bb';
  const showRight = _tgYMode !== 'chips';

  if (_tgChart) { _tgChart.destroy(); _tgChart = null; }

  if (typeof Chart === 'undefined') {
    console.warn('Chart.js not loaded yet');
    return;
  }

  // x-axis range: full view = 0..axisSecs; played only = zoom to played range
  const xMin = _tgPlayedOnly ? Math.max(0, playedMinSecs - 60) : 0;
  const xMax = _tgPlayedOnly ? playedMaxSecs + 120 : axisSecs;

  // Tick callback: Time mode = elapsed hh:mm, Level mode = "L#"
  function xTickCb(v) {
    if (_tgXMode === 'level') {
      const lvl = _tgLevelFromElapsed(v, cfg);
      return `L${lvl}`;
    }
    return _tgFmtElapsed(v);
  }

  // Tick step: aim for ~10 ticks across the visible range
  const visibleRange = xMax - xMin;
  const niceSteps = [5*60, 10*60, 15*60, 20*60, 30*60, 45*60, 60*60];
  const stepSize = niceSteps.find(s => visibleRange / s <= 12) || 30*60;

  // Action dots. Folded hands drop to radius 0 so the line reads clean, and
  // what is left is a sparse map of the hands hero actually played, coloured by
  // how they went. Only one series carries them (chips unless chips is hidden)
  // — two rows of dots would say the same thing twice.
  //
  // Resolved per render, off live dataset visibility rather than _tgYMode, so
  // the dots follow both the Chips/BBs pills (which mutate `hidden` in place
  // without rebuilding the chart) and a click on the Chart.js legend (which
  // hides a series without the pills or _tgYMode ever hearing about it).
  const _tgDotSeries = () => {
    const ds = _tgChart?.data?.datasets;
    if (ds) return ds[0].hidden ? 1 : 0;
    return _tgYMode === 'bb' ? 1 : 0;
  };
  const dotRadius = di => ctx => {
    if (di !== _tgDotSeries()) return 0;
    const h = pointList[ctx.dataIndex];
    if (!_tgIsNotable(h)) return 0;
    return h.last_street === 'SD' ? 3.2 : 2.2;
  };
  const dotHoverRadius = di => ctx => {
    if (di !== _tgDotSeries()) return 0;
    const h = pointList[ctx.dataIndex];
    if (!h) return 0;
    return _tgIsNotable(h) ? (h.last_street === 'SD' ? 7 : 6) : 2.5;
  };
  const dotColor = seriesColor => ctx => {
    const h = pointList[ctx.dataIndex];
    if (!h || !h.profit) return seriesColor;
    return h.profit > 0 ? '#00e676' : '#ff5252';
  };
  const dotHitRadius = di => ctx =>
    (di === _tgDotSeries() && _tgIsNotable(pointList[ctx.dataIndex])) ? 10 : 0;

  Chart.register(_TG_REFLINES_PLUGIN, _TG_MARKERS_PLUGIN, _TG_PIN_PLUGIN);
  Chart.Interaction.modes.tgVpip = _tgVpipMode;

  _tgChart = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      datasets: [
        {
          label: 'Chip Stack',
          data: chipDataset,
          borderColor: chipColor,
          backgroundColor: 'rgba(64,196,255,0.07)',
          fill: true,
          borderWidth: 1.5,
          tension: 0.3,
          pointRadius: dotRadius(0),
          pointHoverRadius: dotHoverRadius(0),
          pointHitRadius: dotHitRadius(0),
          pointBackgroundColor: dotColor(chipColor),
          pointBorderColor: dotColor(chipColor),
          yAxisID: 'yLeft',
          spanGaps: false,
          hidden: _tgYMode === 'bb',
          order: 1,
        },
        {
          label: 'BB Count',
          data: bbDataset,
          borderColor: bbColor,
          backgroundColor: 'rgba(0,230,118,0.05)',
          fill: true,
          borderWidth: 1.5,
          tension: 0.3,
          pointRadius: dotRadius(1),
          pointHoverRadius: dotHoverRadius(1),
          pointHitRadius: dotHitRadius(1),
          pointBackgroundColor: dotColor(bbColor),
          pointBorderColor: dotColor(bbColor),
          yAxisID: 'yRight',
          spanGaps: false,
          hidden: _tgYMode === 'chips',
          order: 2,
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: { duration: 300 },
      interaction: { mode: 'tgVpip', intersect: false },
      tgRefLines: refLines,
      tgMarkers:  markers,
      tgPinnedIndex: _tgPinned,
      plugins: {
        legend: {
          display: true,
          labels: {
            color: '#d0ddd0',
            font: { family: "'Exo 2', sans-serif", size: 12 },
            boxWidth: 12,
            filter: item => !item.hidden,
          },
        },
        // Rendered as real DOM instead of onto the canvas — a canvas tooltip is
        // pixels and cannot hold the replay link.
        tooltip: {
          enabled: false,
          external: _tgTooltipExternal,
          filter: item => !item.dataset.hidden,
        },
      },
      scales: {
        x: {
          type: 'linear',
          min: xMin,
          max: xMax,
          ticks: {
            color: '#6b8c6b',
            font: { size: 10, family: "'Exo 2', sans-serif" },
            stepSize,
            maxRotation: 0,
            callback: xTickCb,
          },
          grid: { color: 'rgba(30,45,30,0.4)' },
        },
        yLeft: {
          type: 'linear',
          position: 'left',
          display: showLeft,
          ticks: {
            color: chipColor,
            font: { size: 10, family: "'Exo 2', sans-serif" },
            callback: v => _tgFmtK(v),
          },
          grid: { color: 'rgba(30,45,30,0.4)' },
        },
        yRight: {
          type: 'linear',
          position: 'right',
          display: showRight,
          ticks: {
            color: bbColor,
            font: { size: 10, family: "'Exo 2', sans-serif" },
            callback: v => `${v}BB`,
          },
          grid: { drawOnChartArea: false },
        },
      },
    },
  });

  _tgWireCanvas(canvas);
}

/* ── Hand card: hover preview, click to pin, replay out to PPPoker ──────── */

// A card that follows the cursor cannot hold a button — reaching for it leaves
// the canvas and dismisses it. So hover previews, and a click pins: the card
// stops following, takes pointer events, and its actions become clickable.
// Pinning also just plain helps on a 5h graph — it keeps one hand on screen
// while you read the shape of the curve around it.
function _tgWireCanvas(canvas) {
  if (canvas.dataset.tgWired) return;
  canvas.dataset.tgWired = '1';

  canvas.addEventListener('click', e => {
    if (!_tgChart || !_tgState) return;
    const els = _tgChart.getElementsAtEventForMode(e, 'tgVpip', { intersect: false }, false);
    const idx = els.length ? els[0].index : null;
    if (idx == null || !_tgIsVpip(_tgState.pointList[idx])) { _tgUnpin(); return; }
    _tgPin(idx);
  });

  const card = document.getElementById('tg-hand-card');
  if (card) {
    card.addEventListener('click', e => {
      const jump = e.target.closest('[data-tg-jump]');
      if (jump) { _tgJumpToRow(jump.dataset.tgJump); return; }
      if (e.target.closest('[data-tg-close]')) _tgUnpin();
    });
  }

  // Arrow keys walk the played hands in order, which turns a 5h graph into a
  // browsable list of the session's actual decisions. Bound on the document
  // rather than the canvas so it works right after a click without depending on
  // where focus landed; it only claims the keys while a hand is pinned.
  document.addEventListener('keydown', e => {
    if (_tgPinned == null) return;
    if (e.key === 'Escape')          { _tgUnpin(); return; }
    if (e.key === 'ArrowLeft')       { e.preventDefault(); _tgStepPin(-1); }
    else if (e.key === 'ArrowRight') { e.preventDefault(); _tgStepPin(1); }
  });

  document.addEventListener('click', e => {
    if (_tgPinned == null || e.target === canvas) return;
    if (card && card.contains(e.target)) return;
    _tgUnpin();
  });
}

function _tgTooltipExternal(context) {
  if (_tgPinned != null) return;              // the pinned card owns the display
  const tt = context.tooltip;
  if (!tt || tt.opacity === 0) { _tgHideCard(); return; }
  const idx = tt.dataPoints?.[0]?.dataIndex;
  if (idx == null || !_tgState?.pointList[idx]) { _tgHideCard(); return; }
  _tgShowCard(idx, false);
}

function _tgPin(idx) {
  _tgPinned = idx;
  // Card first: placing it sets _tgCardBox, which the pin plugin needs in order
  // to draw the leader on this same frame rather than one behind.
  _tgShowCard(idx, true);
  if (_tgChart) { _tgChart.options.tgPinnedIndex = idx; _tgChart.update('none'); }
}

function _tgUnpin() {
  if (_tgPinned == null) return;
  _tgPinned = null;
  if (_tgChart) { _tgChart.options.tgPinnedIndex = null; _tgChart.update('none'); }
  _tgHideCard();
}

function _tgStepPin(dir) {
  const s = _tgState;
  if (!s || _tgPinned == null) return;
  const at = s.vpipIdx.indexOf(_tgPinned);
  if (at !== -1) {
    const next = s.vpipIdx[at + dir];
    if (next != null) _tgPin(next);
    return;
  }
  // Pinned hand isn't in the stepped set — it was reached by the nearest-point
  // fallback. Step from where it would sit rather than dead-ending.
  let after = s.vpipIdx.findIndex(i => i > _tgPinned);
  if (after === -1) after = s.vpipIdx.length;
  const target = dir > 0 ? s.vpipIdx[after] : s.vpipIdx[after - 1];
  if (target != null) _tgPin(target);
}

function _tgHideCard() {
  document.getElementById('tg-hand-card')?.classList.remove('show', 'pinned');
  _tgCardBox = null;
}

// Keeps the pinned card glued to its point after an axis change (Chips/BBs, PH,
// Time/Level all update the chart in place rather than rebuilding it).
function _tgRefreshCard() {
  if (_tgPinned != null) _tgShowCard(_tgPinned, true);
}

function _tgShowCard(idx, pinned) {
  const card = document.getElementById('tg-hand-card');
  const s = _tgState;
  if (!card || !s || !_tgChart) return;
  const p = _tgPointPixels(_tgChart, idx);
  if (!p) { _tgHideCard(); return; }
  card.innerHTML = _tgCardHtml(idx, pinned);
  card.classList.add('show');
  card.classList.toggle('pinned', !!pinned);
  _tgPositionCard(card, p.x, p.y);
  if (pinned && _tgChart) _tgChart.render();   // redraw the leader to the new box
}

// Pixel positions of every plotted point on the visible series, for scoring how
// much of the curve a candidate card position would bury.
function _tgInkPoints() {
  const s = _tgState, chart = _tgChart;
  if (!s || !chart) return [];
  const xs = chart.scales.x, ca = chart.chartArea;
  const series = [];
  if (!chart.data.datasets[0].hidden && _tgScaleOn(chart.scales.yLeft)) {
    series.push([s.chipDataset, chart.scales.yLeft]);
  }
  if (!chart.data.datasets[1].hidden && _tgScaleOn(chart.scales.yRight)) {
    series.push([s.bbDataset, chart.scales.yRight]);
  }
  const out = [];
  for (const [data, ys] of series) {
    for (let i = 0; i < data.length; i++) {
      const d = data[i];
      if (d.y == null) continue;
      const x = xs.getPixelForValue(d.x);
      if (x < ca.left || x > ca.right) continue;
      out.push({ x, y: ys.getPixelForValue(d.y) });
    }
  }
  return out;
}

// Remembers which slot was chosen last, so scrubbing along the line doesn't
// make the card flap between above/below on every pixel of mouse travel.
let _tgCardSlot = null;

// Place the card where it hides the least curve. A card parked over a dense
// stretch of the line covers the very thing you are reading it against, so we
// score a handful of candidate slots by how many plotted points fall under each
// and take the cheapest, with only a mild pull back toward the point. The ring
// and connector drawn by _TG_PIN_PLUGIN keep it tied to its hand when it has to
// sit well away.
function _tgPositionCard(card, px, py) {
  const wrap = card.parentElement;
  const W = wrap.clientWidth, H = wrap.clientHeight;
  const cw = card.offsetWidth, ch = card.offsetHeight;
  const PAD = 6, GAP = 16;
  const ca = _tgChart ? _tgChart.chartArea : { top: 0, bottom: H };
  const clampX = v => Math.max(PAD, Math.min(v, W - cw - PAD));
  const clampY = v => Math.max(PAD, Math.min(v, H - ch - PAD));

  const ink = _tgInkPoints();
  const xOpts = [
    ['c', px - cw / 2], ['r', px + GAP], ['l', px - cw - GAP],
    ['R', px + GAP * 3], ['L', px - cw - GAP * 3],
  ];
  const yOpts = [
    ['a', py - ch - GAP], ['b', py + GAP],
    ['t', ca.top + PAD], ['m', ca.bottom - ch - PAD],
  ];

  let best = null;
  for (const [xk, ox] of xOpts) {
    for (const [yk, oy] of yOpts) {
      const x = clampX(ox), y = clampY(oy);
      let covered = 0;
      for (let i = 0; i < ink.length; i++) {
        const p = ink[i];
        if (p.x >= x && p.x <= x + cw && p.y >= y && p.y <= y + ch) covered++;
      }
      const dx = (x + cw / 2) - px, dy = (y + ch / 2) - py;
      // Covering the curve dominates; distance only breaks ties between
      // equally clear slots. Staying put beats a marginally better neighbour.
      let score = covered * 60 + Math.hypot(dx, dy);
      if (`${xk}${yk}` === _tgCardSlot) score -= 90;
      if (!best || score < best.score) best = { x, y, score, slot: `${xk}${yk}` };
    }
  }

  _tgCardSlot = best.slot;
  card.style.left = `${best.x}px`;
  card.style.top  = `${best.y}px`;
  _tgCardBox = { x: best.x, y: best.y, w: cw, h: ch };
}

function _tgCardHtml(idx, pinned) {
  const s = _tgState;
  const h = s.pointList[idx];
  const lvl = h.level != null ? h.level : _tgInferLevel(h.big_blind, s.roomName, h.chip_stack);
  const cards = (h.hole_cards || []).map(renderCard).join('');
  const bb = h.chip_stack != null && h.big_blind
    ? (h.chip_stack / h.big_blind).toFixed(1) : '—';

  const head = `
    <div class="tg-card-head-pill">
      <span class="tg-card-hand">#${s.handNums[idx]}</span>
      ${lvl ? `<span class="tg-card-meta">L${lvl}</span>` : ''}
    </div>
    ${pinned ? '<button type="button" class="tg-card-close" data-tg-close title="Close (Esc)">✕</button>' : ''}`;

  // Position earns its place — it is what the cards mean. Street does not: the
  // dot's size already says whether the hand reached showdown, and the exact
  // street is in the table row. The pill is fixed-width, so scrubbing between
  // BTN and UTG+1 cannot resize the card out from under the cursor.
  const badges = `
    <div class="tg-card-badges">
      ${cards || '<span class="tg-card-dim">—</span>'}
      ${posBadge(h.position)}
    </div>`;

  const stats = `
    <div class="tg-card-stats">
      <span><i>Chips</i>${_tgFmtK(h.chip_stack)}</span>
      <span><i>BBs</i>${bb}</span>
      <span><i>Net P/L</i>${fmtProfitBB(h.profit, h.big_blind)}</span>
    </div>`;

  if (!_tgIsVpip(h)) return head + badges + stats;

  const replay = h.replay_url && h.replay_url !== '#'
    ? `<a class="tg-card-btn" href="${h.replay_url}" target="_blank" rel="noopener">▶ Replay</a>`
    : `<span class="tg-card-btn is-disabled" title="No replay available for this hand">▶ Replay</span>`;

  // The table is capped at the last 30 hands for free accounts while the graph
  // plots every one, so the row this would jump to often is not rendered.
  const rowExists = h.hand_num && document.querySelector(
    `#tourney-detail-tbody tr[data-hand-num="${CSS.escape(h.hand_num)}"]`);
  const jump = rowExists
    ? `<button type="button" class="tg-card-btn" data-tg-jump="${h.hand_num}">↓ Row</button>`
    : '';

  const actions = pinned
    ? `<div class="tg-card-actions">${replay}${jump}</div>`
    : '<div class="tg-card-hint">Click to pin · ← → to step</div>';

  return head + badges + stats + actions;
}

function _tgJumpToRow(handNum) {
  const row = document.querySelector(
    `#tourney-detail-tbody tr[data-hand-num="${CSS.escape(handNum)}"]`);
  if (!row) return;
  row.scrollIntoView({ behavior: 'smooth', block: 'center' });
  row.classList.remove('hand-jump-flash');
  void row.offsetWidth;                       // reflow so the flash can re-fire
  row.classList.add('hand-jump-flash');
}

let _tgShowChips = true;
let _tgShowBB = true;

function _tgToggleY(which) {
  if (which === 'chips') _tgShowChips = !_tgShowChips;
  else _tgShowBB = !_tgShowBB;
  if (!_tgShowChips && !_tgShowBB) {
    if (which === 'chips') _tgShowBB = true; else _tgShowChips = true;
  }
  document.getElementById('tg-y-chips')?.classList.toggle('active', _tgShowChips);
  document.getElementById('tg-y-bb')?.classList.toggle('active', _tgShowBB);
  _tgYMode = _tgShowChips && _tgShowBB ? 'both' : _tgShowChips ? 'chips' : 'bb';
  if (!_tgChart) return;
  _tgChart.data.datasets[0].hidden = !_tgShowChips;
  _tgChart.data.datasets[1].hidden = !_tgShowBB;
  _tgChart.options.scales.yLeft.display  = _tgShowChips;
  _tgChart.options.scales.yRight.display = _tgShowBB;
  _tgChart.update();
  _tgRefreshCard();
}

function _tgToggleX() {
  _tgXMode = _tgXMode === 'time' ? 'level' : 'time';
  const pill = document.getElementById('tg-x-pill');
  if (pill) { pill.textContent = _tgXMode === 'time' ? 'Time' : 'Level'; pill.classList.toggle('active', _tgXMode === 'time'); }
  if (!_tgChart || !_tgState) return;
  _tgChart.update();
}

function _tgTogglePlayed() {
  _tgPlayedOnly = !_tgPlayedOnly;
  const pill = document.getElementById('tg-played-only-pill');
  if (pill) pill.classList.toggle('active', _tgPlayedOnly);
  if (!_tgChart || !_tgState) return;
  const s = _tgState;
  if (_tgPlayedOnly) {
    _tgChart.options.scales.x.min = Math.max(0, s.playedMinSecs - 60);
    _tgChart.options.scales.x.max = s.playedMaxSecs + 120;
  } else {
    _tgChart.options.scales.x.min = 0;
    _tgChart.options.scales.x.max = s.axisSecs;
  }
  _tgChart.update();
  _tgRefreshCard();
}

function _tgDestroy() {
  if (_tgChart) { _tgChart.destroy(); _tgChart = null; }
  _tgState = null;
  _tgPinned = null;
  _tgHideCard();
  _tgPlayedOnly = false;
  _tgShowChips = true;
  _tgShowBB = true;
  _tgYMode = 'both';
  _tgXMode = 'time';
  const chipPill = document.getElementById('tg-y-chips');
  const bbPill = document.getElementById('tg-y-bb');
  const xPill = document.getElementById('tg-x-pill');
  const phPill = document.getElementById('tg-played-only-pill');
  if (chipPill) chipPill.classList.add('active');
  if (bbPill) bbPill.classList.add('active');
  if (xPill) { xPill.textContent = 'Time'; xPill.classList.add('active'); }
  if (phPill) phPill.classList.remove('active');
  _tgSetGraphWarning('');
  const wrap = document.getElementById('tourney-graph-wrap');
  if (wrap) wrap.classList.add('d-none');
}

/* ── Tournament Details (Pro): hands of the event selected in the Summary ── */

let _selectedTourneyId = null;

/** Clears the Tournament Details table back to its empty placeholder and drops any selection. */
function _resetTournamentDetails() {
  const tbody = document.getElementById('tourney-detail-tbody');
  if (tbody) {
    tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">Select a tournament above to view its hands.</td></tr>';
  }
  const hint = document.getElementById('tourney-detail-hint');
  if (hint) hint.textContent = '';
  document.querySelectorAll('.tsum-event-card.selected, .anon-tourney-row.selected')
    .forEach(c => c.classList.remove('selected'));
  _selectedTourneyId = null;
  window._lastTourneyDetail = null;   // nothing open to re-render on a tz change
  _tgDestroy();
}

/**
 * Render one tournament of a signed-out import into the Tournament Details
 * section, straight from the payload the import returned.
 *
 * Same section, same chart, same table as the signed-in path — the only
 * difference is that the data is already here rather than a fetch away, which
 * is what lets a signed-out visitor see a graph at all.
 */
function _selectAnonTourneyDetail(tid, rowEl) {
  const graph = (window._anonGraphs || {})[tid];
  if (!graph) return;

  if (rowEl && rowEl.classList.contains('selected')) {   // click again to close
    _resetTournamentDetails();
    return;
  }
  document.querySelectorAll('.anon-tourney-row.selected')
    .forEach(el => el.classList.remove('selected'));
  if (rowEl) rowEl.classList.add('selected');

  const hands = graph.hands || [];
  const meta  = graph.meta  || {};
  _selectedTourneyId = tid;
  _tgDestroy();

  const section = document.getElementById('tournament-history-pro-section');
  if (section) section.classList.remove('d-none');
  renderHandsTable(hands, 'tourney-detail-tbody', { showExport: true, exportTid: tid });
  _renderTournamentChart(hands, meta);
  window._lastTourneyDetail = { tid, hands, meta };

  const hint = document.getElementById('tourney-detail-hint');
  if (hint) hint.innerHTML = _tourneyDetailHintHtml(hands.length, meta);
  if (section) section.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// Hands count + K-max as pills — used by both anon and signed-in headers.
function _tourneyDetailHintHtml(handsCount, meta) {
  const maxP = Number(meta?.max_players) || 0;
  const hands = `<span class="tsum-stat-pill">${handsCount} hand${handsCount === 1 ? '' : 's'}</span>`;
  const seats = maxP > 0 ? ` <span class="tsum-stat-pill">${maxP}-max</span>` : '';
  return hands + seats;
}

/** Loads one tournament's hands into the Tournament Details table; clicking the same card again clears it. */
async function _selectTourneyDetail(tid, cardEl) {
  // Toggle off when the already-selected card is clicked again.
  if (cardEl && cardEl.classList.contains('selected')) {
    _resetTournamentDetails();
    return;
  }
  document.querySelectorAll('.tsum-event-card.selected').forEach(c => c.classList.remove('selected'));
  if (cardEl) cardEl.classList.add('selected');
  _selectedTourneyId = tid;
  window._lastTourneyDetail = null;   // drop any prior detail while this one loads

  const tbody   = document.getElementById('tourney-detail-tbody');
  const hint    = document.getElementById('tourney-detail-hint');
  const section = document.getElementById('tournament-history-pro-section');
  if (tbody) tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">Loading hands…</td></tr>';
  _tgDestroy();

  if (!_currentUser) return;
  const token = await _currentUser.getIdToken().catch(() => null);
  if (!token) { _resetTournamentDetails(); return; }

  try {
    const r = await fetch(`/api/tournaments/${tid}/hands`, { headers: { 'Authorization': `Bearer ${token}` } });
    if (_selectedTourneyId !== tid) return;  // selection changed mid-fetch — drop stale response
    if (!r.ok) {
      const err = (await r.json().catch(() => ({}))).error;
      if (err === 'history_expired') {
        if (tbody) tbody.innerHTML =
          `<tr><td colspan="7" class="text-center py-4 text-muted">This tournament is older than `
          + `${FREE_HISTORY_DAYS} days and is outside your free history window.</td></tr>`;
        showUpgradeModal('history');
        return;
      }
      if (tbody) tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">Could not load hands.</td></tr>';
      return;
    }
    const data  = await r.json();
    const hands = data.hands || [];
    const meta  = data.meta  || {};
    // Fall back to _allTournaments for fields the API might not yet return
    if (!meta.room_name || !meta.earliest_ts) {
      const cached = (_allTournaments || []).find(t => t.tourney_id === tid) || {};
      meta.room_name    = meta.room_name    || cached.room_name    || '';
      meta.earliest_ts  = meta.earliest_ts  || cached.earliest_ts  || null;
      meta.finish_busted= meta.finish_busted != null ? meta.finish_busted : (cached.finish_busted || false);
    }
    renderHandsTable(hands, 'tourney-detail-tbody', { showExport: true, exportTid: tid });
    _renderTournamentChart(hands, meta);
    window._lastTourneyDetail = { tid, hands, meta };   // so a tz change can re-render this graph
    if (hint) hint.innerHTML = _tourneyDetailHintHtml(hands.length, meta);
    if (section) {
      section.scrollIntoView({ behavior: 'smooth', block: 'start' });
      const card = section.querySelector('.section-card');
      if (card) {
        card.classList.remove('td-flash');
        void card.offsetWidth;          // restart the highlight animation
        card.classList.add('td-flash');
      }
    }
  } catch (e) {
    if (tbody) tbody.innerHTML = '<tr><td colspan="7" class="text-center py-4 text-muted">Could not load hands.</td></tr>';
  }
}

function exportPersistedTournament(tourneyId, btn) {
  if (!_currentUser) { showSignInModal('Sign in to export this tournament.'); return; }
  _downloadExport(`/api/tournaments/${tourneyId}/export`, {
    kind: 'tourney',
    body: { platform: (btn && btn.dataset.platform) || '' },
    fallbackName: `pppoker_${tourneyId}.txt`,
  }, btn);
}

function exportPersistedTournamentJson(tourneyId, btn) {
  if (!_currentUser) { showSignInModal('Sign in to export this tournament.'); return; }
  _downloadExport(`/api/tournaments/${tourneyId}/export/json`, {
    kind: 'tourney',
    body: {},
    fallbackName: `pppoker_${tourneyId}.json`,
  }, btn);
}

/* ── Export Panel ────────────────────────────────────────── */

function renderHandStats(data) {
  // data.stats / data.validation are scoped to THIS import's records — accurate
  // for anon (nothing persisted) and for signed-in re-imports of already-saved
  // hands, where data.new_hands is 0 but real data is still on screen.
  const s = data.stats || {};
  const v = data.validation || {};
  const _set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val ?? '—'; };
  _set('hs-hands', s.total_hands ?? 0);
  _set('hs-flop',  s.hands_hero_saw_flop  ?? 0);
  _set('hs-won',   v.hands_won            ?? 0);
  _set('hs-turn',  s.hands_hero_saw_turn  ?? 0);
  _set('hs-river', s.hands_hero_saw_river ?? 0);
  _set('hs-sd',    s.hands_at_showdown    ?? 0);
  const row = document.getElementById('loaded-hands-row');
  if (row) row.classList.remove('d-none');
}


/* ── Shared export helpers ───────────────────────────────── */

function _triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a   = Object.assign(document.createElement('a'), { href: url, download: filename });
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

/** tourney_id lives in the middle segment of a gameid (prefix-tourneyid-seq). */
function _tidFromHandId(handId) {
  const parts = String(handId || '').split('-');
  return parts.length >= 2 ? parts[1] : '';
}

/** tourney_ids of the import currently on screen — what a session export covers. */
function _sessionTourneyIds() {
  const data = window._lastData || {};
  return (data.tournaments || []).map(t => t.tourney_id).filter(Boolean);
}

function exportAllHandsJson(btn) {
  _downloadExport('/api/export/json/all', {
    kind: 'session',
    body: { tourney_ids: _sessionTourneyIds() },
    fallbackName: 'pppoker_all.json',
    loadingText: 'Building JSON…',
  }, btn);
}

function exportAllHands(btn) {
  _downloadExport('/api/export/pokerstars', {
    kind: 'session',
    body: { platform: (btn && btn.dataset.platform) || '',
            tourney_ids: _sessionTourneyIds() },
    fallbackName: 'pppoker_export.txt',
    loadingText: 'Generating export…',
  }, btn);
}

function exportTournamentJson(tourneyId, btn) {
  _downloadExport('/api/export/json/tournament', {
    kind: 'tourney',
    body: { tourney_id: tourneyId },
    fallbackName: 'tournament.json',
  }, btn);
}

function _rowExportStatus(btn, state, text, autoClear) {
  const toast = document.getElementById('export-toast');
  if (!toast) return;
  clearTimeout(toast._hideTimer);
  if (state === 'loading') {
    toast.innerHTML = `<span style="color:var(--muted)">${text || 'Exporting…'}</span>`;
  } else if (state === 'ok') {
    toast.innerHTML = `<span style="color:var(--green)">✓ ${text}</span>`;
  } else {
    toast.innerHTML = `<span style="color:var(--red)">${text}</span>`;
  }
  toast.classList.add('et-visible');
  if (autoClear) {
    toast._hideTimer = setTimeout(() => toast.classList.remove('et-visible'), autoClear);
  }
}

function _doExportTournament(tourneyId, btn) {
  _downloadExport('/api/export/tournament', {
    kind: 'tourney',
    body: { tourney_id: tourneyId, platform: (btn && btn.dataset.platform) || '' },
    fallbackName: 'tournament_export.txt',
  }, btn);
}

function exportTournament(tourneyId, btn) {
  // Skip modal if user previously suppressed it.
  if (localStorage.getItem('exportWarningSuppressed') === '1') {
    _doExportTournament(tourneyId, btn);
    return;
  }

  const modal      = new bootstrap.Modal(document.getElementById('exportWarningModal'));
  const suppress   = document.getElementById('export-warning-suppress');
  suppress.checked = false;

  const confirmBtn = document.getElementById('export-confirm-btn');
  // Replace any previous listener to avoid stacking handlers
  const newBtn = confirmBtn.cloneNode(true);
  confirmBtn.parentNode.replaceChild(newBtn, confirmBtn);
  newBtn.addEventListener('click', () => {
    if (suppress.checked) localStorage.setItem('exportWarningSuppressed', '1');
    modal.hide();
    _doExportTournament(tourneyId, btn);
  });
  modal.show();
}

/* ── Init ────────────────────────────────────────────────── */

document.addEventListener('DOMContentLoaded', () => {
  _initConnStatus();
  _initSideBanner();
  _initMidBanner();

  document.getElementById('url-input').addEventListener('keydown', e => {
    if (e.key === 'Enter') handleImport();
  });

  // Bootstrap tooltips (used for info ⓘ buttons that don't need a modal)
  document.querySelectorAll('[data-bs-toggle="tooltip"]').forEach(el => {
    new bootstrap.Tooltip(el, { trigger: 'hover focus' });
  });

  // Auth modal: send link on Enter, and clear state each time modal opens
  const authModal = document.getElementById('modal-auth');
  if (authModal) {
    // Opening the modal from the auth bar must not inherit the "you need this
    // to export" note left behind by an earlier gated open.
    authModal.addEventListener('hidden.bs.modal', () => {
      const note = document.getElementById('auth-gate-note');
      if (note) { note.textContent = ''; note.classList.add('d-none'); }
    });
    authModal.addEventListener('shown.bs.modal', () => {
      const inp = document.getElementById('auth-email-input');
      const msg = document.getElementById('auth-msg');
      if (inp) inp.value = '';
      if (msg) { msg.className = 'mt-2 d-none'; msg.innerHTML = ''; }
      const btn = document.getElementById('auth-send-btn');
      if (btn) btn.disabled = false;
      if (inp) inp.focus();
    });
    document.getElementById('auth-email-input').addEventListener('keydown', e => {
      if (e.key === 'Enter') sendMagicLink();
    });
  }

  // Closing the survey modal stops the credit poll and unloads the provider's
  // iframe — including when it's dismissed with the backdrop or Esc.
  const surveyModal = document.getElementById('survey-modal');
  if (surveyModal) {
    surveyModal.addEventListener('hidden.bs.modal', () => {
      clearTimeout(_surveyState.timer);
      const frame = document.getElementById('survey-frame');
      if (frame) frame.removeAttribute('src');
    });
  }

  // Providers post a message into the page when a survey finishes or when there
  // is nothing eligible to show. Treated as a hint only — it starts the fallback
  // or shortens the wait, but the credit itself still comes from /api/credits,
  // which only moves once the provider's server-to-server callback has landed.
  //
  // TODO(cpx/tally): confirm the exact message payloads in each provider's docs
  // and tighten the matching below once they're known.
  window.addEventListener('message', (ev) => {
    if (!_surveyState.kind) return;
    const raw = typeof ev.data === 'string' ? ev.data : JSON.stringify(ev.data || '');
    if (/no[_\s-]?surveys|no_offers|noSurveysAvailable/i.test(raw)) {
      const url = (_surveyState.config || {}).tally_form_url;
      if (url) {
        _surveyStatus('No surveys available right now — here are a few quick questions instead.');
        _surveyShowTally(url, _surveyState.kind);
      } else {
        _surveyStatus('No surveys are available right now — please try again later.', 'err');
      }
    } else if (/complete|finished|success/i.test(raw)) {
      _surveyStatus('Checking your unlock…');
      _surveyStartPolling();
    }
  });

  const langSelect = document.getElementById('lang-select');
  if (langSelect) {
    langSelect.addEventListener('change', () => {
      document.cookie = 'lang=' + langSelect.value + '; max-age=31536000; path=/; SameSite=Lax';
      location.reload();
    });
  }

  const tzSelect = document.getElementById('tz-select');
  if (tzSelect) {
    tzSelect.value = _savedTz();                 // restore the choice shared with Tournaments
    tzSelect.addEventListener('change', () => {
      localStorage.setItem('pppha_tz', tzSelect.value);
      if (window._lastData) {
        renderRecentHands(window._lastData.recent_hands || []);
        renderRecentWonHands(window._lastData.recent_won_hands || []);
        renderTournaments(window._lastData.tournaments || []);
        updateTzHeaders();
      }
      // Refresh an open tournament detail (hands table + graph) so its dates/times follow the zone.
      const d = window._lastTourneyDetail;
      if (d) {
        renderHandsTable(d.hands, 'tourney-detail-tbody', { showExport: true, exportTid: d.tid });
        _renderTournamentChart(d.hands, d.meta);
      }
    });
  }
});

/* ── Pricing copy ────────────────────────────────────────── */

// Which plan is on sale is set from the admin console (/api/pricing). These are
// the pre-switch values, kept as the fallback so a failed fetch renders the same
// copy the page shipped with rather than an empty CTA.
let _PRICING = {
  label: 'Early Access',
  price_label: 'A$7.99/mo',
  regular_price_label: 'A$13.99/mo',
  is_discounted: true,
};

/** Short CTA label used on the Pro gates, e.g. "Early Access · A$7.99/mo". */
function _pricingCta() {
  return `${_PRICING.label} · ${_PRICING.price_label}`;
}

/** Long CTA label used on the upgrade buttons. */
function _pricingCtaLong() {
  return `Get ${_PRICING.label} — ${_PRICING.price_label.replace('/mo', '/month')}`;
}

/** Push the active plan's wording into the static copy on the page. */
function _applyPricingCopy() {
  document.querySelectorAll('[data-pricing-plan]').forEach(el => {
    el.textContent = _PRICING.label;
  });
  document.querySelectorAll('[data-pricing-price]').forEach(el => {
    el.textContent = _PRICING.price_label;
  });
  document.querySelectorAll('[data-pricing-regular]').forEach(el => {
    el.textContent = _PRICING.regular_price_label;
  });
  document.querySelectorAll('[data-pricing-cta]').forEach(el => {
    el.textContent = _pricingCtaLong();
  });
  // With the full price active there's no discount to show, so the struck-through
  // price and the "locked in until launch" line would both be nonsense.
  document.querySelectorAll('[data-pricing-regular], [data-pricing-discount-only]')
    .forEach(el => el.classList.toggle('d-none', !_PRICING.is_discounted));
}

async function _loadPricing() {
  try {
    const res = await fetch('/api/pricing');
    if (!res.ok) return;
    const p = await res.json();
    if (p && p.label && p.price_label) _PRICING = p;
  } catch (e) {
    console.warn('pricing fetch failed, using default copy', e);
  }
  _applyPricingCopy();
}

/* ── Export ads copy ─────────────────────────────────────── */

// Import/export limits and gate mechanisms, admin-configurable at /admin
// ("Ad Campaigns" -> "Export Ads" / "Import Ads"). These are the shipped
// defaults, kept as the fallback so a failed fetch renders the same copy
// the page shipped with. Shape mirrors the nested public
// /api/export-ads-config response (see export_ads_config_get in app.py) —
// NOT the flat admin config shape.
let _EXPORT_ADS = {
  hand_export:    { hand_hard_limit: 5, hand_soft_limit: 3, gate: 'stub_modal' }, // 2 free, then 3 wait-gated
  tourney_export: { lifetime_free: 1, weekly_limit: 1, gate: 'cpx_survey' },
  import:         { free: 1, gated: 2, total: 3, cadence: 'daily', gate: 'stub_modal' },
};

function _exportAdsHandFreeCount() {
  const h = _EXPORT_ADS.hand_export;
  return Math.max(h.hand_hard_limit - h.hand_soft_limit, 0);
}

/** Push the live import/export limits into the page.
 *
 * The shipped Jinja copy already spells out the default numbers, fully
 * translated. Only overwrite it (in English — no live-fetched copy goes
 * through Flask-Babel) once the live config actually diverges from that
 * shipped default, same pattern this function has always used.
 *
 * Two surfaces read these numbers: the terse tier-compare card list
 * (data-exportads-*-line) and the detailed "Free vs Pro" comparison table
 * (data-exportads-*-cell). Kept as separate attributes/selectors so each
 * surface's copy can differ in verbosity without one JS block clobbering
 * the other. */
function _applyExportAdsCopy() {
  const hand = _EXPORT_ADS.hand_export, tourney = _EXPORT_ADS.tourney_export, imp = _EXPORT_ADS.import;
  const handFree = _exportAdsHandFreeCount();

  // Privacy-paragraph + card use of the plain hand hard limit ("5/day").
  document.querySelectorAll('[data-exportads-hand-hard]').forEach(el => {
    el.textContent = `${hand.hand_hard_limit}/day`;
  });

  // Tier-compare card (short form) — only the tourney/import lines change
  // shape here; the hand line's "N hand exports/day" phrasing is untouched
  // by this task, still driven by the same hand_hard_limit as before.
  if (hand.hand_hard_limit !== 5) {
    document.querySelectorAll('[data-exportads-hand-line]').forEach(el => {
      const n = hand.hand_hard_limit;
      el.textContent = `${n} hand export${n === 1 ? '' : 's'}/day`;
    });
  }
  if (tourney.lifetime_free !== 1 || tourney.weekly_limit !== 1) {
    document.querySelectorAll('[data-exportads-tourney-line]').forEach(el => {
      const plural = tourney.lifetime_free === 1 ? '' : 's';
      el.textContent = `${tourney.lifetime_free} free tournament export${plural}, then ${tourney.weekly_limit}/week`;
    });
  }
  if (imp.total !== 3) {
    document.querySelectorAll('[data-exportads-import-line]').forEach(el => {
      el.textContent = `${imp.total} imports/day`;
    });
  }

  // "Free vs Pro" comparison table (detailed form, honest about the 30s
  // wait vs. the survey — per the 2026-08-24 product-owner directive, imports
  // and hand exports are gated by the self-hosted stub modal, NOT a video ad).
  if (imp.free !== 1 || imp.total !== 3) {
    document.querySelectorAll('[data-exportads-import-cell]').forEach(el => {
      el.textContent = `${imp.free}/day (up to ${imp.total}/day with a 30s wait)`;
    });
  }
  if (handFree !== 2 || hand.hand_hard_limit !== 5) {
    document.querySelectorAll('[data-exportads-hand-cell]').forEach(el => {
      el.textContent = `${handFree}/day (up to ${hand.hand_hard_limit}/day with a 30s wait)`;
    });
  }
  if (tourney.lifetime_free !== 1 || tourney.weekly_limit !== 1) {
    document.querySelectorAll('[data-exportads-tourney-cell]').forEach(el => {
      el.textContent = `${tourney.lifetime_free} free ever, then ${tourney.weekly_limit}/week (with survey)`;
    });
  }
}

async function _loadExportAdsConfig() {
  try {
    const res = await fetch('/api/export-ads-config');
    if (!res.ok) return;
    const c = await res.json();
    if (c && c.hand_export && c.tourney_export && c.import) _EXPORT_ADS = c;
  } catch (e) {
    console.warn('export ads config fetch failed, using default copy', e);
  }
  _applyExportAdsCopy();
}

/* ── Auth helpers ────────────────────────────────────────── */

// UI hint only — mirrors _PERMANENT_ADMIN_EMAILS in app.py so the Admin button
// shows for a permanent admin even before their uid lands in /config/admins.uids.
// Every admin API re-checks server-side; this list grants nothing on its own.
const _PERMANENT_ADMIN_EMAILS = ['caiohn@gmail.com'];

/**
 * Reveal the Admin button for admins. Reads /config/admins (world-readable) the
 * same way the tournaments page does; the server is the actual authority.
 */
async function _checkAdmin(user) {
  const btn = document.getElementById('admin-nav-btn');
  if (!btn) return;
  let isAdmin = false;
  try {
    if (user && _db) {
      if (_PERMANENT_ADMIN_EMAILS.includes((user.email || '').toLowerCase())) {
        isAdmin = true;
      } else {
        const snap = await _db.collection('config').doc('admins').get();
        const uids = (snap.exists && snap.data().uids) || [];
        isAdmin = uids.includes(user.uid);
      }
    }
  } catch (e) {
    console.warn('admin check failed', e);
  }
  btn.classList.toggle('d-none', !isAdmin);
}

/** Returns the Firestore doc ref for the current user (auth) or guest (session). */
function _getUserDocRef() {
  if (!_db) return null;
  if (_currentUser) return _db.collection('users').doc(_currentUser.uid);
  return _db.collection('guests').doc(getSessionId());
}

/**
 * Load (or create) the Firestore user/guest doc and populate _userState.
 * Called whenever auth state changes.
 */
async function _loadUserState() {
  if (!_db) return;
  const ref = _getUserDocRef();
  if (!ref) return;
  try {
    const snap = await ref.get();
    if (snap.exists) {
      _userState = { is_pro: snap.data().is_pro || false };
    } else {
      // First visit — create doc with defaults. Quota and credits are written
      // server-side on first use; the client must not seed them.
      const base = {
        is_pro:     false,
        first_seen: firebase.firestore.FieldValue.serverTimestamp(),
        last_seen:  firebase.firestore.FieldValue.serverTimestamp(),
      };
      if (_currentUser) {
        base.uid   = _currentUser.uid;
        base.email = _currentUser.email;
      } else {
        base.session_id = getSessionId();
      }
      await ref.set(base);
      _userState = { is_pro: false };
    }
    _updateExportGates(); // Refresh gate UI whenever state loads/reloads
  } catch (e) { console.warn('Firestore user state load failed:', e); }
}

/** Re-render the auth bar based on current sign-in state. */
function _renderAuthBar(email) {
  const bar = document.getElementById('auth-bar');
  if (!bar) return;
  if (email) {
    bar.innerHTML =
      `<span class="auth-chip">` +
      `<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>` +
      `<span class="auth-email">${email}</span>` +
      `</span>` +
      `<button class="auth-signout-btn auth-signout-standalone" onclick="signOutUser()">${window.I18N_AUTH?.signOut || 'Sign out'}</button>`;
  } else {
    bar.innerHTML =
      `<button class="auth-chip auth-signin-btn" data-bs-toggle="modal" data-bs-target="#modal-auth">` +
      `<svg xmlns="http://www.w3.org/2000/svg" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>` +
      `<span>${window.I18N_AUTH?.signIn || 'Sign in'}</span>` +
      `</button>`;
  }
}

/**
 * Send a Firebase magic link to the given email.
 * NOTE: For this to work in Firebase Console you must:
 *   1. Enable "Email/Password" provider → enable "Email link (passwordless sign-in)"
 *   2. Add your app's domain to the Authorized Domains list (localhost is pre-authorized)
 */
function sendMagicLink() {
  const t = window.I18N_AUTH || {};
  if (!_auth) {
    const msgEl = document.getElementById('auth-msg');
    if (msgEl) { msgEl.className = 'mt-2'; msgEl.innerHTML = `<span style="color:var(--red)">${t.authServiceUnavailable || 'Auth service not available. Check Firebase config.'}</span>`; msgEl.classList.remove('d-none'); }
    return;
  }
  const emailInput = document.getElementById('auth-email-input');
  const msgEl      = document.getElementById('auth-msg');
  const btn        = document.getElementById('auth-send-btn');
  const email      = (emailInput ? emailInput.value : '').trim();
  if (!email) {
    if (msgEl) { msgEl.className = 'mt-2'; msgEl.innerHTML = `<span style="color:var(--yellow)">${t.enterEmailAddress || 'Please enter your email address.'}</span>`; msgEl.classList.remove('d-none'); }
    return;
  }
  if (btn) btn.disabled = true;
  if (msgEl) { msgEl.className = 'mt-2'; msgEl.innerHTML = `<span style="color:var(--muted)">${t.sending || 'Sending…'}</span>`; msgEl.classList.remove('d-none'); }

  _auth.sendSignInLinkToEmail(email, {
    url: window.location.origin + '/',
    handleCodeInApp: true,
  }).then(() => {
    localStorage.setItem('emailForSignIn', email);
    if (msgEl) msgEl.innerHTML = `<span style="color:var(--green)">✓ ${t.linkSentTo || 'Link sent to'} <strong>${email}</strong> — ${t.checkYourInbox || 'check your inbox.'}</span>`;
    if (btn) btn.disabled = false;
  }).catch(err => {
    if (msgEl) msgEl.innerHTML = `<span style="color:var(--red)">${err.message || t.failedToSendLink || 'Failed to send link.'}</span>`;
    if (btn) btn.disabled = false;
  });
}

/** Sign out the current user. */
function signOutUser() {
  if (!_auth) return;
  _auth.signOut().then(() => {
    _currentUser = null;
    _userState   = { is_pro: false };
    window._lastData = null;
    window._lastTourneyDetail = null;
    window._anonGraphs = null;
    _clearPendingSession();

    // Reset UI to blank-slate state
    const urlInput = document.getElementById('url-input');
    if (urlInput) urlInput.value = '';
    const results = document.getElementById('results-section');
    if (results) results.classList.add('d-none');
    const loadingMsg = document.getElementById('loading-msg');
    if (loadingMsg) loadingMsg.classList.add('d-none');

    const ts = document.getElementById('tournament-summary-section');
    if (ts) ts.classList.add('d-none');
    const th = document.getElementById('tournament-history-pro-section');
    if (th) th.classList.add('d-none');
    const cgs = document.getElementById('cash-games-summary-section');
    if (cgs) cgs.classList.add('d-none');
    const cgsd = document.getElementById('cash-game-detail-section');
    if (cgsd) cgsd.classList.add('d-none');
    _renderAuthBar(null);
    _updateExportGates();
    _loadUserState();
  }).catch(e => console.warn('Sign out failed:', e));
}

/** Sign in with Google popup. onAuthStateChanged handles the rest. */
function signInWithGoogle() {
  const t = window.I18N_AUTH || {};
  const msgEl = document.getElementById('auth-msg');
  if (!_auth) {
    if (msgEl) {
      msgEl.className = 'mt-2';
      msgEl.innerHTML = `<span style="color:var(--red,#f85149)">${t.authNotReady || 'Auth not ready — please wait a moment and try again.'}</span>`;
      msgEl.classList.remove('d-none');
    }
    return;
  }
  const provider = new firebase.auth.GoogleAuthProvider();
  _auth.signInWithPopup(provider)
    .then(() => {
      const modal = bootstrap.Modal.getInstance(document.getElementById('modal-auth'));
      if (modal) modal.hide();
    })
    .catch(err => {
      if (msgEl) {
        msgEl.className = 'mt-2';
        msgEl.innerHTML = `<span style="color:var(--red,#f85149)">${err.message || t.googleSignInFailed || 'Google sign-in failed.'}</span>`;
        msgEl.classList.remove('d-none');
      }
    });
}

/* ── Firebase ─────────────────────────────────────────────── */

function _trackEvent(name, params) {
  try {
    if (_analytics) _analytics.logEvent(name, params || {});
  } catch {}
}

async function _initFirebase() {
  // #auth-bar starts empty in the HTML; leave it that way until onAuthStateChanged
  // (or the SDK-unavailable fallback below) resolves the real state — rendering
  // "Sign in" eagerly here caused a flash of the signed-out UI for signed-in users.
  try {
    const res = await fetch('/api/firebase-config');
    if (!res.ok) return _firebaseUnavailable();
    const cfg = await res.json();
    if (!cfg.FIREBASE_API_KEY) return _firebaseUnavailable();
    if (typeof firebase === 'undefined') return _firebaseUnavailable();

    firebase.initializeApp({
      apiKey:            cfg.FIREBASE_API_KEY,
      authDomain:        cfg.FIREBASE_AUTH_DOMAIN,
      projectId:         cfg.FIREBASE_PROJECT_ID,
      storageBucket:     cfg.FIREBASE_STORAGE_BUCKET,
      messagingSenderId: cfg.FIREBASE_MESSAGING_SENDER_ID,
      appId:             cfg.FIREBASE_APP_ID,
      measurementId:     cfg.FIREBASE_MEASUREMENT_ID,
    });

    _analytics = firebase.analytics();
    _db        = firebase.firestore();
    _auth      = firebase.auth ? firebase.auth() : null;

    // Unlock auth buttons now that Firebase is ready
    ['btn-google-signin', 'auth-send-btn'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.disabled = false;
    });

    // ── Handle magic-link redirect (must run before onAuthStateChanged) ──
    if (_auth && _auth.isSignInWithEmailLink(window.location.href)) {
      let email = localStorage.getItem('emailForSignIn');
      if (!email) email = window.prompt('Please confirm your email to complete sign-in:') || '';
      if (email) {
        try {
          await _auth.signInWithEmailLink(email, window.location.href);
          localStorage.removeItem('emailForSignIn');
          window.history.replaceState({}, document.title, '/');
        } catch (e) { console.warn('Magic link sign-in failed:', e); }
      }
    }

    // ── Auth state listener — fires immediately with current user (or null) ──
    if (_auth) {
      _auth.onAuthStateChanged(async (user) => {
        _currentUser = user;
        await _loadUserState();
        _resolveTierUI();   // reveal the tier UI even if _loadUserState bailed early
        _renderAuthBar(user ? user.email : null);
        _checkAdmin(user);  // hides the button again on sign-out
        _loadGamification();  // hides the banner again on sign-out
        if (user) {
          _getUserDocRef().set({
            email:     user.email,
            last_seen: firebase.firestore.FieldValue.serverTimestamp(),
          }, { merge: true }).catch(() => {});
          // An import made before signing in is adopted first, so the history
          // load below already includes it instead of racing it.
          await _claimPendingSession();
          _loadHistory();     // free accounts have history too, just 7 days of it
        }
      });
    } else {
      // Auth SDK not available — load guest state directly
      await _loadUserState();
      _resolveTierUI();
      _renderAuthBar(null);
    }

    _trackEvent('app_open');
  } catch (e) {
    console.warn('Firebase init failed:', e);
    _firebaseUnavailable();
  }
}

/**
 * Firebase couldn't start (config fetch failed, no API key, SDK blocked, init
 * threw). Nothing will ever resolve the auth/tier state, so fall back to the
 * signed-out free view instead of leaving the header and tier cards blank.
 */
function _firebaseUnavailable() {
  _resolveTierUI();
  _renderAuthBar(null);
}

// Kick off Firebase after the page is interactive (non-blocking). Pricing is
// fetched independently so the CTAs still show the active plan if Firebase is
// unavailable.
function _boot() { _initFirebase(); _loadPricing(); _loadExportAdsConfig(); }

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _boot);
} else {
  _boot();
}
