(() => {
  const $ = (s, ctx = document) => ctx.querySelector(s);
  const $$ = (s, ctx = document) => Array.from(ctx.querySelectorAll(s));

  let _currentRun = null;
  let _currentDiffs = [];
  let _currentLayer = "signal";

  async function loadStrategies() {
    const r = await fetch("/api/fidelity/strategies");
    const { strategies } = await r.json();
    const sel = $("#fid-strategy");
    if (!strategies.length) {
      sel.innerHTML = `<option value="">— nenhuma estratégia com trades fechados —</option>`;
      return;
    }
    sel.innerHTML = strategies.map(s =>
      `<option value="${s.name}">${s.display} — ${s.asset} (${s.trades} trades)</option>`
    ).join("");
  }

  async function loadHistory() {
    const r = await fetch("/api/fidelity/runs?limit=20");
    const { runs } = await r.json();
    const sel = $("#fid-history");
    sel.innerHTML = `<option value="">— últimas verificações —</option>` +
      runs.map(rn => {
        const dt = new Date(rn.created_at).toLocaleString("pt-BR");
        return `<option value="${rn.id}">${dt} · ${rn.strategy} · ★${(rn.fidelity_score || 0).toFixed(2)}</option>`;
      }).join("");
  }

  function bandColor(score) {
    if (score >= 0.9) return "fid-green";
    if (score >= 0.7) return "fid-yellow";
    return "fid-red";
  }
  function bandLabel(score) {
    if (score >= 0.9) return "Excelente";
    if (score >= 0.7) return "Bom";
    return "Investigar";
  }

  function renderCard(run) {
    const lm = run.live_metrics_json ? JSON.parse(run.live_metrics_json) : {};
    const bm = run.bt_metrics_json ? JSON.parse(run.bt_metrics_json) : {};
    const total = Math.max(run.live_signals, run.bt_signals, 1);
    const pricePct = run.matched > 0 ? (1 - run.price_drift / run.matched) * 100 : 100;
    const indPct = run.matched > 0 ? (1 - run.indicator_drift / run.matched) * 100 : 100;
    const score = run.fidelity_score || 0;
    return `
      <div class="fid-card ${bandColor(score)}" data-run="${run.id}">
        <header>
          <span class="fid-name">${run.strategy}</span>
          <span class="fid-score">★ ${score.toFixed(2)} <em>(${bandLabel(score)})</em></span>
        </header>
        <hr>
        <div class="fid-row">Sinais &nbsp; <b>${run.matched}/${total}</b> matched · ${run.phantom} phantom · ${run.missed} missed · ${run.side_mismatch} lado</div>
        <div class="fid-row">Preço &nbsp; <b>${pricePct.toFixed(0)}%</b> dentro tol (${run.price_drift} drift)</div>
        <div class="fid-row">Indicadores &nbsp; <b>${indPct.toFixed(0)}%</b> dentro tol (${run.indicator_drift} drift)</div>
        <hr>
        <div class="fid-row">Live &nbsp; WR ${(lm.win_rate || 0).toFixed(1)}% · PF ${(lm.profit_factor || 0).toFixed(2)} · ROI ${(lm.roi || 0).toFixed(2)}%</div>
        <div class="fid-row">BT &nbsp;&nbsp; WR ${(bm.win_rate || 0).toFixed(1)}% · PF ${(bm.profit_factor || 0).toFixed(2)} · ROI ${(bm.roi || 0).toFixed(2)}%</div>
      </div>`;
  }

  async function selectRun(runId) {
    _currentRun = runId;
    const r = await fetch(`/api/fidelity/runs/${runId}`);
    const run = await r.json();
    $("#fid-cards").innerHTML = renderCard(run);
    $$("#fid-cards .fid-card").forEach(c => c.addEventListener("click", () => loadDrilldown(runId)));
    loadDrilldown(runId);
  }

  async function loadDrilldown(runId) {
    const r = await fetch(`/api/fidelity/runs/${runId}/diffs`);
    const { diffs } = await r.json();
    _currentDiffs = diffs;
    $("#fid-drilldown").classList.remove("hidden");
    const counts = { signal: 0, trade: 0, metric: 0 };
    diffs.forEach(d => { counts[d.layer] = (counts[d.layer] || 0) + 1; });
    Object.entries(counts).forEach(([k, v]) => {
      const span = $(`.fid-tab [data-count="${k}"]`);
      if (span) span.textContent = v;
    });
    renderTab(_currentLayer);
  }

  function renderTab(layer) {
    _currentLayer = layer;
    $$(".fid-tab").forEach(t => t.classList.toggle("active", t.dataset.layer === layer));
    const rows = _currentDiffs.filter(d => d.layer === layer);
    const types = Array.from(new Set(rows.map(r => r.diff_type)));
    const fbox = $("#fid-filters");
    fbox.innerHTML = `<button class="fid-chip active" data-type="">todos (${rows.length})</button>` +
      types.map(t => `<button class="fid-chip" data-type="${t}">${t} (${rows.filter(r => r.diff_type === t).length})</button>`).join("");

    const head = $("#fid-diff-head");
    head.innerHTML = `<th>ts</th><th>tipo</th><th>side</th><th>Δ%</th><th>causa</th>`;
    renderRows(rows);

    $$("#fid-filters .fid-chip").forEach(c => c.addEventListener("click", () => {
      $$("#fid-filters .fid-chip").forEach(x => x.classList.remove("active"));
      c.classList.add("active");
      const t = c.dataset.type;
      const filtered = t ? rows.filter(r => r.diff_type === t) : rows;
      renderRows(filtered);
    }));
  }

  function renderRows(rows) {
    const body = $("#fid-diff-body");
    body.innerHTML = rows.map(r => {
      const dt = r.ts_ms ? new Date(Number(r.ts_ms)).toLocaleString("pt-BR") : "—";
      const delta = r.delta_pct != null ? (r.delta_pct * 100).toFixed(3) + "%" : "—";
      return `<tr data-id="${r.id}"><td>${dt}</td><td>${r.diff_type}</td><td>${r.side || ""}</td><td>${delta}</td><td>${r.notes || ""}</td></tr>`;
    }).join("");
    $$("#fid-diff-body tr").forEach(tr => tr.addEventListener("click", () => {
      const id = Number(tr.dataset.id);
      const diff = _currentDiffs.find(d => d.id === id);
      openModal(diff);
    }));
  }

  function fmt(json) {
    if (!json) return "—";
    try { return JSON.stringify(JSON.parse(json), null, 2); }
    catch { return json; }
  }

  function openModal(diff) {
    $("#fid-modal").classList.remove("hidden");
    $("#fid-modal-title").textContent = `${diff.layer} · ${diff.diff_type}`;
    $("#fid-modal-live").textContent = fmt(diff.live_json);
    $("#fid-modal-bt").textContent = fmt(diff.bt_json);
    $("#fid-modal-cause").textContent = "Provável causa: " + (diff.notes || "—");
  }

  $(".fid-modal-close").addEventListener("click", () => $("#fid-modal").classList.add("hidden"));
  $$(".fid-tab").forEach(t => t.addEventListener("click", () => renderTab(t.dataset.layer)));

  $("#fid-run").addEventListener("click", async () => {
    const strategy = $("#fid-strategy").value;
    if (!strategy) { alert("Selecione uma estratégia"); return; }
    const days = Number($("#fid-days").value);
    $("#fid-progress").classList.remove("hidden");
    $("#fid-progress").textContent = "Iniciando...";
    const r = await fetch("/api/fidelity/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ strategy, days }),
    });
    const body = await r.json();
    if (body.error) { $("#fid-progress").textContent = "Erro: " + body.error; return; }
    poll(body.job_id);
  });

  async function poll(job_id) {
    const r = await fetch(`/api/fidelity/status/${job_id}`);
    const rec = await r.json();
    $("#fid-progress").textContent = `${rec.status}${rec.elapsed_s ? ` (${rec.elapsed_s}s)` : ""}`;
    if (rec.status === "done") {
      $("#fid-progress").classList.add("hidden");
      await loadHistory();
      await selectRun(rec.result);
      return;
    }
    if (rec.status === "error") {
      $("#fid-progress").textContent = "Erro: " + rec.error;
      return;
    }
    setTimeout(() => poll(job_id), 1500);
  }

  $("#fid-history").addEventListener("change", e => {
    if (e.target.value) selectRun(Number(e.target.value));
  });

  loadStrategies();
  loadHistory();
})();
