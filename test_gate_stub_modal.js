// test_gate_stub_modal.js — logic test for the "watch to unlock" stub modal
// (_showGateStubModal and friends) in static/app.js.
//
// This is the self-hosted stand-in for a real rewarded-video ad while
// ayeT-Studios/Wannads publisher approvals are pending: 30s of forced
// attention, an OK button that only enables once the timer elapses, and a
// completion POST that must fire exactly once even on a double-click.
//
// static/app.js is loaded directly (it is already a standalone script, not
// extracted from a template like leaks.html) into a vm context with a fake
// DOM, a fake bootstrap.Modal, a fake fetch, and hand-rolled fake timers so
// the 30s countdown can be driven synchronously instead of actually waiting.
//
//     node test_gate_stub_modal.js

const fs = require('fs');
const vm = require('vm');

// ── Fake timers — manual virtual clock, no real waiting ─────────────────────

function makeFakeTimers() {
  let now = 0;
  let idc = 1;
  const timers = new Map();   // id -> { time, cb, interval }

  function setTimeout(cb, ms) {
    const id = idc++;
    timers.set(id, { time: now + ms, cb, interval: null });
    return id;
  }
  function setInterval(cb, ms) {
    const id = idc++;
    timers.set(id, { time: now + ms, cb, interval: ms });
    return id;
  }
  function clearTimeout(id) { timers.delete(id); }
  function clearInterval(id) { timers.delete(id); }

  function advance(ms) {
    const target = now + ms;
    for (;;) {
      let earliestId = null, earliestTime = Infinity;
      for (const [id, t] of timers) {
        if (t.time <= target && t.time < earliestTime) { earliestTime = t.time; earliestId = id; }
      }
      if (earliestId === null) break;
      now = earliestTime;
      const t = timers.get(earliestId);
      if (t.interval != null) t.time = now + t.interval; else timers.delete(earliestId);
      t.cb();
    }
    now = target;
  }

  return { setTimeout, setInterval, clearTimeout, clearInterval, advance };
}

// ── Fake DOM ─────────────────────────────────────────────────────────────
// A persistent id -> element registry (unlike a fresh object per call) so the
// test can inspect state app.js mutated on an earlier getElementById() call.

class FakeClassList {
  constructor() { this.set = new Set(); }
  add(...c) { c.forEach(x => this.set.add(x)); }
  remove(...c) { c.forEach(x => this.set.delete(x)); }
  contains(c) { return this.set.has(c); }
}

function fakeEl(id) {
  return {
    id, textContent: '', innerHTML: '', value: '', disabled: false, checked: false,
    classList: new FakeClassList(), onclick: null, style: {},
    setAttribute() {}, removeAttribute() {}, addEventListener() {},
    querySelector: () => fakeEl(''),
  };
}

function makeFakeDocument() {
  const elements = new Map();
  return {
    readyState: 'loading',   // keep app.js's bottom-of-file _boot() from firing
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, fakeEl(id));
      return elements.get(id);
    },
    addEventListener() {},
    querySelectorAll: () => [],
    querySelector: () => fakeEl(''),
  };
}

// ── Fake bootstrap.Modal ────────────────────────────────────────────────

function makeFakeBootstrap() {
  const instances = new Map();
  class FakeModalInstance {
    constructor(el) { this.el = el; this.shown = false; this.showCount = 0; }
    show() { this.shown = true; this.showCount++; }
    hide() { this.shown = false; }
  }
  return {
    Modal: {
      getOrCreateInstance(el) {
        if (!instances.has(el)) instances.set(el, new FakeModalInstance(el));
        return instances.get(el);
      },
      getInstance(el) { return instances.get(el) || null; },
    },
    _instances: instances,
  };
}

// ── Load static/app.js into a vm context ────────────────────────────────

function loadFunctions() {
  const src = fs.readFileSync(__dirname + '/static/app.js', 'utf8');

  const timers = makeFakeTimers();
  const document = makeFakeDocument();
  const bootstrap = makeFakeBootstrap();
  const fetchCalls = [];
  let fetchImpl = () => Promise.resolve({ ok: true, json: async () => ({}) });

  const sandbox = {
    document,
    bootstrap,
    firebase: {
      initializeApp: () => {},
      firestore: { FieldValue: { serverTimestamp: () => 'SERVER_TS' } },
      auth: () => ({ onAuthStateChanged: () => {} }),
    },
    fetch: (url, opts) => { fetchCalls.push({ url, opts }); return fetchImpl(url, opts); },
    console,
    URLSearchParams,
    Date, Math, JSON,
    crypto: { randomUUID: () => 'fixed-test-uuid' },
    location: { search: '' },
    navigator: {},
    setTimeout: timers.setTimeout, clearTimeout: timers.clearTimeout,
    setInterval: timers.setInterval, clearInterval: timers.clearInterval,
    GATE_STUB_MODAL_ENABLED: true,
    I18N_GATE_STUB: {
      kindLabelImport: 'this import', kindLabelHandExport: 'this hand export',
      featureImport: 'imports', featureHandExport: 'hand exports',
      unlockCountdown: 'Unlock in __SECONDS__s…', unlockReady: 'Unlock',
    },
  };
  sandbox.window = sandbox;   // window.X === X, matching how a real page's globals work
  vm.createContext(sandbox);

  // Appended to the SAME script so this closes over app.js's top-level `let`
  // bindings (_gateStubState, _currentUser) — a second runInContext call would
  // be a separate lexical scope and could not reach them (see the identical
  // technique in test_leaks_compare.js).
  vm.runInContext(src + `
    var __exported = {
      showGateStubModal: _showGateStubModal,
      okClicked: _gateStubOkClicked,
      closeGateStubModal: closeGateStubModal,
      getState: () => _gateStubState,
      setCurrentUser: (u) => { _currentUser = u; },
      GATE_STUB_SECONDS: _GATE_STUB_SECONDS,
    };
  `, sandbox);

  return {
    T: sandbox.__exported,
    document, bootstrap, timers,
    fetchCalls,
    setFetchImpl: (fn) => { fetchImpl = fn; },
  };
}

async function main() {
  let failures = 0;
  const check = (label, cond, detail) => {
    if (!cond) { failures++; console.log('  FAIL', label, detail !== undefined ? '— ' + String(detail) : ''); }
  };

  const { T, document, bootstrap, timers, fetchCalls } = loadFunctions();

  const fakeUser = { getIdToken: async () => 'fake-token' };
  T.setCurrentUser(fakeUser);

  check('30s constant matches the AC', T.GATE_STUB_SECONDS === 30, T.GATE_STUB_SECONDS);

  // ── 1. Modal renders on trigger, OK button starts disabled at 30s ───────
  T.showGateStubModal('hand_export', () => {});

  const modalEl = document.getElementById('gate-stub-modal');
  const modalInstance = bootstrap._instances.get(modalEl);
  check('modal shown', !!modalInstance && modalInstance.shown === true);

  const okBtn = document.getElementById('gate-stub-ok-btn');
  check('OK button starts disabled', okBtn.disabled === true);
  check('OK button starts at 30s remaining', T.getState().remaining === 30, T.getState().remaining);
  check('OK button shows the 30s countdown text', okBtn.textContent === 'Unlock in 30s…', okBtn.textContent);

  const kindLabelEl = document.getElementById('gate-stub-kind-label');
  const featureLabelEl = document.getElementById('gate-stub-feature-label');
  check('kind label set for hand_export', kindLabelEl.textContent === 'this hand export', kindLabelEl.textContent);
  check('feature label set for hand_export', featureLabelEl.textContent === 'hand exports', featureLabelEl.textContent);

  // ── 2. Countdown decrements each second ──────────────────────────────────
  timers.advance(1000);
  check('remaining decrements after 1s', T.getState().remaining === 29, T.getState().remaining);
  check('button text reflects 29s', okBtn.textContent === 'Unlock in 29s…', okBtn.textContent);
  check('still disabled at 29s', okBtn.disabled === true);

  timers.advance(5000);
  check('remaining decrements over multiple ticks', T.getState().remaining === 24, T.getState().remaining);
  check('button text reflects 24s', okBtn.textContent === 'Unlock in 24s…', okBtn.textContent);

  // ── 3. OK enables at t=30s, label switches to "Unlock" ───────────────────
  timers.advance(24000);   // total elapsed: 30s
  check('remaining hits zero', T.getState().remaining === 0, T.getState().remaining);
  check('OK button enabled at 30s', okBtn.disabled === false);
  check('OK button label switches to Unlock', okBtn.textContent === 'Unlock', okBtn.textContent);

  // Clicking before the button was ever enabled must not have been possible —
  // sanity-check the guard directly: it does nothing while remaining > 0.
  {
    const { T: T2, document: doc2, timers: timers2 } = loadFunctions();
    T2.setCurrentUser(fakeUser);
    T2.showGateStubModal('import', () => {});
    T2.okClicked();   // remaining is still 30 — must be a no-op
    check('OK click ignored before timer elapses', T2.getState().posted === false);
    check('no completion recorded prematurely', T2.getState().remaining === 30);
  }

  // ── 4/5. Completion POST fires exactly once on double-click, correct kind ─
  let completed = 0;
  T.getState().onComplete = () => { completed++; };   // reattach in case earlier steps changed it
  // Re-derive state kind for the assertion below (still 'hand_export' from step 1).
  const kindBefore = T.getState().kind;

  T.okClicked();
  T.okClicked();   // double-click — must not post twice

  check('okClicked marks posted after first click', T.getState().posted === true);

  // _gateStubOkClicked's completion chain is `async function await getIdToken()`
  // then `await fetch()` then `.catch().then(...)` — several microtask hops.
  // Flush enough of them (no real macrotask/timer involved) before asserting.
  for (let i = 0; i < 20; i++) await Promise.resolve();

  const posts = fetchCalls.filter(c => c.url === '/api/gate/stub-completion');
  check('exactly one completion POST sent', posts.length === 1, posts.length);
  if (posts.length >= 1) {
    const body = JSON.parse(posts[0].opts.body);
    check('POST carries the correct kind', body.kind === kindBefore, body.kind);
    check('POST carries a completion_id', typeof body.completion_id === 'string' && body.completion_id.length > 0);
    check('POST is authenticated', posts[0].opts.headers.Authorization === 'Bearer fake-token');
  }
  check('onComplete fired exactly once', completed === 1, completed);

  console.log(failures === 0 ? 'gate stub modal: PASS' : `gate stub modal: FAIL (${failures})`);
  process.exit(failures === 0 ? 0 : 1);
}

main();
