'use strict';

/* 设置 · 手机访问：局域网网关开关 / 二维码配对 / 待确认设备 / 设备表 / 审计
   网关与配对都在本机后端完成；这里只调用 /api/mobile/*（同一套 X-Auth-Token）。 */

let mobilePollTimer = 0;
let mobileLastStatus = {};

function mobilePaneVisible() {
  const pane = document.querySelector('#modal .pane[data-pane="mobile"]');
  return !!pane && !pane.classList.contains('hidden');
}

function mobileLine(text, cls) {
  const div = document.createElement('div');
  if (cls) div.className = cls;
  div.textContent = text;
  return div;
}

function mobileBtn(label, danger, onClick) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'ghost' + (danger ? ' danger' : '');
  b.textContent = label;
  b.addEventListener('click', onClick);
  return b;
}

function mobileRenderStatus(st) {
  const box = $('#mobile-status');
  if (!box) return;
  mobileLastStatus = st || {};
  box.textContent = '';
  const gw = st.gateway || {};
  const running = !!gw.running;
  const ip = st.lan_ip || '（未检测到局域网地址）';
  const port = running ? gw.port : st.port;
  const lines = [];
  lines.push(running
    ? '运行中：https://' + ip + ':' + port
    : '未开启（主服务仍只监听 127.0.0.1）');
  if (running) {
    lines.push('手机打开：https://' + ip + ':' + port + '/ca.crt 安装根证书；然后扫码配对。');
    lines.push('当前连接数：' + (gw.connections || 0) + ' · 根证书指纹：' + (st.ca_fingerprint || ''));
  }
  if (st.candidates && st.candidates.length > 1) {
    lines.push('检测到的局域网地址：' + st.candidates.join(' / ') + '（优先用第一个）');
  }
  if (st.firewall && st.firewall.supported) {
    lines.push(st.firewall.present ? 'Windows 防火墙规则：已存在' : 'Windows 防火墙规则：未添加（连不上时点「添加防火墙规则」）');
  }
  if (gw.error) lines.push('错误：' + gw.error);
  lines.forEach((t) => box.appendChild(mobileLine(t)));

  const toggle = $('#mobile-toggle');
  if (toggle) toggle.textContent = running ? '关闭手机访问' : '开启手机访问';
  const pairBtn = $('#mobile-pair-start');
  if (pairBtn) pairBtn.disabled = !running;

  mobileRenderDevices(st.devices || []);
}

function mobileRenderDevices(items) {
  const box = $('#mobile-devices');
  if (!box) return;
  box.textContent = '';
  if (!items.length) {
    box.appendChild(mobileLine('暂无设备'));
    return;
  }
  items.forEach((d) => {
    const row = document.createElement('div');
    row.style.cssText = 'display:flex;align-items:center;gap:8px;padding:4px 0';
    const revoked = !!d.revoked;
    const expired = Number(d.expires || 0) * 1000 < Date.now();
    const state = revoked ? '已吊销' : (expired ? '已过期' : '有效');
    const seen = d.last_seen ? new Date(Number(d.last_seen) * 1000).toLocaleString() : '—';
    const label = document.createElement('span');
    label.style.flex = '1';
    label.textContent = (d.name || '手机') + ' · ' + (d.platform || '未知平台')
      + ' · ' + state + ' · 最后在线 ' + seen;
    row.appendChild(label);
    if (!revoked) {
      row.appendChild(mobileBtn('吊销', true, () => mobileRevoke(d.id)));
    }
    box.appendChild(row);
  });
}

function mobileRenderPending(items) {
  const box = $('#mobile-pending');
  if (!box) return;
  box.textContent = '';
  (items || []).forEach((p) => {
    const card = document.createElement('div');
    card.className = 'check-row';
    card.style.cssText = 'display:block;padding:8px;border:1px solid var(--border)';
    card.appendChild(mobileLine('待确认配对：' + (p.name || '手机')
      + ' · 校验码 ' + (p.sas || '------')));
    const acts = document.createElement('div');
    acts.className = 'preset-row';
    acts.appendChild(mobileBtn('允许', false, () => mobileConfirm(p.pair_id, true)));
    acts.appendChild(mobileBtn('拒绝', true, () => mobileConfirm(p.pair_id, false)));
    card.appendChild(acts);
    box.appendChild(card);
  });
}

async function loadMobileStatus() {
  try {
    const st = await api('/api/mobile/status', {});
    mobileRenderStatus(st);
    mobileRenderPending(st.pending || []);
  } catch (e) {
    const box = $('#mobile-status');
    if (box) box.textContent = '读取状态失败：' + e.message;
  }
}

async function mobileToggle() {
  const running = !!(mobileLastStatus.gateway || {}).running;
  try {
    const st = running
      ? await api('/api/mobile/disable', {})
      : await api('/api/mobile/enable', {});
    if (st && st.ok === false) {
      toast(st.error || '操作失败');
      return;
    }
    // 同步到内存里的配置：否则随后在设置页点「保存」会把旧值写回去
    const enabled = !!st.enabled;
    if (typeof state !== 'undefined' && state.config) state.config.mobile_enabled = enabled;
    if (typeof draftCfg !== 'undefined' && draftCfg) draftCfg.mobile_enabled = enabled;
    mobileRenderStatus(st);
    toast(running ? '已关闭手机访问' : '已开启手机访问');
  } catch (e) {
    toast(e.message);
  }
}

async function mobilePairStart() {
  const box = $('#mobile-pair-box');
  try {
    const r = await api('/api/mobile/pair/start', {});
    if (!r.ok) throw new Error(r.error || '生成失败');
    const qr = $('#mobile-qr');
    if (qr) {
      qr.innerHTML = r.qr_svg;      // 服务端自己生成的 SVG
      const svg = qr.querySelector('svg');
      if (svg) {
        svg.removeAttribute('width');
        svg.removeAttribute('height');
        svg.style.width = '100%';
        svg.style.height = 'auto';
      }
    }
    if (box) box.classList.remove('hidden');
    const tip = $('#mobile-pair-tip');
    if (tip) {
      const mins = Math.max(1, Math.round((r.expires - Date.now() / 1000) / 60));
      tip.textContent = '扫不到时可用手机浏览器打开：' + r.url + '/ 并手动输入配对信息；'
        + '二维码 ' + mins + ' 分钟内有效，只可用一次。根证书指纹：' + (r.ca_fingerprint || '');
    }
  } catch (e) {
    toast(e.message);
  }
}

async function mobileConfirm(pairId, ok) {
  try {
    await api('/api/mobile/pair/confirm', { pair_id: pairId, ok: !!ok });
    await loadMobileStatus();
  } catch (e) {
    toast(e.message);
  }
}

async function mobileRevoke(deviceId) {
  if (!confirm('吊销后这台手机需要重新配对，继续？')) return;
  try {
    const r = await api('/api/mobile/revoke', { device_id: deviceId });
    mobileRenderDevices(r.devices || []);
  } catch (e) {
    toast(e.message);
  }
}

async function mobileRevokeAll() {
  if (!confirm('吊销全部手机设备？已连接的手机将立即断开。')) return;
  try {
    const r = await api('/api/mobile/revoke-all', {});
    mobileRenderDevices(r.devices || []);
  } catch (e) {
    toast(e.message);
  }
}

async function mobileResetIdentity() {
  if (!confirm('重置桌面身份会同时吊销全部设备，并关闭手机访问。继续？')) return;
  try {
    const st = await api('/api/mobile/reset', {});
    mobileRenderStatus(st);
    toast('已重置桌面身份');
  } catch (e) {
    toast(e.message);
  }
}

async function mobileAudit() {
  const out = $('#mobile-audit-out');
  try {
    const r = await api('/api/mobile/audit', { limit: 100 });
    if (out) {
      out.textContent = (r.items || []).map((it) =>
        it.time + '  ' + it.action + '  ' + JSON.stringify(
          Object.assign({}, it, { ts: undefined, time: undefined, action: undefined }))
      ).join('\n') || '（空）';
    }
  } catch (e) {
    if (out) out.textContent = e.message;
  }
}

async function mobileFirewall() {
  try {
    const r = await api('/api/mobile/firewall', {});
    toast(r.ok ? '防火墙规则已添加' : ((r.result && r.result.message) || '添加失败'));
  } catch (e) {
    toast(e.message);
  }
}

/* 由 auto.js 的常驻事件通道调用：手机上发起配对时，桌面直接弹确认。 */
function showMobilePairPrompt(obj) {
  if (!obj || !obj.pair_id) return;
  const ok = confirm('手机「' + (obj.name || '手机') + '」请求配对\n\n'
    + '校验码：' + (obj.sas || '------') + '\n\n'
    + '与手机屏幕上显示的一致就点「确定」允许；否则点「取消」。');
  api('/api/mobile/pair/confirm', { pair_id: obj.pair_id, ok: !!ok })
    .then(() => loadMobileStatus())
    .catch((e) => toast(e.message));
}

function initMobilePane() {
  const on = (id, fn) => {
    const el = document.getElementById(id);
    if (el) el.addEventListener('click', fn);
  };
  on('mobile-toggle', mobileToggle);
  on('mobile-refresh', loadMobileStatus);
  on('mobile-firewall', mobileFirewall);
  on('mobile-pair-start', mobilePairStart);
  on('mobile-revoke-all', mobileRevokeAll);
  on('mobile-reset', mobileResetIdentity);
  on('mobile-audit', mobileAudit);
  if (!mobilePollTimer) {
    mobilePollTimer = setInterval(() => {
      if (!mobilePaneVisible()) return;
      loadMobileStatus();
    }, 3000);
  }
  loadMobileStatus();
}

document.addEventListener('DOMContentLoaded', initMobilePane);
