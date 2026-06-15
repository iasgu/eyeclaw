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

function closestOptionElement(element) {
  if (!(element instanceof Element)) {
    return null;
  }
  return element.closest(
    [
      "[role='option']",
      "[role='menuitem']",
      ".el-select-dropdown__item",
      ".el-cascader-node",
      ".el-dropdown-menu__item",
      ".ant-select-item-option",
      ".ant-cascader-menu-item",
      ".select2-results__option",
      "option"
    ].join(",")
  );
}

function closestDropdownTrigger(element) {
  if (!(element instanceof Element)) {
    return null;
  }
  return element.closest(
    [
      "select",
      "[role='combobox']",
      "[aria-haspopup='listbox']",
      ".el-select",
      ".ant-select",
      ".select2-container"
    ].join(",")
  );
}

function targetSummary(target) {
  if (!(target instanceof HTMLElement)) {
    return {};
  }

  const optionElement = closestOptionElement(target);
  const summaryTarget = optionElement instanceof HTMLElement ? optionElement : target;
  const textSource =
    summaryTarget.innerText ||
    summaryTarget.textContent ||
    summaryTarget.getAttribute("aria-label") ||
    summaryTarget.getAttribute("placeholder") ||
    summaryTarget.getAttribute("name");

  return {
    target_text: trimText(textSource),
    target_selector: trimText(buildSelector(summaryTarget), 300),
    target_tag: trimText(summaryTarget.tagName?.toLowerCase(), 40),
    target_type: trimText(summaryTarget.getAttribute("type") || summaryTarget.getAttribute("role"), 40)
  };
}

function scrubInputValue(target) {
  if (target instanceof HTMLSelectElement) {
    const selected = target.selectedOptions && target.selectedOptions.length ? target.selectedOptions[0] : null;
    return trimText(selected?.textContent || selected?.value || target.value, 120);
  }
  if (!(target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement)) {
    return null;
  }
  if (target instanceof HTMLInputElement && target.type === "password") {
    return null;
  }
  return trimText(target.value, 120);
}

function dropdownDetails(target) {
  if (!(target instanceof HTMLElement)) {
    return {};
  }

  const details = {};
  if (target instanceof HTMLSelectElement) {
    const selected = target.selectedOptions && target.selectedOptions.length ? target.selectedOptions[0] : null;
    details.selected_option_text = trimText(selected?.textContent || "", 160);
    details.selected_option_value = trimText(selected?.value || target.value || "", 160);
    details.selected_option_index = target.selectedIndex;
    details.dropdown_selector = trimText(buildSelector(target), 300);
    details.dropdown_kind = "native-select";
    return details;
  }

  const optionElement = closestOptionElement(target);
  if (optionElement instanceof HTMLElement) {
    const options = Array.from(optionElement.parentElement?.children || []);
    details.option_text = trimText(optionElement.innerText || optionElement.textContent || "", 160);
    details.option_selector = trimText(buildSelector(optionElement), 300);
    details.option_index = options.indexOf(optionElement);
    details.dropdown_kind = "custom-option";
  }

  const trigger = closestDropdownTrigger(target);
  if (trigger instanceof HTMLElement) {
    details.dropdown_selector = trimText(buildSelector(trigger), 300);
    details.dropdown_expanded = trimText(trigger.getAttribute("aria-expanded") || "", 40);
    details.dropdown_kind = details.dropdown_kind || "custom-trigger";
  }

  return details;
}

function normalizeShortcut(event) {
  if (!(event instanceof KeyboardEvent) || event.isComposing || event.repeat) {
    return null;
  }

  const rawKey = event.key || "";
  const key = rawKey.length === 1 ? rawKey.toUpperCase() : rawKey;
  const loweredKey = rawKey.toLowerCase();
  const modifierPressed = event.ctrlKey || event.metaKey || event.altKey;
  const isFunctionKey = /^F\d{1,2}$/.test(key);

  if (!modifierPressed && !isFunctionKey) {
    return null;
  }
  if (["control", "shift", "alt", "meta"].includes(loweredKey)) {
    return null;
  }

  const parts = [];
  if (event.ctrlKey) parts.push("Ctrl");
  if (event.metaKey) parts.push("Meta");
  if (event.altKey) parts.push("Alt");
  if (event.shiftKey) parts.push("Shift");
  parts.push(key === " " ? "Space" : key);
  return parts.join("+");
}

function emit(payload) {
  try {
    chrome.runtime.sendMessage(
      {
        type: "eyeclaw-listener-event",
        payload: {
          page_url: location.href,
          page_title: document.title,
          client_timestamp_ms: Date.now(),
          ...payload
        }
      },
      () => {
        if (chrome.runtime.lastError) {
          return;
        }
      }
    );
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
        y: Math.round(event.clientY),
        ...dropdownDetails(target)
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
        checked: target instanceof HTMLInputElement ? target.checked : null,
        ...dropdownDetails(target)
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
        checked: target instanceof HTMLInputElement ? target.checked : null,
        ...dropdownDetails(target)
      }
    });
  },
  true
);

document.addEventListener(
  "keydown",
  (event) => {
    const shortcut = normalizeShortcut(event);
    if (!shortcut) {
      return;
    }

    const target = event.target instanceof HTMLElement ? event.target : null;
    const summary = targetSummary(target);
    emit({
      event_type: "keyboard_shortcut",
      key_candidate: true,
      target_text: shortcut,
      target_selector: summary.target_selector,
      target_tag: summary.target_tag,
      target_type: summary.target_type || "keyboard-shortcut",
      input_value: shortcut,
      details: {
        shortcut,
        key: trimText(event.key, 40),
        code: trimText(event.code, 40),
        ctrl_key: event.ctrlKey,
        shift_key: event.shiftKey,
        alt_key: event.altKey,
        meta_key: event.metaKey,
        active_target_text: summary.target_text
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

function isTrustedEyeclawMessage(payload) {
  if (!payload || payload.source !== "eyeclaw-user-app") {
    return false;
  }
  return [
    "eyeclaw-listener-start-session-inline",
    "eyeclaw-listener-stop-session-inline",
    "eyeclaw-listener-get-settings"
  ].includes(payload.type);
}

window.addEventListener("message", (event) => {
  if (event.source !== window || !isTrustedEyeclawMessage(event.data)) {
    return;
  }

  chrome.runtime.sendMessage(
    {
      type: event.data.type,
      payload: event.data.payload || {}
    },
    (response) => {
      const runtimeError = chrome.runtime.lastError;
      window.postMessage(
        {
          source: "eyeclaw-listener-extension",
          type: `${event.data.type}-response`,
          ok: !runtimeError && Boolean(response?.ok),
          response: response || null,
          error: runtimeError ? runtimeError.message : response?.error || null,
          requestId: event.data.requestId || null
        },
        "*"
      );
    }
  );
});
