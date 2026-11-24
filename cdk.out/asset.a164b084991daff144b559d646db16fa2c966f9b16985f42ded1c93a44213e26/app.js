/* Claims Desk — lab portal. Plain JavaScript, no build step.
 * Sign-in: Cognito managed login, authorization code + PKCE. Tokens live in
 * sessionStorage for this tab only. All case data is rendered with textContent
 * (never innerHTML) because claim text is supplied by claimants. */
"use strict";

const TERMINAL = ["APPROVED", "REJECTED", "MANUAL_REVIEW"];
const STATUS_TONE = {
  APPROVED: "good", DECIDED: "good", REJECTED: "bad", MANUAL_REVIEW: "bad",
  PENDING_APPROVAL: "warn", AWAITING_DOCUMENTS: "warn", MORE_INFO_REQUESTED: "warn", DRAFT: "",
};
let CONFIG = null;
let ME = null;
let refreshTimer = null;

// ---------------------------------------------------------------------------
// Small DOM helper: h("div", {class: "x", onclick: fn}, child, "text", ...)
// ---------------------------------------------------------------------------
function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat(Infinity)) {
    if (c === undefined || c === null || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}
const app = () => document.getElementById("app");
function render(...nodes) { app().replaceChildren(...nodes); }
function store(key, value) { try { value === undefined ? sessionStorage.removeItem(key) : sessionStorage.setItem(key, value); } catch (e) { /* storage blocked */ } }
function load(key) { try { return sessionStorage.getItem(key); } catch (e) { return null; } }
const money = (n) => (n === undefined || n === null) ? "—" : "INR " + Number(n).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const when = (iso) => iso ? new Date(iso).toLocaleString() : "—";
function badge(status) { return h("span", { class: "badge " + (STATUS_TONE[status] || "") }, (status || "—").replace(/_/g, " ")); }
function flagBadges(flags) {
  flags = flags || {};
  return h("span", { class: "flags" },
    flags.outOfPolicy && h("span", { class: "badge bad" }, "out of policy"),
    flags.duplicateOf && h("span", { class: "badge warn" }, "possible duplicate"),
    flags.lowConfidence && h("span", { class: "badge warn" }, "low confidence"),
    flags.suspiciousDocument && h("span", { class: "badge bad" }, "suspicious document"),
    flags.escalated && h("span", { class: "badge bad" }, "SLA breached"));
}
function notice(kind, text) { return h("div", { class: "notice " + kind, role: kind === "bad" ? "alert" : "status" }, text); }

// ---------------------------------------------------------------------------
// Auth (authorization code + PKCE)
// ---------------------------------------------------------------------------
function b64url(bytes) { return btoa(String.fromCharCode(...new Uint8Array(bytes))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""); }
function redirectUri() { return location.origin + "/"; }

async function signIn() {
  const verifier = b64url(crypto.getRandomValues(new Uint8Array(32)));
  const challenge = b64url(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier)));
  const state = b64url(crypto.getRandomValues(new Uint8Array(16)));
  store("pkce_verifier", verifier); store("pkce_state", state); store("return_hash", location.hash || "#/cases");
  const q = new URLSearchParams({ response_type: "code", client_id: CONFIG.clientId, redirect_uri: redirectUri(),
    scope: "openid email profile", code_challenge: challenge, code_challenge_method: "S256", state });
  location.assign(`${CONFIG.cognitoDomain}/oauth2/authorize?${q}`);
}

async function completeSignIn(params) {
  if (params.get("state") !== load("pkce_state")) throw new Error("Sign-in state did not match; please try again.");
  const body = new URLSearchParams({ grant_type: "authorization_code", client_id: CONFIG.clientId,
    code: params.get("code"), redirect_uri: redirectUri(), code_verifier: load("pkce_verifier") || "" });
  const res = await fetch(`${CONFIG.cognitoDomain}/oauth2/token`, { method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" }, body });
  if (!res.ok) throw new Error("Could not complete sign-in (" + res.status + ")");
  const tokens = await res.json();
  store("access_token", tokens.access_token);
  store("token_expires", String(Date.now() + (tokens.expires_in - 60) * 1000));
  store("pkce_verifier"); store("pkce_state");
  const back = load("return_hash") || "#/cases";
  history.replaceState(null, "", "/" + back);
}

function signOut() {
  ["access_token", "token_expires"].forEach((k) => store(k));
  const q = new URLSearchParams({ client_id: CONFIG.clientId, logout_uri: redirectUri() });
  location.assign(`${CONFIG.cognitoDomain}/logout?${q}`);
}

function token() {
  const t = load("access_token");
  return t && Number(load("token_expires") || 0) > Date.now() ? t : null;
}

async function api(method, path, body) {
  const t = token();
  if (!t) { await signIn(); throw new Error("Signing in…"); }
  const res = await fetch(CONFIG.apiUrl + path, { method,
    headers: { Authorization: "Bearer " + t, ...(body ? { "Content-Type": "application/json" } : {}) },
    body: body ? JSON.stringify(body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401) { store("access_token"); await signIn(); throw new Error("Session expired"); }
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

async function uploadFiles(uploads, files) {
  for (const u of uploads) {
    const file = files.find((f) => f.name === u.fileName);
    const res = await fetch(u.uploadUrl, { method: "PUT", headers: { "Content-Type": u.contentType }, body: file });
    if (!res.ok) throw new Error(`Upload failed for ${u.fileName} (${res.status})`);
  }
}
function fileSpecs(files) { return files.map((f) => ({ fileName: f.name, contentType: f.type || "application/octet-stream" })); }

// ---------------------------------------------------------------------------
// Views
// ---------------------------------------------------------------------------
async function viewCases() {
  const statusSel = h("select", { "aria-label": "Filter by status" },
    h("option", { value: "" }, "All statuses"),
    ["SUBMITTED", "REGISTERED", "DOCUMENTS_PROCESSED", "SUMMARIZED", "READY_FOR_APPROVAL", "PENDING_APPROVAL",
     "AWAITING_DOCUMENTS", "APPROVED", "REJECTED", "MANUAL_REVIEW"].map((s) => h("option", { value: s }, s.replace(/_/g, " "))));
  const mine = h("input", { type: "checkbox", id: "mine", style: "width:auto" });
  const tableBody = h("tbody");
  const panel = h("div", { class: "panel" },
    h("div", { class: "toolbar" }, statusSel,
      h("label", { for: "mine", style: "margin:0;display:flex;gap:6px;align-items:center;font-weight:400" }, mine, "Only my cases"),
      h("button", { onclick: () => refresh() }, "Refresh")),
    h("table", {}, h("thead", {}, h("tr", {}, ["Case", "Status", "Type", "Claimant", "Amount", "Incident", "Assignee", "Flags"].map((t, i) => h("th", { class: i === 4 ? "num" : "" }, t)))), tableBody));
  render(h("h1", {}, "Cases"), panel);

  async function refresh() {
    tableBody.replaceChildren(h("tr", {}, h("td", { colspan: 8, class: "muted" }, "Loading…")));
    const q = new URLSearchParams();
    if (statusSel.value) q.set("status", statusSel.value);
    if (mine.checked) q.set("mine", "true");
    try {
      const { cases } = await api("GET", "/claims?" + q);
      if (!cases.length) { tableBody.replaceChildren(h("tr", {}, h("td", { colspan: 8, class: "muted" }, "No cases yet. Create one from New claim."))); return; }
      tableBody.replaceChildren(...cases.map((c) => h("tr", { tabindex: 0, onclick: () => (location.hash = "#/cases/" + c.caseId),
        onkeydown: (e) => { if (e.key === "Enter") location.hash = "#/cases/" + c.caseId; } },
        h("td", {}, c.caseId), h("td", {}, badge(c.status)), h("td", {}, c.claimType), h("td", {}, c.claimantName),
        h("td", { class: "num" }, money(c.claimedAmount)), h("td", {}, c.incidentDate), h("td", {}, c.assigneeName || "—"),
        h("td", {}, flagBadges(c.flags)))));
    } catch (e) { tableBody.replaceChildren(h("tr", {}, h("td", { colspan: 8 }, notice("bad", e.message)))); }
  }
  statusSel.addEventListener("change", refresh);
  mine.addEventListener("change", refresh);
  refresh();
}

async function viewCase(caseId) {
  let c;
  try { c = await api("GET", "/claims/" + encodeURIComponent(caseId)); }
  catch (e) { render(h("p", {}, h("a", { href: "#/cases" }, "← Cases")), notice("bad", e.message)); return; }

  const v = c.validation || {}, s = c.summary || {};
  const parts = [
    h("p", {}, h("a", { href: "#/cases" }, "← Cases")),
    h("h1", {}, c.caseId, " ", badge(c.status)),
    c.manualReviewReason && notice("bad", "Manual review: " + c.manualReviewReason),
    c.decision && notice(c.decision.decision === "REJECT" ? "bad" : c.decision.decision === "APPROVE" ? "good" : "warn",
      `Decision ${c.decision.decision} by ${c.decision.decidedBy} (${when(c.decision.decidedAt)}): ${c.decision.rationale}`),
    h("div", { class: "grid" },
      h("section", { class: "panel" }, h("h2", {}, "Claim"),
        h("dl", {},
          h("dt", {}, "Claimant"), h("dd", {}, c.claimantName, " · ", c.claimantEmail),
          h("dt", {}, "Policy"), h("dd", {}, c.policyNumber),
          h("dt", {}, "Type"), h("dd", {}, c.claimType),
          h("dt", {}, "Incident date"), h("dd", {}, c.incidentDate),
          h("dt", {}, "Claimed"), h("dd", {}, money(c.claimedAmount)),
          h("dt", {}, "Region"), h("dd", {}, c.region || "—"),
          h("dt", {}, "Assignee"), h("dd", {}, c.assigneeName || "—"),
          h("dt", {}, "Approver"), h("dd", {}, c.approverName || "—"),
          h("dt", {}, "SLA due"), h("dd", {}, when(c.slaDueAt)),
          h("dt", {}, "Flags"), h("dd", {}, flagBadges(c.flags))),
        h("p", { class: "muted" }, c.description),
        (c.flags || {}).outOfPolicyReasons && c.flags.outOfPolicyReasons.length > 0 &&
          h("div", {}, h("strong", {}, "Out of policy: "), h("ul", { class: "tight" }, c.flags.outOfPolicyReasons.map((r) => h("li", {}, r))))),
      h("section", { class: "panel" }, h("h2", {}, "Summary"),
        s.facts ? [h("p", {}, s.facts),
          s.evidence && s.evidence.length > 0 && [h("strong", {}, "Evidence"), h("ul", { class: "tight" }, s.evidence.map((x) => h("li", {}, x)))],
          s.missingInformation && s.missingInformation.length > 0 && [h("strong", {}, "Missing information"), h("ul", { class: "tight" }, s.missingInformation.map((x) => h("li", {}, x)))],
          s.risks && s.risks.length > 0 && [h("strong", {}, "Risks"), h("ul", { class: "tight" }, s.risks.map((x) => h("li", {}, x)))],
          h("p", {}, h("strong", {}, "Next action: "), s.nextAction)]
          : h("p", { class: "muted" }, "The Summary agent has not saved a summary yet."),
        (v.missingDocTypes || v.issues) && h("div", {},
          h("strong", {}, "Document checks"),
          h("ul", { class: "tight" },
            (v.missingDocTypes || []).map((m) => h("li", {}, "Missing: " + m)),
            (v.issues || []).map((i) => h("li", {}, i)),
            (v.lowConfidenceItems || []).map((i) => h("li", {}, "Low confidence: " + i)))))),
    decisionPanel(c),
    documentsPanel(c),
    h("section", { class: "panel" }, h("h2", {}, "Timeline"),
      h("ol", { class: "tight timeline", reversed: true }, (c.timeline || []).slice().reverse().map((t) =>
        h("li", {}, h("span", { class: "when" }, when(t.at)), h("strong", {}, t.actor), " · ", t.action, t.detail ? " — " + t.detail : "")))),
  ];
  render(...parts);
  if (!TERMINAL.includes(c.status)) refreshTimer = setTimeout(() => { if (location.hash === "#/cases/" + caseId) route(); }, 10000);
}

function decisionPanel(c) {
  const groups = (ME && ME.groups) || [];
  const canDecide = groups.includes("approvers") || groups.includes("supervisors");
  if (c.status === "PENDING_APPROVAL") {
    if (!canDecide) return h("section", { class: "panel" }, h("h2", {}, "Decision"), h("p", { class: "muted" }, `Waiting for ${c.approverName || "the approver"}.`));
    const rationale = h("textarea", { id: "rationale", placeholder: "Why? This is recorded in the audit trail." });
    const msg = h("div");
    const send = async (decision) => {
      if (rationale.value.trim().length < 5) { msg.replaceChildren(notice("warn", "Please give a short rationale.")); return; }
      buttons.forEach((b) => (b.disabled = true));
      try { await api("POST", `/claims/${c.caseId}/decision`, { decision, rationale: rationale.value.trim() }); route(); }
      catch (e) { msg.replaceChildren(notice("bad", e.message)); buttons.forEach((b) => (b.disabled = false)); }
    };
    const buttons = [h("button", { class: "primary", onclick: () => send("APPROVE") }, "Approve"),
      h("button", { class: "danger", onclick: () => send("REJECT") }, "Reject"),
      h("button", { onclick: () => send("MORE_INFO") }, "Request more information")];
    return h("section", { class: "panel" }, h("h2", {}, "Your decision"), msg,
      h("p", { class: "muted" }, "Agents prepared this case; only you can decide it."),
      h("label", { for: "rationale" }, "Rationale"), rationale, h("div", { class: "row" }, buttons));
  }
  if (c.status === "AWAITING_DOCUMENTS") {
    const req = c.missingDocumentRequest || {};
    const input = h("input", { type: "file", multiple: true, accept: "image/png,image/jpeg,application/pdf" });
    const msg = h("div");
    const btn = h("button", { class: "primary", onclick: async () => {
      const files = Array.from(input.files);
      if (!files.length) { msg.replaceChildren(notice("warn", "Choose at least one file.")); return; }
      btn.disabled = true;
      try {
        const { uploads } = await api("POST", `/claims/${c.caseId}/documents`, { files: fileSpecs(files) });
        await uploadFiles(uploads, files);
        await api("POST", `/claims/${c.caseId}/documents/complete`, {});
        route();
      } catch (e) { msg.replaceChildren(notice("bad", e.message)); btn.disabled = false; }
    } }, "Upload and continue");
    return h("section", { class: "panel" }, h("h2", {}, "Documents requested"), msg,
      req.message && h("p", {}, req.message),
      req.documentTypes && h("p", { class: "muted" }, "Needed: " + req.documentTypes.join(", ")),
      input, h("div", { class: "row" }, btn));
  }
  return null;
}

function documentsPanel(c) {
  const docs = c.documents || [];
  return h("section", { class: "panel" }, h("h2", {}, `Documents (${docs.length})`),
    docs.map((d) => h("div", { class: "doc" },
      h("div", {}, h("a", { href: d.downloadUrl, target: "_blank", rel: "noopener" }, d.fileName || d.docId), " ",
        d.classification && h("span", { class: "badge" }, d.classification.docType + " · " + Math.round((d.classification.confidence || 0) * 100) + "%"),
        d.textConfidence !== undefined && h("span", { class: "muted" }, ` OCR ${d.textConfidence}%`),
        d.suspiciousInstructions && h("span", { class: "badge bad" }, "contains instructions")),
      d.fields && h("div", { class: "fields two" }, Object.entries(d.fields).map(([k, f]) =>
        h("div", {}, `${k}: ${f.value ?? "—"} (${f.confidence ?? 0}%)`))))));
}

function viewNew() {
  const f = (id, label, attrs) => [h("label", { for: id }, label), h("input", { id, ...attrs })];
  const type = h("select", { id: "claimType" }, ["motor", "health", "property"].map((t) => h("option", { value: t }, t)));
  const files = h("input", { id: "files", type: "file", multiple: true, accept: "image/png,image/jpeg,application/pdf" });
  const desc = h("textarea", { id: "description" });
  const msg = h("div");
  const submit = h("button", { class: "primary", type: "submit" }, "Create and submit claim");
  const form = h("form", { class: "panel", onsubmit: async (e) => {
    e.preventDefault();
    const val = (id) => document.getElementById(id).value.trim();
    const chosen = Array.from(files.files);
    if (!chosen.length) { msg.replaceChildren(notice("warn", "Attach the claim documents.")); return; }
    submit.disabled = true; msg.replaceChildren(notice("warn", "Creating claim and uploading documents…"));
    try {
      const created = await api("POST", "/claims", { policyNumber: val("policyNumber"), claimType: type.value,
        claimantName: val("claimantName"), claimantEmail: val("claimantEmail"), incidentDate: val("incidentDate"),
        claimedAmount: Number(val("claimedAmount")), description: desc.value.trim(), packId: val("packId"),
        files: fileSpecs(chosen) });
      await uploadFiles(created.uploads, chosen);
      await api("POST", `/claims/${created.caseId}/submit`, {});
      location.hash = "#/cases/" + created.caseId;
    } catch (err) { msg.replaceChildren(notice("bad", err.message)); submit.disabled = false; }
  } },
    msg,
    h("p", { class: "muted" }, "Tip: the sample packs in data/claim_packs/ include pack.json with the values to type here."),
    h("div", { class: "two" },
      h("div", {}, f("policyNumber", "Policy number", { required: true, placeholder: "P-MOT-1001" })),
      h("div", {}, h("label", { for: "claimType" }, "Claim type"), type),
      h("div", {}, f("claimantName", "Claimant name", { required: true })),
      h("div", {}, f("claimantEmail", "Claimant email", { required: true, type: "email" })),
      h("div", {}, f("incidentDate", "Incident date", { required: true, type: "date" })),
      h("div", {}, f("claimedAmount", "Claimed amount (INR)", { required: true, type: "number", step: "0.01", min: "1" }))),
    h("label", { for: "description" }, "What happened"), desc,
    f("packId", "Sample pack ID (optional, for evaluation)", { placeholder: "PACK-01" }),
    h("label", { for: "files" }, "Documents (PNG, JPEG or single-page PDF)"), files,
    h("div", { class: "row" }, submit));
  render(h("h1", {}, "New claim"), form);
}

// ---------------------------------------------------------------------------
// Router and start-up
// ---------------------------------------------------------------------------
async function route() {
  clearTimeout(refreshTimer);
  const hash = location.hash || "#/cases";
  const m = hash.match(/^#\/cases\/([A-Z0-9-]+)$/);
  if (m) return viewCase(m[1]);
  if (hash === "#/new") return viewNew();
  return viewCases();
}

async function start() {
  try {
    CONFIG = await (await fetch("config.json", { cache: "no-store" })).json();
    const params = new URLSearchParams(location.search);
    if (params.get("code")) await completeSignIn(params);
    if (!token()) {
      render(h("div", { class: "panel" }, h("h1", {}, "Claims Desk"),
        h("p", {}, "Sign in with your lab account to work on claims."),
        h("button", { class: "primary", onclick: signIn }, "Sign in")));
      return;
    }
    ME = await api("GET", "/me");
    document.getElementById("who").replaceChildren(
      h("span", {}, `${ME.name || ME.username} · ${(ME.groups || []).join(", ") || "no groups"}`),
      h("button", { onclick: signOut }, "Sign out"));
    window.addEventListener("hashchange", route);
    route();
  } catch (e) {
    render(notice("bad", e.message || String(e)));
  }
}
start();
