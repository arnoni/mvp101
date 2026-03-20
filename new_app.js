document.addEventListener("DOMContentLoaded", () => {
  // 1. STATE MACHINE (Single Source of Truth)
  const state = {
    access: { demandAllowed: document.body.dataset.demandAllowed === "true" },
    coords: { lat: null, lng: null, valid: false, key: null },
    construction: { status: "idle", coordKey: null, score: null },
    demand: { status: "idle", coordKey: null, score: null },
    verification: { required: false, passed: false, token: null, widgetId: null },
    modal: { active: null, step: "intent", email: "", plan: "1_day" },
    requests: { construction: null, demand: null },
    debounce: null
  };

  // DOM Elements
  const els = {
    lat: document.getElementById("latInput"),
    lng: document.getElementById("lngInput"),
    err: document.getElementById("coordError"),
    mainBtn: document.getElementById("mainActionBtn"),
    
    conBtn: document.getElementById("constructionGoBtn"),
    conVal: document.getElementById("constructionValue"),
    conMsg: document.getElementById("constructionMessage"),
    conBand: document.getElementById("constructionBand"),
    conNeedle: document.getElementById("constructionNeedle"),
    
    demBtn: document.getElementById("demandGoBtn"),
    demVal: document.getElementById("demandValue"),
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

  // 2. COORDINATE PARSING & HARD RESET
  function handleCoordInput() {
    clearTimeout(state.debounce);
    state.debounce = setTimeout(() => {
      const lat = parseFloat(els.lat.value);
      const lng = parseFloat(els.lng.value);
      
      let valid = !isNaN(lat) && !isNaN(lng) && lat >= -90 && lat <= 90 && lng >= -180 && lng <= 180;
      const newKey = valid ? `${lat.toFixed(6)},${lng.toFixed(6)}` : null;

      // Hard Reset: If coordinates change, invalidate ALL previous data to prevent ghost states
      if (state.coords.key && state.coords.key !== newKey) {
        if (state.requests.construction) state.requests.construction.abort();
        if (state.requests.demand) state.requests.demand.abort();
        
        state.construction = { status: "idle", coordKey: null, score: null };
        state.demand = { status: "idle", coordKey: null, score: null };
        state.verification = { required: false, passed: false, token: null }; 
        
        animateGauge(els.conBand, els.conNeedle, els.conVal, null);
        animateGauge(els.demBand, els.demNeedle, els.demVal, null);
        els.conMsg.textContent = "Coordinates changed";
        els.demMsg.textContent = state.access.demandAllowed ? "Ready to check" : "Paid pass required";
        els.turnstileSlot.classList.add("hidden");
      }

      state.coords = { lat, lng, valid, key: newKey };
      els.err.textContent = valid ? "" : (els.lat.value || els.lng.value ? "Invalid coordinates." : "");
      updateButtons();
    }, 300);
  }

  function updateButtons() {
    const valid = state.coords.valid;
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
      els.conBtn.textContent = "Check";
    }

    els.demBtn.textContent = state.access.demandAllowed ? "Check" : "Unlock";
  }

  // 3. SVG ANIMATION MATH
  function animateGauge(bandEl, needleEl, valEl, score) {
    if (score === null) {
      bandEl.style.strokeDashoffset = 377;
      needleEl.style.transform = `rotate(-82deg)`;
      valEl.textContent = "--";
      return;
    }
    const clamped = Math.max(0, Math.min(100, score));
    const offset = 377 - (377 * (clamped / 100)); // 377 is total arc length
    const angle = -82 + (clamped / 100) * 164; // -82 to +82 degrees

    bandEl.style.strokeDashoffset = offset;
    needleEl.style.transform = `rotate(${angle}deg)`;
    valEl.textContent = clamped;
  }

  // 4. API CALLS WITH ABORT CONTROLLER
  async function fetchConstruction() {
    if (!state.coords.valid) return;
    
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
      const res = await fetch("/api/construction", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lat: state.coords.lat, lng: state.coords.lng, turnstile_token: state.verification.token }),
        signal: state.requests.construction.signal
      });
      const data = await res.json();

      if (data.coord_key !== state.coords.key) return; // Stale

      if (data.verification_required) {
        state.verification.required = true;
        state.construction.status = "blocked";
        els.conMsg.textContent = "Verification required";
        renderTurnstile();
        updateButtons();
        return;
      }

      state.construction = { status: "ready", score: data.score, coordKey: data.coord_key };
      animateGauge(els.conBand, els.conNeedle, els.conVal, data.score);
      els.conMsg.textContent = data.message || "Analysis complete";

    } catch (e) {
      if (e.name !== "AbortError") {
        state.construction.status = "idle";
        els.conMsg.textContent = "Failed to check construction.";
      }
    } finally {
      updateButtons();
    }
  }

  async function fetchDemand() {
    if (!state.coords.valid) return;

    if (!state.access.demandAllowed) {
      Modal.open("unlockModalLayer");
      return;
    }

    if (state.construction.status !== "ready" || state.construction.coordKey !== state.coords.key) {
      els.demMsg.textContent = "Run construction check first";
      return;
    }

    if (state.requests.demand) state.requests.demand.abort();
    state.requests.demand = new AbortController();
    
    state.demand.status = "loading";
    els.demMsg.textContent = "Checking demand...";
    updateButtons();

    try {
      const res = await fetch("/api/demand", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ lat: state.coords.lat, lng: state.coords.lng, construction_coord_key: state.construction.coordKey }),
        signal: state.requests.demand.signal
      });
      const data = await res.json();

      if (data.coord_key !== state.coords.key) return; // Stale

      state.demand = { status: "ready", score: data.score, coordKey: data.coord_key };
      animateGauge(els.demBand, els.demNeedle, els.demVal, data.score);
      els.demMsg.textContent = data.message || "Demand analyzed";

    } catch (e) {
      if (e.name !== "AbortError") {
        state.demand.status = "idle";
        els.demMsg.textContent = "Failed to check demand.";
      }
    } finally {
      updateButtons();
    }
  }

  function renderTurnstile() {
    els.turnstileSlot.classList.remove("hidden");
    if (!window.turnstile || state.verification.widgetId) return;
    state.verification.widgetId = window.turnstile.render('#turnstileContainer', {
      sitekey: document.body.dataset.turnstileSitekey,
      callback: (token) => {
        state.verification.passed = true;
        state.verification.token = token;
        els.turnstileSlot.classList.add("hidden");
        fetchConstruction(); // Auto-retry
      }
    });
  }

  // 5. MODAL MULTI-STEP MANAGER
  const Modal = {
    trap: null,
    open(id) {
      const layer = document.getElementById(id);
      layer.classList.add("open");
      layer.setAttribute("aria-hidden", "false");
      document.body.style.overflow = "hidden";
      state.modal.active = id;
      this.setStep("stepIntent"); // Always reset to intent

      // Focus Trap Loop for A11y
      const focusables = layer.querySelectorAll('button:not([disabled]), [href], input:not([disabled]), [tabindex]:not([tabindex="-1"])');
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      
      this.trap = (e) => {
        if (e.key === "Escape") this.close(id);
        if (e.key !== "Tab" || focusables.length === 0) return;
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      };
      
      document.addEventListener("keydown", this.trap);
      setTimeout(() => first?.focus(), 100);
    },
    close(id) {
      const layer = document.getElementById(id);
      layer.classList.remove("open");
      layer.setAttribute("aria-hidden", "true");
      document.body.style.overflow = "";
      state.modal.active = null;
      document.removeEventListener("keydown", this.trap);
    },
    setStep(stepId) {
      document.querySelectorAll(".modal-step").forEach(el => el.classList.add("hidden"));
      document.getElementById(stepId).classList.remove("hidden");
    }
  };

  async function initiateCheckout() {
    const email = els.unlockEmail.value.trim();
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      els.unlockEmailErr.textContent = "Valid email required.";
      return;
    }
    els.unlockEmailErr.textContent = "";
    els.continuePaymentBtn.disabled = true;
    els.continuePaymentBtn.textContent = "Preparing...";

    try {
      const res = await fetch("/api/billing/unlock_intent", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: email, plan: state.modal.plan })
      });
      const data = await res.json();
      
      // Open Stripe/Payment provider in new tab
      window.open(data.checkout_url, "_blank");
      
      // Update UI to awaiting state
      els.awaitingEmailDisplay.textContent = email;
      Modal.setStep("stepAwaiting");

      // Optional: Poll backend for status here, or just let them use the email
      setTimeout(() => Modal.setStep("stepSuccess"), 15000); // Mock success after 15s

    } catch (e) {
      els.unlockEmailErr.textContent = "Failed to initiate checkout.";
    } finally {
      els.continuePaymentBtn.disabled = false;
      els.continuePaymentBtn.textContent = "Continue to Payment";
    }
  }

  // Event Listeners Binding
  els.lat.addEventListener("input", handleCoordInput);
  els.lng.addEventListener("input", handleCoordInput);
  
  document.getElementById("coordForm").addEventListener("submit", (e) => { e.preventDefault(); fetchConstruction(); });
  els.conBtn.addEventListener("click", fetchConstruction);
  els.demBtn.addEventListener("click", fetchDemand);
  
  document.getElementById("unlockTriggerBtn").addEventListener("click", () => Modal.open("unlockModalLayer"));
  document.querySelectorAll("[data-close]").forEach(b => b.addEventListener("click", () => Modal.close(b.dataset.close)));
  
  // Chip selection
  document.querySelectorAll(".chip").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".chip").forEach(c => c.classList.remove("active"));
      btn.classList.add("active");
      state.modal.plan = btn.dataset.plan;
    });
  });

  els.continuePaymentBtn.addEventListener("click", initiateCheckout);
});