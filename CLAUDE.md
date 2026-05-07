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

## Heartbeat Configuration

LOQ install, Week 2 step 12. End-to-end smoke green 2026-05-06T00:59:07Z (US.SPY snapshot → Discord post → gbrain tick archive in one cycle). Per spec § Build Order step 12 + § Solo Operator Ergonomics: OpenClaw heartbeat agent reads gbrain watchlist, snapshots each ticker via moomooapi, posts a formatted summary to Discord, archives the tick to gbrain.

| Field | Value |
|---|---|
| Workspace contract | `~/.openclaw/workspace/HEARTBEAT.md` (LOQ) — 6-step recipe (read watchlist → snapshot → format → POST → archive → reply) |
| Watchlist source | gbrain page `arktrade/watchlist` (slug; tag `project-arktrade`); current tickers: `US.SPY` |
| Snapshot script | `cd /opt/ark-trade-agent && /home/ark-dev/.local/bin/uv run python .claude/skills/moomooapi/scripts/quote/get_snapshot.py --json <CODE> 2>/dev/null` (stderr suppression mandatory — moomoo SDK prints connection log lines that pollute the agent's tool output) |
| Discord post | `/opt/ark-trade-agent/scripts/notify_discord.py "<formatted-message>"`; env loaded via systemd `EnvironmentFile=` drop-in (covers OpenClaw-spawned subprocesses) |
| Archive sink | gbrain `arktrade/heartbeats/<...>` page per tick, mandatory frontmatter `tags: [project-arktrade]` |
| Cadence (target) | 30min default per spec § Build Order step 12 |
| Cadence (v0 actual) | **manual fire on demand** — `openclaw agent --agent main --message "Read /home/ark-dev/.openclaw/workspace/HEARTBEAT.md and execute every step in order. End with the heartbeat ok/fail line per the file." --thinking medium --timeout 600 --json`. Runtime ~260s wall, 21 tool calls. |
| Persistent cron | **PARKED.** `openclaw cron add` requires gateway scope upgrade (operator.admin + pairing + read) beyond local CLI's existing `operator.write`. `openclaw devices approve <requestId>` rejects with "unknown requestId" (id rotates per call). Investigate proper scope-upgrade flow during /cso pass (chain step 14). |
| Model used (actual) | `chutes/zai-org/GLM-4.7-TEE` — pi-agent's chutes provider default. Spec calls for `Kimi-K2.6-TEE`; both are 256K-context. To pin: pass `--model moonshotai/Kimi-K2.6-TEE` on `openclaw agent` (or in `cron add --model …` when cron is unblocked). |

**Path discipline:** all paths in HEARTBEAT.md must be absolute (`/home/ark-dev/.bun/bin/gbrain`, `/home/ark-dev/.local/bin/uv`, `/opt/ark-trade-agent/scripts/notify_discord.py`). The systemd-launched agent has no `~/.bun/bin` or `~/.local/bin` in its PATH.

**Slug-shape debt:** gbrain's slug normalizer drops path prefixes + ISO punctuation, so the agent's `arktrade/heartbeats/us-spy-20260506T005708Z` landed as bare `heartbeat-us-spy-20260506`. Pages remain tagged + searchable so the `project-arktrade` namespace still holds for federation; the on-disk hierarchy is what's lost. Tighten when HEARTBEAT.md gets revised: pre-compute the exact slug in the prompt instead of describing how to construct one. Captured in /wiki-ingest backlog.

## TradingAgents Configuration

LOQ install, Week 3 steps 13 + 14. Vanilla `TauricResearch/TradingAgents` v0.2.4 (NOT the Alpaca fork — locked by /ccg adversarial review amendment). LLM-ping smoke green 2026-05-07T15:46Z (Kimi K2.6 returned `' OK'` via patched chutes provider; bind-mount round-trips clean with UID alignment 1000=1000).

| Field | Value |
|---|---|
| Repo path (LOQ) | `/opt/tradingagents/` (clone of `https://github.com/TauricResearch/TradingAgents.git`) |
| Pinned ref | tag `v0.2.4` ≡ commit `7c37249f808f9c169ad2198dc384166e7ca7adf9` (2026-04-25) |
| Image | `tradingagents-tradingagents:latest` — built from `/opt/tradingagents/Dockerfile` (Python 3.12-slim, two-stage); ~790MB |
| Image rebuild after patches | `cd /opt/tradingagents && docker compose build tradingagents` (~80s: ~62s pip + ~15s export) |
| Provider routing | `provider="chutes"` → `tradingagents/llm_clients/openai_client.py` `_PROVIDER_CONFIG["chutes"]` → `https://llm.chutes.ai/v1` (chat-completions API, NOT responses API) |
| Default models | `deep_think_llm="moonshotai/Kimi-K2.6-TEE"`, `quick_think_llm="moonshotai/Kimi-K2.6-TEE"` (256K ctx, 10K max-output cap). If the 10K max-output pinches at Trader/PM final-decision stage, switch to K2.5-TEE (same 256K ctx, 262K max-output) — both ride the same chutes provider, no patch change. |
| Secrets file (LOQ) | `/opt/tradingagents/.env` (mode 600, owned by ark-dev) — currently holds `CHUTES_API_KEY=...` (sourced from pi-agent's `~/.pi/agent/auth.json` chutes block, same key OpenClaw uses) |
| Checkpoint storage (host) | `/opt/ark-data/tradingagents-state/` (bind-mounted into container at `/home/appuser/.tradingagents/`). LangGraph checkpoint SQLite lives at `<host-mount>/cache/checkpoints/<TICKER>.db`. Survives container removal; introspectable from host. |
| UID alignment | container `appuser` uid/gid 1000 == host `ark-dev` uid/gid 1000 — no chmod gymnastics needed |
| Invocation pattern | `cd /opt/tradingagents && docker compose run --rm tradingagents` for the interactive CLI; programmatic / heartbeat-driven invocation will use `--entrypoint python` with a Python entry script (Week 3 step 18 introduces this) |
| Checkpoint enablement | `--checkpoint` runtime flag OR `default_config["checkpoint_enabled"] = True` in the programmatic entry script (deferred to W3.18; default config still ships `False` and patching `default_config.py` would be another patch surface) |

**TradingAgents patches (re-apply after `git pull` of `/opt/tradingagents/`):**

W3.14 — Chutes LLM provider routing (3 patches):

| File | Reason | Patch sha256 (post-patch) | Applied at |
|---|---|---|---|
| `tradingagents/llm_clients/openai_client.py` | Add `"chutes": ("https://llm.chutes.ai/v1", "CHUTES_API_KEY"),` to `_PROVIDER_CONFIG` (line 51) — TradingAgents v0.2.4 has no native Chutes provider | `25a5efd4dd324ee316a1bae76e6b1784729ebd9cfabe3f11b83c6cb24e52c88d` | 2026-05-07 |
| `tradingagents/llm_clients/factory.py` | Append `"chutes"` to `_OPENAI_COMPATIBLE` tuple (line 7) — routes `provider="chutes"` through `OpenAIClient` (chat-completions, not responses-API) | `1a77b4a7cf76960fca25eca583bccc367d661b0b6a59784e8e27936af4414020` | 2026-05-07 |
| `docker-compose.yml` | Swap named volume `tradingagents_data:/home/appuser/.tradingagents` → bind-mount `/opt/ark-data/tradingagents-state:/home/appuser/.tradingagents` for both `tradingagents` and `tradingagents-ollama` services. Orphaned `tradingagents_data:` volume definition at bottom is harmless leftover. | `3023d44c97bd7468fa29b3752333ecfdd1d5960865a4137fe80b45784c92e4ef` | 2026-05-07 |

W3.15 — moomoo content vendors as analyst tools (4 new files + 3 modifications):

| File | Reason | Patch sha256 | Applied at |
|---|---|---|---|
| `tradingagents/dataflows/moomoo_news.py` (NEW) | Lifted from sister-project `moomoo-news-search` SKILL.md. Calls `GET https://ai-news-search.moomoo.com/news_search` (public, unauthenticated). Implements `get_news(ticker, start_date, end_date)` matching the existing news_data signature; date-filtered, formatted as markdown. | `ffef740410d66f511f5e252169f06760bbf467537ebf78a25822b017a3009350` | 2026-05-07 |
| `tradingagents/dataflows/moomoo_digest.py` (NEW) | Lifted from sister-project `moomoo-stock-digest` SKILL.md. Same `/news_search` endpoint as moomoo_news but framed for direction-judgment by the calling analyst (bullish/bearish/neutral interpretation directive). Implements `get_stock_digest(ticker)`. | `17acdbb60f30f7bb8c37df64bbbd705d402652491f122b9c5b313ba30c83e81d` | 2026-05-07 |
| `tradingagents/dataflows/moomoo_sentiment.py` (NEW) | Lifted from sister-project `moomoo-comment-sentiment` SKILL.md. Calls `GET https://ai-news-search.moomoo.com/stock_feed`; strips HTML, returns post list with sentiment-classification directive. Implements `get_community_sentiment(ticker)`. | `bc71c0f1b89e16d530ca70140355351b0c8b1e0829be8a35ddff633899c2dc4e` | 2026-05-07 |
| `tradingagents/agents/utils/social_data_tools.py` (NEW) | New `@tool def get_community_sentiment(ticker)` for the social_data category — gives the Social Media Analyst a community-sentiment tool routed via dispatcher. | `1467f6f470c0d2ccd20c3eb8b6dbdc75d012eb59225d39dfff5ca2de1c5d174d` | 2026-05-07 |
| `tradingagents/dataflows/interface.py` (MODIFIED) | Imports moomoo vendors; appends `"moomoo"` to VENDOR_LIST; appends `"get_stock_digest"` to news_data tools; adds new TOOLS_CATEGORIES entry `"social_data"` with `get_community_sentiment`; adds `"moomoo": get_moomoo_news` to existing `get_news` VENDOR_METHODS entry; adds new VENDOR_METHODS entries for `get_stock_digest` and `get_community_sentiment` (moomoo-only vendors). | `f420c0b0f96205332fee0e2610aca0aa2155c9fd51f8ee6e4bfea7309b2fbdd8` | 2026-05-07 |
| `tradingagents/default_config.py` (MODIFIED) | Adds `"social_data": "moomoo"` default to `data_vendors`; updates `news_data` comment to list moomoo as an option. | `0738c001fa8a08293379f741d8e5c5e5c4fca9714a5b2a58c62dfb3ddf0c76a6` | 2026-05-07 |
| `tradingagents/agents/utils/news_data_tools.py` (MODIFIED) | Appends `@tool def get_stock_digest(ticker)` so the News Analyst can request a single-stock digest distinct from the raw `get_news` roundup. | `0af3ab7c57ef3409c325d8994c0beb110bb46175908b4aa470ad6821812421d7` | 2026-05-07 |

**Architecture note:** moomoo's public endpoints are unauthenticated — no API key, no signing, no auth state. The skills' "intelligence" (interpretation, sorting, classification) was always LLM-driven and stays in the TradingAgents analyst graph. Vendors only fetch + format raw items; the calling analyst LLM does the analysis. This keeps each vendor at ~50-70 LOC and avoids re-implementing skill workflows in deterministic Python.

**Dispatcher behavior** (verified W3.15 smoke): TradingAgents' `route_to_vendor` walks `VENDOR_METHODS[method]` for the configured vendor. Unregistered (method, vendor) pairs are skipped via `if vendor not in VENDOR_METHODS[method]: continue` — so moomoo_news only registers for `get_news`, not for `get_global_news`/`get_insider_transactions` (moomoo's public endpoint has no global-news or insider-transactions feed). The dispatcher gracefully routes those to yfinance/alpha_vantage instead.

**Re-apply protocol on TradingAgents bump:** the validators (`tradingagents/llm_clients/validators.py`) auto-accept unknown providers, so model-catalog edits aren't needed when bumping versions. The 3 NEW vendor files + `social_data_tools.py` are conflict-free on `git pull` (new files don't merge-collide). The 3 MODIFIED files (interface.py, default_config.py, news_data_tools.py) need re-application via the same surgical edits captured above. Total patch surface for W3.14+W3.15: 3 modifications from W3.14 + 4 new files + 3 modifications from W3.15 = 10 files tracked.

**Why this shape vs alternatives:** Faking Chutes as `provider="openai"` triggers TradingAgents' `use_responses_api=True` branch, which Chutes doesn't implement (Chutes is chat-completions-only, like xAI/DeepSeek/OpenRouter). The patch keeps Chutes in the same code path as the other OpenAI-compatible providers — minimal patch surface, no API mismatch.

**Pending hardening (/cso pass — chain step 14):** Chutes API key is plaintext in `/opt/tradingagents/.env` mode 0600 (mirrors OpenClaw's `~/.openclaw/openclaw.json` plaintext-chutes-key debt — same SecretRef migration target). Also: tighten the docker-compose service further (network_mode=none for fully-offline runs once analyst tools accept paths instead of URLs; user namespace remap; resource limits).

## Eval Ledger Configuration

LOQ install, Week 3 step 17. Per spec § Build Order step 17 + § Hermes Evaluation Metrics & Shadow Mode: SQLite eval ledger is the canonical source of truth — TradingAgents writes baseline decisions, Hermes (Week 4+) writes shadow proposals, paper-trade fills attach to baselines, the 7 decision-quality metrics fill in as outcome windows close. Hermes reads from this ledger only, never scrapes logs.

| Field | Value |
|---|---|
| Path (LOQ host) | `/opt/ark-data/eval-ledger.sqlite` (owned ark-dev:ark-dev) |
| Init script | `scripts/init_eval_ledger.py` (project repo) — idempotent, stdlib `sqlite3` only, runs `CREATE TABLE IF NOT EXISTS`. Override path via `--path` or `ARK_EVAL_LEDGER_PATH` env var. |
| Schema version | `0.1.0` (recorded in `schema_meta` table; bump on incompatible DDL changes) |
| Tables | `decisions`, `fills`, `metric_scores`, `schema_meta` |
| Indexes | `idx_decisions_ticker_kind_date`, `idx_decisions_parent`, `idx_fills_decision`, `idx_metric_scores_decision`, `idx_metric_scores_name` |
| Foreign keys | `decisions.parent_decision_id → decisions.decision_id` (shadow→baseline link); `fills.decision_id → decisions.decision_id`; `metric_scores.decision_id → decisions.decision_id` |
| Metrics constraint | `metric_scores.metric_name` CHECK enforces exactly 7 metrics from spec § Hermes Evaluation: `thesis_accuracy`, `next_day_direction`, `volatility_adjusted_move`, `max_adverse_excursion`, `catalyst_correctness`, `risk_rule_compliance`, `rationale_trade_match` |
| Decision-kind constraint | `decisions.decision_kind` CHECK enforces `'baseline'` (TradingAgents-emitted, executed in moomoo paper) or `'shadow'` (Hermes-emitted, never executed) — both ride the same row shape; shadow rows reference their baseline via `parent_decision_id`. |
| Re-init / reset | Re-run `python3 /opt/ark-trade-agent/scripts/init_eval_ledger.py` to ensure schema present (no-op if existing). To wipe and start fresh: `rm /opt/ark-data/eval-ledger.sqlite && python3 .../init_eval_ledger.py`. |

**Schema rationale (normalized 3 tables, not 1 wide):**

- One row per `metric_score` (per decision × metric) lets each metric land independently as its outcome window closes — instead of forcing the whole decision row to wait for the slowest metric (e.g. catalyst correctness, which may need T+5 data).
- Shadow decisions reuse the same `decisions` shape via `decision_kind='shadow'` + `parent_decision_id`. Hermes-Shadow Delta = sum of metric_score differences across (baseline, shadow) pairs over a window.
- Fills attach only to baseline decisions (shadow is never executed). The schema enforces this at app layer (don't INSERT into fills for shadow decision_ids), not at SQL layer.

**Container access pattern (resolved W3.18):** chose option (c) — host-side orchestrator at `scripts/run_prediction_cycle.py`. TradingAgents container stays vendor-pure (no moomoo SDK, no SQLite dependency added to image). Orchestrator drives container via `docker compose run --rm -T --entrypoint python tradingagents -` with the inner script piped through stdin; captures decision JSON between `__ARK_DECISION_JSON__` sentinels in stdout. Then it writes the decision + 7 deferred metric_score rows to the ledger directly, and shells out to `.claude/skills/moomooapi/scripts/trade/place_order.py` via `uv run python` for the paper-trade firing.

**5-tier rating mapping (orchestrator):**
- `Buy` / `Overweight` → BUY 1 share US.{TICKER} MARKET SIMULATE via place_order.py skill
- `Hold` → no order, decision row only
- `Underweight` / `Sell` → no order in v0 (no existing position to close; short-selling left for follow-up). Decision row still lands so Hermes Week 4+ can score it.

**First smoke (W3.18 closeout):** `python3 scripts/run_prediction_cycle.py NVDA 2026-05-06` ran 2026-05-07T17:13–17:32Z (~19 min wall). All 5 phases fired through Chutes/Kimi-K2.6: Market+Social+News+Fundamentals analysts → Bull/Bear research debate → Research Manager → Trader → Aggressive/Conservative/Neutral risk debate → Portfolio Manager. PM landed on `Underweight` (specific $196 entry, $202–$204 trim zone, $181 stop). `decision_id=baseline-20260507T173214Z-8c32b193` lives in ledger with `market_snapshot_json` 38.6KB, `order_intent_json` 7.4KB, `rationale` 2,990 chars + 7 `metric_scores` rows all `score=NULL` `notes='deferred-outcome-window-pending'`. BUY-branch independently exercised via direct place_order.py call returning `order_id=1133546` (US.NVDA SIMULATE).

**Hermes-gate cross-references:**
- 7-metric definitions + outcome windows: `vault/Specs/openclaw-hermes-trading-agent-v0-spec.md` § Hermes Evaluation Metrics & Shadow Mode
- Blessed-baseline prompt SHAs: `vault/Specs/blessed-baseline-tradingagents-prompts-v0.2.4.md` (Hermes proposals diff against this static set)
- Sample-size gate: ≥30–50 completed decision/outcome pairs before any prompt-quality claim. v0 auto-apply: NEVER (Shadow Mode only).

## Patched vendor scripts

These vendor-installed skill scripts have been lifted from sister project moomoo-stock with local modifications — re-apply if a skill is reinstalled (the install copies from `opend-skills.zip` and will overwrite):

- `.claude/skills/moomooapi/scripts/trade/get_all_portfolios.py` — accinfo_query now passes `currency=` derived from each account's `trdmarket_auth` (US→USD, HK→HKD); also adds a `--currency` override flag. Without this, the SDK defaults to HKD for Futu HK margin accounts and inflates US-account totals ~7.8x. Sibling `get_portfolio.py` already exposes `--currency` via CLI; pass `--currency USD` explicitly when querying that one.

## Patch Registry

Tracks lifted-skill modifications from sister projects (Approach A — Lift-and-Adapt, per `vault/Specs/openclaw-hermes-trading-agent-v0-spec.md`). When a moomoo skill patch is applied in either project, diff against the other repo before committing and update both registries.

| File | Source | Patch | Source commit | Sync sha256 | Last sync |
|---|---|---|---|---|---|
| `.claude/skills/moomooapi/scripts/trade/get_all_portfolios.py` | moomoo-stock (`/Users/sunginkim/.superset/worktrees/moomoo-stock/agent-integration`) | currency-fix (trdmarket_auth-derived USD/HKD; `--currency` override flag) | `975acff` | `217b2502…6d74e975` | 2026-05-02 |
