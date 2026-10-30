/* Cockpit training console. Production never falls back to simulated identities. */
(function () {
  'use strict';
  const $ = (s, root = document) => root.querySelector(s);
  const esc = v => String(v == null ? '' : v).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const fmt = v => v ? new Date(v).toLocaleString() : '—';
  const activeStates = ['WAITING_QUOTA','WAITING_MONTHLY_QUOTA','SUBMITTING','SUBMIT_UNKNOWN','PENDING','RUNNING','COMPLETING','CONFIGURING'];
  const statusNames = {WAITING_QUOTA:'等待并发资源',WAITING_MONTHLY_QUOTA:'等待月度额度',SUBMITTING:'提交结果待确认',SUBMIT_UNKNOWN:'提交结果待确认',PENDING:'调度排队',RUNNING:'运行中',COMPLETING:'正在结束',COMPLETED:'已完成',FAILED:'失败',CANCELLED:'已取消',CANCELED:'已取消',TIMEOUT:'超时',NODE_FAIL:'节点故障',OUT_OF_MEMORY:'内存不足',UNKNOWN:'状态待确认',REJECTED:'已拒绝',APPROVED:'已批准',EXPIRED:'已过期',DISABLED:'暂不可用',USED:'已使用'};
  let state = null, page = 'training', queueTab = 'queue', modalState = null;
  let monitorGPU='all', monitorProcessScope='all', monitorSort='memory_used_mb', monitorSortDirection='desc';
  let disconnected = false, refreshBusy = false, ruleDirty = false;
  let lastError = '', queuedState = null, auditRows = null;
  const integerFields = ['gpu','cpu','memory_mb','time_minutes','gpu_limit','concurrent_job_limit','active_job_limit','valid_minutes','gpu_hours'];
  const concurrentLimit = q => q.concurrent_job_limit == null ? q.concurrent_gpu_limit : q.concurrent_job_limit;
  const quotaText = q => `${q.gpu_limit} GPU / ${concurrentLimit(q)} 个并发任务 / ${q.active_job_limit} 个活动任务`;
  const object = v => { if (typeof v === 'string') {try {return JSON.parse(v);} catch (_) {return {};}} return v || {}; };
  const requestData = a => object(a.requested || a.requested_json);
  const approvedData = a => object(a.approved || a.approved_json);
  const isAdmin = () => !!(state && state.identity.is_admin);
  const isManagedJob = j => j && j.managed !== false && j.origin !== 'external';
  const button = (label, action, attrs = '', primary = false) => `<button type="button" class="pf-c-button ${primary?'pf-m-primary':'pf-m-secondary'}" data-action="${action}" ${attrs}>${label}</button>`;
  const badge = (text, color='') => `<span class="tag ${color}">${esc(text)}</span>`;
  const stateBadge = s => badge(statusNames[s] || s, ({RUNNING:'green',APPROVED:'green',COMPLETED:'green',PENDING:'blue',WAITING_QUOTA:'amber',SUBMITTING:'amber',FAILED:'red',REJECTED:'red',UNKNOWN:'amber'})[s] || '');
  const head = (title, subtitle, actions='') => `<div class="pagehead"><div><h1>${esc(title)}</h1><div class="subtitle">${esc(subtitle)}</div></div><div class="actions">${actions}</div></div>`;
  const errorText = e => (e && (e.message || e.code)) || '操作未完成，请稍后重试';

  function api(method, path, body) {
    if (!window.cockpit) return Promise.reject({message:'请从 Cockpit 登录后打开训练管理'});
    return new Promise((resolve, reject) => {
      let output = '', settled = false;
      const proc = cockpit.spawn(['/usr/local/bin/pro6000d-training-client'], {err:'message'});
      const timer = setTimeout(() => { if (!settled) {settled=true; if(typeof proc.close==='function')proc.close('timeout'); reject({message:'服务响应超时。操作结果可能仍在处理中，请刷新查看后再决定是否重试。'});} }, 45000);
      proc.stream(data => { output += data; });
      proc.input(JSON.stringify({method,path,body:body || {}}) + '\n', false);
      const finish = failure => {
        if (settled) return;
        settled = true; clearTimeout(timer);
        try { const r=JSON.parse(output.trim()); if (r.ok) resolve(r.data); else reject(r.error || {message:'服务拒绝了操作'}); }
        catch (_) { reject({message: failure ? String(failure) : '后端响应格式异常'}); }
      };
      proc.then(() => finish(null)); proc.catch(finish);
    });
  }
  function notify(message, bad=false) {
    const n=document.createElement('div'); n.className='toast'+(bad?' toast-error':'');n.textContent=message;
    document.body.appendChild(n); setTimeout(()=>n.remove(),6500);
  }
  function fields(form) {
    const data=Object.fromEntries(new FormData(form));
    integerFields.forEach(k=>{if(k in data)data[k]=Number(data[k]);}); return data;
  }
  function currentUsage() {
    if (state.usage) return state.usage;
    const jobs=state.jobs.filter(j=>j.owner_uid===state.identity.uid && activeStates.includes(j.state));
    const reserved=jobs.filter(j=>!['WAITING_QUOTA','WAITING_MONTHLY_QUOTA'].includes(j.state));
    return {gpu:reserved.reduce((n,j)=>n+j.requested_gpu,0),concurrent_jobs:reserved.length,active_jobs:jobs.length};
  }
  function updateChrome() {
    if (!state) return;
    $('#identity').textContent=state.identity.username+(isAdmin()?' · 管理员':'');
    const unread=(state.notifications||[]).filter(n=>!n.read_at).length;
    $('#notification-count').textContent=unread; $('#notification-count').hidden=!unread;
    $('#connection').textContent=disconnected?'连接中断':state.scheduler_available?'已连接':'调度器未就绪';
    $('#connection').className='tag '+(disconnected?'red':state.scheduler_available?'green':'amber');
    $('#host-state').textContent=state.display_name || state.hostname || '训练服务器';
    $('#last-updated').textContent='最后更新 '+fmt(state.observed_at);
    const pending=(state.applications||[]).filter(a=>a.status==='PENDING').length;
    $('#applications-nav').textContent=(isAdmin()?'申请审批':'我的申请')+(isAdmin()&&pending?' · '+pending:'');
    $('[data-page="rules"]').hidden=!isAdmin();
  }
  function banner() {
    if(disconnected)return `<div class="banner warn"><b>连接已中断，以下为上次读取的数据。</b> ${esc(lastError)} 最后更新：${fmt(state.observed_at)}。${button('重新连接','refresh')}</div>`;
    if(!state.scheduler_available)return `<div class="banner warn">调度服务尚未就绪，暂不能提交训练。当前显示已读取的系统信息。${button('刷新状态','refresh')}</div>`;
    return `<div class="banner">任务按资源和调度优先级排队。并发限制、月度 GPU 小时和单次运行时限分别管理；批准申请后仍需等待可用资源。<span class="last-observed">更新于 ${fmt(state.observed_at)}</span></div>`;
  }
  function render() {
    if(!state)return;
    updateChrome();
    document.querySelectorAll('.navitem').forEach(b=>b.classList.toggle('active',b.dataset.page===page));
    const pages={training:trainingPage,overview:overviewPage,storage:storagePage,accounts:accountsPage,metrics:metricsPage,logs:logsPage,rules:rulesPage,applications:applicationsPage};
    $('#main').innerHTML=(pages[page]||trainingPage)();
  }
  async function refresh(force=false) {
    if(refreshBusy)return; refreshBusy=true;
    try {
      const incoming=await api('GET','/api/v1/state');
      incoming.jobs=incoming.jobs||[];incoming.applications=incoming.applications||[];incoming.notifications=incoming.notifications||[];
      disconnected=false;lastError='';
      if((modalState||ruleDirty)&&!force){queuedState=incoming; updateChrome();}
      else {state=incoming;queuedState=null;render();}
    } catch(e) {
      disconnected=true;lastError=errorText(e);
      if(state){updateChrome();if(!modalState&&!ruleDirty)render();}
      else $('#main').innerHTML=head('训练管理','正在连接服务器')+`<div class="error">${esc(lastError)}</div>${button('重新连接','refresh')}`;
    } finally {refreshBusy=false;}
  }
  function consumeState(){if(queuedState){state=queuedState;queuedState=null;}render();}
  function gpuCards() {
    const gpu=state.gpus||{};
    if(!gpu.available)return `<div class="empty">GPU 数据暂不可用<div class="small">${esc(gpu.reason||'等待采集')}</div></div>`;
    return `<div class="gpu-grid">${(gpu.devices||[]).map(g=>`<div class="gpu"><strong>GPU ${esc(g.index)} · ${esc(g.name)}</strong><div class="owner">${g.owner_job_id?'任务 '+esc(g.owner_job_id):'设备归属以任务队列为准'}</div><div class="meter"><span style="width:${Math.max(0,Math.min(100,Number(g.utilization_percent)||0))}%"></span></div><div class="small">利用率 ${esc(g.utilization_percent)}% · 显存 ${esc(g.memory_used_mb)} / ${esc(g.memory_total_mb)} MiB</div><div class="small mono">${esc(g.id)}</div></div>`).join('')}</div>`;
  }
  const tierNames={debug:'debug · 高优先级',important:'important · 中优先级',normal:'normal · 常规优先级'};
  const gpuHours=v=>v===null||v===undefined||!Number.isFinite(Number(v))?'—':(Number(v)/3600).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
  const monthlyFor=uid=>uid===state.identity.uid?state.monthly_quota:((state.monthly_accounts||[]).find(a=>a.uid===uid)||(state.accounts||[]).find(a=>a.uid===uid)?.monthly_quota);
  const monthlyEnforced=m=>!!(m&&m.enabled&&m.mode==='enforcing'&&m.enforcement_ready);
  function monthlyNotice(m){
    if(!m)return '<div class="notice">月度用量暂不可用，尚未读取到账本状态。</div>';
    if(!monthlyEnforced(m))return `<div class="notice monthly-staged"><strong>月度规则尚未启用强制调度</strong><div>${esc(m.enforcement_limitation||'管理员完成记账与启动限制配置后启用。以下为月度规则及已读取的账本数据，不代表已限制新任务启动。')}</div></div>`;
    if(m.accounting_status!=='ok')return '<div class="notice monthly-staged"><strong>月度记账状态待确认</strong><div>用量可能尚未补齐；是否允许启动以服务端最新调度结果为准。</div></div>';
    if(Number(m.remaining_gpu_seconds)<=0)return `<div class="notice"><strong>本月额度已用完，等待追加 GPU 小时</strong><div>已启动任务继续运行并记录超额；未启动任务等待额度恢复。追加先覆盖 ${gpuHours(m.overage_gpu_seconds)} GPU 小时的超额，余额大于 0 后才能启动新任务。</div></div>`;
    return '';
  }
  function monthlyPanel(m=state.monthly_quota,allowApply=true){
    if(!m)return monthlyNotice(m);
    const reliable=m.accounting_status==='ok',tiers=m.tiers||[];
    return `<section class="card monthly-panel"><header class="cardhead"><div><h2>月度 GPU 小时</h2><div class="small">${esc(m.month||'—')} · 北京时间自然月 · 每账户、每设备独立记账</div></div>${allowApply?button(monthlyEnforced(m)?'申请追加 GPU 小时':'追加申请待启用','monthly-apply',monthlyEnforced(m)?'':'disabled title="月度启动限制尚未启用"'):''}</header><div class="cardbody">${monthlyNotice(m)}<div class="monthly-current">${badge(monthlyEnforced(m)?'强制调度已启用':'待启用',monthlyEnforced(m)?'green':'amber')}<strong>${reliable?(m.current_tier?'当前额度档：'+esc(tierNames[m.current_tier]||m.current_tier):'当前额度档：已耗尽'):'当前额度档：等待账本确认'}</strong><span>本月已用 ${reliable?gpuHours(m.used_gpu_seconds):'—'} GPU 小时${reliable?'':'（待确认）'}</span></div><div class="monthly-tiers">${tiers.map((t,i)=>{const limit=Number(t.limit_gpu_seconds),used=Number(t.used_gpu_seconds),pct=limit>0?Math.min(100,Math.max(0,used/limit*100)):0;return `<article class="monthly-tier ${reliable&&m.current_tier===t.name?'is-current':''}"><div class="monthly-tier-title"><strong>${esc(tierNames[t.name]||t.name)}</strong><span>顺序 ${i+1}</span></div><div class="monthly-balance">${reliable?gpuHours(t.remaining_gpu_seconds):'—'}<small>GPU 小时剩余</small></div><div class="meter"><span style="width:${reliable?pct:0}%"></span></div><div class="monthly-numbers">已用 ${reliable?gpuHours(t.used_gpu_seconds):'—'} / ${gpuHours(t.limit_gpu_seconds)}</div>${t.name==='normal'?`<div class="small">含本月追加 ${gpuHours(m.additional_gpu_seconds)} GPU 小时</div>`:''}</article>`;}).join('')}</div><div class="monthly-foot"><span>本月超额 <strong>${reliable?gpuHours(m.overage_gpu_seconds):'—'} GPU 小时</strong></span><span>4 GPU × 1 小时 = 4 GPU 小时；排队不扣用量，跨档连续记账。</span></div><p class="small">依次使用 debug → important → normal，优先级由系统决定。跨月重新分配额度，余额不结转；已运行部分不因失败或取消退还。</p></div></section>`;
  }
  function monthlyAccounts(){
    if(!isAdmin()||!state.monthly_accounts)return '';
    return `<section class="panel section-space"><div class="panelhead"><h2>用户月度账本</h2><span class="small">GPU 小时 · ${esc(state.monthly_quota?.month||'')}</span></div><div class="table-scroll"><table><thead><tr><th>用户</th><th>当前额度档</th><th>本月已用</th><th>剩余额度</th><th>超额</th><th>已追加</th><th>详情</th></tr></thead><tbody>${state.monthly_accounts.map(m=>`<tr><td>${esc(m.username)}</td><td>${m.accounting_status!=='ok'?'账本待确认':esc(m.current_tier||'已耗尽')}</td><td>${m.accounting_status==='ok'?gpuHours(m.used_gpu_seconds):'—'}</td><td>${m.accounting_status==='ok'?gpuHours(m.remaining_gpu_seconds):'—'}</td><td>${m.accounting_status==='ok'?gpuHours(m.overage_gpu_seconds):'—'}</td><td>${gpuHours(m.additional_gpu_seconds)}</td><td><button class="linkbutton" data-action="monthly-account" data-uid="${m.uid}">查看月账</button></td></tr>`).join('')||'<tr><td colspan="7">暂无月度账本</td></tr>'}</tbody></table></div></section>`;
  }
  function monthlyApplication(){
    const m=state.monthly_quota;if(!m){notify('尚未读取到月度账本，请刷新后再申请。',true);return;}if(!monthlyEnforced(m)){notify('月度启动限制尚未启用，暂不能申请追加 GPU 小时。',true);return;}
    const id=window.crypto&&crypto.randomUUID?crypto.randomUUID():Date.now()+'-'+Math.random().toString(16).slice(2);
    modal('申请追加本月 GPU 小时',`<div class="notice">申请月份：${esc(m.month)}（北京时间）。仅增加本月 normal 额度，不改变 GPU 并发数或系统权限，未使用余额不结转。</div><div class="monthly-addition-summary">已用 ${m.accounting_status==='ok'?gpuHours(m.used_gpu_seconds):'—'} · 余额 ${m.accounting_status==='ok'?gpuHours(m.remaining_gpu_seconds):'—'} · 超额 <strong>${m.accounting_status==='ok'?gpuHours(m.overage_gpu_seconds):'—'}</strong> GPU 小时</div><form id="monthly-application-form">${field('申请追加 GPU 小时','gpu_hours',10,'number','min="1" max="100000" step="1" required')}<div id="monthly-offset" class="help"></div><div class="field"><label for="f-reason">申请理由</label><textarea class="control" id="f-reason" name="reason" minlength="5" maxlength="2000" required></textarea></div></form>`,button('取消','close-modal')+button('提交申请','send-monthly-application','',true),{kind:'monthly-application',requestId:id,month:m.month,overage:m.accounting_status==='ok'?Number(m.overage_gpu_seconds)||0:null});
    monthlyOffset();
  }
  function monthlyOffset(){const el=$('#monthly-offset'),f=$('[name="gpu_hours"]');if(!el||!f||!modalState)return;if(modalState.overage===null){el.textContent='当前账本待确认，暂不能计算覆盖超额后的余额；批准时以服务端账本为准。';return;}const added=Number(f.value)*3600,overage=modalState.overage||0;el.textContent=Number.isFinite(added)?`追加先覆盖超额 ${gpuHours(Math.min(Math.max(0,added),overage))} GPU 小时；覆盖后，本次追加可用于新任务的额度为 ${gpuHours(Math.max(0,added-overage))} GPU 小时。`:'请输入追加小时数。';}
  async function sendMonthlyApplication(){const f=$('#monthly-application-form');if(!f.reportValidity())return;const p=fields(f),ctx=modalState;await mutation(async()=>{await api('POST','/api/v1/applications',{kind:'monthly_gpu_hours',requested:{gpu_hours:p.gpu_hours,client_request_id:ctx.requestId,month:ctx.month},reason:p.reason});closeModal(true);page='applications';await refresh(true);notify('本月 GPU 小时追加申请已提交');});}
  function jobStatus(j){const reason=j.reason_code||'';return /monthly/.test(reason)&&['WAITING_QUOTA','WAITING_MONTHLY_QUOTA','PENDING'].includes(j.state)?badge('等待月度额度','amber'):stateBadge(j.state);}
  function overviewPage(){return head('概览','当前设备和训练资源',button('系统概览','native','data-path="/system"'))+banner()+monthlyPanel()+gpuCards()+`<div class="grid2"><div class="card"><div class="cardbody"><h2>训练任务</h2><p>运行中 ${state.jobs.filter(j=>j.state==='RUNNING').length} · 排队 ${state.jobs.filter(j=>['PENDING','WAITING_QUOTA','WAITING_MONTHLY_QUOTA','SUBMITTING'].includes(j.state)).length}</p>${button('查看任务队列','page','data-target="training"')}</div></div><div class="card"><div class="cardbody"><h2>我的并发限制</h2><p>${esc(quotaText(state.quota))}</p>${button('查看账户与额度','page','data-target="accounts"')}</div></div></div>`;}
  function trainingPage(){
    const u=currentUsage(),q=state.quota;
    return head('训练管理','查看任务、排队原因和运行日志',button('提交训练','submit','',true))+banner()+monthlyPanel()+gpuCards()+
      `<div class="small section-space">以下并发限制统计仅针对当前登录用户 ${esc(state.identity.username)}；上方 GPU 状态为整台设备。</div><div class="overview"><div class="stat"><div class="statlabel">计入限制的 GPU / 上限</div><div class="statvalue">${u.gpu||0} / ${q.gpu_limit}</div><div class="statnote">含已提交调度器的资源预留，不代表实际占用</div></div><div class="stat"><div class="statlabel">并发任务 / 上限</div><div class="statvalue">${u.concurrent_jobs||0} / ${concurrentLimit(q)}</div><div class="statnote">含已交调度器的资源预留</div></div><div class="stat"><div class="statlabel">活动任务 / 上限</div><div class="statvalue">${u.active_jobs||0} / ${q.active_job_limit}</div><div class="statnote">等待、排队和运行任务总数</div></div><div class="stat"><div class="statlabel">并发限制来源</div><div class="statvalue" style="font-size:19px">${q.source==='personal_override'?'个人覆盖':'默认限制'}</div><div class="statnote">${esc(state.identity.username)}</div></div></div>`+
      `<div class="panel"><div class="tabs"><button class="tab ${queueTab==='queue'?'active':''}" data-action="tab" data-tab="queue">任务队列</button><button class="tab ${queueTab==='logs'?'active':''}" data-action="tab" data-tab="logs">训练日志</button></div><div class="panelbody">${queueTab==='queue'?queueTable():logList()}</div></div>`;
  }
  function queueTable(){if(!state.jobs.length)return '<div class="empty">暂无训练任务。点击“提交训练”填写你的命令。</div>';
    return `<div class="table-scroll"><table><thead><tr><th>任务 / 用户</th><th>状态和原因</th><th>资源</th><th>调度档 / 时限 / 用量</th><th>操作</th></tr></thead><tbody>${state.jobs.map(j=>`<tr><td><span class="jobid">${esc(j.scheduler_id||j.id.slice(0,10))}</span><div>${esc(j.name)} ${!isManagedJob(j)?badge('部署前任务'):''}</div><div class="jobmeta">${esc(j.owner_name)} · ${fmt(j.created_at)}</div></td><td>${jobStatus(j)}<div class="reason">${esc(j.reason_text||'—')}</div></td><td>${esc(j.requested_gpu)} GPU · ${esc(j.requested_cpu)} CPU<div class="jobmeta">${esc(j.requested_mem_mb)} MiB</div></td><td>${esc(j.qos)}<div class="jobmeta">${esc(j.requested_minutes)} 分钟</div><div class="jobmeta">本月 ${gpuHours(j.usage_gpu_seconds)} GPU 小时</div></td><td><button class="linkbutton" data-action="job-detail" data-id="${esc(j.id)}">详情</button>${isManagedJob(j)?`<button class="linkbutton" data-action="job-log" data-id="${esc(j.id)}">日志</button>${(activeStates.includes(j.state)&&(!['SUBMITTING','SUBMIT_UNKNOWN'].includes(j.state)||j.scheduler_id))?`<button class="linkbutton" data-action="cancel" data-id="${esc(j.id)}">取消</button>`:''}`:'<div class="small">只读展示</div>'}</td></tr>`).join('')}</tbody></table></div>`;
  }
  function logList(){const jobs=state.jobs.filter(isManagedJob),externalCount=state.jobs.length-jobs.length;return `<div class="notice">选择平台提交的任务查看输出；日志尚未生成时会显示等待提示。${externalCount?` 另有 ${externalCount} 个部署前任务仅在队列中只读展示，其日志请继续使用原有 Slurm 工具查看。`:''}</div>${jobs.map(j=>`<div class="log-row"><span>${esc(j.name)} · ${esc(j.owner_name)} ${stateBadge(j.state)}</span>${button('查看日志','job-log',`data-id="${esc(j.id)}"`)}</div>`).join('')||'<div class="empty">暂无平台任务日志</div>'}`;}
  function storagePage(){return head('存储','查看磁盘、挂载点与可用空间')+banner()+`<div class="card"><div class="cardbody"><p>磁盘和挂载管理使用系统的存储页面。</p>${button('打开存储管理','native','data-path="/storage"')}<div class="notice">训练工作目录和输出路径应放在你的个人目录内。</div></div></div>`;}
  function accountsPage(){
    const d=state.default_quota||state.quota;
    if(!isAdmin())return head('我的账户','月度用量和并发限制分别管理')+banner()+monthlyPanel()+`<div class="card"><div class="cardbody"><h2>${esc(state.identity.username)}</h2><p>${esc(quotaText(state.quota))}</p><p>来源：${state.quota.source==='personal_override'?'个人覆盖':'继承默认'}</p>${button('申请并发限制调整','apply-quota')}</div></div>`;
    return head('账户与额度','月度 GPU 小时独立记账，并发限制可按用户覆盖',button('系统账户管理','native','data-path="/users"'))+banner()+monthlyPanel()+monthlyAccounts()+`<div class="card"><div class="cardhead"><h2>默认并发限制</h2>${button('编辑默认并发限制','edit-quota','data-uid="default"')}</div><div class="cardbody">${esc(quotaText(d))}</div></div><div class="panel section-space"><div class="panelhead"><h2>已开通训练平台的账户</h2></div><div class="table-scroll"><table><thead><tr><th>账户</th><th>当前用量</th><th>并发限制</th><th>来源</th><th>管理</th></tr></thead><tbody>${(state.accounts||[]).map(a=>`<tr><td><strong>${esc(a.username)}</strong><div class="small">${a.is_admin?'管理员':'普通用户'} · UID ${a.uid}</div></td><td>${(a.usage||{}).gpu||0} GPU / ${(a.usage||{}).concurrent_jobs||0} 并发 / ${(a.usage||{}).active_jobs||0} 活动任务</td><td>${esc(quotaText(a.quota))}</td><td>${a.quota.source==='personal_override'?'个人覆盖':'继承默认'}</td><td><button class="linkbutton" data-action="edit-quota" data-uid="${a.uid}">编辑</button>${a.quota.source==='personal_override'?`<button class="linkbutton" data-action="inherit-quota" data-uid="${a.uid}">恢复继承</button>`:''}</td></tr>`).join('')||'<tr><td colspan="5">暂无已开通账户</td></tr>'}</tbody></table></div></div>`;
  }
  const metricNumber=v=>v===null||v===undefined||v===''||!Number.isFinite(Number(v))?null:Number(v);
  function metricValue(v,unit='',digits=0){const n=metricNumber(v);return n===null?'—':n.toFixed(digits)+(unit?' '+unit:'');}
  function memoryValue(v){const n=metricNumber(v);return n===null?'—':(n/1024).toFixed(1)+' GiB';}
  function gpuColor(id){const colors=['#0066cc','#8a4fff','#009596','#ec7a08','#c9190b','#3e8635','#b35c00','#5752d1'];let h=0;for(const c of String(id))h=(h*31+c.charCodeAt(0))>>>0;return colors[h%colors.length];}
  function telemetry(label,value,sub=''){return `<div class="gpu-telemetry"><span>${esc(label)}</span><strong>${esc(value)}</strong>${sub?`<small>${esc(sub)}</small>`:''}</div>`;}
  function usageBar(label,value,description,color='blue'){
    const n=metricNumber(value),clamped=n===null?0:Math.max(0,Math.min(100,n));
    return `<div class="gpu-usage"><div class="gpu-usage-heading"><span>${esc(label)}</span><strong>${metricValue(n,'%')}</strong></div><div class="gpu-usage-track ${color} ${n===null?'unavailable':''}" role="meter" aria-label="${esc(label)}" aria-valuemin="0" aria-valuemax="100" ${n===null?'aria-valuetext="数据暂不可用"':`aria-valuenow="${clamped}"`}><span style="width:${clamped}%"></span></div><div class="gpu-usage-note">${esc(description)}</div></div>`;
  }
  function detailedGpuCard(g,gpu){
    const used=metricNumber(g.memory_used_mb),total=metricNumber(g.memory_total_mb),memoryPercent=used!==null&&total>0?100*used/total:null;
    const pcie=g.pcie_generation!=null&&g.pcie_width!=null?'Gen '+g.pcie_generation+' ×'+g.pcie_width:'—';
    const stateLabel=gpu.stale?'数据过期':gpu.available?'实时采样':'暂不可用';
    return `<section class="card gpu-detail"><header class="gpu-detail-head"><div><div class="gpu-device-title"><span class="gpu-index">GPU ${esc(g.index)}</span><h2>${esc(g.name)}</h2></div><div class="gpu-device-meta">驱动 ${esc(g.driver_version||'—')}<span>性能状态 ${esc(g.pstate||'—')}</span></div></div>${badge(stateLabel,gpu.stale?'amber':gpu.available?'green':'red')}</header><div class="gpu-detail-body"><div class="gpu-dual-bars">${usageBar('GPU 利用率',g.utilization_percent,'计算与图形引擎整体忙碌程度')}${usageBar('显存占用',memoryPercent,memoryValue(used)+' / '+memoryValue(total),'purple')}</div><div class="gpu-telemetry-grid">${telemetry('温度',metricValue(g.temp_c,'°C'))}${telemetry('风扇',metricValue(g.fan_percent,'%'))}${telemetry('功耗',metricValue(g.power_w,'W',1),'上限 '+metricValue(g.power_limit_w,'W'))}${telemetry('显存控制器',metricValue(g.memory_utilization_percent,'%'),'显存读写忙碌度')}${telemetry('核心频率',metricValue(g.graphics_clock_mhz,'MHz'))}${telemetry('显存频率',metricValue(g.memory_clock_mhz,'MHz'))}${telemetry('PCIe 链路',pcie,'当前速率 / 通道宽度')}${telemetry('编码 / 解码',metricValue(g.encoder_utilization_percent,'%')+' / '+metricValue(g.decoder_utilization_percent,'%'))}</div></div></section>`;
  }
  function chartTime(timestamp){const d=new Date(timestamp);return Number.isFinite(d.getTime())?d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}):'—';}
  function historyChart(gpu,devices,kind){
    const isMemory=kind==='memory',title=isMemory?'显存使用趋势':'GPU 利用率趋势';
    const history=(gpu.history||[]).slice(-120).map(s=>({...s,time:Date.parse(s.observed_at)})).filter(s=>Number.isFinite(s.time)).sort((a,b)=>a.time-b.time);
    const end=history.length?history[history.length-1].time:Date.parse(gpu.observed_at||'');
    const start=Number.isFinite(end)?end-600000:0;
    const samples=history.filter(s=>s.time>=start),sampleCount=samples.filter(s=>s.available!==false).length;
    const maxMemory=Math.max(1,...devices.map(d=>(metricNumber(d.memory_total_mb)||0)/1024));
    const max=isMemory?Math.ceil(maxMemory):100;
    const width=560,height=170,left=43,right=14,top=12,bottom=29,plotW=width-left-right,plotH=height-top-bottom;
    const x=t=>left+(t-start)/600000*plotW,y=v=>top+(1-Math.max(0,Math.min(max,v))/max)*plotH;
    const gap=Math.max(15000,(metricNumber(gpu.sampling_interval_seconds)||5)*3000);
    let series='';
    devices.forEach(device=>{
      let path='',previous=null,last=null;
      for(const sample of samples){
        const match=(sample.devices||[]).find(d=>String(d.id)===String(device.id));
        const raw=match?metricNumber(isMemory?match.memory_used_mb:match.utilization_percent):null;
        const value=raw===null?null:isMemory?raw/1024:raw;
        if(sample.available===false||value===null){previous=null;continue;}
        const px=x(sample.time),py=y(value);
        path+=(previous!==null&&sample.time-previous<=gap?' L':' M')+px.toFixed(1)+','+py.toFixed(1);
        previous=sample.time;last={px,py,value,time:sample.time};
      }
      const color=gpuColor(device.id);
      if(path)series+=`<path d="${path}" fill="none" stroke="${color}" stroke-width="2.2" stroke-linejoin="round" stroke-linecap="round"><title>GPU ${esc(device.index)} ${esc(title)}</title></path>`;
      if(last)series+=`<circle cx="${last.px.toFixed(1)}" cy="${last.py.toFixed(1)}" r="3" fill="${color}"><title>GPU ${esc(device.index)} · ${chartTime(last.time)} · ${last.value.toFixed(isMemory?1:0)}${isMemory?' GiB':'%'}</title></circle>`;
    });
    const ticks=[0,max/2,max];
    const grid=ticks.map(v=>`<line x1="${left}" x2="${width-right}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" stroke="#e2e5e8" stroke-dasharray="3 4"/><text x="${left-8}" y="${(y(v)+4).toFixed(1)}" text-anchor="end">${isMemory?v.toFixed(v%1?1:0):v}${isMemory?'':'%'}</text>`).join('');
    const labels=Number.isFinite(end)?`<text x="${left}" y="${height-7}">${chartTime(start)}</text><text x="${left+plotW/2}" y="${height-7}" text-anchor="middle">${chartTime(start+300000)}</text><text x="${width-right}" y="${height-7}" text-anchor="end">${chartTime(end)}</text>`:'';
    const chart=series?`<svg class="gpu-chart" viewBox="0 0 ${width} ${height}" role="img" aria-label="${esc(title)}，最近十分钟；空白表示没有采样数据">${grid}${series}${labels}</svg>`:`<div class="gpu-chart-empty">${gpu.available?'正在积累采样数据':'暂无有效采样'}<small>取得真实数据后将在这里绘制趋势</small></div>`;
    return `<section class="card gpu-chart-card"><div class="cardhead"><h2>${title}</h2><span class="small">${isMemory?'GiB · ':''}最近 10 分钟</span></div><div class="gpu-chart-body">${chart}<div class="gpu-chart-legend">${devices.map(d=>`<span><i style="background:${gpuColor(d.id)}"></i>GPU ${esc(d.index)}</span>`).join('')}<small>${sampleCount<2?'尚不足两个有效采样点':sampleCount+' 个有效采样点'}</small></div></div></section>`;
  }
  function processSortButton(label,key){return `<button type="button" class="gpu-sort" data-action="monitor-sort" data-sort="${key}" aria-label="按${esc(label)}排序">${esc(label)}${monitorSort===key?(monitorSortDirection==='desc'?' ↓':' ↑'):''}</button>`;}
  function gpuProcessTable(gpu,devices){
    const selected=new Set(devices.map(d=>String(d.id)));
    const ownOnly=!isAdmin()||monitorProcessScope==='mine';
    let processes=(gpu.processes||[]).filter(p=>monitorGPU==='all'||selected.has(String(p.gpu_uuid)));
    if(ownOnly)processes=processes.filter(p=>p.uid===state.identity.uid||(p.uid==null&&p.username===state.identity.username));
    processes=processes.slice().sort((a,b)=>{
      const av=metricNumber(a[monitorSort]),bv=metricNumber(b[monitorSort]);
      if(av===null&&bv===null)return Number(a.pid)-Number(b.pid);
      if(av===null)return 1;if(bv===null)return -1;
      return (av-bv)*(monitorSortDirection==='desc'?-1:1)||Number(a.pid)-Number(b.pid);
    });
    const hidden=Number(gpu.hidden_process_count)||0;
    const accessText=isAdmin()?'显示可读取的 GPU 进程；进程与训练任务分别统计。':`仅显示属于你的 GPU 进程。${hidden?'系统中另有 '+hidden+' 个其他账户或身份无法确认的进程，详情已隐藏。':'其他账户的进程详情不会显示。'}`;
    const row=processes.map(p=>`<tr><td><span class="gpu-process-index">GPU ${esc(p.gpu_index)}</span></td><td class="mono">${esc(p.pid)}</td><td>${esc(p.username||'未知')}</td><td><strong class="gpu-process-name" title="${esc(p.name||'未知进程')}">${esc(p.name||'未知进程')}</strong><span class="small">${esc(({C:'计算',G:'图形','C+G':'计算与图形',compute:'计算',graphics:'图形'})[p.type]||p.type||'类型未知')}</span></td><td class="gpu-numeric">${metricValue(p.memory_used_mb,'MiB')}</td><td class="gpu-numeric">${metricValue(p.utilization_percent,'%')}</td><td>${p.association==='matched'&&p.scheduler_id?`<span class="jobid">${esc(p.scheduler_id)}</span><div class="small">${esc(p.job_name||'已关联训练任务')}</div>`:'<span class="gpu-unassociated">未关联调度任务</span>'}</td></tr>`).join('');
    const empty=`<tr><td colspan="7"><div class="gpu-process-empty"><strong>${ownOnly?'当前没有你的 GPU 进程':'当前筛选范围内没有 GPU 进程'}</strong><span>${gpu.available?'训练启动后，进程会在这里显示。设备整体利用率仍可在上方查看。':'GPU 采集暂不可用，恢复后会自动刷新。'}</span></div></td></tr>`;
    return `<section class="panel gpu-process-panel"><div class="cardhead"><div><h2>GPU 进程 <span class="gpu-process-count">${processes.length}</span></h2><div class="small">${accessText}</div></div>${isAdmin()?`<label class="gpu-filter-label">进程范围<select class="control gpu-filter" data-monitor-filter="scope" aria-label="GPU 进程范围"><option value="all" ${monitorProcessScope==='all'?'selected':''}>全部账户</option><option value="mine" ${monitorProcessScope==='mine'?'selected':''}>仅我的进程</option></select></label>`:badge('仅本人','blue')}</div><div class="table-scroll"><table class="gpu-process-table"><thead><tr><th>设备</th><th aria-sort="${monitorSort==='pid'?(monitorSortDirection==='desc'?'descending':'ascending'):'none'}">${processSortButton('PID','pid')}</th><th>账户</th><th>进程 / 类型</th><th aria-sort="${monitorSort==='memory_used_mb'?(monitorSortDirection==='desc'?'descending':'ascending'):'none'}">${processSortButton('显存','memory_used_mb')}</th><th>进程 GPU</th><th>关联训练任务</th></tr></thead><tbody>${row||empty}</tbody></table></div><div class="gpu-process-footer">进程 GPU 利用率未提供时显示“—”，不会使用整卡利用率代替。${gpu.processes_truncated?' 当前进程列表已达到采集上限。':''}</div></section>`;
  }
  function metricsPage(){
    const m=state.metrics||{},gpu=state.gpus||m.gpu||{},all=gpu.devices||[];
    const selected=all.filter(d=>monitorGPU==='all'||String(d.id)===monitorGPU);
    const unavailable=!gpu.available,stale=disconnected||gpu.stale;
    let warning='';
    if(disconnected)warning=`<div class="banner warn"><strong>连接中断，以下为上次采集的数据。</strong> ${esc(lastError)} ${button('重新连接','refresh')}</div>`;
    else if(stale||unavailable)warning=`<div class="banner warn"><strong>${stale?'GPU 数据已过期':'GPU 采集暂不可用'}</strong> ${esc(gpu.reason||'等待下一次采集。')} ${gpu.observed_at?'最后成功采样：'+fmt(gpu.observed_at):''}</div>`;
    else if(!state.scheduler_available)warning='<div class="banner warn">调度服务尚未就绪；GPU 实时监控仍可独立查看。</div>';
    const absent=monitorGPU!=='all'&&!all.some(d=>String(d.id)===monitorGPU);
    const header=head('性能监控','GPU 实时监控 · 利用率、显存与进程',button('系统性能','native','data-path="/metrics"'));
    const toolbar=`<div class="gpu-monitor-toolbar"><label class="gpu-filter-label">设备<select class="control gpu-filter" data-monitor-filter="gpu" aria-label="筛选 GPU 设备"><option value="all" ${monitorGPU==='all'?'selected':''}>全部 GPU${all.length?'（'+all.length+'）':''}</option>${all.map(d=>`<option value="${esc(d.id)}" ${String(d.id)===monitorGPU?'selected':''}>GPU ${esc(d.index)} · ${esc(d.name)}</option>`).join('')}${absent?`<option value="${esc(monitorGPU)}" selected>所选设备暂不可用</option>`:''}</select></label><div class="gpu-sampling"><span class="gpu-live-dot ${stale||unavailable?'paused':''}"></span>${stale?'数据过期':unavailable?'等待采集':'每 '+(gpu.sampling_interval_seconds||5)+' 秒采样'}<span>更新于 ${fmt(gpu.observed_at)}</span></div>${button('立即刷新','refresh')}</div>`;
    const cards=selected.map(d=>detailedGpuCard(d,gpu)).join('')||`<div class="card gpu-monitor-empty"><strong>${absent?'所选 GPU 暂不可用':'尚未读取到 GPU 设备'}</strong><p>${esc(gpu.reason||'设备恢复后将自动刷新，请稍后查看。')}</p></div>`;
    const charts=`<div class="gpu-history-grid">${historyChart(gpu,selected,'utilization')}${historyChart(gpu,selected,'memory')}</div><p class="gpu-history-note">历史从本次服务采样后开始积累，最多保留约 10 分钟；空白和断线表示该时段没有有效数据。${gpu.history_started_at?'本次采样开始于 '+fmt(gpu.history_started_at)+'。':''}</p>`;
    const total=metricNumber(m.memory_total_mb),available=metricNumber(m.memory_available_mb),used=total!==null&&available!==null?total-available:null;
    const host=`<section class="gpu-host-summary" aria-label="主机用量"><strong>主机用量</strong><span>CPU 1 分钟负载 <b>${metricValue(m.cpu_load_1m,'',2)}</b></span><span>内存已用 <b>${memoryValue(used)}</b> / ${memoryValue(total)}</span><span>内存可用 <b>${memoryValue(available)}</b></span></section>`;
    return `<div class="gpu-monitor">${header}${warning}${toolbar}<div class="gpu-device-list">${cards}</div>${charts}${gpuProcessTable(gpu,selected)}${host}</div>`;
  }
  function logsPage(){return head('系统日志','系统日志与训练管理操作记录',button('系统日志','native','data-path="/system/logs"'))+banner()+`<div class="card"><div class="cardhead"><h2>训练管理操作记录</h2>${isAdmin()?button('读取操作记录','audit'):''}</div><div class="cardbody">${isAdmin()?(auditRows?`<div class="table-scroll"><table><thead><tr><th>时间</th><th>账户</th><th>动作</th><th>对象</th><th>结果</th></tr></thead><tbody>${auditRows.map(a=>`<tr><td>${fmt(a.created_at)}</td><td>${esc(a.actor_name)}</td><td>${esc(a.action)}</td><td>${esc(a.object_type)} ${esc(a.object_id)}</td><td>${esc(a.result)}</td></tr>`).join('')}</tbody></table></div>`:'点击读取，查看提交、审批和规则修改记录。'):'你的训练输出可在“训练管理 → 训练日志”中查看。'}</div></div>`;}
  function rulesPage(){if(!isAdmin())return head('调度规则','需要管理员权限');return head('调度规则','月度额度决定调度优先级；单次时限与并发限制独立管理')+banner()+monthlyPanel(state.monthly_quota,false)+`<div class="card"><div class="cardbody"><h2>单次运行时限</h2><form id="rules-form"><div class="formgrid">${Object.keys(state.rules||{}).map(k=>field((k==='long'?'long（历史兼容）':k)+' 最长运行时间（分钟）','rule_'+k,state.rules[k].max_minutes,'number','min="1" required')).join('')}</div><div class="notice">保存前会列出受影响任务。正在运行的任务不会被自动终止。</div>${button('预览修改影响','preview-rules','',true)}</form></div></div>`;}
  function applicationsPage(){return head(isAdmin()?'申请审批':'我的申请',isAdmin()?'处理超额训练与并发限制申请':'查看审批进度和处理结果',button('申请追加 GPU 小时','monthly-apply',monthlyEnforced(state.monthly_quota)?'':'disabled title="月度启动限制尚未启用"')+button('申请并发限制','apply-quota'))+banner()+applicationTable(state.applications);}
  function applicationTable(rows){return `<div class="panel table-scroll"><table><thead><tr><th>申请人 / 类型</th><th>申请内容</th><th>状态</th><th>提交时间</th><th>操作</th></tr></thead><tbody>${rows.map(a=>`<tr><td>${esc(a.owner_name)}<div class="small">${applicationKind(a)}</div></td><td>${esc(applicationSummary(a))}<div class="reason">${esc(a.reason)}</div></td><td>${a.status==='PENDING'?badge('待审批','blue'):stateBadge(a.status)}${a.expires_at?`<div class="small">有效至 ${fmt(a.expires_at)}</div>`:''}${a.authorization_used?'<div class="small">授权已使用</div>':''}</td><td>${fmt(a.created_at)}</td><td><button class="linkbutton" data-action="application-detail" data-id="${esc(a.id)}">${isAdmin()&&a.status==='PENDING'?'处理':'详情'}</button>${canSubmitGrant(a)?`<button class="linkbutton" data-action="submit-grant" data-id="${esc(a.id)}">提交已批准任务</button>`:''}</td></tr>`).join('')||'<tr><td colspan="5" class="empty">暂无申请。超额训练可在提交表单中转为申请。</td></tr>'}</tbody></table></div>`;}
  function applicationKind(a){return a.kind==='monthly_gpu_hours'?'本月 GPU 小时追加':a.kind==='temporary_job'?'一次性训练资源':'长期并发限制';}
  function applicationSummary(a){const r=requestData(a);return a.kind==='monthly_gpu_hours'?`${r.month||a.month||''} · 追加 ${r.gpu_hours} GPU 小时`:a.kind==='temporary_job'?`${r.name||'训练'} · ${r.gpu} GPU · ${r.time_minutes} 分钟` : quotaText(r);}
  function canSubmitGrant(a){return a.owner_uid===state.identity.uid&&a.kind==='temporary_job'&&a.status==='APPROVED'&&!a.authorization_used&&!a.bound_job_id&&(!a.expires_at||new Date(a.expires_at)>new Date());}

  function field(label,name,value='',type='text',attrs=''){return `<div class="field"><label for="f-${name}">${esc(label)}</label><input class="control" id="f-${name}" name="${name}" type="${type}" value="${esc(value)}" ${attrs}></div>`;}
  function selectField(label,name,values,selected){return `<div class="field"><label for="f-${name}">${esc(label)}</label><select class="control" id="f-${name}" name="${name}">${values.map(v=>`<option value="${esc(v.value)}" ${v.value===selected?'selected':''}>${esc(v.label)}</option>`).join('')}</select></div>`;}
  function modal(title, body, footer='', config={}) {
    modalState={dirty:false,busy:false,...config};
    $('#overlay').innerHTML=`<div class="modal-backdrop" data-action="close-modal"></div><section class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title"><div class="modalhead"><h2 id="modal-title">${esc(title)}</h2><button class="close" data-action="close-modal" aria-label="关闭">×</button></div><div class="modalbody">${body}<div id="modal-error" role="alert"></div></div><div class="modalfoot">${footer||button('关闭','close-modal')}</div></section>`;
    setTimeout(()=>{const f=$('input,select,textarea,button',$('.modal'));if(f)f.focus();},0);
  }
  function closeModal(force=false){
    if(modalState&&modalState.busy&&!force)return;
    if(modalState&&modalState.dirty&&!force&&!window.confirm('尚有未保存的输入，确定关闭吗？'))return;
    if(modalState&&modalState.kind==='rules'&&!force){const draft=modalState.ruleDraft;modalState=null;$('#overlay').innerHTML='';Object.entries(draft).forEach(([k,v])=>{const f=$('[name="rule_'+k+'"]');if(f)f.value=v.max_minutes;});return;}
    modalState=null;$('#overlay').innerHTML='';if(state)consumeState();
  }
  async function mutation(action){
    if(modalState&&modalState.busy)return;
    const ref=modalState;if(ref)ref.busy=true;
    document.querySelectorAll('.modalfoot button').forEach(b=>b.disabled=true);
    try {await action();} catch(e){if($('#modal-error'))$('#modal-error').innerHTML=`<div class="error">${esc(errorText(e))}</div>`;else notify(errorText(e),true);}
    finally {if(ref)ref.busy=false;document.querySelectorAll('.modalfoot button').forEach(b=>b.disabled=false);}
  }
  function defaultJob(){const home=state.identity.home||'/home/'+state.identity.username;return {name:'training',gpu:1,cpu:1,memory_mb:8192,time_minutes:60,workdir:home,environment:'system',output_path:home+'/slurm-%j.out',qos:'normal',command:'python train.py'};}
  function jobFields(j){return `<div class="formgrid">${field('任务名称','name',j.name,'text','required pattern="[A-Za-z0-9][A-Za-z0-9_.-]{0,63}"')}${field('GPU 数量','gpu',j.gpu,'number','min="1" required')}${field('工作目录','workdir',j.workdir,'text','required')}${field('Python / Conda 环境','environment',j.environment,'text','required')}${field('日志输出路径','output_path',j.output_path,'text','required')}${monthlyEnforced(state.monthly_quota)?'<input type="hidden" name="qos" value="normal"><div class="field"><label>调度优先级</label><div class="control-static">系统按月度余额自动分档</div><div class="help">debug → important → normal；不支持手动选择优先级。</div></div>':selectField('单次时限档位（月度调度待启用）','qos',Object.entries(state.rules).map(([k,v])=>({value:k,label:k+' · 最长 '+v.max_minutes+' 分钟'})),j.qos)}${field('CPU 数量','cpu',j.cpu,'number','min="1" required')}${field('内存（MiB）','memory_mb',j.memory_mb,'number','min="128" required')}${field('实际运行时限（分钟）','time_minutes',j.time_minutes,'number','min="1" required')}</div><div class="field"><label for="f-command">训练命令</label><textarea id="f-command" class="control" name="command" maxlength="4096" required>${esc(j.command)}</textarea><div class="help">命令在你的账户下运行。system 使用系统环境，也可填写已配置的 Conda 环境。日志文件名可用 %j 代表任务编号。</div></div>`;}
  function jobModal(draft=defaultJob(),apply=false){
    const id=window.crypto&&crypto.randomUUID?crypto.randomUUID():Date.now()+'-'+Math.random().toString(16).slice(2);
    modal(apply?'申请一次性训练资源':'提交训练',`<div class="notice">我的并发限制：${esc(quotaText(state.quota))}。${apply?'管理员批准后，你可从“我的申请”提交该任务。':'并发限制内直接排队；超过并发限制或单次时限可申请。月度余额不足时请另行申请追加 GPU 小时。'}</div><form id="job-form">${jobFields(draft)}${apply?'<div class="field"><label for="f-reason">申请理由</label><textarea class="control" id="f-reason" name="reason" minlength="5" maxlength="2000" required></textarea></div>':''}</form>`,button('取消','close-modal')+(apply?'':button('改为资源申请','convert-application'))+button(apply?'提交申请':'提交到队列',apply?'send-job-application':'send-job','',true),{kind:apply?'job-application':'job',requestId:id});
  }
  async function sendJob(apply){const form=$('#job-form');if(!form.reportValidity())return;const p=fields(form);await mutation(async()=>{
    if(apply){const reason=p.reason;delete p.reason;await api('POST','/api/v1/applications',{kind:'temporary_job',requested:p,reason});closeModal(true);page='applications';await refresh(true);notify('申请已提交，等待管理员处理');}
    else {p.client_request_id=modalState.requestId;try {const r=await api('POST','/api/v1/jobs',p);closeModal(true);await refresh(true);notify(['SUBMITTING','SUBMIT_UNKNOWN'].includes(r.job.state)?'提交结果待确认，请在队列查看，不要重复提交。':r.job.state==='WAITING_QUOTA'?'任务已排队，请查看队列中的等待原因':'任务已提交，开始时间由资源情况决定');}catch(e){if(['monthly_quota_exhausted','monthly_quota_unavailable'].includes(e.code))$('#modal-error').innerHTML=`<div class="error">${esc(errorText(e))}。本次训练内容保留在表单中；可稍后到“我的申请”追加本月 GPU 小时。</div>`;else if(['permanent_quota_exceeded','time_limit_exceeded','quota_exceeded'].includes(e.code))$('#modal-error').innerHTML=`<div class="error">${esc(errorText(e))}。可点击“改为资源申请”，保留当前训练内容。</div>`;else throw e;}}
  });}
  function quotaFields(q){return `<div class="formgrid">${field('同时使用的 GPU 总数','gpu_limit',q.gpu_limit,'number','min="1" max="64" required')}${field('并发任务数','concurrent_job_limit',concurrentLimit(q),'number','min="1" max="64" required')}${field('活动任务总数（含等待）','active_job_limit',q.active_job_limit,'number','min="1" max="64" required')}</div>`;}
  function quotaApplication(){modal('申请长期并发限制',`<form id="quota-application-form">${quotaFields(state.quota)}<div class="field"><label for="f-reason">申请理由</label><textarea class="control" id="f-reason" name="reason" minlength="5" maxlength="2000" required></textarea></div></form>`,button('取消','close-modal')+button('提交申请','send-quota-application','',true),{kind:'quota-application'});}
  async function sendQuotaApplication(){const f=$('#quota-application-form');if(!f.reportValidity())return;const p=fields(f),reason=p.reason;delete p.reason;await mutation(async()=>{await api('POST','/api/v1/applications',{kind:'quota_change',requested:p,reason});closeModal(true);page='applications';await refresh(true);notify('并发限制申请已提交');});}
  function jobDetails(j){return `${!isManagedJob(j)?'<div class="notice">部署前任务：本平台仅展示其状态，不接管任务操作。查看日志或管理此任务，请继续使用原有 Slurm 工具。</div>':''}<dl class="details"><dt>任务</dt><dd>${esc(j.name)}</dd><dt>资源</dt><dd>${esc(j.gpu||j.requested_gpu)} GPU · ${esc(j.cpu||j.requested_cpu)} CPU · ${esc(j.memory_mb||j.requested_mem_mb)} MiB · ${esc(j.time_minutes||j.requested_minutes)} 分钟</dd><dt>本月累计用量</dt><dd>${gpuHours(j.usage_gpu_seconds)} GPU 小时</dd><dt>工作目录</dt><dd class="mono">${esc(j.workdir)}</dd><dt>环境</dt><dd>${esc(j.environment||j.env_name)}</dd><dt>输出路径</dt><dd class="mono">${esc(j.output_path)}</dd><dt>训练命令</dt><dd><pre class="command-preview">${esc(j.command)}</pre></dd></dl>`;}
  function applicationDetail(id){const a=state.applications.find(x=>x.id===id);if(!a)return;const req=requestData(a),approved=approvedData(a),account=(state.accounts||[]).find(x=>x.uid===a.owner_uid);
    const editable=isAdmin()&&a.status==='PENDING',monthlyApprovalBlocked=a.kind==='monthly_gpu_hours'&&!monthlyEnforced(monthlyFor(a.owner_uid));
    let body=`<div class="toolbar"><strong>${esc(a.owner_name)}</strong>${a.status==='PENDING'?badge('待审批','blue'):stateBadge(a.status)}<span>${applicationKind(a)}</span></div><p>${esc(a.reason)}</p>`;
    const monthly=monthlyFor(a.owner_uid);
    if(a.kind==='monthly_gpu_hours')body+=monthlyPanel(monthly||null,false);
    if(account)body+=`<div class="notice">当前并发限制：${esc(quotaText(account.quota))}。当前占用 ${(account.usage||{}).gpu||0} GPU，活动任务 ${(account.usage||{}).active_jobs||0}。</div>`;
    body+=a.kind==='monthly_gpu_hours'?`<p>申请月份：${esc(req.month||a.month||'以服务端绑定月份为准')} · 追加 <strong>${esc(req.gpu_hours)} GPU 小时</strong></p><div class="notice">批准后仅增加申请月份的 normal 额度，先抵扣超额；余额大于 0 才可恢复新任务启动。跨月申请由服务端检查。</div>`:a.kind==='temporary_job'?jobDetails(req):`<p>申请并发限制：${esc(quotaText(req))}</p>`;
    if(a.decision_reason)body+=`<div class="notice">处理意见：${esc(a.decision_reason)}</div>`;
    if(a.status==='APPROVED')body+=`<div class="success">${a.kind==='monthly_gpu_hours'?'已追加 '+esc(approved.gpu_hours||req.gpu_hours)+' GPU 小时至申请月份的 normal 额度。':a.kind==='temporary_job'?'批准资源：'+esc(approved.gpu)+' GPU / '+esc(approved.time_minutes)+' 分钟。授权绑定此任务，有效至 '+fmt(a.expires_at)+'。':'已批准并发限制：'+esc(quotaText(approved))}</div>`;
    if(editable)body+=`<form id="decision-form"><div class="notice">${a.kind==='monthly_gpu_hours'?'追加仅增加申请月份的 normal 额度，超额会优先扣除。不会改变并发限制或授予系统权限。':a.kind==='temporary_job'?'一次性批准只能用于这条训练申请，内容变更需要重新申请。':'调整并发限制不增加月度 GPU 小时。'}</div>${monthlyApprovalBlocked?'<div class="notice">月度启动限制尚未启用，暂不能批准追加。可保留申请等待启用，或填写理由拒绝。</div>':''}${selectField('处理方式','action',monthlyApprovalBlocked?[{value:'reject',label:'拒绝'}]:[{value:'approve',label:'按申请批准'},{value:'approve_adjusted',label:'调整后批准'},{value:'reject',label:'拒绝'}],monthlyApprovalBlocked?'reject':'approve')}<div id="adjusted-fields" hidden>${a.kind==='monthly_gpu_hours'?field('批准追加 GPU 小时','gpu_hours',req.gpu_hours,'number','min="1" max="100000" step="1"'):a.kind==='temporary_job'?`<div class="formgrid">${field('批准 GPU 数量','gpu',req.gpu,'number','min="1"')}${field('批准 CPU 数量','cpu',req.cpu,'number','min="1"')}${field('批准内存（MiB）','memory_mb',req.memory_mb,'number','min="128"')}${field('批准时限（分钟）','time_minutes',req.time_minutes,'number','min="1"')}${monthlyEnforced(state.monthly_quota)?'<div class="field"><label>调度优先级</label><div class="control-static">按用户月度余额自动分档</div></div>':selectField('单次时限档位','qos',Object.keys(state.rules).map(k=>({value:k,label:k})),req.qos)}</div>`:quotaFields(req)}</div>${a.kind==='temporary_job'?field('批准后有效期（分钟）','valid_minutes',1440,'number','min="1" max="10080" required'):''}<div class="field"><label for="f-decision_reason">处理意见（拒绝或调整批准必填）</label><textarea class="control" id="f-decision_reason" name="decision_reason" maxlength="2000"></textarea></div></form>`;
    modal(editable?'处理申请':'申请详情',body,button('关闭','close-modal')+(editable?button('保存决定','save-decision',`data-id="${esc(id)}"`,true):canSubmitGrant(a)?button('提交已批准任务','submit-grant',`data-id="${esc(id)}"`,true):''),{kind:'decision',application:a});
  }
  async function saveDecision(id){const f=$('#decision-form');if(!f.reportValidity())return;const d=fields(f);if(['reject','approve_adjusted'].includes(d.action)&&!d.decision_reason.trim()){$('#modal-error').innerHTML='<div class="error">拒绝或调整批准时，请填写处理理由。</div>';return;}
    const p={action:d.action,decision_reason:d.decision_reason,valid_minutes:d.valid_minutes};
    if(d.action==='approve_adjusted')p.approved=modalState.application.kind==='monthly_gpu_hours'?{gpu_hours:d.gpu_hours}:modalState.application.kind==='temporary_job'?{gpu:d.gpu,cpu:d.cpu,memory_mb:d.memory_mb,time_minutes:d.time_minutes,...(!monthlyEnforced(state.monthly_quota)?{qos:d.qos}:{})}:{gpu_limit:d.gpu_limit,concurrent_job_limit:d.concurrent_job_limit,active_job_limit:d.active_job_limit};
    await mutation(async()=>{await api('POST','/api/v1/applications/'+encodeURIComponent(id)+'/decision',p);closeModal(true);await refresh(true);notify(d.action==='reject'?'申请已拒绝，申请人可看到处理意见':'申请已批准，结果已通知申请人');});
  }
  async function submitGrant(id){const a=state.applications.find(x=>x.id===id);if(!a||!canSubmitGrant(a))return;await mutation(async()=>{await api('POST','/api/v1/jobs',{temporary_application_id:id});closeModal(true);page='training';await refresh(true);notify('已批准任务已提交到队列');});}
  function impactsHtml(p){return `<div class="diff">${(p.impacts||[]).map(x=>`<div><strong>${esc(x.scheduler_id||x.job_id)}</strong> · ${esc(x.user)}：${esc((x.reasons||[]).join('；'))}</div>`).join('')||'当前未发现受影响的任务。'}</div><p class="small">预览不会终止正在运行的任务。保存前服务端会再次检查此预览是否仍有效。</p>`;}
  function editQuota(uid,inherit=false){const target=uid==='default'?null:(state.accounts||[]).find(a=>a.uid===Number(uid));if(uid!=='default'&&!target)return;const quota=target?target.quota:(state.default_quota||state.quota);
    modal(inherit?'恢复默认并发限制':target?'编辑 '+target.username+' 的个人并发限制':'编辑默认并发限制',`<form id="quota-form">${inherit?`<p>将移除 ${esc(target.username)} 的个人覆盖，改为继承默认并发限制：</p><p>${esc(quotaText(state.default_quota))}</p>`:quotaFields(quota)}</form><div id="quota-preview"></div>`,button('取消','close-modal')+button('预览影响','preview-quota','',true),{kind:'quota',uid:target?target.uid:null,scope:target?'user':'default',inherit});
  }
  async function previewQuota(){const f=$('#quota-form');if(!f.reportValidity())return;const ctx=modalState,p={scope:ctx.scope};if(ctx.uid!=null)p.uid=ctx.uid;if(ctx.inherit)p.inherit=true;else p.quota=fields(f);
    await mutation(async()=>{const r=await api('POST','/api/v1/quotas/preview',p);ctx.preview=r;$('#quota-preview').innerHTML=`<h3>变更预览</h3><p>修改前：${esc(quotaText(r.before))}</p><p>修改后：${esc(quotaText(r.after))}</p>${impactsHtml(r)}`;$('#quota-form').hidden=true;$('.modalfoot').innerHTML=button('返回编辑','quota-back')+button('确认保存','apply-quota','',true);});
  }
  async function applyQuota(){const ctx=modalState;if(!ctx||!ctx.preview)return;await mutation(async()=>{await api('POST','/api/v1/quotas/apply',{preview_id:ctx.preview.preview_id});closeModal(true);await refresh(true);notify('并发限制已保存');});}
  async function previewRules(){const f=$('#rules-form');if(!f.reportValidity())return;const d=fields(f),rules={};Object.keys(state.rules).forEach(k=>rules[k]={max_minutes:Number(d['rule_'+k])});
    try {const p=await api('POST','/api/v1/rules/preview',{rules});modal('调度规则变更预览',`<div class="table-scroll"><table><thead><tr><th>档位</th><th>原时限</th><th>新时限</th></tr></thead><tbody>${Object.keys(rules).map(k=>`<tr><td>${esc(k)}</td><td>${state.rules[k].max_minutes} 分钟</td><td>${rules[k].max_minutes} 分钟</td></tr>`).join('')}</tbody></table></div>${impactsHtml(p)}`,button('返回编辑','rules-back')+button('确认保存','apply-rules','',true),{kind:'rules',preview:p,ruleDraft:rules});}
    catch(e){notify(errorText(e),true);}
  }
  async function applyRules(){const ctx=modalState;await mutation(async()=>{await api('POST','/api/v1/rules/apply',{preview_id:ctx.preview.preview_id});ruleDirty=false;closeModal(true);await refresh(true);notify('调度规则已保存');});}
  function notifications(){modal('站内通知',`<div id="notice-list">${(state.notifications||[]).map(n=>`<article class="notice-item ${n.read_at?'':'unread'}"><div class="toolbar"><strong>${esc(n.title)}</strong>${n.read_at?badge('已读'):badge('未读','blue')}</div><p>${esc(n.body)}</p><div class="small">${fmt(n.created_at)}</div>${!n.read_at?`<button class="linkbutton" data-action="read-notice" data-id="${esc(n.id)}">标记已读</button>`:''}</article>`).join('')||'<div class="empty">暂无通知</div>'}</div>`,button('关闭','close-modal')+button('查看申请','open-applications'),{kind:'notifications'});}
  async function readNotice(id){try{await api('POST','/api/v1/notifications/'+encodeURIComponent(id)+'/read');const n=state.notifications.find(x=>x.id===id);if(n)n.read_at=new Date().toISOString();updateChrome();notifications();}catch(e){notify(errorText(e),true);}}
  function navigate(target){if(ruleDirty&&!window.confirm('调度规则尚未保存，确定离开吗？'))return;ruleDirty=false;page=target;consumeState();}
  function native(path){if(window.cockpit&&cockpit.jump)cockpit.jump(path,'localhost');else window.location.assign(path);}
  async function action(name,b){
    if(['job-log','cancel','confirm-cancel'].includes(name)){const j=state&&state.jobs.find(x=>x.id===b.dataset.id);if(j&&!isManagedJob(j)){notify('部署前任务仅供查看，请使用原有 Slurm 工具管理。');return;}}
    if(name==='monitor-sort'){if(monitorSort===b.dataset.sort)monitorSortDirection=monitorSortDirection==='desc'?'asc':'desc';else{monitorSort=b.dataset.sort;monitorSortDirection=monitorSort==='pid'?'asc':'desc';}return render();}
    if(name==='close-modal')return closeModal();
    if(name==='refresh')return refresh();
    if(name==='page')return navigate(b.dataset.target);
    if(name==='native')return native(b.dataset.path);
    if(name==='tab'){queueTab=b.dataset.tab;return render();}
    if(name==='monthly-apply')return monthlyApplication();
    if(name==='send-monthly-application')return sendMonthlyApplication();
    if(name==='monthly-account'){const m=monthlyFor(Number(b.dataset.uid));if(m)modal((m.username||state.identity.username||'用户')+' · 月度账本',monthlyPanel(m,false));return;}
    if(name==='submit')return jobModal();
    if(name==='convert-application'){const f=$('#job-form'),draft=fields(f);return jobModal(draft,true);}
    if(name==='send-job')return sendJob(false);
    if(name==='send-job-application')return sendJob(true);
    if(name==='apply-quota')return applyQuota();
    if(name==='apply-rules')return applyRules();
    if(name==='apply-quota-form')return quotaApplication();
    if(name==='send-quota-application')return sendQuotaApplication();
    if(name==='edit-quota')return editQuota(b.dataset.uid);
    if(name==='inherit-quota')return editQuota(b.dataset.uid,true);
    if(name==='preview-quota')return previewQuota();
    if(name==='quota-back'){$('#quota-form').hidden=false;$('#quota-preview').innerHTML='';modalState.preview=null;$('.modalfoot').innerHTML=button('取消','close-modal')+button('预览影响','preview-quota','',true);return;}
    if(name==='preview-rules')return previewRules();
    if(name==='rules-back'){const draft=modalState.ruleDraft;modalState=null;$('#overlay').innerHTML='';Object.entries(draft).forEach(([k,v])=>{const f=$('[name="rule_'+k+'"]');if(f)f.value=v.max_minutes;});return;}
    if(name==='application-detail')return applicationDetail(b.dataset.id);
    if(name==='save-decision')return saveDecision(b.dataset.id);
    if(name==='submit-grant')return submitGrant(b.dataset.id);
    if(name==='read-notice')return readNotice(b.dataset.id);
    if(name==='open-applications'){closeModal(true);return navigate('applications');}
    if(name==='job-detail'){const j=state.jobs.find(x=>x.id===b.dataset.id);if(j)modal('任务详情',stateBadge(j.state)+jobDetails(j));return;}
    if(name==='job-log'){try{const r=await api('GET','/api/v1/jobs/'+encodeURIComponent(b.dataset.id)+'/log');modal('训练日志',`<div class="small mono">${esc(r.path)}</div><pre class="audit">${esc(r.available?r.content:r.reason||'日志尚未生成')}</pre>`);}catch(e){notify(errorText(e),true);}return;}
    if(name==='cancel'){const j=state.jobs.find(x=>x.id===b.dataset.id);if(j)modal('取消任务',`<p>确定取消 ${esc(j.name)}（${esc(j.scheduler_id||j.id.slice(0,10))}）吗？正在运行的训练会结束。</p>`,button('返回','close-modal')+button('确认取消','confirm-cancel',`data-id="${esc(j.id)}"`,true));return;}
    if(name==='confirm-cancel')return mutation(async()=>{await api('POST','/api/v1/jobs/'+encodeURIComponent(b.dataset.id)+'/cancel');closeModal(true);await refresh(true);notify('取消请求已处理');});
    if(name==='audit'){try{const r=await api('GET','/api/v1/audit');auditRows=r.audit||[];render();}catch(e){notify(errorText(e),true);}return;}
  }
  document.addEventListener('click',e=>{
    const nav=e.target.closest('[data-page]');if(nav){e.preventDefault();navigate(nav.dataset.page);return;}
    const b=e.target.closest('[data-action]');if(!b||b.disabled)return;e.preventDefault();
    // Long-term application is distinct from saving a quota preview.
    const name=b.dataset.action==='apply-quota'&&!modalState?'apply-quota-form':b.dataset.action;
    Promise.resolve(action(name,b)).catch(err=>notify(errorText(err),true));
  });
  document.addEventListener('input',e=>{if(e.target.name==='gpu_hours')monthlyOffset();if(e.target.closest('.modal')&&modalState)modalState.dirty=true;if(e.target.closest('#rules-form'))ruleDirty=true;});
  document.addEventListener('change',e=>{if(e.target.closest('.modal')&&modalState)modalState.dirty=true;if(e.target.name==='action'&&$('#adjusted-fields'))$('#adjusted-fields').hidden=e.target.value!=='approve_adjusted';if(e.target.dataset.monitorFilter==='gpu'){monitorGPU=e.target.value;render();}if(e.target.dataset.monitorFilter==='scope'){monitorProcessScope=e.target.value;render();}});
  document.addEventListener('submit',e=>{
    e.preventDefault();
    const form=e.target;
    if(form.id==='job-form')sendJob(modalState&&modalState.kind==='job-application');
    else if(form.id==='monthly-application-form')sendMonthlyApplication();
    else if(form.id==='quota-application-form')sendQuotaApplication();
    else if(form.id==='quota-form')previewQuota();
    else if(form.id==='rules-form')previewRules();
    else if(form.id==='decision-form'&&modalState&&modalState.application)saveDecision(modalState.application.id);
  });
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&modalState)closeModal();});
  window.addEventListener('beforeunload',e=>{if(ruleDirty||(modalState&&modalState.dirty)){e.preventDefault();e.returnValue='';}});
  $('#notifications').onclick=()=>{if(state)notifications();};
  $('#logout').onclick=()=>{if((ruleDirty||(modalState&&modalState.dirty))&&!window.confirm('有未保存的输入，确定退出吗？'))return;ruleDirty=false;modalState=null;if(window.cockpit)cockpit.logout();};
  $('#login').hidden=true;$('#app').hidden=false;
  if(!window.cockpit){$('#main').innerHTML=head('请从 Cockpit 打开训练管理','此页面使用系统登录，不提供独立密码或演示身份')+`<div class="notice">在 Cockpit 中登录后，选择“训练管理”。</div><a class="pf-c-button pf-m-primary" href="/">系统管理入口</a>`;$('#connection').textContent='未连接';$('#logout').hidden=true;return;}
  refresh();setInterval(()=>{if(!document.hidden)refresh();},5000);
})();
