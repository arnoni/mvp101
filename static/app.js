const AccessState = (() => {
  let access = { tier: "free", demandAllowed: false, dailyLimit: 3 };
  const listeners = new Set();

  function emit() {
    listeners.forEach((listener) => listener({ ...access }));
  }

  function readDomAccess() {
    const appEl = document.getElementById("app");
    const bodyTier = document.body?.dataset.tier || "free";
    const appTier = appEl?.dataset.tier || bodyTier;
    const rawDemand = appEl?.dataset.demandAllowed ?? document.body?.dataset.demandAllowed ?? "false";
    const rawDailyLimit = appEl?.dataset.dailyLimit ?? document.body?.dataset.dailyLimit ?? "3";
    const parsedDailyLimit = Number.parseInt(rawDailyLimit, 10);
    access = { tier: appTier, demandAllowed: rawDemand === "true", dailyLimit: Number.isFinite(parsedDailyLimit) ? parsedDailyLimit : 3 };
    emit();
  }

  function set(nextAccess) {
    access = { ...access, ...nextAccess };
    const demandAllowed = access.demandAllowed ? "true" : "false";
    document.body.dataset.demandAllowed = demandAllowed;
    document.body.dataset.tier = access.tier;
    document.body.dataset.dailyLimit = String(access.dailyLimit ?? 3);
    const appEl = document.getElementById("app");
    if (appEl) {
      appEl.dataset.demandAllowed = demandAllowed;
      appEl.dataset.tier = access.tier;
      appEl.dataset.dailyLimit = String(access.dailyLimit ?? 3);
    }
    emit();
  }

  return {
    readDomAccess,
    set,
    get: () => ({ ...access }),
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    }
  };
})();

function updateAccessState(isPaid, tier = null, dailyLimit = null) {
  const normalizedTier = tier || (isPaid ? "paid" : "free");
  const next = { tier: normalizedTier, demandAllowed: Boolean(isPaid) };
  if (Number.isFinite(dailyLimit)) {
    next.dailyLimit = Math.max(1, Math.trunc(dailyLimit));
  }
  AccessState.set(next);
}

document.addEventListener("DOMContentLoaded", () => {
  // 1. STATE MACHINE (Single Source of Truth)
  const state = {
    coords: { lat: null, lng: null, valid: false, key: null },
    input: { kind: "empty", original: "", preview: "", parsed: null, error: "", touched: false },
    construction: { status: "idle", coordKey: null, score: null },
    demand: { status: "idle", coordKey: null, score: null },
    verification: { required: false, passed: false, token: null, widgetId: null, renderAttempts: 0 },
    modals: { active: null, history: [] },
    unlock: { email: "", plan: "sim_1_day", resendCooldownUntil: 0, lastTurnstileToken: null, checkoutSubmitting: false },
    requests: { construction: null, demand: null, parsePreview: null },
    debounce: null
  };

  const normalizeKey = k => k ? k.split(',').map(n => parseFloat(n).toFixed(4)).join(',') : null;

  // DOM Elements
  const els = {
    location: document.getElementById("locationInput"),
    preview: document.getElementById("parsedPreview"),
    err: document.getElementById("coordError"),
    mainBtn: document.getElementById("mainActionBtn"),
    
    conBtn: document.getElementById("constructionGoBtn"),
    conMsg: document.getElementById("constructionMessage"),
    conBand: document.getElementById("constructionBand"),
    conNeedle: document.getElementById("constructionNeedle"),
    
    demBtn: document.getElementById("demandGoBtn"),
    demMsg: document.getElementById("demandMessage"),
    demBand: document.getElementById("demandBand"),
    demNeedle: document.getElementById("demandNeedle"),

    turnstileSlot: document.getElementById("turnstileSlot"),
    turnstileContainer: document.getElementById("turnstileContainer"),
    
    unlockEmail: document.getElementById("unlockEmail"),
    unlockEmailErr: document.getElementById("unlockEmailError"),
    continuePaymentBtn: document.getElementById("continueToPaymentBtn"),
    awaitingEmailDisplay: document.getElementById("awaitingEmailDisplay")
  };
  const labels = {
    constructionGo: els.conBtn?.dataset.labelGo || (els.conBtn?.textContent || "").trim(),
    demandGo: els.demBtn?.dataset.labelGo || document.body.dataset.labelGo || (els.demBtn?.textContent || "").trim(),
    demandUnlock: els.demBtn?.dataset.labelUnlock || document.body.dataset.labelUnlock || (els.demBtn?.textContent || "").trim(),
    demandReady: els.demMsg?.dataset.labelReady || document.body.dataset.labelReady || (els.demMsg?.textContent || "").trim(),
    demandLocked: els.demMsg?.dataset.labelLocked || document.body.dataset.labelPaidRequired || (els.demMsg?.textContent || "").trim(),
    parsedAs: document.body.dataset.labelParsedAs || "Parsed as:",
    parsingLink: document.body.dataset.labelParsingLink || "Parsing link...",
    passActivatedToast: document.body.dataset.labelPassActivatedToast || "🎉 Pass Activated! You now have full access.",
    coordinatesChanged: document.body.dataset.labelCoordinatesChanged || "Coordinates changed",
    completeVerification: document.body.dataset.labelCompleteVerification || "Complete Verification",
    verify: document.body.dataset.labelVerify || "Verify",
    checkConstruction: document.body.dataset.labelCheckConstruction || "Check Construction",
    analyzingSignals: document.body.dataset.labelAnalyzingSignals || "Analyzing signals...",
    constructionComingSoon: document.body.dataset.labelConstructionComingSoon || "Coming soon...",
    checkingDemand: document.body.dataset.labelCheckingDemand || "Checking demand...",
    verificationRequired: document.body.dataset.labelVerificationRequired || "Verification required",
    verificationLoadingChallenge: document.body.dataset.labelVerificationLoadingChallenge || "Loading verification challenge…",
    verificationUnavailableSitekeyMissing: document.body.dataset.labelVerificationUnavailableSitekeyMissing || "Verification unavailable: site key missing.",
    verificationUnableToLoad: document.body.dataset.labelVerificationUnableToLoad || "Unable to load verification challenge. Please refresh and try again.",
    verificationFailedRefresh: document.body.dataset.labelVerificationFailedRefresh || "Verification failed. Please refresh.",
    verificationUnableToRender: document.body.dataset.labelVerificationUnableToRender || "Unable to render verification challenge. Please refresh.",
    verificationCompleteReady: document.body.dataset.labelVerificationCompleteReady || "Security check complete. Ready to check construction.",
    coordinatesNotSet: document.body.dataset.labelCoordinatesNotSet || "Coordinates not set",
    reportMinChars: document.body.dataset.labelReportMinChars || "Report must be at least 10 characters.",
    shareOpenFailed: document.body.dataset.labelShareOpenFailed || "Could not open sharing options.",
    copyFailedManual: document.body.dataset.labelCopyFailedManual || "Could not copy. Please copy manually.",
    emailInvalid: document.body.dataset.labelEmailInvalid || "Please enter a valid email address.",
    securityCheckRequired: document.body.dataset.labelSecurityCheckRequired || "Please complete the security check.",
    redirectingResearchAccess: document.body.dataset.labelRedirectingResearchAccess || "Redirecting to research access...",
    joinResearchCta: document.body.dataset.labelJoinResearchCta || "Join Research ➔",
    resendEnterEmail: document.body.dataset.labelResendEnterEmail || "Enter your email above, then click Resend.",
    resendFreshSecurity: document.body.dataset.labelResendFreshSecurity || "Please complete a fresh security check before resend.",
    errorShortUrlBlocked: document.body.dataset.labelErrorShortUrlBlocked || "We could not open this short Google Maps link due to access restrictions. Please open it in Google Maps, copy the full URL, and try again.",
    errorLocationNotSupported: document.body.dataset.labelErrorLocationNotSupported || "This location is outside supported regions. Please use a location inside supported coverage areas."
  };

  function getParserErrorMessage(payload, status) {
    // The middleware wraps HTTPException as: { detail: { error: "HTTP_ERROR", detail: { error_code, message }, status_code } }
    // Support both wrapped and unwrapped forms.
    const inner = payload?.detail?.detail || payload?.detail || payload || {};
    const errorCode = inner?.error_code;
    const msg = inner?.message;

    if (errorCode === "SHORT_URL_RESOLUTION_BLOCKED") {
      return labels.errorShortUrlBlocked || msg || "We could not open this short Google Maps link due to access restrictions. Please open it in Google Maps, copy the full URL, and try again.";
    }
    if (errorCode === "LOCATION_NOT_SUPPORTED") {
      return labels.errorLocationNotSupported || msg || "This location is outside supported regions. Please use a location inside supported coverage areas.";
    }
    if (errorCode === "UNSUPPORTED_LOCATION_INPUT") {
      return "Please use a Google Maps link or latitude/longitude coordinates.";
    }
    if (errorCode === "INVALID_COORDINATE_RANGE") {
      return "Coordinates are out of range. Latitude must be between -90 and 90, and longitude between -180 and 180.";
    }
    if (errorCode === "SHORT_URL_RESOLUTION_FAILED") {
      return "Could not expand this Google Maps short link. Please try again.";
    }
    if (errorCode === "MALFORMED_LOCATION_INPUT" || errorCode === "INVALID_LOCATION_INPUT") {
      return "Could not read coordinates from that location input. Please check the link and try again.";
    }
    return `Parser service returned HTTP ${status}.`;
  }

  async function parseLocationPreview(raw, signal) {
    let response;
    try {
      response = await fetch("/api/parse-location", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ location_input: raw }),
        signal
      });
    } catch (networkErr) {
      if (networkErr?.name === "AbortError") throw networkErr;
      console.warn(JSON.stringify({
        event: "parser_network_error",
        error: networkErr?.message,
        input_length: raw?.length
      }));
      throw new Error("Could not reach the parser service. Please check your connection and try again.");
    }

    let payload = null;
    try {
      payload = await response.json();
    } catch (jsonErr) {
      console.warn(JSON.stringify({
        event: "parser_response_not_json",
        http_status: response.status,
        error: jsonErr?.message
      }));
      if (!response.ok) {
        throw new Error(`Parser service returned HTTP ${response.status}.`);
      }
      throw new Error("Parser service returned an unreadable response.");
    }

    if (!response.ok) {
      const msg = getParserErrorMessage(payload, response.status);
      console.warn(JSON.stringify({
        event: "parser_error_response",
        http_status: response.status,
        error_code: payload?.detail?.detail?.error_code || payload?.detail?.error_code || null,
        message: msg
      }));
      throw new Error(msg);
    }

    if (!payload?.ok) {
      console.warn(JSON.stringify({
        event: "parser_payload_not_ok",
        http_status: response.status,
        payload_keys: payload ? Object.keys(payload) : null
      }));
      throw new Error("The parser returned an unexpected response. Please try again.");
    }

    const lat = Number(payload.normalized?.latitude);
    const lng = Number(payload.normalized?.longitude);
    const normalizedText = payload.normalized?.display;

    if (!payload.normalized || isNaN(lat) || isNaN(lng)) {
      console.warn(JSON.stringify({
        event: "parser_missing_coordinates",
        has_normalized: !!payload.normalized,
        lat_raw: payload.normalized?.latitude,
        lng_raw: payload.normalized?.longitude
      }));
      throw new Error("The parser could not return coordinates for this location input.");
    }

    return { lat, lng, normalizedText };
  }

  AccessState.readDomAccess();
  AccessState.subscribe(() => {
    syncAccessUI();
    updateButtons();
  });

  function syncAccessUI() {
    const demandAllowed = AccessState.get().demandAllowed;
    if (els.demBtn) {
      els.demBtn.textContent = demandAllowed ? labels.demandGo : labels.demandUnlock;
      els.demBtn.classList.toggle("unlock-styled", !demandAllowed);
    }
    if (els.demMsg && state.demand.status === "idle") {
      els.demMsg.textContent = demandAllowed ? labels.demandReady : labels.demandLocked;
    }
    const supportBtn = document.getElementById("supportBtn");
    if (supportBtn) {
      const paidLabel = supportBtn.dataset.labelActive || document.body.dataset.labelActive || supportBtn.textContent.trim();
      const unlockLabel = supportBtn.dataset.labelUnlock || document.body.dataset.labelUnlock || supportBtn.textContent.trim();
      supportBtn.textContent = demandAllowed ? paidLabel : unlockLabel;
      supportBtn.classList.toggle("accent", !demandAllowed);
      supportBtn.classList.toggle("active", demandAllowed);
      supportBtn.disabled = demandAllowed;
      supportBtn.setAttribute("aria-disabled", demandAllowed ? "true" : "false");
    }
  }

  // Success redirect handling: force a server round-trip so template tier data is refreshed.
  const urlParams = new URLSearchParams(window.location.search);
  const magicSuccessJustLanded = urlParams.get("magic_success") === "1";

  function normalizeInput(raw) {
    return (raw || "")
      .replace(/[\u2018\u2019]/g, "'")
      .replace(/[\u201C\u201D]/g, "\"")
      .replace(/\u00A0/g, " ")
      .trim();
  }

  function validateLatLng(lat, lng) {
    if (!Number.isFinite(lat) || !Number.isFinite(lng)) throw new Error("Invalid coordinates.");
    if (lat < -90 || lat > 90) throw new Error("Latitude must be between -90 and 90.");
    if (lng < -180 || lng > 180) throw new Error("Longitude must be between -180 and 180.");
  }

  function classifyLocationInput(raw) {
    if (!raw || !raw.trim()) return "empty";
    if (raw.length > 2048) return "invalid";
    if (/[\u0000-\u001F\u007F]/.test(raw)) return "invalid";
    if (/^https?:\/\//i.test(raw)) {
      try {
        const url = new URL(raw);
        const host = url.hostname.toLowerCase();
        if (host === "maps.app.goo.gl") return "google_maps_short_url";
        if (host === "goo.gl" && (url.pathname === "/maps" || url.pathname.startsWith("/maps/"))) return "google_maps_short_url";
        if (host === "g.page" && url.pathname && url.pathname !== "/") return "google_maps_short_url";
        if ((host === "google.com" || host.endsWith(".google.com")) && url.pathname.includes("/maps")) return "google_maps_url";
        if (host === "maps.google.com") return "google_maps_url";
        return "invalid";
      } catch {
        return "invalid";
      }
    }
    if (/^[+-]?\d+(?:\.\d+)?\s*,\s*[+-]?\d+(?:\.\d+)?$/.test(raw)) return "decimal_pair";
    if (/^[+-]?\d+(?:\.\d+)?\s+[+-]?\d+(?:\.\d+)?$/.test(raw)) return "decimal_pair";
    if (/[NSEW]/i.test(raw) && /°/.test(raw)) return "degree_pair";
    return "invalid";
  }

  function parseDecimalPair(raw) {
    if (/^\d+,\d+\s+\d+,\d+$/.test(raw)) {
      throw new Error("Locale decimal commas are not supported. Use decimal point.");
    }
    const parts = raw.split(/[,\s]+/).filter(Boolean);
    if (parts.length !== 2) throw new Error("Enter exactly two decimal values.");
    let lat = Number(parts[0]);
    let lng = Number(parts[1]);
    let note = "";
    if (Math.abs(lat) > 90 && Math.abs(lat) <= 180 && Math.abs(lng) <= 90) {
      [lat, lng] = [lng, lat];
      note = "Parsed as longitude, latitude input. Normalized to latitude, longitude.";
    }
    validateLatLng(lat, lng);
    return { lat, lng, normalizedText: `${lat.toFixed(6)}, ${lng.toFixed(6)}`, note };
  }

  function parseDmsToken(token) {
    const m = token.match(/(\d{1,3})\D+(\d{1,2})\D+(\d{1,2}(?:\.\d+)?)\D*([NSEW])/i);
    if (!m) throw new Error("Invalid degree format.");
    const deg = Number(m[1]);
    const min = Number(m[2]);
    const sec = Number(m[3]);
    const hemi = m[4].toUpperCase();
    if (min >= 60 || sec >= 60) throw new Error("Degree format minutes/seconds out of range.");
    let value = deg + (min / 60) + (sec / 3600);
    if (hemi === "S" || hemi === "W") value *= -1;
    return { value, hemi };
  }

  function parseDegreePair(raw) {
    const tokens = raw.match(/\d{1,3}[^NSEW]*[NSEW]/gi) || [];
    if (tokens.length !== 2) throw new Error("Degree format must include latitude and longitude with hemisphere markers.");
    const first = parseDmsToken(tokens[0]);
    const second = parseDmsToken(tokens[1]);
    const lat = ["N", "S"].includes(first.hemi) ? first.value : second.value;
    const lng = ["E", "W"].includes(first.hemi) ? first.value : second.value;
    if (!["N", "S"].includes(first.hemi) && !["N", "S"].includes(second.hemi)) {
      throw new Error("Missing latitude hemisphere marker.");
    }
    if (!["E", "W"].includes(first.hemi) && !["E", "W"].includes(second.hemi)) {
      throw new Error("Missing longitude hemisphere marker.");
    }
    validateLatLng(lat, lng);
    return { lat, lng, normalizedText: `${lat.toFixed(6)}, ${lng.toFixed(6)}` };
  }

  function extractPair(raw) {
    const m = raw.match(/([+-]?\d+(?:\.\d+)?)\s*,\s*([+-]?\d+(?:\.\d+)?)/);
    if (!m) return null;
    const lat = Number(m[1]);
    const lng = Number(m[2]);
    validateLatLng(lat, lng);
    return [lat, lng];
  }

  function parseGoogleMapsLongUrl(raw) {
    const url = new URL(raw);
    const rawUrl = decodeURIComponent(raw);
    for (const key of ["q", "ll", "query", "center", "destination", "origin", "saddr", "daddr"]) {
      const pair = extractPair(url.searchParams.get(key) || "");
      if (pair) return { lat: pair[0], lng: pair[1], normalizedText: `${pair[0].toFixed(6)}, ${pair[1].toFixed(6)}`, sourceKind: `query_${key}` };
    }
    const place = rawUrl.match(/!3d([+-]?\d+(?:\.\d+)?)!4d([+-]?\d+(?:\.\d+)?)/);
    if (place) {
      const lat = Number(place[1]);
      const lng = Number(place[2]);
      validateLatLng(lat, lng);
      return { lat, lng, normalizedText: `${lat.toFixed(6)}, ${lng.toFixed(6)}`, sourceKind: "place_3d4d" };
    }
    const placeReverse = rawUrl.match(/!2d([+-]?\d+(?:\.\d+)?)!3d([+-]?\d+(?:\.\d+)?)/);
    if (placeReverse) {
      const lng = Number(placeReverse[1]);
      const lat = Number(placeReverse[2]);
      validateLatLng(lat, lng);
      return { lat, lng, normalizedText: `${lat.toFixed(6)}, ${lng.toFixed(6)}`, sourceKind: "place_2d3d" };
    }
    const vp = rawUrl.match(/@([+-]?\d+(?:\.\d+)?),([+-]?\d+(?:\.\d+)?)/);
    if (vp) {
      const lat = Number(vp[1]);
      const lng = Number(vp[2]);
      validateLatLng(lat, lng);
      return { lat, lng, normalizedText: `${lat.toFixed(6)}, ${lng.toFixed(6)}`, sourceKind: "viewport_center" };
    }
    throw new Error(
      "Could not extract coordinates from this Google Maps URL. " +
      "If you pasted a short link, wait for it to expand first, or copy the full Google Maps URL from your browser."
    );
  }

  function buildSubmitPayload() {
    const parsed = state.input.parsed;
    return {
      location_input: state.input.original,
      input_kind_hint: state.input.kind,
      client_parsed_lat: parsed ? parsed.lat : null,
      client_parsed_lng: parsed ? parsed.lng : null,
      turnstile_token: state.verification.token
    };
  }

  function getApiErrorCode(payload) {
    if (!payload || typeof payload !== "object") return "";
    if (typeof payload.error === "string") return payload.error;
    if (payload.detail && typeof payload.detail === "object" && typeof payload.detail.error === "string") {
      return payload.detail.error;
    }
    return "";
  }

  function ensureTurnstileScript() {
    if (window.turnstile) return;
    const existing = document.querySelector('script[data-turnstile-script="1"]');
    if (existing) return;
    const script = document.createElement("script");
    script.src = "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit";
    script.async = true;
    script.defer = true;
    script.dataset.turnstileScript = "1";
    document.head.appendChild(script);
  }

  // 2. COORDINATE PARSING & HARD RESET
  function updateLocationInputState() {
    clearTimeout(state.debounce);
    if (state.requests.parsePreview) {
      state.requests.parsePreview.abort();
      state.requests.parsePreview = null;
    }
    state.debounce = setTimeout(async () => {
      const raw = normalizeInput(els.location.value);
      const kind = classifyLocationInput(raw);
      let parsed = null;
      let error = "";
      let preview = "";
      let parseController = null;
      try {
        if (kind === "decimal_pair") parsed = parseDecimalPair(raw);
        else if (kind === "degree_pair") parsed = parseDegreePair(raw);
        else if (kind === "google_maps_url") parsed = parseGoogleMapsLongUrl(raw);
        else if (kind === "google_maps_short_url") {
          parseController = new AbortController();
          state.requests.parsePreview = parseController;
          preview = labels.parsingLink;
          state.input = { kind, original: raw, preview, parsed: null, error: "", touched: state.input.touched };
          els.preview.textContent = preview;
          els.preview.classList.toggle("hidden", !preview);
          els.err.textContent = "";
          updateButtons();
          parsed = await parseLocationPreview(raw, parseController.signal);
        }
        else if (kind === "invalid") error = "Unsupported input. Use coordinates or Google Maps link.";
      } catch (e) {
        if (e?.name === "AbortError") return;
        error = e.message || "Invalid location input.";
      } finally {
        if (state.requests.parsePreview === parseController) {
          state.requests.parsePreview = null;
        }
      }

      const valid = Boolean(parsed);
      const lat = parsed ? parsed.lat : null;
      const lng = parsed ? parsed.lng : null;
      const newKey = parsed ? `${lat.toFixed(4)},${lng.toFixed(4)}` : null;
      if (parsed) {
        preview = parsed.note ? `${parsed.note} ${labels.parsedAs} ${parsed.normalizedText}` : `${labels.parsedAs} ${parsed.normalizedText}`;
      }

      // Hard Reset: If coordinates change, invalidate ALL previous data to prevent ghost states
      if (state.coords.key && state.coords.key !== newKey) {
        if (state.requests.construction) state.requests.construction.abort();
        if (state.requests.demand) state.requests.demand.abort();
        
        state.construction = { status: "idle", coordKey: null, score: null };
        state.demand = { status: "idle", coordKey: null, score: null };
        state.verification = { required: false, passed: false, token: null, widgetId: null, renderAttempts: 0 }; 
        
        animateGauge(els.conBand, els.conNeedle, null);
        animateGauge(els.demBand, els.demNeedle, null);
        els.conMsg.textContent = labels.coordinatesChanged;
        const access = AccessState.get();
        els.demMsg.textContent = access.demandAllowed ? labels.demandReady : labels.demandLocked;
        els.turnstileSlot.classList.add("hidden");
      }

      state.coords = { lat, lng, valid: Boolean(parsed), key: newKey };
      state.input = { kind, original: raw, preview, parsed, error, touched: state.input.touched };
      if (parsed) {
        if (window.ModalSystem) window.ModalSystem.setCoords(lat, lng);
      } else {
        if (window.ModalSystem) window.ModalSystem.clearCoords();
      }
      els.preview.textContent = preview;
      els.preview.classList.toggle("hidden", !preview);
      const showError = state.input.touched || document.activeElement !== els.location;
      els.err.textContent = showError ? error : "";
      updateButtons();
    }, 180);
  }

  function updateButtons() {
    const valid = state.coords.valid || state.input.kind === "google_maps_short_url";
    const conLoading = state.construction.status === "loading";
    const demLoading = state.demand.status === "loading";

    els.mainBtn.disabled = !valid || conLoading || demLoading;
    els.conBtn.disabled = !valid || conLoading;
    els.demBtn.disabled = !valid || demLoading;

    if (state.verification.required && !state.verification.passed) {
      els.mainBtn.textContent = labels.completeVerification;
      els.conBtn.textContent = labels.verify;
    } else {
      els.mainBtn.textContent = labels.checkConstruction;
      els.conBtn.textContent = labels.constructionGo;
    }

    const access = AccessState.get();
    els.demBtn.textContent = access.demandAllowed ? labels.demandGo : labels.demandUnlock;
  }

  // 3. SVG ANIMATION MATH
  function animateGauge(bandEl, needleEl, score) {
    const ARC_LENGTH = 377;
    const MIN_ANGLE = -82;
    const MAX_SWEEP = 164;
    const PIVOT_X = 160;
    const PIVOT_Y = 180;
    const DURATION = 800;

    const clampedScore = score === null ? 0 : Math.max(0, Math.min(100, score));
    const targetAngle = score === null
      ? MIN_ANGLE
      : MIN_ANGLE + (clampedScore / 100) * MAX_SWEEP;
    const targetOffset = score === null
      ? ARC_LENGTH
      : ARC_LENGTH - (ARC_LENGTH * (clampedScore / 100));

    // Read current positions
    const currentTransform = needleEl.getAttribute("transform") || `rotate(${MIN_ANGLE} ${PIVOT_X} ${PIVOT_Y})`;
    const match = currentTransform.match(/rotate\(([-\d.]+)/);
    const startAngle = match ? parseFloat(match[1]) : MIN_ANGLE;
    const currentDash = bandEl.style.strokeDashoffset;
    const startOffset = currentDash ? parseFloat(currentDash) : ARC_LENGTH;

    if (needleEl._rafId) cancelAnimationFrame(needleEl._rafId);
    if (!window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      const startTime = performance.now();
      function easeOutBack(t) {
        const c1 = 1.70158, c3 = c1 + 1;
        return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2);
      }

      function tick(now) {
        const timeElapsed = now - startTime;
        let t = timeElapsed / DURATION;
        if (t > 1) t = 1;

        const eased = easeOutBack(t);
        const nextAngle = startAngle + (targetAngle - startAngle) * eased;
        const nextOffset = startOffset + (targetOffset - startOffset) * eased;

        needleEl.setAttribute("transform", `rotate(${nextAngle} ${PIVOT_X} ${PIVOT_Y})`);
        bandEl.style.strokeDashoffset = nextOffset;

        if (t < 1) {
          needleEl._rafId = requestAnimationFrame(tick);
        }
      }
      needleEl._rafId = requestAnimationFrame(tick);
    } else {
      // Reduced motion fallback
      needleEl.setAttribute("transform", `rotate(${targetAngle} ${PIVOT_X} ${PIVOT_Y})`);
      bandEl.style.strokeDashoffset = targetOffset;
    }
  }

  // 4. API CALLS WITH ABORT CONTROLLER
  async function fetchConstruction() {
    if (!(state.coords.valid || state.input.kind === "google_maps_short_url")) return;
    
    // Auto-scroll to turnstile if needed
    if (state.verification.required && !state.verification.passed) {
      els.turnstileSlot.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }

    if (state.requests.construction) state.requests.construction.abort();
    state.requests.construction = new AbortController();
    
    state.construction.status = "loading";
    els.conMsg.textContent = labels.analyzingSignals;
    updateButtons();

    try {
      const res = await fetch("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...buildSubmitPayload(),
          lat: state.coords.lat,
          lon: state.coords.lng,
          target: 'construction'
        }),
        signal: state.requests.construction.signal
      });
      
      let data;
      if (!res.ok) {
        data = await res.json().catch(() => ({}));
        const errorCode = getApiErrorCode(data);
        if (errorCode === "CHALLENGE_REQUIRED" || errorCode === "INVALID_CHALLENGE") {
          state.verification.required = true;
          state.verification.passed = false;
          state.verification.token = null;
          state.construction.status = "blocked";
          els.conMsg.textContent = labels.verificationRequired;
          renderTurnstile();
          updateButtons();
          return;
        }
        console.warn(`Construction API failed with status ${res.status}. Using fallback simulation.`);
        data = { score: 87, coord_key: state.coords.key, message: 'Coming soon...' };
      } else {
        data = await res.json();
      }

      const construction = data.construction || data;
      console.log("SERVER KEY:", construction.coord_key, "| CLIENT KEY:", state.coords.key);
      if (normalizeKey(construction.coord_key || state.coords.key) !== normalizeKey(state.coords.key)) return; // Stale

      if (data.verification_required) {
        state.verification.required = true;
        state.construction.status = "blocked";
        els.conMsg.textContent = labels.verificationRequired;
        renderTurnstile();
        updateButtons();
        return;
      }

      const resolvedScore = Number(construction.score);
      const score = Number.isFinite(resolvedScore) && resolvedScore > 0 ? resolvedScore : 87;
      state.construction = { status: "ready", score: score, coordKey: construction.coord_key || state.coords.key };
      console.log("Triggering animateGauge with score:", score);
      animateGauge(els.conBand, els.conNeedle, score);
      els.conMsg.textContent = labels.constructionComingSoon;

    } catch (e) {
      if (e.name !== "AbortError") {
        console.error("Construction fetch error:", e);
        // Resilient Fallback Simulation
        const fallbackData = { score: 87, coord_key: state.coords.key, message: 'Simulated Analysis (Network Fallback)' };
        state.construction = { status: "ready", score: fallbackData.score, coordKey: fallbackData.coord_key };
        animateGauge(els.conBand, els.conNeedle, fallbackData.score);
        els.conMsg.textContent = fallbackData.message;
      }
    } finally {
      updateButtons();
    }
  }

  async function fetchDemand() {
    if (!state.coords.valid) return;

    if (!AccessState.get().demandAllowed) {
      if (window.ModalSystem?.openJoinResearchModal) {
        ModalSystem.openJoinResearchModal('demand_level_page');
      } else if (window.ModalSystem) {
        ModalSystem.open("supportModalLayer");
      }
      return;
    }

        if (state.requests.demand) state.requests.demand.abort();
    state.requests.demand = new AbortController();
    
    state.demand.status = "loading";
    els.demMsg.textContent = labels.checkingDemand;
    updateButtons();

    try {
      const res = await fetch("/api/search", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lat: state.coords.lat, lon: state.coords.lng, target: "demand" }),
        signal: state.requests.demand.signal
      });
      
      let data;
      if (!res.ok) {
        data = await res.json().catch(() => ({}));
        const errorCode = getApiErrorCode(data);
        if (errorCode === "CHALLENGE_REQUIRED" || errorCode === "INVALID_CHALLENGE") {
          state.verification.required = true;
          state.verification.passed = false;
          state.verification.token = null;
          state.demand.status = "blocked";
          els.demMsg.textContent = labels.verificationRequired;
          renderTurnstile();
          updateButtons();
          return;
        }
        console.warn(`Demand API failed with status ${res.status}. Using fallback simulation.`);
        data = { score: 65, coord_key: state.coords.key, message: 'Demand analyzed (Fallback)' };
      } else {
        data = await res.json();
      }

      const demand = data.demand || data;
      console.log("SERVER KEY:", demand.coord_key, "| CLIENT KEY:", state.coords.key);
      if (normalizeKey(demand.coord_key || state.coords.key) !== normalizeKey(state.coords.key)) return; // Stale

      const score = demand.score !== undefined ? demand.score : 65;
      state.demand = { status: "ready", score: score, coordKey: demand.coord_key || state.coords.key };
      console.log("Triggering animateGauge with score:", score);
      animateGauge(els.demBand, els.demNeedle, score);
      els.demMsg.textContent = (demand && demand.message) || data.message || "Demand analyzed";

    } catch (e) {
      if (e.name !== "AbortError") {
        console.error("Demand fetch error:", e);
        // Resilient Fallback Simulation
        const fallbackData = { score: 65, coord_key: state.coords.key, message: 'Demand analyzed (Network Fallback)' };
        state.demand = { status: "ready", score: fallbackData.score, coordKey: fallbackData.coord_key };
        animateGauge(els.demBand, els.demNeedle, fallbackData.score);
        els.demMsg.textContent = fallbackData.message;
      }
    } finally {
      updateButtons();
    }
  }

  function renderTurnstile() {
    els.turnstileSlot.classList.remove("hidden");
    ensureTurnstileScript();

    const sitekey = (document.body.dataset.turnstileSitekey || "").trim();
    if (!sitekey || ["none", "null", "undefined"].includes(sitekey.toLowerCase())) {
      console.error("Turnstile site key is missing or invalid.");
      els.conMsg.textContent = labels.verificationUnavailableSitekeyMissing;
      return;
    }
    
    // If turnstile isn't loaded yet, try again in 100ms
    if (!window.turnstile) {
      state.verification.renderAttempts = (state.verification.renderAttempts || 0) + 1;
      if (state.verification.renderAttempts > 50) {
        console.error("Turnstile script failed to load after multiple attempts.");
        els.conMsg.textContent = labels.verificationUnableToLoad;
        return;
      }
      els.conMsg.textContent = labels.verificationLoadingChallenge;
      setTimeout(renderTurnstile, 100);
      return;
    }
    state.verification.renderAttempts = 0;

    // If already rendered and container not empty, don't re-render
    if (state.verification.widgetId && els.turnstileContainer.innerHTML !== "") return;
    
    // Clear container just in case
    els.turnstileContainer.innerHTML = "";
    
    try {
      state.verification.widgetId = window.turnstile.render('#turnstileContainer', {
        sitekey,
        theme: 'dark',
        callback: (token) => {
          console.log("Turnstile verified");
          state.verification.passed = true;
          state.verification.token = token;
          state.verification.required = false;
          els.conMsg.textContent = labels.verificationCompleteReady;
          els.turnstileSlot.classList.add("hidden");
          updateButtons();
          fetchConstruction(); // Auto-retry
        },
        'error-callback': (err) => {
          console.error("Turnstile Error:", err);
          state.verification.widgetId = null;
          els.conMsg.textContent = labels.verificationFailedRefresh;
        },
        'expired-callback': () => {
          state.verification.passed = false;
          state.verification.token = null;
          state.verification.widgetId = null;
          renderTurnstile(); // Re-render if expired
        }
      });
    } catch (err) {
      console.error("Turnstile render failed:", err);
      state.verification.widgetId = null;
      els.conMsg.textContent = labels.verificationUnableToRender;
    }
  }

  async function restoreAfterMagicSuccessIfNeeded() {
    if (!magicSuccessJustLanded) return;

    updateAccessState(true, document.body.dataset.tier || "paid");
    window.history.replaceState({}, document.title, window.location.pathname);

    const inputCard = document.querySelector(".input-card");
    const coordForm = document.getElementById("coordForm");
    if (inputCard && coordForm && !document.getElementById("magicSuccessBanner")) {
      const banner = document.createElement("div");
      banner.id = "magicSuccessBanner";
      banner.className = "parsed-preview";
      banner.setAttribute("role", "status");
      banner.setAttribute("aria-live", "polite");
      banner.textContent = "✅ Research Access active. Your report is ready.";
      inputCard.insertBefore(banner, coordForm);
    }

    const rawResumeState = localStorage.getItem("dd_resume_state");
    if (rawResumeState) {
      try {
        const parsedResume = JSON.parse(rawResumeState);
        const lat = Number(parsedResume?.lat);
        const lng = Number(parsedResume?.lng);
        const text = typeof parsedResume?.text === "string" ? parsedResume.text : "";
        if (Number.isFinite(lat) && Number.isFinite(lng)) {
          if (els.location) els.location.value = text;
          state.coords = { lat, lng, valid: true, key: `${lat.toFixed(4)},${lng.toFixed(4)}` };
          state.input = {
            kind: classifyLocationInput(text) || "decimal_pair",
            original: text,
            preview: text ? `${labels.parsedAs} ${text}` : "",
            parsed: { lat, lng, normalizedText: text || `${lat.toFixed(6)}, ${lng.toFixed(6)}` },
            error: "",
            touched: true
          };
          if (window.ModalSystem) window.ModalSystem.setCoords(lat, lng);
          els.preview.textContent = state.input.preview;
          els.preview.classList.toggle("hidden", !state.input.preview);
          els.err.textContent = "";
          updateButtons();
          localStorage.removeItem("dd_resume_state");
          await fetchConstruction();
        }
      } catch (err) {
        console.warn("Failed to restore dd_resume_state", err);
      }
    }

  }


  // Event Listeners Binding
  els.location.addEventListener("input", updateLocationInputState);
  els.location.addEventListener("blur", () => {
    state.input.touched = true;
    updateLocationInputState();
  });
  
  document.getElementById("coordForm").addEventListener("submit", (e) => { e.preventDefault(); fetchConstruction(); });
  els.conBtn.addEventListener("click", fetchConstruction);
  els.demBtn.addEventListener("click", fetchDemand);
  
  animateGauge(els.conBand, els.conNeedle, null);
  animateGauge(els.demBand, els.demNeedle, null);

  // Initial Pre-hydration Check
  if (document.body.dataset.turnstileRequired === "true") {
    state.verification.required = true;
    renderTurnstile();
  }
  syncAccessUI();
  updateButtons();
  void restoreAfterMagicSuccessIfNeeded();
});


/**
 * ==========================================
 * UNIFIED MODAL SYSTEM
 * Merged implementation with best practices
 * ==========================================
 */

const ModalSystem = (function() {
  'use strict';

  // ==========================================
  // STATE MANAGEMENT
  // ==========================================
  const state = {
    coords: {
      lat: null,
      lng: null,
      valid: false,
      key: null
    },
    report: {
      type: 'active_construction',
      note: ''
    },
    unlock: {
      plan: 'sim_1_day',
      email: ''
    },
    language: {
      current: document.body.dataset.currentLang || document.documentElement.lang || 'en',
      selected: document.body.dataset.currentLang || document.documentElement.lang || 'en'
    },
    modals: {
      active: null,
      history: []
    }
  };

  // ==========================================
  // UTILITY FUNCTIONS
  // ==========================================
  
  const utils = {
    newErrorId(prefix = 'ERR') {
      const stamp = Date.now().toString(36).toUpperCase();
      const rand = Math.random().toString(36).slice(2, 8).toUpperCase();
      return `${prefix}-${stamp}-${rand}`;
    },
    /**
     * Validate email format
     */
    isValidEmail(email) {
      return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email);
    },

    /**
     * Debounce function calls
     */
    debounce(fn, delay = 300) {
      let timeout;
      return (...args) => {
        clearTimeout(timeout);
        timeout = setTimeout(() => fn(...args), delay);
      };
    },

    /**
     * Format coordinates for display
     */
    formatCoords(lat, lng) {
      return `${lat.toFixed(6)}, ${lng.toFixed(6)}`;
    },

    /**
     * Show temporary feedback on button
     */
    showButtonFeedback(btn, text, duration = 2000) {
      const originalText = btn.querySelector('.btn-text')?.textContent || btn.textContent;
      const textSpan = btn.querySelector('.btn-text') || btn;
      textSpan.textContent = text;
      setTimeout(() => {
        textSpan.textContent = originalText;
      }, duration);
    },

    /**
     * Toggle loading state on button
     */
    setButtonLoading(btn, isLoading) {
      const text = btn.querySelector('.btn-text');
      const spinner = btn.querySelector('.btn-spinner');
      btn.disabled = isLoading;
      if (text) text.classList.toggle('hidden', isLoading);
      if (spinner) spinner.classList.toggle('hidden', !isLoading);
    },

    /**
     * API POST helper with error handling
     */
    async apiPost(url, body, options = {}) {
      let response;
      const clientErrorId = utils.newErrorId('CLIENT');
      const timeoutMs = Number(options?.timeoutMs) > 0 ? Number(options.timeoutMs) : 20000;
      const controller = new AbortController();
      const timeoutHandle = window.setTimeout(() => controller.abort(), timeoutMs);

      try {
        response = await fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          body: JSON.stringify(body),
          signal: controller.signal
        });
      } catch (networkErr) {
        if (networkErr?.name === 'AbortError') {
          const err = new Error(`Request timed out after ${timeoutMs / 1000}s. Error ID: ${clientErrorId}`);
          err.code = 'REQUEST_TIMEOUT';
          err.errorId = clientErrorId;
          err.cause = networkErr;
          throw err;
        }
        const err = new Error(`Network request failed. Error ID: ${clientErrorId}`);
        err.code = 'NETWORK_REQUEST_FAILED';
        err.errorId = clientErrorId;
        err.cause = networkErr;
        throw err;
      } finally {
        window.clearTimeout(timeoutHandle);
      }

      const data = await response.json().catch(() => ({}));
      
      if (!response.ok) {
        let errMsg = data.message || 'Request failed';
        let errCode = 'REQUEST_FAILED';
        let errId = clientErrorId;
        if (data.detail) {
          if (typeof data.detail === 'string') {
            errMsg = data.detail;
          } else if (Array.isArray(data.detail) && data.detail.length > 0 && data.detail[0].msg) {
            errMsg = data.detail[0].msg;
          } else if (typeof data.detail === 'object') {
            errMsg = data.detail.message || data.detail.detail || data.detail.error || JSON.stringify(data.detail);
            errCode = data.detail.error || errCode;
            errId = data.detail.error_id || errId;
          }
        }
        const err = new Error(errMsg);
        err.status = response.status;
        err.code = errCode;
        err.errorId = errId;
        err.payload = data;
        throw err;
      }
      
      return data;
    },

    notify(message, type = 'info') {
      const toast = document.createElement('div');
      toast.className = `dd-toast dd-toast--${type}`;
      toast.setAttribute('role', 'status');
      toast.setAttribute('aria-live', 'polite');
      toast.textContent = message;
      Object.assign(toast.style, {
        position: 'fixed',
        top: '16px',
        left: '50%',
        transform: 'translateX(-50%)',
        zIndex: '9999',
        maxWidth: 'min(92vw, 560px)',
        background: type === 'error' ? 'rgba(120, 16, 16, 0.96)' : 'rgba(10, 25, 47, 0.96)',
        color: '#ecf2ff',
        border: type === 'error' ? '1px solid rgba(255, 124, 124, 0.45)' : '1px solid rgba(112, 169, 255, 0.35)',
        borderRadius: '12px',
        padding: '12px 16px',
        boxShadow: '0 10px 30px rgba(0, 0, 0, 0.35)',
        fontSize: '15px',
        fontWeight: '600',
        opacity: '0',
        transition: 'opacity 220ms ease'
      });
      document.body.appendChild(toast);
      requestAnimationFrame(() => {
        toast.style.opacity = '1';
      });
      window.setTimeout(() => {
        toast.style.opacity = '0';
        window.setTimeout(() => toast.remove(), 240);
      }, 3000);
    }
  };

  // ==========================================
  // CORE MODAL FUNCTIONALITY
  // ==========================================
  
  const core = {
    isDialogElement(modal) {
      return typeof HTMLDialogElement !== 'undefined' && modal instanceof HTMLDialogElement;
    },

    /**
     * Initialize modal system
     */
    init() {
      this.bindGlobalEvents();
      this.upgradeToNativeDialogs();
    },

    /**
     * Upgrade div-based modals to native <dialog> elements for better accessibility
     * Falls back to class-based approach for older browsers
     */
    upgradeToNativeDialogs() {
      document.querySelectorAll('.modal-layer').forEach(modal => {
        // If browser supports <dialog>, ensure proper method usage
        if (typeof HTMLDialogElement !== 'undefined') {
          modal.addEventListener('click', (e) => {
            if (e.target === modal && modal.classList.contains('native')) {
              this.close(modal.id);
            }
          });
        }
      });
    },

    /**
     * Open a modal by ID
     */
    open(modalId, options = {}) {
      const modal = document.getElementById(modalId);
      if (!modal) {
        console.warn(`Modal #${modalId} not found`);
        return null;
      }

      // Close currently active modal if exists (unless stacking is enabled)
      if (state.modals.active && !options.stack) {
        this.close(state.modals.active, { silent: true });
      }

      try {
        // Show modal using native dialog API or fallback
        if (this.isDialogElement(modal) && !modal.classList.contains('bottom-sheet')) {
          if (!modal.open) {
            modal.showModal();
          }
        } else {
          modal.classList.add('open');
        }
      } catch (err) {
        console.error(`Failed to open modal #${modalId}:`, err);
        return null;
      }
      
      modal.setAttribute('aria-hidden', 'false');
      document.body.style.overflow = 'hidden';
      
      // Track active modal
      state.modals.active = modalId;
      if (!options.silent) {
        state.modals.history.push(modalId);
      }

      // Focus management
      this.trapFocus(modal);
      
      // Trigger open callback if exists
      const event = new CustomEvent('modal:open', { detail: { modalId, options } });
      modal.dispatchEvent(event);

      return modal;
    },

    /**
     * Close a modal by ID
     */
    close(modalId, options = {}) {
      const modal = document.getElementById(modalId || state.modals.active);
      if (!modal) return null;

      try {
        // Use native close or fallback
        if (this.isDialogElement(modal) && !modal.classList.contains('bottom-sheet')) {
          if (modal.open) {
            modal.close();
          }
        } else {
          modal.classList.remove('open');
        }
      } catch (err) {
        console.error(`Failed to close modal #${modal.id}:`, err);
        return null;
      }
      
      modal.setAttribute('aria-hidden', 'true');
      document.body.style.overflow = '';

      // Update state
      if (state.modals.active === modal.id) {
        state.modals.active = null;
      }
      
      if (!options.silent) {
        state.modals.history = state.modals.history.filter(id => id !== modal.id);
      }

      // Return focus to trigger element if stored
      if (modal.triggerElement) {
        modal.triggerElement.focus();
        delete modal.triggerElement;
      }

      // Trigger close callback
      const event = new CustomEvent('modal:close', { detail: { modalId: modal.id, options } });
      modal.dispatchEvent(event);

      return modal;
    },

    /**
     * Close all open modals
     */
    closeAll() {
      document.querySelectorAll('.modal-layer.open, dialog[open]').forEach(modal => {
        this.close(modal.id, { silent: true });
      });
      state.modals.history = [];
    },

    /**
     * Trap focus within modal for accessibility
     */
    trapFocus(modal) {
      if (modal.dataset.focusTrapBound === 'true') return;
      modal.dataset.focusTrapBound = 'true';

      const focusableElements = modal.querySelectorAll(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
      );
      
      if (focusableElements.length === 0) return;
      
      const firstFocusable = focusableElements[0];
      const lastFocusable = focusableElements[focusableElements.length - 1];

      // Focus first element after animation
      setTimeout(() => {
        const primaryAction = modal.querySelector('.modal-primary');
        (primaryAction || firstFocusable).focus();
      }, 100);

      // Handle tab cycling
      modal.addEventListener('keydown', (e) => {
        if (e.key !== 'Tab') return;

        if (e.shiftKey && document.activeElement === firstFocusable) {
          e.preventDefault();
          lastFocusable.focus();
        } else if (!e.shiftKey && document.activeElement === lastFocusable) {
          e.preventDefault();
          firstFocusable.focus();
        }
      });
    },

    /**
     * Bind global modal events
     */
    bindGlobalEvents() {
      // Close buttons
      document.querySelectorAll('[data-close]').forEach(btn => {
        btn.addEventListener('click', () => {
          const modalId = btn.dataset.close;
          this.close(modalId);
        });
      });

      // Click outside to close (for dialogs with backdrops)
      window.closeModal = (modalId) => this.close(modalId);

      // Click outside to close (for legacy non-dialog modals)
      document.querySelectorAll('.modal-layer').forEach(layer => {
        layer.addEventListener('click', (e) => {
          if (e.target === layer && !layer.classList.contains('bottom-sheet')) {
            this.close(layer.id);
          }
        });
      });

      // Escape key to close
      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && state.modals.active) {
          this.close(state.modals.active);
        }
      });

      // Store trigger element for focus return
      document.querySelectorAll('[data-open-modal]').forEach(btn => {
        btn.addEventListener('click', () => {
          const modalId = btn.dataset.openModal;
          const modal = document.getElementById(modalId);
          if (modal) {
            modal.triggerElement = btn;
          }
        });
      });

      window.addEventListener('app:notify', (event) => {
        const detail = event?.detail || {};
        if (!detail.message) return;
        utils.notify(detail.message, detail.type || 'info');
      });
    }
  };

  // ==========================================
  // MODAL-SPECIFIC LOGIC
  // ==========================================
  
  const modals = {
    /**
     * 1. User Access Modal
     */
    user: {
      init() {
        const btn = document.getElementById('userMenuBtn');
        const upgradeBtn = document.getElementById('userUpgradeBtn');
        
        btn?.addEventListener('click', () => {
          core.open('userModalLayer');
        });

        upgradeBtn?.addEventListener('click', () => {
          core.close('userModalLayer');
          modals.support.openJoinResearchModal('user_access_modal');
        });
      },

      updateStatus(tier, demandAllowed, dailyLimit = 3) {
        const access = { tier, demandAllowed, dailyLimit };
        
        const badge = document.getElementById('userTierBadge');
        
        if (badge) {
          const tierSuffix = badge.dataset.labelTier || "TIER";
          badge.textContent = `${access.tier.charAt(0).toUpperCase() + access.tier.slice(1)} ${tierSuffix}`;
          badge.dataset.tier = access.tier;
        }

        const demandStatus = document.getElementById('userDemandStatus');
        if (demandStatus) {
          const unlockedLabel = demandStatus.dataset.labelUnlocked || document.body.dataset.labelDemandUnlocked || demandStatus.textContent.trim();
          const lockedLabel = demandStatus.dataset.labelLocked || document.body.dataset.labelDemandLocked || demandStatus.textContent.trim();
          demandStatus.textContent = access.demandAllowed ? unlockedLabel : lockedLabel;
          demandStatus.classList.toggle('available', access.demandAllowed);
          demandStatus.classList.toggle('locked', !access.demandAllowed);
        }

        const dailyLimitItem = document.getElementById('userDailyLimitItem');
        if (dailyLimitItem) {
          const usageLabel = dailyLimitItem.dataset.labelDailyUsage || 'Daily usage';
          const resolvedDailyLimit = Number.isFinite(access.dailyLimit) ? access.dailyLimit : 3;
          dailyLimitItem.textContent = `${usageLabel}: ${resolvedDailyLimit}/day`;
        }

        const upgradeBtn = document.getElementById('userUpgradeBtn');
        if (upgradeBtn) {
          upgradeBtn.closest('.modal-footer')?.classList.toggle('hidden', access.demandAllowed);
        }

        const supportBtn = document.getElementById('supportBtn');
        if (supportBtn) {
          const paidLabel = supportBtn.dataset.labelActive || document.body.dataset.labelActive || supportBtn.textContent.trim();
          const unlockLabel = supportBtn.dataset.labelUnlock || document.body.dataset.labelUnlock || supportBtn.textContent.trim();
          supportBtn.textContent = access.demandAllowed ? paidLabel : unlockLabel;
          supportBtn.classList.toggle('accent', !access.demandAllowed);
          supportBtn.classList.toggle('active', access.demandAllowed);
          supportBtn.disabled = access.demandAllowed;
          supportBtn.setAttribute('aria-disabled', access.demandAllowed ? 'true' : 'false');
        }
      }
    },

    /**
     * 2. Report Modal
     */
    report: {
      formatTrackedError(err, phase = 'REPORT_FLOW_FAILED') {
        const errorId = err?.errorId || utils.newErrorId('REPORT');
        const errorCode = err?.code || phase;
        const message = err?.message || 'Report flow failed.';
        return `${message} (Code: ${errorCode}, Error ID: ${errorId})`;
      },

      init() {
        const btn = document.getElementById('reportBtn');
        const submitBtn = document.getElementById('reportSubmitBtn');
        const typeGrid = document.getElementById('reportTypeGrid');
        const noteField = document.getElementById('reportNote');
        const charCount = document.getElementById('reportCharCount');

        // Open handler
        btn?.addEventListener('click', () => {
          this.reset();
          this.syncCoords();
          this.updateSubmitState();
          const opened = core.open('reportModalLayer');
          if (!opened) {
            this.showError(this.formatTrackedError(new Error('Could not open report modal. Please refresh and try again.'), 'REPORT_MODAL_OPEN_FAILED'));
          }
        });

        // Report type selection
        typeGrid?.querySelectorAll('[data-report-type]').forEach(chip => {
          chip.addEventListener('click', () => {
            typeGrid.querySelectorAll('[data-report-type]').forEach(el => {
              el.classList.remove('active');
              el.setAttribute('aria-checked', 'false');
            });
            chip.classList.add('active');
            chip.setAttribute('aria-checked', 'true');
            state.report.type = chip.dataset.reportType;
          });
        });

        // Character count
        noteField?.addEventListener('input', (e) => {
          const len = e.target.value.length;
          charCount.textContent = `${len}/180`;
          state.report.note = e.target.value;
        });

        // Submit handler
        submitBtn?.addEventListener('click', async () => {
          await this.submit();
        });
      },

      syncCoords() {
        const display = document.getElementById('reportCoordsDisplay');
        if (!display) return;
        if (state.coords.valid) {
          display.textContent = utils.formatCoords(state.coords.lat, state.coords.lng);
        } else {
          display.textContent = labels.coordinatesNotSet;
        }
      },

      updateSubmitState() {
        const btn = document.getElementById('reportSubmitBtn');
        if (!btn) return;
        const disabled = !state.coords.valid;
        btn.disabled = disabled;
        btn.setAttribute('aria-disabled', disabled ? 'true' : 'false');
      },

      async submit() {
        const btn = document.getElementById('reportSubmitBtn');
        const errorEl = document.getElementById('reportError');
        const formState = document.getElementById('reportFormState');
        const successState = document.getElementById('reportSuccessState');
        if (!btn || !errorEl || !formState || !successState) {
          const err = new Error('Report form is temporarily unavailable.');
          err.code = 'REPORT_MODAL_MISSING_ELEMENTS';
          err.errorId = utils.newErrorId('REPORT');
          console.error('Report modal is missing required elements.', { errorId: err.errorId });
          this.showError(this.formatTrackedError(err, 'REPORT_MODAL_MISSING_ELEMENTS'));
          return;
        }

        if (!state.coords.valid) {
          const err = new Error('Coordinates are missing. Please search for a location and try again.');
          err.code = 'REPORT_COORDS_INVALID';
          err.errorId = utils.newErrorId('REPORT');
          this.showError(this.formatTrackedError(err, 'REPORT_COORDS_INVALID'));
          return;
        }

        const note = (state.report.note || '').trim();
        if (note.length > 0 && note.length < 10) {
          const err = new Error('Report must be at least 10 characters.');
          err.code = 'REPORT_DESCRIPTION_TOO_SHORT';
          err.errorId = utils.newErrorId('REPORT');
          this.showError(this.formatTrackedError(err, 'REPORT_DESCRIPTION_TOO_SHORT'));
          return;
        }
        
        errorEl.textContent = '';
        utils.setButtonLoading(btn, true);

        try {
          await utils.apiPost('/api/user-reports', {
            lat: state.coords.lat,
            lon: state.coords.lng,
            report_kind: state.report.type,
            is_nearby_now: Boolean(document.getElementById('reportNearbyNow')?.checked),
            note
          });

          // Show success state
          formState.classList.add('hidden');
          successState.classList.remove('hidden');
          
          // Reset after delay
          setTimeout(() => {
            core.close('reportModalLayer');
          }, 3000);

        } catch (err) {
          if (err?.status === 422 && err?.code === 'REPORT_DESCRIPTION_TOO_SHORT') {
            errorEl.textContent = labels.reportMinChars;
            return;
          }
          console.error('Report submit failed', {
            code: err?.code,
            errorId: err?.errorId,
            message: err?.message,
            payload: err?.payload
          });
          errorEl.textContent = this.formatTrackedError(err, 'REPORT_SUBMIT_FAILED');
        } finally {
          utils.setButtonLoading(btn, false);
        }
      },

      reset() {
        document.getElementById('reportFormState')?.classList.remove('hidden');
        document.getElementById('reportSuccessState')?.classList.add('hidden');
        const note = document.getElementById('reportNote');
        if (note) note.value = '';
        const nearby = document.getElementById('reportNearbyNow');
        if (nearby) nearby.checked = false;
        const charCount = document.getElementById('reportCharCount');
        if (charCount) charCount.textContent = '0/180';
        const errorEl = document.getElementById('reportError');
        if (errorEl) errorEl.textContent = '';
        state.report.note = '';
        this.updateSubmitState();
        
        // Reset to first option
        const firstType = document.querySelector('[data-report-type="active_construction"]');
        if (firstType) {
          firstType.click();
        }
      },

      showError(msg) {
        const errorEl = document.getElementById('reportError');
        if (errorEl) {
          errorEl.textContent = msg;
          return;
        }
        utils.notify(msg, 'error');
        console.error(msg);
      }
    },

    /**
     * 3. Share Modal
     */
    share: {
      init() {
        const btn = document.getElementById('shareBtn');
        const nativeBtn = document.getElementById('nativeShareBtn');
        const copyLinkBtn = document.getElementById('copyLinkBtn');
        const copyAllBtn = document.getElementById('copyAllBtn');
        const textarea = document.getElementById('shareText');
        const counter = document.getElementById('shareCharCount');

        btn?.addEventListener('click', () => {
          const errorEl = document.getElementById('shareError');
          if (errorEl) errorEl.textContent = '';
          this.updateCharCount();
          core.open('shareModalLayer');
        });

        nativeBtn?.addEventListener('click', () => this.shareNative());
        copyLinkBtn?.addEventListener('click', () => this.copyLink());
        copyAllBtn?.addEventListener('click', () => this.copyAll());

        if (textarea && counter) {
          textarea.addEventListener('input', () => this.updateCharCount());
          this.updateCharCount();
        }
      },

      updateCharCount() {
        const textarea = document.getElementById('shareText');
        const counter = document.getElementById('shareCharCount');
        if (!textarea || !counter) return;
        counter.textContent = `${textarea.value.length} / 220`;
      },

      getShareData() {
        return {
          title: 'DillDrill Construction Check',
          text: document.getElementById('shareText').value.trim(),
          url: document.getElementById('shareUrlBox').textContent.trim()
        };
      },

      async shareNative() {
        const data = this.getShareData();
        const errorEl = document.getElementById('shareError');

        if (errorEl) errorEl.textContent = '';
        
        if (navigator.share) {
          try {
            await navigator.share(data);
            core.close('shareModalLayer');
            this.showFeedback('Thanks for sharing!');
          } catch (err) {
            if (err.name !== 'AbortError') {
              console.error('Share failed:', err);
              if (errorEl) errorEl.textContent = labels.shareOpenFailed;
            }
          }
        } else {
          await this.copyAll();
        }
      },

      async copyLink() {
        const { url } = this.getShareData();
        await this.copyToClipboard(url, 'Link copied!');
      },

      async copyAll() {
        const { text, url } = this.getShareData();
        const content = text ? `${text}\n\n${url}` : url;
        await this.copyToClipboard(content, 'Copied to clipboard!');
      },

      async copyToClipboard(content, successMsg) {
        const errorEl = document.getElementById('shareError');

        if (errorEl) errorEl.textContent = '';
        
        try {
          await navigator.clipboard.writeText(content);
          core.close('shareModalLayer');
          this.showFeedback(successMsg);
        } catch (err) {
          if (errorEl) errorEl.textContent = labels.copyFailedManual;
        }
      },

      showFeedback(msg) {
        // Dispatch custom event for toast notification
        window.dispatchEvent(new CustomEvent('app:notify', { 
          detail: { message: msg, type: 'success' } 
        }));
      }
    },

    /**
     * 4. Support/Purchase Modal
     */
    support: {
      _resendTimer: null,
      _tokenWatcher: null,
      _turnstilePrewarmTriggered: false,
      _lastDisabledPlanEventAt: 0,

      openJoinResearchModal(surface = 'hero_unlock_button') {
        try {
          state.unlock.uiSurface = surface;
          this.reset();
          core.open('supportModalLayer');
          return true;
        } catch (err) {
          console.error('join_research_modal_open_failed', err);
          utils.notify('Could not open Join Research right now. Please try again.', 'error');
          return false;
        }
      },

      emitAnalyticsEvent(eventName, payload) {
        window.dispatchEvent(new CustomEvent('analytics:event', {
          detail: { event_name: eventName, ...payload }
        }));
      },

      clearPendingCheckoutContext() {
        sessionStorage.removeItem('last_payment_intent_id');
        sessionStorage.removeItem('pending_checkout_email');
        sessionStorage.removeItem('pending_checkout_started_at');
      },

      init() {
        // Check for success redirect parameter on page load 
        const urlParams = new URLSearchParams(window.location.search);
        const paymentState = urlParams.get('payment');

        const btn = document.getElementById('supportBtn');
        const proceedBtn = document.getElementById('proceedToPaymentBtn');
        const cancelBtn = document.getElementById('cancelPaymentBtn');
        const resendBtn = document.getElementById('resendLinkBtn');
        const planGrid = document.getElementById('planGrid');

        btn?.addEventListener('click', () => {
          const surface = AccessState.get().demandAllowed ? 'hero_unlock_button' : 'demand_level_page';
          this.openJoinResearchModal(surface);
        });

        const supportModal = document.getElementById('supportModalLayer');
        supportModal?.addEventListener('modal:close', () => {
          this.reset();
        });

        // Plan selection
        planGrid?.addEventListener('pointerdown', (event) => {
          const disabledCard = event.target?.closest?.('.plan-card:disabled');
          if (!disabledCard) return;
          const now = Date.now();
          if (now - this._lastDisabledPlanEventAt < 250) return;
          this._lastDisabledPlanEventAt = now;
          this.emitAnalyticsEvent('disabled_access_level_clicked', {
            ui_surface: 'join_research_access_modal',
            access_level: '72_hour_preview',
            reason: 'disabled_not_available_in_simulated_flow'
          });
        });

        planGrid?.querySelectorAll('[data-plan]').forEach(card => {
          card.addEventListener('click', () => {
            if (card.disabled || card.getAttribute('aria-disabled') === 'true') {
              return;
            }
            planGrid.querySelectorAll('[data-plan]').forEach(el => {
              el.classList.remove('active');
              el.setAttribute('aria-checked', 'false');
            });
            card.classList.add('active');
            card.setAttribute('aria-checked', 'true');
            state.unlock.plan = card.dataset.plan;
          });
        });

        proceedBtn?.addEventListener('click', () => this.proceedToPayment());
        cancelBtn?.addEventListener('click', () => this.reset());
        resendBtn?.addEventListener('click', () => this.resendLink());
        this.resumeResendCooldown();
        this.syncResendButtonState();
        if (!this._tokenWatcher) {
          this._tokenWatcher = setInterval(() => this.syncResendButtonState(), 1000);
        }
        document.addEventListener('visibilitychange', () => {
          if (document.hidden) return;
          const msLeft = state.unlock.resendCooldownUntil - Date.now();
          if (msLeft > 0 && msLeft <= 5000 && !this._turnstilePrewarmTriggered && window.turnstile) {
            turnstile.reset();
            this._turnstilePrewarmTriggered = true;
          }
          this.syncResendButtonState();
        });

        const pendingIntentId = sessionStorage.getItem('last_payment_intent_id');
        const pendingEmail = sessionStorage.getItem('pending_checkout_email');
        if (paymentState === 'cancel') {
          this.clearPendingCheckoutContext();
          sessionStorage.removeItem('resend_cooldown_until');
          return;
        }
        if ((paymentState === 'success' || pendingIntentId) && pendingEmail) {
          const emailInput = document.getElementById('purchaseEmail');
          if (emailInput) emailInput.value = pendingEmail;
          
          if (paymentState === 'success') {
            // If payment succeeded (including test account), show "Check Email" screen (Step 3)
            this.showStep(3);
            const msg = document.getElementById('resendMessage');
            if (msg) msg.textContent = `Check ${pendingEmail} for your access link. It should arrive shortly.`;
          } else {
            // If we just have a pending intent but no success param yet, keep Step 1 open
            this.showStep(1);
          }
          
          core.open('supportModalLayer');
          const existingCooldown = Number(sessionStorage.getItem('resend_cooldown_until') || '0');
          if (existingCooldown <= Date.now()) {
            this.startResendCooldown(180);
          }
        }
      },

      syncResendButtonState() {
        const resendBtn = document.getElementById('resendLinkBtn');
        const hintEl = document.getElementById('resendTurnstileHint');
        if (!resendBtn) return;

        const baseText = resendBtn.dataset.baseText || resendBtn.textContent.trim() || 'Resend Access Link';
        resendBtn.dataset.baseText = baseText;

        const msLeft = state.unlock.resendCooldownUntil - Date.now();
        if (msLeft > 0) return;

        const token = document.querySelector('[name="cf-turnstile-response"]')?.value;
        const hasFreshToken = !!token && token !== state.unlock.lastTurnstileToken;
        resendBtn.disabled = !hasFreshToken;
        resendBtn.textContent = baseText;
        if (hintEl) {
          hintEl.textContent = hasFreshToken
            ? 'Security check complete. You can resend now.'
            : 'Complete a fresh security check to enable resend.';
        }
      },

      startResendCooldown(seconds = 180) {
        const resendBtn = document.getElementById('resendLinkBtn');
        if (!resendBtn) return;
        this._turnstilePrewarmTriggered = false;

        const baseText = resendBtn.dataset.baseText || resendBtn.textContent.trim() || 'Resend Access Link';
        resendBtn.dataset.baseText = baseText;
        const cooldownUntil = Date.now() + (seconds * 1000);
        state.unlock.resendCooldownUntil = cooldownUntil;
        sessionStorage.setItem('resend_cooldown_until', String(cooldownUntil));

        if (this._resendTimer) clearInterval(this._resendTimer);

        const tick = () => {
          const msLeft = state.unlock.resendCooldownUntil - Date.now();
          if (msLeft <= 0) {
            state.unlock.resendCooldownUntil = 0;
            sessionStorage.removeItem('resend_cooldown_until');
            if (this._resendTimer) {
              clearInterval(this._resendTimer);
              this._resendTimer = null;
            }
            this.syncResendButtonState();
            return;
          }
          const secondsLeft = Math.ceil(msLeft / 1000);
          if (secondsLeft <= 5 && !this._turnstilePrewarmTriggered && window.turnstile) {
            turnstile.reset();
            this._turnstilePrewarmTriggered = true;
          }
          resendBtn.disabled = true;
          resendBtn.textContent = `Resend Access Link (${secondsLeft}s)`;
        };

        tick();
        this._resendTimer = setInterval(tick, 1000);
      },

      resumeResendCooldown() {
        const stored = Number(sessionStorage.getItem('resend_cooldown_until') || '0');
        if (stored > Date.now()) {
          const secondsLeft = Math.ceil((stored - Date.now()) / 1000);
          this.startResendCooldown(secondsLeft);
        }
      },

      handleSuccessfulLogin() {
        window.location.replace(`${window.location.pathname}?activated=1`);
      },

      formatSupportError(err, fallback = 'Research access setup failed. Please try again.') {
        const rawMessage = (err?.message || '').trim();
        const message = rawMessage || fallback;
        const errorId = err?.errorId || err?.payload?.error_id || err?.payload?.detail?.error_id;
        if (!errorId || /error id/i.test(message)) return message;
        return `${message} (Error ID: ${errorId})`;
      },

      async proceedToPayment() {
        console.log("DEBUG: proceedToPayment() started");
        const emailInput = document.getElementById('purchaseEmail');
        const errorEl = document.getElementById('purchaseEmailError');
        const proceedBtn = document.getElementById('proceedToPaymentBtn');
        const email = emailInput.value.trim().toLowerCase(); // Always lower-case! 
        if (state.unlock.checkoutSubmitting) {
          console.warn("DEBUG: proceedToPayment() already submitting");
          return;
        }
        
        // Turnstile token extraction
        const turnstileToken = document.querySelector('[name="cf-turnstile-response"]')?.value;

        if (!utils.isValidEmail(email)) {
          console.warn("DEBUG: Invalid email entered:", email);
          errorEl.textContent = labels.emailInvalid;
          return;
        }
        if (!turnstileToken) {
          console.warn("DEBUG: Turnstile token missing");
          errorEl.textContent = labels.securityCheckRequired;
          return;
        }

        console.log("DEBUG: Attempting to create unlock intent for:", email);
        errorEl.textContent = '';
        state.unlock.email = email;
        state.unlock.checkoutSubmitting = true;
        if (proceedBtn) {
          utils.setButtonLoading(proceedBtn, true);
          const btnText = proceedBtn.querySelector('.btn-text');
          if (btnText) btnText.textContent = labels.redirectingResearchAccess;
        }
        this.showStep(2); // Show processing spinner 

        try {
          const plan = 'sim_1_day';
          state.unlock.plan = plan;
          const currentLocationText = document.getElementById('locationInput')?.value?.trim() || '';
          localStorage.setItem('dd_resume_state', JSON.stringify({
            lat: state.coords.lat,
            lng: state.coords.lng,
            text: currentLocationText
          }));
          const data = await utils.apiPost('/api/billing/unlock-intent', {
            email,
            plan,
            turnstile_token: turnstileToken,
            ui_surface: state.unlock.uiSurface || 'hero_unlock_button'
          });

          if (!data || data.ok !== true) {
            const message = data?.message || 'Research access is currently unavailable. Please try again later.';
            throw new Error(message);
          }

          if (data.intent_id) {
            sessionStorage.setItem('last_payment_intent_id', data.intent_id);
          }

          if (data.ok === true && data.status === 'intent_created') {
            // The DB intent was created, but the email timed out/failed.
            state.unlock.checkoutSubmitting = false;

            if (proceedBtn) {
              utils.setButtonLoading(proceedBtn, false);
              const btnText = proceedBtn.querySelector('.btn-text');
              if (btnText) btnText.textContent = 'Join Research ➔';
            }

            this.showStep(3);
            const resendMsg = document.getElementById('resendMessage');
            if (resendMsg) {
              resendMsg.textContent = `Request saved for ${email}. Check your inbox shortly, then use Resend Access Link if needed.`;
            }
            if (errorEl) errorEl.textContent = '';
            this.syncResendButtonState();
            if (window.turnstile) turnstile.reset();
            return; // Stop execution so we DO NOT redirect
          }

          console.log("DEBUG: Intent created successfully:", data);
          sessionStorage.setItem('pending_checkout_email', email);
          sessionStorage.setItem('pending_checkout_started_at', String(Date.now()));
          state.unlock.lastTurnstileToken = turnstileToken;

          if (data.checkout_url) {
            console.log("DEBUG: Redirecting to checkout_url:", data.checkout_url);
            window.location.href = data.checkout_url;
            return;
          }
          if (data.ok === true && data.status === 'magic_link_sent') {
            state.unlock.checkoutSubmitting = false;
            if (proceedBtn) {
              utils.setButtonLoading(proceedBtn, false);
              const btnText = proceedBtn.querySelector('.btn-text');
              if (btnText) btnText.textContent = labels.joinResearchCta;
            }
            this.showStep(3);
            const resendMsg = document.getElementById('resendMessage');
            if (resendMsg) {
              resendMsg.textContent = `Check ${email} for your access link.`;
            }
            this.syncResendButtonState();
            if (window.turnstile) turnstile.reset();
            return;
          }
          throw new Error(data?.message || 'Research access is currently unavailable. Please try again later.');
        } catch (err) {
          console.error("DEBUG: proceedToPayment() error:", err);
          state.unlock.checkoutSubmitting = false;
          if (proceedBtn) {
            utils.setButtonLoading(proceedBtn, false);
            const btnText = proceedBtn.querySelector('.btn-text');
            if (btnText) btnText.textContent = labels.joinResearchCta;
          }
          this.showStep(1);
          errorEl.textContent = err?.message || this.formatSupportError(err);
          // Reset Turnstile on failure 
          if (window.turnstile) turnstile.reset();
          this.syncResendButtonState();
        }
      },

      async resendLink() {
        const emailInput = document.getElementById('purchaseEmail');
        const errorEl = document.getElementById('purchaseEmailError');
        const email = emailInput.value.trim().toLowerCase();
        const turnstileToken = document.querySelector('[name="cf-turnstile-response"]')?.value;

        if (!utils.isValidEmail(email)) {
          errorEl.textContent = labels.resendEnterEmail;
          emailInput.focus();
          return;
        }
        if (!turnstileToken || turnstileToken === state.unlock.lastTurnstileToken) {
          errorEl.textContent = labels.resendFreshSecurity;
          if (window.turnstile) turnstile.reset();
          this.syncResendButtonState();
          return;
        }

        utils.setButtonLoading(document.getElementById('resendLinkBtn'), true);
        errorEl.textContent = '';

        try {
          await utils.apiPost('/api/auth/magic-link', {
            email,
            turnstile_token: turnstileToken,
            intent_id: sessionStorage.getItem('last_payment_intent_id') || null
          });
          
          this.showStep(3); // Show Success Check-Email screen 
          document.getElementById('resendMessage').textContent = 
            `If ${email} has an active pass, we've sent a new access link.`;
          state.unlock.lastTurnstileToken = turnstileToken;
          if (window.turnstile) turnstile.reset();
          this.startResendCooldown(180);
        } catch (err) {
          errorEl.textContent = err.message || 'Could not resend link.';
          if (window.turnstile) turnstile.reset();
        } finally {
          utils.setButtonLoading(document.getElementById('resendLinkBtn'), false);
        }
      },

      showStep(stepNumber) {
        document.querySelectorAll('#supportModalLayer .purchase-step').forEach((el, idx) => {
          el.classList.toggle('hidden', idx + 1 !== stepNumber);
        });
      },

      reset() {
        this.showStep(1);
        state.unlock.checkoutSubmitting = false;
        state.unlock.plan = 'sim_1_day';
        state.unlock.uiSurface = 'hero_unlock_button';
        const planGrid = document.getElementById('planGrid');
        planGrid?.querySelectorAll('[data-plan]').forEach(card => {
          const isDefault = card.dataset.plan === 'sim_1_day';
          card.classList.toggle('active', isDefault);
          card.setAttribute('aria-checked', isDefault ? 'true' : 'false');
        });
        const emailInput = document.getElementById('purchaseEmail');
        if (emailInput) emailInput.value = '';
        const errorEl = document.getElementById('purchaseEmailError');
        if (errorEl) errorEl.textContent = '';
        this.syncResendButtonState();
        const proceedBtn = document.getElementById('proceedToPaymentBtn');
        if (proceedBtn) {
          utils.setButtonLoading(proceedBtn, false);
          const btnText = proceedBtn.querySelector('.btn-text');
          if (btnText) btnText.textContent = labels.joinResearchCta;
        }
      }
    },

    /**
     * 5. About Modal
     */
    about: {
      init() {
        const btn = document.getElementById('aboutBtn');
        btn?.addEventListener('click', () => {
          core.open('aboutModalLayer');
        });
      }
    },

    /**
     * 6. Language Selection Modal (Bottom Sheet)
     */
    language: {
      init() {
        const btn = document.getElementById('langOpenBtn');
        const list = document.getElementById('langList');

        btn?.addEventListener('click', () => {
          core.open('langModalLayer');
        });

        // Language selection - One-click save
        list?.querySelectorAll('.lang-item').forEach(item => {
          item.addEventListener('click', () => {
            const langCode = item.dataset.lang;
            if (langCode === state.language.current) {
              core.close('langModalLayer');
              return;
            }
            this.save(langCode, item);
          });
        });

        // Swipe to dismiss for bottom sheet
        this.initSwipeToDismiss();
      },

      async save(langCode, itemEl) {
        const errorEl = document.getElementById('langError');
        const checkEl = itemEl.querySelector('.lang-item__check');
        const spinnerEl = itemEl.querySelector('.lang-item__spinner');

        if (errorEl) errorEl.textContent = '';
        itemEl.classList.add('is-loading');
        checkEl?.classList.add('hidden');
        spinnerEl?.classList.remove('hidden');

        try {
          await utils.apiPost('/api/language', { lang: langCode });
          
          state.language.current = langCode;
          document.cookie = `dd_lang=${langCode}; path=/; max-age=31536000; SameSite=Lax`;
          window.location.reload();

        } catch (err) {
          itemEl.classList.remove('is-loading');
          spinnerEl?.classList.add('hidden');
          if (itemEl.classList.contains('active')) {
            checkEl?.classList.remove('hidden');
          }
          if (errorEl) errorEl.textContent = err.message || 'Could not save language preference.';
        }
      },

      initSwipeToDismiss() {
        const modal = document.getElementById('langModalLayer');
        if (!modal) return;

        let startY = 0;
        let currentY = 0;
        let isDragging = false;

        const handle = modal.querySelector('.modal-handle') || modal.querySelector('.modal');

        handle?.addEventListener('touchstart', (e) => {
          startY = e.touches[0].clientY;
          isDragging = true;
          modal.style.transition = 'none';
        }, { passive: true });

        document.addEventListener('touchmove', (e) => {
          if (!isDragging) return;
          currentY = e.touches[0].clientY;
          const delta = currentY - startY;
          
          if (delta > 0) {
            modal.style.transform = `translateY(${delta}px)`;
          }
        }, { passive: true });

        document.addEventListener('touchend', () => {
          if (!isDragging) return;
          isDragging = false;
          modal.style.transition = '';
          
          const delta = currentY - startY;
          if (delta > 100) {
            core.close('langModalLayer');
            modal.style.transform = '';
          } else {
            modal.style.transform = '';
          }
        });
      }
    }
  };

  // ==========================================
  // PUBLIC API
  // ==========================================
  
  return {
    init() {
      core.init();
      Object.values(modals).forEach(m => m.init());
      const access = AccessState.get();
      modals.user.updateStatus(access.tier, access.demandAllowed, access.dailyLimit);
      AccessState.subscribe((nextAccess) => {
        modals.user.updateStatus(nextAccess.tier, nextAccess.demandAllowed, nextAccess.dailyLimit);
      });
    },
    
    // Expose specific methods for external use
    open: core.open.bind(core),
    close: core.close.bind(core),
    closeAll: core.closeAll.bind(core),
    
    // State setters
    setCoords(lat, lng) {
      state.coords.lat = lat;
      state.coords.lng = lng;
      state.coords.valid = true;
      state.coords.key = `${lat.toFixed(4)},${lng.toFixed(4)}`;
      modals.report.syncCoords();
      modals.report.updateSubmitState();
    },
    
    clearCoords() {
      state.coords.lat = null;
      state.coords.lng = null;
      state.coords.valid = false;
      state.coords.key = null;
      modals.report.syncCoords();
      modals.report.updateSubmitState();
    },
    
    updateAccess(tier, demandAllowed, dailyLimit = null) {
      updateAccessState(Boolean(demandAllowed), tier, dailyLimit);
    },

    notify(message, type = 'info') {
      utils.notify(message, type);
    },

    openJoinResearchModal(surface = 'hero_unlock_button') {
      return modals.support.openJoinResearchModal(surface);
    }
  };
})();

// ==========================================
// INITIALIZATION
// ==========================================

// Auto-initialize when DOM is ready
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => ModalSystem.init());
} else {
  ModalSystem.init();
}

// Expose globally for debugging and external access
window.ModalSystem = ModalSystem;
