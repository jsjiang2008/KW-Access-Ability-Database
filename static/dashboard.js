
let csrfToken = "";
const $ = (id) => document.getElementById(id);
const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" });
const num = new Intl.NumberFormat("en-US");

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  Object.assign(node, props);
  for (const child of children) node.append(child);  // append() treats strings as text, never HTML.
  return node;
}

let toastTimer;
function toast(message, isError = false) {
  const t = $("toast");
  t.textContent = message;
  t.className = "show" + (isError ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = ""), 4000);
}

async function api(path, options) {
  const headers = new Headers(options?.headers);
  if (options?.method && !["GET", "HEAD"].includes(options.method.toUpperCase())) headers.set("X-CSRF-Token", csrfToken);
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  if (response.status === 401) {
    window.location.replace("/login");
    throw new Error("Your session expired. Please sign in again.");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function render(data) {
  const { files, leaderboard, totals } = data;
  $("t-tickets").textContent = num.format(totals.tickets);
  $("t-revenue").textContent = usd.format(totals.revenue);
  $("t-purchases").textContent = num.format(totals.purchases);
  $("t-sellers").textContent = num.format(totals.sellers);
  $("review").hidden = !totals.review;
  $("review").textContent = `⚠ ${totals.review} purchase row(s) need review (missing names, quantity mismatches, duplicate balls, etc.). Details are in the exported CSV's Issues column.`;

  const exp = $("export");
  exp.setAttribute("aria-disabled", String(!totals.purchases));
  exp.tabIndex = totals.purchases ? 0 : -1;

  $("files").replaceChildren(...files.map((f) => {
    const remove = el("button", { type: "button", textContent: "Remove", title: `Remove ${f.name}` });
    remove.onclick = () => removeFile(f.name);
    return el("li", {}, el("span", { textContent: "📄" }), el("span", { className: "fname", textContent: f.name }),
      el("span", { className: "meta", textContent: `${f.purchases} purchase${f.purchases === 1 ? "" : "s"} · ${(f.size / 1024).toFixed(0)} KB` }), remove);
  }));
  $("no-files").hidden = files.length > 0;

  const max = Math.max(1, ...leaderboard.map((s) => s.tickets));
  $("board").replaceChildren(...leaderboard.map((s, i) => {
    const sub = el("small", { textContent: `${s.purchases} purchase${s.purchases === 1 ? "" : "s"} ` });
    if (s.review) sub.append(el("span", { className: "tag", textContent: `${s.review} to review` }));
    return el("li", { className: i < 3 && s.tickets ? `r${i + 1}` : "" },
      el("span", { className: "rank", textContent: i + 1 }),
      el("span", { className: "seller", textContent: s.seller }, sub),
      el("div", { className: "bar" }, el("div", { style: `width:${(s.tickets / max) * 100}%` })),
      el("span", { className: "nums" }, el("b", { textContent: `${num.format(s.tickets)} tickets` }), el("small", { textContent: usd.format(s.revenue) })));
  }));
  $("no-board").hidden = leaderboard.length > 0;

  const top = leaderboard[0];
  const tied = top ? leaderboard.filter((s) => s.tickets === top.tickets) : [];
  $("champion").replaceChildren(top && top.tickets ? el("div", { className: "champion" },
    el("span", { className: "trophy", textContent: "🏆" }),
    el("div", {},
      el("strong", { textContent: tied.length > 1 ? `Tie: ${tied.map((s) => s.seller).join(", ")}` : `${top.seller} is the top seller!` }),
      el("span", { textContent: `${num.format(top.tickets)} tickets sold${tied.length > 1 ? " each" : ` · ${usd.format(top.revenue)} in sales`}` }))) : "");
}

async function refresh() {
  try { render(await api("/api/summary")); }
  catch (e) { toast(e.message, true); }
}

async function upload(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  let ok = 0;
  for (const file of files) {
    if (!file.name.toLowerCase().endsWith(".xlsx")) { toast(`${file.name}: only .xlsx files are supported.`, true); continue; }
    try {
      await api("/api/files", { method: "POST", headers: { "X-Filename": encodeURIComponent(file.name) }, body: file });
      ok++;
    } catch (e) { toast(`${file.name}: ${e.message}`, true); }
  }
  if (ok) toast(`Uploaded ${ok} file${ok === 1 ? "" : "s"}.`);
  await refresh();
  if ($("q").value) runSearch();
}

async function removeFile(name) {
  if (!confirm(`Remove ${name} from the dashboard?`)) return;
  try { await api(`/api/files/${encodeURIComponent(name)}`, { method: "DELETE" }); toast(`Removed ${name}.`); }
  catch (e) { toast(e.message, true); }
  await refresh();
  if ($("q").value) runSearch();
}

async function runSearch() {
  const q = $("q").value.trim();
  const box = $("results");
  if (!/^\d+$/.test(q) || Number(q) < 1) { box.replaceChildren(el("div", { className: "notice err", textContent: "Enter a positive whole ball number." })); return; }
  try {
    const { number, matches } = await api(`/api/search/${q}`);
    if (!matches.length) { box.replaceChildren(el("p", { className: "empty", textContent: `No buyer found for ball #${number}.` })); return; }
    const cards = matches.map((m) => {
      const field = (label, value) => el("div", {}, el("dt", { textContent: label }), el("dd", { textContent: value || "—" }));
      const card = el("dl", { className: "buyer" },
        el("div", { className: "name", textContent: `#${number} · ${m.name || "(no name)"}` }),
        field("Phone", m.phone), field("Email", m.email),
        field("Address", [m.street, m.city].filter(Boolean).join(", ")),
        field("Sold by", m.seller), field("Tickets in purchase", m.tickets ?? "—"), field("Source", m.source));
      if (m.issues.length) card.append(el("div", { className: "issues", textContent: "⚠ " + m.issues.join(" ") }));
      return card;
    });
    if (matches.length > 1) cards.unshift(el("div", { className: "notice warn", textContent: `Ball #${number} is assigned to ${matches.length} purchases. Check with the sellers.` }));
    box.replaceChildren(...cards);
  } catch (e) { box.replaceChildren(el("div", { className: "notice err", textContent: e.message })); }
}

$("export").onclick = async (e) => {
  e.preventDefault();
  if ($("export").getAttribute("aria-disabled") === "true") return;
  try {
    const response = await fetch("/api/export.csv", { credentials: "same-origin" });
    if (response.status === 401) { window.location.replace("/login"); return; }
    if (!response.ok) throw new Error("Could not download the CSV. Please try again.");
    const url = URL.createObjectURL(await response.blob());
    const link = el("a", { href: url, download: "master_sales.csv" });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (e) { toast(e.message, true); }
};
$("logout").onclick = async () => {
  try { await api("/api/logout", { method: "POST" }); window.location.replace("/login"); }
  catch (e) { toast(e.message, true); }
};
$("pick").onclick = () => $("file").click();
$("file").onchange = (e) => { upload(e.target.files); e.target.value = ""; };
const drop = $("drop");
drop.ondragover = (e) => { e.preventDefault(); drop.classList.add("over"); };
drop.ondragleave = () => drop.classList.remove("over");
drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove("over"); upload(e.dataTransfer.files); };
$("search").onsubmit = (e) => { e.preventDefault(); runSearch(); };
async function initialize() {
  try {
    const current = await api("/api/session");
    if (!current.authenticated) { window.location.replace("/login"); return; }
    csrfToken = current.csrfToken;
    $("username").textContent = current.username;
    await refresh();
  } catch (e) { toast(e.message, true); }
}
window.addEventListener("pageshow", (event) => { if (event.persisted) window.location.reload(); });
initialize();
