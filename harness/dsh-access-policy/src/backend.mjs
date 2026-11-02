import { access, realpath, stat } from 'node:fs/promises';
import { constants } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { SandboxManager, SandboxRuntimeConfigSchema, getDefaultWritePaths } from '@anthropic-ai/sandbox-runtime';
import { contains, explain, PolicyError } from './policy.mjs';

const require = createRequire(import.meta.url);
export const PACKAGE_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
export const SRT_ROOT = path.dirname(path.dirname(require.resolve('@anthropic-ai/sandbox-runtime')));
export const SRT_VERSION = '0.0.81';

export async function commandFilesystem(config) {
  await SandboxManager.waitForNetworkInitialization();
  // A root read-deny also hides the proxy socket parent. Re-expose only the
  // two transport nodes, so the trusted socat relays can reach the policy proxy.
  const sockets = [...new Set([SandboxManager.getLinuxHttpSocketPath(),
    SandboxManager.getLinuxSocksSocketPath()].filter(Boolean))];
  return { filesystem: { ...config.filesystem, allowRead: [...config.filesystem.allowRead,
    ...sockets.map(socket => ({ path: socket, literal: true }))] } };
}

export function quote(argument) {
  if (typeof argument !== 'string' || argument.includes('\0')) throw new PolicyError('Invalid command argument');
  return `'${argument.replaceAll("'", "'\\''")}'`;
}

export async function executable(name, searchPath = process.env.PATH ?? '') {
  const candidates = path.isAbsolute(name) ? [name]
    : searchPath.split(path.delimiter).filter(p => path.isAbsolute(p)).map(p => path.join(p, name));
  for (const candidate of candidates) {
    try {
      await access(candidate, constants.X_OK);
      if ((await stat(candidate)).isFile()) return await realpath(candidate);
    } catch (error) {
      if (!['ENOENT', 'EACCES', 'ENOTDIR'].includes(error.code)) throw error;
    }
  }
  throw new PolicyError(`Executable not found: ${name}`, 7);
}

export async function resolveRuntime(policy, dsh) {
  const names = ['bash', 'bwrap', 'socat', 'rg'];
  const paths = await Promise.all(names.map(name => executable(name)));
  const tools = Object.fromEntries(names.map((name, index) => [name, paths[index]]));
  tools.node = await realpath(process.execPath);
  if (dsh) tools.dsh = await executable(dsh);
  if (!['x64', 'arm64'].includes(process.arch)) throw new PolicyError('Supported Linux architectures: x64, arm64', 7);
  tools.seccomp = path.join(SRT_ROOT, 'vendor', 'seccomp', process.arch, 'apply-seccomp');
  await access(tools.seccomp, constants.X_OK);
  const writes = [...policy.files.write, policy.stateDirectory];
  for (const [name, target] of Object.entries(tools)) {
    if (writes.some(root => contains(root, target))) throw new PolicyError(`Trusted ${name} binary is agent-writable: ${target}`);
    if (name !== 'seccomp' && !policy.files.runtimeRead.some(root => contains(root, target))) {
      throw new PolicyError(`Add this ${name} executable or its installation directory to files.runtimeRead: ${target}`);
    }
  }
  return tools;
}

export function compileSrt(policy, { controlFiles = [], controlDirectory, tools }) {
  const base = explain(policy);
  const readable = [...new Set([...base.readable, PACKAGE_ROOT, SRT_ROOT, ...controlFiles])].sort();
  const literal = value => ({ path: value, literal: true });
  const defaultWrites = getDefaultWritePaths({ denyRead: ['/'], allowRead: readable });
  const excludedDefaults = defaultWrites.filter(target => !target.startsWith('/dev/')
    && !base.writable.some(root => contains(root, target)));
  const deniedWrites = [...excludedDefaults, ...(controlDirectory ? [controlDirectory] : [])];
  const config = {
    network: {
      allowedDomains: policy.network.allow,
      deniedDomains: [],
      strictAllowlist: true,
      allowAllUnixSockets: false,
      allowLocalBinding: false,
    },
    filesystem: {
      denyRead: ['/', '/sys'],
      allowRead: readable.map(literal),
      allowWrite: base.writable.map(literal),
      denyWrite: deniedWrites.map(literal),
      allowGitConfig: false,
    },
    enableWeakerNestedSandbox: false,
    allowNestedUserNamespaces: false,
    enableWeakerNetworkIsolation: false,
    allowPty: false,
    bwrapPath: tools.bwrap,
    socatPath: tools.socat,
    ripgrep: { command: tools.rg },
    seccomp: { applyPath: tools.seccomp },
  };
  // Validate with the actual installed SRT schema before any process is launched.
  SandboxRuntimeConfigSchema.parse(config);
  return {
    config,
    effective: {
      ...base, readable,
      deniedDefaultWritePaths: excludedDefaults,
      protectedControlDirectory: controlDirectory ?? null,
      pseudoFilesystems: ['/proc (private PID namespace)', '/dev (private device filesystem)'],
      runtimeVersion: SRT_VERSION,
    },
  };
}

