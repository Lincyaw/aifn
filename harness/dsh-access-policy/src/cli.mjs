#!/usr/bin/env node
import { parseArgs } from 'node:util';
import { loadPolicy, explain, validatePaths, PolicyError } from './policy.mjs';
const HELP = `dsh-policy 0.1.0
  policy check --policy /var/lib/aifn/control/policy.json [--paths]
  policy explain --policy /var/lib/aifn/control/policy.json
  doctor --policy /var/lib/aifn/control/policy.json [--dry-run]

All results are JSON. doctor verifies a real Linux boundary before agent prompts.
The aifn SRT providers isolate file and shell operations; DSH stays outside.
`;
async function main() {
  const input = process.argv.slice(2);
  if (!input.length || input.includes('--help')) { process.stdout.write(HELP); return; }
  if (input.length === 1 && input[0] === '--version') { console.log('0.1.0'); return; }
  const { values, positionals } = parseArgs({ args: input, allowPositionals: true, strict: true,
    options: { policy: { type: 'string' }, paths: { type: 'boolean' }, 'dry-run': { type: 'boolean' } } });
  const command = positionals.join(' ');
  if (!['policy check', 'policy explain', 'doctor'].includes(command)) throw new PolicyError('Unknown command; use --help');
  if (!values.policy) throw new PolicyError('--policy is required');
  if (values.paths && command !== 'policy check') throw new PolicyError('--paths belongs to policy check');
  if (values['dry-run'] && command !== 'doctor') throw new PolicyError('--dry-run belongs to doctor');
  let result;
  if (command === 'doctor') result = await (await import('./launcher.mjs')).launch({ policyFile: values.policy, dryRun: values['dry-run'] });
  else {
    const policy = await loadPolicy(values.policy);
    if (values.paths) {
      const { PACKAGE_ROOT, SRT_ROOT } = await import('./backend.mjs');
      await validatePaths(policy, values.policy, PACKAGE_ROOT, [SRT_ROOT]);
    }
    result = command === 'policy check' ? { valid: true, pathsChecked: Boolean(values.paths) } : explain(policy);
  }
  console.log(JSON.stringify(result, null, 2));
}
try { await main(); }
catch (error) {
  const code = Number.isInteger(error.code) ? error.code : error.code === 'ENOENT' ? 3 : error.code === 'EACCES' ? 4 : 7;
  console.error(JSON.stringify({ error: error.name, message: error.message, exitCode: code })); process.exitCode = code;
}
