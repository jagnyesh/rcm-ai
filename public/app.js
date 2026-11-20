// Recorded mode: everything comes from demo-cases.json, written by `rcm-ai export-demo`.
"use strict";

const REPO = "https://github.com/jagnyesh/rcm-ai/blob/main/data/fixtures/";
const ARMS = [
  ["graph", "The graph decides on each case's own evidence, as a perfect extractor would return it. No model call."],
  ["extracted", "The model extracts the evidence from the note; the graph decides on it."],
  ["llm-only", "The model decides alone, without the graph."],
];

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

const outcome = (o) => el("span", { class: `outcome o-${o}` }, o ? o.replace("_", " ") : "n/a");

function sourceLine(src, anchor) {
  if (!src) return el("div", { class: "src fail" }, "unsourced: no source row backs this step");
  return el("div", { class: "src" },
    el("a", { href: `${REPO}${src.path}#L${src.line}`, target: "_blank", rel: "noopener" }, `${src.path}:${src.line}`),
    ` [${src.version}]`,
    anchor ? el("div", {}, "look for: ", el("q", {}, anchor)) : null);
}

function highlightedNote(note, evidence) {
  const spans = [];
  if (evidence) {
    for (const d of evidence.diagnoses) spans.push(d.span);
    for (const c of evidence.criteria) spans.push(c.span);
    for (const c of evidence.criteria_not_met) spans.push(c.span);
  }
  const ranges = [];
  for (const s of spans) {
    const i = note.indexOf(s);
    if (i >= 0) ranges.push([i, i + s.length]);
  }
  ranges.sort((a, b) => a[0] - b[0]);
  const out = el("p", { class: "note" });
  let at = 0;
  for (const [start, end] of ranges) {
    if (start < at) continue;
    out.append(note.slice(at, start), el("mark", {}, note.slice(start, end)));
    at = end;
  }
  out.append(note.slice(at));
  return out;
}

function evidenceList(ev) {
  if (!ev) return el("p", { class: "faint" }, "No evidence block.");
  const items = [
    ...ev.diagnoses.map((d) => el("li", {}, el("code", {}, d.code), " documented: ", el("q", {}, d.span))),
    ...ev.criteria.map((c) => el("li", {}, el("code", {}, c.criterion), " met: ", el("q", {}, c.span))),
    ...ev.criteria_not_met.map((c) => el("li", {}, el("code", {}, c.criterion), " not met: ", el("q", {}, c.span))),
  ];
  return el("ul", {}, items);
}

function verdictParts(v) {
  const parts = [];
  if (v.path.length) {
    parts.push(el("h3", {}, "Why: each step and its source"),
      el("ol", { class: "steps" }, v.path.map((h) => el("li", {}, h.statement, sourceLine(h.source, h.anchor)))));
  }
  if (v.reasons.length) {
    parts.push(el("h3", {}, "Reason"),
      el("ul", {}, v.reasons.map((r) => el("li", {}, el("code", {}, r.kind), " ", r.statement,
        r.span ? el("div", { class: "src" }, "note: ", el("q", {}, r.span)) : null,
        r.source ? sourceLine(r.source, r.anchor) : null))));
  }
  if (v.findings.length) {
    parts.push(el("h3", {}, "Findings"),
      el("ul", {}, v.findings.map((f) => el("li", {}, el("code", {}, f.type), " ", f.statement,
        f.source ? sourceLine(f.source, f.anchor) : null))));
  }
  return parts;
}

function checkLine(chk) {
  return el("span", { class: chk.passed ? "ok" : "fail" },
    chk.passed ? `Citation check passed: ${chk.citations.join(", ") || "nothing cited"}.` : `Citation check failed: ${chk.unsupported.join(", ")} unsupported.`);
}

function renderCase(c) {
  const v = c.verdict;
  const claim = c.claim;
  const parts = [
    el("h3", {}, `${c.id} `, outcome(v.outcome)),
    el("p", {}, `Claim: ${claim.hcpcs} for ${claim.icd10.join(", ")}, denied with CARC ${claim.carc} (${claim.coverage} coverage).`),
    el("p", {}, "Label: ", outcome(c.gold.verdict), c.gold.reason ? ` ${c.gold.reason}` : ""),
    el("h3", {}, "The note"),
    highlightedNote(c.note, c.evidence),
    el("details", {}, el("summary", {}, "Evidence spans"), evidenceList(c.evidence)),
  ];
  parts.push(...verdictParts(v));
  parts.push(el("h3", {}, "Each arm"), el("ul", {}, ARMS.map(([arm]) => {
    const o = c.arms[arm];
    const agrees = o === c.gold.verdict;
    return el("li", {}, `${arm}: `, outcome(o), " ", el("span", { class: agrees ? "ok" : "fail" }, agrees ? "agrees with the label" : "differs from the label"));
  })));
  if (c.letter) {
    const chk = c.letter.check;
    parts.push(
      el("h3", {}, "Appeal letter"),
      el("p", { class: "legend" }, `Recorded ${c.letter.recorded} from ${c.letter.model} (${c.letter.arm} arm), not drafted now. `,
        checkLine(chk)),
      el("div", { class: "letter", tabindex: "0" }, c.letter.text));
  }
  document.getElementById("case").replaceChildren(...parts);
}

function dots(c) {
  return el("span", { class: "dots", "aria-label": ARMS.map(([a]) => `${a} ${c.arms[a] === c.gold.verdict ? "agrees" : "differs"}`).join(", ") },
    ARMS.map(([a]) => el("span", { class: c.arms[a] === c.gold.verdict ? "ok" : "fail" }, "●")));
}

function renderList(cases, select) {
  const list = document.getElementById("case-list");
  const buttons = cases.map((c) => {
    const b = el("button", { type: "button", "data-id": c.id },
      el("span", { class: "id" }, c.id, c.featured ? el("span", { class: "star" }, " ★") : null),
      outcome(c.verdict.outcome), dots(c));
    b.addEventListener("click", () => select(c.id));
    return el("li", {}, b);
  });
  list.replaceChildren(...buttons);
}

function renderSources(sources) {
  const body = document.querySelector("#sources tbody");
  body.replaceChildren(...sources.map((s) => {
    const complete = s.expected_records === s.in_graph && s.rejected === 0;
    return el("tr", {},
      el("td", {}, el("a", { href: REPO + s.path, target: "_blank", rel: "noopener" }, s.path)),
      el("td", {}, s.version),
      el("td", { class: "num" }, s.retrieved),
      el("td", { class: "num" }, s.expected_records),
      el("td", { class: "num" }, s.parsed),
      el("td", { class: `num ${s.rejected ? "fail" : ""}` }, s.rejected),
      el("td", { class: `num ${complete ? "ok" : "fail"}` }, s.in_graph, complete ? " ✓" : " ✗"));
  }));
}

function renderArms(cases, run) {
  const body = document.querySelector("#arms tbody");
  body.replaceChildren(...ARMS.map(([arm, how]) => {
    const differs = cases.filter((c) => c.arms[arm] !== c.gold.verdict);
    return el("tr", {}, el("td", {}, el("code", {}, arm)), el("td", {}, how),
      el("td", { class: "num" }, `${cases.length - differs.length}/${cases.length}`),
      el("td", {}, differs.length ? differs.map((c) => `${c.id} (${(c.arms[arm] || "n/a").replace("_", " ")})`).join(", ") : "none"));
  }));
  document.getElementById("run-line").textContent =
    `One run of each arm on ${run.date.slice(0, 10)}, model ${run.model} at effort ${run.effort}, ${run.model_calls} model calls.`;
  document.getElementById("footer-run").textContent = run.source;
}

// Live mode: shown only when the server says it can run. The code is kept for this tab only.
const CODE_KEY = "rcm-ai-live-code";
const store = {
  get() { try { return sessionStorage.getItem(CODE_KEY) || ""; } catch { return ""; } },
  set(v) { try { sessionStorage.setItem(CODE_KEY, v); } catch { /* private window */ } },
};

function liveStep(out, text) {
  const li = el("li", {}, el("span", { class: "pending" }, text));
  out.append(li);
  return li;
}

function settle(li, ...children) {
  li.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined));
}

function onLiveEvent(e, ctx) {
  const { out, note } = ctx;
  switch (e.event) {
    case "extraction_started":
      ctx.extraction = liveStep(out, "The model is reading the note…");
      break;
    case "extraction_done":
      settle(ctx.extraction,
        el("strong", {}, "Evidence read from the note"), ` (${e.model || "model"}, stop ${e.stop_reason})`,
        e.problems.length
          ? el("ul", {}, e.problems.map((p) => el("li", { class: "fail" }, p)))
          : [highlightedNote(note, e.evidence), el("details", {}, el("summary", {}, "Evidence spans"), evidenceList(e.evidence))]);
      break;
    case "support_check_started":
      ctx.check = liveStep(out, `A second call is checking each of ${e.items} spans in its sentence…`);
      break;
    case "support_check_done":
      settle(ctx.check, el("strong", {}, "Span check"), " ",
        e.unsupported === null
          ? el("span", { class: "fail" }, `didn't judge every span (stop ${e.stop_reason}).`)
          : e.unsupported.length
            ? el("ul", {}, e.unsupported.map((u) => el("li", { class: "fail" }, u)))
            : el("span", { class: "ok" }, "every span supports what it's attached to."));
      break;
    case "verdict":
      out.append(el("li", {}, el("strong", {}, "The graph's verdict "), outcome(e.verdict.outcome), ...verdictParts(e.verdict)));
      break;
    case "draft_started":
      ctx.draft = liveStep(out, "The model is drafting the appeal letter…");
      break;
    case "draft_done":
      settle(ctx.draft, el("strong", {}, "Appeal letter"), ` (${e.model || "model"}, drafted now) `,
        e.letter ? [checkLine(e.check), el("div", { class: "letter", tabindex: "0" }, e.letter)]
          : el("span", { class: "fail" }, `The drafter returned no letter (stop ${e.stop_reason}).`));
      break;
    case "error":
      out.append(el("li", { class: "fail" }, `The run stopped: ${e.detail}`));
      break;
    case "done":
      ctx.calls = e.model_calls;
      break;
  }
}

async function runLive(id, note, code, status) {
  const out = document.getElementById("live-out");
  out.replaceChildren();
  const ctx = { out, note, calls: null };
  const started = Date.now();
  const tick = setInterval(() => { status.textContent = `Running… ${Math.round((Date.now() - started) / 1000)}s`; }, 1000);
  status.textContent = "Running…";
  try {
    const res = await fetch("/api/live", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Access-Code": code },
      body: JSON.stringify({ case: id, note }),
    });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try { detail = (await res.json()).detail || detail; } catch { /* not JSON */ }
      out.append(el("li", { class: "fail" }, detail));
      return;
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let i;
      while ((i = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, i);
        buffer = buffer.slice(i + 1);
        if (line.trim()) onLiveEvent(JSON.parse(line), ctx);
      }
    }
  } catch (e) {
    out.append(el("li", { class: "fail" }, `The run stopped: ${e.message}`));
  } finally {
    clearInterval(tick);
    const secs = Math.round((Date.now() - started) / 1000);
    status.textContent = ctx.calls === null ? `Ended after ${secs}s.` : `Done in ${secs}s, ${ctx.calls} model call${ctx.calls === 1 ? "" : "s"}.`;
  }
}

async function initLive(cases, currentId) {
  let health;
  try {
    const res = await fetch("/api/health", { cache: "no-store" });
    health = await res.json();
  } catch {
    return;
  }
  const on = health.live_mode === "on";
  document.getElementById("meta").textContent =
    document.getElementById("meta").textContent.replace("live mode off", on ? "live mode on (access code)" : "live mode off");
  if (!on) return;
  document.getElementById("footer-live").textContent =
    "Live mode runs the model on a case you pick, for people with the access code.";
  const select = document.getElementById("live-case");
  const noteBox = document.getElementById("live-note");
  const codeBox = document.getElementById("live-code");
  const byId = new Map(cases.map((c) => [c.id, c]));
  select.replaceChildren(...cases.map((c) => el("option", { value: c.id }, `${c.id} (labeled ${c.gold.verdict.replace("_", " ")})`)));
  const load = () => { noteBox.value = byId.get(select.value).note; };
  select.value = byId.has(currentId()) ? currentId() : cases[0].id;
  load();
  codeBox.value = store.get();
  select.addEventListener("change", load);
  document.getElementById("live-reset").addEventListener("click", load);
  const form = document.getElementById("live-form");
  const button = document.getElementById("live-run");
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    store.set(codeBox.value);
    button.disabled = true;
    try {
      await runLive(select.value, noteBox.value, codeBox.value, document.getElementById("live-status"));
    } finally {
      button.disabled = false;
    }
  });
  document.getElementById("live").hidden = false;
}

async function main() {
  let data;
  try {
    const res = await fetch("demo-cases.json", { cache: "no-cache" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data = await res.json();
  } catch (e) {
    document.getElementById("meta").textContent = `Couldn't load the recorded cases (${e.message}).`;
    return;
  }
  document.getElementById("meta").textContent =
    `recorded run ${data.run.date.slice(0, 10)} · eval set ${data.inputs.eval_set_sha256.slice(0, 8)} · live mode off`;
  renderSources(data.sources);
  renderArms(data.cases, data.run);
  const ordered = [...data.cases.filter((c) => c.featured), ...data.cases.filter((c) => !c.featured)];
  const byId = new Map(data.cases.map((c) => [c.id, c]));
  const select = (id) => {
    const c = byId.get(id);
    if (!c) return;
    renderCase(c);
    for (const b of document.querySelectorAll("#case-list button")) b.setAttribute("aria-current", String(b.dataset.id === id));
    if (location.hash !== `#${id}`) history.replaceState(null, "", `#${id}`);
  };
  renderList(ordered, select);
  select(byId.has(location.hash.slice(1)) ? location.hash.slice(1) : ordered[0].id);
  initLive(data.cases, () => location.hash.slice(1));
}

main();
