# Hydra Restructure — Architecture & Implementation Spec

> **Archived reference (Rev 2).** This is the source-of-truth design spec for the multi-provider /
> Slack / hardening restructure. For **current status and what's left to build**, see
> [`../CLAUDE.md`](../CLAUDE.md) — it tracks which phases are done, deviations, and remaining work.

**Audience:** Claude Code (or any agent) executing this refactor.
**Source repo:** `VIXAL-OS/Opus-Deipseek` — a single-file (`bot.py`, ~6,800 lines) multi-model Discord bot (Claude Opus 4.8 · DeepSeek V4-Pro · Gemini 3.1 Pro core; Mistral · Qwen · GLM heads added in Phase 1), MAGI-themed, with heuristic routing, two-tier memory, prompt caching, bookclub mode, and cost tracking.
**Status of this doc:** implementation-ready. Work the phases in order; each is independently shippable and testable.

**Rev 2 changes (this version):**
- **Gemini:** Vertex is now an *optional* enterprise tier, not a co-equal toggle. The existing Gemini **Developer API** path is no-train **as long as billing is enabled on the Google account** — that's an account setting, not a code branch. You do **not** need Vertex to get no-train Gemini.
- **Open-weight providers consolidated on Fireworks AI:** Qwen, Mistral, **and GLM** now share a single OpenAI-compatible Fireworks endpoint — **US jurisdiction, zero-data-retention, one key, one bill**. Qwen's jurisdiction flips **China → US**.
- **DeepSeek gains a `fireworks` backend** (US ZDR, server-side cache, no hardware) as a middle rung between the China API and full self-hosting — primarily for the lab route.

---

## 1. Goals
Add four **core** capabilities to Hydra, all **config-toggleable** (default config preserves current behavior), plus an **optional fifth** for the lab variant:

1. **New providers via one US endpoint** — add Mistral, Qwen, **and GLM** alongside the existing three, **all served through a single Fireworks AI (OpenAI-compatible) endpoint**: US jurisdiction, zero-data-retention (ZDR), one API key. Uses the existing OpenAI-compatible code path; no new generator.
2. **Gemini backend** — the existing **Developer API** path is the default and is **no-train when billing is enabled on the Google account** (an account setting, *not* a code toggle — free AI Studio and the paid Developer API are the same endpoint/SDK/caching path; billing is the only difference and it's what flips training off). **Vertex** is an **optional** backend for when you additionally need EU/regional **data residency, IAM/VPC, SLA, or compliance** — i.e., the patentable/lab tier. Not needed for everyday no-train use.
3. **DeepSeek backend toggle** — `api | fireworks | self_hosted`. `api` is DeepSeek's own API (China). **`fireworks`** routes the open DeepSeek weights through Fireworks (US, ZDR, server-side cache, no hardware) — the recommended middle option for the lab route. `self_hosted` points at a local OpenAI-compatible server (vLLM / Ollama / SGLang) for maximal isolation (no egress at all).
4. **Slack support** — a platform abstraction so the same core runs on Discord *or* Slack, with all current features (threads, reactions, files, prefix/slash commands, bookclub).
5. **(Optional — lab research variant) Hermes Agent layer** — for the funds-strapped, sensitive-data lab deployment, delegate long-horizon and scheduled research workflows to a co-located, model-agnostic [Hermes Agent](https://hermes-agent.org/) instance pointed at the **same** hardened endpoints (Fireworks US/ZDR, self-hosted DeepSeek, or Vertex). Adds self-improving skills, persistent project memory, scheduled automations, and parallel subagents. Entirely skippable for the default chat-only deployment. See §10 and Phase 5.

## 2. Non-goals / constraints
- Do **not** rewrite the routing heuristics, memory semantics, or bookclub logic — preserve behavior; only change *where models live* and *what platform the I/O speaks to*.
- Keep `bot.py` runnable with any subset of providers present (graceful degradation is an existing feature — keep it).
- No behavior change when the default config is used. Every new behavior is opt-in via config.
- Keep the single-file structure if practical, **or** split into a small package (`hydra/`) if that materially eases the platform abstraction. If splitting, do it in Phase 0 and keep import paths obvious.

---

## 3. Current architecture (map before you touch)
From the existing `bot.py`, the relevant classes/methods:

| Component | Role |
|---|---|
| `BotConfig` | per-bot settings (context budget, message-fetch limit) |
| `ModelProvider` | per-model config: pricing (cached + tiered), context window, search backend, runtime stats |
| `SearchResult` | text + structured citations + grounded-answer flag |
| `ReadingMaterial` | bookclub: pinned text, chapter breaks, per-provider cache handles, TTL |
| `CalibrationTracker` | confidence-bid tracking via emoji feedback |
| `WorkingMemory` / `LongTermMemory` | auto-decay notes / permanent facts |
| `ConversationManager` | Discord history fetch, per-guild memories, per-channel reading materials, persistence |
| `ClaudeBot` | main bot class |

`ClaudeBot` methods that matter here:
- `_select_model()` — heuristic routing (no LLM call), three-way argmax with cost tiebreaks.
- `_generate_response()` — dispatches to Claude / DeepSeek / Gemini.
- `_generate_openai_compatible_response()` — **the shim** for DeepSeek + non-bookclub Gemini. **This is the seam Mistral / Qwen / GLM / Fireworks-DeepSeek all plug into.**
- `_generate_gemini_native_response()` — Gemini bookclub path (native API: `cachedContent` + `google_search` grounding). **This same path serves both free AI Studio and the paid Developer API — billing on the Google account is the only difference.**
- `_web_search()` / `_search_for()` / `_tavily_search()` / `_google_native_search()` — search backends.
- `_create_gemini_cache()` / `_ensure_gemini_cache()` — Gemini explicit `cachedContents` lifecycle.
- `_record_claude_usage()` — cache-aware token accounting.
- `_fetch_ao3_work()` / `_detect_chapter_breaks()` / `_slice_material_to_chapters()` — bookclub.

**Persistence:** `memories.json` (memories, calibration, reading materials, cache handles), keyed per-guild.
**Secrets:** `.env`. **Settings:** `config.json` (`allowed_channels`, `default_model`).

---

## 4. Target architecture

### 4.1 Provider registry (foundation for goals 1–3)
Replace the hardcoded three-provider setup with a **registry built from config**. A provider entry declares everything `_generate_response()` needs to dispatch without per-provider `if` branches.

```jsonc
// provider entry shape
{
  "id": "qwen",
  "display_name": "Qwen (Fireworks)",
  "alias": "qwen",                    // optional command alias (!qwen)
  "sdk_type": "openai_compatible",    // "anthropic" | "openai_compatible" | "gemini"
  "base_url": "https://api.fireworks.ai/inference/v1",
  "api_key_env": "FIREWORKS_API_KEY", // env var name; resolved at load
  "model": "accounts/fireworks/models/qwen3p6-plus",  // VERIFY exact slug in Fireworks model library
  "context_window": 131072,
  "pricing": { "input": 0.50, "cached_input": 0.25, "output": 3.00 },  // $/Mtok; Fireworks caches input at 50% by default
  "supports_server_cache": true,
  "search_backend": "tavily",         // "native_anthropic" | "google" | "tavily" | "none"
  "vision": false,
  "routing_tags": [],                 // empty = override-only (does not enter argmax)
  "enabled": true
}
```

Dispatch contract: `_generate_response(provider, messages, ...)` reads `provider.sdk_type` and calls one of three internal generators:
- `anthropic` → existing Claude path (native `web_search_20250305`, ephemeral cache).
- `openai_compatible` → existing `_generate_openai_compatible_response()` (DeepSeek today; **Mistral, Qwen, GLM, and Fireworks-DeepSeek** all use this).
- `gemini` → backend-dispatched (see 4.2).

Routing: extend `_select_model()` to argmax over providers whose `routing_tags` are non-empty. Providers with empty `routing_tags` are reachable only by explicit command/override. **Default Mistral, Qwen, and GLM to override-only** so the tuned three-way router is untouched; expose optional tags in config for users who want them in the rotation.

#### New providers — consolidated on Fireworks (goal 1)
All three new open-weight providers share **one** Fireworks endpoint and **one** key. Only the `model` string differs per entry. This gives **US jurisdiction + ZDR + a single prepaid bill** and, critically, **moves Qwen off DashScope (China) onto US infrastructure** — so the open Qwen weights run on a US zero-retention host, removing the China-jurisdiction concern entirely rather than just gating it to override-only.

| Provider | endpoint | model (example — verify slug) | jurisdiction | notes |
|---|---|---|---|---|
| Mistral | Fireworks `…/inference/v1` | `accounts/fireworks/models/mistral-large-3` | **US (Fireworks, ZDR)** | open-weight Mistral Large 3 (675B/41B MoE, Apache 2.0). Was EU `api.mistral.ai`; now consolidated on Fireworks. |
| Qwen | **same** Fireworks endpoint | `accounts/fireworks/models/qwen3p6-plus` | **US (Fireworks, ZDR)** | was DashScope (China) → now US ZDR. Kills the China-residency concern. Strong coding/math. |
| GLM | **same** Fireworks endpoint | `accounts/fireworks/models/glm-5p2` | **US (Fireworks, ZDR)** | newest GLM (5.2) is live on Fireworks; Opus-class open model. GLM 5.1 also available. |

- **One key:** add `FIREWORKS_API_KEY`. No `QWEN_API_KEY` (DashScope) or `MISTRAL_API_KEY` needed for the consolidated path.
- **Caching:** Fireworks discounts cached input by 50% by default — set `cached_input = 0.5 × input` and `supports_server_cache = true` for Fireworks entries. (Batch is also 50% but isn't used by the live bot path.)
- **Alternates still possible:** to use a direct endpoint instead (DashScope for Qwen, `api.mistral.ai` for Mistral, Zhipu for GLM), override `base_url` + `api_key_env` on that entry. Fireworks is the **default for the hardened / lab path** (US, ZDR, prepaid, one bill); direct endpoints are opt-in overrides.
- **Pricing anchors (serverless, $/Mtok in/out, verify on Fireworks pricing page):** GLM 5.1 `1.40 / 4.40`, Qwen 3.6 Plus `0.50 / 3.00`, DeepSeek V4 Pro `1.74 / 3.48`, DeepSeek V4 Flash `0.14 / 0.28`. Note serverless can run ~2–4× a model-creator's own direct API price — that delta is the US-residency + ZDR + single-bill premium, which is the point for the lab tier.

> MAGI/theming note: the EVA MAGI trinity is three by design. Adding heads fits the "Hydra" metaphor better than MAGI — either leave Mistral/Qwen/GLM un-personified, or extend with a separate naming scheme. Cosmetic; out of scope for correctness.

### 4.2 Gemini backend (goal 2) — Developer API default, Vertex optional
**Key clarification:** free AI Studio and the **paid Developer API are the same endpoint, SDK, and caching surface** (`_generate_gemini_native_response()` + `cachedContents`). The *only* difference is whether **billing is enabled on the Google Cloud project** — and that is exactly what flips training off. So **no-train Gemini does not require Vertex or a code change**; it requires billing enabled on the Developer API key Hydra already uses. (Operator note, not a config branch.)

Therefore the backend toggle is a **two-way code split**, with Vertex as the optional enterprise rung:
Config: `providers.gemini.backend = "developer_api" | "vertex"` (default `developer_api`).

| Backend | When to use | Auth | SDK / endpoint | Caching | Data policy |
|---|---|---|---|---|---|
| **`developer_api`** (default) | Everyday no-train use. Hydra's existing path. | `GEMINI_API_KEY` (Developer API key) | native AI Studio / Gemini Developer API (existing `_generate_gemini_native_response()`) | `cachedContents` (existing `_ensure_gemini_cache`) | **No-train when billing is enabled** on the project; free/unbilled tier trains. Billing is an account setting. |
| **`vertex`** (optional) | Only when you need EU/regional **data residency**, **IAM/VPC Service Controls**, **SLA**, or **compliance certs** (patentable/lab tier). | GCP ADC: `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `GOOGLE_APPLICATION_CREDENTIALS` (service-account JSON) | `google-cloud-aiplatform` / `vertexai` (`vertexai.generative_models.GenerativeModel`) — new `_generate_gemini_vertex_response()` | Vertex `CachedContent` (`vertexai.caching`) — **different surface**; add `_ensure_gemini_vertex_cache()` | No-train **plus** enterprise residency/IAM/SLA. |

Notes:
- **Price is not a reason to choose Vertex.** Base per-token rates are identical to the Developer API; Vertex only wins at committed/provisioned volume (CUDs ~20–50%, ~4-month breakeven), which a chat bot won't hit. Vertex's enterprise add-ons can *raise* effective cost. Choose Vertex for residency/SLA/compliance, never for price.
- Refactor: introduce thin `_gemini_generate(...)` and `_gemini_ensure_cache(...)` routers that branch on `backend`. The existing native methods become the `developer_api` branch **unchanged**. Only add the `vertex` branch if/when the lab tier needs it — it can be deferred (see Phase 2, now optional).
- If both backends ever coexist, the bookclub cache lifecycle in `ReadingMaterial` must store a **backend-tagged** handle (content-hash the cache key *and* include the backend) so a Developer-API cache id is never reused against Vertex. (`cachedContents` ≠ `CachedContent`.)

### 4.3 DeepSeek backend toggle (goal 3) — `api | fireworks | self_hosted`
All three targets are OpenAI-compatible, so this stays the cheapest kind of toggle: it changes `base_url`, key, model, caching, and cost mode — no new generator.
Config: `providers.deepseek.backend = "api" | "fireworks" | "self_hosted"` (default `api`).

| Aspect | `api` (current) | **`fireworks`** (new — lab route) | `self_hosted` (max isolation) |
|---|---|---|---|
| `base_url` | `https://api.deepseek.com` | `https://api.fireworks.ai/inference/v1` | `http://localhost:8000/v1` (vLLM) / `:11434/v1` (Ollama) |
| Auth | `DEEPSEEK_API_KEY` | `FIREWORKS_API_KEY` (shared with the §4.1 providers) | dummy/none (local server) |
| Model | `deepseek-chat` / `deepseek-reasoner` | `accounts/fireworks/models/deepseek-v4` (Pro) or `…/deepseek-v4-flash` | local tag / quantized GGUF |
| Jurisdiction | **China** | **US (ZDR)** | **on your hardware (no egress)** |
| Server-side cache | yes (~99% discount) | **yes** (Fireworks 50% cached input) | **none** → `supports_server_cache=false`; cached price = input price |
| Cost tracking | per-token | per-token (Fireworks rates) | mark **"local"** (electricity/amortized HW); zero or per-kWh estimate |
| Web search | Tavily (client-side) | Tavily (unchanged) | Tavily (unchanged) |

**Lab-route guidance:** `fireworks` is the recommended default for the funds-strapped lab — it gets DeepSeek off Chinese infrastructure onto a **US ZDR host with server-side caching and no GPUs to manage**, at V4-Pro `1.74 / 3.48` (or V4-Flash `0.14 / 0.28` for cheap traffic). Reserve `self_hosted` for when even a US ZDR host is too much trust for a specific patentable prompt — that's the only tier where the prompt never leaves the building, at the cost of running the hardware. Full V4 is ~1T MoE (multi-GPU); for single-GPU self-host, use **V4-Flash** or a 4-bit quantized variant. When `self_hosted`, branch `_record_*_usage()` to skip per-token cost and label the turn `local`; `fireworks` and `api` keep per-token accounting.

### 4.4 Platform abstraction + Slack adapter (goal 4)
Introduce a `ChatPlatform` interface; make `ClaudeBot` / `ConversationManager` depend on it instead of `discord.py` directly.

```python
class ChatPlatform(Protocol):
    async def start(self) -> None: ...
    def on_message(self, handler) -> None: ...        # normalized Message events
    def on_reaction(self, handler) -> None: ...        # reaction add/remove -> calibration
    async def send_text(self, channel_id, text, *, thread_id=None) -> str: ...
    async def send_file(self, channel_id, data, filename, *, thread_id=None) -> None: ...
    async def fetch_history(self, channel_id, *, thread_id=None, limit=60) -> list[Message]: ...
    async def typing(self, channel_id) -> None: ...
    def make_thread(self, channel_id, name) -> str: ...
```

A normalized `Message` (platform-agnostic): `id, channel_id, thread_id, author_id, author_name, text, attachments, is_bot, platform`.
- `DiscordAdapter` — wraps the existing `discord.py` logic (extract it; don't rewrite behavior).
- `SlackAdapter` — `slack_bolt` (Bolt for Python) in **Socket Mode** (no public URL needed).

Config: `platform = "discord" | "slack"` (accept a list to run both concurrently).

**Slack specifics**

| Discord concept | Slack mapping |
|---|---|
| `discord.py` event loop | `slack_bolt.App` + `SocketModeHandler` |
| MESSAGE CONTENT INTENT | OAuth scopes: `channels:history`, `groups:history`, `im:history`, `chat:write`, `reactions:read`, `files:read`, `files:write`, `app_mentions:read` |
| message event | `message` event (channels/groups/im) |
| threads | native threads via `thread_ts` (pass as `thread_id`) |
| reactions (👍/👎 → calibration) | `reaction_added` / `reaction_removed` events |
| file attachments | `files_upload_v2` |
| `!claude` / `!opus` prefix commands | slash commands (`/claude`, `/opus`, …) **or** `app_mention` + text parse to preserve the `!`-prefix UX |
| bookclub `!load` etc. | slash commands or message commands |
| LaTeX → PNG | upload via `files_upload_v2` (unchanged pipeline) |
| Markdown output | Slack **mrkdwn** + Block Kit (different from Markdown — convert code blocks, bold, links) |

Tokens: `SLACK_BOT_TOKEN` (`xoxb-…`), `SLACK_APP_TOKEN` (`xapp-…`, Socket Mode). Provide an app manifest with the scopes + event subscriptions + slash commands.

**Persistence migration:** `memories.json` is keyed per-guild. Re-key persistence on `(platform, team_or_guild_id, channel_id)` so Discord and Slack state don't collide and per-workspace memory works on Slack. Write a tiny migration that namespaces existing Discord keys under `discord:<guild>`.

---

## 5. Unified config schema
`config.json` (extends the current `{allowed_channels, default_model}`):

```jsonc
{
  "platform": "discord",                 // "discord" | "slack" | ["discord","slack"]
  "allowed_channels": [123456789012345678],
  "default_model": "auto",
  "providers": {
    "claude":  { "enabled": true, "model": "claude-opus-4-8", "routing_tags": ["code","careful","vision"] },
    "gemini":  {
      "enabled": true,
      "backend": "developer_api",         // "developer_api" (default, no-train when billing enabled) | "vertex" (optional enterprise)
      "model": "gemini-3.1-pro",
      "routing_tags": ["novel","longcontext","vision"],
      "vertex": { "project_env": "GOOGLE_CLOUD_PROJECT", "location": "us-central1" }  // only used when backend=="vertex"
    },
    "deepseek": {
      "enabled": true,
      "backend": "api",                   // "api" (China) | "fireworks" (US/ZDR, lab route) | "self_hosted" (max isolation)
      "model": "deepseek-chat",
      "routing_tags": ["cjk","cheap"],
      "fireworks":    { "model": "accounts/fireworks/models/deepseek-v4" },
      "self_hosted":  { "base_url": "http://localhost:8000/v1", "model": "deepseek-v4-flash" }
    },
    // --- New open-weight providers, all on ONE Fireworks endpoint + ONE key (FIREWORKS_API_KEY) ---
    "mistral": { "enabled": false, "base_url": "https://api.fireworks.ai/inference/v1",
                 "model": "accounts/fireworks/models/mistral-large-3", "routing_tags": [] },
    "qwen":    { "enabled": false, "base_url": "https://api.fireworks.ai/inference/v1",
                 "model": "accounts/fireworks/models/qwen3p6-plus", "routing_tags": [] },
    "glm":     { "enabled": false, "base_url": "https://api.fireworks.ai/inference/v1",
                 "model": "accounts/fireworks/models/glm-5p2", "routing_tags": [] }
  }
}
```

`.env` additions: `FIREWORKS_API_KEY` (covers Mistral + Qwen + GLM + Fireworks-DeepSeek), `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`. **Optional, only if `gemini.backend=="vertex"`:** `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION`, `GOOGLE_APPLICATION_CREDENTIALS`. Existing keys (`GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, Claude, Tavily) unchanged. **No `MISTRAL_API_KEY` / `QWEN_API_KEY` needed** on the consolidated path.

Simulator-mode fields are additive and default off, so the schema above is unchanged for instruct-only deployments: `completions_mode` (bool) per provider, the base sampler knobs (`min_p`, `top_a`, `repetition_penalty`, `stop`), and the optional `persona_name` / per-channel `ambient` toggle. See §9.

---

## 6. Implementation phases
Each phase: implement → test → ship. Don't start the next until the current passes its acceptance tests.

**Phase 0 — Provider registry refactor (no behavior change).**
Build the config loader + registry; route the existing three providers through it via `sdk_type` dispatch. (Optional: split `bot.py` into a `hydra/` package here.)
*Acceptance:* default config reproduces current Discord behavior exactly — routing, caching, cost, bookclub all unchanged.

**Phase 1 — Add Mistral + Qwen + GLM on Fireworks (override-only).**
Three registry entries pointing at the one Fireworks `base_url` + `FIREWORKS_API_KEY`; reachable via `!mistral` / `!qwen` / `!glm` (or slash). Set `cached_input = 0.5 × input`.
*Acceptance:* all three respond when `FIREWORKS_API_KEY` is present; absent key degrades all three gracefully; the original three-way routing is unchanged; `!cost` shows per-provider Fireworks usage with the 50% cache discount applied; **no traffic to DashScope or `api.mistral.ai`** (verify endpoint).

**Phase 2 — Gemini backend (Vertex optional, deferrable).**
Confirm the `developer_api` default works no-train with billing enabled (operator: enable billing on the Google project). **Only if the lab tier needs residency/SLA:** add the `vertex` branch for generate + caching + grounding; backend-tag cache handles.
*Acceptance:* `developer_api` unchanged and no-train with billing on. *(If Vertex built:)* `vertex` generates, caches a pinned bookclub work via `CachedContent`, returns grounded citations, and never reuses a Developer-API cache id.

**Phase 3 — DeepSeek backend toggle (`api | fireworks | self_hosted`).**
Add the `fireworks` backend (base_url/key/model swap to the shared Fireworks endpoint; keep server-cache accounting at 50%) and the `self_hosted` backend (local base_url/key/model; disable server-cache accounting; cost mode `local`).
*Acceptance:* `fireworks` responds against Fireworks DeepSeek with US/ZDR and cached-input discount in `!cost`; `self_hosted` responds against a local vLLM/Ollama server and shows the turn as `local` (no per-token charge); Tavily search works in all three; `api` mode unchanged.

**Phase 4 — Platform abstraction + Slack.**
Extract `DiscordAdapter`; add `ChatPlatform`; build `SlackAdapter` (Socket Mode); re-key persistence; provide Slack app manifest.
*Acceptance:* same bot runs on Slack with threads (`thread_ts`), 👍/👎 calibration via `reaction_added`, file uploads, `/claude`-style commands, and full bookclub flow; Discord still works from the same codebase; memory is per-workspace and doesn't collide across platforms.

**Phase 5 — (Optional, lab variant) Hermes delegation bridge.**
Stand up a co-located Hermes agent pointed at the hardened endpoints (Fireworks US/ZDR, self-hosted DeepSeek, or Vertex); add the delegation hand-off (command → local Hermes → result back to channel) and, optionally, cron delivery. Do not merge codebases. See §10.
*Acceptance:* a `/research`-class command runs a multi-step Hermes task against hardened/no-egress models and posts the result in-channel with **no sensitive prompt leaving the lab**; a scheduled Hermes job can post to the lab channel; Hydra still runs fully with Hermes absent.

**Phase 6 — (Optional) Ephemeral big-model co-op.**
Build the lifecycle controller + `ephemeral` provider kind + a completions/transcript mode (borrowed from Nexari) + session metering. See §11. Independent of Phases 1–5; only needed if you want a model nobody hosts per-token (e.g. 405B base).
*Acceptance:* `!session start 405b-base` provisions an 8×H100 node, loads the model, and flips the provider WARM; `!base <prompt>` returns a completion via the transcript path with base sampler params; `!session cost` shows live box-hours and the per-participant split; a hard idle timeout **and** a provider-side TTL each independently tear the box down; with no session active, `!base` reports the model cold and the rest of Hydra is unaffected.

**Phase 7 — (Optional, but higher-want than Hermes) Simulator mode.**
Add `completions_mode` + the transcript formatter + base sampler knobs (§9), sharing the existing search and cost-tracking layers. Independent of Phases 4–6; needs only a completions-capable endpoint (a small self-hosted base model is enough — no 405B required). Optionally layer ambient turn-taking and webhook personas later.
*Acceptance:* with `completions_mode=true` on any base/completions endpoint, `!sim <prompt>` (or a designated channel) returns a transcript continuation with base sampler params; the turn is logged to `!cost` (tokens + carbon) and can be fed web-search context; instruct providers and `_select_model` are unchanged; ambient turn-taking and personas remain off unless explicitly enabled.

---

## 7. Risks & gotchas
- **Gemini billing ≠ code.** The only thing standing between "trains on your prompts" and "doesn't" on the Developer API is **billing enabled on the Google project**. Don't model this as a code branch; document it as an operator prerequisite. If the bill lapses, the account can drop to the unbilled (training) tier — keep billing current.
- **Fireworks is prepaid.** As of **July 1, 2026, Fireworks self-serve accounts are prepaid** — purchase credits up front; usage draws down the balance. **Set auto-reload** (e.g., top up $10 when balance drops below $5) or the bot's Fireworks calls fail at $0 balance. This doubles as the lab's spend cap.
- **Fireworks model slugs.** Use `accounts/fireworks/models/<id>`; the example ids here (`qwen3p6-plus`, `glm-5p2`, `mistral-large-3`, `deepseek-v4`) **must be verified against the live Fireworks model library** — slugs change as versions ship.
- **Serverless price premium.** Fireworks serverless can run ~2–4× a model creator's own direct API price for some models. That's the US-residency + ZDR + one-bill premium; accept it for the hardened/lab path, or use a direct endpoint override for non-sensitive, cost-critical traffic.
- **Vertex is optional** — only build/configure it for residency/SLA/compliance. Its auth is ADC (service-account JSON via `GOOGLE_APPLICATION_CREDENTIALS`, or workload identity), the most common failure point, and its caching API (`vertexai.caching.CachedContent`) differs from `cachedContents`. Don't take on that surface unless the lab tier requires it.
- **Self-hosted DeepSeek loses server-side caching.** Don't let cost code divide by zero or report fake savings; treat `self_hosted` as `local`. `fireworks` *keeps* server caching (50%), so only `self_hosted` flips `supports_server_cache=false`.
- **Self-hosted hardware:** full V4 is ~1T params. Default the self-hosted example to V4-Flash or a quantized variant; note VRAM expectations.
- **Slack ≠ Discord formatting:** mrkdwn + Block Kit. Audit every place the bot emits Markdown (code fences, bold, links, the `!help` tables). LaTeX→PNG is fine (image upload).
- **Slack Socket Mode rate limits + event de-dupe:** Slack retries events; de-duplicate on `event_id`/`client_msg_id` so the bot doesn't double-respond.
- **Persistence collision:** re-key memory on `(platform, team/guild, channel)` before going multi-platform, or Discord and Slack will clobber each other.
- **Graceful degradation** must survive the refactor: a missing key for any provider disables only that provider. Note one key (`FIREWORKS_API_KEY`) now gates Mistral + Qwen + GLM + Fireworks-DeepSeek together — its absence should disable exactly those, not the originals.

---

## 8. Quick reference — file/method touch list
- Config loader + `ProviderRegistry`: new (Phase 0).
- `_generate_response()`: dispatch on `sdk_type` (Phase 0); **no new branch** for Mistral/Qwen/GLM or Fireworks-DeepSeek — they're `openai_compatible` registry entries (Phases 1, 3).
- `_generate_gemini_native_response()`: stays the `developer_api` branch unchanged; wrap under `_gemini_generate()` router. Add `_generate_gemini_vertex_response()` + `_ensure_gemini_vertex_cache()` **only if** Vertex is built (Phase 2, optional).
- `_generate_openai_compatible_response()`: unchanged logic; receives Fireworks (Mistral/Qwen/GLM/DeepSeek) and self-hosted DeepSeek base_url/model from registry (Phases 1, 3).
- `_record_*_usage()`: keep per-token for `api`/`fireworks`; add `local` cost mode for self-hosted DeepSeek (Phase 3). Apply Fireworks 50% cache discount.
- `ConversationManager` + `ClaudeBot`: depend on `ChatPlatform`; extract `DiscordAdapter`; add `SlackAdapter` (Phase 4).
- Persistence keys in `memories.json`: namespace by `(platform, team/guild, channel)` (Phase 4).
- Simulator mode (Phase 7): add `completions_mode` (+ base sampler fields) to `ModelProvider`; new `_generate_simulator_response()`, `_format_transcript()`, `_parse_transcript_turn()`; one dispatch branch in `_generate_response()`. Reuses `_maybe_search` / search backends and `_record_*_usage` unchanged. See §9.

---

## 9. Simulator mode — a generation mode (priority over Hermes; works with or without 405B)
This is a **generation mode**, not a second bot, and it's the feature you want most after Slack. Hydra has one mode today: instruct/chat. Simulator mode adds a second — **transcript completion** — behind the same dispatch, so the bot can talk to *base* models (and instruct models prompted in completion style) while keeping everything that makes Hydra Hydra. Explicitly **unlike Nexari, it is not tool-less or cost-blind**: a simulator turn still logs tokens (and carbon) and can still be fed web search.

**It does not require 405B.** Simulator mode needs only a **completions-capable endpoint**, which can be (a) a small base model self-hosted on a gaming rig or the CRC (Ministral/Magistral-class, or any GGUF base), (b) an instruct model that also exposes `/completions` or is prompted in completion style, or (c) the rented 405B box from §11. You can **build, ship, and test simulator mode today against a cheap small base model** with zero dependency on the expensive ephemeral hosting — 405B is the *maximal* backend, not a prerequisite.

### 9.1 Two modes behind one dispatch
Add a single flag and one branch; the instruct path is untouched.
- `ModelProvider.completions_mode: bool = False` — `False` → current chat path (default); `True` → transcript path (this section). Mirrors Nexari's `instruct_tuned`, inverted.
- One branch in `_generate_response()` beside the `sdk_type` cases: if `provider.completions_mode`, call `_generate_simulator_response(...)`; otherwise the existing dispatch.
- Both modes return the **same tuple** `(text, reactions, reasoning)` so message-splitting, posting, and LaTeX→PNG downstream are unchanged.

### 9.2 What's shared — do NOT fork these
The two cross-cutting layers stay identical across both modes:
- **Web search / grounding** (Tavily, native Anthropic, Google). The only difference is *where* the result lands: instruct appends a message/tool result; simulator folds the retrieved snippets into the **transcript/system preamble**. Same `SearchBackend` plumbing, same `_maybe_search` call.
- **Per-token + carbon cost tracking.** A completions response still carries `.usage`, so `_record_*_usage` (and your `grid_gco2_per_kwh` accounting) works unchanged. A base/sim turn shows up in `!cost` exactly like a chat turn.

### 9.3 Transcript-completion mechanics (lift Nexari's `irc` path)
A base model doesn't take chat messages; it continues a transcript. Add a thin completions path:
- Call the provider's `/completions` endpoint, not `/chat/completions`. **Because Hydra already holds OpenAI-compatible `client` objects, this is just `client.completions.create(...)` — no raw `aiohttp` needed** (Nexari used `aiohttp` only because it had no client).
- Format channel history as a transcript (IRC-/script-style log) in `_format_transcript()`, optionally with a `force_speaker`, the search preamble, and any pinned bookclub material; continue it; cut on `stop` sequences (e.g. `["\n\n\n"]` or a speaker-tag regex) in `_parse_transcript_turn()`.
- Expose the base-model sampler knobs the instruct path ignores: `temperature`, `top_p`, `top_k`, `min_p`, `top_a`, `repetition_penalty`, `frequency/presence_penalty`, `stop`.

This is exactly what `au-to-pi-lot/nexari` does for 405B base via its `irc` formatter and a raw `/completions` call — port that formatter and the sampler plumbing rather than reinventing them.

### 9.4 Optional layers (each independently toggleable, off by default)
The spectrum — adopt only as far as you want; the cost/search/persistence spine is identical the whole way down:
1. **Transcript completion** (9.3) — talk to a base model, still cost-tracked + searchable. *This is the whole feature for most uses.*
2. **Ambient turn-taking** — a designated sim model predicts the *next speaker* (Nexari's `get_next_participant`) instead of `_select_model`'s argmax. Opt-in **per channel**. Rate-limit it (Nexari's per-channel queue + lock) so it can't loop.
3. **Webhook personas** — post under per-agent name/avatar (Nexari's webhook output) instead of the single MAGI/ISAIC identity.

So: **Hydra-as-is** (instruct + argmax + single identity) → **+ transcript mode** (base models, cost-tracked + searchable) → **+ ambient + personas** (full Nexari-style simulator). Build only the first rung to get ~95% of the value; the rest are there if you want the loom.

### 9.5 Phase 7 acceptance (see also §6)
With `completions_mode=true` on any completions endpoint (a small self-hosted base model suffices — no 405B), `!sim <prompt>` (or a designated channel) returns a transcript continuation using the base sampler params; the turn logs to `!cost` with tokens **and** carbon and can be fed web-search context; instruct providers and `_select_model` are unchanged; ambient turn-taking and personas stay off unless explicitly enabled.

---

## 10. Optional — Hermes Agent integration (lab research variant)
§§1–8 turn Hydra into a configurable, hardenable multi-provider chat bot. This section is **optional and lab-specific**: it adds a research-automation layer for the funds-strapped, sensitive-data lab deployment (hardened / no-train models, potentially patentable work). Skip it for the default chat deployment; adopt it when researchers need long-horizon workflows, scheduled jobs, and reusable skills rather than turn-by-turn chat.

[Hermes Agent](https://hermes-agent.org/) is Nous Research's open-source (MIT), model-agnostic agent harness — not a coding CLI but a long-running personal-assistant/automation harness with a built-in self-improving skill loop. It is adopted **alongside** Hydra, never merged into it.

### 10.1 Why Hermes for the lab (capability → need)
| Hermes capability | Lab need it serves |
|---|---|
| Local-first + model-agnostic (any OpenAI-compatible endpoint) | Point it at the **same hardened endpoints** from the Hydra registry — Fireworks (US/ZDR), self-hosted DeepSeek, or Vertex — so sensitive prompts stay on no-train/no-egress infra |
| Self-improving skills, auto-created from successful trajectories (agentskills.io-portable) | Recurring workflows (lit extraction, analysis pipelines) crystallize into shareable team skills — less repeated prompting |
| Persistent cross-session memory | Project/domain context (BandR conventions, datasets, target endpoints) persists across sessions |
| Scheduled automations (cron) with delivery to any platform | Nightly literature scans, periodic data QA, scheduled report posts into the lab channel |
| Subagent delegation + parallel workstreams | Native version of the `!research` fan-out: lit-search ∥ analysis ∥ drafting |
| Programmatic tool calling (`execute_code` collapses pipelines into single inference calls) | Fewer round-trips = fewer tokens = lower spend (matters on a strapped budget) |
| MCP support | Connect lab data sources / tools |
| Trajectory export + RL-data generation | Optional bridge if the lab ever fine-tunes a domain model (e.g., a neuro-literature model) |

### 10.2 Integration architecture
**Pattern: Hydra stays the chat front-end + provider router (§§1–8); Hermes is a co-located agentic engine; a thin bridge delegates heavy/long-horizon/scheduled tasks to Hermes and surfaces results back in the channel.** Do **not** merge the codebases.

Three seams (adopt per need):
1. **Delegation hand-off (primary).** Add a command tier — route the existing `!research`, or add `/task` / `/deep` — that hands the prompt to the local Hermes agent (via its CLI / socket / local API), streams the trajectory and final result back to the channel through Hydra's existing `send_text` / `send_file` path. Hermes runs model-agnostic against the **same registry endpoints** (Fireworks US/ZDR for Qwen/Mistral/GLM/DeepSeek, self-hosted DeepSeek, or Vertex). Hydra stays the I/O surface; Hermes does the multi-step work.
2. **Scheduled delivery (cron).** Use Hermes's built-in cron + platform delivery to run scheduled lab jobs and post results directly into the lab channel. Register Hydra's channel as the delivery target (or relay through it).
3. **Shared skills/memory (optional).** Adopt the agentskills.io skill format so skills are portable between Hermes and any Hydra-side tooling; key Hermes memory per project so BandR context persists.

### 10.3 Boundaries & constraints
- **Two systems, one bridge.** Hydra = front-end/router (the §§1–8 work). Hermes = separate co-located service. The integration is a thin delegation/delivery bridge, not a fork or a rewrite of either.
- **Provider-egress discipline (critical for sensitive work).** Hermes defaults can route to Nous Portal / OpenRouter. For the lab variant, **force Hermes onto the same hardened endpoints as the Hydra registry** — Fireworks (US, ZDR), self-hosted DeepSeek (no egress), or Vertex (residency) — and gate any genuinely patentable prompt to **self-hosted only**, since that's the one tier where nothing leaves the building. Treat Hermes's endpoint config as part of the data-security surface, not a convenience setting. Note: Fireworks US/ZDR is the right default for *most* lab work (no hardware, US, no-retention); reserve self-hosted for the prompts you can't let touch any third party at all.
- **Cost containment.** The self-improvement loop and parallel subagents can burn tokens. Cap subagent parallelism, keep the cheap-model tier (Fireworks DeepSeek V4-Flash, Qwen Plus) for routine sub-tasks, lean on `execute_code` pipeline-collapsing to cut round-trips, and remember Fireworks prepaid auto-reload is your hard spend cap.
- **Optional and isolated.** None of this touches the default (chat-only) Hydra. It is the research-variant add-on; the bot must run fully with Hermes absent.

### 10.4 Phase 5 acceptance (see also §6)
A `/research`-class command runs a multi-step Hermes task against hardened endpoints (Fireworks US/ZDR or self-hosted), streams progress, and posts the result in-channel; **patentable prompts egress to no third party** (self-hosted tier); a scheduled Hermes job can post into the lab channel; the default Hydra deployment still runs with Hermes absent.

---

## 11. Optional — ephemeral big-model hosting (the "405B base co-op")
§§1–10 assume always-on endpoints (cloud APIs, Fireworks, or a persistent self-hosted server). This section is the opposite pattern: **spin a rented multi-GPU box up on demand or on a schedule, serve one big model nobody hosts per-token anymore (e.g. Llama-3.1-405B *base*), expose it as a Hydra provider, meter it, and split the hourly cost across whoever's in the session.** It's the "weekend hosting co-op" for models the market has abandoned. Entirely optional; skip it unless you specifically want a model only a rented cluster can run.

This section is purely the **rental lifecycle + cost-splitting**; the *how do I talk to a base model* mechanics live in **§9 (simulator mode)** — the rented box just serves a base model that simulator mode drives. So §9 stands alone without this section, and this section depends on §9. The other thing that makes this its own section (rather than a backend toggle) is that the box has a **lifecycle**: cold → warm → torn down, which the always-on registry doesn't model.

### 11.1 Components (keep the controller out of `bot.py`)
- **Lifecycle controller** — a separate small service that owns the rented box: provisions it, health-checks it, tears it down, and exposes one stable gateway URL to Hydra. Hydra never talks to the cloud provider directly; it talks to the controller's gateway, which 503s when cold and proxies to vLLM when warm.
- **Provider kind `ephemeral`** in the registry (§4.1) — `sdk_type: "openai_compatible"`, `completions_mode: true` (it serves a base model — see §9), `base_url` = controller gateway, plus lifecycle metadata (`gpu_spec`, `idle_timeout`, `schedule`, `billing_mode`). Default **override-only** (`routing_tags: []`), reachable by an explicit command (`!base` / `!405`), never in the argmax — it isn't always up, and you don't want the auto-router picking a cold box.

### 11.2 Lifecycle state machine
`COLD → SPINNING_UP → WARM → DRAINING → COLD`
- **Triggers up:** on-demand (`!session start 405b-base`) or cron (`schedule: "Sat 18:00–23:00 America/New_York"`).
- **SPINNING_UP:** controller calls the GPU provider's API (RunPod / Lambda / CoreWeave) to launch a node from a prebuilt vLLM image, then polls `/health` until the OpenAI-compatible endpoint answers. FP8 405B fits a single 8×H100 node (640 GB); BF16 wants ~2× that. Cold start is **minutes**, dominated by pulling ~400–800 GB of weights — pre-bake them onto a network volume or image snapshot so you're not re-downloading every session.
- **WARM:** gateway proxies completions to vLLM; usage is metered (11.3).
- **DRAINING → COLD:** torn down by a hard idle timeout, a max-session cap, **or** the cron window closing. See the dead-man's-switch in 11.4 — that's the part that protects your wallet.

### 11.3 Metering & cost-splitting
The controller keeps a per-session ledger with two numbers: **box-time** (wall-clock the node is WARM × $/hr of the instance) and **per-user usage** (tokens or request count, attributed by Discord user id). Pick a `billing_mode`:
- `per_hour_split` — divide the session's box-hours across participants, evenly or weighted by usage share. Simplest; matches the "co-op" framing.
- `per_token_markup` — charge a per-token rate set high enough to cover box-hours + expected idle; you (or the lab) front the cluster and reconcile later. Smoother for users, riskier for whoever fronts it.

Surface it in `!session cost` (live: box-hours so far, $/participant, idle time) and a post-session summary. The per-provider token accounting in `ModelProvider` is the wrong tool here — box-time isn't per-token — so give `ephemeral` a `cost_mode: "session"` that bypasses token pricing and reads the controller's ledger.

### 11.4 Wallet guardrails (do these first)
The thread's whole point is that the economics are unforgiving; a forgotten 8×H100 at ~$25/hr is ~$600/day. Non-negotiable:
- **Hard idle timeout** — N minutes with no requests → auto-drain. The single most important setting.
- **Max session duration** — absolute cap regardless of activity.
- **Provider-side TTL / dead-man's-switch** — set the instance to self-terminate on a TTL *at the cloud provider*, so a crashed controller can't leak a running box. Belt and suspenders.
- **Minimum-participants gate (optional)** — don't spin up (or drain early) if fewer than K people are actually using it, so one person doesn't silently eat a full cluster.
- **Spend ceiling** — per-session and per-month dollar caps the controller refuses to exceed.

### 11.5 Security note
The rented box is single-tenant (better than shared serverless), but it's still third-party cloud hardware — same tier as the Fireworks / GPU-rental options in §4 and §10, *not* the airgapped-CRC tier. Fine for base-model creative play; for genuinely patentable prompts, stay on self-hosted / CRC.
