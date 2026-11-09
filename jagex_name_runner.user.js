// ==UserScript==
// @name         Jagex Name Runner (local queue)
// @namespace    local.jagex-name-runner
// @version      1.1.0
// @description  Checks queued character names on the official Jagex page; never submits a name change.
// @match        https://account.jagex.com/*
// @match        https://secure.runescape.com/m=displaynames/*
// @match        https://secure.runescape.com/m=weblogin/*
// @run-at       document-idle
// @grant        GM_xmlhttpRequest
// @connect      127.0.0.1
// ==/UserScript==

(() => {
  'use strict';

  const API = 'http://127.0.0.1:8765';
  const STORAGE_KEY = 'jagex-name-runner-id-v1';
  const RESULT_TIMEOUT_MS = 45000;
  const PROGRESS_EVERY_MS = 10000;
  const state = { runnerId: loadRunnerId(), running: false, paused: false, stopped: false, oneOffPending: false, startPending: false,
    active: null, activeInterrupted: false, startedAt: 0, currentName: null, timer: null, observer: null,
    remainingSeconds: null, remainingAttempts: null };

  function loadRunnerId() {
    let id = localStorage.getItem(STORAGE_KEY);
    if (!id) { id = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`); localStorage.setItem(STORAGE_KEY, id); }
    return id;
  }
  function request(path, method = 'POST', body) {
    return new Promise((resolve, reject) => GM_xmlhttpRequest({
      method, url: `${API}${path}`, headers: { 'Content-Type': 'application/json', 'X-Name-Runner': '1' },
      data: body === undefined ? undefined : JSON.stringify(body), timeout: 10000,
      onload: r => { if (r.status < 200 || r.status >= 300) return reject(new Error(`Local API ${r.status}`));
        try { resolve(r.responseText ? JSON.parse(r.responseText) : {}); } catch { reject(new Error('Invalid API response')); } },
      onerror: () => reject(new Error('Local API unavailable; runner paused.')),
      ontimeout: () => reject(new Error('Local API timed out; runner paused.'))
    }));
  }
  function log(message) { const el = document.querySelector('#jnr-log'); if (el) { el.textContent = message; el.title = message; } }
  function updateButtons() {
    const set = (id, disabled) => { const e = document.getElementById(id); if (e) e.disabled = disabled; };
    set('jnr-start', state.running || state.active !== null || state.startPending);
    set('jnr-pause', !state.running || state.paused); set('jnr-resume', !state.running || !state.paused);
    set('jnr-stop', !state.running && !state.active);
  }
  function panel() {
    if (document.getElementById('jnr-panel')) return;
    const box = document.createElement('section'); box.id = 'jnr-panel';
    box.style.cssText = 'position:fixed;z-index:2147483647;right:16px;bottom:16px;width:300px;padding:12px;background:#171923;color:#fff;border:1px solid #687087;border-radius:8px;font:13px/1.4 system-ui,sans-serif;box-shadow:0 4px 24px #0008';
    box.innerHTML = `<strong>Jagex Name Runner</strong><div style="opacity:.75;margin:4px 0 8px">Queue ${state.runnerId.slice(0,8)}…</div>
      <div style="display:flex;gap:5px;flex-wrap:wrap"><button id="jnr-start">Start</button><button id="jnr-pause">Pause</button><button id="jnr-resume">Resume</button><button id="jnr-stop">Stop</button></div>
      <div style="display:flex;gap:5px;margin-top:8px"><input id="jnr-oneoff" placeholder="One-off name" maxlength="12" style="min-width:0;flex:1"><button id="jnr-check">Check once</button></div>
      <div id="jnr-log" aria-live="polite" style="margin-top:8px;min-height:18px">Idle</div>`;
    document.body.appendChild(box);
    box.querySelectorAll('button').forEach(b => { b.style.cssText = 'cursor:pointer;padding:5px 8px;background:#e5b94f;color:#171923;border:1px solid #f0d27b;border-radius:4px;font-weight:700'; });
    box.querySelector('#jnr-oneoff').style.cssText = 'min-width:0;flex:1;padding:5px;background:#fff;color:#171923;border:1px solid #bbb;border-radius:4px';
    box.querySelector('#jnr-start').onclick = () => start();
    box.querySelector('#jnr-pause').onclick = () => pause();
    box.querySelector('#jnr-resume').onclick = () => resume();
    box.querySelector('#jnr-stop').onclick = () => stop();
    box.querySelector('#jnr-check').onclick = () => oneOff(); updateButtons();
  }

  // Jagex's result does not repeat the candidate name. The adapter therefore watches
  // a fresh validation transition after a single update to this exact input field.
  function findNameField() {
    const candidates = [...document.querySelectorAll('input:not([type=hidden]):not([type=submit]):not([type=button])')];
    return candidates.find(e => {
      if (e.disabled || e.readOnly) return false;
      const meta = [e.name, e.id, e.placeholder, e.getAttribute('aria-label'), e.labels?.[0]?.textContent].join(' ').toLowerCase();
      return /character.?name|display.?name|new.?name|name.?change/.test(meta);
    }) || null;
  }
  function findSetNameButton() {
    return [...document.querySelectorAll('button,input[type="submit"],input[type="button"]')]
      .find(e => !e.closest('#jnr-panel') && /\bset\s+name\b/i.test(e.value || e.innerText || e.textContent || '')) || null;
  }
  function namePanel(field, button) {
    let node = field.parentElement;
    while (node && button && !node.contains(button)) node = node.parentElement;
    return node || field.closest('form') || document.body;
  }
  function visible(el) {
    if (!el || !el.isConnected || !el.getClientRects().length) return false;
    const style = getComputedStyle(el);
    return style.display !== 'none' && style.visibility !== 'hidden' && style.opacity !== '0';
  }
  function unavailableNode(root) {
    const pattern = /character name is not available/i;
    return [...root.querySelectorAll('p,span,div,small,label,[role="alert"]')].find(el =>
      visible(el) && pattern.test(el.innerText || el.textContent || '') &&
      ![...el.children].some(child => pattern.test(child.innerText || child.textContent || ''))
    ) || null;
  }
  function checkmarkVisible(field) {
    const wrapper = field.parentElement;
    if (!wrapper) return false;
    return [wrapper, ...wrapper.querySelectorAll('span,i,svg')].some(el =>
      visible(el) && (/^[\s✓✔]+$/.test(el.innerText || el.textContent || '') && /[✓✔]/.test(el.innerText || el.textContent || '') ||
        /(?:^|[-_ ])(?:checkmark|valid-name|name-valid|success-icon)(?:$|[-_ ])/.test(String(el.className?.baseVal || el.className || '').toLowerCase()))
    );
  }
  function formSnapshot(field, button, root) {
    const error = unavailableNode(root);
    const style = button ? getComputedStyle(button) : null;
    return {
      error: Boolean(error), errorNode: error, errorMarkup: error?.outerHTML || '',
      buttonUsable: Boolean(button && !button.disabled && button.getAttribute('aria-disabled') !== 'true' && !/(?:^|\s)disabled(?:\s|$)/i.test(button.className || '')),
      buttonVisual: button ? [button.className, button.disabled, button.getAttribute('aria-disabled'), style.backgroundColor, style.backgroundImage, style.borderColor, style.opacity].join('|') : '',
      checkmark: checkmarkVisible(field)
    };
  }
  function detectVerdict(current, signals) {
    const requestSettled = signals.networkAt && signals.now - signals.networkAt >= 120;
    if (current.error && (signals.errorAppearedAt || signals.errorChangedAt || requestSettled))
      return 'unavailable';
    if (!current.error && current.buttonUsable &&
        (signals.checkmarkAt || signals.buttonChangedAt || requestSettled))
      return 'available';
    return null;
  }
  function setInputValue(field, value) {
    const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(field), 'value')?.set;
    if (setter) setter.call(field, value); else field.value = value;
    field.dispatchEvent(new Event('input', { bubbles: true }));
    field.dispatchEvent(new Event('change', { bubbles: true }));
  }
  function canonicalName(name) { return String(name || '').trim().toLowerCase().replace(/[ _]+/g, ' '); }
  function readSiteLimits() {
    const text = (document.body?.innerText || '').replace(/\s+/g, ' ');
    let seconds = null, attempts = null;
    let m = text.match(/(?:time|session)\s+remaining\s*:?\s*(\d{1,2}):(\d{2})/i) || text.match(/(\d+)\s*(?:minutes?|mins?)\s*(?:(\d+)\s*(?:seconds?|secs?))?\s*(?:remaining|left)/i);
    if (m) seconds = Number(m[1]) * 60 + Number(m[2] || 0);
    m = text.match(/(\d+)\s*(?:attempts?|checks?)\s*(?:remaining|left)/i) || text.match(/(?:remaining|left)\s*:?\s*(\d+)\s*(?:attempts?|checks?)/i);
    if (m) attempts = Number(m[1]);
    return { seconds, attempts };
  }
  function siteStopReason() {
    const text = (document.body?.innerText || '').replace(/\s+/g, ' ');
    if (/session (?:has )?expired|time limit (?:has )?expired|your session timed out/i.test(text)) return 'Site session expired.';
    if (/too many requests|rate limit|temporarily throttled|try again later/i.test(text)) return 'Jagex throttled the checks.';
    if (/queue (?:is )?empty|no more names (?:to check|in queue)/i.test(text)) return 'Site queue is empty.';
    return null;
  }
  async function enterAndObserve(name) {
    const field = findNameField();
    if (!field) throw new Error('Name field not recognized. Update the site adapter selectors; no verdict sent.');
    const button = findSetNameButton();
    const root = namePanel(field, button);
    const baseline = formSnapshot(field, button, root);
    const start = Date.now();
    const startPerf = performance.now();
    let last = baseline;
    let errorAppearedAt = 0, errorChangedAt = 0, buttonChangedAt = 0, checkmarkAt = 0, networkAt = 0;
    const mutations = new MutationObserver(() => {
      const current = formSnapshot(field, button, root);
      if (Date.now() - start >= 500 && current.error &&
          (current.errorNode !== baseline.errorNode || current.errorMarkup !== baseline.errorMarkup))
        errorChangedAt = Date.now();
    });
    mutations.observe(root, { subtree: true, childList: true, characterData: true, attributes: true });
    let resources = null;
    try {
      resources = new PerformanceObserver(list => {
        for (const entry of list.getEntries()) {
          if (entry.startTime >= startPerf && /^(fetch|xmlhttprequest)$/i.test(entry.initiatorType) &&
              new URL(entry.name).origin === location.origin) networkAt = Date.now();
        }
      });
      resources.observe({ entryTypes: ['resource'] });
    } catch { resources = null; }
    setInputValue(field, name);
    return await new Promise((resolve, reject) => {
      let timer;
      const finish = (verdict, error) => {
        clearTimeout(timer); mutations.disconnect(); resources?.disconnect();
        if (error) reject(error); else resolve(verdict);
      };
      const check = () => {
        if (!document.contains(field)) return finish(null, new Error('Name form changed during check; no verdict sent.'));
        const stopReason = siteStopReason();
        if (stopReason) return finish(null, new Error(`${stopReason} No verdict sent.`));
        const limits = readSiteLimits();
        if (limits.seconds !== null && limits.seconds <= 0 || limits.attempts !== null && limits.attempts <= 0)
          return finish(null, new Error('Site limit reached. No verdict sent.'));
        if (canonicalName(field.value) !== canonicalName(name))
          return finish(null, new Error('Name field changed during check; lease left pending.'));
        const current = formSnapshot(field, button, root);
        const now = Date.now();
        if (now - start >= 500) {
          if (!last.error && current.error) errorAppearedAt = now;
          if (!last.checkmark && current.checkmark) checkmarkAt = now;
          if (!current.error && current.buttonUsable && current.buttonVisual !== last.buttonVisual &&
              current.buttonVisual !== baseline.buttonVisual) buttonChangedAt = now;
        }
        last = current;
        const verdict = detectVerdict(current, { errorAppearedAt, errorChangedAt, buttonChangedAt, checkmarkAt, networkAt, now });
        if (verdict) return finish(verdict);
        if (now - start >= RESULT_TIMEOUT_MS)
          return finish(null, new Error(`No fresh Jagex validation transition for ${name}; lease left pending.`));
        timer = setTimeout(check, 100);
      }; check();
    });
  }

  async function submitVerdict(job, verdict) {
    const siteLimits = readSiteLimits();
    const payload = { runner_id: state.runnerId, candidate_id: job.candidate.id,
      lease_id: job.lease_id, verdict, checked_at: new Date().toISOString() };
    if (siteLimits.seconds !== null) payload.remaining_seconds = siteLimits.seconds;
    if (siteLimits.attempts !== null) payload.remaining_attempts = siteLimits.attempts;
    await request('/api/runner/verdict', 'POST', payload);
  }
  async function runLease(job) {
    state.active = job; state.activeInterrupted = false; state.currentName = job?.candidate?.name || null; state.startedAt = Date.now(); updateButtons();
    log(`Checking ${job.candidate?.name || 'candidate'}…`);
    const progressTimer = setInterval(() => {
      if (!state.active || state.active.lease_id !== job.lease_id) return;
      const siteLimits = readSiteLimits();
      const progress = { runner_id: state.runnerId };
      if (siteLimits.seconds !== null) progress.remaining_seconds = siteLimits.seconds;
      if (siteLimits.attempts !== null) progress.remaining_attempts = siteLimits.attempts;
      if (siteLimits.seconds !== null || siteLimits.attempts !== null)
        request('/api/runner/progress', 'POST', progress).catch(() => {});
    }, PROGRESS_EVERY_MS);
    try {
      if (!job.candidate?.id || !job.candidate?.name || !job.lease_id) throw new Error('Malformed lease; no site action taken.');
      const verdict = await enterAndObserve(job.candidate.name);
      if (state.stopped || state.paused || state.activeInterrupted) return;
      await submitVerdict(job, verdict); log(`${job.candidate.name}: ${verdict}.`);
      if (verdict === 'available') { state.paused = true; await request('/api/runner/pause', 'POST', {}); log(`${job.candidate.name} is available. Paused; review the site to claim manually.`); }
    } catch (e) {
      if (/^(Site session expired|Jagex throttled|Site queue is empty|Site limit reached)/.test(e.message)) {
        await stop(`${e.message} Lease remains pending.`);
        log(`${e.message} Stopped; lease remains pending.`);
      } else {
        state.paused = true;
        log(`${e.message} Paused; lease remains pending.`);
        try { await request('/api/runner/pause', 'POST', {}); } catch {}
      }
    }
    finally { clearInterval(progressTimer); state.active = null; state.currentName = null; updateButtons(); }
  }
  async function nextLoop() {
    if (!state.running || state.paused || state.stopped || state.active) return;
    try {
      const siteReason = siteStopReason();
      const siteLimits = readSiteLimits();
      if (siteReason) { await stop(siteReason); return; }
      if (siteLimits.seconds !== null && siteLimits.seconds <= 0 || siteLimits.attempts !== null && siteLimits.attempts <= 0) { await stop('Site time or attempt limit reached.'); return; }
      const job = await request(`/api/runner/next?runner_id=${encodeURIComponent(state.runnerId)}`, 'GET');
      if (job.status && /expired|throttl|queue.empty|exhausted|stopped|empty/i.test(job.status)) { await stop(job.status); return; }
      state.remainingSeconds = Number.isFinite(job.remaining_seconds) ? job.remaining_seconds : null;
      state.remainingAttempts = Number.isFinite(job.remaining_attempts) ? job.remaining_attempts : null;
      if (state.remainingSeconds !== null && state.remainingSeconds <= 0 || state.remainingAttempts !== null && state.remainingAttempts <= 0) { await stop('Runner time or attempt limit reached.'); return; }
      if (!job.candidate) { await stop('Queue is empty.'); return; }
      runLease(job).catch(e => { state.paused = true; log(`${e.message} Paused; lease remains pending.`); }).then(() => { if (state.running && !state.paused && !state.stopped) state.timer = setTimeout(nextLoop, 0); });
    } catch (e) { state.paused = true; log(`${e.message} Paused.`); updateButtons(); }
  }
  async function start() {
    if (state.startPending || state.running || state.active) return;
    state.startPending = true; updateButtons();
    state.stopped = false; state.running = true; state.paused = false;
    try {
      let session = await request('/api/runner/start', 'POST', { runner_id: state.runnerId });
      if (session.status === 'expired') {
        localStorage.removeItem(STORAGE_KEY);
        state.runnerId = loadRunnerId();
        session = await request('/api/runner/start', 'POST', { runner_id: state.runnerId });
      }
      if (session.status === 'paused') {
        state.paused = true;
        log('Session is paused. Press Resume when ready.');
      } else if (session.status !== 'running') {
        throw new Error(`Cannot start runner: ${session.status || 'unknown status'}`);
      } else {
        log('Runner started.');
        nextLoop();
      }
    }
    catch (e) { state.running = false; state.paused = true; log(`${e.message} Start failed.`); }
    finally { state.startPending = false; updateButtons(); }
  }
  async function pause() { state.paused = true; if (state.active) state.activeInterrupted = true; clearTimeout(state.timer); try { await request('/api/runner/pause', 'POST', {}); } catch (e) { log(e.message); } updateButtons(); }
  async function resume() { if (state.stopped) return; state.paused = false; state.running = true; try { await request('/api/runner/resume', 'POST', {}); nextLoop(); } catch (e) { state.paused = true; log(e.message); } updateButtons(); }
  async function stop(reason = 'Stopped.') { state.stopped = true; state.running = false; state.paused = false; if (state.active) state.activeInterrupted = true; clearTimeout(state.timer); try { await request('/api/runner/stop', 'POST', {}); } catch (e) { log(e.message); } if (/session expired|queue is empty|time or attempt limit reached|^expired$/i.test(reason)) { localStorage.removeItem(STORAGE_KEY); state.runnerId = loadRunnerId(); } if (!state.active) log(reason); updateButtons(); }
  async function oneOff() {
    const name = document.getElementById('jnr-oneoff')?.value.trim(); if (!name) { log('Enter a name for the one-off check.'); return; }
    if (state.active || state.running && !state.paused || state.oneOffPending) { log('Pause the queue before a one-off check.'); return; }
    state.oneOffPending = true;
    let startedForOneOff = false;
    const wasPaused = state.paused;
    try {
      startedForOneOff = !state.running;
      if (startedForOneOff) {
        await request('/api/runner/start', 'POST', { runner_id: state.runnerId });
        state.stopped = false;
        state.running = true;
      }
      state.paused = false;
      const job = await request('/api/runner/oneoff', 'POST', { runner_id: state.runnerId, candidate_id: canonicalName(name), defer_pending: true });
      const lease = job.candidate ? job : null;
      if (!lease) throw new Error('One-off API did not return a lease.');
      // Use queue API result only; local display names never enter another account's form.
      if (canonicalName(lease.candidate.name) !== canonicalName(name)) throw new Error('One-off response name mismatch.');
      await runLease(lease);
      if (startedForOneOff && !state.paused) await stop('One-off check complete; queue remains stopped.');
    } catch (e) { log(`${e.message} One-off check not completed.`); }
    finally {
      if (wasPaused && !state.paused) await pause();
      if (startedForOneOff && !state.paused && !state.stopped) await stop('One-off check ended; queue remains stopped.');
      state.oneOffPending = false;
    }
  }
  function init() { panel(); state.observer = new MutationObserver(panel); state.observer.observe(document.documentElement, { childList: true, subtree: true }); }
  if (globalThis.__JNR_TEST__) globalThis.__JNR_TEST__.detectVerdict = detectVerdict;
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true }); else init();
})();
