import { createHash } from 'node:crypto';
import { access, open, realpath, stat } from 'node:fs/promises';
import { constants } from 'node:fs';
import { isIP } from 'node:net';
import path from 'node:path';
import { domainToASCII } from 'node:url';
import { z } from 'zod';

export class PolicyError extends Error {
  constructor(message, code = 2) { super(message); this.name = 'PolicyError'; this.code = code; }
}

export const MAX_POLICY_BYTES = 65536;
const absolute = z.string().min(1).max(4096).refine(value => value.startsWith('/') && !value.includes('\0'),
  'Use an absolute Linux path without NUL bytes');
const variable = z.string().regex(/^[A-Z_][A-Z0-9_]*$/);
// The DeepSeek function-name contract, which is what a registered tool is called by.
const tool = z.string().regex(/^[A-Za-z0-9_-]{1,64}$/, 'Use a registered tool name of [A-Za-z0-9_-]{1,64}');
// The dsh mcp-client contract for serverName; a policy accepts exactly the names it does.
const server = z.string().regex(/^[A-Za-z0-9_-]{1,32}$/, 'Use a dsh MCP server name of [A-Za-z0-9_-]{1,32}');
export const DEPLOYMENT_TOOLS = ['read', 'write', 'edit', 'str_replace_editor', 'bash', 'validate_output', 'submit_output', 'refuse_output'];
/** The result protocol: a tool set without these cannot deliver an answer at all. */
export const RESULT_TOOLS = ['validate_output', 'submit_output', 'refuse_output'];
export const PolicySchema = z.object({
  version: z.literal(1),
  cwd: absolute,
  stateDirectory: absolute,
  files: z.object({
    read: z.array(absolute).max(128).default([]),
    write: z.array(absolute).max(128).default([]),
    runtimeRead: z.array(absolute).max(128),
  }).strict(),
  network: z.object({
    allow: z.array(z.string().max(300)).max(128).default([]),
    // Named sockets are filtered by path only where the platform can: see SrtPolicy.unix_sockets.
    unixSockets: z.array(absolute).max(32).default([]),
    localPorts: z.boolean().default(false),
  }).strict(),
  environment: z.object({
    inherit: z.array(variable).max(128).default([]),
  }).strict().default({ inherit: [] }),
  tools: z.object({
    // Absent is the deployment tool set; a declared list replaces it rather than adding to it.
    allow: z.array(tool).max(64).optional(),
    mcpServers: z.array(server).max(16).default([]),
  }).strict().default({ mcpServers: [] }),
}).strict();

export function mcpNamespace(server) {
  // The mcp-client plugin owns `mcp__<serverName>__` and derives every public tool name
  // in it from the raw name; the tail may be normalized or hashed, the namespace may not.
  return `mcp__${server}__`;
}

export function contains(parent, child) {
  const relative = path.posix.relative(parent, child);
  return relative === '' || (relative !== '..' && !relative.startsWith('../') && !path.posix.isAbsolute(relative));
}

export function hostRule(value) {
  if (typeof value !== 'string' || value.trim() !== value || /[\s/@?#*\\]/.test(value)) {
    throw new PolicyError(`Expected an exact host:port network rule: ${value}`);
  }
  let host, port;
  const ipv6 = /^\[([^\]]+)\]:([0-9]+)$/.exec(value);
  const ordinary = /^([^:]+):([0-9]+)$/.exec(value);
  if (ipv6) {
    if (isIP(ipv6[1]) !== 6) throw new PolicyError(`Invalid IPv6 rule: ${value}`);
    host = `[${ipv6[1].toLowerCase()}]`; port = Number(ipv6[2]);
  } else if (ordinary) {
    host = domainToASCII(ordinary[1].replace(/\.$/, '')).toLowerCase();
    port = Number(ordinary[2]);
    if (/^[0-9.]+$/.test(ordinary[1]) && isIP(ordinary[1]) !== 4) throw new PolicyError(`Use a canonical IPv4 address: ${value}`);
    if (!host || host.length > 253 || (!host.includes('.') && host !== 'localhost') || !host.split('.').every(label => /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$/.test(label) && label.length <= 63)) {
      throw new PolicyError(`Invalid hostname rule: ${value}`);
    }
  } else {
    throw new PolicyError(`Network rules require an explicit port, for example api.deepseek.com:443: ${value}`);
  }
  if (!Number.isSafeInteger(port) || port < 1 || port > 65535) throw new PolicyError(`Invalid port: ${value}`);
  return `${host}:${port}`;
}

export function normalizePolicy(input) {
  const parsed = PolicySchema.safeParse(input);
  if (!parsed.success) throw new PolicyError(parsed.error.issues.map(issue => `${issue.path.join('.')}: ${issue.message}`).join('; '));
  const policy = parsed.data;
  for (const key of ['read', 'write', 'runtimeRead']) {
    policy.files[key] = [...new Set(policy.files[key].map(value => path.posix.normalize(value)))].sort();
    if (policy.files[key].includes('/')) throw new PolicyError(`${key} cannot grant the entire host root`);
  }
  policy.cwd = path.posix.normalize(policy.cwd);
  policy.stateDirectory = path.posix.normalize(policy.stateDirectory);
  if (policy.stateDirectory === '/') throw new PolicyError('stateDirectory cannot be /');
  const writable = [...policy.files.write, policy.stateDirectory];
  if (writable.some(root => policy.files.runtimeRead.some(runtime => contains(root, runtime) || contains(runtime, root)))) {
    throw new PolicyError('Runtime dependency roots and agent-writable roots must not overlap');
  }
  if (![...policy.files.read, ...policy.files.runtimeRead, ...writable].some(root => contains(root, policy.cwd))) {
    throw new PolicyError('cwd must be inside an explicitly readable or writable directory');
  }
  const forbiddenEnv = /^(?:PATH|HOME|DSH_HOME|DSH_ACCESS_.*|NODE_.*|BASH_ENV|ENV|LD_.*|DYLD_.*|.*PROXY|SRT_.*|SANDBOX_.*|CLAUDE_.*|TMP.*|SHELL|SSL_CERT_.*|CURL_.*)$/i;
  if (policy.environment.inherit.some(name => forbiddenEnv.test(name))) {
    throw new PolicyError('Do not inherit PATH, HOME, proxy, loader or sandbox-control environment variables');
  }
  policy.environment.inherit = [...new Set(policy.environment.inherit)].sort();
  policy.network.allow = [...new Set(policy.network.allow.map(hostRule))].sort();
  policy.network.unixSockets = [...new Set(policy.network.unixSockets)].sort();
  policy.tools.allow = [...new Set(policy.tools.allow ?? DEPLOYMENT_TOOLS)].sort();
  policy.tools.mcpServers = [...new Set(policy.tools.mcpServers)].sort();
  const missing = RESULT_TOOLS.filter(name => !policy.tools.allow.includes(name));
  if (missing.length) {
    throw new PolicyError(`A declared tool set must keep the result protocol: add ${missing.join(', ')}`);
  }
  return policy;
}

export async function loadPolicy(filename) {
  let content;
  try {
    const handle = await open(filename, 'r');
    try {
      const buffer = Buffer.alloc(MAX_POLICY_BYTES + 1);
      const { bytesRead } = await handle.read(buffer, 0, buffer.length, 0);
      if (bytesRead > MAX_POLICY_BYTES) throw new PolicyError('Policy JSON exceeds 64 KiB');
      content = JSON.parse(buffer.subarray(0, bytesRead).toString('utf8'));
    } finally { await handle.close(); }
  }
  catch (error) { throw new PolicyError(`Cannot read policy JSON: ${error.message}`, 3); }
  return normalizePolicy(content);
}

export function policyHash(policy) {
  return createHash('sha256').update(JSON.stringify(policy)).digest('hex');
}

export async function validatePaths(policy, policyFile, packageRoot, trustedRoots = []) {
  const writes = [...policy.files.write, policy.stateDirectory];
  for (const target of [...policy.files.read, ...policy.files.runtimeRead, ...writes, policy.cwd]) {
    const resolved = await realpath(target);
    if (resolved !== target) throw new PolicyError(`Path has a symlink or noncanonical spelling; configure its real path: ${target} -> ${resolved}`);
    const info = await stat(target);
    if (writes.includes(target) && !info.isDirectory()) throw new PolicyError(`Writable roots must be directories: ${target}`);
  }
  if (!(await stat(policy.cwd)).isDirectory()) throw new PolicyError('cwd must be a directory');
  for (const protectedPath of [await realpath(policyFile), await realpath(packageRoot), process.execPath]) {
    if (writes.some(root => contains(root, protectedPath))) throw new PolicyError(`Agent-writable roots cover protected launcher/config/runtime: ${protectedPath}`);
  }
  for (const protectedRoot of [await realpath(packageRoot), ...trustedRoots]) {
    if (writes.some(root => contains(root, protectedRoot) || contains(protectedRoot, root))) {
      throw new PolicyError(`Writable roots overlap trusted launcher/dependencies: ${protectedRoot}`);
    }
  }
  await access(policy.stateDirectory, constants.R_OK | constants.W_OK | constants.X_OK);
  return true;
}

export function explain(policy) {
  return {
    policyHash: policyHash(policy),
    cwd: policy.cwd,
    readable: [...new Set([...policy.files.read, ...policy.files.runtimeRead, ...policy.files.write, policy.stateDirectory])].sort(),
    writable: [...new Set([...policy.files.write, policy.stateDirectory])].sort(),
    network: {
      default: 'deny', allowedHosts: policy.network.allow,
      unixSockets: policy.network.unixSockets, localPorts: policy.network.localPorts,
    },
    inheritedEnvironmentNames: policy.environment.inherit,
    tools: { allowed: policy.tools.allow, mcpNamespaces: policy.tools.mcpServers.map(mcpNamespace) },
    note: 'Runtime-specific pseudo-filesystems and launcher support paths are reported by the backend plan.',
  };
}
