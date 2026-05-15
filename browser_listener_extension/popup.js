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

function setError(message) {
  els.statusText.textContent = message;
  els.statusText.style.background = "#fff0f0";
  els.statusText.style.border = "1px solid #ffc8c8";
  els.statusText.style.color = "#c02020";
}

function setStatus(text, recordingWarning) {
  els.statusText.textContent = text;
  els.statusText.style.background = "#eef5ff";
  els.statusText.style.border = "1px solid #d6e7ff";
  els.statusText.style.color = "#2d4f78";
  if (recordingWarning) {
    els.statusText.textContent += " " + recordingWarning;
  }
}

async function getSettings() {
  const response = await chrome.runtime.sendMessage({
    type: "eyeclaw-listener-get-settings"
  });
  return response.settings;
}

async function saveSettings() {
  const response = await chrome.runtime.sendMessage({
    type: "eyeclaw-listener-save-settings",
    payload: {
      apiBase: els.apiBase.value.trim(),
      clientName: els.clientName.value.trim()
    }
  });
  render(response.settings);
  setStatus("设置已保存。", null);
}

async function startListening() {
  const response = await chrome.runtime.sendMessage({
    type: "eyeclaw-listener-start-session",
    payload: {
      apiBase: els.apiBase.value.trim(),
      clientName: els.clientName.value.trim()
    }
  });
  if (!response || !response.ok) {
    throw new Error(response && response.error ? response.error : "无法开始监听");
  }
  render(response.settings);
  if (response.recording_warning) {
    setStatus("当前状态：监听中", `(录屏未启动: ${response.recording_warning})`);
  } else {
    setStatus("当前状态：监听中", null);
  }
}

async function stopListening() {
  const response = await chrome.runtime.sendMessage({
    type: "eyeclaw-listener-stop-session",
    payload: {
      apiBase: els.apiBase.value.trim(),
      clientName: els.clientName.value.trim()
    }
  });
  if (!response || !response.ok) {
    throw new Error(response && response.error ? response.error : "无法停止监听");
  }
  render(response.settings);
  setStatus("当前状态：未监听", null);
}

async function resetSession() {
  const response = await chrome.runtime.sendMessage({
    type: "eyeclaw-listener-reset-session"
  });
  render(response.settings);
  setStatus("会话已重置。", null);
}

function render(settings) {
  const enabled = !!settings.enabled;
  els.apiBase.value = settings.apiBase || "";
  els.clientName.value = settings.clientName || "";
  els.startBtn.disabled = enabled;
  els.stopBtn.disabled = !enabled;
  els.startBtn.style.opacity = enabled ? "0.6" : "1";
  els.stopBtn.style.opacity = enabled ? "1" : "0.6";
  els.sessionId.textContent = `当前会话：${settings.sessionId}`;
}

function handleError(context, error) {
  console.error(context, error);
  setError(`${context}: ${error.message || error}`);
}

els.startBtn.addEventListener("click", () => {
  startListening().catch((error) => handleError("启动失败", error));
});

els.stopBtn.addEventListener("click", () => {
  stopListening().catch((error) => handleError("停止失败", error));
});

els.saveBtn.addEventListener("click", () => {
  saveSettings().catch((error) => handleError("保存失败", error));
});

els.resetBtn.addEventListener("click", () => {
  resetSession().catch((error) => handleError("重置失败", error));
});

getSettings()
  .then((settings) => {
    render(settings);
    if (settings.enabled) {
      setStatus("当前状态：监听中", null);
    } else {
      setStatus("当前状态：未监听", null);
    }
  })
  .catch((error) => {
    console.error(error);
    setError("无法加载设置: " + (error.message || error));
  });
