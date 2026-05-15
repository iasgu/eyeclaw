function trimText(value, limit = 240) {
  if (typeof value !== "string") {
    return value ?? null;
  }
  const text = value.replace(/\s+/g, " ").trim();
  if (!text) {
    return null;
  }
  if (text.length <= limit) {
    return text;
  }
  return `${text.slice(0, limit - 3)}...`;
}

function buildSelector(element) {
  if (!(element instanceof Element)) {
    return null;
  }

  if (element.id) {
    return `#${element.id}`;
  }

  const parts = [];
  let current = element;
  let depth = 0;
  while (current && current.nodeType === Node.ELEMENT_NODE && depth < 4) {
    let part = current.tagName.toLowerCase();
    if (current.classList.length) {
      part += `.${Array.from(current.classList).slice(0, 2).join(".")}`;
    }
    const parent = current.parentElement;
    if (parent) {
      const siblings = Array.from(parent.children).filter((node) => node.tagName === current.tagName);
      if (siblings.length > 1) {
        const index = siblings.indexOf(current) + 1;
        part += `:nth-of-type(${index})`;
      }
    }
    parts.unshift(part);
    current = parent;
    depth += 1;
  }
  return parts.join(" > ");
}

function targetSummary(target) {
  if (!(target instanceof HTMLElement)) {
    return {};
  }

  const textSource =
    target.innerText ||
    target.textContent ||
    target.getAttribute("aria-label") ||
    target.getAttribute("placeholder") ||
    target.getAttribute("name");

  return {
    target_text: trimText(textSource),
    target_selector: trimText(buildSelector(target), 300),
    target_tag: trimText(target.tagName?.toLowerCase(), 40),
    target_type: trimText(target.getAttribute("type") || target.getAttribute("role"), 40)
  };
}

function scrubInputValue(target) {
  if (!(target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement)) {
    return null;
  }
  if (target instanceof HTMLInputElement && target.type === "password") {
    return null;
  }
  return trimText(target.value, 120);
}

function emit(payload) {
  try {
    chrome.runtime.sendMessage({
      type: "eyeclaw-listener-event",
      payload: {
        page_url: location.href,
        page_title: document.title,
        client_timestamp_ms: Date.now(),
        ...payload
      }
    });
  } catch (error) {
    console.warn("Eyeclaw Listener emit failed", error);
  }
}

let lastScrollSentAt = 0;
let lastScrollY = window.scrollY;

document.addEventListener(
  "click",
  (event) => {
    const target = event.target instanceof HTMLElement ? event.target : null;
    emit({
      event_type: "click",
      key_candidate: true,
      ...targetSummary(target),
      details: {
        button: event.button,
        x: Math.round(event.clientX),
        y: Math.round(event.clientY)
      }
    });
  },
  true
);

document.addEventListener(
  "input",
  (event) => {
    const target = event.target instanceof HTMLElement ? event.target : null;
    const inputValue = scrubInputValue(target);
    emit({
      event_type: "input",
      key_candidate: Boolean(inputValue),
      ...targetSummary(target),
      input_value: inputValue,
      details: {
        checked: target instanceof HTMLInputElement ? target.checked : null
      }
    });
  },
  true
);

document.addEventListener(
  "change",
  (event) => {
    const target = event.target instanceof HTMLElement ? event.target : null;
    emit({
      event_type: "change",
      key_candidate: true,
      ...targetSummary(target),
      input_value: scrubInputValue(target),
      details: {
        checked: target instanceof HTMLInputElement ? target.checked : null
      }
    });
  },
  true
);

window.addEventListener(
  "scroll",
  () => {
    const now = Date.now();
    if (now - lastScrollSentAt < 500) {
      return;
    }
    const nextScrollY = Math.round(window.scrollY);
    const deltaY = nextScrollY - lastScrollY;
    emit({
      event_type: "scroll",
      key_candidate: Math.abs(deltaY) >= 400 || Math.abs(nextScrollY) >= 600,
      scroll_x: Math.round(window.scrollX),
      scroll_y: nextScrollY,
      delta_y: deltaY
    });
    lastScrollY = nextScrollY;
    lastScrollSentAt = now;
  },
  { passive: true }
);

window.addEventListener("focus", () => {
  emit({ event_type: "focus", key_candidate: false });
});

document.addEventListener("visibilitychange", () => {
  emit({
    event_type: "visibility",
    key_candidate: false,
    details: {
      visibility_state: document.visibilityState
    }
  });
});

window.addEventListener("load", () => {
  emit({ event_type: "page_loaded", key_candidate: true });
});

const originalPushState = history.pushState;
history.pushState = function pushStateListener(...args) {
  const result = originalPushState.apply(this, args);
  emit({
    event_type: "history",
    key_candidate: true,
    details: {
      route_source: "pushState"
    }
  });
  return result;
};

const originalReplaceState = history.replaceState;
history.replaceState = function replaceStateListener(...args) {
  const result = originalReplaceState.apply(this, args);
  emit({
    event_type: "history",
    key_candidate: true,
    details: {
      route_source: "replaceState"
    }
  });
  return result;
};

window.addEventListener("hashchange", () => {
  emit({
    event_type: "history",
    key_candidate: true,
    details: {
      route_source: "hashchange"
    }
  });
});

window.addEventListener("popstate", () => {
  emit({
    event_type: "history",
    key_candidate: true,
    details: {
      route_source: "popstate"
    }
  });
});
