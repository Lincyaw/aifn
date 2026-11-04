# aifn SRT providers

Default file/shell isolation for `DeepSeekEngine`, with grants derived from each
call's workspace. Internal Node package for aifn's pinned npm runtime. Install through
`python -m aifn install`; its root lockfile pins SRT 0.0.81, Undici 8.12.3 and
Zod 4.6.7, including the root override for SRT's Zod import.

The trusted DSH process owns `services.mjs`. `remote-fs.mjs` routes file I/O to
`fs-worker.mjs` inside SRT; `shell.mjs` wraps every Bash command with SRT.
Controller configuration and trajectories are outside granted agent paths.
`cli.mjs doctor --policy FILE` checks real Linux isolation before agent prompts.
See ../../docs/srt-and-atif.md for deployment and verification limits.

The policy also carries the tool set: `tools.allow` names the callable tools and defaults
to the deployment set when absent, and `tools.mcpServers` grants the `mcp__<serverName>__`
namespace of an MCP server the call mounts. A declared `tools.allow` must keep
`validate_output`, `submit_output` and `refuse_output`, or the policy is rejected.

The installer applies a SHA-256 guarded SRT 0.0.81 Linux compatibility patch
for readonly-parent/writable-child restore ordering and readonly deny scaffolds.
The implementation and accepted hashes are in `src/aifn/runtime_patch.py`.
Proxy transports use short private temp directories; agent TMPDIR remains its
authorized scratch directory. Relay addresses use numeric loopback, preserving
proxy authentication while host `/etc/hosts` remains hidden.
