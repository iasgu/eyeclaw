const DEFAULT_API_BASE = "http://127.0.0.1:8018";
const DEFAULT_SETTINGS = {
  apiBase: DEFAULT_API_BASE,
  enabled: false,
  sessionId: crypto.randomUUID(),
  clientName: "show-once-listener",
  browserName: "edge-or-chrome"
};

const SCREENSHOT_EVENT_TYPES = new Set([
  "tab_activated",
  "tab_updated",
  "history",
  "page_loaded",
  "click",
  "change",
  "scroll"
]);
const MIN_SCREENSHOT_INTERVAL_MS = 1200;
const queue = [];
const lastScreenshotAtByTab = new Map();

let flushTimer = null;
let isFlushing = false;
let activeRecordingSessionId = null;

async function getSettings() {
  const stored = await chrome.storage.local.get(DEFAULT_SETTINGS);
  return { ...DEFAULT_SETTINGS, ...stored };
}

async function saveSettings(patch) {
  await chrome.storage.local.set(patch);
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

async function ensureOffscreenDocument() {
  const offscreenUrl = chrome.runtime.getURL("offscreen.html");
  const existingContexts = await chrome.runtime.getContexts({
    contextTypes: ["OFFSCREEN_DOCUMENT"],
    documentUrls: [offscreenUrl]
  });
  if (existingContexts.length > 0) {
    return;
  }
  await chrome.offscreen.createDocument({
    url: "offscreen.html",
    reasons: ["USER_MEDIA"],
    justification: "Record the current browser tab for session-based workflow analysis."
  });
}

async function getActiveTab() {
  const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  return tabs && tabs.length ? tabs[0] : null;
}

async function startRecordingForCurrentTab(settings) {
  const tab = await getActiveTab();
  if (!tab || tab.id == null) {
    throw new Error("No active tab is available to record.");
  }
  await ensureOffscreenDocument();
  const streamId = await chrome.tabCapture.getMediaStreamId({
    targetTabId: tab.id
  });
  const response = await chrome.runtime.sendMessage({
    target: "offscreen",
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
}

async function stopRecordingIfActive() {
  if (!activeRecordingSessionId) {
    return;
  }
  const response = await chrome.runtime.sendMessage({
    target: "offscreen",
    type: "stop-recording"
  });
  activeRecordingSessionId = null;
  if (!response || !response.ok) {
    throw new Error(response && response.error ? response.error : "Unable to stop offscreen recording.");
  }
}

function inferKeyCandidate(event) {
  if (typeof event.key_candidate === "boolean") {
    return event.key_candidate;
  }
  if (["navigation", "history", "tab_activated", "tab_updated", "page_loaded", "click", "change"].includes(event.event_type)) {
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
  if (eventType === "click" || eventType === "change") {
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
    console.warn("Show Once Listener screenshot capture failed", error);
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
    console.warn("Show Once Listener flush failed", error);
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
  if (message?.type === "show-once-listener-start-session") {
    (async () => {
      const nextSettings = {
        apiBase: message.payload?.apiBase || DEFAULT_API_BASE,
        clientName: message.payload?.clientName || "show-once-listener",
        enabled: true,
        sessionId: crypto.randomUUID()
      };
      await saveSettings(nextSettings);
      const settings = await getSettings();
      await startRecordingForCurrentTab(settings);
      sendResponse({ ok: true, settings });
    })().catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }

  if (message?.type === "show-once-listener-stop-session") {
    (async () => {
      await saveSettings({
        apiBase: message.payload?.apiBase || DEFAULT_API_BASE,
        clientName: message.payload?.clientName || "show-once-listener",
        enabled: false
      });
      await stopRecordingIfActive();
      const settings = await getSettings();
      sendResponse({ ok: true, settings });
    })().catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }

  if (message?.type === "show-once-listener-event") {
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

  if (message?.type === "show-once-listener-get-settings") {
    getSettings().then((settings) => sendResponse({ ok: true, settings }));
    return true;
  }

  if (message?.type === "show-once-listener-save-settings") {
    saveSettings(message.payload || {}).then(async () => {
      const settings = await getSettings();
      sendResponse({ ok: true, settings });
    });
    return true;
  }

  if (message?.type === "show-once-listener-reset-session") {
    saveSettings({ sessionId: crypto.randomUUID() }).then(async () => {
      const settings = await getSettings();
      sendResponse({ ok: true, settings });
    });
    return true;
  }

  if (message?.type === "show-once-listener-recording-uploaded") {
    sendResponse({ ok: true });
    return false;
  }

  return false;
});

chrome.tabs.onActivated.addListener(async ({ tabId, windowId }) => {
  try {
    const tab = await chrome.tabs.get(tabId);
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
    console.warn("Show Once Listener tab activation failed", error);
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
    console.warn("Show Once Listener navigation event failed", error);
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
    console.warn("Show Once Listener history event failed", error);
  }
});
