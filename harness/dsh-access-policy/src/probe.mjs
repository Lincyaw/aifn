import { readFile, open, writeFile, unlink } from 'node:fs/promises';
import { randomUUID } from 'node:crypto';
import path from 'node:path';
import net from 'node:net';

const [snapshot, canary, marker, temporary, hostPort] = process.argv.slice(2);
const checks = [];
const policy = JSON.parse(await readFile(snapshot, 'utf8'));
if (policy.format !== 'dsh-access-policy-v1') throw new Error('Readable snapshot check failed');
checks.push('protected policy snapshot is readable');
let hidden;
try { hidden = await readFile(canary, 'utf8'); }
catch (error) { if (!['ENOENT', 'EACCES', 'EPERM'].includes(error.code)) throw error; }
if (hidden === marker) throw new Error('Read allowlist failed: private host canary is readable');
checks.push('ungranted host file content is hidden');
let descriptor;
try { descriptor = await open(snapshot, 'r+'); }
catch (error) { if (!['EACCES', 'EPERM', 'EROFS'].includes(error.code)) throw error; }
if (descriptor) { await descriptor.close(); throw new Error('Policy snapshot is writable'); }
checks.push('policy snapshot cannot be opened for writing');
const output = path.join(temporary, `probe-${randomUUID()}`);
await writeFile(output, marker, { flag: 'wx', mode: 0o600 });
if (await readFile(output, 'utf8') !== marker) throw new Error('Writable state check failed');
await unlink(output);
checks.push('explicit state directory is writable');
const hostReachable = await new Promise((resolve, reject) => {
  const socket = net.connect({ host: '127.0.0.1', port: Number(hostPort) });
  socket.setTimeout(1000);
  socket.once('connect', () => { socket.destroy(); resolve(true); });
  socket.once('error', () => { socket.destroy(); resolve(false); });
  socket.once('timeout', () => { socket.destroy(); resolve(false); });
});
if (hostReachable) throw new Error('Network namespace check failed: host listener is reachable directly');
checks.push('host loopback listener is inaccessible directly');
try {
  await fetch(`https://${randomUUID()}.invalid`, { signal: AbortSignal.timeout(5000) });
  throw new Error('Unlisted destination unexpectedly returned a response');
} catch (error) {
  const chain = [error, error.cause, error.cause?.cause];
  if (!chain.some(item => item?.statusCode === 403 || /(?:403|Forbidden)/i.test(item?.message ?? ''))) {
    throw new Error(`Expected an explicit proxy denial, got: ${error.message}`, { cause: error });
  }
}
checks.push('unlisted network destination explicitly denied by proxy');
console.log(JSON.stringify({ checks, policyHash: policy.hash }));
