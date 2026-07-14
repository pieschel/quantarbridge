"use strict";

const state = {
  status: null,
  session: null,
  settings: null,
  settingsBaseline: "",
  activeView: "operations",
  pollTimer: null,
  clockTimer: null,
  radioMap: null,
  radioMapLayer: null,
  radioMapSignature: "",
};

const elements = {};

document.addEventListener("DOMContentLoaded", () => {
  cacheElements();
  bindEvents();
  updateThemeControl();
  updateClock();
  state.clockTimer = window.setInterval(updateClock, 1000);
  loadSession();
  pollStatus();
  state.pollTimer = window.setInterval(pollStatus, 1000);
});

function cacheElements() {
  const ids = [
    "header-status-dot", "header-status", "header-clock", "last-update",
    "metric-calls", "metric-calls-detail", "metric-radios", "metric-radios-detail",
    "metric-bm", "metric-bm-detail", "metric-services-up", "metric-services-total",
    "metric-services-detail", "channel-state", "channel-state-label", "active-call-list", "radio-count",
    "radios-body", "service-count", "service-list", "calls-body", "view-operations",
    "view-admin", "login-shell", "login-form", "login-error", "admin-content",
    "logout-button", "settings-form", "mapping-rows", "mapping-row-template",
    "add-mapping", "save-state", "settings-version", "save-settings",
    "password-form", "toast-stack", "theme-toggle", "theme-icon", "map-count",
    "radio-map", "radio-map-empty", "talkgroup-count", "talkgroup-list", "talkgroup-source",
    "connection-ars-address", "connection-mapping-count", "connection-mapping-list",
  ];
  for (const id of ids) elements[id] = document.getElementById(id);
  elements.tabs = [...document.querySelectorAll(".nav-tab")];
}

function bindEvents() {
  for (const tab of elements.tabs) {
    tab.addEventListener("click", () => switchView(tab.dataset.view));
  }
  elements["login-form"].addEventListener("submit", login);
  elements["logout-button"].addEventListener("click", logout);
  elements["settings-form"].addEventListener("submit", saveSettings);
  elements["settings-form"].addEventListener("input", markSettingsDirty);
  elements["settings-form"].elements.audioDmrP25Agc.addEventListener("change", updateAudioControlState);
  elements["add-mapping"].addEventListener("click", () => {
    addMappingRow({ p25: "", brandmeister: "" });
    markSettingsDirty();
  });
  elements["password-form"].addEventListener("submit", changePassword);
  elements["theme-toggle"].addEventListener("click", toggleTheme);
  window.addEventListener("beforeunload", (event) => {
    if (!settingsAreDirty()) return;
    event.preventDefault();
    event.returnValue = "";
  });
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json", ...(options.headers || {}) };
  if (options.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
  if (state.session?.csrfToken && options.method && options.method !== "GET") {
    headers["X-CSRF-Token"] = state.session.csrfToken;
  }
  const response = await fetch(path, { credentials: "same-origin", ...options, headers });
  let payload = {};
  try {
    payload = await response.json();
  } catch (_) {
    payload = {};
  }
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/auth/login") {
      state.session = { authenticated: false };
      renderAuth();
    }
    throw new Error(payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

function switchView(view) {
  if (view !== "operations" && view !== "admin") return;
  state.activeView = view;
  for (const tab of elements.tabs) {
    const active = tab.dataset.view === view;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", String(active));
  }
  for (const target of ["operations", "admin"]) {
    const panel = elements[`view-${target}`];
    const active = target === view;
    panel.classList.toggle("is-visible", active);
    panel.hidden = !active;
  }
  if (view === "admin" && state.session?.authenticated && !state.settings) loadSettings();
  if (view === "operations" && state.radioMap) {
    window.requestAnimationFrame(() => state.radioMap.invalidateSize());
  }
}

function toggleTheme() {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  document.documentElement.style.colorScheme = next;
  try {
    window.localStorage.setItem("quantar-dashboard-theme", next);
  } catch (_) {
    // Theme selection still applies for the current page.
  }
  updateThemeControl();
  if (state.radioMap) window.requestAnimationFrame(() => state.radioMap.invalidateSize());
}

function updateThemeControl() {
  const dark = document.documentElement.dataset.theme === "dark";
  elements["theme-icon"].textContent = dark ? "☀" : "☾";
  elements["theme-toggle"].title = dark ? "Lightmode einschalten" : "Darkmode einschalten";
  elements["theme-toggle"].setAttribute("aria-label", elements["theme-toggle"].title);
}

function updateClock() {
  elements["header-clock"].textContent = new Intl.DateTimeFormat("de-DE", {
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).format(new Date());
  if (state.status) renderActiveCalls(state.status.activeCalls || []);
  if (state.status) renderTalkgroups(state.status.talkgroups || {});
}

async function pollStatus() {
  try {
    state.status = await api("/api/status");
    renderStatus();
  } catch (error) {
    renderOffline(error.message);
  }
}

function renderStatus() {
  const data = state.status;
  const summary = data.summary || {};
  const healthy = summary.systemState === "healthy";
  elements["header-status-dot"].className = `status-dot ${healthy ? "status-dot--ok" : "status-dot--warn"}`;
  elements["header-status"].textContent = healthy ? "System bereit" : "System eingeschränkt";
  elements["last-update"].dateTime = data.serverTime;
  elements["last-update"].textContent = formatDateTime(data.serverTime);

  elements["metric-calls"].textContent = summary.activeCalls ?? 0;
  elements["metric-calls-detail"].textContent = summary.activeCalls ? "Funkverkehr aktiv" : "Kanal frei";
  elements["metric-radios"].textContent = summary.registeredRadios ?? 0;
  elements["metric-radios-detail"].textContent = summary.registeredRadios === 1 ? "1 aktive Anmeldung" : `${summary.registeredRadios || 0} aktive Anmeldungen`;

  const bm = brandmeisterLabel(data.brandmeister?.state);
  elements["metric-bm"].textContent = bm.label;
  elements["metric-bm-detail"].textContent = data.brandmeister?.lastChange ? `Seit ${formatTime(data.brandmeister.lastChange)}` : "Kein Verbindungsereignis";
  elements["metric-services-up"].textContent = summary.runningServices ?? 0;
  elements["metric-services-total"].textContent = summary.totalServices ?? 0;
  elements["metric-services-detail"].textContent = healthy ? "Alle Kerndienste laufen" : "Dienstprüfung erforderlich";

  renderActiveCalls(data.activeCalls || []);
  renderConnection(data.connection || {});
  renderRadios(data.radios || []);
  renderRadioMap(data.radios || []);
  renderTalkgroups(data.talkgroups || {});
  renderServices(data.services || []);
  renderCallHistory(data.recentCalls || []);
}

function renderConnection(connection) {
  const address = String(connection.arsServerAddress || "").trim();
  elements["connection-ars-address"].textContent = address || "Nicht konfiguriert";

  const mappings = (connection.talkgroupMappings || []).filter((entry) => (
    Number(entry.p25) > 0 && Number(entry.brandmeister) > 0
  ));
  elements["connection-mapping-count"].textContent = mappings.length;
  if (!mappings.length) {
    elements["connection-mapping-list"].innerHTML = '<div class="empty-state empty-state--inline"><div><strong>Kein Talkgroup-Mapping konfiguriert</strong></div></div>';
    return;
  }

  elements["connection-mapping-list"].innerHTML = mappings.map((entry) => `
    <div class="connection-mapping-row">
      <div class="connection-route" aria-label="P25 Talkgroup ${entry.p25} zu BrandMeister Talkgroup ${entry.brandmeister}">
        <span><small>P25</small><strong>${escapeHtml(entry.p25)}</strong></span>
        <b aria-hidden="true">↔</b>
        <span><small>BM</small><strong>${escapeHtml(entry.brandmeister)}</strong></span>
      </div>
      <span class="connection-mapping-name">${escapeHtml(entry.name || "Bidirektional")}</span>
    </div>
  `).join("");
}

function renderOffline(message) {
  elements["header-status-dot"].className = "status-dot status-dot--error";
  elements["header-status"].textContent = "Dashboard offline";
  elements["metric-services-detail"].textContent = message || "Keine Verbindung";
}

function brandmeisterLabel(value) {
  const labels = {
    connected: { label: "Verbunden", tone: "ok" },
    connecting: { label: "Verbindet", tone: "warn" },
    disconnected: { label: "Getrennt", tone: "error" },
    unknown: { label: "Unbekannt", tone: "muted" },
  };
  return labels[value] || labels.unknown;
}

function identityPresentation(identity = {}, fallbackId = 0, fallbackLabel = "") {
  const id = Number(identity.id || fallbackId) || fallbackId;
  const callsign = String(identity.callsign || "").trim();
  const name = String(identity.name || "").trim();
  const localLabel = String(identity.localLabel || fallbackLabel || "").trim();
  const primary = callsign || name || localLabel || `RID ${id}`;
  const details = [];
  for (const value of [name, localLabel]) {
    if (value && value.toLocaleLowerCase() !== primary.toLocaleLowerCase()
        && !details.some((item) => item.toLocaleLowerCase() === value.toLocaleLowerCase())) {
      details.push(value);
    }
  }
  details.push(`RID ${id}`);
  const location = String(identity.location || "").trim();
  return {
    id,
    primary,
    secondary: details.join(" · "),
    title: [...details, location].filter(Boolean).join(" · "),
  };
}

function identityMarkup(identity, fallbackId, fallbackLabel = "") {
  const view = identityPresentation(identity, fallbackId, fallbackLabel);
  return `<span class="radio-id" title="${escapeHtml(view.title)}">
    <strong>${escapeHtml(view.primary)}</strong>
    <small>${escapeHtml(view.secondary)}</small>
  </span>`;
}

function renderActiveCalls(calls) {
  const channel = elements["channel-state"];
  channel.classList.toggle("is-active", calls.length > 0);
  elements["channel-state-label"].textContent = calls.length ? "On Air" : "Kanal frei";
  if (!calls.length) {
    elements["active-call-list"].innerHTML = `
      <div class="empty-state empty-state--inline">
        <span class="empty-state__signal" aria-hidden="true"></span>
        <div><strong>Kein laufendes Gespräch</strong><span>Der Sprachkanal ist frei.</span></div>
      </div>`;
    return;
  }
  elements["active-call-list"].innerHTML = calls.map((call) => {
    const direction = call.direction === "uplink" ? "P25 → BrandMeister" : "BrandMeister → P25";
    const bmTalkgroup = call.mappedTalkgroup || call.talkgroup;
    const talkgroup = call.talkgroupName || `TG ${bmTalkgroup}`;
    const mapping = call.mappedTalkgroup
      ? `P25 TG ${call.talkgroup} ↔ BM TG ${call.mappedTalkgroup}`
      : `TG ${call.talkgroup}`;
    return `
      <article class="on-air-call">
        <div class="on-air-call__direction"><span class="direction-label">${escapeHtml(direction)}</span><small>Sprachverbindung</small></div>
        <div class="on-air-call__field"><span>Teilnehmer</span>${identityMarkup(call.sourceIdentity, call.sourceId, call.sourceLabel)}</div>
        <div class="on-air-call__field"><span>Talkgroup</span><strong>${escapeHtml(talkgroup)}</strong><small>${escapeHtml(mapping)}</small></div>
        <div class="on-air-call__timer"><span>Dauer (MM:SS)</span><strong>${formatDuration(call.durationSeconds)}</strong></div>
      </article>`;
  }).join("");
}

function renderRadios(radios) {
  elements["radio-count"].textContent = radios.length;
  if (!radios.length) {
    elements["radios-body"].innerHTML = '<tr><td colspan="5" class="table-empty">Noch keine Funkgeräte registriert</td></tr>';
    return;
  }
  elements["radios-body"].innerHTML = radios.map((radio) => {
    const gps = gpsLabel(radio.gpsStatus);
    return `<tr>
      <td>${identityMarkup(radio.identity, radio.id, radio.label)}</td>
      <td class="mono">${escapeHtml(radio.subscriberIp || "--")}</td>
      <td><span class="status-pill ${radio.tms ? "status-pill--ok" : "status-pill--warn"}">${radio.tms ? "Bereit" : "Wartet"}</span></td>
      <td><span class="status-pill status-pill--${gps.tone}">${gps.label}</span></td>
      <td title="${escapeHtml(formatDateTime(radio.lastSeen))}">${formatAgo(radio.lastSeen)}</td>
    </tr>`;
  }).join("");
}

function gpsLabel(value) {
  const labels = {
    fix: { label: "Fix", tone: "ok" },
    no_fix: { label: "Kein Fix", tone: "warn" },
    querying: { label: "Abfrage", tone: "muted" },
    waiting: { label: "Wartet", tone: "muted" },
  };
  return labels[value] || labels.waiting;
}

function ensureRadioMap() {
  if (state.radioMap || typeof window.L === "undefined") return;
  state.radioMap = window.L.map("radio-map", {
    zoomControl: true,
    attributionControl: true,
  }).setView([51.0, 10.0], 6);
  window.L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    className: "map-tiles",
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
  }).addTo(state.radioMap);
  state.radioMapLayer = window.L.layerGroup().addTo(state.radioMap);
}

function renderRadioMap(radios) {
  const positioned = radios.filter((radio) => radio.position
    && Number.isFinite(Number(radio.position.latitude))
    && Number.isFinite(Number(radio.position.longitude)));
  elements["map-count"].textContent = `${positioned.length}/${radios.length}`;
  elements["radio-map-empty"].hidden = positioned.length > 0;
  ensureRadioMap();
  if (!state.radioMap || !state.radioMapLayer) return;

  const signature = JSON.stringify(positioned.map((radio) => [
    radio.id, radio.label, radio.identity, radio.gpsStatus, radio.position.latitude,
    radio.position.longitude, radio.position.updatedAt,
  ]));
  if (signature === state.radioMapSignature) return;
  state.radioMapSignature = signature;
  state.radioMapLayer.clearLayers();
  if (!positioned.length) {
    state.radioMap.setView([51.0, 10.0], 6);
    return;
  }

  const bounds = [];
  for (const radio of positioned) {
    const latitude = Number(radio.position.latitude);
    const longitude = Number(radio.position.longitude);
    const hasFix = radio.gpsStatus === "fix";
    const identity = identityPresentation(radio.identity, radio.id, radio.label);
    const marker = window.L.circleMarker([latitude, longitude], {
      radius: 9,
      weight: 3,
      color: hasFix ? "#385500" : "#8a5a13",
      fillColor: hasFix ? "#a8e52a" : "#e1a440",
      fillOpacity: 0.92,
    });
    marker.bindPopup(`<div class="map-popup">
      <strong>${escapeHtml(identity.primary)}</strong>
      <span>${escapeHtml(identity.secondary)}</span>
      <small>${latitude.toFixed(5)}, ${longitude.toFixed(5)}</small>
      <small>Position ${escapeHtml(formatAgo(radio.position.updatedAt))}</small>
    </div>`);
    marker.bindTooltip(escapeHtml(identity.primary), { direction: "top", offset: [0, -8] });
    marker.addTo(state.radioMapLayer);
    bounds.push([latitude, longitude]);
  }
  if (bounds.length === 1) state.radioMap.setView(bounds[0], 14);
  else state.radioMap.fitBounds(bounds, { padding: [42, 42], maxZoom: 15 });
  window.requestAnimationFrame(() => state.radioMap.invalidateSize());
}

function renderTalkgroups(subscriptions) {
  const groups = [
    ...(subscriptions.dynamic || []).map((entry) => ({ ...entry, type: "dynamic" })),
    ...(subscriptions.static || []).map((entry) => ({ ...entry, type: "static" })),
    ...(subscriptions.timed || []).map((entry) => ({ ...entry, type: "timed" })),
  ];
  elements["talkgroup-count"].textContent = groups.length;
  if (!groups.length) {
    const label = subscriptions.status === "loading"
      ? "Abonnements werden geladen"
      : "Keine Talkgroups abonniert";
    elements["talkgroup-list"].innerHTML = `<div class="empty-state empty-state--inline"><div><strong>${label}</strong></div></div>`;
  } else {
    const timeout = Number(subscriptions.dynamicTimeoutSeconds) || 600;
    elements["talkgroup-list"].innerHTML = groups.map((entry) => {
      const dynamic = entry.type === "dynamic";
      const remaining = dynamic && entry.expiresAt
        ? Math.max(0, Math.ceil((new Date(entry.expiresAt).getTime() - Date.now()) / 1000))
        : null;
      const labels = {
        dynamic: ["Dynamisch", "talkgroup-type--dynamic"],
        static: ["Statisch", "talkgroup-type--static"],
        timed: ["Zeitplan", "talkgroup-type--timed"],
      };
      const [typeLabel, typeClass] = labels[entry.type];
      const status = dynamic
        ? (remaining === null ? "Timer wird synchronisiert" : formatDuration(remaining))
        : (entry.type === "static" ? "Dauerhaft" : "Zeitgesteuert");
      const progress = dynamic && remaining !== null
        ? `<progress class="talkgroup-progress" max="${timeout}" value="${Math.min(timeout, remaining)}">${remaining}</progress>`
        : "";
      return `<div class="talkgroup-row ${dynamic ? "is-dynamic" : ""}">
        <div class="talkgroup-row__identity">
          <strong>${escapeHtml(entry.name || `TG ${entry.talkgroup}`)}</strong>
          <span>TG ${entry.talkgroup} · ${entry.slot ? `TS ${entry.slot}` : "Hotspot"}</span>
        </div>
        <div class="talkgroup-row__state">
          <span class="talkgroup-type ${typeClass}">${typeLabel}</span>
          <strong class="talkgroup-timer">${status}</strong>
        </div>
        ${progress}
      </div>`;
    }).join("");
  }

  if (subscriptions.status === "stale") {
    elements["talkgroup-source"].textContent = subscriptions.lastUpdated
      ? `Letzter BrandMeister-Stand ${formatAgo(subscriptions.lastUpdated)}`
      : "BrandMeister-Profil nicht erreichbar";
    elements["talkgroup-source"].classList.add("is-stale");
  } else if (subscriptions.lastUpdated) {
    elements["talkgroup-source"].textContent = `BrandMeister-Stand ${formatAgo(subscriptions.lastUpdated)}`;
    elements["talkgroup-source"].classList.remove("is-stale");
  } else {
    elements["talkgroup-source"].textContent = "BrandMeister-Profil wird abgefragt";
    elements["talkgroup-source"].classList.remove("is-stale");
  }
}

function renderServices(services) {
  const running = services.filter((service) => service.running).length;
  elements["service-count"].textContent = `${running}/${services.length}`;
  if (!services.length) {
    elements["service-list"].innerHTML = '<div class="empty-state empty-state--inline"><div><strong>Keine Dienste gefunden</strong></div></div>';
    return;
  }
  elements["service-list"].innerHTML = services.map((service) => `
    <div class="service-row">
      <span class="service-row__dot ${service.running ? "is-running" : ""}" aria-hidden="true"></span>
      <span class="service-row__name"><strong>${escapeHtml(service.label)}</strong><small>${service.running ? "Läuft" : escapeHtml(service.state || "Gestoppt")}</small></span>
      <span class="service-row__pid">${service.pid ? `PID ${service.pid}` : "--"}</span>
    </div>`).join("");
}

function renderCallHistory(calls) {
  if (!calls.length) {
    elements["calls-body"].innerHTML = '<tr><td colspan="6" class="table-empty">Noch keine Gespräche erfasst</td></tr>';
    return;
  }
  elements["calls-body"].innerHTML = calls.map((call) => `
    <tr>
      <td title="${escapeHtml(formatDateTime(call.startedAt))}">${formatDateTime(call.startedAt)}</td>
      <td><span class="direction-chip">${call.direction === "uplink" ? "Uplink" : "Downlink"}</span></td>
      <td>${identityMarkup(call.sourceIdentity, call.sourceId, call.sourceLabel)}</td>
      <td class="mono">${call.talkgroup}</td>
      <td><span class="radio-id"><strong>${escapeHtml(call.talkgroupName || (call.mappedTalkgroup ? `TG ${call.mappedTalkgroup}` : `TG ${call.talkgroup}`))}</strong><small>${call.mappedTalkgroup ? `TG ${call.mappedTalkgroup}` : `TG ${call.talkgroup}`}</small></span></td>
      <td class="mono">${formatDuration(call.durationSeconds)}</td>
    </tr>`).join("");
}

async function loadSession() {
  try {
    state.session = await api("/api/auth/session");
  } catch (_) {
    state.session = { authenticated: false };
  }
  renderAuth();
}

function renderAuth() {
  const authenticated = Boolean(state.session?.authenticated);
  elements["login-shell"].hidden = authenticated;
  elements["admin-content"].hidden = !authenticated;
  elements["logout-button"].hidden = !authenticated;
  if (authenticated && state.activeView === "admin" && !state.settings) loadSettings();
  if (!authenticated) {
    state.settings = null;
    state.settingsBaseline = "";
  }
}

async function login(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector("button[type=submit]");
  elements["login-error"].textContent = "";
  button.disabled = true;
  try {
    state.session = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({
        username: form.elements.username.value,
        password: form.elements.password.value,
      }),
    });
    form.elements.password.value = "";
    renderAuth();
    await loadSettings();
    toast("Anmeldung erfolgreich.");
  } catch (error) {
    elements["login-error"].textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST", body: "{}" });
  } catch (_) {
    // Local session state is cleared even if the server session already expired.
  }
  state.session = { authenticated: false };
  renderAuth();
  toast("Abgemeldet.");
}

async function loadSettings() {
  try {
    const settings = await api("/api/settings");
    state.settings = settings;
    populateSettings(settings);
  } catch (error) {
    toast(error.message, "error");
  }
}

function populateSettings(settings) {
  const form = elements["settings-form"];
  const dmrToP25 = settings.audio.dmrToP25;
  const p25ToDmr = settings.audio.p25ToDmr;
  form.elements.repeaterId.value = settings.repeaterId;
  form.elements.brandmeisterCallsign.value = settings.brandmeisterCallsign;
  form.elements.brandmeisterTimeslot.value = settings.brandmeisterTimeslot;
  form.elements.brandmeisterRxFrequencyMhz.value = (settings.brandmeisterRxFrequency / 1e6).toFixed(6);
  form.elements.brandmeisterTxFrequencyMhz.value = (settings.brandmeisterTxFrequency / 1e6).toFixed(6);
  form.elements.brandmeisterAddress.value = settings.brandmeisterAddress;
  form.elements.brandmeisterPassword.value = "";
  form.elements.dynamicTimeoutSeconds.value = settings.dynamicTimeoutSeconds;
  form.elements.gpsInitial.value = settings.gps.initialDelaySeconds;
  form.elements.gpsUpdate.value = settings.gps.updateIntervalSeconds;
  form.elements.gpsNoFix.value = settings.gps.noFixRetrySeconds;
  form.elements.audioDmrP25Rx.value = dmrToP25.rxAudioGain;
  form.elements.audioDmrP25Decoder.value = dmrToP25.vocoderDecoderAudioGain;
  form.elements.audioDmrP25DecoderAuto.checked = dmrToP25.vocoderDecoderAutoGain;
  form.elements.audioDmrP25Tx.value = dmrToP25.txAudioGain;
  form.elements.audioDmrP25Encoder.value = dmrToP25.vocoderEncoderAudioGain;
  form.elements.audioDmrP25Presence.value = dmrToP25.p25EncodePresenceGain;
  form.elements.audioDmrP25Agc.checked = dmrToP25.p25EncodeAgc;
  form.elements.audioDmrP25AgcTarget.value = dmrToP25.p25EncodeAgcTargetRms;
  form.elements.audioDmrP25AgcMin.value = dmrToP25.p25EncodeAgcMinGain;
  form.elements.audioDmrP25AgcMax.value = dmrToP25.p25EncodeAgcMaxGain;
  form.elements.audioDmrP25AgcAttack.value = dmrToP25.p25EncodeAgcAttack;
  form.elements.audioDmrP25AgcRelease.value = dmrToP25.p25EncodeAgcRelease;
  form.elements.audioDmrP25Peak.value = dmrToP25.p25EncodeAgcPeakLimit;
  form.elements.audioP25DmrRx.value = p25ToDmr.rxAudioGain;
  form.elements.audioP25DmrDecoder.value = p25ToDmr.vocoderDecoderAudioGain;
  form.elements.audioP25DmrDecoderAuto.checked = p25ToDmr.vocoderDecoderAutoGain;
  form.elements.audioP25DmrTx.value = p25ToDmr.txAudioGain;
  form.elements.audioP25DmrEncoder.value = p25ToDmr.vocoderEncoderAudioGain;
  updateAudioControlState();
  elements["mapping-rows"].innerHTML = "";
  for (const mapping of settings.talkgroupMappings) addMappingRow(mapping);
  updateRemoveButtons();
  elements["settings-version"].textContent = `Konfiguration ${settings.version}`;
  state.settingsBaseline = serializeSettings();
  updateSaveState(false);
}

function updateAudioControlState() {
  const form = elements["settings-form"];
  const enabled = form.elements.audioDmrP25Agc.checked;
  for (const name of [
    "audioDmrP25AgcTarget", "audioDmrP25AgcMin", "audioDmrP25AgcMax",
    "audioDmrP25AgcAttack", "audioDmrP25AgcRelease", "audioDmrP25Peak",
  ]) {
    form.elements[name].disabled = !enabled;
  }
}

function addMappingRow(mapping) {
  const fragment = elements["mapping-row-template"].content.cloneNode(true);
  const row = fragment.querySelector(".mapping-row");
  row.querySelector(".mapping-p25").value = mapping.p25;
  row.querySelector(".mapping-bm").value = mapping.brandmeister;
  row.querySelector(".remove-mapping").addEventListener("click", () => {
    row.remove();
    updateRemoveButtons();
    markSettingsDirty();
  });
  elements["mapping-rows"].appendChild(fragment);
  updateRemoveButtons();
}

function updateRemoveButtons() {
  const buttons = [...elements["mapping-rows"].querySelectorAll(".remove-mapping")];
  for (const button of buttons) button.disabled = buttons.length <= 1;
}

function settingsPayload() {
  const form = elements["settings-form"];
  return {
    repeaterId: Number(form.elements.repeaterId.value),
    brandmeisterCallsign: form.elements.brandmeisterCallsign.value.trim().toUpperCase(),
    brandmeisterTimeslot: Number(form.elements.brandmeisterTimeslot.value),
    brandmeisterRxFrequency: Math.round(Number(form.elements.brandmeisterRxFrequencyMhz.value) * 1e6),
    brandmeisterTxFrequency: Math.round(Number(form.elements.brandmeisterTxFrequencyMhz.value) * 1e6),
    brandmeisterPassword: form.elements.brandmeisterPassword.value,
    dynamicTimeoutSeconds: Number(form.elements.dynamicTimeoutSeconds.value),
    talkgroupMappings: [...elements["mapping-rows"].querySelectorAll(".mapping-row")].map((row) => ({
      p25: Number(row.querySelector(".mapping-p25").value),
      brandmeister: Number(row.querySelector(".mapping-bm").value),
    })),
    gps: {
      initialDelaySeconds: Number(form.elements.gpsInitial.value),
      updateIntervalSeconds: Number(form.elements.gpsUpdate.value),
      noFixRetrySeconds: Number(form.elements.gpsNoFix.value),
    },
    audio: {
      dmrToP25: {
        rxAudioGain: Number(form.elements.audioDmrP25Rx.value),
        vocoderDecoderAudioGain: Number(form.elements.audioDmrP25Decoder.value),
        vocoderDecoderAutoGain: form.elements.audioDmrP25DecoderAuto.checked,
        txAudioGain: Number(form.elements.audioDmrP25Tx.value),
        vocoderEncoderAudioGain: Number(form.elements.audioDmrP25Encoder.value),
        p25EncodePresenceGain: Number(form.elements.audioDmrP25Presence.value),
        p25EncodeAgc: form.elements.audioDmrP25Agc.checked,
        p25EncodeAgcTargetRms: Number(form.elements.audioDmrP25AgcTarget.value),
        p25EncodeAgcMinGain: Number(form.elements.audioDmrP25AgcMin.value),
        p25EncodeAgcMaxGain: Number(form.elements.audioDmrP25AgcMax.value),
        p25EncodeAgcAttack: Number(form.elements.audioDmrP25AgcAttack.value),
        p25EncodeAgcRelease: Number(form.elements.audioDmrP25AgcRelease.value),
        p25EncodeAgcPeakLimit: Number(form.elements.audioDmrP25Peak.value),
      },
      p25ToDmr: {
        rxAudioGain: Number(form.elements.audioP25DmrRx.value),
        vocoderDecoderAudioGain: Number(form.elements.audioP25DmrDecoder.value),
        vocoderDecoderAutoGain: form.elements.audioP25DmrDecoderAuto.checked,
        txAudioGain: Number(form.elements.audioP25DmrTx.value),
        vocoderEncoderAudioGain: Number(form.elements.audioP25DmrEncoder.value),
      },
    },
  };
}

function serializeSettings() {
  return JSON.stringify(settingsPayload());
}

function settingsAreDirty() {
  return Boolean(state.settingsBaseline && serializeSettings() !== state.settingsBaseline);
}

function markSettingsDirty() {
  updateSaveState(settingsAreDirty());
}

function updateSaveState(dirty) {
  elements["save-settings"].disabled = !dirty;
  elements["save-state"].textContent = dirty ? "Ungespeicherte Änderungen" : "Keine offenen Änderungen";
}

async function saveSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.reportValidity()) return;
  const button = elements["save-settings"];
  button.disabled = true;
  button.textContent = "Wird angewendet...";
  try {
    const result = await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify(settingsPayload()),
    });
    state.settings = result.settings;
    populateSettings(result.settings);
    const restarted = (result.restarted || []).length;
    toast(restarted ? `Gespeichert. ${restarted} Dienste wurden neu gestartet.` : "Keine Änderung erforderlich.");
  } catch (error) {
    toast(error.message, "error", 7000);
    updateSaveState(true);
  } finally {
    button.textContent = "Speichern und anwenden";
    if (!settingsAreDirty()) button.disabled = true;
  }
}

async function changePassword(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.reportValidity()) return;
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    await api("/api/auth/password", {
      method: "POST",
      body: JSON.stringify({
        currentPassword: form.elements.currentPassword.value,
        newPassword: form.elements.newPassword.value,
      }),
    });
    form.reset();
    toast("Admin-Passwort geändert.");
  } catch (error) {
    toast(error.message, "error");
  } finally {
    button.disabled = false;
  }
}

function toast(message, tone = "ok", duration = 4500) {
  const item = document.createElement("div");
  item.className = `toast ${tone === "error" ? "toast--error" : tone === "warn" ? "toast--warn" : ""}`;
  const text = document.createElement("span");
  text.textContent = message;
  item.appendChild(text);
  elements["toast-stack"].appendChild(item);
  window.setTimeout(() => item.remove(), duration);
}

function formatDuration(value) {
  const total = Math.max(0, Math.ceil(Number(value) || 0));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function formatDateTime(value) {
  if (!value) return "--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--";
  return new Intl.DateTimeFormat("de-DE", {
    day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
  }).format(date);
}

function formatTime(value) {
  if (!value) return "--";
  return new Intl.DateTimeFormat("de-DE", { hour: "2-digit", minute: "2-digit" }).format(new Date(value));
}

function formatAgo(value) {
  if (!value) return "--";
  const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 10) return "gerade eben";
  if (seconds < 60) return `vor ${seconds} Sek.`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `vor ${minutes} Min.`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `vor ${hours} Std.`;
  return formatDateTime(value);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
  })[character]);
}
