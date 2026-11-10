"use strict";

// A hover menu on a mouse; the native disclosure also supports click, touch, and keyboard.
document.querySelectorAll("[data-navigation]").forEach(menu => {
  const trigger = menu.querySelector("summary");
  let closeTimer;
  menu.addEventListener("toggle", () => trigger.setAttribute("aria-expanded", String(menu.open)));
  menu.addEventListener("pointerenter", event => {
    window.clearTimeout(closeTimer);
    if (event.pointerType === "mouse") menu.open = true;
  });
  menu.addEventListener("pointerleave", event => {
    if (event.pointerType === "mouse") closeTimer = window.setTimeout(() => {
      if (!menu.contains(document.activeElement)) menu.open = false;
    }, 140);
  });
  menu.addEventListener("focusout", () => window.setTimeout(() => {
    if (!menu.contains(document.activeElement) && !menu.matches(":hover")) menu.open = false;
  }, 0));
  document.addEventListener("pointerdown", event => {
    if (!menu.contains(event.target)) menu.open = false;
  });
  document.addEventListener("keydown", event => {
    if (event.key === "Escape" && menu.open) {
      menu.open = false;
      trigger.focus();
    }
  });
});

document.querySelectorAll("form[data-busy-form]").forEach(form => {
  form.addEventListener("invalid", event => {
    let disclosure = event.target.closest("details");
    while (disclosure) {
      disclosure.open = true;
      disclosure = disclosure.parentElement.closest("details");
    }
  }, true);
  form.addEventListener("submit", event => {
    if (form.dataset.submitting === "true") {
      event.preventDefault();
      return;
    }
    form.dataset.submitting = "true";
    form.setAttribute("aria-busy", "true");
    event.submitter?.classList.add("is-loading");
  });
});
window.addEventListener("pageshow", () => {
  document.querySelectorAll("form[data-busy-form]").forEach(form => {
    delete form.dataset.submitting;
    form.removeAttribute("aria-busy");
    form.querySelectorAll(".is-loading").forEach(button => button.classList.remove("is-loading"));
  });
});

const dropZone = document.querySelector("[data-drop-zone]");
if (dropZone) {
  const input = dropZone.querySelector('input[type="file"]');
  const caption = dropZone.querySelector("[data-file-caption]");
  const updateFile = () => {
    const file = input.files[0];
    caption.textContent = file ? file.name : "PDF, DOCX, TXT or MD · Up to 5 MB";
    dropZone.classList.toggle("has-file", Boolean(file));
  };
  input.addEventListener("change", updateFile);
  ["dragenter", "dragover"].forEach(name => dropZone.addEventListener(name, event => {
    event.preventDefault();
    dropZone.classList.add("is-dragging");
  }));
  ["dragleave", "drop"].forEach(name => dropZone.addEventListener(name, event => {
    event.preventDefault();
    dropZone.classList.remove("is-dragging");
  }));
  dropZone.addEventListener("drop", event => {
    if (event.dataTransfer.files.length) {
      const transfer = new DataTransfer();
      transfer.items.add(event.dataTransfer.files[0]);
      input.files = transfer.files;
      updateFile();
    }
  });
}

// Keep static headings readable without JavaScript or with reduced motion.
const typedLines = [...document.querySelectorAll("[data-typewriter]")];
let typingFinished = Promise.resolve();
if (typedLines.length && !window.matchMedia("(prefers-reduced-motion: reduce)").matches && !document.querySelector(".errorlist")) {
  let finishTyping;
  typingFinished = new Promise(resolve => { finishTyping = resolve; });
  const text = typedLines.map(line => line.textContent);
  typedLines.forEach(line => { line.textContent = ""; });
  const typeLine = (index, position = 0) => {
    const line = typedLines[index];
    line.setAttribute("data-typing", "");
    line.textContent = text[index].slice(0, position);
    if (position < text[index].length) window.setTimeout(() => typeLine(index, position + 1), Number(line.dataset.typeSpeed) || (index === 0 ? 65 : 45));
    else {
      line.removeAttribute("data-typing");
      if (index + 1 < typedLines.length) window.setTimeout(() => typeLine(index + 1), 160);
      else finishTyping();
    }
  };
  typeLine(0);
}

const interests = document.querySelector("[data-interests]");
if (interests) {
  const updateInterests = () => {
    const values = [...new Set(interests.value.split(/[,\n]/).map(value => value.trim().replace(/\s+/g, " ").toLowerCase()).filter(Boolean))];
    interests.setCustomValidity(values.length > 8 ? "Choose up to 8 interests." : values.some(value => value.split(" ").length > 3) ? "Keep each interest to one to three words." : "");
  };
  interests.addEventListener("input", updateInterests);
  updateInterests();
}

// The local demo uses the real intake form and the same preparation POST.
const janeDemoForm = document.querySelector("[data-jane-demo]");
janeDemoForm?.addEventListener("submit", event => {
  const data = document.getElementById("jane-demo-data");
  const form = document.querySelector(".setup-form");
  if (!data || !form) return;
  event.preventDefault();
  const values = JSON.parse(data.textContent);
  form.reset();
  Object.entries(values).forEach(([name, value]) => {
    form.querySelectorAll(`[name="${name}"]`).forEach(field => {
      if (field.type === "checkbox") field.checked = Array.isArray(value) && value.includes(field.value);
      else if (field.type !== "file") field.value = value;
      field.dispatchEvent(new Event("input", {bubbles: true}));
      field.dispatchEvent(new Event("change", {bubbles: true}));
    });
  });
  form.requestSubmit();
});

// Schools and areas share the same searchable dropdown and keyboard behavior.
document.querySelectorAll("[data-picker]").forEach(picker => {
  const input = picker.querySelector("input");
  const multiple = input.hasAttribute("data-area-picker");
  const panel = picker.querySelector("[data-picker-options]");
  const options = [...panel.querySelectorAll("[data-picker-value]")];
  const status = picker.querySelector("[data-picker-status]");
  const known = new Set(options.map(option => option.dataset.pickerValue.toLowerCase()));
  let visible = [];
  let active = -1;
  const selectedValues = () => (multiple ? input.value.split(",") : [input.value]).map(value => value.trim()).filter(Boolean);
  const close = () => {
    panel.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    active = -1;
  };
  const validate = () => {
    const values = selectedValues();
    input.setCustomValidity(multiple && values.length > 5 ? "Choose up to 5 areas." : values.some(value => !known.has(value.toLowerCase())) ? (multiple ? "Choose cities or regions from the list." : "Choose a school from the list.") : "");
  };
  const showOptions = () => {
    const pieces = multiple ? input.value.split(",") : [input.value];
    const query = pieces.pop().trim().toLowerCase();
    const selected = pieces.map(value => value.trim().toLowerCase()).filter(Boolean);
    visible = [];
    options.forEach(option => {
      const label = option.dataset.pickerValue.toLowerCase();
      option.hidden = selected.includes(label) || !label.includes(query) || (multiple && selected.length >= 5);
      option.setAttribute("aria-selected", "false");
      if (!option.hidden) visible.push(option);
    });
    active = -1;
    input.removeAttribute("aria-activedescendant");
    panel.hidden = visible.length === 0;
    input.setAttribute("aria-expanded", String(!panel.hidden));
    status.textContent = visible.length ? `${visible.length} ${multiple ? "areas" : "schools"} available.` : "No matching choices.";
  };
  const choose = option => {
    if (multiple) {
      const pieces = input.value.split(",");
      pieces.pop();
      const selected = pieces.map(value => value.trim()).filter(Boolean);
      if (!selected.some(value => value.toLowerCase() === option.dataset.pickerValue.toLowerCase()) && selected.length < 5) selected.push(option.dataset.pickerValue);
      input.value = selected.join(", ") + (selected.length < 5 ? ", " : "");
      status.textContent = `${selected.length} of 5 areas selected.`;
    } else {
      input.value = option.dataset.pickerValue;
      status.textContent = `${input.value} selected.`;
    }
    validate();
    input.focus();
    if (multiple) showOptions();
    else close();
  };
  options.forEach(option => option.addEventListener("pointerdown", event => {
    event.preventDefault();
    choose(option);
  }));
  input.addEventListener("focus", showOptions);
  input.addEventListener("click", showOptions);
  input.addEventListener("input", () => { validate(); showOptions(); });
  input.addEventListener("blur", () => { validate(); close(); });
  document.addEventListener("pointerdown", event => {
    if (!picker.contains(event.target)) close();
  });
  input.addEventListener("keydown", event => {
    if (event.key === "Escape") { close(); return; }
    if (event.key === "Enter" && !panel.hidden && active >= 0) {
      event.preventDefault(); choose(visible[active]); return;
    }
    if (!["ArrowDown", "ArrowUp"].includes(event.key)) return;
    event.preventDefault();
    if (panel.hidden) showOptions();
    if (!visible.length) return;
    active = active < 0 ? (event.key === "ArrowDown" ? 0 : visible.length - 1) : (active + (event.key === "ArrowDown" ? 1 : -1) + visible.length) % visible.length;
    visible.forEach((option, index) => option.setAttribute("aria-selected", String(index === active)));
    input.setAttribute("aria-activedescendant", visible[active].id);
    visible[active].scrollIntoView({block: "nearest"});
  });
  validate();
});

const graduation = document.querySelector('[name="graduation_status"]');
const updateGraduation = () => {
  const target = document.querySelector("[data-graduation-fields]");
  if (!target) return;
  const relevant = ["graduated", "studying"].includes(graduation.value);
  target.hidden = !relevant;
  target.querySelectorAll("input").forEach(input => { input.disabled = !relevant; });
};
graduation?.addEventListener("change", updateGraduation);
if (graduation) updateGraduation();
const startMode = document.querySelector('[name="start_mode"]');
const updateStart = () => {
  const target = document.querySelector("[data-start-date]");
  if (!target) return;
  const relevant = startMode.value === "date";
  target.hidden = !relevant;
  target.querySelectorAll("input").forEach(input => { input.disabled = !relevant; });
};
startMode?.addEventListener("change", updateStart);
if (startMode) updateStart();

document.querySelectorAll("[data-add-form]").forEach(button => {
  button.addEventListener("click", () => {
    const prefix = button.dataset.addForm;
    const total = document.getElementById(`id_${prefix}-TOTAL_FORMS`);
    const maximum = Number(document.getElementById(`id_${prefix}-MAX_NUM_FORMS`).value);
    const index = Number(total.value);
    if (index >= maximum) return;
    const fragment = document.getElementById(`${prefix}-empty`).content.cloneNode(true);
    fragment.querySelectorAll("*").forEach(element => {
      ["id", "name", "for", "aria-describedby"].forEach(attribute => {
        if (element.hasAttribute(attribute)) {
          element.setAttribute(attribute, element.getAttribute(attribute).replaceAll("__prefix__", String(index)));
        }
      });
    });
    document.querySelector(`[data-formset="${prefix}"]`).append(fragment);
    total.value = index + 1;
    if (index + 1 >= maximum) button.disabled = true;
  });
});

// Only read status while waiting. Network errors never repeat a paid operation.
async function followTask(url, {update, complete, failed}) {
  try {
    const response = await fetch(url, {headers: {Accept: "application/json"}, cache: "no-store"});
    if (!response.ok) throw new Error("Status unavailable");
    const result = await response.json();
    update(result);
    if (result.status === "complete") { await complete(result); return; }
    if (result.status === "failed") { failed(result); return; }
  } catch {
    update({connectionLost: true});
  }
  window.setTimeout(() => followTask(url, {update, complete, failed}), 1500);
}

function initialiseInterview(deck) {
  const form = deck.querySelector("[data-answer-form]");
  if (!form) return;
  // Named action controls shadow form.action; always read the destination attribute.
  const interviewUrl = form.getAttribute("action");
  const status = deck.querySelector("[data-answer-status]");
  const progress = deck.querySelector("[data-question-progress]");
  const questionNumber = Number(deck.dataset.questionNumber) || 1;
  const questionLimit = Number(deck.dataset.questionLimit) || 6;
  if (progress) progress.style.width = `${Math.min(100, questionNumber / questionLimit * 100)}%`;
  let pending = false;

  deck.addEventListener("keydown", event => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.isComposing && event.target.closest("[data-answer-form]")) {
      event.preventDefault();
      if (!pending) form.requestSubmit(form.querySelector('[value="answer"]'));
    }
  });

  const setPending = value => {
    pending = value;
    form?.setAttribute("aria-busy", String(value));
    const answer = form?.querySelector("textarea");
    if (answer) answer.readOnly = value;
    deck.querySelector(".interview-continue")?.classList.toggle("is-loading", value);
    deck.querySelectorAll('[data-answer-form] button').forEach(button => { button.disabled = value; });
    status?.classList.toggle("pending", value);
  };
  const replaceDeck = async () => {
    const response = await fetch(interviewUrl, {headers: {Accept: "text/html"}, cache: "no-store"});
    if (!response.ok) throw new Error("Next question unavailable");
    const documentFragment = new DOMParser().parseFromString(await response.text(), "text/html");
    const updated = documentFragment.querySelector("[data-interview]");
    if (!updated) { window.location.assign(response.url); return; }
    deck.replaceWith(updated);
    initialiseInterview(updated);
    if (Number(updated.dataset.questionNumber) > questionNumber) updated.querySelector("[data-current]")?.classList.add("slide-enter");
    window.scrollTo({top: 0, behavior: "instant"});
    updated.querySelector("#answer")?.focus({preventScroll: true});
  };
  const wait = (taskUrl, activityUrl) => {
    setPending(true);
    followTask(taskUrl, {
      update: result => {
        if (status) status.textContent = result.connectionLost ? "Reconnecting to your saved progress…" : "Reading your answer and choosing the next question…";
      },
      complete: replaceDeck,
      failed: () => window.location.assign(activityUrl),
    });
  };

  form?.addEventListener("submit", async event => {
    event.preventDefault();
    if (pending) return;
    const action = event.submitter?.value || "answer";
    const answer = form.querySelector("textarea");
    if (action === "answer" && answer.value.trim().length < 8) {
      status.textContent = "Add a little detail before continuing.";
      answer.focus();
      return;
    }
    const data = new FormData(form);
    data.set("action", action);
    setPending(true);
    status.textContent = "Reading your answer…";
    try {
      const response = await fetch(interviewUrl, {method: "POST", body: data, headers: {Accept: "application/json"}, cache: "no-store"});
      if (response.status === 202) {
        const result = await response.json();
        wait(result.task_url, result.activity_url);
      } else if (response.ok && response.headers.get("Content-Type")?.includes("text/html")) {
        const documentFragment = new DOMParser().parseFromString(await response.text(), "text/html");
        const updated = documentFragment.querySelector("[data-interview]");
        if (updated) { deck.replaceWith(updated); initialiseInterview(updated); }
        else window.location.assign(response.url);
      } else {
        throw new Error("Answer status uncertain");
      }
    } catch {
      status.textContent = "Connection interrupted. Check saved progress before sending again.";
      const link = document.createElement("a");
      link.className = "text-link";
      link.href = "/";
      link.textContent = "Check progress";
      status.append(" ", link);
    }
  });
  if (deck.dataset.pendingTask) wait(deck.dataset.pendingTask, deck.dataset.pendingActivity);
}
document.querySelectorAll("[data-interview]").forEach(initialiseInterview);
document.querySelector("[data-auto-prepare]")?.requestSubmit();

const activity = document.querySelector("[data-task-url]");
if (activity) {
  const message = activity.querySelector("[data-task-message]");
  const log = activity.querySelector("[data-event-log]");
  let previousEvents = "";
  followTask(activity.dataset.taskUrl, {
    update: result => {
      if (!message) return;
      if (result.connectionLost) {
        message.textContent = "Reconnecting to the agent. Your progress is saved.";
        return;
      }
      message.textContent = result.message;
      const history = (result.events || []).slice(0, -1).slice(-3);
      const signature = JSON.stringify(history);
      if (log && signature !== previousEvents) {
        previousEvents = signature;
        log.replaceChildren(...history.map(event => {
          const line = document.createElement("p");
          const mark = document.createElement("span");
          mark.setAttribute("aria-hidden", "true");
          mark.textContent = "✓";
          line.append(mark, document.createTextNode(event.message));
          return line;
        }));
      }
    },
    complete: async result => {
      await typingFinished;
      window.location.replace(result.destination);
    },
    failed: () => window.location.reload(),
  });
}

const errors = document.querySelector(".errorlist");
if (errors) {
  errors.setAttribute("role", "alert");
  let disclosure = errors.closest("details");
  while (disclosure) { disclosure.open = true; disclosure = disclosure.parentElement.closest("details"); }
  const input = errors.closest(".field")?.querySelector("input, select, textarea");
  if (input) input.focus();
  else errors.scrollIntoView({block: "center"});
}
