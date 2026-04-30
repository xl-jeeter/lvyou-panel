const API = '/api';
let smsPage = 1, logPage = 1;
let _apiKey = localStorage.getItem('api_key') || '';

// ── API Key helpers ───────────────────────────────────────────
function apiHeaders(extra) {
  const h = {'Content-Type':'application/json', ...extra};
  if (_apiKey) h['X-API-Key'] = _apiKey;
  return h;
}
async function apiFetch(url, opts) {
  opts = opts || {};
  opts.headers = apiHeaders(opts.headers);
  const r = await fetch(url, opts);
  if (r.status === 401) {
    localStorage.removeItem('api_key');
    _apiKey = '';
    showLoginPage();
    throw new Error('Unauthorized');
  }
  return r;
}

// ── Login page ────────────────────────────────────────────────
function showLoginPage() {
  document.querySelector('main').innerHTML = `
    <div style="max-width:400px;margin:80px auto;text-align:center">
      <h2 style="margin-bottom:24px">🔐 需要登录</h2>
      <form onsubmit="doLogin(event)">
        <div class="form-group" style="margin-bottom:16px">
          <input id="loginKey" type="password" placeholder="输入 API 密钥" required autofocus style="text-align:center;font-size:16px;padding:12px">
        </div>
        <button class="btn btn-p" type="submit" style="padding:10px 32px;font-size:15px">登录</button>
      </form>
    </div>`;
}
async function doLogin(e) {
  e.preventDefault();
  const key = document.getElementById('loginKey').value.trim();
  if (!key) return;
  try {
    const r = await apiFetch(API+'/auth', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({api_key:key})
    });
    if (r.ok) {
      _apiKey = key;
      localStorage.setItem('api_key', key);
      toast('登录成功');
      initPage();
    } else {
      toast('密钥错误', false);
    }
  } catch(err) { toast('连接失败', false); }
}

// ── Nav highlight ─────────────────────────────────────────────
(function(){
  const links = document.querySelectorAll('nav a');
  const path = location.pathname;
  links.forEach(a => {
    if (a.pathname === path) a.classList.add('active');
  });
})();

// ── Toast ─────────────────────────────────────────────────────
function toast(msg, ok=true) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = 'toast show ' + (ok?'ok':'err');
  setTimeout(() => t.classList.remove('show'), 2500);
}

// ── Formatting ────────────────────────────────────────────────
function fmtTime(ts) { if(!ts) return '-'; return ts.replace('T',' ').substring(0,19); }
function fmtUptime(m) { if(!m&&m!==0) return '-'; m=parseInt(m); const d=Math.floor(m/1440),h=Math.floor((m%1440)/60),s=m%60; let r=''; if(d>0) r+=d+'天'; if(h>0||d>0) r+=h+'时'; r+=s+'分'; return r; }
function signalBars(dbm) {
  let v = parseInt(dbm)||0, bars='';
  for(let i=1;i<=5;i++) bars += `<span class="bar${i*20>v?' gray':''}" style="height:${i*4}px"></span>`;
  return bars;
}

// ══════════ Dashboard ══════════════════════════════════════════
async function refreshAll() {
  const grid = document.getElementById('deviceGrid');
  if (window._devCache) {
    renderDevices(window._devCache);
    document.getElementById('lastRefresh').textContent = (new Date()).toLocaleTimeString() + ' (缓存)';
  } else {
    grid.innerHTML = '<div style="text-align:center;padding:40px;color:var(--sub)"><span class="spin"></span> 加载中...</div>';
  }
  try {
    const ctrl = new AbortController();
    const timeout = setTimeout(() => ctrl.abort(), 6000);
    const r = await apiFetch(API+'/devices/status/all', {signal: ctrl.signal});
    clearTimeout(timeout);
    const data = await r.json();
    window._devCache = data;
    renderDevices(data);
    document.getElementById('lastRefresh').textContent = new Date().toLocaleTimeString();
  } catch(e) {
    if (window._devCache) {
      document.getElementById('lastRefresh').textContent = new Date().toLocaleTimeString() + ' (刷新失败，显示缓存)';
    } else {
      grid.innerHTML = '<div style="text-align:center;padding:40px;color:var(--danger)">加载失败，<a href="javascript:refreshAll()" style="color:var(--c)">点击重试</a></div>';
    }
  }
}

function renderDevices(data) {
  let online = 0, offline = 0;
  let html = '';
  data.forEach(d => {
    const s = d.status || {};
    const ok = s.code === 0;
    ok ? online++ : offline++;
    const wifi = s.wifi || {};
    const si = s.slotInfo || {};
    const simNet = s.simNet || [false, false];

    html += `<div class="device-card">
      <div class="head">
        <div>
          <div class="name"><span class="name-edit" onclick="editName(event,'${esc(d.device.id)}',&quot;${esc(d.device.name)}&quot;)" title="点击改名">${esc(d.device.name)}</span> <span style="font-weight:400;color:var(--sub);font-size:12px">${esc(d.device.ip)}</span></div>
          <div class="ip">${ok ? wifi.ssid||'-' : '离线'} · ${ok ? s.hwVer||'' : ''} · 运行${fmtUptime(s.uptime)}</div>
        </div>
        <span class="status-dot ${ok?'online':'offline'}"></span>
      </div>
      <div class="body">`;

    for(let sl=1; sl<=2; sl++) {
      const key = `sim${sl}`;
      const sta = si[`slot${sl}_sta`] || 'NOSIM';
      const dbmv = si[`sim${sl}_dbm`] || '0';
      const phone = si[`sim${sl}_msIsdn`] || '';
      const scName = si[`sim${sl}_scName`] || '';
      const netOn = simNet[sl-1];
      const label = sta === 'NOSIM' ? '无卡' : sta === 'OK' ? (scName || '在线') : sta;

      html += `<div class="sim-row">
        <span class="sim-name">卡${sl}</span>
        <span class="sim-status" style="color:${sta==='NOSIM'?'var(--sub)':netOn?'var(--c)':'var(--warn)'}">${label}</span>
        <span class="sim-phone">${phone||'-'}</span>
        <span class="sim-signal">${signalBars(dbmv)} ${dbmv}%</span>
      </div>`;
    }

    html += `</div>
      <div class="foot">
        <button class="btn btn-p btn-sm" onclick="sendSMSModal('${esc(d.device.id)}')">📩 发短信</button>
        <button class="btn btn-sm" onclick="toggleNet('${esc(d.device.id)}','sim1')">卡1联网</button>
        <button class="btn btn-sm" onclick="toggleNet('${esc(d.device.id)}','sim2')">卡2联网</button>
        <button class="btn btn-sm" onclick="cmdAction('${esc(d.device.id)}','restart','确认重启设备？')" title="重启设备">🔄</button>
        <button class="btn btn-sm" onclick="refreshSMS('${esc(d.device.id)}')">📥 同步短信</button>
      </div>
      <div style="padding:8px 16px;border-top:1px solid var(--border);font-size:12px;color:var(--sub);display:flex;gap:12px;align-items:center">
        📦 短信存储：<span id="sms-store-${esc(d.device.id)}" style="color:var(--warn)">查询中...</span>
      </div>
    </div>`;
  });

  document.getElementById('deviceGrid').innerHTML = html;
  document.getElementById('statsCards').innerHTML =
    statCard(online, '在线设备', 'var(--c)') +
    statCard(offline, '离线设备', 'var(--danger)');
  data.forEach(d => { if (d.status&&d.status.code===0) checkSmsStore(d.device); });
}

function statCard(num, label, color) {
  return `<div class="stat-card"><div class="num" style="color:${color}">${num}</div><div class="label">${label}</div></div>`;
}

function esc(s) { return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }

// ── Inline name edit ──────────────────────────────────────────
function editName(e, devId, oldName) {
  e.stopPropagation();
  const el = e.target;
  const input = document.createElement('input');
  input.value = oldName;
  input.onblur = async () => {
    const newName = input.value.trim();
    if (newName && newName !== oldName) {
      try {
        await apiFetch(API+'/devices/'+devId, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:newName})});
        toast('改名成功');
      } catch(e) { toast('改名失败',false); }
    }
    refreshAll();
  };
  input.onkeydown = ev => { if (ev.key==='Enter') input.blur(); if (ev.key==='Escape') { input.value=oldName; input.blur(); } };
  el.innerHTML = '';
  el.appendChild(input);
  input.focus();
  input.select();
}

async function checkSmsStore(dev) {
  const el = document.getElementById('sms-store-'+dev.id);
  if (!el) return;
  try {
    const r = await apiFetch(API+'/devices/'+dev.id+'/cmd/storesmsen', {method:'POST'});
    const d = await r.json();
    if (d.code===0 && d.val) {
      const parts = d.val.split(';');
      let h = '';
      parts.forEach(p => { const [s,v]=p.split(':'); h += `卡${s}:<span style="color:${v==='on'?'var(--c)':'var(--danger)'}">${v==='on'?'✅':'❌'}</span> `; });
      h += `<button class="btn btn-sm" style="margin-left:4px;padding:2px 6px;font-size:10px" onclick="toggleSmsStore('${esc(dev.id)}')">${d.val.includes('off')?'开启':'关闭'}</button>`;
      el.innerHTML = h;
    }
  } catch(e) {}
}

async function toggleSmsStore(devId) {
  if (!confirm('切换短信存储后需要重启设备才能生效。继续？')) return;
  try {
    const r1 = await apiFetch(API+'/devices/'+devId+'/cmd/storesmsen', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({p1:'0',p2:'on'})});
    const d1 = await r1.json();
    if (d1.code!==0) { toast('设置失败: '+(d1.note||''), false); return; }
    toast('已开启，正在重启设备...');
    await apiFetch(API+'/devices/'+devId+'/cmd/restart', {method:'POST'});
    setTimeout(refreshAll, 10000);
  } catch(e) { toast('操作失败', false); }
}

// ══════════ Commands ═══════════════════════════════════════════
async function cmdAction(devId, cmd, confirmMsg, params) {
  if (confirmMsg && !confirm(confirmMsg)) return;
  try {
    const opts = params ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(params)} : {method:'POST'};
    const r = await apiFetch(API+'/devices/'+devId+'/cmd/'+cmd, opts);
    const d = await r.json();
    toast(d.code===0 ? '执行成功' : '失败: '+(d.note||''), d.code===0);
    if (d.code===0) setTimeout(refreshAll, 1000);
  } catch(e) { toast('请求失败', false); }
}

async function toggleNet(devId, slot) {
  try {
    const r = await apiFetch(API+'/devices/'+devId+'/status');
    const d = await r.json();
    const s = d.status || {};
    const simNet = s.simNet || [false, false];
    const idx = slot==='sim1'?0:1;
    const newVal = simNet[idx] ? 0 : 1;
    const label = newVal ? '开启' : '关闭';
    if (!confirm(`确定${label} ${slot} 联网？`)) return;
    await cmdAction(devId, 'slotnet', null, {sid: slot, v: String(newVal)});
  } catch(e) { toast('获取状态失败', false); }
}

async function refreshSMS(devId) {
  try {
    const r = await apiFetch(API+'/sms/refresh/'+devId, {method:'POST'});
    const d = await r.json();
    toast('同步完成', d.ok);
  } catch(e) { toast('同步失败', false); }
}

// ══════════ Send SMS Modal ═════════════════════════════════════
function sendSMSModal(devId) {
  const html = `
    <div class="modal-box">
      <h3>📩 发送短信</h3>
      <form onsubmit="sendSMS(event,'${esc(devId)}')">
        <div class="form-group" style="margin-bottom:12px">
          <label>卡槽</label>
          <select id="smsSlot"><option value="sim1">SIM卡1</option><option value="sim2">SIM卡2</option></select>
        </div>
        <div class="form-group" style="margin-bottom:12px">
          <label>目标号码 *</label><input id="smsTarget" placeholder="手机号" required>
        </div>
        <div class="form-group" style="margin-bottom:12px">
          <label>短信内容 *</label><input id="smsContent" placeholder="短信内容" required>
        </div>
        <div class="actions">
          <button type="button" class="btn" onclick="closeModal()">取消</button>
          <button type="submit" class="btn btn-p">发送</button>
        </div>
      </form>
    </div>`;
  showModal(html);
}

function showModal(html) {
  let o = document.getElementById('modal');
  if (!o) {
    o = document.createElement('div'); o.id = 'modal'; o.className = 'modal-overlay';
    o.onclick = e => { if (e.target===o) closeModal(); };
    document.body.appendChild(o);
  }
  o.innerHTML = html; o.classList.add('show');
}
function closeModal() {
  const o = document.getElementById('modal');
  if (o) o.classList.remove('show');
}

async function sendSMS(e, devId) {
  e.preventDefault();
  const slot = document.getElementById('smsSlot').value;
  const phone = document.getElementById('smsTarget').value.trim();
  const content = document.getElementById('smsContent').value.trim();
  if (!phone || !content) return;
  await cmdAction(devId, 'sendsms', null, {p1: slot.replace('sim',''), p2: phone, p3: content});
  closeModal();
}

// ══════════ SMS Page ═══════════════════════════════════════════
async function loadSMS(pg) {
  if (pg) smsPage = pg;
  const dev = document.getElementById('smsDevFilter').value;
  const phone = document.getElementById('smsPhoneFilter').value.trim();
  const kw = document.getElementById('smsKwFilter').value.trim();
  const dir = document.getElementById('smsDirFilter').value;
  const params = new URLSearchParams({page: smsPage, per_page: 30});
  if (dev) params.set('device_id', dev);
  if (phone) params.set('phone', phone);
  if (kw) params.set('keyword', kw);
  if (dir) params.set('direction', dir);

  try {
    const r = await apiFetch(API+'/sms?'+params);
    const d = await r.json();
    let html = '';
    d.items.forEach(m => {
      html += `<tr>
        <td>${fmtTime(m.sms_time)}</td>
        <td>${esc(m.device_name||m.device_id)}</td>
        <td>${esc(m.sim_slot)}</td>
        <td><span class="badge ${m.direction==='received'?'badge-rec':'badge-sent'}">${m.direction==='received'?'接收':'发送'}</span></td>
        <td>${esc(m.phone)}</td>
        <td class="content">${esc(m.content)}</td>
      </tr>`;
    });
    document.getElementById('smsTableBody').innerHTML = html || '<tr><td colspan="6" style="text-align:center;color:var(--sub)">暂无记录</td></tr>';
    renderPagination('sms', d.total, d.page, d.per_page);
  } catch(e) { toast('加载失败', false); }
}

async function loadLogs(pg) {
  if (pg) logPage = pg;
  const dev = document.getElementById('logDevFilter').value;
  const phone = document.getElementById('logPhoneFilter').value.trim();
  const params = new URLSearchParams({page: logPage, per_page: 30});
  if (dev) params.set('device_id', dev);
  if (phone) params.set('phone', phone);

  try {
    const r = await apiFetch(API+'/logs?'+params);
    const d = await r.json();
    let html = '';
    d.items.forEach(l => {
      html += `<tr>
        <td>${fmtTime(l.call_time)}</td>
        <td>${esc(l.device_name||l.device_id)}</td>
        <td><span class="badge ${l.direction==='incoming'?'badge-incoming':'badge-outgoing'}">${l.direction==='incoming'?'呼入':'呼出'}</span></td>
        <td>${esc(l.phone)}</td>
        <td>${esc(l.action)}</td>
        <td>${l.duration||0}秒</td>
      </tr>`;
    });
    document.getElementById('logTableBody').innerHTML = html || '<tr><td colspan="6" style="text-align:center;color:var(--sub)">暂无记录</td></tr>';
    renderPagination('log', d.total, d.page, d.per_page);
  } catch(e) { toast('加载失败', false); }
}

function renderPagination(type, total, page, pp) {
  const max = Math.ceil(total / pp);
  const el = document.getElementById(type+'Pagination');
  if (max <= 1) { el.innerHTML = ''; return; }
  let h = `<button ${page>1?'':'disabled'} onclick="load${type==='sms'?'SMS':'Logs'}(${page-1})">‹</button>`;
  h += `<span>${page}/${max} (共${total}条)</span>`;
  h += `<button ${page<max?'':'disabled'} onclick="load${type==='sms'?'SMS':'Logs'}(${page+1})">›</button>`;
  el.innerHTML = h;
}

// ══════════ Device Management ══════════════════════════════════
async function loadDevices() {
  try {
    const r = await apiFetch(API+'/devices');
    const data = await r.json();
    let html = '';
    if (data.length === 0) html = '<tr><td colspan="6" style="text-align:center;color:var(--sub)">暂无设备，请添加</td></tr>';
    else data.forEach(d => {
      html += `<tr>
        <td style="font-size:12px;font-family:monospace">${esc(d.id)}</td>
        <td>${esc(d.name)}</td>
        <td>${esc(d.ip)}</td>
        <td>${esc(d.notes)}</td>
        <td style="font-size:12px">${fmtTime(d.created_at)}</td>
        <td><button class="btn btn-r btn-sm" onclick="delDevice('${esc(d.id)}')">删除</button></td>
      </tr>`;
    });
    document.getElementById('devTableBody').innerHTML = html;

    ['smsDevFilter','logDevFilter'].forEach(fid => {
      const sel = document.getElementById(fid);
      if (!sel) return;
      const v = sel.value;
      sel.innerHTML = '<option value="">全部设备</option>';
      data.forEach(d => { sel.innerHTML += `<option value="${esc(d.id)}">${esc(d.name)}</option>`; });
      sel.value = v;
    });
  } catch(e) { toast('加载设备列表失败', false); }
}

async function addDevice(e) {
  e.preventDefault();
  const name = document.getElementById('addName').value.trim();
  const ip = document.getElementById('addIP').value.trim();
  const pwd = document.getElementById('addPwd').value.trim();
  try {
    const r = await apiFetch(API+'/devices', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({name, ip, password: pwd})
    });
    if (!r.ok) { const d = await r.json(); toast(d.detail||'添加失败', false); return; }
    toast('添加成功');
    document.getElementById('addName').value = '';
    document.getElementById('addIP').value = '';
    document.getElementById('addPwd').value = '';
    loadDevices();
  } catch(e) { toast('请求失败', false); }
}

async function quickAddDevice(e) {
  e.preventDefault();
  const name = document.getElementById('qaName').value.trim();
  const ip = document.getElementById('qaIP').value.trim();
  const pwd = document.getElementById('qaPwd').value.trim();
  try {
    const r = await apiFetch(API+'/devices', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({name, ip, password: pwd})
    });
    if (!r.ok) { const d = await r.json(); toast(d.detail||'添加失败', false); return; }
    toast('添加成功');
    document.getElementById('qaName').value = '';
    document.getElementById('qaIP').value = '';
    document.getElementById('qaPwd').value = '';
    refreshAll();
    loadDevices();
  } catch(e) { toast('请求失败', false); }
}

async function delDevice(id) {
  if (!confirm('确定删除该设备？短信和通话记录也将被删除。')) return;
  try {
    await apiFetch(API+'/devices/'+id, {method:'DELETE'});
    toast('已删除');
    loadDevices();
  } catch(e) { toast('删除失败', false); }
}

// ══════════ Init ═══════════════════════════════════════════════
function getCurrentPage() {
  const path = location.pathname;
  if (path === '/sms') return 'sms';
  if (path === '/logs') return 'logs';
  if (path === '/devices') return 'devices';
  return 'dashboard';
}

// ── DingTalk Settings ──────────────────────────────────────────
async function loadDingtalkSettings() {
  try {
    const r = await apiFetch(API+'/settings');
    const d = await r.json();
    if (d.dingtalk_webhook) {
      document.getElementById('dingtalkWebhook').value = d.dingtalk_webhook;
    }
    if (d.dingtalk_secret) {
      document.getElementById('dingtalkSecret').value = d.dingtalk_secret;
    }
    const st = document.getElementById('dingtalkStatus');
    if (d.dingtalk_webhook) {
      st.innerHTML = '<span style="color:var(--c)"> · 钉钉已配置</span>';
    }
    // Load API key status
    const apiSt = document.getElementById('apiKeyStatus');
    if (apiSt) {
      if (d.api_key) {
        apiSt.innerHTML = '<span style="color:var(--c)">已启用</span>';
      } else {
        apiSt.innerHTML = '<span style="color:var(--sub)">未启用</span>';
      }
    }
  } catch(e) {}
}

async function saveDingtalk() {
  const url = document.getElementById('dingtalkWebhook').value.trim();
  const secret = document.getElementById('dingtalkSecret').value.trim();
  if (!url) { toast('请输入 Webhook 地址', false); return; }
  try {
    await apiFetch(API+'/settings', {method:'POST',headers:apiHeaders(),body:JSON.stringify({dingtalk_webhook:url, dingtalk_secret:secret})});
    document.getElementById('dingtalkStatus').innerHTML = '<span style="color:var(--c)"> · 钉钉已配置</span>';
    toast('已保存');
  } catch(e) { toast('保存失败', false); }
}

async function testDingtalk() {
  const url = document.getElementById('dingtalkWebhook').value.trim();
  if (!url) { toast('请先输入并保存 Webhook 地址', false); return; }
  try {
    const r = await apiFetch(API+'/test-dingtalk', {method:'POST'});
    const d = await r.json();
    if (d.ok) toast('✅ 测试成功！请查看钉钉群');
    else toast('发送失败: ' + (d.body||d.status), false);
  } catch(e) { toast('请求失败', false); }
}

// ── API Key Settings ──────────────────────────────────────────
async function saveApiKey() {
  const key = document.getElementById('apiKeyInput').value.trim();
  try {
    await apiFetch(API+'/settings', {method:'POST',headers:apiHeaders(),body:JSON.stringify({api_key:key})});
    if (key) {
      _apiKey = key;
      localStorage.setItem('api_key', key);
      document.getElementById('apiKeyStatus').innerHTML = '<span style="color:var(--c)">已启用</span>';
      toast('API 密钥已保存');
    } else {
      _apiKey = '';
      localStorage.removeItem('api_key');
      document.getElementById('apiKeyStatus').innerHTML = '<span style="color:var(--sub)">已关闭</span>';
      toast('API 认证已关闭');
    }
  } catch(e) { toast('保存失败', false); }
}

async function initPage() {
  // Check if API key is needed
  try {
    const r = await fetch(API+'/auth', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({api_key:_apiKey})
    });
    if (r.status === 401) { showLoginPage(); return; }
    const d = await r.json();
    if (d.needsSetup) {
      // No API key configured yet, proceed without auth
    }
  } catch(e) { /* server might be down */ }

  const p = getCurrentPage();
  if (p === 'dashboard') { loadDingtalkSettings(); refreshAll(); }
  else if (p === 'sms') { loadDevices(); loadSMS(); }
  else if (p === 'logs') { loadDevices(); loadLogs(); }
  else if (p === 'devices') loadDevices();
}

document.addEventListener('DOMContentLoaded', initPage);
