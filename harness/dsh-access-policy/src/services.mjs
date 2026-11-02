import { spawn } from 'node:child_process';
import { mkdir, realpath } from 'node:fs/promises';
import { writeFileSync } from 'node:fs';
import { once } from 'node:events';
import { createInterface } from 'node:readline';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { SandboxManager } from '@anthropic-ai/sandbox-runtime';
import { FsError } from '@deepseek-ai/dsh-fs';
import { commandFilesystem, compileSrt, PACKAGE_ROOT, SRT_ROOT, quote, resolveRuntime } from './backend.mjs';
import { contains, loadPolicy, validatePaths } from './policy.mjs';

export const name = 'aifn-srt-boundary';
export const inject = ['tools', 'systemPrompt'];
let owned = false;
const ALLOWED_TOOLS = new Set(['read', 'write', 'edit', 'str_replace_editor', 'bash', 'validate_output', 'submit_output', 'refuse_output']);

class Boundary {
  constructor(config) { this.config = config; this.pending = new Map(); this.next = 0; this.ready = new Set(); }
  async init() {
    if (owned || process.platform !== 'linux') throw new Error('One Linux SRT boundary per DSH process is required');
    owned = true;
    this.ownsManager = true;
    this.policy = await loadPolicy(this.config.policyFile);
    await validatePaths(this.policy, this.config.policyFile, PACKAGE_ROOT, [SRT_ROOT]);
    this.tools = await resolveRuntime(this.policy);
    const scratch = this.policy.stateDirectory;
    this.environment = { PATH: [...new Set(Object.values(this.tools).map(p => path.dirname(p)))].join(':'),
      HOME: path.join(scratch, 'home'), TMPDIR: path.join(scratch, 'tmp'), LANG: 'C.UTF-8', LC_ALL: 'C.UTF-8' };
    for (const directory of [this.environment.HOME, this.environment.TMPDIR]) {
      try { await mkdir(directory, { mode: 0o700 }); } catch (error) { if (error.code !== 'EEXIST') throw error; }
      if (await realpath(directory) !== directory) throw new Error('Scratch subdirectory must not be a symlink');
    }
    const compiled = compileSrt(this.policy, { tools: this.tools });
    this.srtConfig = compiled.config;
    this.readable = compiled.effective.readable;
    // Initialize proxy resolution without ambient credentials/proxies; restore controller env immediately.
    const original = { ...process.env };
    for (const key of Object.keys(process.env)) delete process.env[key];
    Object.assign(process.env, this.environment);
    try { await SandboxManager.initialize(compiled.config); }
    finally { for (const key of Object.keys(process.env)) delete process.env[key]; Object.assign(process.env, original); }
    const argv = await this.wrap([this.tools.node, path.join(PACKAGE_ROOT, 'src', 'fs-worker.mjs')]);
    this.child = spawn(argv[0], argv.slice(1), { cwd: this.policy.cwd, env: this.environment, stdio: ['pipe', 'pipe', 'inherit'] });
    const started = new Promise((resolve, reject) => { this.started = { resolve, reject }; });
    const timer = setTimeout(() => this.started.reject(new Error('SRT filesystem worker startup timed out')), 15000);
    this.child.once('error', error => this.fail(error));
    this.child.once('exit', code => this.fail(new Error(`SRT filesystem worker exited (${code})`)));
    const lines = createInterface({ input: this.child.stdout });
    lines.on('line', line => {
      try {
        if (line.length > 32 * 1024 * 1024) throw new Error('SRT filesystem reply exceeds limit');
        const response = JSON.parse(line);
        if (response.ready) { this.started.resolve(); return; }
        const pending = this.pending.get(response.id); if (!pending) return;
        this.pending.delete(response.id); pending.dispose();
        if (response.error) { const error = new FsError(response.error.message, response.error.code ?? 'FS_IO_ERROR'); pending.reject(error); }
        else pending.resolve(response.value);
      } catch (error) { this.fail(error); this.child.kill('SIGKILL'); }
    });
    try { await started; } finally { clearTimeout(timer); }
  }
  async wrap(argv, signal, requestedCwd = this.policy.cwd) {
    if (this.closed || this.failed) throw this.failed ?? new Error('SRT boundary is closed');
    signal?.throwIfAborted();
    const cwd = path.resolve(this.policy.cwd, requestedCwd);
    if (!this.readable.some(root => contains(root, cwd))) throw new Error('Working directory is outside SRT grants');
    const preload = pathToFileURL(path.join(PACKAGE_ROOT, 'src', 'proxy-fetch.mjs')).href;
    // SRT's cwd parameter is ignored on Linux; change directory inside the boundary.
    const command = `cd -- ${quote(cwd)} || exit; export NO_PROXY='' no_proxy=''; export NODE_OPTIONS=${quote(`--import=${preload}`)}; exec ${argv.map(quote).join(' ')}`;
    const spec = await SandboxManager.wrapWithSandboxArgv(command, this.tools.bash, await commandFilesystem(this.srtConfig), signal, cwd);
    return spec.argv;
  }
  rpc(method, args, signal) {
    signal?.throwIfAborted();
    if (this.failed) return Promise.reject(this.failed);
    const id = ++this.next;
    const message = JSON.stringify({ id, method, args });
    if (message.length > 16 * 1024 * 1024) return Promise.reject(new Error('SRT filesystem request exceeds 16 MiB'));
    return new Promise((resolve, reject) => {
      const abort = () => { this.child.stdin.write(JSON.stringify({ cancel: id }) + '\n'); };
      this.pending.set(id, { resolve, reject, dispose: () => signal?.removeEventListener('abort', abort) });
      signal?.addEventListener('abort', abort, { once: true });
      this.child.stdin.write(message + '\n', error => { if (error) this.fail(error); });
    });
  }
  fail(error) {
    this.failed = error; this.started?.reject(error);
    for (const pending of this.pending.values()) { pending.dispose(); pending.reject(error); }
    this.pending.clear();
  }
  markReady(capability) {
    this.ready.add(capability);
    if (this.ready.has('fs') && this.ready.has('shell')) {
      // This file lives outside every namespace granted to agent tools.
      writeFileSync(this.config.readyFile, JSON.stringify({ nonce: this.config.nonce }), { mode: 0o600 });
    }
  }
  async close() {
    if (!this.ownsManager) return;
    if (this.closing) return this.closing;
    this.closing = this.closeOwned();
    return this.closing;
  }
  async closeOwned() {
    this.closed = true;
    this.fail(new Error('SRT boundary closed'));
    if (this.child?.pid && this.child.exitCode === null && this.child.signalCode === null) {
      const closed = once(this.child, 'close'); this.child.kill('SIGKILL'); await closed;
    }
    await SandboxManager.reset(); owned = false; this.ownsManager = false;
  }
}

export async function apply(ctx, config) {
  const boundary = new Boundary(config);
  try { await boundary.init(); } catch (error) { await boundary.close(); throw error; }
  ctx.provide('aifnSrt', boundary);
  ctx.systemPrompt.context({ name: 'deployment:srt', order: 9000, required: true, interpolate: false,
    text: () => `Agent file and shell operations run under SRT. Readable paths: ${JSON.stringify(boundary.readable)}. Writable paths: ${JSON.stringify([...boundary.policy.files.write, boundary.policy.stateDirectory])}. Network destinations: ${JSON.stringify(boundary.policy.network.allow)}. Deployment escalation cannot widen these grants.` });
  ctx.tools.guard(call => ALLOWED_TOOLS.has(call.name ?? call.tool?.name) ? undefined : 'This tool is outside the deployment SRT tool set.');
  ctx.on('dispose', () => boundary.close());
}
