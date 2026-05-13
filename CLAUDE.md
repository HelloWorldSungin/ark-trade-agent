# Ark Trade Agent

Educational paper-trading agent stack. Hermes-as-coach over TradingAgents (vanilla) + OpenClaw + gbrain orchestrator. Mac dev / LOQ Ubuntu runtime split. Detailed config lives in `vault/Operations/`; insights + patterns in `vault/Compiled-Insights/`; spec + amendments in `vault/Specs/`.

## Project Configuration

| Topic | Location |
|-------|----------|
| **Obsidian Vault** | `vault/` (symlinked from centralized vault repo) |
| **Session Logs** | `vault/Session-Logs/` |
| **Task Management** | `vault/TaskNotes/` — prefix: `ArkTrade-`, project: `ark-trade-agent` |
| **Spec (source of truth)** | `vault/Specs/openclaw-hermes-trading-agent-v0-spec.md` |
| **As-built topology** | `vault/Operations/system-architecture-v0.md` |
| **LOQ runtime target** | `192.168.68.83` (ark-dev@ark-dev-server, Ubuntu 26.04) — see `vault/Operations/loq-hardware-verified.md` + `loq-reboot-runbook.md` |

## Subsystem Reference (vault/Operations/)

Read the relevant page **before** modifying any subsystem — these capture deployed state, patch surfaces, and known debt.

| Subsystem | Page | One-line state |
|-----------|------|----------------|
| GBrain | [gbrain-config.md](vault/Operations/gbrain-config.md) | LOQ-local PGLite v0.18.2; `arktrade/*` slug + `project-arktrade` tag namespacing |
| OpenClaw | [openclaw-config.md](vault/Operations/openclaw-config.md) | 2026.5.4 loopback-only gateway; user-level systemd; token in JSON (not env) |
| Discord | [discord-config.md](vault/Operations/discord-config.md) | "Ark Trader" bot in dedicated guild; standalone helper wire (native plugin parked) |
| Heartbeat | [heartbeat-config.md](vault/Operations/heartbeat-config.md) | Manual-fire on demand; persistent cron parked behind OpenClaw scope-upgrade |
| TradingAgents | [tradingagents-config.md](vault/Operations/tradingagents-config.md) | v0.2.4 vanilla in Docker; **10 tracked patches** (Chutes routing + moomoo vendors) |
| Eval Ledger | [eval-ledger-config.md](vault/Operations/eval-ledger-config.md) | SQLite at `/opt/ark-data/eval-ledger.sqlite`, schema v0.1.0, 3-table normalized |
| Hermes | [hermes-config.md](vault/Operations/hermes-config.md) | v0.12.0 "Curator", Curator disabled (auto-apply NEVER), dashboard on loopback :9119 |
| Hermes proposals | [hermes-proposal-pipeline.md](vault/Operations/hermes-proposal-pipeline.md) | Counterfactual shadow + dated MD; telemetry-proposal framing below sample-size gate |
| Outcome scorer | [outcome-scorer-config.md](vault/Operations/outcome-scorer-config.md) | 1 of 7 metrics live (`next_day_direction`); 6 metrics queued |

## Cross-cutting debt + open items

- **Chutes API key plaintext on 4 surfaces** (`~/.openclaw/openclaw.json`, `/opt/tradingagents/.env`, `~/.pi/agent/auth.json`, `~/.hermes/.env`). SecretRef + `EnvironmentFile=` consolidation pending /cso pass.
- **OpenClaw cron scope-upgrade** blocks persistent heartbeat + daily Hermes-proposal cron. Direct systemd-timer is the fallback.
- **Sample-size gate**: ≥30–50 fully-scored decision/outcome pairs before any prompt-quality claim. Currently <5 pairs scored. v0 auto-apply: **NEVER** (Shadow Mode only).

## Patched vendor scripts

Vendor-installed skill scripts with local modifications — re-apply if the skill is reinstalled (install overwrites from `opend-skills.zip`):

- `.claude/skills/moomooapi/scripts/trade/get_all_portfolios.py` — `accinfo_query` now passes `currency=` derived from each account's `trdmarket_auth` (US→USD, HK→HKD); also adds a `--currency` override flag. Without this, the SDK defaults to HKD for Futu HK margin accounts and inflates US-account totals ~7.8x. Sibling `get_portfolio.py` already exposes `--currency` via CLI; pass `--currency USD` explicitly when querying that one.

## Patch Registry

Tracks lifted-skill modifications from sister projects (Approach A — Lift-and-Adapt, per `vault/Specs/openclaw-hermes-trading-agent-v0-spec.md`). When a moomoo skill patch is applied in either project, diff against the other repo before committing and update both registries.

| File | Source | Patch | Source commit | Sync sha256 | Last sync |
|---|---|---|---|---|---|
| `.claude/skills/moomooapi/scripts/trade/get_all_portfolios.py` | moomoo-stock (`/Users/sunginkim/.superset/worktrees/moomoo-stock/agent-integration`) | currency-fix (trdmarket_auth-derived USD/HKD; `--currency` override flag) | `975acff` | `217b2502…6d74e975` | 2026-05-02 |

TradingAgents patch registry lives in [vault/Operations/tradingagents-config.md](vault/Operations/tradingagents-config.md) (10 patches across W3.14 + W3.15, with /cso-finding-3 trust-boundary wrap applied 2026-05-13).
