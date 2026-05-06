# Ark Trade Agent

Trading agent project. Vault holds project knowledge (sessions, tasks, research) and is symlinked from a centralized vault repo.

## Project Configuration

| Topic | Location |
|-------|----------|
| **Obsidian Vault** | `vault/` |
| **Session Logs** | `vault/Session-Logs/` |
| **Task Management** | `vault/TaskNotes/` — prefix: `ArkTrade-`, project: `ark-trade-agent` |

## GBrain Configuration

LOQ-local install, set up 2026-05-05 during Week 2 step 8. Federation to homelab CT120 (per `[[Compiled-Insights/federated-gbrain-shared-brain-on-ct120]]` in moomoo-stock vault) deferred — v0 stays self-contained on LOQ to preserve educational visibility and failure-independence. Namespacing convention adopted from day one so a future migration is mechanical.

| Field | Value |
|---|---|
| Engine | PGLite (local Postgres-compat, single-file) |
| Brain file | `/home/ark-dev/.gbrain/brain.pglite` (LOQ) |
| Schema version | 24 |
| Source repo | `https://github.com/garrytan/gbrain.git` |
| Pinned commit | `08b3698e90532b7b66c445e6b1d8cdfe71822802` (≡ v0.18.2) |
| Build toolchain | Bun 1.3.13 (LOQ-installed at `~/.bun/bin/bun`) |
| Invocation path (LOQ) | `/home/ark-dev/.bun/bin/gbrain` (Bun-shebang — systemd ExecStart needs this absolute path; PATH won't include `~/.bun/bin` in non-interactive shells) |
| MCP registration | NOT registered. OpenClaw shells out to the CLI; we re-evaluate `gbrain serve` + systemd unit only if MCP-typed tools become required. |
| Sync mode | Local-only. No `gstack-brain-sync`, no federation, no cross-machine push. |
| Namespacing | Slug prefix `arktrade/...` + tag `project-arktrade` on every page written from this project. Mandatory for future federation cleanup. |
| Future migration target | Homelab CT120 federated brain at `postgresql://gbrain_user@192.168.68.120:5433/gbrain` (already running, hosts moomoo-stock content with `project-moomoo` tag). Migration is `pg_dump | psql` + reconfigure `~/.gbrain/config.json`. |

Mac side: gbrain is federated to CT120 (`engine: postgres`, `database_url` to CT120) — the cross-project memory substrate moomoo-stock already populates. Mac and LOQ point at different brains for v0; reconcile only when Hermes (Week 4+) needs cross-project queries.

## OpenClaw Configuration

LOQ install, set up 2026-05-05 during Week 2 step 7. Spec § Risks calls out 9 March-2026 CVEs + 341 malicious skill marketplace entries + ~135K publicly-exposed instances → loopback-only bind is mandatory, no skills auto-installed, version pinned.

| Field | Value |
|---|---|
| Version | OpenClaw 2026.5.4 (commit 325df3e), released 2026-05-05 |
| Install | `npm install -g openclaw@2026.5.4` via user-level npm prefix `~/.npm-global/` (no sudo) |
| Binary path (LOQ) | `/home/ark-dev/.npm-global/bin/openclaw` |
| Daemon runtime | Node 22.22.1 (system), launched by `/usr/bin/node` |
| Systemd unit | `~/.config/systemd/user/openclaw-gateway.service` (user-level, lingering enabled) |
| Gateway port | 18789 (WS) + 18791 (RPC), both **loopback-only** (`127.0.0.1` + `[::1]`) |
| Auth | token mode (`OPENCLAW_GATEWAY_TOKEN` env-var-set in service) |
| LLM provider | `chutes-api-key` (auth-choice), API key from pi-agent's `~/.pi/agent/auth.json` chutes block, stored plaintext in `~/.openclaw/openclaw.json` (mode 0600) |
| Heartbeat | 30min default on agent `main` |
| Workspace | `/home/ark-dev/.openclaw/workspace` |
| Sessions | `/home/ark-dev/.openclaw/agents/main/sessions/sessions.json` |
| Channels | NOT wired in step 7. Discord wiring deferred to step 11 (`--skip-channels`). |
| Tailscale | OFF (LAN already segmented; spec doesn't want extra exposure surface) |
| MCP / skills | 8 eligible skills detected. NOT auto-installed — supply-chain risk per spec § Risks. Install only after review. |
| Command owner | NOT configured. Set `commands.ownerAllowFrom` to your Discord user ID when wiring step 11. |

Daemon control: `openclaw daemon {start|stop|restart|status|install|uninstall}`. Logs at `/tmp/openclaw/openclaw-YYYY-MM-DD.log`. Doctor: `openclaw doctor`. Health probe: `ws://127.0.0.1:18789` returns connectivity probe ok.

**Hardening pending /cso pass (chain step 14):** move chutes-api-key from plaintext → SecretRef + EnvironmentFile pattern (mirroring gateway token handling); add `commands.ownerAllowFrom` once Discord user ID known; tighten systemd unit (SystemCallFilter, IPAddressDeny=any).

## Discord Configuration

LOQ install, Week 2 step 11. Spec § Build Order step 11 amended (Telegram → Discord per user choice; OpenClaw + Hermes both shell out to a single helper for outbound posts).

| Field | Value |
|---|---|
| Bot identity | "Ark Trader" (id `1501310855910789200`, discriminator 8847) |
| Bot guild | "Ark-Trade-Agent" (guild id `1501310713916948480`) |
| Default channel | `#general` (text channel id `1501310714684637327`) |
| Bot permissions | guild perms `2248490645704257` (includes Send Messages); no per-channel overrides |
| Privileged Gateway Intents | all OFF (v0 outbound-only; flip on if slash commands or message-content reads needed later) |
| Secrets file (LOQ) | `/home/ark-dev/.config/ark-trade-agent/discord.env` (mode 600, owner ark-dev) |
| Secrets file (Mac) | `.env` at repo root (gitignored — `.gitignore` line 8 covers `.env` + `.env.*`) |
| Helper script | `scripts/notify_discord.py` (Python stdlib `urllib`, POSTs to `/api/v10/channels/{id}/messages`). Reads `DISCORD_BOT_TOKEN` + `DISCORD_CHANNEL_ID` from env. Modes: arg-based, stdin-based; exit codes 0/1/2 documented inline. |
| Invocation pattern (LOQ) | `cd /opt/ark-trade-agent && uv run python scripts/notify_discord.py "<message>"` (env loaded via systemd EnvironmentFile or `set -a; source discord.env; set +a` for shell use) |
| OpenClaw integration | systemd drop-in at `~/.config/systemd/user/openclaw-gateway.service.d/10-discord-env.conf` with `EnvironmentFile=/home/ark-dev/.config/ark-trade-agent/discord.env`, so any OpenClaw-spawned subprocess inherits the secrets when shelling out to `notify_discord.py`. |

**OpenClaw native discord plugin: PARKED.** OpenClaw 2026.5.4 has a `@openclaw/discord` channel plugin. We installed it (`channels add --channel discord --use-env --name ark-trade-agent`), enabled it (`plugins enable discord`), confirmed `DISCORD_BOT_TOKEN` reaches the gateway PID, and verified the plugin's `configured-state.js` only checks the env var. But OpenClaw's runtime state never reaches "connected" — `directory self --channel discord` returns "Not available" and `channels list` shows `token=none, not configured`. Direct Discord API calls from the same env work fine, so the issue is purely inside OpenClaw's plugin/auth layer. **Standalone helper is the canonical wire for v0**; revisit OpenClaw native discord during /cso pass (chain step 14) or when OpenClaw natively-routed messaging is required.

**Token rotation:** if leaked, regenerate at https://discord.com/developers/applications → Ark Trade Agent app → Bot → Reset Token. Update `.env` (Mac) and `/home/ark-dev/.config/ark-trade-agent/discord.env` (LOQ, mode 600), then `systemctl --user restart openclaw-gateway.service` so any OpenClaw subprocess picks it up.

## Patched vendor scripts

These vendor-installed skill scripts have been lifted from sister project moomoo-stock with local modifications — re-apply if a skill is reinstalled (the install copies from `opend-skills.zip` and will overwrite):

- `.claude/skills/moomooapi/scripts/trade/get_all_portfolios.py` — accinfo_query now passes `currency=` derived from each account's `trdmarket_auth` (US→USD, HK→HKD); also adds a `--currency` override flag. Without this, the SDK defaults to HKD for Futu HK margin accounts and inflates US-account totals ~7.8x. Sibling `get_portfolio.py` already exposes `--currency` via CLI; pass `--currency USD` explicitly when querying that one.

## Patch Registry

Tracks lifted-skill modifications from sister projects (Approach A — Lift-and-Adapt, per `vault/Specs/openclaw-hermes-trading-agent-v0-spec.md`). When a moomoo skill patch is applied in either project, diff against the other repo before committing and update both registries.

| File | Source | Patch | Source commit | Sync sha256 | Last sync |
|---|---|---|---|---|---|
| `.claude/skills/moomooapi/scripts/trade/get_all_portfolios.py` | moomoo-stock (`/Users/sunginkim/.superset/worktrees/moomoo-stock/agent-integration`) | currency-fix (trdmarket_auth-derived USD/HKD; `--currency` override flag) | `975acff` | `217b2502…6d74e975` | 2026-05-02 |
