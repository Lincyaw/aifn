import { execFileSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { parseArgs } from 'node:util'

const { values } = parseArgs({
  options: {
    check: { type: 'boolean', default: false },
    help: { type: 'boolean', default: false },
  },
})

if (values.help) {
  console.log('Usage: node scripts/bump-dsh.mjs [--check]')
  console.log('Pin the newest npm dsh version, including prereleases, and update its lockfile.')
  console.log('--check queries versions without changing files.')
  process.exit(0)
}

const directory = fileURLToPath(new URL('../harness/aifn-harness/runtime/', import.meta.url))
const npm = process.platform === 'win32' ? 'npm.cmd' : 'npm'
const packageName = '@deepseek-ai/dsh'
const registry = '--registry=https://registry.npmjs.org'
const manifest = JSON.parse(readFileSync(`${directory}/package.json`, 'utf8'))
const current = manifest.dependencies[packageName]
// npm view sorts published versions using semver, including prereleases.
const versions = JSON.parse(execFileSync(npm, ['view', packageName, 'versions', '--json', registry], {
  encoding: 'utf8',
  timeout: 120_000,
  stdio: ['ignore', 'pipe', 'inherit'],
}))
if (!Array.isArray(versions) || !versions.length || typeof versions.at(-1) !== 'string') {
  throw new Error('npm returned no published dsh versions')
}
const latest = versions.at(-1)
const updateAvailable = latest !== current
if (updateAvailable && !values.check) {
  execFileSync(npm, [
    'install', '--prefix', directory, '--package-lock-only', '--save-exact',
    '--ignore-scripts', '--no-audit', '--no-fund', registry, `${packageName}@${latest}`,
  ], { timeout: 300_000, stdio: ['ignore', 2, 2] })
}
console.log(JSON.stringify({ current, latest, updateAvailable, updated: updateAvailable && !values.check }))
