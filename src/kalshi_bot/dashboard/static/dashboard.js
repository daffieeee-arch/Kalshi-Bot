const state = {
  paused: false,
  depth: 10,
  tape: "all",
  focus: null,
  latest: null,
};

const $ = (id) => document.getElementById(id);

function fmtPx(value) {
  if (value == null || value === "") return "—";
  return Number(value).toFixed(2);
}

function fmtUsd(value) {
  if (value == null || value === "") return "—";
  return Number(value).toLocaleString("en-US", { maximumFractionDigits: 2 });
}

function fmtBytes(bytes) {
  if (!bytes) return "0 B";
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

function countdown(closeAt) {
  if (!closeAt) return "--:--";
  const ms = Date.parse(closeAt) - Date.now();
  if (Number.isNaN(ms)) return "--:--";
  if (ms <= 0) return "00:00";
  const total = Math.floor(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function setPill(id, text, kind) {
  const el = $(id);
  el.textContent = text;
  el.className = `pill ${kind}`;
}

function render(data) {
  state.latest = data;
  const recorder = data.recorder || {};
  setPill(
    "recorder-pill",
    recorder.running ? `recorder pid ${recorder.lock_pid}` : "recorder down",
    recorder.running ? "live" : "off",
  );
  const bookKind =
    data.book_status === "live" ? "live" : data.book_status === "waiting_snapshot" ? "warn" : "off";
  setPill("book-pill", data.book_status.replaceAll("_", " "), bookKind);

  $("ticker").textContent = data.ticker || "waiting…";
  $("title").textContent = data.title || "";
  $("close-at").textContent = data.close_at ? data.close_at.replace("T", " ") : "";
  $("countdown").textContent = countdown(data.close_at);
  $("brti").textContent = fmtUsd(data.brti && data.brti.value);
  const closeAvg = data.brti && data.brti.close_avg;
  const closeWin = data.brti && data.brti.close_window;
  $("brti-meta").textContent = closeAvg
    ? `15m close ${fmtUsd(closeAvg)} · n=${closeWin}`
    : `60s avg ${fmtUsd(data.brti && data.brti.avg_60s)}`;
  const book = data.book || {};
  $("yes-quote").textContent = `${fmtPx(book.yes_bid)} / ${fmtPx(book.yes_ask)}`;
  $("strike").textContent = data.floor_strike
    ? `floor ${fmtUsd(data.floor_strike)} · NO ${fmtPx(book.no_bid)} / ${fmtPx(book.no_ask)}`
    : `NO ${fmtPx(book.no_bid)} / ${fmtPx(book.no_ask)}`;
  $("file-meta").textContent = `${fmtBytes(recorder.bytes)} · ${data.last_row_at || ""}`;
  $("error").hidden = !data.error;
  $("error").textContent = data.error || "";
  renderBook(book);
  renderTape(data.tape || []);
  renderRates(data.rates || {}, data.counts || {});
  if (state.focus) {
    $("focus").textContent = `Pinned ${state.focus.side.toUpperCase()} ${fmtPx(state.focus.price)}`;
    $("focus").classList.remove("muted");
  }
}

function maxSize(levels) {
  return levels.reduce((m, row) => Math.max(m, Number(row[1]) || 0), 0) || 1;
}

function renderBook(book) {
  const depth = state.depth;
  const yes = (book.yes || []).slice(0, depth);
  const no = (book.no || []).slice(0, depth);
  const yesMax = maxSize(yes);
  const noMax = maxSize(no);
  const rows = Math.max(yes.length, no.length, 1);
  const root = $("book");
  root.replaceChildren();
  for (let i = 0; i < rows; i += 1) {
    const y = yes[i];
    const n = no[i];
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "book-row";
    const yPx = y ? y[0] : "";
    const nPx = n ? n[0] : "";
    if (
      state.focus &&
      ((state.focus.side === "yes" && state.focus.price === yPx) ||
        (state.focus.side === "no" && state.focus.price === nPx))
    ) {
      btn.classList.add("active");
    }
    btn.innerHTML = `
      <span class="yes">${y ? fmtPx(y[0]) : ""}</span>
      <span>${y ? `${Number(y[1]).toFixed(2)}<div class="bar yes"><span style="width:${(Number(y[1]) / yesMax) * 100}%"></span></div>` : ""}</span>
      <span class="no">${n ? fmtPx(n[0]) : ""}</span>
      <span>${n ? `${Number(n[1]).toFixed(2)}<div class="bar no"><span style="width:${(Number(n[1]) / noMax) * 100}%"></span></div>` : ""}</span>
    `;
    btn.addEventListener("click", () => {
      if (y) state.focus = { side: "yes", price: y[0] };
      else if (n) state.focus = { side: "no", price: n[0] };
      if (state.latest) render(state.latest);
    });
    root.append(btn);
  }
}

function renderTape(tape) {
  const rows = tape.filter((row) => state.tape === "all" || row.taker_side === state.tape);
  const root = $("tape");
  root.replaceChildren();
  for (const row of rows.slice(0, 40)) {
    const el = document.createElement("div");
    el.className = "tape-row";
    const side = row.taker_side === "no" ? "no" : "yes";
    el.innerHTML = `
      <span class="${side}">${side.toUpperCase()}</span>
      <span>${fmtPx(row.yes_price)} · ${row.count ?? ""}</span>
      <span class="muted">${row.book_side || ""}</span>
    `;
    root.append(el);
  }
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No trades in this filter yet.";
    root.append(empty);
  }
}

function renderRates(rates, counts) {
  const root = $("rates");
  root.replaceChildren();
  const keys = Object.keys(counts).sort();
  for (const key of keys) {
    const chip = document.createElement("span");
    chip.className = "chip";
    const perSec = rates[key] != null ? `${rates[key]}/s` : "0/s";
    chip.textContent = `${key} ${counts[key]} · ${perSec}`;
    root.append(chip);
  }
}

function bind() {
  $("pause").addEventListener("click", () => {
    state.paused = !state.paused;
    $("pause").textContent = state.paused ? "Resume" : "Pause";
    document.body.classList.toggle("paused", state.paused);
  });
  document.querySelectorAll('input[name="depth"]').forEach((el) => {
    el.addEventListener("change", () => {
      state.depth = Number(el.value);
      if (state.latest) render(state.latest);
    });
  });
  document.querySelectorAll('input[name="tape"]').forEach((el) => {
    el.addEventListener("change", () => {
      state.tape = el.value;
      if (state.latest) render(state.latest);
    });
  });
  setInterval(() => {
    if (state.latest) $("countdown").textContent = countdown(state.latest.close_at);
  }, 250);
}

function connect() {
  const events = new EventSource("/api/stream");
  events.onmessage = (event) => {
    if (state.paused) return;
    render(JSON.parse(event.data));
  };
  events.onerror = () => {
    $("error").hidden = false;
    $("error").textContent = "Live stream disconnected. Retrying…";
  };
}

bind();
connect();
