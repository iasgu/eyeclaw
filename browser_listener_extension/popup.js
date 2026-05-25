const els = {
  apiBase: document.getElementById("apiBase"),
  clientName: document.getElementById("clientName"),
  statusText: document.getElementById("statusText"),
  startBtn: document.getElementById("startBtn"),
  stopBtn: document.getElementById("stopBtn"),
  openAppBtn: document.getElementById("openAppBtn"),
  sessionId: document.getElementById("sessionId"),
  saveBtn: document.getElementById("saveBtn"),
  resetBtn: document.getElementById("resetBtn")
};

function setBusy(isBusy) {
  els.startBtn.disabled = isBusy || els.startBtn.dataset.enabled === "true";
  els.stopBtn.disabled = isBusy || els.stopBtn.dataset.enabled !== "true";
  els.openAppBtn.disabled = isBusy;
  els.saveBtn.disabled = isBusy;
  els.resetBtn.disabled = isBusy;
}

function setError(message) {
  els.statusText.textContent = message;
  els.statusText.style.background = "#fff0f0";
  els.statusText.style.border = "1px solid #ffc8c8";
  els.statusText.style.color = "#c02020";
}

function setStatus(text) {
  els.statusText.textContent = text;
  els.statusText.style.background = "#eef5ff";
  els.statusText.style.border = "1px solid #d6e7ff";
  els.statusText.style.color = "#2d4f78";
}

async function sendMessage(type, payload) {
  const response = await chrome.runtime.sendMessage({ type, payload });
  if (!response || !response.ok) {
    throw new Error(response && response.error ? response.error : "扩展后台没有返回有效响应");
  }
  return response;
}

async function getSettings() {
  const response = await sendMessage("eyeclaw-listener-get-settings");
  return response.settings;
}

async function checkBackend() {
  const response = await sendMessage("eyeclaw-listener-check-backend", {
    apiBase: els.apiBase.value.trim()
  });
  return response.status;
}

async function saveSettings() {
  const response = await sendMessage("eyeclaw-listener-save-settings", {
    apiBase: els.apiBase.value.trim(),
    clientName: els.clientName.value.trim()
  });
  render(response.settings);
  const status = await checkBackend();
  setStatus(`设置已保存，后端连接正常（HTTP ${status}）`);
}

async function startListening() {
  setBusy(true);
  try {
    await checkBackend();
    const response = await sendMessage("eyeclaw-listener-start-session", {
      apiBase: els.apiBase.value.trim(),
      clientName: els.clientName.value.trim()
    });
    render(response.settings);
    if (response.settings?.recordingState === "recording") {
      setStatus("当前状态：监听中，当前普通标签页录屏已启动。");
    } else {
      setStatus("当前状态：监听中；插件弹窗无法录制当前页。需要录屏时，请点“打开 Eyeclaw 前端录屏”。");
    }
  } finally {
    setBusy(false);
  }
}

async function stopListening() {
  setBusy(true);
  try {
    const response = await sendMessage("eyeclaw-listener-stop-session", {
      apiBase: els.apiBase.value.trim(),
      clientName: els.clientName.value.trim()
    });
    render(response.settings);
    setStatus("当前状态：未监听，录屏已停止");
  } finally {
    setBusy(false);
  }
}

async function resetSession() {
  const response = await sendMessage("eyeclaw-listener-reset-session");
  render(response.settings);
  setStatus("会话 ID 已重置");
}

async function openApp() {
  const apiBase = (els.apiBase.value.trim() || "http://127.0.0.1:8018").replace(/\/+$/, "");
  await chrome.tabs.create({ url: `${apiBase}/app` });
}

function render(settings) {
  const enabled = !!settings.enabled;
  const recordingState = settings.recordingState === "recording"
    ? "录屏中"
    : settings.recordingState === "pending"
      ? "插件录屏未启动"
      : "未录屏";
  els.apiBase.value = settings.apiBase || "";
  els.clientName.value = settings.clientName || "";
  els.startBtn.dataset.enabled = enabled ? "true" : "false";
  els.stopBtn.dataset.enabled = enabled ? "true" : "false";
  els.startBtn.disabled = enabled;
  els.stopBtn.disabled = !enabled;
  els.startBtn.style.opacity = enabled ? "0.6" : "1";
  els.stopBtn.style.opacity = enabled ? "1" : "0.6";
  els.sessionId.textContent = `当前会话：${settings.sessionId} · ${recordingState}`;
}

function handleError(context, error) {
  console.error(context, error);
  setBusy(false);
  setError(`${context}：${error.message || error}`);
}

els.startBtn.addEventListener("click", () => {
  startListening().catch((error) => handleError("启动失败", error));
});

els.stopBtn.addEventListener("click", () => {
  stopListening().catch((error) => handleError("停止失败", error));
});

els.openAppBtn.addEventListener("click", () => {
  openApp().catch((error) => handleError("打开前端失败", error));
});

els.saveBtn.addEventListener("click", () => {
  saveSettings().catch((error) => handleError("保存失败", error));
});

els.resetBtn.addEventListener("click", () => {
  resetSession().catch((error) => handleError("重置失败", error));
});

getSettings()
  .then(async (settings) => {
    render(settings);
    setStatus(settings.enabled ? "当前状态：监听中" : "当前状态：未监听");
    try {
      const status = await checkBackend();
      setStatus(`${settings.enabled ? "当前状态：监听中" : "当前状态：未监听"}，后端连接正常（HTTP ${status}）`);
    } catch (error) {
      setError(`后端连接失败：${error.message || error}`);
    }
  })
  .catch((error) => {
    console.error(error);
    setError("无法加载设置：" + (error.message || error));
  });
