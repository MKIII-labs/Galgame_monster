'use strict';

/* Galgame Monster · 手机端（局域网 PWA）
   链路：HTTPS（本地 CA）→ WebSocket → 应用层 E2E（ECDH P-256 → HKDF → AES-256-GCM）。
   设备私钥是不可导出的 CryptoKey（IndexedDB 保存），审批/发消息等高危请求由它签名。
   界面取向：深色、高信息密度，按「工具」而不是「聊天软件」来排版。 */

const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const enc = new TextEncoder();
const dec = new TextDecoder();
const T = { REQ: 1, RES_HEAD: 2, RES_CHUNK: 3, RES_END: 4, CANCEL: 5, PING: 6, PONG: 7, ERROR: 8 };
const SENSITIVE = new Set(['/api/agent/approve', '/api/agent/answer', '/api/session/new', '/api/chat']);
const VERSION = 1;

const state = {
  devKey: null, deviceId: '', deviceName: '', pinnedFp: '',
  pairFp: '', ticket: '', ticketSecret: '', pairName: '',
  link: null, sessions: [], current: '', streaming: {}, pending: {},
  connected: false, retry: 0, reconnectTimer: 0, view: 'sessions',
  filter: 'all', query: '', wakeLock: null,
};

/* ---------------- 小工具 ---------------- */

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function b64(buf) {
  const b = new Uint8Array(buf);
  let s = '';
  for (let i = 0; i < b.length; i += 0x8000) {
    s += String.fromCharCode.apply(null, b.subarray(i, i + 0x8000));
  }
  return btoa(s);
}

function unb64(text) {
  const bin = atob(String(text || ''));
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

function toHex(buf) {
  return Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, '0')).join('');
}

function toast(text, ms) {
  const old = document.querySelector('.toast');
  if (old) old.remove();
  const box = el('div', 'toast', text);
  document.body.appendChild(box);
  setTimeout(() => box.remove(), ms || 2400);
}

async function copyText(text, note) {
  try {
    await navigator.clipboard.writeText(text);
    toast(note || '已复制');
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); toast(note || '已复制'); } catch (e2) { toast('复制失败'); }
    ta.remove();
  }
}

function setConnected(on) {
  state.connected = !!on;
  const dot = $('#dot');
  if (dot) dot.className = 'dot ' + (on ? 'on' : 'off');
  const tip = $('#net-tip');
  if (tip) tip.textContent = on ? '已连接' : '未连接';
}

function setNetTip(text) {
  const tip = $('#net-tip');
  if (tip) tip.textContent = text;
}

/* ---------------- IndexedDB ---------------- */

function idbOpen() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open('gm-mobile', 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains('kv')) db.createObjectStore('kv');
      if (!db.objectStoreNames.contains('keys')) db.createObjectStore('keys');
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function store(storeName, mode, fn) {
  const db = await idbOpen();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(storeName, mode);
    const os = tx.objectStore(storeName);
    let out;
    try { out = fn(os); } catch (e) { reject(e); return; }
    tx.oncomplete = () => resolve(out && out.result !== undefined ? out.result : out);
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error);
  });
}

const kvGet = (k) => store('kv', 'readonly', (os) => os.get(k));
const kvSet = (k, v) => store('kv', 'readwrite', (os) => os.put(v, k));
const keyGet = (k) => store('keys', 'readonly', (os) => os.get(k));
const keySet = (k, v) => store('keys', 'readwrite', (os) => os.put(v, k));

/* ---------------- WebCrypto ---------------- */

async function sha256(data) {
  return new Uint8Array(await crypto.subtle.digest('SHA-256', data));
}

async function getDeviceKey() {
  if (state.devKey) return state.devKey;
  let key = await keyGet('ecdsa');
  if (!key) {
    key = await crypto.subtle.generateKey(
      { name: 'ECDSA', namedCurve: 'P-256' }, false, ['sign', 'verify']);
    await keySet('ecdsa', key);
  }
  state.devKey = key;
  return key;
}

async function devicePubB64(key) {
  return b64(await crypto.subtle.exportKey('spki', key.publicKey));
}

async function signB64(text) {
  return signBytesB64(enc.encode(String(text)));
}

async function signBytesB64(bytes) {
  const key = await getDeviceKey();
  const sig = await crypto.subtle.sign({ name: 'ECDSA', hash: 'SHA-256' },
    key.privateKey, bytes);
  return b64(sig);
}

async function verifyB64(pubB64, sigB64, bytes) {
  const pub = await crypto.subtle.importKey('spki', unb64(pubB64),
    { name: 'ECDSA', namedCurve: 'P-256' }, false, ['verify']);
  return crypto.subtle.verify({ name: 'ECDSA', hash: 'SHA-256' }, pub, unb64(sigB64), bytes);
}

async function hmacB64(secret, bytes) {
  const key = await crypto.subtle.importKey('raw', enc.encode(secret),
    { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
  return new Uint8Array(await crypto.subtle.sign('HMAC', key, bytes));
}

async function deriveKeys(e1Private, peerSpkiB64, transcript) {
  const peer = await crypto.subtle.importKey('spki', unb64(peerSpkiB64),
    { name: 'ECDH', namedCurve: 'P-256' }, false, []);
  const shared = await crypto.subtle.deriveBits({ name: 'ECDH', public: peer }, e1Private, 256);
  const salt = await sha256(transcript);
  const hk = await crypto.subtle.importKey('raw', shared, 'HKDF', false, ['deriveBits']);
  const bits = new Uint8Array(await crypto.subtle.deriveBits(
    { name: 'HKDF', hash: 'SHA-256', salt, info: enc.encode('gm-mobile-v1') }, hk, 576));
  return {
    c2s: await crypto.subtle.importKey('raw', bits.slice(0, 32), 'AES-GCM', false, ['encrypt', 'decrypt']),
    s2c: await crypto.subtle.importKey('raw', bits.slice(32, 64), 'AES-GCM', false, ['encrypt', 'decrypt']),
    c2sPrefix: bits.slice(64, 68),
    s2cPrefix: bits.slice(68, 72),
  };
}

function transcriptBytes(fields) {
  // 与 Python 侧一致：键排序 + 无空格 JSON
  const keys = Object.keys(fields).sort();
  const ordered = {};
  keys.forEach((k) => { ordered[k] = fields[k]; });
  return enc.encode(JSON.stringify(ordered));
}

function sasCode(transcript) {
  const buf = new Uint8Array(transcript.length + 4);
  buf.set(transcript, 0);
  buf.set(enc.encode('|sas'), transcript.length);
  return sha256(buf).then((digest) => {
    const n = ((digest[0] << 24) | (digest[1] << 16) | (digest[2] << 8) | digest[3]) >>> 0;
    return String(n % 1000000).padStart(6, '0');
  });
}

/* ---------------- 生物识别（仅高风险审批） ---------------- */

async function registerBiometric() {
  if (!window.PublicKeyCredential) return;
  try {
    const cred = await navigator.credentials.create({
      publicKey: {
        challenge: crypto.getRandomValues(new Uint8Array(32)),
        rp: { name: 'Galgame Monster', id: location.hostname },
        user: {
          id: crypto.getRandomValues(new Uint8Array(16)),
          name: 'gm-mobile',
          displayName: 'Galgame Monster 手机',
        },
        pubKeyCredParams: [{ type: 'public-key', alg: -7 }, { type: 'public-key', alg: -257 }],
        authenticatorSelection: {
          authenticatorAttachment: 'platform',
          userVerification: 'required',
          residentKey: 'preferred',
        },
        timeout: 60000,
      },
    });
    if (cred && cred.rawId) await kvSet('webauthn_id', new Uint8Array(cred.rawId));
  } catch (e) {
    // 没有屏幕锁 / 用户取消：不阻断配对，审批时退回二次确认
  }
}

/* 返回 'biometric'（已验证）/ 'none'（本环境不支持，退化为二次确认）/ 'denied'（验证失败）。
   注意：WebAuthn 的 RP ID 不接受 IP 地址，局域网用 https://IP:端口 访问时浏览器会拒绝注册，
   此时只能退化为显式二次确认；以后走域名（如 Tailscale 的 *.ts.net）时会自动启用。 */
async function biometricGate() {
  if (!window.PublicKeyCredential) return 'none';
  let credId = null;
  try { credId = await kvGet('webauthn_id'); } catch (e) { /* ignore */ }
  if (!credId) return 'none';
  try {
    await navigator.credentials.get({
      publicKey: {
        challenge: crypto.getRandomValues(new Uint8Array(32)),
        allowCredentials: [{ type: 'public-key', id: new Uint8Array(credId) }],
        userVerification: 'required',
        timeout: 60000,
      },
    });
    return 'biometric';
  } catch (e) {
    return 'denied';
  }
}

function isHighRisk(name) {
  return /run_command|shell|write_file|edit_file|apply_worktree|delete/i.test(String(name || ''));
}

/* ---------------- 帧加解密 ---------------- */

class Cipher {
  constructor(keys) {
    this.sendKey = keys.c2s;
    this.recvKey = keys.s2c;
    this.sendPrefix = keys.c2sPrefix;
    this.recvPrefix = keys.s2cPrefix;
    this.sendCounter = 0n;
    this.recvMax = -1n;
  }

  async seal(type, payload) {
    const header = new Uint8Array(17);
    header[0] = 0x47; header[1] = 0x4d; header[2] = VERSION; header[3] = type; header[4] = 0;
    header.set(this.sendPrefix, 5);
    new DataView(header.buffer).setBigUint64(9, this.sendCounter, false);
    const nonce = new Uint8Array(12);
    nonce.set(this.sendPrefix, 0);
    nonce.set(header.subarray(9, 17), 4);
    const ct = new Uint8Array(await crypto.subtle.encrypt(
      { name: 'AES-GCM', iv: nonce, additionalData: header }, this.sendKey,
      payload || new Uint8Array()));
    this.sendCounter += 1n;
    const out = new Uint8Array(17 + ct.length);
    out.set(header, 0);
    out.set(ct, 17);
    return out;
  }

  async open(frame) {
    if (frame.length < 33) throw new Error('帧过短');
    if (frame[0] !== 0x47 || frame[1] !== 0x4d || frame[2] !== VERSION) throw new Error('帧头不正确');
    const type = frame[3];
    const prefix = frame.subarray(5, 9);
    if (String(prefix) !== String(this.recvPrefix)) throw new Error('nonce 前缀不匹配');
    const counter = new DataView(frame.buffer, frame.byteOffset, frame.byteLength).getBigUint64(9, false);
    if (counter <= this.recvMax) throw new Error('重放帧');
    const nonce = new Uint8Array(12);
    nonce.set(prefix, 0);
    nonce.set(frame.subarray(9, 17), 4);
    const plain = new Uint8Array(await crypto.subtle.decrypt(
      { name: 'AES-GCM', iv: nonce, additionalData: frame.subarray(0, 17) },
      this.recvKey, frame.subarray(17)));
    this.recvMax = counter;
    return { type, payload: plain };
  }
}

/* ---------------- 链路 ---------------- */

class Link {
  constructor(ws, cipher, devKey) {
    this.ws = ws;
    this.cipher = cipher;
    this.devKey = devKey;
    this.seq = 0;
    this.pending = new Map();
    this.jsonQueue = [];
    this.jsonWaiters = [];
    this.chain = Promise.resolve();
    ws.onmessage = (ev) => {
      this.chain = this.chain.then(() => this._onMessage(ev)).catch(() => {});
    };
    ws.onclose = () => {
      setConnected(false);
      this.failAll(new Error('连接已断开'));
      appReconnect();
    };
  }

  _onMessage(ev) {
    if (typeof ev.data === 'string') {
      let obj = null;
      try { obj = JSON.parse(ev.data); } catch (e) { return; }
      const waiter = this.jsonWaiters.shift();
      if (waiter) waiter(obj);
      else this.jsonQueue.push(obj);
      return;
    }
    return this._onBinary(new Uint8Array(ev.data));
  }

  async _onBinary(frame) {
    let out;
    try { out = await this.cipher.open(frame); } catch (e) { return; }
    let obj = {};
    try { obj = JSON.parse(dec.decode(out.payload) || '{}'); } catch (e) { return; }
    if (out.type === T.PONG) return;
    const entry = this.pending.get(obj.id);
    if (!entry) return;
    if (out.type === T.RES_HEAD) {
      entry.head = obj;
    } else if (out.type === T.RES_CHUNK) {
      const bytes = unb64(obj.data);
      entry.chunks.push(bytes);
      if (entry.sse) {
        entry.text += dec.decode(bytes, { stream: true });
        this._feedSSE(entry);
      }
    } else if (out.type === T.RES_END) {
      if (entry.sse) this._feedSSE(entry, true);
      this.pending.delete(obj.id);
      entry.resolve({ status: entry.head && entry.head.status, head: entry.head,
        text: entry.text, bytes: concat(entry.chunks) });
    } else if (out.type === T.ERROR) {
      this.pending.delete(obj.id);
      entry.reject(new Error(obj.error || '请求失败'));
    }
  }

  _feedSSE(entry, final) {
    const parts = entry.text.split('\n\n');
    entry.text = final ? '' : parts.pop();
    parts.forEach((frame) => {
      const line = frame.split('\n').find((l) => l.startsWith('data: '));
      if (!line) return;
      let obj = null;
      try { obj = JSON.parse(line.slice(6)); } catch (e) { return; }
      if (entry.onEvent) entry.onEvent(obj);
    });
  }

  send(type, obj) {
    const payload = enc.encode(JSON.stringify(obj));
    return this.cipher.seal(type, payload).then((frame) => this.ws.send(frame));
  }

  async request(method, path, body, opts) {
    opts = opts || {};
    const id = 'r' + (++this.seq);
    const bytes = opts.raw ? opts.raw
      : (body === undefined ? new Uint8Array() : enc.encode(JSON.stringify(body)));
    const req = { id, method, path, body: b64(bytes) };
    if (opts.headers) req.headers = opts.headers;
    if (SENSITIVE.has(path)) {
      req.ts = Math.floor(Date.now() / 1000);
      const digest = toHex(await sha256(bytes));
      req.sig = await signB64(`${state.deviceId}\n${method}\n${path}\n${req.ts}\n${digest}`);
    }
    const promise = new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject, chunks: [], text: '', sse: !!opts.sse,
        onEvent: opts.onEvent, head: null });
    });
    await this.send(T.REQ, req);
    return promise;
  }

  cancel(id) { this.send(T.CANCEL, { id }); }

  failAll(err) {
    this.pending.forEach((entry) => entry.reject(err));
    this.pending.clear();
  }

  close() {
    try { this.ws.close(); } catch (e) { /* ignore */ }
  }
}

function concat(chunks) {
  let len = 0;
  chunks.forEach((c) => { len += c.length; });
  const out = new Uint8Array(len);
  let at = 0;
  chunks.forEach((c) => { out.set(c, at); at += c.length; });
  return out;
}

function waitJson(link, timeout) {
  if (link.jsonQueue.length) return Promise.resolve(link.jsonQueue.shift());
  return new Promise((resolve, reject) => {
    link.jsonWaiters.push(resolve);
    setTimeout(() => reject(new Error('等待桌面响应超时')), timeout || 15000);
  });
}

/* ---------------- 握手 + 配对 ---------------- */

async function connectOnce() {
  const info = await fetch('/handshake.json', { cache: 'no-store' }).then((r) => r.json());
  const pinned = state.ticket ? (state.pairFp || state.pinnedFp) : state.pinnedFp;
  if (pinned && info.fingerprint !== pinned) {
    throw new Error('桌面身份指纹不一致，已中止连接（可能被中间人替换）');
  }
  const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
  const ws = new WebSocket(proto + location.host + '/ws');
  ws.binaryType = 'arraybuffer';
  await new Promise((resolve, reject) => {
    ws.onopen = resolve;
    ws.onerror = () => reject(new Error('无法连接电脑网关（检查同一 Wi-Fi / 防火墙 / 开关）'));
  });

  const fake = { jsonQueue: [], jsonWaiters: [] };
  ws.onmessage = (ev) => {
    if (typeof ev.data !== 'string') return;
    let obj = null;
    try { obj = JSON.parse(ev.data); } catch (e) { return; }
    const waiter = fake.jsonWaiters.shift();
    if (waiter) waiter(obj); else fake.jsonQueue.push(obj);
  };

  const serverHello = await waitJson(fake, 10000);
  if (serverHello.t !== 'hello') throw new Error('网关返回异常');
  const mode = state.deviceId ? 'connect' : 'pair';
  const e1 = await crypto.subtle.generateKey({ name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits']);
  const e1Pub = b64(await crypto.subtle.exportKey('spki', e1.publicKey));
  const cnonce = b64(crypto.getRandomValues(new Uint8Array(32)));
  ws.send(JSON.stringify({
    t: 'hello', v: VERSION, mode, device_id: state.deviceId || '', e1: e1Pub,
    cnonce, ticket_id: state.ticket || '',
  }));

  const challenge = await waitJson(fake, 10000);
  if (challenge.t !== 'challenge') throw new Error(challenge.error || '握手失败');
  const fields = {
    v: VERSION, mode, sid: challenge.sid, device_id: state.deviceId || '',
    e1: e1Pub, e2: challenge.e2, cnonce, snonce: serverHello.snonce,
    fp: serverHello.fp,
  };
  const transcript = transcriptBytes(fields);
  const okSig = await verifyB64(serverHello.sign_pub, challenge.sig, transcript);
  if (!okSig) throw new Error('桌面签名校验失败，已中止连接');

  if (mode === 'pair') {
    const proof = await hmacB64(state.ticketSecret, transcript);
    ws.send(JSON.stringify({
      t: 'auth', proof: b64(proof),
      name: state.pairName || ('手机 ' + (navigator.platform || '')),
      platform: (navigator.userAgentData && navigator.userAgentData.platform) || navigator.platform || '',
      sign_pub: await devicePubB64(await getDeviceKey()),
    }));
    const waiting = await waitJson(fake, 15000);
    if (waiting.t === 'error') throw new Error(waiting.error || '配对失败');
    showSas(waiting.sas);
    const paired = await waitJson(fake, 125000);
    hideSas();
    if (paired.t !== 'paired') throw new Error(paired.error || '桌面未允许配对');
    state.deviceId = paired.device_id;
    state.pinnedFp = serverHello.fp;
    await kvSet('device_id', state.deviceId);
    await kvSet('device_name', paired.name || '');
    await kvSet('pinned_fp', state.pinnedFp);
    state.ticket = ''; state.ticketSecret = '';
    try { history.replaceState(null, '', location.pathname); } catch (e) { /* ignore */ }
    toast('配对成功，已信任这台电脑');
    registerBiometric().catch(() => {});
  } else {
    const sig = await signBytesB64(transcript);
    ws.send(JSON.stringify({ t: 'auth', sig }));
    const ready = await waitJson(fake, 15000);
    if (ready.t !== 'ready') throw new Error(ready.error || '设备认证失败');
  }

  const keys = await deriveKeys(e1, challenge.e2, transcript);
  const cipher = new Cipher(keys);
  const link = new Link(ws, cipher, await getDeviceKey());
  state.link = link;
  setConnected(true);
  state.retry = 0;
  return link;
}

function showSas(code) {
  const mask = $('#pair-mask');
  mask.classList.remove('hidden');
  $('#pair-title').textContent = '确认配对';
  $('#pair-text').textContent = '请在电脑上核对同一校验码并点「允许」。不一致请立刻拒绝。';
  const box = $('#pair-sas');
  box.textContent = code || '------';
  box.classList.remove('hidden');
}

function hideSas() {
  $('#pair-mask').classList.add('hidden');
}

async function appReconnect() {
  if (state.reconnectTimer) return;
  const wait = Math.min(2000 * Math.pow(2, Math.min(state.retry, 4)), 20000);
  state.retry += 1;
  setNetTip('重连中…');
  state.reconnectTimer = setTimeout(async () => {
    state.reconnectTimer = 0;
    try {
      await connectOnce();
      toast('已重新连接');
      await refreshSessions();
    } catch (e) {
      appReconnect();
    }
  }, wait);
}

/* ---------------- 会话列表 ---------------- */

async function refreshSessions() {
  if (!state.link) return;
  const r = await state.link.request('POST', '/api/mobile/state', {});
  const data = JSON.parse(dec.decode(r.bytes) || '{}');
  state.sessions = (data.sessions && data.sessions.sessions) || [];
  state.streaming = {};
  (Array.isArray(data.streaming) ? data.streaming : []).forEach((sid) => {
    state.streaming[sid] = true;
  });
  state.pending = data.pending || {};
  renderSessions();
  if (state.current) {
    const s = state.sessions.find((x) => x.id === state.current);
    if (s) $('#chat-title').textContent = s.title || '会话';
  }
}

function groupOf(ts) {
  const d = new Date((Number(ts) || 0) * 1000);
  const now = new Date();
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const t = d.getTime();
  if (t >= startToday) return '今天';
  if (t >= startToday - 86400000) return '昨天';
  if (t >= startToday - 6 * 86400000) return '本周';
  return '更早';
}

function shortTime(ts) {
  const d = new Date((Number(ts) || 0) * 1000);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return sameDay ? hh + ':' + mm : (d.getMonth() + 1) + '/' + d.getDate();
}

function pendingLabel(kind) {
  if (kind === 'approval') return '待批准';
  if (kind === 'ask') return '待回答';
  return '待处理';
}

function renderSessions() {
  const list = $('#session-list');
  if (!list) return;
  list.textContent = '';
  const q = state.query.trim().toLowerCase();
  const rows = state.sessions.filter((s) => {
    if (q && !String(s.title || '').toLowerCase().includes(q)) return false;
    if (state.filter === 'busy' && !state.streaming[s.id]) return false;
    if (state.filter === 'pending' && !state.pending[s.id]) return false;
    return true;
  });
  if (!rows.length) {
    list.appendChild(el('div', 'empty',
      (q || state.filter !== 'all') ? '没有匹配的会话' : '还没有会话，点「新建」开始'));
    return;
  }
  const groups = new Map();
  rows.forEach((s) => {
    const key = groupOf(s.updated_at);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  });
  groups.forEach((items, key) => {
    list.appendChild(el('div', 'group-title', key));
    items.forEach((s) => list.appendChild(sessionRow(s)));
  });
}

function sessionRow(s) {
  const row = el('div', 'sess' + (s.id === state.current ? ' current' : ''));
  const main = el('div', 'sess-main');
  main.appendChild(el('div', 'sess-title', s.title || '（未命名）'));
  const meta = el('div', 'sess-meta');
  meta.appendChild(el('span', '', shortTime(s.updated_at)));
  if (s.msg_total) meta.appendChild(el('span', '', s.msg_total + ' 条'));
  if (state.streaming[s.id]) meta.appendChild(el('span', '', '生成中'));
  main.appendChild(meta);
  row.appendChild(main);

  const badges = el('div', 'sess-badges');
  if (state.pending[s.id]) {
    badges.appendChild(el('span', 'badge pending', pendingLabel(state.pending[s.id])));
  } else if (state.streaming[s.id]) {
    badges.appendChild(el('span', 'badge busy', '进行中'));
  }
  if (badges.childNodes.length) row.appendChild(badges);
  row.addEventListener('click', () => openSession(s.id));
  return row;
}

/* ---------------- 聊天 ---------------- */

function escapeHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function renderInto(box, text) {
  const raw = String(text || '');
  box.textContent = '';
  if (!/```|`|\*\*|!\[|\]\(/.test(raw)) {
    box.textContent = raw;
    return;
  }
  let html = escapeHtml(raw);
  // 代码块先抽出来，避免里面的反引号/星号被二次处理
  const blocks = [];
  html = html.replace(/```\w*\n?([\s\S]*?)```/g, (m, code) => {
    blocks.push(code.replace(/\n$/, ''));
    return '\u0000' + (blocks.length - 1) + '\u0000';
  });
  html = html.replace(/`([^`\n]+)`/g, '<code>$1</code>');
  html = html.replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>');
  html = html.replace(/!\[([^\]\n]*)\]\(([^)\s]+)\)/g, (m, alt, src) => {
    if (/^https?:/i.test(src)) return '<img src="' + src + '" alt="' + alt + '">';
    return '<img data-sandbox="' + src + '" alt="' + alt + '">';
  });
  html = html.replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noreferrer">$1</a>');
  html = html.replace(/\u0000(\d+)\u0000/g, (m, i) =>
    '<pre><button class="copy-btn" type="button">复制</button><code>' + blocks[Number(i)] + '</code></pre>');
  box.innerHTML = html;
  box.querySelectorAll('img[data-sandbox]').forEach(hydrateImage);
  box.querySelectorAll('pre > .copy-btn').forEach((btn) => {
    btn.addEventListener('click', () => {
      const code = btn.parentNode.querySelector('code');
      if (code) copyText(code.textContent, '已复制代码');
    });
  });
  box.querySelectorAll('img').forEach((img) => {
    img.addEventListener('click', () => {
      if (img.src) showImage(img.src);
    });
  });
}

function showImage(src) {
  const mask = $('#img-mask');
  const img = $('#img-view');
  img.src = src;
  mask.classList.remove('hidden');
}

async function hydrateImage(img) {
  const path = img.getAttribute('data-sandbox');
  if (!path || !state.link) return;
  try {
    const r = await state.link.request('POST', '/api/sandbox/raw',
      { session_id: state.current, path });
    if (r.status !== 200) throw new Error('HTTP ' + r.status);
    const blob = new Blob([r.bytes], { type: (r.head && r.head.ctype) || 'image/png' });
    img.src = URL.createObjectURL(blob);
  } catch (e) {
    const note = el('span', 'hint', '图片读取失败：' + e.message);
    img.replaceWith(note);
  }
}

function bindLongPress(box, textGetter) {
  let timer = 0;
  box.addEventListener('touchstart', () => {
    timer = setTimeout(() => copyText(textGetter(), '已复制消息'), 600);
  }, { passive: true });
  ['touchend', 'touchmove', 'touchcancel'].forEach((name) => {
    box.addEventListener(name, () => clearTimeout(timer), { passive: true });
  });
}

async function openSession(id) {
  state.current = id;
  switchView('chat');
  const s = state.sessions.find((x) => x.id === id);
  $('#chat-title').textContent = (s && s.title) || '会话';
  setSub((s && s.msg_total ? s.msg_total + ' 条消息' : '') + (state.streaming[id] ? ' · 生成中' : ''));
  $('#messages').textContent = '';
  $('#cards').textContent = '';
  $('#cards').classList.add('hidden');
  state.turn = null;
  try {
    const r = await state.link.request('POST', '/api/session/messages',
      { session_id: id, limit: 60 });
    const data = JSON.parse(dec.decode(r.bytes) || '{}');
    (data.messages || []).forEach((m) => appendMessage(m));
  } catch (e) {
    appendMsg('system', '读取消息失败：' + e.message);
  }
  scrollBottom(true);
}

function appendMessage(m) {
  const role = m.role === 'user' ? 'user' : (m.role === 'assistant' ? 'assistant' : 'system');
  const text = typeof m.content === 'string' ? m.content : JSON.stringify(m.content || '');
  if (!text) return;
  appendMsg(role, text);
}

function appendMsg(role, text) {
  const box = el('div', 'msg ' + role);
  const body = el('div', 'text');
  renderInto(body, text);
  box.appendChild(body);
  bindLongPress(box, () => body.textContent || '');
  $('#messages').appendChild(box);
  scrollBottom();
  return box;
}

function appendNotice(text) {
  const box = el('div', 'msg system', text);
  $('#messages').appendChild(box);
  scrollBottom();
}

function setSub(text) {
  const sub = $('#chat-sub');
  if (sub) sub.textContent = text || '';
}

function nearBottom() {
  const box = $('#messages');
  return box.scrollHeight - box.scrollTop - box.clientHeight < 80;
}

function scrollBottom(force) {
  const box = $('#messages');
  if (force || nearBottom()) {
    box.scrollTop = box.scrollHeight;
    $('#btn-bottom').classList.add('hidden');
  } else {
    $('#btn-bottom').classList.remove('hidden');
  }
}

/* ---- 一轮生成 ---- */

function newTurn() {
  const bubble = el('div', 'msg assistant');
  const steps = el('div', 'steps hidden');
  const text = el('div', 'text');
  const typing = el('div', 'typing', '正在生成…');
  bubble.append(steps, text, typing);
  $('#messages').appendChild(bubble);
  bindLongPress(bubble, () => text.textContent || '');
  return { bubble, steps, text, typing, stepList: [] };
}

function argsBrief(args) {
  if (!args || typeof args !== 'object') return '';
  const val = Object.values(args).find((v) => typeof v === 'string' && v.trim());
  return val ? ' · ' + String(val).slice(0, 42) : '';
}

function addStep(turn, ev) {
  const det = el('details', 'step');
  det.dataset.running = '1';
  const sum = el('summary', '', '工具 · ' + (ev.name || '') + argsBrief(ev.args));
  const pre = el('pre');
  pre.textContent = JSON.stringify(ev.args || {}, null, 2);
  det.append(sum, pre);
  turn.steps.classList.remove('hidden');
  turn.steps.appendChild(det);
  turn.stepList.push({ det, name: ev.name || '', out: pre, running: true });
  scrollBottom();
}

function finishStep(turn, ev) {
  const list = turn.stepList;
  let item = null;
  for (let i = list.length - 1; i >= 0; i--) {
    if (list[i].running && (!ev.name || list[i].name === ev.name)) { item = list[i]; break; }
  }
  if (!item) return;
  item.running = false;
  item.det.dataset.running = '0';
  const res = String(ev.result || '').trim();
  item.out.textContent += '\n\n' + (res || '（完成）');
}

function appendStepOutput(turn, ev) {
  const list = turn.stepList;
  for (let i = list.length - 1; i >= 0; i--) {
    if (list[i].running) {
      list[i].out.textContent += String(ev.text || '');
      return;
    }
  }
}

function handleChatEvent(ev, turn) {
  if (!ev || !ev.type) return;
  if (ev.type === 'delta') {
    turn.text.textContent += ev.text || '';
    setSub('正在生成…');
    scrollBottom();
    return;
  }
  if (ev.type === 'tool') {
    addStep(turn, ev);
    setSub('运行 ' + (ev.name || '工具') + '…');
    return;
  }
  if (ev.type === 'tool_done') {
    finishStep(turn, ev);
    return;
  }
  if (ev.type === 'tool_output') {
    appendStepOutput(turn, ev);
    return;
  }
  if (ev.type === 'approval') {
    showApproval(ev);
    setSub('等待你批准');
    return;
  }
  if (ev.type === 'ask') {
    showAsk(ev);
    setSub('等待你回答');
    return;
  }
  if (ev.type === 'plan_review') {
    showNotice('计划已生成，待确认。手机端暂不支持改计划，请在电脑上批准。');
    setSub('等待计划确认');
    return;
  }
  if (ev.type === 'error' || ev.type === 'warn') {
    appendNotice(ev.message || ev.error || '出错');
    return;
  }
  if (ev.type === 'done') {
    finishTurn(turn, ev);
  }
}

function finishTurn(turn, ev) {
  if (turn.typing) turn.typing.remove();
  turn.typing = null;
  if (!turn.text.textContent.trim() && !turn.stepList.length) {
    turn.text.textContent = ev && ev.stopped ? '（已停止）' : '（无输出）';
  }
  if (ev && ev.error) appendNotice('本轮出错：' + ev.error);
  keepAwake(false);
  if (state.current) delete state.streaming[state.current];
  setSub('');
  setTimeout(refreshSessions, 300);
}

/* ---------------- 审批 / 提问 ---------------- */

function buttonEl(label, cls, onClick) {
  const b = el('button', cls || 'btn', label);
  b.type = 'button';
  b.addEventListener('click', onClick);
  return b;
}

function showCard(card) {
  const box = $('#cards');
  box.classList.remove('hidden');
  box.appendChild(card);
}

function showApproval(ev) {
  const card = el('div', 'card');
  card.appendChild(el('div', 'card-title', '需要你批准：' + (ev.name || '操作')));
  if (ev.reason) card.appendChild(el('div', 'hint', ev.reason));
  const pre = el('pre');
  pre.textContent = JSON.stringify(ev.args || {}, null, 2);
  card.appendChild(pre);
  const acts = el('div', 'row-actions');
  acts.appendChild(buttonEl('允许', 'btn primary tiny', async () => {
    if (isHighRisk(ev.name)) {
      const gate = await biometricGate();
      if (gate === 'denied') {
        toast('高风险操作需要指纹 / 面容验证');
        return;
      }
      if (gate === 'none'
          && !confirm('这是高风险操作（' + (ev.name || '') + '），当前环境不支持生物识别。确认允许？')) {
        return;
      }
    }
    await answerApproval(ev.aid, true);
    card.remove();
  }));
  acts.appendChild(buttonEl('拒绝', 'btn danger tiny', async () => {
    await answerApproval(ev.aid, false);
    card.remove();
  }));
  card.appendChild(acts);
  showCard(card);
  scrollBottom();
}

async function answerApproval(aid, ok) {
  try {
    await state.link.request('POST', '/api/agent/approve', { aid, ok: !!ok });
  } catch (e) {
    toast(e.message);
  }
}

function showAsk(ev) {
  const card = el('div', 'card');
  card.appendChild(el('div', 'card-title', 'AI 需要你回答'));
  const inputs = [];
  (ev.questions || []).forEach((q, idx) => {
    card.appendChild(el('div', 'hint', (idx + 1) + '. ' + (q.question || '')));
    const input = document.createElement('input');
    input.type = 'text';
    input.placeholder = '输入回答';
    card.appendChild(input);
    inputs.push(input);
    const opts = Array.isArray(q.options) ? q.options : [];
    if (opts.length) {
      const row = el('div', 'opt-row');
      opts.forEach((opt) => {
        row.appendChild(buttonEl(String(opt), 'btn tiny', () => { input.value = String(opt); }));
      });
      card.appendChild(row);
    }
  });
  const acts = el('div', 'row-actions');
  acts.appendChild(buttonEl('提交', 'btn primary tiny', async () => {
    try {
      await state.link.request('POST', '/api/agent/answer', {
        qid: ev.qid, answers: inputs.map((i) => i.value.trim()),
      });
      card.remove();
    } catch (e) {
      toast(e.message);
    }
  }));
  acts.appendChild(buttonEl('让 AI 自己决定', 'btn tiny', async () => {
    try {
      await state.link.request('POST', '/api/agent/ask/dismiss', {
        qid: ev.qid, note: '手机端选择让 AI 自行决定',
      });
      card.remove();
    } catch (e) {
      toast(e.message);
    }
  }));
  card.appendChild(acts);
  showCard(card);
  scrollBottom();
}

function showNotice(text) {
  const card = el('div', 'card notice');
  card.appendChild(el('div', 'hint', text));
  showCard(card);
}

/* ---------------- 发送 / 停止 ---------------- */

async function keepAwake(on) {
  try {
    if (on && 'wakeLock' in navigator && !state.wakeLock) {
      state.wakeLock = await navigator.wakeLock.request('screen');
    } else if (!on && state.wakeLock) {
      await state.wakeLock.release();
      state.wakeLock = null;
    }
  } catch (e) { /* 不支持就算了 */ }
}

async function sendMessage() {
  const input = $('#input');
  const text = input.value.trim();
  if (!text || !state.current || !state.link) return;
  input.value = '';
  input.style.height = 'auto';
  appendMsg('user', text);
  const turn = newTurn();
  state.streaming[state.current] = true;
  setSub('正在生成…');
  keepAwake(true);
  try {
    await state.link.request('POST', '/api/chat',
      { session_id: state.current, content: text }, {
        sse: true,
        onEvent: (ev) => handleChatEvent(ev, turn),
      });
  } catch (e) {
    if (turn.typing) {
      turn.typing.remove();
      turn.typing = null;
    }
    turn.text.textContent = '发送失败：' + e.message;
    delete state.streaming[state.current];
    keepAwake(false);
    setSub('');
  }
}

async function stopStreaming() {
  if (!state.current || !state.link) return;
  try {
    await state.link.request('POST', '/api/stop', { session_id: state.current });
    toast('已请求停止');
  } catch (e) {
    toast(e.message);
  }
}

/* ---------------- 视图 ---------------- */

function switchView(name) {
  state.view = name;
  ['sessions', 'chat', 'device'].forEach((v) => {
    $('#view-' + v).classList.toggle('hidden', v !== name);
  });
  $$('.tab').forEach((t) => t.classList.toggle('active', t.dataset.view === name));
  if (name === 'device') renderDevice();
  if (name === 'sessions') renderSessions();
}

function renderDevice() {
  const conn = $('#conn-info');
  conn.textContent = '';
  const rows = [
    ['状态', state.connected ? '已连接（HTTPS + E2E）' : '未连接（会自动重试）'],
    ['地址', location.host],
    ['设备名', state.deviceName || '手机'],
    ['设备 ID', state.deviceId || '（未配对）'],
    ['桌面公钥指纹', state.pinnedFp || '（未固定）'],
  ];
  rows.forEach(([k, v]) => {
    conn.appendChild(el('div', 'k', k));
    conn.appendChild(el('div', 'v', v));
  });
  const info = $('#device-info');
  info.textContent = '';
  info.appendChild(el('div', 'k', '说明'));
  info.appendChild(el('div', 'hint',
    '这台手机只能访问聊天、审批、截图、语音等白名单接口；主令牌与 API Key 不会下发到手机。'));
  fetch('/handshake.json', { cache: 'no-store' }).then((r) => r.json()).then((res) => {
    $('#ca-fp').textContent = res.ca_fingerprint || '';
  }).catch(() => {});
}

/* ---------------- 语音输入 ---------------- */

let recorder = null;
let chunks = [];

async function toggleMic() {
  if (recorder && recorder.state === 'recording') {
    recorder.stop();
    return;
  }
  if (!navigator.mediaDevices || !window.MediaRecorder) {
    toast('当前浏览器不支持录音（需要 HTTPS）');
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    recorder = new MediaRecorder(stream);
    chunks = [];
    recorder.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
    recorder.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      $('#btn-mic').textContent = '语音';
      await transcribe(new Blob(chunks, { type: recorder.mimeType || 'audio/webm' }));
    };
    recorder.start();
    $('#btn-mic').textContent = '停止';
  } catch (e) {
    toast('无法录音：' + e.message);
  }
}

async function transcribe(blob) {
  toast('识别中…', 6000);
  try {
    const buf = await blob.arrayBuffer();
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const audio = await ctx.decodeAudioData(buf);
    const pcm = toPcm16(audio, 16000);
    await ctx.close();
    const r = await state.link.request('POST', '/api/stt', pcm, {
      raw: pcm, headers: { 'X-Sample-Rate': '16000' },
    });
    const data = JSON.parse(dec.decode(r.bytes) || '{}');
    if (!data.ok) throw new Error(data.error || '识别失败');
    const input = $('#input');
    input.value = (input.value + ' ' + (data.text || '')).trim();
  } catch (e) {
    toast('语音识别失败：' + e.message);
  }
}

/* 任意采样率 → 16kHz 单声道 int16 PCM（线性插值，够识别用） */
function toPcm16(audioBuffer, targetRate) {
  const src = audioBuffer.getChannelData(0);
  const ratio = audioBuffer.sampleRate / targetRate;
  const len = Math.floor(src.length / ratio);
  const out = new Int16Array(len);
  for (let i = 0; i < len; i++) {
    const pos = i * ratio;
    const i0 = Math.floor(pos);
    const i1 = Math.min(src.length - 1, i0 + 1);
    const frac = pos - i0;
    const val = Math.max(-1, Math.min(1, src[i0] * (1 - frac) + src[i1] * frac));
    out[i] = val < 0 ? val * 0x8000 : val * 0x7fff;
  }
  return new Uint8Array(out.buffer);
}

/* ---------------- 启动 ---------------- */

async function boot() {
  const params = new URLSearchParams((location.hash || '').replace(/^#/, ''));
  if (params.get('t')) {
    const parts = String(params.get('t')).split('.');
    state.ticket = parts[0] || '';
    state.ticketSecret = parts.slice(1).join('.') || '';
    state.pairFp = params.get('fp') || '';
    state.pairName = params.get('n') || '';
    state.deviceId = '';
  }
  state.deviceId = state.deviceId || (await kvGet('device_id')) || '';
  state.deviceName = (await kvGet('device_name')) || '';
  state.pinnedFp = (await kvGet('pinned_fp')) || '';

  $('#btn-refresh').addEventListener('click', () => refreshSessions().catch((e) => toast(e.message)));
  $('#btn-new').addEventListener('click', async () => {
    try {
      await state.link.request('POST', '/api/session/new', { title: '手机新建' });
      await refreshSessions();
      toast('已新建会话');
    } catch (e) { toast(e.message); }
  });
  $('#btn-back').addEventListener('click', () => switchView('sessions'));
  $('#btn-send').addEventListener('click', sendMessage);
  $('#btn-stop').addEventListener('click', stopStreaming);
  $('#btn-mic').addEventListener('click', toggleMic);
  $('#btn-bottom').addEventListener('click', () => scrollBottom(true));
  $('#img-close').addEventListener('click', () => $('#img-mask').classList.add('hidden'));
  $('#img-mask').addEventListener('click', (e) => {
    if (e.target.id === 'img-mask') $('#img-mask').classList.add('hidden');
  });

  $('#session-search').addEventListener('input', (e) => {
    state.query = e.target.value || '';
    renderSessions();
  });
  $$('#session-filters .chip').forEach((chip) => {
    chip.addEventListener('click', () => {
      state.filter = chip.dataset.filter || 'all';
      $$('#session-filters .chip').forEach((c) => c.classList.toggle('active', c === chip));
      renderSessions();
    });
  });

  $('#btn-unpair').addEventListener('click', async () => {
    if (!confirm('解除配对会删除本机密钥，需要重新扫码。继续？')) return;
    await store('kv', 'readwrite', (os) => {
      os.delete('device_id'); os.delete('pinned_fp'); os.delete('device_name');
    });
    await store('keys', 'readwrite', (os) => os.delete('ecdsa'));
    location.reload();
  });
  $('#btn-reconnect').addEventListener('click', () => {
    if (state.link) state.link.close();
    state.retry = 0;
    appReconnect();
  });
  $('#pair-cancel').addEventListener('click', () => {
    hideSas();
    toast('已取消，可在电脑上重新生成二维码');
  });

  $$('.tab').forEach((t) => {
    t.addEventListener('click', () => {
      if (t.dataset.view === 'chat' && !state.current && state.sessions.length) {
        openSession(state.sessions[0].id);
        return;
      }
      switchView(t.dataset.view);
    });
  });

  const input = $('#input');
  input.addEventListener('input', () => {
    input.style.height = 'auto';
    input.style.height = Math.min(118, input.scrollHeight) + 'px';
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });

  $('#messages').addEventListener('scroll', () => {
    if (nearBottom()) $('#btn-bottom').classList.add('hidden');
  });

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && state.connected) refreshSessions().catch(() => {});
  });

  if (!state.deviceId && !state.ticket) {
    switchView('device');
    $('#conn-info').textContent = '';
    $('#conn-info').appendChild(el('div', 'hint',
      '尚未配对：请在电脑「设置 → 手机访问」里开启并生成二维码，用手机扫码打开本页。'));
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.register('/sw.js').catch(() => {});
    }
    return;
  }
  try {
    await connectOnce();
    await refreshSessions();
    switchView('sessions');
  } catch (e) {
    toast(e.message, 4000);
    switchView('device');
    renderDevice();
    appReconnect();
  }
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
}

document.addEventListener('DOMContentLoaded', boot);
