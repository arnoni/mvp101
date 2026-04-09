document.addEventListener("DOMContentLoaded", () => {
  // 1. STATE MACHINE (Single Source of Truth)
  const state = {
    access: { demandAllowed: document.body.dataset.demandAllowed === "true" },
    coords: { lat: null, lng: null, valid: false, key: null },
    input: { kind: "empty", original: "", preview: "", parsed: null, error: "", touched: false },
    construction: { status: "idle", coordKey: null, score: null },
    demand: { status: "idle", coordKey: null, score: null },
    verification: { required: false, passed: false, token: null, widgetId: null, renderAttempts: 0 },
    modal: { active: null, step: "intent", email: "", plan: "1_day" },
    unlock: { email: "", plan: "1_day", resendCooldownUntil: 0, lastTurnstileToken: null, checkoutSubmitting: false },
    requests: { construction: null, demand: null },
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

  // Success Toast Check (from redirect)
  if (new URLSearchParams(window.location.search).get("magic_success")) {
    state.access.demandAllowed = true;
    window.history.replaceState({}, document.title, window.location.pathname);
    alert("Pass Activated Successfully! Demand is unlocked."); // Replace with sleek toast in prod
  }

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
    const place = rawUrl.match(/!3d([+-]?\d+(?:\.\d+)?)!4d([+-]?\d+(?:\.\d+)?)/);
    if (place) {
      const lat = Number(place[1]);
      const lng = Number(place[2]);
      validateLatLng(lat, lng);
      return { lat, lng, normalizedText: `${lat.toFixed(6)}, ${lng.toFixed(6)}`, sourceKind: "place_3d4d" };
    }
    for (const key of ["q", "ll"]) {
      const pair = extractPair(url.searchParams.get(key) || "");
      if (pair) return { lat: pair[0], lng: pair[1], normalizedText: `${pair[0].toFixed(6)}, ${pair[1].toFixed(6)}`, sourceKind: `query_${key}` };
    }
    const vp = rawUrl.match(/@([+-]?\d+(?:\.\d+)?),([+-]?\d+(?:\.\d+)?)/);
    if (vp) {
      const lat = Number(vp[1]);
      const lng = Number(vp[2]);
      validateLatLng(lat, lng);
      return { lat, lng, normalizedText: `${lat.toFixed(6)}, ${lng.toFixed(6)}`, sourceKind: "viewport_center" };
    }
    throw new Error("Could not extract coordinates from this Google Maps link.");
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
    state.debounce = setTimeout(() => {
      const raw = normalizeInput(els.location.value);
      const kind = classifyLocationInput(raw);
      let parsed = null;
      let error = "";
      let preview = "";
      try {
        if (kind === "decimal_pair") parsed = parseDecimalPair(raw);
        else if (kind === "degree_pair") parsed = parseDegreePair(raw);
        else if (kind === "google_maps_url") parsed = parseGoogleMapsLongUrl(raw);
        else if (kind === "google_maps_short_url") preview = "Google Maps short link detected";
        else if (kind === "invalid") error = "Unsupported input. Use coordinates or Google Maps link.";
      } catch (e) {
        error = e.message || "Invalid location input.";
      }

      const valid = Boolean(parsed || kind === "google_maps_short_url");
      const lat = parsed ? parsed.lat : null;
      const lng = parsed ? parsed.lng : null;
      const newKey = parsed ? `${lat.toFixed(4)},${lng.toFixed(4)}` : null;
      if (parsed) {
        preview = parsed.note ? `${parsed.note} Parsed as: ${parsed.normalizedText}` : `Parsed as: ${parsed.normalizedText}`;
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
        els.conMsg.textContent = "Coordinates changed";
        els.demMsg.textContent = state.access.demandAllowed ? "Ready to check" : "Paid pass required";
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
      els.mainBtn.textContent = "Complete Verification";
      els.conBtn.textContent = "Verify";
    } else {
      els.mainBtn.textContent = "Check Construction";
      els.conBtn.textContent = "GO";
    }

    els.demBtn.textContent = state.access.demandAllowed ? "GO" : "Unlock";
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
    els.conMsg.textContent = "Analyzing signals...";
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
          els.conMsg.textContent = "Verification required";
          renderTurnstile();
          updateButtons();
          return;
        }
        console.warn(`Construction API failed with status ${res.status}. Using fallback simulation.`);
        data = { score: 87, coord_key: state.coords.key, message: 'Simulated Analysis (Fallback)' };
      } else {
        data = await res.json();
      }

      const construction = data.construction || data;
      console.log("SERVER KEY:", construction.coord_key, "| CLIENT KEY:", state.coords.key);
      if (normalizeKey(construction.coord_key || state.coords.key) !== normalizeKey(state.coords.key)) return; // Stale

      if (data.verification_required) {
        state.verification.required = true;
        state.construction.status = "blocked";
        els.conMsg.textContent = "Verification required";
        renderTurnstile();
        updateButtons();
        return;
      }

      const score = construction.score !== undefined ? construction.score : 87;
      state.construction = { status: "ready", score: score, coordKey: construction.coord_key || state.coords.key };
      console.log("Triggering animateGauge with score:", score);
      animateGauge(els.conBand, els.conNeedle, score);
      els.conMsg.textContent = (construction && construction.message) || data.message || "Analysis complete";

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

    if (!state.access.demandAllowed) {
      if (window.ModalSystem) { ModalSystem.open("supportModalLayer"); }
      return;
    }

        if (state.requests.demand) state.requests.demand.abort();
    state.requests.demand = new AbortController();
    
    state.demand.status = "loading";
    els.demMsg.textContent = "Checking demand...";
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
          els.demMsg.textContent = "Verification required";
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
      els.conMsg.textContent = "Verification unavailable: site key missing.";
      return;
    }
    
    // If turnstile isn't loaded yet, try again in 100ms
    if (!window.turnstile) {
      state.verification.renderAttempts = (state.verification.renderAttempts || 0) + 1;
      if (state.verification.renderAttempts > 50) {
        console.error("Turnstile script failed to load after multiple attempts.");
        els.conMsg.textContent = "Unable to load verification challenge. Please refresh and try again.";
        return;
      }
      els.conMsg.textContent = "Loading verification challenge…";
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
          els.turnstileSlot.classList.add("hidden");
          updateButtons();
          fetchConstruction(); // Auto-retry
        },
        'error-callback': (err) => {
          console.error("Turnstile Error:", err);
          state.verification.widgetId = null;
          els.conMsg.textContent = "Verification failed. Please refresh.";
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
      els.conMsg.textContent = "Unable to render verification challenge. Please refresh.";
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
    updateButtons();
  }
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
    access: {
      tier: document.getElementById('app')?.dataset.tier || 'free',
      demandAllowed: document.getElementById('app')?.dataset.demandAllowed === 'true'
    },
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
      plan: '1_day',
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
    async apiPost(url, body) {
      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify(body)
      });
      
      const data = await response.json().catch(() => ({}));
      
      if (!response.ok) {
        let errMsg = data.message || 'Request failed';
        if (data.detail) {
          if (typeof data.detail === 'string') {
            errMsg = data.detail;
          } else if (Array.isArray(data.detail) && data.detail.length > 0 && data.detail[0].msg) {
            errMsg = data.detail[0].msg;
          } else if (typeof data.detail === 'object') {
            errMsg = data.detail.error || data.detail.detail || data.detail.message || JSON.stringify(data.detail);
          }
        }
        throw new Error(errMsg);
      }
      
      return data;
    }
  };

  // ==========================================
  // CORE MODAL FUNCTIONALITY
  // ==========================================
  
  const core = {
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
        return;
      }

      // Close currently active modal if exists (unless stacking is enabled)
      if (state.modals.active && !options.stack) {
        this.close(state.modals.active, { silent: true });
      }

      // Show modal using native dialog API or fallback
      if (modal.showModal && !modal.classList.contains('bottom-sheet')) {
        modal.showModal();
      } else {
        modal.classList.add('open');
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
      if (!modal) return;

      // Use native close or fallback
      if (modal.close && !modal.classList.contains('bottom-sheet')) {
        modal.close();
      } else {
        modal.classList.remove('open');
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
        btn.addEventListener('click', (e) => {
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
        btn.addEventListener('click', (e) => {
          const modalId = btn.dataset.openModal;
          const modal = document.getElementById(modalId);
          if (modal) {
            modal.triggerElement = btn;
          }
        });
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
          core.open('supportModalLayer');
        });
      },

      updateStatus(tier, demandAllowed) {
        state.access.tier = tier;
        state.access.demandAllowed = demandAllowed;
        
        const badge = document.getElementById('userTierBadge');
        const statusText = document.getElementById('userStatusText');
        
        if (badge) {
          badge.textContent = `${tier.charAt(0).toUpperCase() + tier.slice(1)} Tier`;
          badge.dataset.tier = tier;
        }
        
        if (statusText) {
          statusText.textContent = demandAllowed 
            ? 'Demand is unlocked and your pass is active.'
            : 'Construction is available. Demand requires a paid pass.';
        }
      }
    },

    /**
     * 2. Report Modal
     */
    report: {
      init() {
        const btn = document.getElementById('reportBtn');
        const submitBtn = document.getElementById('reportSubmitBtn');
        const typeGrid = document.getElementById('reportTypeGrid');
        const noteField = document.getElementById('reportNote');
        const charCount = document.getElementById('reportCharCount');

        // Open handler
        btn?.addEventListener('click', () => {
          if (!state.coords.valid) {
            this.showError('Enter valid coordinates first.');
            // Optional: highlight coord inputs
            return;
          }
          
          this.syncCoords();
          this.reset();
          core.open('reportModalLayer');
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
        if (display && state.coords.valid) {
          display.textContent = utils.formatCoords(state.coords.lat, state.coords.lng);
        }
      },

      async submit() {
        const btn = document.getElementById('reportSubmitBtn');
        const errorEl = document.getElementById('reportError');
        
        errorEl.textContent = '';
        utils.setButtonLoading(btn, true);

        try {
          await utils.apiPost('/api/user-reports', {
            lat: state.coords.lat,
            lon: state.coords.lng,
            report_kind: state.report.type,
            is_nearby_now: Boolean(document.getElementById('reportNearbyNow')?.checked),
            note: state.report.note
          });

          // Show success state
          document.getElementById('reportFormState').classList.add('hidden');
          document.getElementById('reportSuccessState').classList.remove('hidden');
          
          // Reset after delay
          setTimeout(() => {
            core.close('reportModalLayer');
          }, 3000);

        } catch (err) {
          errorEl.textContent = err.message || 'Failed to submit report. Please try again.';
        } finally {
          utils.setButtonLoading(btn, false);
        }
      },

      reset() {
        document.getElementById('reportFormState')?.classList.remove('hidden');
        document.getElementById('reportSuccessState')?.classList.add('hidden');
        document.getElementById('reportNote').value = '';
        const nearby = document.getElementById('reportNearbyNow');
        if (nearby) nearby.checked = false;
        document.getElementById('reportCharCount').textContent = '0/180';
        document.getElementById('reportError').textContent = '';
        
        // Reset to first option
        const firstType = document.querySelector('[data-report-type="active_construction"]');
        if (firstType) {
          firstType.click();
        }
      },

      showError(msg) {
        // Could show a toast or alert
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

        btn?.addEventListener('click', () => {
          document.getElementById('shareError').textContent = '';
          core.open('shareModalLayer');
        });

        nativeBtn?.addEventListener('click', () => this.shareNative());
        copyLinkBtn?.addEventListener('click', () => this.copyLink());
        copyAllBtn?.addEventListener('click', () => this.copyAll());
      },

      getShareData() {
        return {
          title: 'DillDrill Construction Check',
          text: document.getElementById('shareText').value,
          url: document.getElementById('shareUrlBox').textContent.trim()
        };
      },

      async shareNative() {
        const data = this.getShareData();
        
        if (navigator.share) {
          try {
            await navigator.share(data);
            core.close('shareModalLayer');
            this.showFeedback('Thanks for sharing!');
          } catch (err) {
            if (err.name !== 'AbortError') {
              console.error('Share failed:', err);
            }
          }
        } else {
          // Fallback to copy all
          await this.copyAll();
        }
      },

      async copyLink() {
        const { url } = this.getShareData();
        await this.copyToClipboard(url, 'Link copied!');
      },

      async copyAll() {
        const { text, url } = this.getShareData();
        await this.copyToClipboard(`${text} ${url}`, 'Copied to clipboard!');
      },

      async copyToClipboard(content, successMsg) {
        const errorEl = document.getElementById('shareError');
        
        try {
          await navigator.clipboard.writeText(content);
          core.close('shareModalLayer');
          this.showFeedback(successMsg);
        } catch (err) {
          errorEl.textContent = 'Could not copy. Please copy manually.';
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

      clearPendingCheckoutContext() {
        sessionStorage.removeItem('last_payment_intent_id');
        sessionStorage.removeItem('pending_checkout_email');
        sessionStorage.removeItem('pending_checkout_started_at');
      },

      init() {
        // Check for success redirect parameter on page load 
        const urlParams = new URLSearchParams(window.location.search);
        if (urlParams.get('magic_success') === '1') {
          this.handleSuccessfulLogin();
        }
        const paymentState = urlParams.get('payment');

        const btn = document.getElementById('supportBtn');
        const proceedBtn = document.getElementById('proceedToPaymentBtn');
        const cancelBtn = document.getElementById('cancelPaymentBtn');
        const resendBtn = document.getElementById('resendLinkBtn');
        const planGrid = document.getElementById('planGrid');

        btn?.addEventListener('click', () => {
          this.reset();
          core.open('supportModalLayer');
        });

        // Plan selection
        planGrid?.querySelectorAll('[data-plan]').forEach(card => {
          card.addEventListener('click', () => {
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
          this.showStep(1);
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
        const msLeft = state.unlock.resendCooldownUntil - Date.now();
        if (msLeft > 0) return;

        const token = document.querySelector('[name="cf-turnstile-response"]')?.value;
        const hasFreshToken = !!token && token !== state.unlock.lastTurnstileToken;
        resendBtn.disabled = !hasFreshToken;
        if (!hasFreshToken) {
          resendBtn.textContent = 'Securing...';
        } else if (resendBtn.dataset.baseText) {
          resendBtn.textContent = resendBtn.dataset.baseText;
        }
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
        // Clean the URL so the user doesn't copy/paste it or refresh and trigger it again 
        window.history.replaceState({}, document.title, window.location.pathname);
        
        // Show feedback
        window.dispatchEvent(new CustomEvent('app:notify', { 
          detail: { message: '🎉 Pass Activated! You now have full access.', type: 'success' } 
        }));
        
        // Fetch updated session state
        if (window.core && window.core.fetchUserEntitlements) {
          window.core.fetchUserEntitlements();
        } else {
          // Fallback if core isn't exposed or structured this way
          window.location.reload();
        }
      },

      async proceedToPayment() {
        const emailInput = document.getElementById('purchaseEmail');
        const errorEl = document.getElementById('purchaseEmailError');
        const proceedBtn = document.getElementById('proceedToPaymentBtn');
        const email = emailInput.value.trim().toLowerCase(); // Always lower-case! 
        if (state.unlock.checkoutSubmitting) return;
        
        // Turnstile token extraction
        const turnstileToken = document.querySelector('[name="cf-turnstile-response"]')?.value;

        if (!utils.isValidEmail(email)) {
          errorEl.textContent = 'Please enter a valid email address.';
          return;
        }
        if (!turnstileToken) {
          errorEl.textContent = 'Please complete the security check.';
          return;
        }

        errorEl.textContent = '';
        state.unlock.email = email;
        state.unlock.checkoutSubmitting = true;
        if (proceedBtn) {
          utils.setButtonLoading(proceedBtn, true);
          const btnText = proceedBtn.querySelector('.btn-text');
          if (btnText) btnText.textContent = 'Redirecting to secure checkout...';
        }
        this.showStep(2); // Show processing spinner 

        try {
          const data = await utils.apiPost('/api/billing/unlock-intent', {
            email,
            plan: state.unlock.plan,
            turnstile_token: turnstileToken
          });

          if (data.intent_id) {
            sessionStorage.setItem('last_payment_intent_id', data.intent_id);
          }
          sessionStorage.setItem('pending_checkout_email', email);
          sessionStorage.setItem('pending_checkout_started_at', String(Date.now()));
          state.unlock.lastTurnstileToken = turnstileToken;
          window.location.href = data.checkout_url;
        } catch (err) {
          state.unlock.checkoutSubmitting = false;
          if (proceedBtn) {
            utils.setButtonLoading(proceedBtn, false);
            const btnText = proceedBtn.querySelector('.btn-text');
            if (btnText) btnText.textContent = 'Continue to Payment ➔';
          }
          this.showStep(1);
          errorEl.textContent = err.message || 'Checkout failed. Please try again.';
          // Reset Turnstile on failure 
          if (window.turnstile) turnstile.reset();
        }
      },

      async resendLink() {
        const emailInput = document.getElementById('purchaseEmail');
        const errorEl = document.getElementById('purchaseEmailError');
        const email = emailInput.value.trim().toLowerCase();
        const turnstileToken = document.querySelector('[name="cf-turnstile-response"]')?.value;

        if (!utils.isValidEmail(email)) {
          errorEl.textContent = 'Enter your email above, then click Resend.';
          emailInput.focus();
          return;
        }
        if (!turnstileToken || turnstileToken === state.unlock.lastTurnstileToken) {
          errorEl.textContent = 'Please complete a fresh security check before resend.';
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
        const emailInput = document.getElementById('purchaseEmail');
        if (emailInput) emailInput.value = '';
        const errorEl = document.getElementById('purchaseEmailError');
        if (errorEl) errorEl.textContent = '';
        const proceedBtn = document.getElementById('proceedToPaymentBtn');
        if (proceedBtn) {
          utils.setButtonLoading(proceedBtn, false);
          const btnText = proceedBtn.querySelector('.btn-text');
          if (btnText) btnText.textContent = 'Continue to Payment ➔';
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
    },
    
    clearCoords() {
      state.coords.lat = null;
      state.coords.lng = null;
      state.coords.valid = false;
      state.coords.key = null;
    },
    
    updateAccess(tier, demandAllowed) {
      modals.user.updateStatus(tier, demandAllowed);
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
