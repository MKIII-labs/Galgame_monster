// 手机端 JS 与 Python 的加解密互通自检（由 tests/test_mobile_js_interop.py 调用）
// 用法：node interop.mjs <app.js 路径> <fixture.json 路径>
import fs from 'node:fs';
import vm from 'node:vm';

const [, , appPath, fixturePath] = process.argv;

globalThis.document = {
  querySelector: () => null,
  addEventListener: () => {},
  createElement: () => ({ style: {}, appendChild() {}, textContent: '' }),
};
globalThis.window = globalThis;
globalThis.location = { hash: '', protocol: 'https:', host: '127.0.0.1' };

let code = fs.readFileSync(appPath, 'utf8');
code = code.replace("document.addEventListener('DOMContentLoaded', boot);", '');
const build = vm.runInThisContext(
  '(function(){' + code + '\nreturn {Cipher, deriveKeys, transcriptBytes, sasCode};})');
const lib = build();

const fixture = JSON.parse(fs.readFileSync(fixturePath, 'utf8'));
const b64d = (s) => new Uint8Array(Buffer.from(s, 'base64'));
const b64e = (u8) => Buffer.from(u8).toString('base64');
const hex = (u8) => Buffer.from(u8).toString('hex');

function fail(msg) {
  console.error('FAIL: ' + msg);
  process.exit(1);
}

(async () => {
  // 1) transcript 字节一致
  const transcript = lib.transcriptBytes(fixture.fields);
  if (hex(transcript) !== fixture.transcript_hex) {
    fail('transcript 不一致\n  js=' + hex(transcript) + '\n  py=' + fixture.transcript_hex);
  }

  // 2) ECDH + HKDF 派生（密钥不可导出，正确性由第 3 步的帧字节整体比对）
  const e1 = await crypto.subtle.importKey('pkcs8', b64d(fixture.e1_pkcs8),
    { name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits']);
  const keys = await lib.deriveKeys(e1, fixture.e2_spki, transcript);

  // 3) 帧格式与 Python 完全一致（GCM 确定性：同密钥同 nonce 同明文 = 同密文）
  const cipher = new lib.Cipher(keys);
  for (const item of fixture.frames) {
    const frame = await cipher.seal(item.type, b64d(item.payload));
    if (hex(frame) !== item.frame_hex) {
      fail('帧不一致 type=' + item.type + '\n  js=' + hex(frame) + '\n  py=' + item.frame_hex);
    }
  }

  // 4) 能解开 Python 生成的帧（服务端→客户端方向）
  const cipher2 = new lib.Cipher(keys);
  for (const item of fixture.recv_frames) {
    const opened = await cipher2.open(new Uint8Array(Buffer.from(item.frame_hex, 'hex')));
    if (opened.type !== item.type || b64e(opened.payload) !== item.payload) {
      fail('解帧结果不一致 type=' + item.type);
    }
  }

  // 5) SAS 一致
  const sas = await lib.sasCode(transcript);
  if (sas !== fixture.sas) fail('SAS 不一致 js=' + sas + ' py=' + fixture.sas);

  console.log('OK');
})().catch((e) => fail(e && e.stack ? e.stack : String(e)));
