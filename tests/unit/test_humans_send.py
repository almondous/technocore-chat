"""Execute the page's send path with a real signature held across an identity change.

Node supplies WebCrypto; the browser probe separately exercises the visible controls and
server acceptance. The gate makes the race deterministic without a timing-sensitive sleep.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")

PROBE = r"""
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { webcrypto } from 'node:crypto';
import vm from 'node:vm';

const mode = process.argv[1];
const html = readFileSync('src/humans.html', 'utf8');
const send = html.match(/  function send\(\) \{[\s\S]*?\n  \}/)?.[0];
const logout = html.match(/keyOutEl\.addEventListener\('click', function \(\) \{([\s\S]*?)\n  \}\);/)?.[1];
assert.ok(send && logout, 'send and sign-out handlers exist');
const watchdog = setTimeout(() => { throw new Error('send did not settle'); }, 5000);

async function identity() {
  const pair = await webcrypto.subtle.generateKey('Ed25519', true, ['sign', 'verify']);
  const raw = await webcrypto.subtle.exportKey('raw', pair.publicKey);
  let n = BigInt('0xed01' + Buffer.from(raw).toString('hex')), encoded = '';
  const alphabet = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';
  while (n) { encoded = alphabet[Number(n % 58n)] + encoded; n /= 58n; }
  return { did: 'did:key:z' + encoded, key: pair.privateKey, publicKey: pair.publicKey };
}
const original = await identity(), replacement = await identity();
let release, started, settled;
const gate = new Promise(resolve => { release = resolve; });
const signing = new Promise(resolve => { started = resolve; });
const done = new Promise(resolve => { settled = resolve; });
const posts = [], errors = [];
const env = {
  TextEncoder, Promise, String, encodeURIComponent,
  me: original, room: 'identityprobe',
  textEl: { value: 'hello', focus() {} }, nickEl: { value: 'guest' },
  swept: value => value, nextNonce: () => 123,
  bytesB64u: value => Buffer.from(value).toString('base64url'),
  crypto: { subtle: { sign(...args) {
    started();
    return webcrypto.subtle.sign(...args).then(async signature => {
      await gate;
      return signature;
    });
  } } },
  fetch: async (url, options) => {
    posts.push({ url, options, payload: JSON.parse(options.body) });
    return { ok: true };
  },
  fail: message => { errors.push(message); settled(); },
  stopPoll() {}, pump() { settled(); },
  SEED_KEY: 'seed', drop() {}, renderIdentity() {}, say() {},
};
vm.createContext(env);
vm.runInContext(send + '\nsend();', env);
await signing;
if (mode !== 'unchanged') vm.runInContext(logout, env);
if (mode === 'switch') env.me = replacement;
assert.equal(posts.length, 0, 'no write before signing completes');
release();
await done;
assert.deepEqual(errors, [], 'changing identity must not crash the pending send');
assert.equal(posts.length, 1, 'exactly one write');
const { url, options, payload } = posts[0];
assert.equal(url, '/r/identityprobe');
assert.equal(options.method, 'POST');
assert.equal(payload.did, original.did, 'the clicked identity owns the pending send');
assert.equal(payload.text, 'hello');
assert.equal(payload.nonce, '123');
const signature = Buffer.from(payload.sig, 'base64url');
const canonical = new TextEncoder().encode('identityprobe|123|hello');
assert.equal(await webcrypto.subtle.verify('Ed25519', original.publicKey, signature, canonical), true);
assert.equal(await webcrypto.subtle.verify('Ed25519', replacement.publicKey, signature, canonical), false);
assert.equal(env.me, mode === 'unchanged' ? original : mode === 'switch' ? replacement : null,
             'finishing the send must not restore the previous identity');
clearTimeout(watchdog);
"""


@pytest.mark.skipif(NODE is None, reason="Node is needed to execute the page's send path")
@pytest.mark.parametrize("mode", ["unchanged", "logout", "switch"])
def test_pending_send_keeps_the_identity_that_started_it(mode):
    assert NODE is not None
    result = subprocess.run(
        [NODE, "--input-type=module", "-e", PROBE, mode],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
