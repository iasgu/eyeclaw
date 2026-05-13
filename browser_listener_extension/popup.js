const els = {
  apiBase: document.getElementById("apiBase"),
  clientName: document.getElementById("clientName"),
  statusText: document.getElementById("statusText"),
  startBtn: document.getElementById("startBtn"),
  stopBtn: document.getElementById("stopBtn"),
  sessionId: document.getElementById("sessionId"),
  saveBtn: document.getElementById("saveBtn"),
  resetBtn: document.getElementById("resetBtn")
};

async function getSettings() {
  const response = await chrome.runtime.sendMessage({
    type: "show-once-listener-get-settings"
  });
  return response.settings;
}

async function saveSettings() {
  const response = await chrome.runtime.sendMessage({
    type: "show-once-listener-save-settings",
    payload: {
      apiBase: els.apiBase.value.trim(),
      clientName: els.clientName.value.trim()
    }
  });
  render(response.settings);
}

async function startListening() {
  const response = await chrome.runtime.sendMessage({
    type: "show-once-listener-save-settings",
    payload: {
      apiBase: els.apiBase.value.trim(),
      clientName: els.clientName.value.trim(),
      enabled: true,
      sessionId: crypto.randomUUID()
    }
  });
  render(response.settings);
}

async function stopListening() {
  const response = await chrome.runtime.sendMessage({
    type: "show-once-listener-save-settings",
    payload: {
      apiBase: els.apiBase.value.trim(),
      clientName: els.clientName.value.trim(),
      enabled: false
    }
  });
  render(response.settings);
}

async function resetSession() {
  const response = await chrome.runtime.sendMessage({
    type: "show-once-listener-reset-session"
  });
  render(response.settings);
}

function render(settings) {
  const enabled = !!settings.enabled;
  els.apiBase.value = settings.apiBase || "";
  els.clientName.value = settings.clientName || "";
  els.statusText.textContent = enabled ? "当前状态：监听中" : "当前状态：未监听";
  els.startBtn.disabled = enabled;
  els.stopBtn.disabled = !enabled;
  els.startBtn.style.opacity = enabled ? "0.6" : "1";
  els.stopBtn.style.opacity = enabled ? "1" : "0.6";
  els.sessionId.textContent = `当前会话：${settings.sessionId}`;
}

els.startBtn.addEventListener("click", () => {
  startListening().catch((error) => {
    console.error(error);
  });
});

els.stopBtn.addEventListener("click", () => {
  stopListening().catch((error) => {
    console.error(error);
  });
});

els.saveBtn.addEventListener("click", () => {
  saveSettings().catch((error) => {
    console.error(error);
  });
});

els.resetBtn.addEventListener("click", () => {
  resetSession().catch((error) => {
    console.error(error);
  });
});

getSettings()
  .then(render)
  .catch((error) => {
    console.error(error);
  });
