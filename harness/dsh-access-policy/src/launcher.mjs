import { spawn } from 'node:child_process';
import { lstat, mkdtemp, mkdir, realpath, rm, writeFile } from 'node:fs/promises';
import net from 'node:net';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { once } from 'node:events';
import { SandboxManager } from '@anthropic-ai/sandbox-runtime';
import { commandFilesystem, compileSrt, PACKAGE_ROOT, SRT_ROOT, payloadEnvironment, quote, resolveRuntime } from './backend.mjs';
import { contains, loadPolicy, PolicyError, policyHash, validatePaths } from './policy.mjs';

let active = false;

function environmentFor(policy, tools, original) {
  const environment = {
    PATH: [...new Set([path.dirname(tools.node), ...Object.values(tools).map(p => path.dirname(p)), '/usr/bin', '/bin'])].join(':'),
    HOME: path.join(policy.stateDirectory, 'home'),
    DSH_HOME: path.join(policy.stateDirectory, 'dsh'),
    TMPDIR: path.join(policy.stateDirectory, 'tmp'),
    CLAUDE_CODE_TMPDIR: path.join(policy.stateDirectory, 'tmp'),
    LANG: 'C.UTF-8',
    LC_ALL: 'C.UTF-8',
  };
  for (const name of policy.environment.inherit) {
    if (!(name in original)) throw new PolicyError(`Configured environment variable is not set: ${name}`, 7);
    environment[name] = original[name];
  }
  return environment;
}

function replaceEnvironment(environment) {
  for (const name of Object.keys(process.env)) delete process.env[name];
  Object.assign(process.env, environment);
}

function killGroup(child, signal) {
  if (!child?.pid) return;
  try { process.kill(-child.pid, signal); }
  catch (error) { if (error.code !== 'ESRCH') throw error; }
}

export async function launch({ policyFile, dryRun = false }) {
  if (active) throw new PolicyError('One active SRT boundary per launcher process is supported', 5);
  if (process.platform !== 'linux') throw new PolicyError('Enforced execution requires Linux; policy check/explain can run on other systems', 7);
  active = true;
  const originalEnvironment = { ...process.env };
  const originalCwd = process.cwd();
  let control, server, child, killTimer, wrapped = false;
  const controller = new AbortController();
  const interrupt = signal => {
    controller.abort();
    killGroup(child, signal);
    if (child?.pid) {
      killTimer = setTimeout(() => killGroup(child, 'SIGKILL'), 2000);
      killTimer.unref();
    }
  };
  const onInt = () => interrupt('SIGINT');
  const onTerm = () => interrupt('SIGTERM');
  process.on('SIGINT', onInt);
  process.on('SIGTERM', onTerm);
  try {
    const file = await realpath(policyFile);
    const policy = await loadPolicy(file);
    await validatePaths(policy, file, PACKAGE_ROOT, [SRT_ROOT]);
    const tools = await resolveRuntime(policy);
    const environment = environmentFor(policy, tools, originalEnvironment);
    const summary = compileSrt(policy, { tools }).effective;
    if (dryRun) return { dryRun: true, effective: summary, tools,
      kernelIsolationVerified: false };
    for (const directory of [environment.HOME, environment.DSH_HOME, environment.TMPDIR]) {
      try { await mkdir(directory, { mode: 0o700 }); }
      catch (error) { if (error.code !== 'EEXIST') throw error; }
      if (!(await lstat(directory)).isDirectory()) throw new PolicyError(`State subdirectory must be a real directory: ${directory}`);
      if (await realpath(directory) !== directory) throw new PolicyError(`State subdirectory must not be a symlink: ${directory}`);
    }
    for (const parent of ['/tmp', '/var/tmp']) {
      if ([...policy.files.read, ...policy.files.runtimeRead, ...policy.files.write, policy.stateDirectory]
        .some(root => contains(root, parent))) continue;
      control = await mkdtemp(path.join(parent, 'dsh-policy-'));
      break;
    }
    if (!control) throw new PolicyError('Keep /tmp or /var/tmp outside the granted roots so protected control files can be created');
    const snapshotFile = path.join(control, 'snapshot.json');
    const canary = path.join(control, 'private-canary');
    const marker = randomUUID();
    const compiled = compileSrt(policy, { tools, controlDirectory: control,
      controlFiles: [snapshotFile] });
    const snapshot = { format: 'dsh-access-policy-v1', hash: policyHash(policy),
      policy, effective: compiled.effective };
    await writeFile(snapshotFile, JSON.stringify(snapshot), { mode: 0o400, flag: 'wx' });
    await writeFile(canary, marker, { mode: 0o600, flag: 'wx' });
    environment.DSH_ACCESS_POLICY_HASH = snapshot.hash;
    // Linux AF_UNIX paths must fit sun_path; agent state paths may be very long.
    replaceEnvironment({ ...environment, TMPDIR: control, CLAUDE_CODE_TMPDIR: control });
    process.chdir(policy.cwd);
    server = net.createServer(socket => socket.end('host-canary'));
    server.listen(0, '127.0.0.1');
    await once(server, 'listening');
    await SandboxManager.initialize(compiled.config);
    const preload = pathToFileURL(path.join(PACKAGE_ROOT, 'src', 'proxy-fetch.mjs')).href;
    const command = argv => `${payloadEnvironment(environment.TMPDIR)} export NODE_OPTIONS=${quote(`--import=${preload}`)}; exec ${argv.map(quote).join(' ')}`;
    const run = async (argv, capture) => {
      const specification = await SandboxManager.wrapWithSandboxArgv(command(argv), tools.bash,
        await commandFilesystem(compiled.config), controller.signal, policy.cwd);
      wrapped = true;
      const chunks = [];
      child = spawn(specification.argv[0], specification.argv.slice(1), {
        cwd: policy.cwd, env: { ...specification.env, TMPDIR: environment.TMPDIR,
          CLAUDE_CODE_TMPDIR: environment.CLAUDE_CODE_TMPDIR }, detached: true,
        stdio: capture ? ['ignore', 'pipe', 'inherit'] : 'inherit',
      });
      let bytes = 0;
      child.stdout?.on('data', data => {
        bytes += data.length;
        if (bytes > 65536) { killGroup(child, 'SIGKILL'); return; }
        chunks.push(data);
      });
      try {
        const [code, signal] = await once(child, 'close');
        if (bytes > 65536) throw new PolicyError('Isolation probe produced excessive output', 7);
        return { code: code ?? (signal === 'SIGINT' ? 130 : 143), output: Buffer.concat(chunks).toString() };
      } finally {
        child = undefined;
        if (killTimer) clearTimeout(killTimer);
        SandboxManager.cleanupAfterCommand();
        wrapped = false;
      }
    };
    const probe = await run([tools.node, path.join(PACKAGE_ROOT, 'src', 'probe.mjs'),
      snapshotFile, canary, marker, environment.TMPDIR, String(server.address().port)], true);
    if (probe.code !== 0) throw new PolicyError('Isolation self-check failed; dsh was not started', 7);
    const checked = JSON.parse(probe.output);
    await new Promise(resolve => server.close(resolve)); server = undefined;
    return { verified: true, policyHash: snapshot.hash, checks: checked.checks,
      effective: compiled.effective, platform: process.platform };
  } finally {
    const cleanupErrors = [];
    const attempt = async action => { try { await action(); } catch (error) { cleanupErrors.push(error); } };
    await attempt(async () => {
      if (child) { const closed = once(child, 'close'); killGroup(child, 'SIGKILL'); await closed; }
    });
    if (killTimer) clearTimeout(killTimer);
    if (wrapped) await attempt(() => SandboxManager.cleanupAfterCommand());
    await attempt(() => SandboxManager.reset());
    if (server) await attempt(() => new Promise(resolve => server.close(resolve)));
    await attempt(() => process.chdir(originalCwd));
    replaceEnvironment(originalEnvironment);
    if (control) await attempt(() => rm(control, { recursive: true, force: true }));
    process.off('SIGINT', onInt); process.off('SIGTERM', onTerm);
    active = false;
    if (cleanupErrors.length) throw new AggregateError(cleanupErrors, 'Sandbox cleanup failed');
  }
}
