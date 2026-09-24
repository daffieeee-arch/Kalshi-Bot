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

function sessionCountdown(endsAt) {
  if (!endsAt) return "--:--:--";
  const ms = Date.parse(endsAt) - Date.now();
  if (Number.isNaN(ms)) return "--:--:--";
  if (ms <= 0) return "00:00:00";
  const total = Math.floor(ms / 1000);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function fmtAmsterdam(iso) {
  if (!iso) return "";
  const dt = new Date(iso);
  if (Number.isNaN(dt.getTime())) return "";
  return `${new Intl.DateTimeFormat("nl-NL", {
    timeZone: "Europe/Amsterdam",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).format(dt)} Amsterdam`;
}

function fmtMoney(value) {
  if (value == null || value === "") return "—";
  const n = Number(value);
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 4 })}`;
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
  $("close-at").textContent = fmtAmsterdam(data.close_at);
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
  $("file-meta").textContent = `${fmtBytes(recorder.bytes)} · ${fmtAmsterdam(data.last_row_at)}`;
  $("error").hidden = !data.error;
  $("error").textContent = data.error || "";
  renderSignal(data.signal || {});
  renderPaperBoard(data);
  renderBook(book);
  renderTape(data.tape || []);
  renderRates(data.streams || []);
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

function signedUsd(value) {
  if (value == null || value === "") return "—";
  const n = Number(value);
  const sign = n > 0 ? "+" : "";
  return `${sign}${n.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
}

function renderSignal(signal) {
  $("gap").textContent = signedUsd(signal.gap);
  $("gap").className = `big mono ${Number(signal.gap) >= 0 ? "yes" : "no"}`;
  $("gap-meta").textContent = signal.regime === "last_minute" ? "last minute" : "mid-window";
  $("model-p").textContent = signal.model_yes == null ? "—" : Number(signal.model_yes).toFixed(2);
  $("model-meta").textContent = signal.note || "";
  const yesEdge = signal.yes_edge == null ? null : Number(signal.yes_edge);
  const noEdge = signal.no_edge == null ? null : Number(signal.no_edge);
  let edge = yesEdge;
  let edgeLabel = "YES ask";
  if ((noEdge ?? -99) > (yesEdge ?? -99)) {
    edge = noEdge;
    edgeLabel = "NO ask";
  }
  $("edge").textContent = edge == null ? "—" : signedUsd(edge);
  $("edge").className = `big mono ${edge != null && edge >= 0 ? "yes" : "no"}`;
  $("edge-meta").textContent = `after taker fee · ${edgeLabel}`;
  $("hint").textContent = signal.hint || "wait";
  $("hint").className = `big mono ${
    signal.hint === "paper YES?" ? "yes" : signal.hint === "paper NO?" ? "no" : ""
  }`;
}

function renderPaperBoard(data) {
  const sessions = (data.paper_sessions && data.paper_sessions.length
    ? data.paper_sessions
    : data.paper_session
      ? [data.paper_session]
      : []);
  const multi = sessions.length > 1;
  document.body.classList.toggle("multi-arm", multi);
  if (multi) {
    renderArms(sessions);
    const live = sessions.filter((session) => session.running).length;
    const pill = live ? `${live}/${sessions.length} arms live` : `${sessions.length} arms`;
    setPill("paper-pill", pill, live ? "live" : "warn");
    return;
  }
  $("paper-arms").replaceChildren();
  renderPaper(sessions[0] || null);
}

function renderPaper(session) {
  const running = Boolean(session && session.running);
  const arm = session && session.arm_label && session.arm !== "legacy" ? session.arm_label : "paper";
  setPill(
    "paper-pill",
    running ? `${arm} live` : session ? `${arm} idle` : "paper off",
    running ? "live" : session ? "warn" : "off",
  );
  if (!session) {
    $("paper-equity").textContent = "—";
    $("paper-cash").textContent = "start paper-btc-15m";
    $("paper-countdown").textContent = "--:--:--";
    $("paper-ends").textContent = "no paper session";
    $("paper-pnl").textContent = "—";
    $("paper-pnl-meta").textContent = "realized — · unrealized —";
    $("paper-progress-label").textContent = "—";
    $("paper-progress").style.width = "0%";
    $("paper-note").textContent = "aspirational KPI · not a forecast";
    $("paper-win").textContent = "—";
    $("paper-record").textContent = "no settled trades";
    $("paper-strategy").textContent = "—";
    $("paper-variant").textContent = "";
    $("paper-params").textContent = "";
    $("paper-learn").textContent = "learning —";
    fillRows("paper-adapt", [], "No adaptation yet.");
    fillRows("paper-open", [], "No open paper trades.");
    fillRows("paper-closed", [], "No closed paper trades.");
    fillRows("paper-fills", [], "No paper fills.");
    return;
  }
  $("paper-equity").textContent = fmtMoney(session.equity).replace("+", "");
  $("paper-cash").textContent = `cash ${fmtMoney(session.cash).replace("+", "")} · start ${fmtMoney(session.bankroll).replace("+", "")}`;
  $("paper-countdown").textContent = sessionCountdown(session.ends_at);
  $("paper-ends").textContent = session.ends_at_amsterdam || fmtAmsterdam(session.ends_at);
  const realized = Number(session.realized_pnl);
  $("paper-pnl").textContent = fmtMoney(session.realized_pnl);
  $("paper-pnl").className = `big mono ${realized >= 0 ? "yes" : "no"}`;
  $("paper-pnl-meta").textContent = `realized ${fmtMoney(session.realized_pnl)} · unrealized ${fmtMoney(session.unrealized_pnl)}`;
  const progress = session.progress == null ? null : Number(session.progress);
  $("paper-progress-label").textContent = progress == null ? "—" : `${(progress * 100).toFixed(1)}%`;
  const width = progress == null ? 0 : Math.max(0, Math.min(100, progress * 100));
  $("paper-progress").style.width = `${width}%`;
  $("paper-note").textContent = session.target_note || "aspirational KPI · not a forecast";
  const win = session.win_rate == null ? null : Number(session.win_rate);
  $("paper-win").textContent = win == null ? "—" : `${(win * 100).toFixed(0)}%`;
  $("paper-record").textContent = `${session.wins || 0} wins · ${session.losses || 0} losses`;
  $("paper-strategy").textContent = session.strategy || "—";
  $("paper-variant").textContent = variantLine(session);
  const params = session.params || {};
  $("paper-params").textContent = params.mid_edge
    ? `mid ${params.mid_edge} · last ${params.last_minute_edge} · size ${params.contracts} · maker bias ${params.maker_bias} · cooldown ${params.cooldown_s}s · risk ${params.max_open_risk} · stop ${params.stop_loss || "—"} · tp ${params.take_profit || "—"}`
    : "";
  renderLearner(session.learner);
  renderAdaptations(session.adaptations || []);
  renderTrades("paper-open", session.open_trades || [], "No open paper trades.");
  renderTrades("paper-closed", session.closed_trades || [], "No closed paper trades.");
  renderFills(session.fills || []);
}

function variantLine(session) {
  const orders = session.learn_exit_orders === false ? "learn-exit orders off" : "learn-exit orders on";
  const label = session.arm_label || "LEGACY";
  return `${label} · ${orders} · training, adjust_params, adapt, stop, flip, TP, edge_gone stay on`;
}

function renderArms(sessions) {
  const root = $("paper-arms");
  root.replaceChildren();
  for (const session of sessions) {
    root.append(armCard(session));
  }
}

function armCard(session) {
  const card = document.createElement("section");
  card.className = "arm-card";
  const head = document.createElement("div");
  head.className = "arm-head";
  const title = document.createElement("h2");
  title.textContent = session.arm_label || "PAPER";
  const state = document.createElement("span");
  state.className = session.running ? "yes" : "muted";
  state.textContent = session.running ? "live" : "idle";
  const countdown = document.createElement("span");
  countdown.className = "mono arm-countdown";
  countdown.dataset.ends = session.ends_at || "";
  countdown.textContent = sessionCountdown(session.ends_at);
  head.append(title, state, countdown);
  const note = document.createElement("p");
  note.className = "muted arm-note";
  note.textContent = variantLine(session);
  const metrics = document.createElement("p");
  metrics.className = "arm-metrics mono";
  metrics.textContent = metricsLine(session);
  const trades = document.createElement("div");
  trades.className = "tape";
  const closed = session.closed_trades || [];
  if (!closed.length) {
    trades.append(emptyLine("No closed paper trades."));
  } else {
    for (const row of closed.slice(0, 12)) {
      trades.append(closedLine(row));
    }
  }
  card.append(head, note, metrics, trades);
  return card;
}

function metricsLine(session) {
  const metrics = session.metrics || {};
  const reasons = metrics.exit_reasons || {};
  const mix = Object.entries(reasons).map(([name, count]) => `${name} ${count}`).join(" · ");
  const net = metrics.net_pnl == null ? fmtMoney(session.realized_pnl) : fmtMoney(metrics.net_pnl);
  const fees = metrics.fees == null ? "—" : fmtMoney(metrics.fees);
  const closes = metrics.closes == null ? (session.wins || 0) + (session.losses || 0) : metrics.closes;
  const sub = metrics.sub_1s_closes == null ? "—" : metrics.sub_1s_closes;
  const dd = metrics.drawdown_from_bankroll == null ? "—" : fmtMoney(metrics.drawdown_from_bankroll);
  return `equity ${fmtMoney(session.equity)} · net ${net} · fees ${fees} · closes ${closes} · sub-1s ${sub} · dd ${dd}${mix ? ` · ${mix}` : ""}`;
}

function closedLine(row) {
  const el = document.createElement("div");
  el.className = "trade-row";
  const side = document.createElement("span");
  side.className = row.outcome === "no" ? "no" : "yes";
  side.textContent = `${(row.outcome || "").toUpperCase()} ${row.style || ""}`.trim();
  const detail = document.createElement("span");
  const hold = row.hold_ms == null ? "" : ` · hold ${row.hold_ms}ms`;
  const delta = row.exit_delta || {};
  const contradiction = delta.same_quote_contradiction ? " · same-quote" : "";
  const flipped = Array.isArray(delta.predicate_flipped_true) && delta.predicate_flipped_true.length
    ? ` · flipped ${delta.predicate_flipped_true.join(",")}`
    : "";
  detail.textContent = `${row.ticker || ""} · ${row.exit_reason || "open"}${hold}${contradiction}${flipped} · pnl ${row.pnl == null ? "—" : fmtMoney(row.pnl)}`;
  const when = document.createElement("span");
  when.className = "muted";
  when.textContent = row.at_amsterdam || "";
  el.append(side, detail, when);
  return el;
}

function renderLearner(learn) {
  if (!learn) {
    $("paper-learn").textContent = "learning cold start";
    return;
  }
  const n = learn.samples || 0;
  const stateLabel = learn.active ? "learning live" : `warming up · rules until ${learn.min_samples || 4}`;
  const weights = learn.weights || {};
  const edge = weights.edge == null ? "—" : Number(weights.edge).toFixed(3);
  const maker = weights.maker == null ? "—" : Number(weights.maker).toFixed(3);
  $("paper-learn").textContent = `${stateLabel} · ${n} samples · ${learn.last_reason || "cold start"} · w edge ${edge} · maker ${maker}`;
}

function renderAdaptations(rows) {
  const root = $("paper-adapt");
  root.replaceChildren();
  if (!rows.length) {
    root.append(emptyLine("No adaptation yet."));
    return;
  }
  for (const row of rows.slice(0, 8)) {
    const el = document.createElement("div");
    el.className = "adapt-row";
    const params = row.params || {};
    el.innerHTML = `
      <span class="muted">${row.at_amsterdam || ""}</span>
      <span>${row.reason || ""} · size ${params.contracts || ""} · mid ${params.mid_edge || ""}</span>
    `;
    root.append(el);
  }
}

function renderTrades(id, rows, empty) {
  const root = $(id);
  root.replaceChildren();
  if (!rows.length) {
    root.append(emptyLine(empty));
    return;
  }
  for (const row of rows.slice(0, 20)) {
    const el = document.createElement("div");
    el.className = "trade-row";
    const pnl = row.pnl == null ? "" : fmtMoney(row.pnl);
    const mark = row.mark ? ` · mark ${fmtPx(row.mark)}` : "";
    const upnl = row.unrealized == null || row.unrealized === "" ? "" : ` · u ${fmtMoney(row.unrealized)}`;
    const hold = row.hold_ms == null ? "" : ` · ${row.hold_ms}ms`;
    const reason = row.exit_reason ? ` · ${row.exit_reason}` : "";
    el.innerHTML = `
      <span class="${row.outcome === "no" ? "no" : "yes"}">${(row.outcome || "").toUpperCase()} ${row.style || ""}</span>
      <span>${row.ticker || ""} · ${row.filled || "0"} @ ${fmtPx(row.avg_price || row.limit)}${mark}${upnl} · fee ${row.fee || "0"}${reason}${hold}</span>
      <span class="muted">${pnl} ${row.at_amsterdam || ""}</span>
    `;
    root.append(el);
  }
}

function renderFills(rows) {
  const root = $("paper-fills");
  root.replaceChildren();
  if (!rows.length) {
    root.append(emptyLine("No paper fills."));
    return;
  }
  for (const row of rows.slice(0, 20)) {
    const el = document.createElement("div");
    el.className = "trade-row";
    el.innerHTML = `
      <span class="${row.outcome === "no" ? "no" : "yes"}">${(row.outcome || "").toUpperCase()} ${row.action === "sell" ? "sell" : "buy"}</span>
      <span>${row.count || ""} @ ${fmtPx(row.price)} · fee ${row.fee || "0"} · ${row.source || ""}</span>
      <span class="muted">${row.at_amsterdam || ""}</span>
    `;
    root.append(el);
  }
}

function fillRows(id, rows, empty) {
  const root = $(id);
  root.replaceChildren();
  if (!rows.length) root.append(emptyLine(empty));
}

function emptyLine(text) {
  const el = document.createElement("p");
  el.className = "muted";
  el.textContent = text;
  return el;
}

function renderRates(streams) {
  const root = $("rates");
  root.replaceChildren();
  for (const row of streams) {
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.title = `${row.id} · ${row.count} events this session`;
    chip.textContent = `${row.label} ${Number(row.per_sec).toFixed(1)}/s`;
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
    if (!state.latest) return;
    $("countdown").textContent = countdown(state.latest.close_at);
    const session = state.latest.paper_session;
    if (session && session.ends_at && !document.body.classList.contains("multi-arm")) {
      $("paper-countdown").textContent = sessionCountdown(session.ends_at);
    }
    document.querySelectorAll(".arm-countdown").forEach((el) => {
      el.textContent = sessionCountdown(el.dataset.ends);
    });
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
