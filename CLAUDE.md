# CLAUDE.md — Hydra (Opus-Deipseek)

Single-file (`bot.py`) multi-model Discord bot. EVA/MAGI-themed heuristic router over
**Claude (Balthasar) · DeepSeek (Melchior) · Gemini (Caspar)** plus open-weight heads
**Qwen (Rei) · GLM (Asuka)** on Fireworks and **Mistral (Mari)** on its own EU API. Two-tier memory,
prompt caching, bookclub mode, Mandarin (`!speak`) + French (`!french`) TTS,
research panel (`!research`), and cost + carbon tracking.

This file is the working status + remaining-work tracker for the "Hydra Restructure"
spec. The full spec lives outside the repo (Sarah's Claude.ai artifact); this file is
self-contained — you don't need it to pick up the remaining work below.

## Run / verify
- `python bot.py` — needs `.env` (`DISCORD_TOKEN` + ≥1 model key) and `config.json`
  (`allowed_channels`, `default_model`).
- Syntax gate (no deps): `python -c "import ast; ast.parse(open('bot.py',encoding='utf-8').read())"`
- Import/logic test: `python -c "import bot"` works (deps installed). On Windows console
  prefix `PYTHONIOENCODING=utf-8` when printing bot output (emoji/CJK/IPA).

## Editing rules (load-bearing — read before touching bot.py)
- **Graceful degradation:** a missing API key disables ONLY that provider. `FIREWORKS_API_KEY`
  gates Qwen+GLM together; **Mistral has its own `MISTRAL_API_KEY`** (`api.mistral.ai`) — its
  flagship isn't on Fireworks serverless (404s "not deployed"). Absent keys disable exactly
  those providers; the original trio is untouched.
- **`provider.name` is the canonical routing key.** `_select_model`, `_estimate_confidence`,
  `panel_members`, cost, and persistence all branch on it. NEVER rename it. Display names and
  command aliases are a separate theme layer on top (see naming below).
- **Dispatch** routes on `provider.name in self.openai_compatible_clients`. Qwen+GLM share
  `self.fireworks_client`; Mistral uses `self.mistral_client` (`api.mistral.ai`). Claude uses the
  anthropic SDK; Gemini bookclub uses the native `cachedContents` path; everything else is the
  OpenAI-compatible shim (`_generate_openai_compatible_response`).
- **Model slugs drift** — verify in the live libraries. Fireworks (verified 2026-06):
  `qwen3p7-plus`, `glm-5p2`, `deepseek-v4-pro` (under `accounts/fireworks/models/`). Mistral on
  its own API uses `mistral-large-latest`. ⚠️ Mistral Large 3 is NOT on Fireworks serverless
  (`mistral-large-3-fp8` / `mistral-large-3` both 404 "not deployed") — hence the own-API route.
- New-provider **pricing + energy constants are estimates** flagged `VERIFY` in comments —
  confirm against the Fireworks pricing page before trusting `!cost` $ figures.

## Naming theme (decided; only the Discord/EVA half is in code)
- **Discord bot = EVA/MAGI.** MAGI trinity capped at 3 (Balthasar/Melchior/Caspar). New heads
  are the pilots: Mistral=Mari (`!mari`), Qwen=Rei (`!rei`), GLM=Asuka (`!asuka`).
- **Slack bot = ISAIC** = "International System of AI Coopertition", models named for the twelve
  tribes (Judah=Claude, Joseph=Gemini, Zebulun=DeepSeek, Naphtali=Mistral, Benjamin=Qwen,
  Gad=GLM). NOT in code yet — lands with the Slack adapter (Phase 4) as a per-platform skin.

---

## Status — 2026-06-24

### ✅ Built this session (live; validated offline, needs live-key smoke tests)
- **Phase 1 — Qwen/GLM on Fireworks, Mistral on `api.mistral.ai`.** Qwen+GLM share one
  Fireworks key (cached input = 0.5×input); Mistral is on its own EU API (`MISTRAL_API_KEY`)
  because Mistral Large 3 isn't on Fireworks serverless — a happy accident: EU-resident + France
  ~nuclear grid (grid≈20 in `!cost`). Graceful degradation, EVA aliases, per-provider personas,
  in `!cost` and the `!research all` panel.
- **Routing — intentional deviation from the spec.** The spec said new heads "override-only."
  Per Sarah's call that cheap models should earn routing: **Qwen is in the auto-router as the
  cheap coder/mathematician** (wins routine code/math, hands complex/careful work back to
  Claude), **Mistral gets a narrow French-intent nudge**, **GLM stays override-only**.
  ⚠️ Do NOT "fix" this back to all-override-only — it's deliberate.
- **Beyond the spec (Sarah's additions):**
  - `!research all` — opt-in 6-model panel (plain `!research` stays the lean core trio).
  - **Energy/CO₂ in `!cost`** — per-provider Wh (`est_wh_per_1k_tokens`) × per-provider grid
    intensity (`grid_gco2_per_kwh`, follows the *endpoint* not the brand), plus a separate
    amortized-training line (`train_tco2e` over `MODEL_LIFETIME_TOKENS`). Env knobs:
    `GRID_GCO2_PER_KWH`, `MODEL_LIFETIME_TOKENS`, `AMORTIZE_TRAINING`. All flagged
    order-of-magnitude; never used for routing.
  - **`!french` tutor** — inverse of the Mandarin path: Azure `fr-FR-DeniseNeural`, Mistral-native
    G2P returning text+IPA+liaison note, IPA is display-only (no forced phonemes — Azure infers),
    inline `[[french:..]]` for models. Shares Azure config with `!speak`.

### Remaining from the restructure spec
| Phase | Status | Priority |
|---|---|---|
| 0 — Provider registry refactor (`sdk_type`, config-driven) | ❌ skipped (additive approach used instead) | Low — working code; only if config-driven providers are wanted |
| 1 — Mistral/Qwen/GLM on Fireworks | ✅ done (with routing deviation) | — |
| 2 — Gemini Developer-API default | ✅ already the default (operator: keep billing on) | — |
| 2 — Gemini **Vertex** backend | ❌ not built | Optional / lab-only (residency/SLA) |
| 3 — Provider backend toggles: DeepSeek (`api`/`fireworks`/`self_hosted`) **+ Mistral** (`api`/`together`/`self_hosted`) | ❌ not built (both hardcoded to `api`) | Med — lab route |
| 4 — Slack platform abstraction | ❌ not built | **High — the big one** |
| 5 — Hermes delegation bridge | ❌ not built | Optional / lab-only |

#### Phase 0 — Provider registry refactor — *skipped, not blocking*
We added the new providers as hardcoded `ModelProvider(...)` instances + a name-keyed
`openai_compatible_clients` dict, NOT the spec's config-driven registry with `sdk_type` dispatch.
The goal (new providers, graceful degradation) is met. Do this only if you want providers declared
in `config.json` (with `routing_tags`) instead of code, or to split `bot.py` into a `hydra/`
package. It's a refactor of working code — defer unless there's a reason.

#### Phase 2 (Vertex) — *optional, deferred*
Developer API is the default and already no-train **as long as billing stays enabled on the Google
project** (operator setting, not code — Sarah is paying). Only build Vertex if the lab needs
EU/regional residency, IAM/VPC, SLA, or compliance: add `_generate_gemini_vertex_response()` +
`_ensure_gemini_vertex_cache()`, branch a `gemini.backend` toggle, and **backend-tag bookclub
cache handles** (`cachedContents` ≠ Vertex `CachedContent` — never reuse an id across backends).
Auth is ADC (`GOOGLE_APPLICATION_CREDENTIALS` service-account JSON) — the usual failure point.

#### Phase 3 — Provider backend toggles (DeepSeek + Mistral) — *TODO (lab route)*
Both DeepSeek and Mistral are currently **hardcoded to their `api` backend**; the spec wants a
config toggle. Cheapest kind of change — swap base_url/key/model only (all OpenAI-compatible, no
new generator) + adjust per-backend cost/grid.

**DeepSeek** stays on its China `api` for the Discord bot (Sarah's call — cheap, and "the Chinese can
have the shitposts"). To add the toggle: `providers.deepseek.backend = api | fireworks | self_hosted`.
- `fireworks` → `accounts/fireworks/models/deepseek-v4-pro`, shares `FIREWORKS_API_KEY`, keep
  per-token cost at Fireworks rates (~1.74/3.48), server cache at 50%. Update its
  `grid_gco2_per_kwh` to ~400 (US) when on this backend.
- `self_hosted` → local vLLM/Ollama base_url; set `supports_server_cache=False`; add a `local`
  cost mode in `_record_*_usage()` (skip per-token $, label the turn `local`); V4-Flash or 4-bit
  for single-GPU.

**Mistral** is hardcoded to `api.mistral.ai` (set up this way because Mistral Large 3 isn't on
Fireworks serverless — `mistral-large-3-fp8` 404s "not deployed", catalog/on-demand only). Same
toggle shape: `providers.mistral.backend = api | together | self_hosted`.
- `api` (current) → `api.mistral.ai`, `MISTRAL_API_KEY`, model `mistral-large-latest`. EU-resident
  + France ~nuclear grid (`grid_gco2_per_kwh=20`). **Best default for the Discord bot — keep it.**
- `together` → Together AI serverless (US) *does* host Mistral Large 3 — use for US residency / one
  fewer bill; set `grid_gco2_per_kwh≈400` + Together's rates. (Fireworks only has it on-demand —
  i.e. rent a dedicated GPU — not worth it for a chat bot.)
- `self_hosted` → Apache-2.0 weights, but Large 3 is 675B (multi-GPU). For single-GPU, self-host a
  smaller Mistral (Ministral 3B/8B/14B, Magistral Small 24B); reuse DeepSeek's `local` cost mode.

#### Phase 4 — Slack platform abstraction — *TODO, the big remaining chunk*
Introduce a `ChatPlatform` protocol; make `ClaudeBot`/`ConversationManager` depend on it instead
of `discord.py` directly. Extract `DiscordAdapter` (wrap existing logic, don't rewrite behavior);
build `SlackAdapter` on `slack_bolt` in **Socket Mode** (no public URL). Specifics:
- Threads → `thread_ts`; 👍/👎 calibration → `reaction_added`/`reaction_removed`; files →
  `files_upload_v2`; `!claude`-style commands → slash commands or `app_mention` parse.
- **Re-key persistence** `memories.json` on `(platform, team/guild, channel)` + a migration that
  namespaces existing Discord keys under `discord:<guild>` (else Discord/Slack clobber each other).
- **Formatting:** Slack is mrkdwn + Block Kit, not Markdown — audit every emit (code fences, bold,
  links, `!help` tables). LaTeX→PNG and the TTS MP3 paths are fine (image/file upload).
- **De-dupe** Slack's event retries on `event_id`/`client_msg_id`.
- Ship a Slack app manifest (scopes + event subs + slash commands). Tokens: `SLACK_BOT_TOKEN`
  (`xoxb-`), `SLACK_APP_TOKEN` (`xapp-`).
- This is where the **ISAIC naming skin** activates (Slack = lab bot).

#### Phase 5 — Hermes delegation bridge — *optional, lab-only*
Co-located Hermes Agent (separate service, thin bridge — do NOT merge codebases). Route `!research`
or a new `/task` to a local Hermes instance, stream the trajectory back via existing `send_text`/
`send_file`. **Egress discipline is a security rule, not a convenience:** force Hermes onto the same
hardened endpoints (Fireworks US/ZDR, self-hosted DeepSeek, Vertex); gate genuinely patentable
prompts to self-hosted only. Optional cron delivery into the lab channel. Hydra must still run
fully with Hermes absent.

### Cross-cutting follow-ups
- Verify **pricing**: Fireworks for `qwen3p7-plus` / `glm-5p2`, and `console.mistral.ai` for
  `mistral-large-latest` (current values are estimates). The **energy** constants
  (`est_wh_per_1k_tokens`, `train_tco2e`) are order-of-magnitude.
- Live smoke tests still owed: `!mari`/`!rei`/`!asuka` round-trips, `!french bonjour` (Azure
  fr-FR synth) and `!french how do you say …` (Mistral G2P), inline `[[french:..]]`.
- Operator: keep Fireworks prepaid balance topped up (auto-reload) and Gemini billing current.
