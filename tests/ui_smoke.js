"use strict";

const assert = require("node:assert/strict");

class FakeElement {
  constructor(id = "") {
    this.id = id;
    this.hidden = false;
    this.textContent = "";
    this.value = id === "ratePicker" ? "1000" : id === "viewMode" ? "single" : "";
    this.dataset = {};
    this.style = {};
    this.children = [];
    this.listeners = {};
    this.lastChild = { textContent: "" };
    this.classList = { toggle() {} };
    this.option = { disabled: false };
    this.context = {
      strokes: [],
      strokeStyle: "",
      setTransform() {}, clearRect() {}, save() {}, setLineDash() {}, beginPath() {},
      moveTo() {}, lineTo() {}, restore() {},
      stroke() { this.strokes.push(this.strokeStyle); },
    };
  }

  addEventListener(name, handler) { this.listeners[name] = handler; }
  querySelector(selector) {
    if (selector === 'option[value="dual"]') return this.option;
    return new FakeElement();
  }
  replaceChildren(...children) {
    this.children = children;
    const selected = children.find((child) => child.selected);
    this.value = selected?.value || children[0]?.value || "";
  }
  getBoundingClientRect() { return { width: 600, height: 300 }; }
  getContext() { return this.context; }
}

const elements = new Map();
const getElement = (id) => {
  if (!elements.has(id)) elements.set(id, new FakeElement(id));
  return elements.get(id);
};
const windowLabels = [new FakeElement(), new FakeElement()];

global.document = {
  getElementById: getElement,
  createElement: () => new FakeElement(),
  querySelectorAll: (selector) => selector === ".window-label" ? windowLabels : [],
};
global.window = { devicePixelRatio: 1, addEventListener() {} };
global.setInterval = () => 1;
global.clearInterval = () => {};
global.fetch = async () => ({
  ok: true,
  json: async () => ({
    timestamp: 1_700_000_000_000,
    hostname: "test-host",
    demo: false,
    gpus: [
      { id: "gpu-0", index: 0, name: "GPU Zero", utilization: 72, memoryPercent: 31,
        temperatureC: 58, powerW: 150, powerLimitW: 300 },
      { id: "gpu-1", index: 1, name: "GPU One", utilization: 44, memoryPercent: 19,
        temperatureC: 49, powerW: 100, powerLimitW: 250 },
    ],
  }),
});

require("../static/app.js");

setImmediate(() => {
  const mode = getElement("viewMode");
  mode.value = "dual";
  mode.listeners.change();

  assert.equal(getElement("singleOverview").hidden, true);
  assert.equal(getElement("dualOverview").hidden, false);
  assert.equal(getElement("singleChartCard").hidden, true);
  assert.equal(getElement("dualCharts").hidden, false);
  assert.equal(getElement("dualLoadA").textContent, "72");
  assert.equal(getElement("dualLoadB").textContent, "44");
  assert.ok(getElement("dualHistoryChartA").context.strokes.includes("#b48cff"));
  assert.ok(getElement("dualHistoryChartB").context.strokes.includes("#b48cff"));

  mode.value = "single";
  mode.listeners.change();
  assert.equal(getElement("singleOverview").hidden, false);
  assert.equal(getElement("dualOverview").hidden, true);
  assert.equal(getElement("singleChartCard").hidden, false);
  assert.equal(getElement("dualCharts").hidden, true);
  console.log("UI mode and chart smoke test: OK");
});
