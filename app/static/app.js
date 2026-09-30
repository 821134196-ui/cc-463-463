/* 热柜鲜食台前端:轮询状态,七个页签各自渲染。 */
'use strict';

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const state = { data: null, products: new Map(), shelves: new Map() };

const STATUS_LABEL = {
  made: '待抽检', placed: '在柜销售', pending: '待下架',
  discarded: '已报损', soldout: '已售罄',
};
const REASON_LABEL = {
  expired: '到期', temp: '温度持续不合格', close_remaining: '闭店剩余',
};
const EVENT_LABEL = {
  baked: '出炉', placed: '上柜', probe: '抽检温度', temp_bad: '温度超温',
  temp_missing: ['温度读数缺失', 'bad'], wrong_shelf: ['上错层位', 'bad'],
  shelf_over_capacity: ['层位超容量', 'bad'], near_expiry: '进入临期',
  near_overlap: ['临期重叠', 'bad'], to_pending: '转待下架',
  early_soldout: ['提前售罄', 'good'], soldout: ['售罄', 'good'],
  qty_adjust: '数量修订', qty_variance: ['数量差异', 'bad'], count_ok: ['清点一致', 'good'],
  discount: '降价', wasted: '完成报损', wrong_pull_restored: ['误下架已撤回', 'good'],
  close_remaining: '闭店剩余', sim_sale_rejected: ['模拟销售被拒', 'bad'],
};

// ---------------- 基础工具 ---------------- //
function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return d.toTimeString().slice(0, 5);
}
function fmtFull(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${d.toTimeString().slice(0, 8)}`;
}
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function money(v) { return '¥' + Number(v).toFixed(2); }
function role() { return $('#role').value; }

async function api(path, body) {
  const opt = body ? {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ role: role(), ...body }),
  } : {};
  const res = await fetch(path, opt);
  const j = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(j.error || ('请求失败 ' + res.status));
  return j;
}
let toastTimer;
function toast(msg, isErr) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast' + (isErr ? ' err' : '');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add('hidden'), 3200);
}

// ---------------- 页签切换 ---------------- //
$('#tabs').addEventListener('click', e => {
  const btn = e.target.closest('button[data-tab]');
  if (!btn) return;
  $$('.tabs button').forEach(b => b.classList.toggle('active', b === btn));
  $$('.tab').forEach(s => s.classList.toggle('active', s.id === 'tab-' + btn.dataset.tab));
});

// ---------------- 数据轮询 ---------------- //
async function refresh() {
  try {
    const d = await api('/api/state');
    state.data = d;
    state.products = new Map(d.products.map(p => [p.id, p]));
    state.shelves = new Map(d.shelves.map(s => [s.id, s]));
    $('#sim-clock').textContent = fmtFull(d.now).slice(6) + ' 模拟时钟';
    render(d);
  } catch (e) { /* 网络抖动忽略,下轮重试 */ }
}

function render(d) {
  renderMakeProducts(d);
  renderBatches(d);
  renderCabinet(d);
  renderExpiry(d);
  renderPull(d);
  renderReport(d);
  $('#pull-badge').textContent = d.batches.filter(b => b.status === 'pending').length;
  $('#pull-badge').classList.toggle('hidden', !d.batches.some(b => b.status === 'pending'));
}

function tempTag(b) {
  if (b.temp_state === 'bad') return '<span class="tag bad">温度超温</span>';
  if (b.temp_state === 'missing') return '<span class="tag missing">温度缺失</span>';
  if (b.temp_state === 'ok') return '<span class="tag ok">温度合格</span>';
  return '<span class="tag">未测温</span>';
}
function statusTag(b, d) {
  const m = b.minutes_to_expire;
  const near = b.status === 'placed' && m != null && m <= d.near_window;
  return `<span class="tag ${near ? 'near' : ''}">${STATUS_LABEL[b.status]}${
    near ? ` · ${m}min` : ''}</span>`;
}

// ---------------- 制作批次 ---------------- //
function renderMakeProducts(d) {
  const sel = $('#make-product');
  if (sel.options.length) return;
  d.products.forEach(p => {
    const o = document.createElement('option');
    o.value = p.id;
    o.textContent = `${p.name}(${money(p.base_price)} · 效期${p.life_minutes}分钟)`;
    sel.appendChild(o);
  });
}
$('#form-make').addEventListener('submit', async e => {
  e.preventDefault();
  try {
    await api('/api/batch/make', {
      product_id: $('#make-product').value, qty: e.target.qty.value,
    });
    toast('已记录出炉,请尽快抽检温度');
    refresh();
  } catch (err) { toast(err.message, true); }
});

function renderBatches(d) {
  const box = $('#batch-list');
  box.innerHTML = '';
  d.batches.forEach(b => {
    const card = document.createElement('div');
    card.className = `batch-card ${b.status}`;
    const p = state.products.get(b.product_id);
    const shelf = b.shelf_id ? state.shelves.get(b.shelf_id) : null;
    const wrong = shelf && shelf.zone !== b.category;
    card.innerHTML = `
      <div class="bc-head">
        <div><div class="bc-name">${esc(b.product_name)}</div>
        <div class="bc-no">${b.batch_no} · v${b.version}</div></div>
        <div class="bc-qty">${b.qty_remaining}<small>/${b.qty_initial}份</small></div>
      </div>
      <div class="bc-meta">
        ${statusTag(b, d)}${tempTag(b)}
        ${shelf ? `<span class="tag place ${wrong ? 'bad' : ''}">${esc(shelf.label)}${wrong ? ' ⚠错层' : ''}</span>` : ''}
        ${b.discount ? `<span class="tag disc">${Math.round(b.discount * 100)}%价</span>` : ''}
        ${b.status === 'discarded' ? `<span class="tag bad">报损:${REASON_LABEL[b.waste_reason] || b.waste_reason} ${b.wasted_qty ?? ''}份</span>` : ''}
        ${b.status === 'made' ? '<span class="tag">出炉 ' + fmtTime(b.produced_at) + '</span>' : ''}
      </div>
      <div class="bc-actions">${batchActions(b, p)}</div>`;
    box.appendChild(card);
  });
  box.querySelectorAll('[data-act]').forEach(btn =>
    btn.addEventListener('click', () => onAction(btn.dataset.act, btn.dataset.id)));
}

function batchActions(b, p) {
  const btns = [];
  if (b.status === 'made') {
    btns.push(`<button class="btn primary sm" data-act="probe" data-id="${b.id}">①记录抽检温度</button>`);
  }
  if (b.status === 'made' && b.probed_at && b.temp_state !== 'bad') {
    btns.push(`<button class="btn sm ghost" data-act="place" data-id="${b.id}">②分配层位上柜</button>`);
  }
  if (b.status === 'placed') {
    btns.push(`<button class="btn primary sm" data-act="sell" data-id="${b.id}">销售扣减</button>`);
    btns.push(`<button class="btn sm ghost" data-act="discount" data-id="${b.id}">降价</button>`);
    btns.push(`<button class="btn sm ghost" data-act="adjust" data-id="${b.id}">修订数量</button>`);
  }
  if (b.status === 'pending') {
    btns.push(`<button class="btn primary sm" data-act="count" data-id="${b.id}">去清点报损</button>`);
    btns.push(`<button class="btn sm ghost" data-act="restore" data-id="${b.id}">误下架撤回</button>`);
  }
  return btns.join('');
}

// ---------------- 弹层动作 ---------------- //
let modalSubmit = null;
function openModal(title, bodyHtml, onOk) {
  $('#modal-title').textContent = title;
  $('#modal-body').innerHTML = bodyHtml;
  modalSubmit = onOk;
  $('#modal').classList.remove('hidden');
}
$('#modal-cancel').onclick = () => $('#modal').classList.add('hidden');
$('#modal-ok').onclick = async () => {
  if (!modalSubmit) return;
  try {
    await modalSubmit();
    $('#modal').classList.add('hidden');
    refresh();
  } catch (err) { toast(err.message, true); }
};

async function onAction(act, id) {
  const b = state.data.batches.find(x => x.id === id);
  const p = state.products.get(b.product_id);
  if (act === 'probe') {
    openModal('记录抽检温度(中心温度)', `
      <label>温度 ℃(合格区间 ${p.temp_min}~${p.temp_max})</label>
      <input type="number" id="m-temp" step="0.1" value="67.0">
      <label>备注</label><input type="text" id="m-note" placeholder="如:出炉抽检">
      <label>抽检照片/记录附件(可选,存本地目录)</label>
      <input type="file" id="m-file" accept="image/*,.pdf">`,
      async () => {
        let attachment_b64;
        const f = $('#m-file').files[0];
        if (f) attachment_b64 = await new Promise((res, rej) => {
          const fr = new FileReader();
          fr.onload = () => res(fr.result.split(',')[1]);
          fr.onerror = rej;
          fr.readAsDataURL(f);
        });
        return api('/api/batch/probe', { batch_id: id, temp: $('#m-temp').value,
          note: $('#m-note').value, attachment_b64 });
      });
  } else if (act === 'place') {
    const opts = state.data.shelves.map(s =>
      `<option value="${s.id}" ${s.zone === b.category ? '' : ''}>${s.label}${s.zone === b.category ? '' : '（非同品类区⚠）'}</option>`).join('');
    openModal('分配热柜层位并开始计时',
      `<label>层位</label><select id="m-shelf">${opts}</select>
      <p class="hint">效期 ${p.life_minutes} 分钟,从上柜时刻起算</p>`,
      () => api('/api/batch/place', { batch_id: id, shelf_id: $('#m-shelf').value }));
  } else if (act === 'sell') {
    openModal(`销售扣减 · ${b.product_name}`, `
      <label>当前剩余 ${b.qty_remaining} 份(数据版本 v${b.version})</label>
      <label>本次扣减份数</label><input type="number" id="m-qty" value="1" min="1">
      <p class="hint">提交时携带版本号,他人先改则本次提交失败</p>`,
      () => api('/api/sell', { batch_id: id, qty: $('#m-qty').value,
        expected_version: b.version }).then(r => toast(`扣减成功,回执 ${r.receipt_no},剩 ${r.remaining} 份`)));
  } else if (act === 'adjust') {
    openModal(`修订数量 · ${b.product_name}`, `
      <label>账上剩余 ${b.qty_remaining} 份(版本 v${b.version})</label>
      <label>修订为</label><input type="number" id="m-newqty" value="${b.qty_remaining}">`,
      () => api('/api/adjust', { batch_id: id, new_qty: $('#m-newqty').value,
        expected_version: b.version }));
  } else if (act === 'discount') {
    const presets = JSON.parse(p.preset_discounts);
    const near = b.minutes_to_expire != null && b.minutes_to_expire <= state.data.near_window;
    openModal(`降价 · ${b.product_name}`, `
      <p>当前 ${b.discount ? Math.round(b.discount * 100) + '%价' : '原价'} ${money(p.base_price)}
      ${near ? ' · 已临期' : ' · <b style="color:var(--tomato-dark)">未临期</b>'}</p>
      <label>新折扣(${role() === 'manager' ? '店长:可输入任意折扣,需二次确认' : '店员:仅预设折扣 ' + presets.map(x => Math.round(x * 100) + '%').join('/') + ' 且限临期'})</label>
      <input type="number" id="m-disc" step="0.01" min="0.05" max="1" value="${presets[0] ?? 0.8}">
      <label><input type="checkbox" id="m-force"> 我确认执行本次调价(店长例外价格必勾)</label>`,
      async () => {
        try {
          await api('/api/discount', { batch_id: id, discount: $('#m-disc').value,
            force: $('#m-force').checked });
        } catch (e) {
          if (role() !== 'manager' && /预设|临期/.test(e.message)) throw e;
          throw e;
        }
      });
  } else if (act === 'restore') {
    openModal(`误下架撤回 · ${b.product_name}`, `
      <p>下架原因:<b>${REASON_LABEL[b.waste_reason] || b.waste_reason}</b></p>
      <label>说明</label><input type="text" id="m-note" placeholder="如:设备误判,复温合格">
      <p class="hint">已报损或确实到期的批次不能撤回</p>`,
      () => api('/api/restore', { batch_id: id, note: $('#m-note').value }));
  } else if (act === 'count') {
    $$('.tabs button').forEach(x => x.classList.toggle('active', x.dataset.tab === 'pull'));
    $$('.tab').forEach(x => x.classList.toggle('active', x.id === 'tab-pull'));
  }
}

// ---------------- 热柜陈列图 ---------------- //
function renderCabinet(d) {
  const box = $('#cabinet');
  box.innerHTML = '';
  // 从上到下绘制实际层板
  const layers = [...d.shelves].sort((a, b2) => b2.layer_index - a.layer_index);
  layers.forEach(s => {
    const bs = d.batches.filter(b => b.shelf_id === s.id &&
      (b.status === 'placed' || b.status === 'pending'));
    const used = bs.reduce((n, b) => n + b.qty_remaining, 0);
    const layer = document.createElement('div');
    layer.className = 'layer';
    layer.innerHTML = `
      <div class="layer-label"><span>${esc(s.cabinet_name)} · ${esc(s.label)}</span>
        <span>${used}/${s.capacity} 份${used > s.capacity ? ' · ⚠超容量' : ''}</span></div>
      <div class="layer-track ${bs.length ? '' : 'empty'}"></div>`;
    const track = layer.querySelector('.layer-track');
    bs.forEach(b => {
      const near = b.minutes_to_expire != null && b.minutes_to_expire <= d.near_window;
      const wrong = s.zone !== b.category;
      const tile = document.createElement('div');
      tile.className = 'tile' + (b.status === 'pending' ? ' pending' : '');
      tile.innerHTML = `
        <div><div class="tile-name">${esc(b.product_name)}${wrong ? ' ⚠' : ''}</div>
        <div class="tile-no">${b.batch_no}</div></div>
        <div class="tile-stats">
          <div class="tile-qty">${b.qty_remaining}<small>份</small></div>
          <div class="tile-min ${near ? 'near' : ''}">${
            b.status === 'pending' ? '待下架' : '剩' + Math.max(0, b.minutes_to_expire) + 'min'}</div>
        </div>`;
      track.appendChild(tile);
    });
    box.appendChild(layer);
  });
}

// ---------------- 效期队列 ---------------- //
function renderExpiry(d) {
  const list = d.batches
    .filter(b => b.status === 'placed' && b.minutes_to_expire != null)
    .sort((a, b2) => a.minutes_to_expire - b2.minutes_to_expire);
  $('#expiry-list').innerHTML = list.length ? list.map(b => {
    const p = state.products.get(b.product_id);
    const m = Math.max(0, b.minutes_to_expire);
    const near = m <= d.near_window;
    const pct = Math.max(4, Math.min(100, 100 - m / p.life_minutes * 100));
    return `<div class="qrow">
      <div class="qmin ${near ? 'near' : ''}">${near ? '仅剩 ' : ''}${m} 分钟</div>
      <div class="qbar"><i style="width:${pct}%"></i></div>
      <div class="qname">${esc(b.product_name)} <span class="bc-no">${b.batch_no}</span></div>
      <span class="tag">余 ${b.qty_remaining} 份</span>
      ${b.discount ? `<span class="tag disc">${Math.round(b.discount * 100)}%价</span>` :
        `<span class="tag">${money(p.base_price)}</span>`}
      ${tempTag(b)}
      <div class="spacer"></div>
      <button class="btn sm primary" data-act="discount" data-id="${b.id}">降价</button>
      <button class="btn sm ghost" data-act="sell" data-id="${b.id}">销售</button>
    </div>`;
  }).join('') : '<p class="hint">当前无在柜批次</p>';
  $('#expiry-list').querySelectorAll('[data-act]').forEach(btn =>
    btn.addEventListener('click', () => onAction(btn.dataset.act, btn.dataset.id)));
}

// ---------------- 温度 / 销售 / 事件(异步表格) ---------------- //
async function loadAuxTables() {
  try {
    const [temps, sales, events] = await Promise.all([
      api('/api/temps'), api('/api/sales'), api('/api/events?limit=100'),
    ]);
    $('#temp-table').querySelector('tbody').innerHTML = temps.map(t => `
      <tr><td>${fmtFull(t.ts)}</td><td>${esc(t.batch_no)}</td><td>${esc(t.product_name)}</td>
      <td class="${t.ok ? 'ok-text' : 'bad-text'}">${t.temp.toFixed(1)}℃</td>
      <td>${t.source === 'probe' ? '出炉抽检' : '传感器'}</td>
      <td class="${t.ok ? 'ok-text' : 'bad-text'}">${t.ok ? '合格' : '超温'}</td>
      <td>${esc(t.note || '')}${t.attachment ? ` <a href="/api/attachment?id=${t.id}" target="_blank">📎附件</a>` : ''}</td></tr>`).join('');
    $('#sales-table').querySelector('tbody').innerHTML = sales.map(s => `
      <tr><td>${fmtFull(s.ts)}</td><td>${esc(s.batch_no)}</td><td>${esc(s.product_name)}</td>
      <td>${s.qty}</td><td>${money(s.unit_price)}
      ${s.unit_price < s.base_price ? '<span class="tag disc">折扣</span>' : ''}</td>
      <td>${esc(s.receipt_no)}</td><td>${esc(s.created_by)}</td></tr>`).join('');
    $('#event-list').innerHTML = events.map(ev => {
      const meta = EVENT_LABEL[ev.event_type] || ev.event_type;
      const [label, cls = 'neutral'] = Array.isArray(meta) ? meta : [meta, 'neutral'];
      let detail = ev.detail;
      try { detail = JSON.stringify(JSON.parse(ev.detail), null, 0); } catch {}
      return `<div class="event ${cls}"><span class="et-time">${fmtFull(ev.ts)}</span>
        <span class="et-type">${esc(label)}</span>${esc(detail)}
        <span class="hint">— ${esc(ev.actor)}</span></div>`;
    }).join('') || '<p class="hint">暂无留痕</p>';
  } catch {}
}

// ---------------- 下架清点(触屏步进器) ---------------- //
function renderPull(d) {
  const pend = d.batches.filter(b => b.status === 'pending');
  $('#pull-list').innerHTML = pend.length ? pend.map(b => {
    const reason = REASON_LABEL[b.waste_reason] || b.waste_reason;
    return `<div class="count-card" data-id="${b.id}">
      <div class="count-info">
        <div class="nm">${esc(b.product_name)}</div>
        <div class="sub">${b.batch_no} · 下架原因:${reason} · 账上 <b>${b.qty_remaining}</b> 份</div>
      </div>
      <div class="stepper">
        <button type="button" data-step="-1">−</button>
        <input type="number" class="count-input" value="${b.qty_remaining}" min="0" max="${b.qty_initial}">
        <button type="button" data-step="1">＋</button>
      </div>
      <div class="diff-note grow"></div>
      <button class="btn ghost sm btn-count">先清点(差异留痕)</button>
      <button class="btn primary btn-waste">确认报损(不可逆)</button>
    </div>`;
  }).join('') : '<div class="card"><p class="hint">没有待下架批次。到期或温度持续不合格的批次会自动进入本页。</p></div>';

  $$('#pull-list .count-card').forEach(card => {
    const id = card.dataset.id;
    const b = pend.find(x => x.id === id);
    const input = $('.count-input', card);
    const note = $('.diff-note', card);
    const showDiff = () => {
      const v = parseInt(input.value, 10);
      if (isNaN(v)) return;
      const diff = v - b.qty_remaining;
      note.innerHTML = diff === 0
        ? '<span class="diff-ok">账实一致</span>'
        : `<span class="diff-bad">差异 ${diff > 0 ? '+' : ''}${diff} 份,将留痕</span>`;
    };
    $$('.stepper button', card).forEach(btn => btn.addEventListener('click', () => {
      let v = (parseInt(input.value, 10) || 0) + Number(btn.dataset.step);
      v = Math.max(0, Math.min(b.qty_initial, v));
      input.value = v; showDiff();
    }));
    input.addEventListener('input', showDiff);
    $('.btn-count', card).addEventListener('click', async () => {
      try { await api('/api/count', { batch_id: id, counted_qty: input.value });
        toast('清点已记录'); refresh();
      } catch (e) { toast(e.message, true); }
    });
    $('.btn-waste', card).addEventListener('click', async () => {
      if (!confirm(`确认按实物 ${input.value} 份报损?报损后不可恢复销售。`)) return;
      try { const r = await api('/api/waste', { batch_id: id, counted_qty: input.value });
        toast(`已报损 ${r.wasted_qty} 份(${REASON_LABEL[r.reason] || r.reason})`); refresh();
      } catch (e) { toast(e.message, true); }
    });
  });
}

$('#form-close').addEventListener('submit', async e => {
  e.preventDefault();
  if (!confirm('闭店操作:全部在柜批次转待下架?之后需逐批清点报损。')) return;
  try { const r = await api('/api/close');
    toast(`${r.pending.length} 个批次转入闭店剩余,请清点`); refresh();
  } catch (err) { toast(err.message, true); }
});

// ---------------- 日报 ---------------- //
async function renderReport(d) {
  if (!$('#tab-report').classList.contains('active')) return;
  try {
    const r = await api(`/api/report?hours=24&end=${d.now}`);
    $('#report-kpis').innerHTML = `
      <div class="kpi"><div class="v">${r.batch_count}</div><div class="l">制作批次</div></div>
      <div class="kpi"><div class="v">${r.sold_qty}</div><div class="l">销售份数</div></div>
      <div class="kpi"><div class="v">${money(r.revenue)}</div><div class="l">销售额(样例)</div></div>
      <div class="kpi"><div class="v">${r.waste_qty}</div><div class="l">报损份数</div></div>
      <div class="kpi"><div class="v">${money(r.waste_cost)}</div><div class="l">报损成本(样例)</div></div>`;
    const reasons = { expired: '到期', temp: '温度不合格', close_remaining: '闭店剩余' };
    $('#report-detail').innerHTML = '<h3 style="font-size:14px">报损构成</h3>' +
      Object.entries(r.waste_by_reason).map(([k, v]) =>
        `<span class="tag bad" style="margin-right:8px">${reasons[k] || k}:${v} 份</span>`).join('')
      || '<p class="hint">近 24 小时无报损</p>';
  } catch {}
}

// ---------------- 启动 ---------------- //
refresh();
setInterval(refresh, 2000);
setInterval(loadAuxTables, 4000);
loadAuxTables();
