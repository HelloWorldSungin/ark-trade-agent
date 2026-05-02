# Ark Trade Agent

Trading agent project. Vault holds project knowledge (sessions, tasks, research) and is symlinked from a centralized vault repo.

## Project Configuration

| Topic | Location |
|-------|----------|
| **Obsidian Vault** | `vault/` |
| **Session Logs** | `vault/Session-Logs/` |
| **Task Management** | `vault/TaskNotes/` — prefix: `ArkTrade-`, project: `ark-trade-agent` |

## Patched vendor scripts

These vendor-installed skill scripts have been lifted from sister project moomoo-stock with local modifications — re-apply if a skill is reinstalled (the install copies from `opend-skills.zip` and will overwrite):

- `.claude/skills/moomooapi/scripts/trade/get_all_portfolios.py` — accinfo_query now passes `currency=` derived from each account's `trdmarket_auth` (US→USD, HK→HKD); also adds a `--currency` override flag. Without this, the SDK defaults to HKD for Futu HK margin accounts and inflates US-account totals ~7.8x. Sibling `get_portfolio.py` already exposes `--currency` via CLI; pass `--currency USD` explicitly when querying that one.

## Patch Registry

Tracks lifted-skill modifications from sister projects (Approach A — Lift-and-Adapt, per `vault/Specs/openclaw-hermes-trading-agent-v0-spec.md`). When a moomoo skill patch is applied in either project, diff against the other repo before committing and update both registries.

| File | Source | Patch | Source commit | Sync sha256 | Last sync |
|---|---|---|---|---|---|
| `.claude/skills/moomooapi/scripts/trade/get_all_portfolios.py` | moomoo-stock (`/Users/sunginkim/.superset/worktrees/moomoo-stock/agent-integration`) | currency-fix (trdmarket_auth-derived USD/HKD; `--currency` override flag) | `975acff` | `217b2502…6d74e975` | 2026-05-02 |
