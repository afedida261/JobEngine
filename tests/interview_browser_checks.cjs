"use strict";

// Run the shipped event handlers without a browser or model dependency. In particular,
// preserve the HTML form's named-property behavior when several controls are named action.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

class Element {
  constructor() {
    this.dataset = {};
    this.attrs = {};
    this.handlers = {};
    this.classes = new Set();
    this.classList = {
      add: (...names) => names.forEach(name => this.classes.add(name)),
      remove: (...names) => names.forEach(name => this.classes.delete(name)),
      toggle: (name, active) => active ? this.classes.add(name) : this.classes.delete(name),
    };
  }
  addEventListener(name, handler) { this.handlers[name] = handler; }
  setAttribute(name, value) { this.attrs[name] = value; }
  getAttribute(name) { return this.attrs[name] ?? null; }
  removeAttribute(name) { delete this.attrs[name]; }
  closest() { return null; }
  focus() { this.focused = true; }
  append() {}
}

function makeDeck(count) {
  const deck = new Element();
  deck.dataset.questionNumber = String(count);
  deck.slides = Array.from({length: count}, () => {
    const slide = new Element();
    slide.heading = new Element();
    slide.querySelector = () => slide.heading;
    return slide;
  });
  deck.dots = deck.slides.map((_, index) => {
    const dot = new Element();
    dot.dataset.slideTarget = String(index);
    return dot;
  });
  deck.previous = deck.slides.map(() => new Element());
  deck.next = deck.slides.map(() => new Element());
  deck.status = new Element();
  deck.viewport = new Element();
  deck.counter = new Element();
  deck.progress = new Element();
  deck.progress.style = {};
  deck.answer = new Element();
  deck.answer.value = "I enjoy coordinating useful work with a team.";
  deck.answer.scrollHeight = deck.answer.clientHeight = 100;
  deck.answer.scrollTop = 0;
  deck.submit = new Element();
  deck.submit.value = "answer";
  deck.skip = new Element();
  deck.skip.value = "skip";
  deck.form = new Element();
  deck.form.setAttribute("action", "/conversation/");
  deck.form.action = {toString: () => "[object RadioNodeList]"};
  deck.form.querySelector = selector => selector === "textarea" ? deck.answer : deck.submit;
  deck.form.requestSubmit = button => { deck.submitted = button; };
  const elements = {
    "[data-answer-form]": deck.form,
    "[data-answer-status]": deck.status,
    "[data-question-counter]": deck.counter,
    "[data-question-progress]": deck.progress,
    ".deck-viewport": deck.viewport,
    ".interview-continue": deck.submit,
    "#answer": deck.answer,
    "[data-current]": deck.slides.at(-1),
  };
  deck.querySelector = selector => elements[selector] ?? null;
  deck.querySelectorAll = selector => ({
    "[data-slide]": deck.slides,
    "[data-slide-target]": deck.dots,
    "[data-previous]": deck.previous,
    "[data-next]": deck.next,
    "[data-answer-form] button": [deck.submit, deck.skip],
  })[selector] ?? [];
  deck.replaceWith = replacement => { deck.replacement = replacement; };
  return deck;
}

function runtime({typedLine = null, reducedMotion = false} = {}) {
  const requests = [];
  const timers = [];
  const windowEvents = {};
  const context = {
    document: {
      querySelectorAll: selector => selector === "[data-typewriter]" && typedLine ? [typedLine] : [],
      querySelector: () => null,
      addEventListener() {}, createElement: () => new Element(),
    },
    window: {
      addEventListener(name, callback) { windowEvents[name] = callback; },
      getSelection: () => "", scrollTo() {},
      matchMedia: () => ({matches: reducedMotion}), location: {assign() {}},
      setTimeout(callback, delay) {
        if (!typedLine) throw new Error("Unexpected repeat poll");
        timers.push({callback, delay});
      },
    },
    FormData: class {
      constructor(form) { this.values = {revision: "1"}; }
      set(name, value) { this.values[name] = value; }
    },
    DOMParser: class {
      parseFromString() { return {querySelector: () => context.nextDeck}; }
    },
    fetch: async (url, options = {}) => {
      requests.push({url: String(url), method: options.method || "GET", body: options.body});
      if (url !== "/conversation/" && url !== "/status/") return {status: 404, ok: false};
      if (options.method === "POST") return {
        status: 202, json: async () => ({task_url: "/status/", activity_url: "/activity/"}),
      };
      if (url === "/status/") return {ok: true, json: async () => ({status: "complete"})};
      return {ok: true, text: async () => "next question", url: "/conversation/"};
    },
    Date,
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../jobengine/web/static/jobengine/app.js"), "utf8"), context);
  return {context, requests, timers, windowEvents};
}

function checkResultsTypewriter(reducedMotion) {
  const line = new Element();
  const title = "Your search is complete.";
  line.textContent = title;
  line.dataset.typeSpeed = "35";
  const {timers, requests, windowEvents} = runtime({typedLine: line, reducedMotion});
  if (reducedMotion) {
    assert.equal(line.textContent, title);
    assert.equal(timers.length, 0);
  } else {
    assert.equal(line.textContent, "");
    let steps = 0;
    while (timers.length) {
      const timer = timers.shift();
      assert.equal(timer.delay, 35);
      timer.callback();
      assert.ok(++steps <= title.length, "The title must finish without looping");
    }
    assert.equal(line.textContent, title);
    assert.equal(line.getAttribute("data-typing"), null);
  }
  windowEvents.pageshow();
  assert.equal(timers.length, 0, "Page restoration must not restart the animation");
  assert.equal(requests.length, 0);
}

async function checkSubmission(action) {
  const {context, requests} = runtime();
  const deck = makeDeck(2);
  context.nextDeck = makeDeck(3);
  context.initialiseInterview(deck);
  await deck.form.handlers.submit({preventDefault() {}, submitter: action === "skip" ? deck.skip : deck.submit});
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(!deck.status.textContent.includes("Connection interrupted"), `${action} shows a false connection failure`);
  assert.deepEqual(requests.map(({url, method}) => [url, method]), [
    ["/conversation/", "POST"], ["/status/", "GET"], ["/conversation/", "GET"],
  ]);
  assert.equal(requests[0].body.values.action, action);
  assert.equal(deck.replacement, context.nextDeck);
  assert.equal(context.nextDeck.slides[2].classes.has("slide-enter"), true);
  assert.equal(context.nextDeck.answer.focused, true);
}

function checkKeyboardWithoutHistoryNavigation() {
  const {context} = runtime();
  const deck = makeDeck(2);
  context.initialiseInterview(deck);
  assert.equal(deck.handlers.wheel, undefined);
  assert.equal(deck.viewport.handlers.pointerup, undefined);
  assert.ok(deck.previous.every(button => !button.handlers.click));
  assert.ok(deck.next.every(button => !button.handlers.click));
  deck.handlers.keydown({key: "Enter", ctrlKey: true, target: {closest: () => deck.form}, preventDefault() {}});
  assert.equal(deck.submitted, deck.submit);
}

(async () => {
  await checkSubmission("answer");
  await checkSubmission("skip");
  checkKeyboardWithoutHistoryNavigation();
  checkResultsTypewriter(false);
  checkResultsTypewriter(true);
  console.log("PASS: interview/navigation handlers and one-shot, reduced-motion-aware results title.");
})().catch(error => { console.error(error); process.exitCode = 1; });
