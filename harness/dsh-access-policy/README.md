# aifn SRT providers

Internal Node package for aifn's pinned npm runtime. Install through
`python -m aifn install`; its root lockfile pins SRT 0.0.81, Undici 8.12.3 and
Zod 4.6.6, including the root override for SRT's Zod import.

The trusted DSH process owns `services.mjs`. `remote-fs.mjs` routes file I/O to
`fs-worker.mjs` inside SRT; `shell.mjs` wraps every Bash command with SRT.
Controller configuration and trajectories are outside granted agent paths.
`cli.mjs doctor --policy FILE` checks real Linux isolation before agent prompts.
See ../../docs/srt-and-atif.md for deployment and verification limits.
