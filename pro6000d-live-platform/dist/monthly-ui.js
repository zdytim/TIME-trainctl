/* Monthly GPU-hour presentation; controller readiness is supplied by the API. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.TimeMonthlyUI = factory();
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';
  const escape = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const hours = v => typeof v === 'number' && Number.isFinite(v) && v >= 0 ? (v / 3600).toFixed(2) : '—';
  const names = ['debug', 'important', 'normal'];
  function panel(view) {
    const q = view || {};
    const active = q.enforcement_ready === true && q.selectable_tiers_ready === true;
    const status = active ? '强制调度已启用' : '强制调度未启用';
    const apply = active && q.request_available === true ? '<button type="button" class="pf-c-button pf-m-secondary" data-action="monthly-apply">申请追加 GPU 小时</button>' : '';
    const valid = q.usage_verified === true && Array.isArray(q.tiers) &&
      names.every(name => q.tiers.filter(t => t && t.name === name).length === 1);
    if (!valid) return `<section class="panel section-space"><div class="panelhead"><h2>月度 GPU 小时</h2><span>${status}</span></div><div class="panelbody">${escape(q.enforcement_limitation || '月度账本暂不可用。')}</div></section>`;
    const rows = names.map(name => {
      const t = q.tiers.find(item => item.name === name);
      return `<tr><td>${escape(name)}</td><td>${hours(t.limit_gpu_seconds)}</td><td>${hours(t.used_gpu_seconds)}</td><td>${hours(t.remaining_gpu_seconds)}</td></tr>`;
    }).join('');
    return `<section class="panel section-space"><div class="panelhead"><h2>月度 GPU 小时 · ${escape(q.month || '—')}</h2><span>${status} ${apply}</span></div><div class="panelbody"><div class="table-scroll"><table><thead><tr><th>档位</th><th>额度</th><th>已使用</th><th>账面剩余</th></tr></thead><tbody>${rows}</tbody></table></div><p class="small">单位：GPU·小时。账面剩余未扣除排队或运行任务的未消耗预留，实际可预留额度可能更少；超额 ${hours(q.overage_gpu_seconds)}，运行任务继续计费。</p>${active?'':`<div class="notice">${escape(q.enforcement_limitation || '账本仅供查看，调度门控尚未启用。')}</div>`}</div></section>`;
  }
  function selector(view, selected) {
    const q = view || {};
    if (q.enforcement_ready !== true || q.selectable_tiers_ready !== true) return '';
    const current = names.includes(selected) ? selected : 'normal';
    return `<div class="field"><label for="f-selected_tier">月度额度档位</label><select class="control" id="f-selected_tier" name="selected_tier">${names.map(name => `<option value="${name}"${name === current ? ' selected' : ''}>${name}</option>`).join('')}</select><div class="help">启动前，所选档位余额须覆盖 GPU 数量乘申请时限。</div></div>`;
  }
  return {panel, selector};
});
