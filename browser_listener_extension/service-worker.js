const DEFAULT_API_BASE = "http://127.0.0.1:8018";
const DEFAULT_SETTINGS = {
  apiBase: DEFAULT_API_BASE,
  enabled: false,
  sessionId: crypto.randomUUID(),
  clientName: "eyeclaw-listener",
  browserName: "edge-or-chrome",
  recordingState: "idle",
  recordingTabId: null
};

const SCREENSHOT_EVENT_TYPES = new Set([
  "tab_activated",
  "tab_updated",
  "history",
  "page_loaded",
  "click",
  "keyboard_shortcut",
  "change",
  "scroll"
]);
const MIN_SCREENSHOT_INTERVAL_MS = 1200;
const OFFSCREEN_MESSAGE_ATTEMPTS = 8;
const OFFSCREEN_MESSAGE_RETRY_MS = 150;
const queue = [];
const lastScreenshotAtByTab = new Map();

let flushTimer = null;
let isFlushing = false;
let activeRecordingSessionId = null;
let pendingRecordingSettings = null;

function normalizeApiBase(value) {
  const apiBase = typeof value === "string" && value.trim() ? value.trim() : DEFAULT_API_BASE;
  return apiBase.replace(/\/+$/, "");
}

async function getSettings() {
  const stored = await chrome.storage.local.get(DEFAULT_SETTINGS);
  const merged = { ...DEFAULT_SETTINGS, ...stored };
  merged.apiBase = normalizeApiBase(merged.apiBase);
  if (merged.apiBase === "http://127.0.0.1:8010") {
    merged.apiBase = DEFAULT_API_BASE;
  }
  if (merged.clientName === "show-once-listener") {
    merged.clientName = DEFAULT_SETTINGS.clientName;
  }
  return merged;
}

async function saveSettings(patch) {
  const nextPatch = { ...patch };
  if (Object.prototype.hasOwnProperty.call(nextPatch, "apiBase")) {
    nextPatch.apiBase = normalizeApiBase(nextPatch.apiBase);
  }
  await chrome.storage.local.set(nextPatch);
}

async function checkBackend(apiBase) {
  const response = await fetch(`${normalizeApiBase(apiBase)}/api/browser-listener/status`, {
    method: "GET",
    cache: "no-store"
  });
  if (!response.ok) {
    throw new Error(`后端返回 HTTP ${response.status}`);
  }
  return response.status;
}

function scheduleFlush() {
  if (flushTimer !== null) {
    return;
  }
  flushTimer = setTimeout(() => {
    flushTimer = null;
    flushQueue().catch(() => {});
  }, 1000);
}

function trimText(value, limit = 240) {
  if (typeof value !== "string") {
    return value ?? null;
  }
  const text = value.trim();
  if (!text) {
    return null;
  }
  if (text.length <= limit) {
    return text;
  }
  return `${text.slice(0, limit - 3)}...`;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function isMissingReceiverError(error) {
  const text = String(error?.message || error || "");
  return text.includes("Could not establish connection") || text.includes("Receiving end does not exist");
}

async function sendOffscreenMessage(message, attempts = OFFSCREEN_MESSAGE_ATTEMPTS) {
  let lastError = null;
  for (let index = 0; index < attempts; index += 1) {
    try {
      return await chrome.runtime.sendMessage({
        target: "offscreen",
        ...message
      });
    } catch (error) {
      lastError = error;
      if (!isMissingReceiverError(error) || index === attempts - 1) {
        throw error;
      }
      await sleep(OFFSCREEN_MESSAGE_RETRY_MS);
    }
  }
  throw lastError || new Error("Unable to contact offscreen recorder.");
}

async function waitForOffscreenDocumentReady() {
  const response = await sendOffscreenMessage({ type: "ping" });
  if (!response || !response.ok) {
    throw new Error("Offscreen recorder did not become ready.");
  }
}

async function ensureOffscreenDocument() {
  const offscreenUrl = chrome.runtime.getURL("offscreen.html");
  const existingContexts = await chrome.runtime.getContexts({
    contextTypes: ["OFFSCREEN_DOCUMENT"],
    documentUrls: [offscreenUrl]
  });
  if (existingContexts.length === 0) {
    await chrome.offscreen.createDocument({
      url: "offscreen.html",
      reasons: ["USER_MEDIA"],
      justification: "Record the current browser tab for session-based workflow analysis."
    });
  }
  await waitForOffscreenDocumentReady();
}

async function getActiveTab() {
  const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tabs && tabs.length ? tabs[0] : null;
}

function isRecordableTab(tab) {
  if (!tab || tab.id == null) {
    return false;
  }
  const tabUrl = tab.url || "";
  return /^https?:\/\//i.test(tabUrl) || /^file:\/\//i.test(tabUrl);
}

function isEyeclawConsoleTab(tab, apiBase) {
  const tabUrl = tab?.url || "";
  if (!tabUrl) {
    return false;
  }
  try {
    const tabParsed = new URL(tabUrl);
    const apiParsed = new URL(normalizeApiBase(apiBase));
    if (tabParsed.origin !== apiParsed.origin) {
      return false;
    }
    return tabParsed.pathname === "/" || tabParsed.pathname === "/app";
  } catch {
    return false;
  }
}

async function startRecordingForTab(tab, settings) {
  if (!tab || tab.id == null) {
    throw new Error("没有可录制的当前标签页，请先切到一个普通网页。");
  }
  if (!isRecordableTab(tab)) {
    throw new Error("当前标签页不能录制，请切到普通网页后再开始监听。");
  }
  await ensureOffscreenDocument();
  const streamId = await chrome.tabCapture.getMediaStreamId({
    targetTabId: tab.id
  });
  const response = await sendOffscreenMessage({
    type: "start-recording",
    streamId,
    sessionId: settings.sessionId,
    apiBase: settings.apiBase,
    tabId: tab.id
  });
  if (!response || !response.ok) {
    throw new Error(response && response.error ? response.error : "Unable to start offscreen recording.");
  }
  activeRecordingSessionId = settings.sessionId;
  pendingRecordingSettings = null;
  await saveSettings({
    recordingState: "recording",
    recordingTabId: tab.id
  });
}

async function startRecordingForCurrentTab(settings) {
  const tab = await getActiveTab();
  await startRecordingForTab(tab, settings);
}

async function startRecordingForNextRecordableTab(settings) {
  pendingRecordingSettings = settings;
  await saveSettings({
    ...settings,
    enabled: true,
    recordingState: "pending",
    recordingTabId: null
  });

  const activeTab = await getActiveTab();
  if (activeTab && isRecordableTab(activeTab) && !isEyeclawConsoleTab(activeTab, settings.apiBase)) {
    try {
      await startRecordingForTab(activeTab, settings);
    } catch (error) {
      console.warn("Eyeclaw Listener active tab recording deferred", error);
    }
  }
}

async function maybeStartPendingRecordingForTab(tab) {
  const settings = pendingRecordingSettings || (await getSettings());
  if (settings.recordingState !== "pending" || !settings.enabled) {
    return;
  }
  if (activeRecordingSessionId === settings.sessionId) {
    return;
  }
  if (!isRecordableTab(tab) || isEyeclawConsoleTab(tab, settings.apiBase)) {
    return;
  }

  try {
    await startRecordingForTab(tab, settings);
  } catch (error) {
    console.warn("Eyeclaw Listener pending recording start failed", error);
  }
}

async function stopRecordingIfActive(settingsOverride = null) {
  pendingRecordingSettings = null;
  const settings = settingsOverride || await getSettings();
  const storedState = settings.recordingState || "idle";
  if (!activeRecordingSessionId && storedState !== "recording") {
    await saveSettings({
      recordingState: "idle",
      recordingTabId: null
    });
    return {
      stopped: false,
      skipped: true,
      reason: storedState === "pending" ? "recording-never-started" : "no-active-recording"
    };
  }
  const response = await sendOffscreenMessage({
    type: "stop-recording"
  });
  if (!response || !response.ok) {
    throw new Error(response && response.error ? response.error : "Unable to stop offscreen recording.");
  }
  activeRecordingSessionId = null;
  await saveSettings({
    recordingState: "idle",
    recordingTabId: null
  });
  return {
    stopped: true,
    skipped: Boolean(response.result?.skipped),
    reason: response.result?.reason || null,
    upload: response.result?.upload || null
  };
}

function inferKeyCandidate(event) {
  if (typeof event.key_candidate === "boolean") {
    return event.key_candidate;
  }
  if (["navigation", "history", "tab_activated", "tab_updated", "page_loaded", "click", "keyboard_shortcut", "change"].includes(event.event_type)) {
    return true;
  }
  if (event.event_type === "input") {
    return Boolean(event.input_value);
  }
  if (event.event_type === "scroll") {
    return Math.abs(event.delta_y || 0) >= 400 || Math.abs(event.scroll_y || 0) >= 600;
  }
  return false;
}

function shouldCaptureScreenshot(event, tab) {
  if (!tab || !tab.active || tab.id == null) {
    return false;
  }
  if (!inferKeyCandidate(event)) {
    return false;
  }
  if (!SCREENSHOT_EVENT_TYPES.has(event.event_type)) {
    return false;
  }
  return true;
}

function screenshotDelayMs(eventType) {
  if (eventType === "tab_activated") {
    return 350;
  }
  if (eventType === "tab_updated" || eventType === "history" || eventType === "page_loaded") {
    return 500;
  }
  if (eventType === "click" || eventType === "keyboard_shortcut" || eventType === "change") {
    return 180;
  }
  if (eventType === "scroll") {
    return 120;
  }
  return 0;
}

async function maybeAttachScreenshot(event, tab) {
  if (!shouldCaptureScreenshot(event, tab)) {
    return event;
  }

  const windowId = tab.windowId ?? event.window_id;
  const tabId = tab.id ?? event.tab_id;
  const now = Date.now();
  const lastAt = lastScreenshotAtByTab.get(tabId) || 0;
  if (now - lastAt < MIN_SCREENSHOT_INTERVAL_MS) {
    return event;
  }

  const delayMs = screenshotDelayMs(event.event_type);
  if (delayMs > 0) {
    await sleep(delayMs);
  }

  try {
    const screenshotDataUrl = await chrome.tabs.captureVisibleTab(windowId, { format: "png" });
    lastScreenshotAtByTab.set(tabId, Date.now());
    return {
      ...event,
      key_candidate: inferKeyCandidate(event),
      screenshot_data_url: screenshotDataUrl,
      screenshot_reason: `${event.event_type}:key-candidate`
    };
  } catch (error) {
    console.warn("Eyeclaw Listener screenshot capture failed", error);
    return event;
  }
}

async function enqueueEvent(event) {
  const settings = await getSettings();
  if (!settings.enabled) {
    return;
  }
  queue.push(event);
  if (queue.length >= 12) {
    await flushQueue();
    return;
  }
  scheduleFlush();
}

async function queueEventWithOptionalScreenshot(event, tab) {
  const withScreenshot = await maybeAttachScreenshot(event, tab);
  await enqueueEvent(withScreenshot);
}

async function flushQueue() {
  if (isFlushing || queue.length === 0) {
    return;
  }
  isFlushing = true;
  const batch = queue.splice(0, 25);

  try {
    const settings = await getSettings();
    const response = await fetch(`${settings.apiBase}/api/browser-listener/events`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        client_name: settings.clientName,
        browser_name: settings.browserName,
        session_id: settings.sessionId,
        events: batch
      })
    });

    if (!response.ok) {
      throw new Error(`Listener API returned ${response.status}`);
    }
  } catch (error) {
    queue.unshift(...batch);
    console.warn("Eyeclaw Listener flush failed", error);
  } finally {
    isFlushing = false;
    if (queue.length > 0) {
      scheduleFlush();
    }
  }
}

async function emitBackgroundEvent(baseEvent, tab) {
  await queueEventWithOptionalScreenshot(
    {
      source: "extension_background",
      client_timestamp_ms: Date.now(),
      key_candidate: inferKeyCandidate(baseEvent),
      ...baseEvent
    },
    tab
  );
}

chrome.runtime.onInstalled.addListener(async () => {
  await saveSettings(DEFAULT_SETTINGS);
});

chrome.runtime.onStartup.addListener(async () => {
  const settings = await getSettings();
  if (!settings.sessionId) {
    await saveSettings({ sessionId: crypto.randomUUID() });
  }
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "eyeclaw-listener-start-session") {
    (async () => {
      const nextSettings = {
        apiBase: normalizeApiBase(message.payload?.apiBase),
        clientName: message.payload?.clientName || "eyeclaw-listener",
        enabled: false,
        sessionId: crypto.randomUUID(),
        recordingState: "idle",
        recordingTabId: null
      };
      await checkBackend(nextSettings.apiBase);
      await stopRecordingIfActive();
      let recordingWarning = null;
      try {
        await startRecordingForCurrentTab(nextSettings);
        const enabledSettings = { ...nextSettings, enabled: true, recordingState: "recording" };
        await saveSettings(enabledSettings);
      } catch (error) {
        recordingWarning = String(error);
        console.warn("Eyeclaw Listener recording deferred until a normal web page is active:", error);
        await startRecordingForNextRecordableTab(nextSettings);
      }
      const settings = await getSettings();
      sendResponse({ ok: true, settings, recording_warning: recordingWarning });
    })().catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }

  if (message?.type === "eyeclaw-listener-start-session-inline") {
    (async () => {
      const nextSettings = {
        apiBase: normalizeApiBase(message.payload?.apiBase),
        clientName: message.payload?.clientName || "eyeclaw-listener",
        enabled: true,
        sessionId: message.payload?.sessionId || crypto.randomUUID(),
        recordingState: "idle",
        recordingTabId: null
      };
      await checkBackend(nextSettings.apiBase);
      await stopRecordingIfActive();
      if (message.payload?.recordingMode === "next-recordable-tab") {
        await startRecordingForNextRecordableTab(nextSettings);
      } else {
        await saveSettings(nextSettings);
      }
      const settings = await getSettings();
      sendResponse({ ok: true, settings });
    })().catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }

  if (message?.type === "eyeclaw-listener-stop-session") {
    (async () => {
      const currentSettings = await getSettings();
      const nextApiBase = normalizeApiBase(message.payload?.apiBase);
      const nextClientName = message.payload?.clientName || "eyeclaw-listener";
      await saveSettings({
        apiBase: nextApiBase,
        clientName: nextClientName
      });
      const recordingResult = await stopRecordingIfActive(currentSettings);
      await saveSettings({
        apiBase: nextApiBase,
        clientName: nextClientName,
        browserName: currentSettings.browserName || DEFAULT_SETTINGS.browserName,
        sessionId: currentSettings.sessionId,
        enabled: false,
        recordingState: "idle",
        recordingTabId: null
      });
      const settings = await getSettings();
      sendResponse({ ok: true, settings, recording_result: recordingResult });
    })().catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }

  if (message?.type === "eyeclaw-listener-stop-session-inline") {
    (async () => {
      const currentSettings = await getSettings();
      const nextApiBase = normalizeApiBase(message.payload?.apiBase);
      const nextClientName = message.payload?.clientName || "eyeclaw-listener";
      await saveSettings({
        apiBase: nextApiBase,
        clientName: nextClientName
      });
      const recordingResult = await stopRecordingIfActive(currentSettings);
      await saveSettings({
        apiBase: nextApiBase,
        clientName: nextClientName,
        browserName: currentSettings.browserName || DEFAULT_SETTINGS.browserName,
        sessionId: currentSettings.sessionId,
        enabled: false,
        recordingState: "idle",
        recordingTabId: null
      });
      const settings = await getSettings();
      sendResponse({ ok: true, settings, recording_result: recordingResult });
    })().catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }

  if (message?.type === "eyeclaw-listener-event") {
    const tab = sender.tab || null;
    queueEventWithOptionalScreenshot(
      {
        source: "extension_content",
        tab_id: tab?.id ?? null,
        window_id: tab?.windowId ?? null,
        page_url: trimText(message.payload?.page_url || tab?.url),
        page_title: trimText(message.payload?.page_title || tab?.title),
        key_candidate: inferKeyCandidate(message.payload || {}),
        ...message.payload
      },
      tab
    ).catch(() => {});
    sendResponse({ ok: true });
    return true;
  }

  if (message?.type === "eyeclaw-listener-get-settings") {
    getSettings().then((settings) => sendResponse({ ok: true, settings }));
    return true;
  }

  if (message?.type === "eyeclaw-listener-save-settings") {
    saveSettings(message.payload || {}).then(async () => {
      const settings = await getSettings();
      sendResponse({ ok: true, settings });
    });
    return true;
  }

  if (message?.type === "eyeclaw-listener-check-backend") {
    (async () => {
      const status = await checkBackend(message.payload?.apiBase || DEFAULT_API_BASE);
      sendResponse({ ok: true, status });
    })().catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }

  if (message?.type === "eyeclaw-listener-reset-session") {
    saveSettings({ sessionId: crypto.randomUUID() }).then(async () => {
      const settings = await getSettings();
      sendResponse({ ok: true, settings });
    });
    return true;
  }

  if (message?.type === "eyeclaw-listener-recording-uploaded") {
    sendResponse({ ok: true });
    return false;
  }

  return false;
});

chrome.tabs.onActivated.addListener(async ({ tabId, windowId }) => {
  try {
    const tab = await chrome.tabs.get(tabId);
    await maybeStartPendingRecordingForTab(tab);
    await emitBackgroundEvent(
      {
        event_type: "tab_activated",
        tab_id: tabId,
        window_id: windowId,
        page_url: trimText(tab.url),
        page_title: trimText(tab.title),
        details: {
          audible: !!tab.audible,
          discarded: !!tab.discarded
        }
      },
      tab
    );
  } catch (error) {
    console.warn("Eyeclaw Listener tab activation failed", error);
  }
});

chrome.tabs.onUpdated.addListener(async (tabId, changeInfo, tab) => {
  const hasInterestingChange =
    typeof changeInfo.url === "string" ||
    typeof changeInfo.title === "string" ||
    changeInfo.status === "complete";

  if (!hasInterestingChange) {
    return;
  }

  await maybeStartPendingRecordingForTab(tab);
  await emitBackgroundEvent(
    {
      event_type: "tab_updated",
      tab_id: tabId,
      window_id: tab.windowId ?? null,
      page_url: trimText(changeInfo.url || tab.url),
      page_title: trimText(changeInfo.title || tab.title),
      details: {
        status: changeInfo.status || null
      }
    },
    tab
  );
});

chrome.webNavigation.onCommitted.addListener(async (details) => {
  if (details.frameId !== 0) {
    return;
  }

  try {
    const tab = await chrome.tabs.get(details.tabId);
    await emitBackgroundEvent(
      {
        event_type: "navigation",
        tab_id: details.tabId,
        frame_id: details.frameId,
        page_url: trimText(details.url),
        details: {
          transition_type: details.transitionType || null,
          transition_qualifiers: Array.isArray(details.transitionQualifiers)
            ? details.transitionQualifiers.join(",")
            : null
        }
      },
      tab
    );
  } catch (error) {
    console.warn("Eyeclaw Listener navigation event failed", error);
  }
});

chrome.webNavigation.onHistoryStateUpdated.addListener(async (details) => {
  if (details.frameId !== 0) {
    return;
  }

  try {
    const tab = await chrome.tabs.get(details.tabId);
    await emitBackgroundEvent(
      {
        event_type: "history",
        tab_id: details.tabId,
        frame_id: details.frameId,
        page_url: trimText(details.url)
      },
      tab
    );
  } catch (error) {
    console.warn("Eyeclaw Listener history event failed", error);
  }
});
