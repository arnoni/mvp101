(() => { "use strict";
  // ── 1. AccessState ──────────────────────────────────────────────
  let accessStateValue = { tier: "free", demandAllowed: false, dailyLimit: 3 };
  const accessListeners = new Set();
  function emitAccessState() {
    const snapshot = { ...accessStateValue };
    accessListeners.forEach((listener) => { try { listener(snapshot); } catch (err) { logClientException("access_state_listener_failed", err); } });
  }
  function readDomAccess() {
    const appEl = document.getElementById("app");
    const tier = appEl?.dataset.tier || document.body?.dataset.tier || "free";
    const demandRaw = appEl?.dataset.demandAllowed ?? document.body?.dataset.demandAllowed ?? "false";
    const limitRaw = appEl?.dataset.dailyLimit ?? document.body?.dataset.dailyLimit ?? "3";
    const dailyLimit = Number.parseInt(limitRaw, 10);
    accessStateValue = { tier: String(tier || "free"), demandAllowed: demandRaw === "true", dailyLimit: Number.isFinite(dailyLimit) ? dailyLimit : 3 };
    emitAccessState();
    return { ...accessStateValue };
  }
  function setAccessState(next) {
    accessStateValue = { ...accessStateValue, ...(next || {}) };
    const appEl = document.getElementById("app");
    const demandAllowed = accessStateValue.demandAllowed ? "true" : "false";
    const dailyLimit = String(accessStateValue.dailyLimit ?? 3);
    if (document.body) { document.body.dataset.tier = accessStateValue.tier; document.body.dataset.demandAllowed = demandAllowed; document.body.dataset.dailyLimit = dailyLimit; }
    if (appEl) { appEl.dataset.tier = accessStateValue.tier; appEl.dataset.demandAllowed = demandAllowed; appEl.dataset.dailyLimit = dailyLimit; }
    emitAccessState();
    return { ...accessStateValue };
  }
  function subscribeAccessState(fn) { accessListeners.add(fn); return () => accessListeners.delete(fn); }
  const AccessState = { get: () => ({ ...accessStateValue }), set: setAccessState, readDomAccess, subscribe: subscribeAccessState };
  // ── 2. Module-scope state, constants, utilities
  const REPORT_TYPE_VALUES = {
    active: "active",
    maybe: "maybe",
    ended: "ended",
    active_construction: "active",
    maybe_construction: "maybe",
    construction_ended: "ended",
    "Active Construction": "active",
    "Maybe Construction": "maybe",
    "Construction Ended": "ended",
  };
  const state = { coords: { lat: null, lng: null, valid: false, key: null },
    input: { kind: "empty", original: "", preview: "", error: "", touched: false },
    hero: { constructionStatus: "idle", demandStatus: "idle", constructionScore: null, demandScore: null, searchState: "idle", searchRequestInFlight: false, searchAttemptId: 0 },
    unlock: { plan: "sim_1_day", email: "", step: 1, uiSurface: null, cooldownUntil: 0, submitting: false, resendSubmitting: false },
    report: { type: "active", note: "", locationRaw: "", locationParsed: null, locationError: null, locationSource: "manual_input", locationSourceLocked: false, uiState: "idle", quotaBlocked: false, submitAttemptId: 0, autoCloseTimer: null },
    modals: { active: null } };
  const ARC_LENGTH = 377;
  const MIN_ANGLE = -82;
  const MAX_SWEEP = 164;
  const PIVOT_X = 160;
  const PIVOT_Y = 180;
  const DURATION = 800;
  const QUIET_CELEBRATION_DURATION = 3400;
  const QUIET_CELEBRATION_REDUCED_MOTION_DURATION = 1800;
  const QUIET_PLACE_MESSAGE = "You found yourself a quiet place. Congratulations.";
  const VERIFY_TIMEOUT_TEXT = "Verification unavailable. Please check your connection or disable ad blockers and try again.";
  const SEARCH_CHALLENGE_CODES = new Set(["CHALLENGE_REQUIRED", "INVALID_CHALLENGE", "TURNSTILE_REQUIRED", "TURNSTILE_INVALID"]);
  const TURNSTILE_VISIBLE_WAIT_MS = 2500;
  const TURNSTILE_ERROR_RETRY_MS = 650;
  const TURNSTILE_MAX_ERROR_RETRIES = 2;
  const HANDLED_SEARCH_ERROR_CODES = new Set([
    "FREE_DAILY_QUOTA_EXCEEDED",
    "IP_RATE_LIMIT_EXCEEDED",
    "SEARCH_IN_FLIGHT",
    "SEARCH_TEMPORARILY_THROTTLED",
    ...SEARCH_CHALLENGE_CODES
  ]);
  window.dilldrillTurnstileToken = window.dilldrillTurnstileToken || null;
  let searchRequestInFlight = false;
  let parseAbortController = null;
  let reportParseAbortController = null;
  let _checkoutOpSeq = 0;
  let _resendOpSeq = 0;
  let resendTimer = null;
  let quietCelebrationState = "idle";
  let quietCelebrationAnimation = null;
  let quietCelebrationTimer = null;
  let lastQuietCelebrationReportId = null;
  const shownQuietCelebrationReportIds = new Set();
  const $ = (id) => document.getElementById(id);
  function easeOutBack(t) {
    const c1 = 1.70158;
    const c3 = c1 + 1;
    return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2); }
  function scoreToGaugeAngle(score) {
    const value = Math.max(0, Math.min(100, Number(score) || 0));
    return MIN_ANGLE + (MAX_SWEEP * value) / 100; }
  function animateGauge(bandEl, needleEl, score) {
    if (!bandEl || !needleEl) return;
    const value = Math.max(0, Math.min(100, Number(score) || 0));
    const targetOffset = ARC_LENGTH - (ARC_LENGTH * value) / 100;
    const targetAngle = scoreToGaugeAngle(value);
    const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
    if (reduceMotion) { bandEl.style.strokeDashoffset = String(targetOffset);
      needleEl.setAttribute("transform", `rotate(${targetAngle} ${PIVOT_X} ${PIVOT_Y})`);
      return; }
    const startOffset = Number.parseFloat(bandEl.style.strokeDashoffset || ARC_LENGTH);
    const currentTransform = needleEl.getAttribute("transform") || `rotate(${MIN_ANGLE} ${PIVOT_X} ${PIVOT_Y})`;
    const startAngle = Number.parseFloat((currentTransform.match(/rotate\((-?\d+(?:\.\d+)?)/) || [])[1] || MIN_ANGLE);
    const startedAt = performance.now();
    function tick(now) {
      const progress = Math.min(1, (now - startedAt) / DURATION);
      const eased = easeOutBack(progress);
      const offset = startOffset + (targetOffset - startOffset) * eased;
      const angle = startAngle + (targetAngle - startAngle) * eased;
      bandEl.style.strokeDashoffset = String(offset);
      needleEl.setAttribute("transform", `rotate(${angle} ${PIVOT_X} ${PIVOT_Y})`);
      if (progress < 1) requestAnimationFrame(tick); }
    requestAnimationFrame(tick); }
  function setQuietCelebrationState(next) {
    quietCelebrationState = next === "running" ? "running" : "idle";
    const gauge = $("constructionGauge");
    if (gauge && quietCelebrationState === "running") gauge.dataset.celebration = "running";
    else if (gauge) gauge.removeAttribute("data-celebration"); }
  function cleanupQuietCelebrationVisuals() {
    const needle = $("constructionNeedle");
    if (needle) {
      needle.style.willChange = "";
      needle.style.transform = "";
      needle.style.transformOrigin = "";
      needle.style.transformBox = ""; }
    window.clearTimeout(quietCelebrationTimer);
    quietCelebrationTimer = null;
    setQuietCelebrationState("idle"); }
  function cancelQuietCelebration() {
    try { quietCelebrationAnimation?.cancel?.(); } catch (_) {}
    quietCelebrationAnimation = null;
    cleanupQuietCelebrationVisuals(); }
  function getQuietCelebrationReportId(result, score, attemptId) {
    return String(result?.id || result?.report_id || result?.completed_at || result?.completion_timestamp || `${attemptId || "report"}:${normalizeKey(result?.coord_key || state.coords.key)}:${score}:${result?.result_tier || result?.message_code || ""}`); }
  function isQuietPlaceCelebrationResult(result, score) {
    if (!result || !Number.isFinite(score) || score < 0 || score >= 10) return false;
    if (result.success === false || result.error || result.degraded || result.partial || result.cached) return false;
    if (result.result_tier) return result.result_tier === "quiet_place_found";
    if (score < 10) return true;
    return result.message === QUIET_PLACE_MESSAGE; }
  function captureQuietCelebrationShown() {
    captureEvent("quiet_place_celebration_shown", {
      surface: "construction_report",
      result_tier: "quiet_place_found",
      construction_score_bucket: "under_10"
    }); }
  function markQuietCelebrationShown(reportId) {
    shownQuietCelebrationReportIds.add(reportId);
    lastQuietCelebrationReportId = reportId;
    captureQuietCelebrationShown(); }
  function runQuietCelebration(finalAngleDeg, reportId) {
    if (!reportId || shownQuietCelebrationReportIds.has(reportId)) return;
    cancelQuietCelebration();
    const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
    if (reduceMotion) {
      setQuietCelebrationState("running");
      markQuietCelebrationShown(reportId);
      quietCelebrationTimer = window.setTimeout(cleanupQuietCelebrationVisuals, QUIET_CELEBRATION_REDUCED_MOTION_DURATION);
      return; }
    const needle = $("constructionNeedle");
    if (!needle) return;
    try {
      setQuietCelebrationState("running");
      markQuietCelebrationShown(reportId);
      needle.style.willChange = "transform";
      // Avoid needle.animate([ ... ]) here: CSS transforms on SVG <g> lose the rotate(cx cy) pivot.
      const startedAt = performance.now();
      let frameId = null;
      let cancelled = false;
      const keyframes = [
        { angle: finalAngleDeg, offset: 0 },
        { angle: finalAngleDeg + 20, offset: 0.25 },
        { angle: finalAngleDeg + 60, offset: 0.5 },
        { angle: finalAngleDeg - 40, offset: 0.75 },
        { angle: finalAngleDeg, offset: 1 }
      ];
      const setNeedleAngle = (angle) => needle.setAttribute("transform", `rotate(${angle} ${PIVOT_X} ${PIVOT_Y})`);
      const finish = () => {
        if (quietCelebrationAnimation?.cancel === cancel) quietCelebrationAnimation = null;
        setNeedleAngle(finalAngleDeg);
        cleanupQuietCelebrationVisuals(); };
      const cancel = () => {
        cancelled = true;
        if (frameId !== null) window.cancelAnimationFrame(frameId);
        if (quietCelebrationAnimation?.cancel === cancel) quietCelebrationAnimation = null;
        cleanupQuietCelebrationVisuals(); };
      const tick = (now) => {
        if (cancelled) return;
        const progress = Math.min(1, (now - startedAt) / QUIET_CELEBRATION_DURATION);
        let previous = keyframes[0];
        let next = keyframes[keyframes.length - 1];
        for (let i = 1; i < keyframes.length; i += 1) {
          if (progress <= keyframes[i].offset) {
            next = keyframes[i];
            previous = keyframes[i - 1];
            break; } }
        const span = Math.max(0.001, next.offset - previous.offset);
        const localProgress = Math.max(0, Math.min(1, (progress - previous.offset) / span));
        const eased = 0.5 - Math.cos(localProgress * Math.PI) / 2;
        setNeedleAngle(previous.angle + (next.angle - previous.angle) * eased);
        if (progress < 1) frameId = window.requestAnimationFrame(tick);
        else finish(); };
      quietCelebrationAnimation = { cancel };
      frameId = window.requestAnimationFrame(tick);
      quietCelebrationTimer = window.setTimeout(cancelQuietCelebration, 5000);
    } catch (_) { cleanupQuietCelebrationVisuals(); } }
  function maybeRunQuietCelebration(result, score, attemptId) {
    if (!isQuietPlaceCelebrationResult(result, score)) {
      cancelQuietCelebration();
      return; }
    const reportId = getQuietCelebrationReportId(result, score, attemptId);
    if (reportId === lastQuietCelebrationReportId || shownQuietCelebrationReportIds.has(reportId)) return;
    runQuietCelebration(scoreToGaugeAngle(score), reportId); }
  async function apiPost(url, body, options = {}) { let response;
    try { response = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...(options.headers || {}) },
        body: JSON.stringify(body || {}),
        signal: options.signal,
        keepalive: options.keepalive === true });
    } catch (err) { if (err?.name === "AbortError") throw err;
      const networkError = new Error("Network error. Please check your connection and try again.");
      networkError.cause = err;
      throw networkError; }
    let data = null;
    try { data = await response.json();
    } catch (err) { if (response.ok) return { ok: true, status: response.status, data: null, response }; }
    if (!response.ok) { const err = new Error(extractApiMessage(data) || "Something went wrong. Please try again.");
      err.status = response.status;
      err.data = data;
      err.errorCode = extractErrorCode(data);
      throw err; }
    return { ok: true, status: response.status, data, response }; }
  function notify(message, type = "info") {
    const text = String(message || "").trim();
    if (!text) return;
    const toast = document.createElement("div");
    toast.className = `toast toast-${type}`;
    toast.setAttribute("role", "status");
    toast.textContent = text;
    document.body.appendChild(toast);
    requestAnimationFrame(() => toast.classList.add("show"));
    window.setTimeout(() => { toast.classList.remove("show");
      window.setTimeout(() => toast.remove(), 250);
    }, 3200); }
  function debounce(fn, delay) {
    let timer = null;
    return (...args) => { window.clearTimeout(timer);
      timer = window.setTimeout(() => fn(...args), delay); }; }
  function logClientException(name, err, ctx = {}) {
    try {
      const error = err instanceof Error ? err : new Error(String(err || name));
      const payload = {
        client_event: name,
        message: error.message,
        stack: error.stack?.split("\n").slice(0, 6).join(" | ") || null,
        tier: document.body?.dataset.tier || "unknown",
        path: window.location.pathname,
        ...ctx
      };
      console.error(JSON.stringify({ level: "error", event: name, ...payload }));
      window.Sentry?.captureException?.(error, {
        tags: { client_event: name, tier: payload.tier },
        extra: payload
      });
      window.posthog?.capture?.("client_exception", payload);
      window.posthog?.flush?.();
    } catch (_) {
      try { console.error("[DillDrill] logClientException itself threw", name); } catch (__) {}
    }
  }
  function installGlobalErrorReporting() {
    if (window.__DD_GLOBAL_ERROR_REPORTING_INSTALLED) return;
    window.__DD_GLOBAL_ERROR_REPORTING_INSTALLED = true;
    window.addEventListener("error", (event) => {
      logClientException("frontend_unhandled_error", event.error || event.message, {
        filename: event.filename || null,
        lineno: event.lineno || null,
        colno: event.colno || null
      });
    });
    window.addEventListener("unhandledrejection", (event) => {
      logClientException("frontend_unhandled_rejection", event.reason || "Unhandled promise rejection");
    });
  }
  function safeLogJoinResearchException(eventName, err, extra = {}) {
    try { if (typeof window.logJoinResearchException === "function") {
        window.logJoinResearchException(eventName, err, extra);
        return; }
      logClientException(eventName, err, extra);
    } catch (logErr) { logClientException("join_research_exception_logger_failed", logErr, { eventName }); } }
  function captureEvent(name, props = {}) {
    try { const payload = { ...(props || {}) };
      const distinctId = window.posthog?.get_distinct_id?.();
      if (distinctId) payload.posthog_distinct_id = distinctId;
      window.posthog?.capture?.(name, payload);
      window.posthog?.flush?.();
    } catch (err) { logClientException("capture_event_failed", err, { name }); } }
  async function logFlowEvent(eventName, payload = {}) { try {
      await fetch("/api/telemetry/client-event", { method: "POST",
        headers: { "Content-Type": "application/json" },
        keepalive: true,
        body: JSON.stringify({ flow_type: "research_access",
          surface: "demand_level_page",
          modal_name: "join_research_access_modal",
          event: eventName,
          ...(payload || {})
        }) });
    } catch (err) { logClientException("client_flow_event_failed", err, { eventName }); } }
  function normalizeKey(k) {
    if (k === null || k === undefined || k === "") return null;
    const parts = String(k).split(",").map((part) => Number.parseFloat(part));
    if (parts.length < 2 || parts.some((n) => !Number.isFinite(n))) return null;
    return parts.slice(0, 2).map((n) => n.toFixed(4)).join(","); }
  function normalizeInput(raw) {
    return String(raw || "").replace(/[\u2018\u2019\u201C\u201D]/g, "").replace(/\u00A0/g, " ").trim(); }
  function validateLatLng(lat, lng) {
    const parsedLat = Number(lat);
    const parsedLng = Number(lng);
    if (!Number.isFinite(parsedLat) || !Number.isFinite(parsedLng)) throw new Error("Coordinates must be numbers.");
    if (parsedLat < -90 || parsedLat > 90 || parsedLng < -180 || parsedLng > 180) {
      throw new Error("Coordinates are out of range. Latitude must be between -90 and 90, and longitude between -180 and 180."); }
    return { lat: parsedLat, lng: parsedLng }; }
  function classifyLocationInput(raw) {
    const value = normalizeInput(raw);
    if (!value) return "empty";
    if (/^https?:\/\/(maps\.app\.goo\.gl|goo\.gl\/maps)\//i.test(value)) return "google_maps_short_url";
    if (/^https?:\/\/([^\s/]+\.)?(google\.[^\s/]+|googleusercontent\.com)\/maps/i.test(value)) return "google_maps_url";
    if (/^https?:\/\/maps\.google\.[^\s]+/i.test(value)) return "google_maps_url";
    if (/^[-+]?\d+(?:\.\d+)?\s*,\s*[-+]?\d+(?:\.\d+)?$/.test(value)) return "decimal_pair";
    return "invalid"; }
  function initFrontendSentry() {
    try { if (window.__DD_SENTRY_INIT_DONE) return;
      const dsn = document.body?.dataset.sentryDsn;
      if (!dsn || !window.Sentry?.init) {
        const reason = !dsn ? "missing_dsn" : "missing_sdk";
        const payload = { event: "frontend_sentry_missing", reason, path: window.location.pathname };
        console.warn(JSON.stringify(payload));
        window.posthog?.capture?.("frontend_sentry_missing", payload);
        window.posthog?.flush?.();
        return;
      }
      window.Sentry.init({ dsn, tracesSampleRate: 0.05, environment: document.body.dataset.env || "production" });
      window.__DD_SENTRY_INIT_DONE = true;
    } catch (err) { logClientException("sentry_init_failed", err); } }
  function getTierDisplayLabel(tier) {
    const normalized = String(tier || "free").toLowerCase();
    if (normalized === "simulated_paid") return "Joined";
    if (normalized === "pass_1_day") return "1 Day Pass";
    if (normalized === "pass_3_day") return "3 Day Pass";
    if (normalized === "free") return "Free";
    return normalized.charAt(0).toUpperCase() + normalized.slice(1).replaceAll("_", " "); }
  function setButtonLoading(btn, isLoading) {
    if (!btn) return;
    btn.disabled = Boolean(isLoading);
    btn.querySelector(".btn-text")?.classList.toggle("hidden", Boolean(isLoading));
    btn.querySelector(".btn-spinner")?.classList.toggle("hidden", !isLoading); }
  function extractErrorCode(data) {
    return data?.error || data?.detail?.error || data?.detail?.detail?.error || data?.detail?.detail?.error_code || data?.detail?.error_code || data?.error_code || data?.reason_code || data?.detail?.reason_code || null; }
  function extractApiMessage(data) {
    return data?.message || data?.detail?.message || data?.detail?.detail?.message || (typeof data?.detail === "string" ? data.detail : ""); }
  function parseLocationPayload(data, fallbackText) {
    const normalized = data?.normalized || data;
    const lat = Number(normalized?.latitude ?? normalized?.lat);
    const lng = Number(normalized?.longitude ?? normalized?.lng ?? normalized?.lon);
    const valid = validateLatLng(lat, lng);
    const display = normalized?.display || fallbackText || `${valid.lat.toFixed(6)}, ${valid.lng.toFixed(6)}`;
    return { lat: valid.lat, lng: valid.lng, display, inputKind: normalized?.input_kind || null, key: normalizeKey(`${valid.lat},${valid.lng}`) }; }
  function setText(id, text) {
    const el = $(id);
    if (el) el.textContent = text || ""; }
  function setHidden(id, hidden) {
    $(id)?.classList.toggle("hidden", Boolean(hidden)); }
  function resetHeroResultUi() {
    cancelQuietCelebration();
    state.hero.constructionScore = null;
    state.hero.demandScore = null;
    state.hero.constructionStatus = "idle";
    state.hero.demandStatus = "idle";
    setSearchState("idle");
    animateGauge($("constructionBand"), $("constructionNeedle"), 0);
    animateGauge($("demandBand"), $("demandNeedle"), 0);
    setText("constructionMessage", "");
    setText("demandMessage", ""); }
  function recordTurnstileLifecycle(eventName, level, props) {
    window.posthog?.capture?.(eventName, props);
    window.posthog?.flush?.();
    window.Sentry?.addBreadcrumb?.({ message: eventName, level });
  }
  function setHeroTurnstileToken(token) {
    window.dilldrillTurnstileToken = token || null;
    const tokenInput = $("heroTurnstileToken");
    if (tokenInput) tokenInput.value = token || "";
  }
  function getHeroTurnstileToken() {
    return window.dilldrillTurnstileToken || heroTurnstile.getToken();
  }
  function setHeroTurnstileVerified(verified) {
    const challenge = document.querySelector("[data-turnstile-container]");
    const badge = document.querySelector("[data-turnstile-verified]");
    if (challenge) {
      challenge.classList.toggle("sr-only", Boolean(verified));
      if (!verified) {
        challenge.classList.remove("hidden");
        challenge.removeAttribute("hidden");
      }
    }
    if (badge) badge.toggleAttribute("hidden", !verified);
  }
  function showHeroTurnstileChallenge() {
    setHeroTurnstileVerified(false);
  }
  function onTurnstileRendered() {
    recordTurnstileLifecycle("turnstile_started", "info");
  }
  function onTurnstileSuccess(token) {
    setHeroTurnstileToken(token);
    setHeroTurnstileVerified(true);
    recordTurnstileLifecycle("turnstile_success", "info");
    updateButtons();
  }
  function onTurnstileExpired() {
    setHeroTurnstileToken(null);
    showHeroTurnstileChallenge();
    recordTurnstileLifecycle("turnstile_expired", "warning");
    updateButtons();
  }
  function onTurnstileError(code) {
    setHeroTurnstileToken(null);
    showHeroTurnstileChallenge();
    recordTurnstileLifecycle("turnstile_error", "error", { code });
    window.Sentry?.captureMessage?.("Turnstile widget error", { level: "warning", extra: { code } });
    updateButtons();
  }
  window.onTurnstileRendered = onTurnstileRendered;
  window.onTurnstileSuccess = onTurnstileSuccess;
  window.onTurnstileExpired = onTurnstileExpired;
  window.onTurnstileError = onTurnstileError;
  // ── 3. TurnstileManager factory
  function TurnstileManager(containerId, opts = {}) {
    let widgetId;
    let token = null;
    let initPromise = null;
    let errorRetryCount = 0;
    let errorRetryTimer = null;
    function container() { return $(containerId); }
    function statusEl() { return opts.statusElId ? $(opts.statusElId) : null; }
    function setStatus(message) { const el = statusEl(); if (el) el.textContent = message || ""; }
    function getSitekey(el) { return el?.dataset.sitekey || document.body?.dataset.turnstileSitekey || ""; }
    function isVisibleForRender(el) {
      if (!el?.isConnected) return false;
      const slot = el.closest?.("[data-turnstile-container]") || el;
      if (slot.hasAttribute?.("hidden") || slot.classList?.contains("hidden")) return false;
      const style = window.getComputedStyle ? window.getComputedStyle(slot) : null;
      if (style && (style.display === "none" || style.visibility === "hidden")) return false;
      const rect = el.getBoundingClientRect?.();
      return Boolean((rect?.width || el.offsetWidth) > 0 && (rect?.height || el.offsetHeight || slot.offsetHeight) > 0);
    }
    function waitForVisible(el) {
      const startedAt = Date.now();
      return new Promise((resolve, reject) => {
        if (isVisibleForRender(el)) return resolve();
        let observer = null;
        let frameId = null;
        let timeoutId = null;
        const cleanup = () => {
          if (observer) observer.disconnect();
          if (frameId !== null) window.cancelAnimationFrame?.(frameId);
          if (timeoutId !== null) window.clearTimeout(timeoutId);
        };
        const check = () => {
          if (isVisibleForRender(el)) { cleanup(); resolve(); return; }
          if (Date.now() - startedAt >= TURNSTILE_VISIBLE_WAIT_MS) { cleanup(); reject(new Error("turnstile_container_not_visible")); return; }
          frameId = window.requestAnimationFrame ? window.requestAnimationFrame(check) : window.setTimeout(check, 50);
        };
        if (typeof ResizeObserver === "function") {
          observer = new ResizeObserver(check);
          observer.observe(el);
          const slot = el.closest?.("[data-turnstile-container]");
          if (slot && slot !== el) observer.observe(slot);
        }
        timeoutId = window.setTimeout(check, 50);
        check();
      });
    }
    function clearErrorRetry() { if (errorRetryTimer) window.clearTimeout(errorRetryTimer); errorRetryTimer = null; }
    function waitForTurnstile() {
      const startedAt = Date.now();
      let delay = 100;
      return new Promise((resolve, reject) => { function poll() {
          if (window.turnstile?.render && window.turnstile?.reset) return resolve(window.turnstile);
          if (Date.now() - startedAt >= 10000) return reject(new Error("turnstile_load_timeout"));
          window.setTimeout(poll, delay);
          delay = Math.min(delay * 2, 1600); }
        poll(); }); }
    async function runInit() {
      const el = container();
      if (!el) return null;
      const sitekey = getSitekey(el);
      console.info(JSON.stringify({
        level: "info",
        event: "turnstile_init_attempt",
        containerId,
        elementSitekey: el?.dataset?.sitekey,
        resolvedSitekey: sitekey,
        bodySitekey: document.body?.dataset?.turnstileSitekey,
        elementWidth: el?.offsetWidth,
        elementHeight: el?.offsetHeight,
      }));
      if (!sitekey) { setStatus(document.body?.dataset.labelVerificationUnavailableSitekeyMissing || "Verification unavailable: site key missing."); opts.onError?.(); return null; }
      try { const turnstile = await waitForTurnstile();
        if (widgetId !== undefined) { turnstile.reset(widgetId); token = null; return widgetId; }
        // Wait for a visible, laid-out container before rendering. Rendering into
        // a hidden or zero-size slot can leave the widget invisible on first load,
        // especially on mobile browsers that finish layout after deferred scripts.
        await waitForVisible(el);
        widgetId = turnstile.render(el, { sitekey, theme: el.dataset.theme || "auto", "render-callback": () => opts.onRender?.(), callback: (newToken) => { errorRetryCount = 0; clearErrorRetry(); token = newToken || null; setStatus(""); try { console.info(JSON.stringify({ level: "info", event: "turnstile_token_received", containerId, token_length: token?.length || 0 })); } catch (_) {} opts.onToken?.(token); }, "expired-callback": () => { token = null; opts.onExpire?.(); }, "error-callback": (code) => { token = null; opts.onError?.(code); if (errorRetryCount < TURNSTILE_MAX_ERROR_RETRIES) { errorRetryCount += 1; clearErrorRetry(); errorRetryTimer = window.setTimeout(() => { destroy(); init(); }, TURNSTILE_ERROR_RETRY_MS * errorRetryCount); } } });
        return widgetId;
      } catch (err) { setStatus(VERIFY_TIMEOUT_TEXT); opts.onError?.(err);
        logClientException("turnstile_init_failed", err, {
          containerId,
          tier: document.body?.dataset.tier || "unknown",
          timed_out: err?.message === "turnstile_load_timeout"
        });
        try {
          window.posthog?.capture?.("turnstile_init_failed", {
            containerId,
            timed_out: err?.message === "turnstile_load_timeout"
          });
          window.posthog?.flush?.();
        } catch (_) {}
        return null;
      } finally { initPromise = null; }
    }
    async function init() { if (initPromise) return initPromise; initPromise = runInit(); return initPromise; }
    function reset() {
      clearErrorRetry();
      errorRetryCount = 0;
      const hadWidget = widgetId !== undefined;
      try { if (hadWidget && window.turnstile?.reset) window.turnstile.reset(widgetId);
      } catch (err) { widgetId = undefined;
        logClientException("turnstile_reset_failed", err, { containerId });
      } finally { token = null;
        setStatus("");
        opts.onReset?.(); } }
    function destroy() {
      clearErrorRetry();
      try { if (widgetId !== undefined && window.turnstile?.remove) window.turnstile.remove(widgetId);
      } catch (err) { logClientException("turnstile_destroy_failed", err, { containerId }); }
      widgetId = undefined;
      token = null;
      initPromise = null;
      setStatus("");
      const el = container();
      if (el) {
        const staleIframe = el.querySelector("iframe");
        if (staleIframe) staleIframe.remove();
      }
      opts.onReset?.(); }
    return { init, reset, getToken: () => token || null, destroy }; }
  // ── 4. Turnstile instances
  const heroTurnstile = TurnstileManager("turnstileContainer", { onRender: () => onTurnstileRendered(),
    onToken: (token) => onTurnstileSuccess(token),
    onExpire: () => onTurnstileExpired(),
    onError: (code) => onTurnstileError(code),
    onReset: () => setHeroTurnstileToken(null) });
  const unlockTurnstile = TurnstileManager("unlock-turnstile-widget", { statusElId: "unlockTurnstileStatusMsg",
    onToken: () => syncResendButtonState(),
    onExpire: () => syncResendButtonState(),
    onError: () => syncResendButtonState() });
  const reportTurnstile = TurnstileManager("report-turnstile-widget", { onToken: () => setText("reportError", ""),
    onExpire: () => {},
    onError: () => setText("reportError", VERIFY_TIMEOUT_TEXT) });
  // ── 5. syncAccessUI + updateButtons
  function syncAccessUI() {
    const access = AccessState.get();
    const locked = !access.demandAllowed;
    const demandBtn = $("demandGoBtn");
    if (demandBtn) {
      demandBtn.textContent = locked ? (demandBtn.dataset.labelUnlock || document.body.dataset.labelUnlock || "Unlock") : (demandBtn.dataset.labelGo || document.body.dataset.labelGo || "GO");
      demandBtn.classList.toggle("unlock-styled", locked); }
    const supportBtn = $("supportBtn");
    if (supportBtn) {
      supportBtn.textContent = locked ? (supportBtn.dataset.labelUnlock || "Unlock") : (supportBtn.dataset.labelActive || document.body.dataset.labelActive || "Available");
      supportBtn.classList.toggle("accent", locked);
      supportBtn.classList.toggle("active", !locked);
      supportBtn.disabled = !locked;
      supportBtn.setAttribute("aria-disabled", locked ? "false" : "true"); }
    const tierLabel = getTierDisplayLabel(access.tier);
    setText("userMenuBtn", tierLabel);
    const tierBadge = $("userTierBadge");
    if (tierBadge) { tierBadge.textContent = `${tierBadge.dataset.labelTier || "TIER"}: ${tierLabel}`;
      tierBadge.dataset.tier = access.tier; }
    const demandStatus = $("userDemandStatus");
    if (demandStatus) {
      demandStatus.textContent = access.demandAllowed ? (demandStatus.dataset.labelUnlocked || document.body.dataset.labelDemandUnlocked || "Available ✓") : (demandStatus.dataset.labelLocked || document.body.dataset.labelDemandLocked || "Locked 🔒");
      demandStatus.classList.toggle("available", access.demandAllowed);
      demandStatus.classList.toggle("locked", !access.demandAllowed); }
    const limitItem = $("userDailyLimitItem");
    if (limitItem) limitItem.textContent = `${limitItem.dataset.labelDailyUsage || "Daily usage"}: ${access.dailyLimit ?? 3}`;
    updateButtons(); }
  function updateButtons() {
    const hasCoords = state.coords.valid;
    const access = AccessState.get();
    const tier = access.tier;
    // Only free-tier users must present a Turnstile token to search.
    // Paid / simulated_paid users are verified by session, not CAPTCHA.
    const turnstileRequiredForSearch = tier === "free";
    const hasTurnstileToken = window.dilldrillTurnstileToken !== null;
    const busy = searchRequestInFlight || state.hero.searchRequestInFlight || state.hero.constructionStatus === "loading" || state.hero.demandStatus === "loading";
    const mainBtn = $("mainActionBtn");
    const conBtn = $("constructionGoBtn");
    const demandBtn = $("demandGoBtn");
    const canSearch = hasCoords && (!turnstileRequiredForSearch || hasTurnstileToken) && !busy;
    // Emit telemetry for search button state changes
    captureEvent("search_button_state_changed", {
      enabled: canSearch,
      reason: canSearch
        ? "valid_location_parsed"
        : !hasCoords
          ? "no_coords"
          : turnstileRequiredForSearch
            ? "turnstile_missing"
            : "busy",
      lat: state.coords.lat || null,
      lon: state.coords.lng || null,
      tier,
      has_turnstile_token: hasTurnstileToken,
      turnstile_required_for_search: turnstileRequiredForSearch,
    });
    if (mainBtn) mainBtn.disabled = !canSearch;
    if (conBtn) conBtn.disabled = !canSearch;
    if (demandBtn) demandBtn.disabled = !canSearch; }
  // ── 6. Hero: location input, parse preview, fetchConstruction, fetchDemand
  function setHeroCoords(parsed) {
    state.coords = { lat: parsed.lat, lng: parsed.lng, valid: true, key: normalizeKey(parsed.key || `${parsed.lat},${parsed.lng}`) };
    state.input.preview = parsed.display || "";
    state.input.error = ""; }
  function clearHeroCoords() {
    state.coords = { lat: null, lng: null, valid: false, key: null };
    state.input.preview = "";
    updateButtons(); }
  function displayParseError(message) {
    setHidden("parsedPreview", true);
    setText("coordError", message);
    state.input.error = message || "";
    clearHeroCoords(); }
  async function parseHeroLocation() { const inputEl = $("locationInput");
    const raw = normalizeInput(inputEl?.value || "");
    state.input.original = raw;
    state.input.touched = true;
    state.input.kind = classifyLocationInput(raw);
    if (parseAbortController) parseAbortController.abort();
    if (state.input.kind === "empty") { setHidden("parsedPreview", true);
      setText("coordError", "");
      clearHeroCoords();
      return; }
    if (state.input.kind === "invalid") { displayParseError("Please use a Google Maps link or latitude/longitude coordinates.");
      return; }
    parseAbortController = new AbortController();
    setText("coordError", "");
    const preview = $("parsedPreview");
    if (preview) {
      preview.textContent = state.input.kind === "google_maps_short_url" ? (document.body.dataset.labelParsingLink || "Parsing link...") : "Parsing location...";
      preview.classList.remove("hidden"); }
    try { const { data } = await apiPost("/api/parse-location", { location_input: raw }, { signal: parseAbortController.signal });
      const parsed = parseLocationPayload(data, raw);
      setHeroCoords(parsed);
      if (preview) { preview.textContent = `${document.body.dataset.labelParsedAs || "Parsed as:"} ${parsed.display}`;
        preview.classList.remove("hidden"); }
    } catch (err) { if (err?.name === "AbortError") return;
      const code = err.errorCode;
      const fallback = code === "SHORT_URL_RESOLUTION_BLOCKED" ? document.body.dataset.labelErrorShortUrlBlocked : code === "LOCATION_NOT_SUPPORTED" ? document.body.dataset.labelErrorLocationNotSupported : null;
      if (err?.name !== "AbortError") {
        try {
          console.warn(JSON.stringify({
            level: "warn",
            event: "location_parse_failed",
            error_code: code || null,
            input_kind: state.input.kind,
            input_length: (raw || "").length
          }));
          window.posthog?.capture?.("location_parse_failed", {
            error_code: code || null,
            input_kind: state.input.kind
          });
          window.posthog?.flush?.();
        } catch (_) {}
      }
      displayParseError(fallback || err.message || "Could not read that location input.");
    } finally { parseAbortController = null;
      updateButtons(); } }
  const debouncedParseHeroLocation = debounce(parseHeroLocation, 350);

  function productSearchErrorMessage(code) {
    if (code === "FREE_DAILY_QUOTA_EXCEEDED") return "You've used today's free checks. Try again tomorrow or join research access.";
    if (code === "IP_RATE_LIMIT_EXCEEDED") return "Too many requests from your location. Please wait and try again.";
    if (code === "TURNSTILE_REQUIRED" || code === "TURNSTILE_INVALID") return "Verification failed. Please try again.";
    if (code === "SEARCH_IN_FLIGHT") return "A search for this location is already running. Please wait.";
    if (code === "SEARCH_TEMPORARILY_THROTTLED") return "Service temporarily busy. Please try again in a moment.";
    return "Something went wrong. Please try again."; }
  function handleStructuredSearchError(err, attemptId) {
    const code = err?.errorCode || null;
    if ((err?.status === 429 || err?.status === 503) && !code) {
      try { window.Sentry?.captureMessage?.("search_unstructured_backend_error", {
          level: "warning",
          extra: { http_status: err.status, coord_key: state.coords.key, attempt_id: attemptId }
        });
      } catch (_) {}
    }
    if (code === "TURNSTILE_REQUIRED" || code === "TURNSTILE_INVALID" || SEARCH_CHALLENGE_CODES.has(code)) {
      heroTurnstile.reset();
      showHeroTurnstileChallenge();
      heroTurnstile.init();
    }
    const props = { tier: AccessState.get().tier, coord_key: state.coords.key, attempt_id: attemptId };
    if (code === "FREE_DAILY_QUOTA_EXCEEDED") { captureEvent("search_quota_exceeded", props); openJoinResearchModal("quota_exceeded_search"); }
    if (code === "IP_RATE_LIMIT_EXCEEDED") captureEvent("search_rate_limited", props);
    if (code === "SEARCH_TEMPORARILY_THROTTLED") captureEvent("search_throttled", props);
    return { message: productSearchErrorMessage(code), handled: HANDLED_SEARCH_ERROR_CODES.has(code) }; }
  function searchPayload(target) {
    return { lat: state.coords.lat,
      lon: state.coords.lng,
      target,
      turnstile_token: heroTurnstile.getToken(),
      coord_key: state.coords.key,
      location_input: $("locationInput")?.value || "" }; }
  function setSearchState(next) {
    state.hero.searchState = next;
    return next; }
  function isHeroTurnstileRequired() {
    return AccessState.get().tier === "free"; }
  function searchTelemetryProps(extra = {}) {
    const token = getHeroTurnstileToken();
    return {
      source: "hero_search_form",
      tier: AccessState.get().tier,
      has_sentry: Boolean(window.__DD_SENTRY_INIT_DONE),
      has_posthog: Boolean(window.posthog),
      has_turnstile_sitekey: Boolean(document.body?.dataset.turnstileSitekey),
      input_type: state.input.kind,
      parsed_lat: state.coords.lat,
      parsed_lon: state.coords.lng,
      has_turnstile_token: Boolean(token),
      route: "/api/search",
      search_state: state.hero.searchState,
      ...extra
    }; }
  function logSearchEvent(name, props = {}) {
    const payload = searchTelemetryProps(props);
    try { console.info(JSON.stringify({ level: "info", event: name, ...payload })); } catch (_) {}
    captureEvent(name, payload);
    return payload; }
  function blockSearchSubmit(blockReason, message, props = {}) {
    setSearchState("blocked");
    if (blockReason !== "request_in_flight") state.hero.constructionStatus = "error";
    if (message) setText("constructionMessage", message);
    logSearchEvent("search_submit_blocked", { block_reason: blockReason, ...props });
    return null; }
  function resultIsCurrent(result) {
    const responseKey = normalizeKey(result?.coord_key);
    const currentKey = normalizeKey(state.coords.key);
    return !responseKey || !currentKey || responseKey === currentKey; }
  async function fetchConstruction(options = {}) {
    logSearchEvent("search_submit_clicked", { trigger: options.trigger || "unknown" });
    if (searchRequestInFlight || state.hero.searchRequestInFlight) return blockSearchSubmit("request_in_flight", "A search is already running. Please wait.");
    if (!state.coords.valid) return blockSearchSubmit("invalid_location", state.input.error || "Please enter a valid location before searching.");
    const turnstileToken = getHeroTurnstileToken();
    const turnstileRequiredForSearch = isHeroTurnstileRequired();
    if (turnstileRequiredForSearch && !turnstileToken) {
      showHeroTurnstileChallenge();
      await heroTurnstile.init();
      return blockSearchSubmit("turnstile_token_missing", document.body.dataset.labelSecurityCheckRequired || "Please complete the security check.");
    }
    const attemptId = ++state.hero.searchAttemptId;
    setSearchState(turnstileToken ? "turnstile_verified" : "ready_without_turnstile");
    logSearchEvent("search_validation_passed", { attempt_id: attemptId });
    captureEvent("search_initiated", {
      trigger: options.trigger || "unknown",
      target: "construction",
      search_state: state.hero.searchState,
      has_valid_coords: state.coords.valid,
      lat: Number.isFinite(state.coords.lat) ? Math.round(state.coords.lat * 1e5) / 1e5 : null,
      lng: Number.isFinite(state.coords.lng) ? Math.round(state.coords.lng * 1e5) / 1e5 : null
    });
    cancelQuietCelebration();
    searchRequestInFlight = true;
    state.hero.searchRequestInFlight = true;
    state.hero.constructionStatus = "loading";
    animateGauge($("constructionBand"), $("constructionNeedle"), 0);
    setSearchState("request_started");
    updateButtons();
    setText("constructionMessage", document.body.dataset.labelAnalyzingSignals || "Analyzing signals...");
    try {
      logSearchEvent("search_request_started", { attempt_id: attemptId });
      const payload = searchPayload("construction");
      if (turnstileRequiredForSearch && !payload.turnstile_token) {
        console.warn(JSON.stringify({ level: "warning", event: "search_payload_missing_token", attempt_id: attemptId, search_state: state.hero.searchState }));
        heroTurnstile.reset();
        setSearchState("idle");
        state.hero.constructionStatus = "error";
        setText("constructionMessage", "Verification failed. Please try again.");
        return null;
      }
      const { data } = await apiPost("/api/search", payload);
      if (attemptId !== state.hero.searchAttemptId) return null;
      if (data?.verification_required) { showHeroTurnstileChallenge();
        heroTurnstile.reset();
        await heroTurnstile.init();
        state.hero.constructionStatus = "idle";
        setSearchState("turnstile_required");
        setText("constructionMessage", document.body.dataset.labelVerificationRequired || "Verification required");
        logSearchEvent("search_submit_blocked", { attempt_id: attemptId, block_reason: "verification_required" });
        return null; }
      const result = data?.construction;
      if (!result) {
        logSearchEvent("search_ui_render_blocked_no_backend_response", { attempt_id: attemptId, block_reason: "missing_construction_result" });
        state.hero.constructionStatus = "error";
        setSearchState("response_missing_result");
        setText("constructionMessage", "Search completed, but no construction result was returned. Please try again.");
        return null;
      }
      if (!resultIsCurrent(result)) {
        logSearchEvent("search_ui_render_blocked_no_backend_response", { attempt_id: attemptId, block_reason: "stale_response" });
        state.hero.constructionStatus = "idle";
        setSearchState("stale_response");
        return null;
      }
      setSearchState("request_success");
      logSearchEvent("search_request_succeeded", { attempt_id: attemptId, http_status: 200 });
      const score = Number(result.score);
      if (Number.isFinite(score)) { const restored = Number(options.restoredScore);
        const shouldAnimate = !Number.isFinite(restored) || Math.abs(score - restored) > 2;
        if (shouldAnimate) animateGauge($("constructionBand"), $("constructionNeedle"), score);
        state.hero.constructionScore = score;
        if (!Number.isFinite(restored)) maybeRunQuietCelebration(result, score, attemptId); } else { cancelQuietCelebration(); }
      state.hero.constructionStatus = "ready";
      setSearchState("render_result");
      setText("constructionMessage", result.message || document.body.dataset.labelReady || "Ready");
      return result;
    } catch (err) { const searchError = handleStructuredSearchError(err, attemptId);
      state.hero.constructionStatus = "error";
      setSearchState("request_failed");
      setText("constructionMessage", searchError.message);
      logSearchEvent("search_request_failed", {
        attempt_id: attemptId,
        error_code: err.errorCode || null,
        http_status: err.status || null
      });
      if (!searchError.handled) { logClientException("construction_search_failed", err, {
          error_code: err.errorCode || null,
          http_status: err.status || null,
          coord_key: state.coords.key,
          tier: AccessState.get().tier
        }); }
      return null;
    } finally {
      if (attemptId === state.hero.searchAttemptId) { state.hero.searchRequestInFlight = false; searchRequestInFlight = false; }
      if (state.hero.constructionStatus === "loading") state.hero.constructionStatus = "idle";
      updateButtons();
    } }
  async function fetchDemand() { const access = AccessState.get();
    cancelQuietCelebration();
    if (searchRequestInFlight || state.hero.searchRequestInFlight) return null;
    if (!access.demandAllowed || access.tier === "free") { openJoinResearchModal("demand_level_page");
      return null; }
    if (!state.coords.valid) return null;
    const demandAttemptId = ++state.hero.searchAttemptId;
    searchRequestInFlight = true;
    state.hero.searchRequestInFlight = true;
    state.hero.demandStatus = "loading";
    updateButtons();
    setText("demandMessage", document.body.dataset.labelCheckingDemand || "Checking demand...");
    try { const token = getHeroTurnstileToken();
      const turnstileRequiredForSearch = isHeroTurnstileRequired();
      setSearchState(token ? "turnstile_verified" : "ready_without_turnstile");
      if (turnstileRequiredForSearch && !token) {
        console.warn(JSON.stringify({ level: "warning", event: "search_dispatch_blocked_missing_token", attempt_id: demandAttemptId, search_state: state.hero.searchState }));
        setSearchState("idle");
        heroTurnstile.reset();
        await heroTurnstile.init();
        setText("demandMessage", "Verification failed. Please try again.");
        return null;
      }
      const demandPayload = searchPayload("demand");
      if (turnstileRequiredForSearch && !demandPayload.turnstile_token) {
        console.warn(JSON.stringify({ level: "warning", event: "search_payload_missing_token", attempt_id: demandAttemptId, search_state: state.hero.searchState }));
        heroTurnstile.reset();
        setSearchState("idle");
        setText("demandMessage", "Verification failed. Please try again.");
        return null;
      }
      const { data } = await apiPost("/api/search", demandPayload);
      if (data?.verification_required) { showHeroTurnstileChallenge();
        await heroTurnstile.init();
        setText("demandMessage", document.body.dataset.labelVerificationRequired || "Verification required");
        state.hero.demandStatus = "idle";
        return null; }
      const result = data?.demand;
      if (!result || !resultIsCurrent(result)) return null;
      const score = Number(result.score);
      if (Number.isFinite(score)) { animateGauge($("demandBand"), $("demandNeedle"), score);
        state.hero.demandScore = score; }
      state.hero.demandStatus = "ready";
      setText("demandMessage", result.message || document.body.dataset.labelReady || "Ready");
      return result;
    } catch (err) { const searchError = handleStructuredSearchError(err, demandAttemptId);
      state.hero.demandStatus = "error";
      setText("demandMessage", searchError.message);
      if (!searchError.handled) { logClientException("demand_search_failed", err, {
          error_code: err.errorCode || null,
          http_status: err.status || null,
          coord_key: state.coords.key,
          tier: AccessState.get().tier
        }); }
      try {
        window.posthog?.capture?.("demand_search_failed", {
          error_code: err.errorCode || null,
          http_status: err.status || null,
          tier: AccessState.get().tier
        });
        window.posthog?.flush?.();
      } catch (_) {}
      return null;
    } finally { if (demandAttemptId === state.hero.searchAttemptId) { state.hero.searchRequestInFlight = false; searchRequestInFlight = false; }
      if (state.hero.demandStatus === "loading") state.hero.demandStatus = "idle";
      updateButtons(); } }
  // ── 7. Modal system: openModal, closeModal, hooks map
  const hooks = { supportModalLayer: {
      onOpen: () => unlockTurnstile.init(),
      onClose: () => { logFlowEvent("join_research_access_modal_closed", { action: "close_join_research_modal", status: "closed", ui_surface: state.unlock.uiSurface, step: `purchaseStep${state.unlock.step}` }); unlockTurnstile.destroy(); resetSupportModal(); } },
    reportModalLayer: { onOpen: () => reportTurnstile.init(),
      onClose: () => { reportTurnstile.destroy(); resetReportModal(); } } };
  function shouldUseDialogOpen(el) {
    return el?.tagName === "DIALOG" && !el.classList.contains("bottom-sheet") && !el.classList.contains("sheet-layer"); }
  function openModal(id, options = {}) {
    cancelQuietCelebration();
    const el = $(id);
    if (!el) return;
    if (state.modals.active && state.modals.active !== id) closeModal(state.modals.active, { silent: true });
    try { if (shouldUseDialogOpen(el)) {
        if (!el.open) el.showModal();
      } else { el.classList.add("open");
        if (el.tagName === "DIALOG" && !el.open && el.classList.contains("bottom-sheet")) el.show?.(); }
      el.setAttribute("aria-hidden", "false");
      document.body.style.overflow = "hidden";
      state.modals.active = id;
      hooks[id]?.onOpen?.(options);
      el.dispatchEvent(new CustomEvent("modal:open", { detail: options }));
    } catch (err) { logClientException("modal_open_failed", err, { id }); } }
  function closeModal(id, options = {}) {
    cancelQuietCelebration();
    const el = $(id);
    if (!el) return;
    try { if (shouldUseDialogOpen(el)) {
        if (el.open) el.close();
      } else { el.classList.remove("open");
        if (el.tagName === "DIALOG" && el.open && el.classList.contains("bottom-sheet")) el.close?.(); }
      el.setAttribute("aria-hidden", "true");
      if (state.modals.active === id) state.modals.active = null;
      if (!state.modals.active) document.body.style.overflow = "";
      hooks[id]?.onClose?.(options);
      if (!options.silent) el.dispatchEvent(new CustomEvent("modal:close", { detail: options }));
    } catch (err) { logClientException("modal_close_failed", err, { id }); } }
  // ── 8. Modal: Support / Join Research
  function openJoinResearchModal(surface = "hero_unlock_button") {
    state.unlock.uiSurface = surface;
    try { resetSupportModal({ keepSurface: true }); } catch (err) { safeLogJoinResearchException("join_research_modal_reset_failed", err, { surface }); }
    openModal("supportModalLayer", { surface });
    logFlowEvent("join_research_access_modal_opened", { action: "open_join_research_modal", status: "opened", ui_surface: surface, step: "purchaseStep1" }); }
  function showSupportStep(step) {
    state.unlock.step = step;
    [1, 2, 3].forEach((n) => setHidden(`purchaseStep${n}`, n !== step));
    syncResendButtonState(); }
  function resetSupportModal(options = {}) {
    state.unlock.plan = "sim_1_day";
    state.unlock.email = "";
    state.unlock.step = 1;
    state.unlock.submitting = false;
    state.unlock.resendSubmitting = false;
    if (!options.keepSurface) state.unlock.uiSurface = null;
    window.clearInterval(resendTimer);
    resendTimer = null;
    state.unlock.cooldownUntil = 0;
    _checkoutOpSeq += 1;
    _resendOpSeq += 1;
    showSupportStep(1);
    const emailEl = $("purchaseEmail");
    if (emailEl) emailEl.value = "";
    setText("purchaseEmailError", "");
    setText("purchaseRedirectError", "");
    setText("resendMessage", "");
    const proceedText = $("proceedToPaymentBtn")?.querySelector(".btn-text");
    if (proceedText) proceedText.textContent = document.body.dataset.labelJoinResearchCta || "Join Research ➔";
    setButtonLoading($("proceedToPaymentBtn"), false);
    $("planGrid")?.querySelectorAll("[data-plan]").forEach((card) => { const active = card.dataset.plan === "sim_1_day";
      card.classList.toggle("active", active);
      card.setAttribute("aria-checked", active ? "true" : "false"); });
    syncResendButtonState(); }
  function isCurrentOperation(type, id) {
    return type === "checkout" ? id === _checkoutOpSeq : id === _resendOpSeq; }
  function validEmail(email) {
    return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(email || "").trim()); }
  function syncResendButtonState() {
    const btn = $("resendLinkBtn");
    if (!btn) return;
    const email = normalizeInput($("purchaseEmail")?.value || state.unlock.email);
    const cooldownMs = Math.max(0, state.unlock.cooldownUntil - Date.now());
    const cooldownActive = cooldownMs > 0;
    btn.disabled = !email || !unlockTurnstile.getToken() || cooldownActive || state.unlock.resendSubmitting;
    if (cooldownActive) btn.textContent = `Resend Access Link (${Math.ceil(cooldownMs / 1000)}s)`;
    else btn.textContent = document.body.dataset.labelResendLink || "Resend Access Link";
    const hint = $("resendTurnstileHint");
    if (hint) {
      const hasToken = Boolean(unlockTurnstile.getToken());
      hint.textContent = hasToken
        ? "Security check complete. You can resend now."
        : "Complete a fresh security check to enable resend.";
    } }
  function startResendCooldown() {
    state.unlock.cooldownUntil = Date.now() + 180000;
    window.clearInterval(resendTimer);
    resendTimer = window.setInterval(() => { syncResendButtonState();
      if (Date.now() >= state.unlock.cooldownUntil) { window.clearInterval(resendTimer);
        resendTimer = null; }
    }, 1000);
    syncResendButtonState(); }
  function saveResumeStateBeforeMagicLink() {
    try { if (!state.coords.valid) return;
      localStorage.setItem("dd_resume_state", JSON.stringify({ lat: state.coords.lat,
        lng: state.coords.lng,
        text: $("locationInput")?.value || "",
        constructionScore: state.hero.constructionScore,
        constructionMessage: $("constructionMessage")?.textContent || ""
      }));
    } catch (err) { logClientException("resume_state_save_failed", err); } }
  async function submitMagicLink(event) { event?.preventDefault?.();
    const btn = $("proceedToPaymentBtn");
    const email = normalizeInput($("purchaseEmail")?.value || "");
    const token = unlockTurnstile.getToken();
    setText("purchaseEmailError", "");
    if (!validEmail(email)) { setText("purchaseEmailError", document.body.dataset.labelEmailInvalid || "Please enter a valid email address."); return; }
    if (!token) { setText("purchaseEmailError", document.body.dataset.labelSecurityCheckRequired || "Please complete the security check."); return; }
    const opId = ++_checkoutOpSeq;
    state.unlock.email = email;
    state.unlock.submitting = true;
    setButtonLoading(btn, true);
    logFlowEvent("join_research_access_email_submit_started", { action: "submit_email", status: "started", ui_surface: state.unlock.uiSurface, step: "purchaseStep1" });
    saveResumeStateBeforeMagicLink();
    try { const plan = 'sim_1_day';
      // apiPost wraps successful responses as { ok, status, data, response }.
      const { data } = await apiPost("/api/billing/unlock-intent", {
            email,
            plan,
            turnstile_token: token,
            ui_surface: state.unlock.uiSurface || "hero_unlock_button"
          });
      if (!isCurrentOperation("checkout", opId)) return;
      if (data?.intent_id) {
        sessionStorage.setItem("last_payment_intent_id", data.intent_id);
      }
      // UnlockIntentResponse.ok is serialized by app/schemas/billing.py.
      if (data?.ok !== true || data?.status !== "magic_link_sent") {
        const deliveryError = new Error(data?.message || "Magic link delivery failed.");
        deliveryError.errorCode = data?.error || data?.status || "MAGIC_LINK_DELIVERY_FAILED";
        deliveryError.status = 502;
        throw deliveryError;
      }
      logFlowEvent("join_research_access_magic_link_succeeded", { action: "request_magic_link", status: "succeeded", ui_surface: state.unlock.uiSurface, step: "purchaseStep3" });
      setText("resendMessage", `We've sent a secure access link to ${email}. Click it to activate your pass.`);
      showSupportStep(3);
    } catch (err) { if (!isCurrentOperation("checkout", opId)) return;
      unlockTurnstile.reset();
      await unlockTurnstile.init();
      setText("purchaseEmailError", "Join Research is temporarily unavailable. Please try again.");
      logFlowEvent("join_research_access_magic_link_failed", { action: "request_magic_link", status: "failed", error_code: err.errorCode, error_message: err.message, ui_surface: state.unlock.uiSurface, step: "purchaseStep1" });
      console.error(JSON.stringify({
        level: "error",
        event: "join_research_magic_link_failed",
        error_code: err.errorCode || null,
        http_status: err.status || null,
        ui_surface: state.unlock.uiSurface
      }));
      safeLogJoinResearchException("join_research_access_flow_failed", err, {
        email,
        error_code: err.errorCode || null,
        http_status: err.status || null,
        ui_surface: state.unlock.uiSurface
      });
      try {
        window.posthog?.capture?.("join_research_magic_link_failed", {
          error_code: err.errorCode || null,
          http_status: err.status || null,
          ui_surface: state.unlock.uiSurface
        });
        window.posthog?.flush?.();
      } catch (_) {}
    } finally { if (isCurrentOperation("checkout", opId)) {
        state.unlock.submitting = false;
        setButtonLoading(btn, false);
        syncResendButtonState(); } } }
  async function resendMagicLink(event) { event?.preventDefault?.();
    const email = normalizeInput($("purchaseEmail")?.value || state.unlock.email);
    const token = unlockTurnstile.getToken();
    if (!validEmail(email)) { setText("purchaseEmailError", document.body.dataset.labelResendEnterEmail || "Enter your email above, then click Resend."); return; }
    if (!token) { setText("purchaseEmailError", document.body.dataset.labelResendFreshSecurity || "Please complete a fresh security check before resend."); return; }
    const opId = ++_resendOpSeq;
    state.unlock.resendSubmitting = true;
    syncResendButtonState();
    try { await apiPost("/api/auth/magic-link", { email, turnstile_token: token });
      if (!isCurrentOperation("resend", opId)) return;
      state.unlock.email = email;
      startResendCooldown();
      showSupportStep(3);
      setText("resendMessage", `We've sent a fresh access link to ${email}.`);
      logFlowEvent("join_research_access_resend_succeeded", { action: "resend_magic_link", status: "succeeded", ui_surface: state.unlock.uiSurface, step: "purchaseStep3" });
    } catch (err) { if (!isCurrentOperation("resend", opId)) return;
      unlockTurnstile.reset();
      await unlockTurnstile.init();
      setText("purchaseEmailError", "Could not resend right now. Please try again.");
      logFlowEvent("join_research_access_resend_failed", { action: "resend_magic_link", status: "failed", error_code: err.errorCode, error_message: err.message, ui_surface: state.unlock.uiSurface, step: "purchaseStep1" });
      console.error(JSON.stringify({
        level: "error",
        event: "join_research_resend_failed",
        error_code: err.errorCode || null,
        http_status: err.status || null,
        ui_surface: state.unlock.uiSurface
      }));
      logClientException("join_research_resend_failed", err, {
        error_code: err.errorCode || null,
        ui_surface: state.unlock.uiSurface
      });
    } finally { if (isCurrentOperation("resend", opId)) {
        state.unlock.resendSubmitting = false;
        syncResendButtonState(); } } }
  // ── 9. Modal: Report
  function prefillReportLocation() {
    const input = $("reportLocationInput");
    if (!input || state.report.locationSourceLocked) return;
    const heroText = normalizeInput($("locationInput")?.value || "");
    if (heroText && state.coords.valid) { input.value = heroText;
      state.report.locationRaw = heroText;
      state.report.locationParsed = { lat: state.coords.lat, lng: state.coords.lng };
      state.report.locationSource = "hero_prefill"; } }
  function resetReportModal() {
    window.clearTimeout(state.report.autoCloseTimer);
    state.report = { type: "active", note: "", locationRaw: "", locationParsed: null, locationError: null, locationSource: "manual_input", locationSourceLocked: false, uiState: "idle", quotaBlocked: false, submitAttemptId: state.report.submitAttemptId + 1, autoCloseTimer: null };
    setHidden("reportFormState", false);
    setHidden("reportSuccessState", true);
    setText("reportError", "");
    setText("reportLocationError", "");
    const loc = $("reportLocationInput"); if (loc) loc.value = "";
    const note = $("reportNote"); if (note) note.value = "";
    const nearby = $("reportNearbyNow"); if (nearby) nearby.checked = false;
    setText("reportCharCount", "0/180");
    $("reportTypeGrid")?.querySelectorAll("[data-report-type]").forEach((pill) => { const active = REPORT_TYPE_VALUES[pill.dataset.reportType] === "active";
      pill.classList.toggle("active", active);
      pill.setAttribute("aria-checked", active ? "true" : "false"); });
    setButtonLoading($("reportSubmitBtn"), false);
    prefillReportLocation(); }
  async function parseReportLocation() { const raw = normalizeInput($("reportLocationInput")?.value || "");
    state.report.locationRaw = raw;
    state.report.locationSourceLocked = true;
    state.report.locationSource = "manual_input";
    if (reportParseAbortController) reportParseAbortController.abort();
    if (!raw) { state.report.locationParsed = null; setText("reportLocationError", ""); return; }
    reportParseAbortController = new AbortController();
    try { const { data } = await apiPost("/api/parse-location", { location_input: raw }, { signal: reportParseAbortController.signal });
      const parsed = parseLocationPayload(data, raw);
      state.report.locationParsed = { lat: parsed.lat, lng: parsed.lng };
      state.report.locationError = null;
      setText("reportLocationError", "");
    } catch (err) { if (err?.name === "AbortError") return;
      state.report.locationParsed = null;
      state.report.locationError = err.message;
      setText("reportLocationError", err.message || "Could not read that location.");
    } finally { reportParseAbortController = null; } }
  const debouncedParseReportLocation = debounce(parseReportLocation, 350);
  async function submitReport(event) { event?.preventDefault?.();
    const note = normalizeInput($("reportNote")?.value || "");
    const parsed = state.report.locationParsed || (state.coords.valid ? { lat: state.coords.lat, lng: state.coords.lng } : null);
    setText("reportError", "");
    if (!parsed) { setText("reportError", document.body.dataset.labelCoordinatesNotSet || "Coordinates not set"); return; }
    if (note.length > 0 && note.length < 10) { setText("reportError", document.body.dataset.labelReportMinChars || "Report must be at least 10 characters."); return; }
    const token = reportTurnstile.getToken();
    if (!token) { setText("reportError", document.body.dataset.labelSecurityCheckRequired || "Please complete the security check."); return; }
    const attemptId = ++state.report.submitAttemptId;
    setButtonLoading($("reportSubmitBtn"), true);
    try { const { data } = await apiPost("/api/user-reports", {
        lat: parsed.lat,
        lon: parsed.lng,
        report_type: state.report.type,
        note,
        is_nearby_now: Boolean($("reportNearbyNow")?.checked),
        location_source: state.report.locationSource,
        cf_turnstile_token: token });
      if (attemptId !== state.report.submitAttemptId) return;
      setHidden("reportFormState", true);
      setHidden("reportSuccessState", false);
      const successMsg = $("reportSuccessState")?.querySelector(".success-msg");
      if (successMsg && data?.message) successMsg.textContent = data.message;
      const reportStatus = data?.status || "report_created";
      console.info(JSON.stringify({
        level: "info",
        event: "user_report_submitted",
        status: reportStatus,
        report_type: state.report.type,
        is_duplicate: reportStatus === "duplicate_report",
        location_source: state.report.locationSource,
        tier: AccessState.get().tier
      }));
      captureEvent("user_report_submitted", {
        status: reportStatus,
        report_type: state.report.type,
        is_duplicate: reportStatus === "duplicate_report",
        location_source: state.report.locationSource,
        tier: AccessState.get().tier
      });
      try {
        window.posthog?.capture?.("user_report_submitted", {
          status: reportStatus,
          report_type: state.report.type,
          is_duplicate: reportStatus === "duplicate_report",
          location_source: state.report.locationSource,
          tier: AccessState.get().tier
        });
        window.posthog?.capture?.("report_sent", {
          $current_url: window.location.href
        });
        window.posthog?.flush?.();
      } catch (_) {}
      state.report.autoCloseTimer = window.setTimeout(() => closeModal("reportModalLayer"), 2000);
    } catch (err) { const code = err.errorCode;
      const normalizedCode = String(code || "").toLowerCase();
      if (normalizedCode === "turnstile_failed" || normalizedCode === "turnstile_required" || err.status === 403) { reportTurnstile.destroy(); await reportTurnstile.init(); }
      setText("reportError", err.message || "Could not submit report right now.");
      const isExpected429 = err.status === 429 &&
        (code === "daily_report_quota_exceeded" || code === "user_report_rate_limited");
      console.error(JSON.stringify({
        level: "error",
        event: "report_submit_failed",
        error_code: code || null,
        http_status: err.status || null,
        is_expected_429: isExpected429,
        tier: AccessState.get().tier
      }));
      captureEvent("user_report_failed", {
        error_code: code || null,
        http_status: err.status || null,
        is_expected_429: isExpected429,
        tier: AccessState.get().tier
      });
      try {
        window.posthog?.capture?.("user_report_failed", {
          error_code: code || null,
          http_status: err.status || null,
          is_expected_429: isExpected429,
          tier: AccessState.get().tier
        });
        window.posthog?.flush?.();
      } catch (_) {}
      if (!isExpected429) {
        logClientException("report_submit_failed", err, {
          error_code: code || null,
          http_status: err.status || null
        });
      }
    } finally { if (attemptId === state.report.submitAttemptId) setButtonLoading($("reportSubmitBtn"), false); } }
  // ── 10. Modal: Share
  function getShareUrl() {
    return $("shareUrlBox")?.textContent?.trim() || window.location.href.split("?")[0]; }
  function defaultShareText() {
    const senderName = document.body.dataset.userDisplayName || "Someone";
    return `${senderName} checked a property with DillDrill — and thinks you should too before you commit. See construction activity near any address in seconds:`; }
  function updateShareCounter() {
    const text = $("shareText")?.value || "";
    setText("shareCharCount", `${text.length} / 220`); }
  async function copyToClipboard(text) { try {
      await navigator.clipboard.writeText(text);
      notify("Copied.", "success");
      captureEvent("share_copied", { surface: state.modals.active });
      return true;
    } catch (err) { const msg = document.body.dataset.labelCopyFailedManual || "Could not copy. Please copy manually.";
      setText("shareError", msg);
      logClientException("clipboard_copy_failed", err);
      return false; } }
  async function copyAll() { const text = normalizeInput($("shareText")?.value || defaultShareText());
    return copyToClipboard(`${text} ${getShareUrl()}`.trim()); }
  async function shareNative() { const payload = { title: "🏗️ Check what's being built near this address", text: defaultShareText(), url: getShareUrl() };
    try { if (navigator.share) await navigator.share(payload);
      else await copyAll();
      closeModal("shareModalLayer");
      notify("Shared.", "success");
      captureEvent("native_share_completed", { method: navigator.share ? "native" : "clipboard" });
    } catch (err) { if (err?.name === "AbortError") return;
      setText("shareError", document.body.dataset.labelShareOpenFailed || "Could not open sharing options.");
      logClientException("share_native_failed", err); } }
  function openShareModal() {
    const textEl = $("shareText");
    if (textEl && !textEl.value) textEl.value = defaultShareText().slice(0, 220);
    const urlEl = $("shareUrlBox");
    if (urlEl && !urlEl.textContent.trim()) urlEl.textContent = window.location.href.split("?")[0];
    setText("shareError", "");
    updateShareCounter();
    openModal("shareModalLayer");
    captureEvent("share_modal_opened", { surface: "utility_button" }); }
  // ── 11. Modal: User status
  function openUserModal() { openModal("userModalLayer"); }
  // ── 12. Modal: About, Language
  function openAboutModal() { openModal("aboutModalLayer"); }
  async function selectLanguage(item) { const lang = item?.dataset.lang;
    if (!lang) return;
    const current = document.body.dataset.currentLang || document.documentElement.lang || "en";
    if (lang === current) { closeModal("langModalLayer"); return; }
    item.classList.add("is-loading");
    item.querySelector(".lang-item__check")?.classList.add("hidden");
    item.querySelector(".lang-item__spinner")?.classList.remove("hidden");
    try { await apiPost("/api/language", { lang });
      window.location.reload();
    } catch (err) { item.classList.remove("is-loading");
      item.querySelector(".lang-item__check")?.classList.remove("hidden");
      item.querySelector(".lang-item__spinner")?.classList.add("hidden");
      setText("langError", err.message || "Could not change language.");
      notify("Could not change language. Please try again.", "error"); } }
  // ── 13. DOMContentLoaded init
  function insertMagicBanner() {
    if ($("magicSuccessBanner")) return;
    const form = $("coordForm");
    const banner = document.createElement("div");
    banner.id = "magicSuccessBanner";
    banner.className = "success-banner";
    banner.textContent = "✅ Research Access active. Your report is ready.";
    form?.parentNode?.insertBefore(banner, form); }
  function stripQueryParams() {
    try { window.history.replaceState({}, "", window.location.pathname); } catch (_) {} }
  function handleMagicErrorLanding(params) {
    const error = params.get("error");
    if (!error) return false;
    if (error === "invalid_link") { setText("coordError", "This access link has expired or has already been used. Request a new one from the Join Research modal.");
    } else if (error === "system_error") { setText("coordError", "Something went wrong activating your access. Please try the link again or request a new one.");
      const errorCode = params.get("code");
      console.error(JSON.stringify({
        level: "error",
        event: "magic_link_error_landing",
        error_param: error,
        code: errorCode || null,
        path: window.location.pathname
      }));
      logClientException("magic_link_error_landing",
        new Error(`magic_link_error: ${error}`),
        { error_param: error, code: errorCode || null }
      );
      try {
        window.posthog?.capture?.("magic_link_error_landing", {
          error_param: error,
          code: errorCode || null
        });
        window.posthog?.flush?.();
      } catch (_) {} }
    stripQueryParams();
    return true; }
  function restoreAfterMagicSuccess() {
    const params = new URLSearchParams(window.location.search);
    if (handleMagicErrorLanding(params)) return;
    const magicSuccess = params.get("magic_success") === "1";
    const resumeRaw = localStorage.getItem("dd_resume_state");
    if (!magicSuccess) return;
    if (!resumeRaw) { stripQueryParams(); insertMagicBanner(); return; }
    let resume = null;
    try { resume = JSON.parse(resumeRaw); } catch (err) { stripQueryParams(); insertMagicBanner(); return; }
    if (Number.isFinite(Number(resume?.lat)) && Number.isFinite(Number(resume?.lng))) { const lat = Number(resume.lat);
      const lng = Number(resume.lng);
      state.coords = { lat, lng, valid: true, key: normalizeKey(`${lat},${lng}`) };
      const input = $("locationInput");
      if (input) input.value = resume.text || "";
      const restoredScore = Number(resume.constructionScore);
      if (Number.isFinite(restoredScore)) { state.hero.constructionScore = restoredScore;
        state.hero.constructionStatus = "ready";
        animateGauge($("constructionBand"), $("constructionNeedle"), restoredScore);
        setText("constructionMessage", resume.constructionMessage || "");
        fetchConstruction({ restoredScore });
      } else { fetchConstruction(); } }
    localStorage.removeItem("dd_resume_state");
    stripQueryParams();
    insertMagicBanner();
    syncAccessUI();
    updateButtons();
    try {
      console.info(JSON.stringify({
        level: "info",
        event: "magic_link_resume_restored",
        had_score: Number.isFinite(Number(resume?.constructionScore)),
        tier: AccessState.get().tier,
        path: window.location.pathname
      }));
      window.posthog?.capture?.("magic_link_resume_restored", {
        had_score: Number.isFinite(Number(resume?.constructionScore)),
        tier: AccessState.get().tier
      });
      window.posthog?.flush?.();
    } catch (_) {} }
  function bindModalCloseControls() {
    document.addEventListener("click", (event) => { const closeBtn = event.target.closest?.("[data-close]");
      if (closeBtn) { event.preventDefault();
        closeModal(closeBtn.dataset.close); } });
    ["supportModalLayer", "reportModalLayer", "shareModalLayer", "userModalLayer", "aboutModalLayer", "langModalLayer"].forEach((id) => { const el = $(id);
      el?.addEventListener("cancel", (event) => { event.preventDefault(); closeModal(id); });
      el?.addEventListener("click", (event) => { if (event.target === el) closeModal(id); }); });
    document.addEventListener("visibilitychange", () => { if (document.visibilityState === "hidden") cancelQuietCelebration(); });
    window.addEventListener("pagehide", cancelQuietCelebration);
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      const active = state.modals.active;
      if (!active) return;
      const el = $(active);
      // Native <dialog> elements already fire cancel on Escape;
      // prevent double-close for those.
      if (el?.tagName === "DIALOG" && !el.classList.contains("bottom-sheet")) return;
      event.preventDefault();
      closeModal(active);
    }); }
  function bindEvents() {
    $("locationInput")?.addEventListener("input", () => { resetHeroResultUi(); debouncedParseHeroLocation(); });
    $("locationInput")?.addEventListener("paste", () => { resetHeroResultUi(); });
    $("coordForm")?.addEventListener("submit", (event) => { event.preventDefault(); fetchConstruction({ trigger: "form_submit" }); });
    $("mainActionBtn")?.addEventListener("click", (event) => { event.preventDefault(); fetchConstruction({ trigger: "main_button_click" }); });
    $("constructionGoBtn")?.addEventListener("click", (event) => { event.preventDefault(); fetchConstruction({ trigger: "construction_button_click" }); });
    $("demandGoBtn")?.addEventListener("click", (event) => { event.preventDefault(); fetchDemand(); });
    $("supportBtn")?.addEventListener("click", (event) => { event.preventDefault(); if (!AccessState.get().demandAllowed) openJoinResearchModal("hero_unlock_button"); });
    $("userUpgradeBtn")?.addEventListener("click", (event) => { event.preventDefault(); openJoinResearchModal("user_access_modal"); });
    $("userMenuBtn")?.addEventListener("click", openUserModal);
    $("userMenuBtn")?.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openUserModal(); } });
    $("reportBtn")?.addEventListener("click", (event) => { event.preventDefault(); resetReportModal(); openModal("reportModalLayer"); });
    $("shareBtn")?.addEventListener("click", (event) => { 
      event.preventDefault(); 
      openShareModal(); 
      try {
        window.posthog?.capture?.("share_clicked", {
          $current_url: window.location.href
        });
        window.posthog?.flush?.();
      } catch (_) {}
    });
    $("aboutBtn")?.addEventListener("click", (event) => { event.preventDefault(); openAboutModal(); });
    $("langOpenBtn")?.addEventListener("click", (event) => { event.preventDefault(); openModal("langModalLayer"); });
    $("proceedToPaymentBtn")?.addEventListener("click", submitMagicLink);
    $("resendLinkBtn")?.addEventListener("click", resendMagicLink);
    $("cancelPaymentBtn")?.addEventListener("click", (event) => { event.preventDefault(); closeModal("supportModalLayer"); });
    $("purchaseEmail")?.addEventListener("input", () => { state.unlock.email = normalizeInput($("purchaseEmail")?.value || ""); syncResendButtonState(); });
    $("planGrid")?.addEventListener("click", (event) => { const card = event.target.closest?.("[data-plan]");
      if (!card) return;
      if (card.disabled || card.classList.contains("plan-card-disabled")) {
        logFlowEvent("disabled_access_level_clicked", { action: "disabled_access_level_clicked", status: "blocked", ui_surface: state.unlock.uiSurface, step: "purchaseStep1", error_code: "disabled_not_available_in_simulated_flow", error_message: "72_hour_preview" });
        return; }
      state.unlock.plan = card.dataset.plan || "sim_1_day";
      $("planGrid")?.querySelectorAll("[data-plan]").forEach((el) => { const active = el === card;
        el.classList.toggle("active", active);
        el.setAttribute("aria-checked", active ? "true" : "false"); }); });
    $("reportLocationInput")?.addEventListener("input", debouncedParseReportLocation);
    $("reportTypeGrid")?.addEventListener("click", (event) => { const pill = event.target.closest?.("[data-report-type]");
      if (!pill) return;
      state.report.type = REPORT_TYPE_VALUES[pill.dataset.reportType] || REPORT_TYPE_VALUES[normalizeInput(pill.textContent || "")] || "active";
      $("reportTypeGrid")?.querySelectorAll("[data-report-type]").forEach((el) => { const active = el === pill;
        el.classList.toggle("active", active);
        el.setAttribute("aria-checked", active ? "true" : "false"); }); });
    $("reportNote")?.addEventListener("input", () => { const text = $("reportNote")?.value || ""; state.report.note = text; setText("reportCharCount", `${text.length}/180`); });
    $("reportSubmitBtn")?.addEventListener("click", submitReport);
    $("shareText")?.addEventListener("input", updateShareCounter);
    $("nativeShareBtn")?.addEventListener("click", (event) => { event.preventDefault(); shareNative(); });
    $("copyLinkBtn")?.addEventListener("click", (event) => { event.preventDefault(); copyToClipboard(getShareUrl()); });
    $("copyAllBtn")?.addEventListener("click", (event) => { event.preventDefault(); copyAll(); });
    $("langList")?.addEventListener("click", (event) => { const item = event.target.closest?.(".lang-item"); if (item) selectLanguage(item); }); }
  function initialGaugeRender() {
    animateGauge($("constructionBand"), $("constructionNeedle"), state.hero.constructionScore || 0);
    animateGauge($("demandBand"), $("demandNeedle"), state.hero.demandScore || 0); }
  function init() {
    initFrontendSentry();
    installGlobalErrorReporting();
    try {
      const initPayload = {
        level: "info",
        event: "app_js_init",
        tier: document.body?.dataset.tier || "unknown",
        demand_allowed: document.body?.dataset.demandAllowed || "false",
        daily_limit: document.body?.dataset.dailyLimit || "3",
        has_sentry: Boolean(window.__DD_SENTRY_INIT_DONE),
        has_posthog: Boolean(window.posthog),
        has_turnstile_sitekey: Boolean(document.body?.dataset.turnstileSitekey),
        path: window.location.pathname,
        referrer: document.referrer || null,
        ua: navigator.userAgent.slice(0, 120)
      };
      console.info(JSON.stringify(initPayload));
      window.posthog?.capture?.("app_js_init", initPayload);
      window.posthog?.flush?.();
    } catch (_) {}
    AccessState.subscribe(syncAccessUI);
    AccessState.readDomAccess();
    bindModalCloseControls();
    bindEvents();
    restoreAfterMagicSuccess();
    initialGaugeRender();
    if (document.body?.dataset.turnstileRequired === "true") { showHeroTurnstileChallenge(); heroTurnstile.init(); }
    syncAccessUI();
    updateButtons();
    window.App = { AccessState, state, openModal, closeModal, openJoinResearchModal, fetchConstruction, fetchDemand, parseHeroLocation, heroTurnstile, unlockTurnstile, reportTurnstile, notify, captureEvent, logFlowEvent, logClientException, cancelQuietCelebration }; }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init, { once: true });
  else init();
})();
