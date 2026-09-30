/* 热柜鲜食效期与报损 · 前端（无框架，轮询刷新且保持 DOM 稳定不闪烁） */
"use strict";

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const api = {
  async get(path) { return req("GET", path); },
  async post(path, body) { return req("POST", path, body); },
};
async function req(method, path, body) {
  const headers = { "X-Role": state.role, "X-Actor": state.actor };
  if (body) headers["Content-Type"] = "application/json";
  const res = await fetch(path, {
    method, headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.message || data.error || "请求失败");
    err.status = res.status;
    err.code = data.code;
    throw err;
  }
  return data;
}

const state = {
  role: "clerk",
  actor: "小林(店员)",
  ref: null,
  view: "batches",
  batches: [],
  queue: [],
  pending: [],
  selectedTempBatch: null,
};

const STATUS_LABELS_KEY = {}; // 从 /api/ref 注入

/* ---------- 通用 UI ---------- */
function toast(msg, isError = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (isError ? " error" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.className = "toast"; }, 3200);
}

function modal({ title, body, okText = "确认", onOk, danger = false }) {
  $("#modalTitle").textContent = title;
  $("#modalBody").innerHTML = "";
  $("#modalBody").append(body);
  const ok = $("#modalOk");
  ok.textContent = okText;
  ok.className = "btn " + (danger ? "danger" : "primary");
  $("#modalBackdrop").classList.remove("hidden");
  ok.onclick = async () => {
    try {
      const keep = await onOk();
      if (keep !== false) closeModal();
    } catch (e) {
      toast(e.message, true);
    }
  };
}
function closeModal() { $("#modalBackdrop").classList.add("hidden"); }
$("#modalCancel").onclick = closeModal;
$("#modalBackdrop").addEventListener("click", e => {
  if (e.target.id === "modalBackdrop") closeModal();
});

function fmtTime(isoStr) {
  return isoStr ? isoStr.slice(11, 16) : "--";
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function labels() { return state.ref; }
function statusLabel(s) { return state.ref.status_labels[s] || s; }
function reasonLabel(r) { return state.ref.reason_labels[r] || r || ""; }
function productOf(pid) { return state.ref.products.find(p => p.id === pid); }
function shelfOf(sid) { return state.ref.shelves.find(s => s.id === sid); }

/* ---------- 初始化 ---------- */
async function init() {
  state.ref = await api.get("/api/ref");
  $("#storeName").textContent = state.ref.store.name;
  $("#roleSelect").innerHTML = state.ref.users
    .map(u => `<option value="${u.role}" data-actor="${esc(u.username)}">`
      + `${u.role === "manager" ? "店长" : "店员"} · ${esc(u.name)}</option>`)
    .join("");
  applyRole();
  $("#roleSelect").onchange = applyRole;

  $("#tabs").addEventListener("click", e => {
    const btn = e.target.closest(".tab");
    if (btn) switchView(btn.dataset.view);
  });
  $("#createForm").addEventListener("submit", onCreateBatch);
  $("#tempBatchSelect").addEventListener("change", e => {
    state.selectedTempBatch = Number(e.target.value);
    renderTemps();
  });
  $("#closeStoreBtn").onclick = onCloseStore;
  setInterval(tickClock, 1000);
  tickClock();
  await refresh();
  setInterval(refresh, 5000);
}
function applyRole() {
  const opt = $("#roleSelect").selectedOptions[0];
  if (!opt) return;
  state.role = opt.value;
  state.actor = opt.dataset.actor;
  renderAll();
}
function switchView(v) {
  state.view = v;
  $$(".tab").forEach(t => t.classList.toggle("active", t.dataset.view === v));
  $$(".view").forEach(s => s.classList.toggle("active", s.id === `view-${v}`));
  refresh();
}
function tickClock() {
  $("#clock").textContent = new Date().toLocaleTimeString("zh-CN", { hour12: false });
}

/* ---------- 数据刷新 ---------- */
async function refresh() {
  try {
    const v = state.view;
    if (v === "batches") {
      state.batches = (await api.get("/api/batches")).items;
      renderBatches();
    } else if (v === "cabinet") {
      renderCabinet(await api.get("/api/cabinet"));
    } else if (v === "queue") {
      state.queue = (await api.get("/api/queue")).items;
      renderQueue();
    } else if (v === "temps") {
      if (!state.selectedTempBatch) {
        state.batches = (await api.get("/api/batches")).items;
        populateTempSelect();
      }
      await renderTemps();
    } else if (v === "sales") {
      renderSales(await api.get("/api/sales"), await api.get("/api/audit"));
    } else if (v === "count") {
      state.pending = (await api.get("/api/pending")).items;
      renderPending();
    } else if (v === "report") {
      renderReport(await api.get("/api/report"));
    }
  } catch (e) {
    if (e.message) console.warn(e.message);
  }
}
function renderAll() {
  if (state.view === "batches") renderBatches();
  if (state.view === "queue") renderQueue();
  if (state.view === "count") renderPending();
}

/* ---------- 制作批次视图 ---------- */
function renderBatches() {
  if (!state.ref) return;
  $("#newProduct").innerHTML = state.ref.products
    .map(p => `<option value="${p.id}">${esc(p.name)}（${p.category} · 效期${p.life_minutes}分）</option>`)
    .join("");
  const el = $("#batchList");
  if (!state.batches.length) {
    el.innerHTML = `<div class="empty-state">今日暂无批次，先登记出炉。</div>`;
    return;
  }
  el.innerHTML = state.batches.map(batchCard).join("");
  bindBatchActions(el);
}

function batchCard(b) {
  const isManager = state.role === "manager";
  const near = b.near_expiry;
  const pct = b.initial_qty ? Math.round(b.remaining / b.initial_qty * 100) : 0;
  let actions = "";
  if (b.status === "draft") {
    actions = `
      <div class="temp-field">
        <input type="number" step="0.1" placeholder="抽检℃" data-act="tempval" aria-label="抽检温度">
        <button class="btn small primary" data-act="check" data-id="${b.id}" data-ver="${b.version}">提交抽检</button>
        <button class="btn small ghost warn" data-act="missing" data-id="${b.id}" data-ver="${b.version}">读数缺失</button>
      </div>`;
  } else if (b.status === "checked") {
    const opts = state.ref.shelves
      .map(s => `<option value="${s.id}" ${s.category === b.category ? "" : ""}>${esc(s.label)}（${s.category}）</option>`)
      .join("");
    actions = `
      <select data-act="shelf">${opts}</select>
      <button class="btn small primary" data-act="assign" data-id="${b.id}" data-ver="${b.version}">分配层位开始计时</button>`;
  } else if (b.status === "on_sale") {
    actions = `
      <button class="btn small" data-act="sell1" data-id="${b.id}" data-ver="${b.version}">售出 1 份</button>
      <button class="btn small" data-act="sellmore" data-id="${b.id}" data-ver="${b.version}">售出…</button>
      <button class="btn small ghost warn" data-act="discount" data-id="${b.id}" data-ver="${b.version}">降价</button>
      <button class="btn small ghost warn" data-act="manualoff" data-id="${b.id}" data-ver="${b.version}">人工下架</button>`;
  } else if (b.status === "pending_off") {
    actions = `<span class="hint">已在「下架清点」待清点（${reasonLabel(b.pending_reason)}）</span>`;
  } else if (b.status === "sold_out") {
    actions = `<span class="hint">提前售罄，等待闭店归档</span>`;
  } else if (b.status === "loss") {
    actions = `<span class="hint">已报损 ${b.loss_qty} 份（${reasonLabel(b.loss_reason)}）· 不可恢复销售</span>`;
  }
  const priceHtml = b.discount_rate
    ? `<span class="price-now">¥${b.current_price}</span> <span class="price-old">¥${b.unit_price}</span> ${esc(b.discount_label || "")}`
    : `¥${b.unit_price}`;
  return `
  <div class="batch-card ${near ? "near" : b.status}">
    <div class="batch-head">
      <div>
        <div class="batch-no">${esc(b.batch_no)}</div>
        <div class="batch-prod">${esc(b.product_name)} · ${b.category}</div>
      </div>
      <span class="badge ${b.status}">${statusLabel(b.status)}</span>
    </div>
    <div class="batch-meta">
      <span>出炉 <b>${fmtTime(b.produced_at)}</b></span>
      <span>抽检 <b>${b.check_temp == null ? "缺失" : b.check_temp + "℃"}</b></span>
      <span>层位 <b>${esc(b.shelf_label || "—")}</b></span>
      <span>到期 <b>${b.minutes_left == null ? "—" : b.minutes_left < 0 ? "已到期" : b.minutes_left + " 分"}</b></span>
      <span>现价 <b>${priceHtml}</b></span>
    </div>
    <div class="qty-bar"><i style="width:${pct}%"></i></div>
    <div class="batch-meta"><b>${b.remaining}</b> 份剩余 / 已售 ${b.sold} / 出炉 ${b.initial_qty}</div>
    <div class="batch-actions">${actions}</div>
  </div>`;
}

function bindBatchActions(root) {
  root.addEventListener("click", async e => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const id = Number(btn.dataset.id);
    const ver = Number(btn.dataset.ver);
    const card = btn.closest(".batch-card");
    const act = btn.dataset.act;
    try {
      if (act === "check") {
        const v = Number($('[data-act="tempval"]', card).value);
        if (!v) return toast("请填写抽检温度", true);
        await api.post(`/api/batches/${id}/check-temp`, { temp: v, version: ver });
        toast("抽检温度已记录，可分配层位");
      } else if (act === "missing") {
        await api.post(`/api/batches/${id}/check-temp`, { temp: null, note: "人工登记读数缺失", version: ver });
        toast("已按读数缺失留痕，批次不能上柜", true);
      } else if (act === "assign") {
        const shelfId = Number($('[data-act="shelf"]', card).value);
        await api.post(`/api/batches/${id}/assign`, { shelf_id: shelfId, version: ver });
        toast("已上柜并开始效期计时");
      } else if (act === "sell1") {
        const r = await api.post(`/api/batches/${id}/sell`, { qty: 1, version: ver });
        toast(r.status === "sold_out" ? "该批次已售罄" : "售出 1 份已扣减");
      } else if (act === "sellmore") {
        await askSell(id, ver);
        return;
      } else if (act === "discount") {
        await askDiscount(id, ver);
        return;
      } else if (act === "manualoff") {
        await api.post(`/api/batches/${id}/pending-manual`, { version: ver });
        toast("已转待下架；如属误下架可在下架清点页撤销");
      }
      await refresh();
    } catch (err) {
      toast(err.message, true);
      if (err.status === 409) refresh();
    }
  });
}

async function onCreateBatch(e) {
  e.preventDefault();
  try {
    const b = await api.post("/api/batches", {
      product_id: Number($("#newProduct").value),
      qty: Number($("#newQty").value),
    });
    toast(`批次 ${b.batch_no} 已登记，请记录出炉抽检温度`);
    await refresh();
  } catch (err) { toast(err.message, true); }
}

function askSell(id, ver) {
  const body = document.createElement("div");
  body.innerHTML = `<label class="hint">售出份数
    <input id="sellQty" type="number" min="1" value="1" style="margin-left:8px"></label>`;
  modal({
    title: "按批次扣减销售", body, okText: "确认售出",
    onOk: async () => {
      const qty = Number($("#sellQty", body).value);
      const r = await api.post(`/api/batches/${id}/sell`, { qty, version: ver });
      toast(r.status === "sold_out" ? "该批次已提前售罄并留痕" : `已扣减 ${qty} 份`);
      refresh();
    },
  });
  setTimeout(() => $("#sellQty", body).focus(), 50);
}

function askDiscount(id, ver) {
  const b = state.queue.find(x => x.id === id) || state.batches.find(x => x.id === id);
  const body = document.createElement("div");
  const preset = (state.ref.preset_discounts || []).filter(p => b && b.minutes_left <= p.from_min);
  const presetHtml = preset.length
    ? preset.map(p => `<button class="btn small" data-rate="${p.rate}" data-label="${esc(p.label)}">${esc(p.label)}（剩余≤${p.from_min}分）</button>`).join(" ")
    : `<span class="hint">当前剩余 ${b ? b.minutes_left : "?"} 分，未到预设折扣时点（≤${state.ref.near_minutes}分9折／≤10分7折）</span>`;
  body.innerHTML = `
    <p class="hint">店员只能执行预设折扣；其他价格须由店长输入为例外价。</p>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin:10px 0">${presetHtml}</div>
    <div style="display:flex;gap:8px;align-items:center">
      <input id="exRate" type="number" min="1" max="99" placeholder="折扣率 1-99" style="width:150px">
      <span class="hint">（如 85 = 8.5折，仅店长可用）</span>
    </div>`;
  const doApply = async (rate, label) => {
    await api.post(`/api/batches/${id}/discount`,
      { rate, label: label || `${rate / 10}折`, version: ver, role: state.role });
    toast("降价已留痕");
    closeModal();
    refresh();
  };
  modal({
    title: "临期降价授权", body, okText: "提交例外价（店长）",
    onOk: () => doApply(Number($("#exRate", body).value)),
  });
  $$("button[data-rate]", body).forEach(btn =>
    btn.onclick = () => doApply(Number(btn.dataset.rate), btn.dataset.label).catch(e => toast(e.message, true)));
}

async function onCloseStore() {
  modal({
    title: "执行闭店下架", danger: true, okText: "全部下架",
    body: Object.assign(document.createElement("div"),
      { innerHTML: `<p>将把所有销售中批次转为「待下架（闭店剩余）」并留痕，随后逐批清点报损。</p>` }),
    onOk: async () => {
      const r = await api.post("/api/store/close", {});
      toast(`闭店下架 ${r.batch_ids.length} 批 / ${r.qty} 份`);
      refresh();
    },
  });
}

/* ---------- 热柜陈列图 ---------- */
function renderCabinet(data) {
  const el = $("#cabinetMap");
  el.innerHTML = data.cabinets.map(cab => `
    <div class="cabinet-frame">
      ${cab.shelves.map(sh => `
        <div class="shelf-band">
          <div class="shelf-rail">
            <div class="shelf-name">${esc(sh.label)}</div>
            <div class="shelf-cat">限定品类 · ${sh.category}</div>
          </div>
          <div class="shelf-items">
            ${sh.batches.length ? sh.batches.map(b => {
              const ml = b.minutes_left;
              const cls = ml == null ? "" : ml < 0 ? "overdue" : b.near_expiry ? "near" : "";
              const minText = ml == null ? "计时中" : ml < 0 ? `超期${-ml}分` : `${ml}分`;
              return `<div class="food-tile ${b.near_expiry ? "near" : ""}">
                <div>
                  <div class="ft-name">${esc(b.product_name)}</div>
                  <div class="ft-no">${esc(b.batch_no)}</div>
                </div>
                <div class="ft-stats">
                  <div class="ft-qty">${b.remaining}<small> 份</small></div>
                  <div class="ft-min ${cls}">${minText}</div>
                </div>
                <div class="ft-price">${b.discount_rate ? esc(b.discount_label + " ") : ""}¥${b.current_price}</div>
              </div>`;
            }).join("") : `<div class="shelf-empty">本层暂无在售批次</div>`}
          </div>
        </div>`).join("")}
    </div>`).join("");
}

/* ---------- 效期队列 ---------- */
function urgencyClass(ml) {
  if (ml <= 5) return "u0";
  if (ml <= 10) return "u1";
  if (ml <= 20) return "u2";
  return "u3";
}
function renderQueue() {
  const body = $("#queueBody");
  if (!state.queue.length) {
    body.innerHTML = `<tr><td colspan="8" class="empty-state">暂无在售批次</td></tr>`;
    return;
  }
  body.innerHTML = state.queue.map(b => {
    const ml = b.minutes_left;
    const preset = (state.ref.preset_discounts || []).filter(p => ml <= p.from_min);
    const discountBtns = preset.map(p =>
      `<button class="btn small" data-act="preset" data-id="${b.id}" data-rate="${p.rate}" data-label="${esc(p.label)}" data-ver="${b.version}">${esc(p.label)}</button>`
    ).join("");
    const exBtn = state.role === "manager"
      ? `<button class="btn small danger ghost" data-act="exception" data-id="${b.id}" data-ver="${b.version}">例外价</button>`
      : "";
    return `
    <tr class="${b.near_expiry ? "near-row" : ""}">
      <td><span class="urgency ${urgencyClass(ml)}">${ml < 0 ? "已到期" : ml + "分"}</span></td>
      <td>${esc(b.batch_no)}</td>
      <td>${esc(b.product_name)}</td>
      <td>${esc(b.shelf_label || "—")}</td>
      <td class="num">${b.remaining}</td>
      <td class="num">${fmtTime(b.expiry_at)}</td>
      <td class="num">${b.discount_rate ? `<b class="price-now">¥${b.current_price}</b> <span class="price-old">¥${b.unit_price}</span>` : `¥${b.unit_price}`}</td>
      <td class="row-actions">
        ${discountBtns}
        ${exBtn}
        <button class="btn small" data-act="sell" data-id="${b.id}" data-ver="${b.version}">售出1份</button>
      </td>
    </tr>`;
  }).join("");
  body.onclick = async e => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const id = Number(btn.dataset.id), ver = Number(btn.dataset.ver);
    try {
      if (btn.dataset.act === "preset") {
        await api.post(`/api/batches/${id}/discount`,
          { rate: Number(btn.dataset.rate), label: btn.dataset.label, version: ver, role: "clerk" });
        toast("预设折扣已执行并留痕");
      } else if (btn.dataset.act === "exception") {
        askDiscount(id, ver);
        return;
      } else if (btn.dataset.act === "sell") {
        const r = await api.post(`/api/batches/${id}/sell`, { qty: 1, version: ver });
        if (r.status === "sold_out") toast("批次提前售罄");
      }
      refresh();
    } catch (err) {
      toast(err.message, true);
      if (err.status === 409) refresh();
    }
  };
}

/* ---------- 温度记录视图 ---------- */
async function populateTempSelect() {
  const sel = $("#tempBatchSelect");
  if (sel.options.length) return;
  const batches = state.batches.length ? state.batches : (await api.get("/api/batches")).items;
  state.batches = batches;
  sel.innerHTML = batches.map(b =>
    `<option value="${b.id}">${esc(b.batch_no)} · ${esc(b.product_name)}</option>`).join("");
  if (batches.length) {
    state.selectedTempBatch = batches[0].id;
    sel.value = state.selectedTempBatch;
  }
}
async function renderTemps() {
  if (!state.selectedTempBatch) {
    $("#tempChart").innerHTML = `<div class="empty-state">暂无批次</div>`;
    $("#tempTable").innerHTML = "";
    return;
  }
  const b = state.batches.find(x => x.id === state.selectedTempBatch)
    || await api.get(`/api/batches/${state.selectedTempBatch}`).catch(() => null);
  const data = await api.get(`/api/batches/${state.selectedTempBatch}/temps`);
  const rows = data.items;
  if (b) $("#tempMeta").textContent =
    `合格温区 ${b.temp_min}–${b.temp_max}℃ · 出炉抽检 ${b.check_temp == null ? "缺失" : b.check_temp + "℃"}`;
  $("#tempTable").innerHTML = rows.map(r => `
    <tr>
      <td>${esc(r.created_at.slice(11, 19))}</td>
      <td>${{ check: "出炉抽检", adapter: "探头", manual: "人工" }[r.source] || r.source}</td>
      <td class="num">${r.temp_c == null ? "—" : r.temp_c + "℃"}</td>
      <td>${r.ok ? '<span class="badge on_sale">合格</span>' : '<span class="badge near">不合格</span>'}</td>
      <td>${esc(r.note || "")}</td>
    </tr>`).join("");
  drawTempChart(rows, b);
}

/* 单序列折线：合格温区带 + 不合格点番茄红 + 十字线悬停提示 */
function drawTempChart(rows, b) {
  const host = $("#tempChart");
  host.innerHTML = "";
  if (!rows.length) {
    host.innerHTML = `<div class="empty-state">该批次暂无温度读数</div>`;
    return;
  }
  const W = Math.max(host.clientWidth || 800, 480), H = 220;
  const pad = { l: 44, r: 16, t: 14, b: 34 };
  const temps = rows.map(r => r.temp_c).filter(v => v != null);
  const lo0 = b ? b.temp_min - 6 : 50, hi0 = b ? b.temp_max + 6 : 90;
  const yMin = Math.min(lo0, ...temps) - 2;
  const yMax = Math.max(hi0, ...temps) + 2;
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const x = i => pad.l + (rows.length === 1 ? iw / 2 : i / (rows.length - 1) * iw);
  const y = v => pad.t + ih - (v - yMin) / (yMax - yMin) * ih;
  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  const add = (tag, attrs, parent = svg) => {
    const n = document.createElementNS(svgNS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    parent.appendChild(n);
    return n;
  };
  if (b) {
    add("rect", { class: "range-band", x: pad.l, y: y(b.temp_max),
      width: iw, height: y(b.temp_min) - y(b.temp_max) });
  }
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const v = yMin + (yMax - yMin) * i / ticks;
    const yy = y(v);
    add("line", { class: "axis", x1: pad.l, x2: W - pad.r, y1: yy, y2: yy,
      "stroke-dasharray": "2 4", opacity: .55 });
    const t = add("text", { x: pad.l - 8, y: yy + 4, "text-anchor": "end" });
    t.textContent = Math.round(v);
  }
  rows.forEach((r, i) => {
    const t = add("text", { x: x(i), y: H - 10, "text-anchor": "middle" });
    t.textContent = r.created_at.slice(11, 16);
  });
  const pts = rows.map((r, i) => r.temp_c == null ? null : [x(i), y(r.temp_c)]);
  const seg = pts.filter(Boolean).map(p => p.join(",")).join(" ");
  if (pts.length > 1) add("polyline", { class: "line-temp", points: seg });
  rows.forEach((r, i) => {
    if (r.temp_c == null) {
      add("circle", { class: "pt-missing", cx: x(i), cy: pad.t + ih,
        r: 5, fill: "none", stroke: "#B9B09C", "stroke-width": 2 });
    } else {
      add("circle", { class: r.ok ? "pt-ok" : "pt-bad",
        cx: x(i), cy: y(r.temp_c), r: 4.5 });
    }
  });
  const cross = add("line", { class: "crosshair", y1: pad.t, y2: pad.t + ih });
  const tip = document.createElement("div");
  tip.className = "chart-tip";
  host.style.position = "relative";
  host.appendChild(tip);
  svg.addEventListener("mousemove", e => {
    const rect = svg.getBoundingClientRect();
    const px = (e.clientX - rect.left) / rect.width * W;
    let idx = Math.round((px - pad.l) / iw * (rows.length - 1));
    idx = Math.max(0, Math.min(rows.length - 1, idx));
    const r = rows[idx];
    cross.setAttribute("x1", x(idx)); cross.setAttribute("x2", x(idx));
    cross.setAttribute("opacity", 1);
    tip.style.opacity = 1;
    tip.style.left = `${e.clientX + 12}px`;
    tip.style.top = `${e.clientY - 10}px`;
    tip.innerHTML = `${r.created_at.slice(11, 19)} · ${r.temp_c == null ? "读数缺失" : r.temp_c + "℃"}`
      + `<br>${{ check: "出炉抽检", adapter: "探头", manual: "人工" }[r.source] || r.source}`
      + ` · ${r.ok ? "合格" : "不合格"}${r.note ? " · " + esc(r.note) : ""}`;
  });
  svg.addEventListener("mouseleave", () => {
    cross.setAttribute("opacity", 0);
    tip.style.opacity = 0;
  });
  host.prepend(svg);
}

/* ---------- 销售差异视图 ---------- */
function renderSales(sales, audit) {
  $("#salesBody").innerHTML = sales.items.length ? sales.items.map(s => `
    <tr>
      <td>${esc(s.created_at.slice(11, 19))}</td>
      <td>${esc(s.batch_no)}</td>
      <td>${esc(s.product_name)}</td>
      <td class="num">${s.qty}</td>
      <td class="num">¥${s.unit_price.toFixed(2)}</td>
      <td class="num">¥${s.amount.toFixed(2)}</td>
      <td>${s.source === "adapter" ? "模拟回执" : "收银台"}</td>
    </tr>`).join("")
    : `<tr><td colspan="7" class="empty-state">今日暂无销售</td></tr>`;
  const interesting = new Set([
    "early_soldout", "qty_discrepancy", "wrong_off", "near_overlap",
    "close_remain", "temp_fail", "expired", "discount",
  ]);
  const evs = audit.items.filter(a => interesting.has(a.event_type));
  $("#diffTrail").innerHTML = evs.length ? evs.map(a => {
    const cls = ["early_soldout", "temp_fail", "expired", "qty_discrepancy"].includes(a.event_type)
      ? "ev-danger" : ["close_remain", "near_overlap"].includes(a.event_type) ? "ev-warn" : "ev-ok";
    return `<li class="${cls}">
      <span class="ev-time">${esc(a.created_at.slice(11, 19))}</span>
      <span class="ev-type">${esc(state.ref.audit_labels[a.event_type] || a.event_type)}</span>
      <span class="ev-batch">${esc(a.batch_no || "")}</span>
      ${esc(summarize(a))}
    </li>`;
  }).join("") : `<li class="hint">暂无差异事件</li>`;
}
function summarize(a) {
  const d = a.detail || {};
  switch (a.event_type) {
    case "early_soldout": return `提前售罄 ${d.qty} 份，距到期 ${d.minutes_to_expiry} 分`;
    case "qty_discrepancy": return `账上 ${d.expected_remaining}，实点 ${d.counted}，差异 ${d.difference} 份`;
    case "near_overlap": return `与 ${(d.overlap_with || []).join("、")} 同品临期重叠`;
    case "discount": return `${d.label}（${d.kind === "preset" ? "预设" : "例外"}，${d.role === "manager" ? "店长" : "店员"}）`;
    case "temp_fail": return `连续 ${d.streak} 次不合格（末次 ${d.last_temp}℃）`;
    case "close_remain": return `闭店剩余 ${d.remaining} 份`;
    case "expired": return `效期 ${d.expiry_at ? d.expiry_at.slice(11, 16) : ""} 已过，自动下架`;
    case "wrong_off": return "人工下架为误操作，已恢复销售";
    default: return "";
  }
}

/* ---------- 下架清点视图（触屏步进器） ---------- */
function renderPending() {
  const el = $("#pendingList");
  if (!state.pending.length) {
    el.innerHTML = `<div class="empty-state">没有待下架批次，一切在柜正常。</div>`;
    return;
  }
  el.innerHTML = state.pending.map(b => `
    <div class="count-card ${b.pending_reason === "temp" ? "temp" : ""}" data-id="${b.id}">
      <h4>${esc(b.product_name)} <span class="hint">${esc(b.batch_no)}</span></h4>
      <div class="count-reason">下架原因：${reasonLabel(b.pending_reason)}${b.pending_reason === "manual" ? "（误下架可撤销）" : ""}</div>
      <div class="count-expected">账上剩余 <b>${b.remaining}</b> 份 · 层位 ${esc(b.shelf_label || "—")}</div>
      <div class="stepper" data-id="${b.id}">
        <button type="button" data-step="-1" aria-label="减少">−</button>
        <input type="number" value="${b.remaining}" min="0" max="${b.remaining}"
               inputmode="numeric" aria-label="实点数量">
        <button type="button" data-step="1" aria-label="增加">＋</button>
      </div>
      <div class="count-foot">
        <span class="diff-note ok" data-diff>账实相符</span>
        <span style="display:flex;gap:8px">
          ${b.pending_reason === "manual"
            ? `<button class="btn small" data-act="restore" data-id="${b.id}" data-ver="${b.version}">撤销误下架</button>` : ""}
          <button class="btn small primary" data-act="confirm" data-id="${b.id}" data-ver="${b.version}">确认报损</button>
        </span>
      </div>
    </div>`).join("");

  $$(".count-card", el).forEach(card => {
    const id = Number(card.dataset.id);
    const b = state.pending.find(x => x.id === id);
    const input = $(".stepper input", card);
    const note = $("[data-diff]", card);
    const sync = () => {
      let v = Number(input.value);
      if (Number.isNaN(v)) v = 0;
      v = Math.max(0, Math.min(b.remaining, v));
      const diff = b.remaining - v;
      note.textContent = diff === 0 ? "账实相符"
        : `账实差异 ${diff} 份（已售未扣减？将留痕）`;
      note.className = "diff-note " + (diff === 0 ? "ok" : "bad");
    };
    $$(".stepper button", card).forEach(btn =>
      btn.onclick = () => {
        input.value = Math.max(0, Math.min(b.remaining,
          Number(input.value || 0) + Number(btn.dataset.step)));
        sync();
      });
    input.oninput = sync;
  });

  el.onclick = async e => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const id = Number(btn.dataset.id), ver = Number(btn.dataset.ver);
    const card = btn.closest(".count-card");
    try {
      if (btn.dataset.act === "restore") {
        await api.post(`/api/batches/${id}/restore`, { version: ver });
        toast("误下架已撤销，批次恢复销售并留痕");
      } else {
        const qty = Number($(".stepper input", card).value);
        const r = await api.post(`/api/batches/${id}/loss`,
          { counted_qty: qty, version: ver });
        const d = r.meta;
        toast(d.difference === 0
          ? `报损 ${d.qty} 份完成（成本 ¥${d.loss_cost}），不可恢复`
          : `报损完成，账实差异 ${d.difference} 份已留痕`, true);
      }
      refresh();
    } catch (err) {
      toast(err.message, true);
      if (err.status === 409) refresh();
    }
  };
}

/* ---------- 日报视图 ---------- */
function tile(v, k, tomato) {
  return `<div class="stat-tile"><div class="v ${tomato ? "tomato" : ""}">${v}</div><div class="k">${k}</div></div>`;
}
function renderReport(r) {
  $("#reportTitle").textContent = `${r.store.name} · 日报 ${r.date}`;
  $("#reportStats").innerHTML =
    tile(r.batch_count, "出炉批次") +
    tile(r.produced_qty, "出炉份数") +
    tile(r.sold_qty, "销售份数") +
    tile("¥" + r.revenue.toFixed(2), "销售额") +
    tile(r.loss_qty, "报损份数", true) +
    tile("¥" + r.loss_cost.toFixed(2), "报损成本", true) +
    tile("¥" + r.discount_given.toFixed(2), "折扣让利") +
    tile((r.waste_rate * 100).toFixed(1) + "%", "报损率", r.waste_rate > .15);
  $("#reportLossBody").innerHTML = r.losses.length ? r.losses.map(l => `
    <tr>
      <td>${esc(l.created_at.slice(11, 16))}</td>
      <td>${esc(l.batch_no)}</td>
      <td>${esc(l.product_name)}</td>
      <td class="num">${l.qty}</td>
      <td>${reasonLabel(l.reason)}</td>
      <td>${esc(l.confirmed_by)}</td>
    </tr>`).join("")
    : `<tr><td colspan="6" class="empty-state">今日暂无报损</td></tr>`;
  const ec = r.event_counts;
  const ordered = Object.keys(state.ref.audit_labels);
  $("#reportEventCounts").innerHTML = ordered
    .filter(k => ec[k]).map(k =>
      `<div class="ec-row"><span>${state.ref.audit_labels[k]}</span><span class="ec-n">${ec[k]}</span></div>`)
    .join("") || `<span class="hint">今日暂无异常事件</span>`;
  api.get("/api/audit").then(audit => {
    $("#auditTrail").innerHTML = audit.items.map(a0 => `
      <li class="${["temp_missing", "wrong_shelf", "qty_discrepancy", "early_soldout",
        "temp_fail", "expired", "close_remain"].includes(a0.event_type) ? "ev-warn" : "ev-ok"}">
        <span class="ev-time">${esc(a0.created_at.slice(11, 19))}</span>
        <span class="ev-type">${esc(state.ref.audit_labels[a0.event_type] || a0.event_type)}</span>
        <span class="ev-batch">${esc(a0.batch_no || "")}</span>
        <span class="hint">${esc(a0.actor || "")}</span>
      </li>`).join("");
  });
}

init().catch(e => toast("初始化失败：" + e.message, true));
