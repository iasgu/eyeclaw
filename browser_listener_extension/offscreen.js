let currentRecording = null;

function buildVideoConstraints(streamId) {
  return {
    mandatory: {
      chromeMediaSource: "tab",
      chromeMediaSourceId: streamId
    }
  };
}

async function uploadRecording(recording) {
  if (!recording || !recording.blob) {
    return;
  }

  const form = new FormData();
  form.append("session_id", recording.sessionId);
  form.append("tab_id", String(recording.tabId || ""));
  form.append("started_at_ms", String(recording.startedAtMs || ""));
  form.append("ended_at_ms", String(recording.endedAtMs || ""));
  form.append("mime_type", recording.blob.type || "video/webm");
  form.append("video", recording.blob, `session-${recording.sessionId}.webm`);

  const response = await fetch(`${recording.apiBase}/api/browser-listener/session-recording`, {
    method: "POST",
    body: form
  });

  if (!response.ok) {
    const text = await response.text();
    throw new Error(`Recording upload failed: ${response.status} ${text}`);
  }

  chrome.runtime.sendMessage({
    type: "show-once-listener-recording-uploaded",
    payload: {
      sessionId: recording.sessionId
    }
  });
}

async function stopCurrentRecording() {
  if (!currentRecording) {
    return;
  }

  const active = currentRecording;
  currentRecording = null;

  if (active.recorder && active.recorder.state !== "inactive") {
    await new Promise((resolve) => {
      active.recorder.addEventListener(
        "stop",
        () => {
          resolve();
        },
        { once: true }
      );
      active.recorder.stop();
    });
  }

  if (active.stream) {
    active.stream.getTracks().forEach((track) => track.stop());
  }

  active.endedAtMs = Date.now();
  active.blob = new Blob(active.chunks, { type: active.mimeType || "video/webm" });
  await uploadRecording(active);
}

async function startRecording(message) {
  if (currentRecording) {
    await stopCurrentRecording();
  }

  const media = await navigator.mediaDevices.getUserMedia({
    audio: false,
    video: buildVideoConstraints(message.streamId)
  });

  const chunks = [];
  const mimeType = MediaRecorder.isTypeSupported("video/webm;codecs=vp9")
    ? "video/webm;codecs=vp9"
    : "video/webm";
  const recorder = new MediaRecorder(media, { mimeType });
  recorder.ondataavailable = (event) => {
    if (event.data && event.data.size > 0) {
      chunks.push(event.data);
    }
  };
  recorder.start(1000);

  currentRecording = {
    apiBase: message.apiBase,
    sessionId: message.sessionId,
    tabId: message.tabId,
    startedAtMs: Date.now(),
    endedAtMs: null,
    stream: media,
    recorder,
    chunks,
    mimeType,
    blob: null
  };
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.target !== "offscreen") {
    return false;
  }

  if (message.type === "start-recording") {
    startRecording(message)
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }

  if (message.type === "stop-recording") {
    stopCurrentRecording()
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }

  return false;
});
