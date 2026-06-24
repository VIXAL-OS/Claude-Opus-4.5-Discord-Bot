# Hydra Restructure — Architecture & Implementation Spec

> **Archived reference (Rev 2).** This is the source-of-truth design spec for the multi-provider /
> Slack / hardening restructure. For **current status and what's left to build**, see
> [`../CLAUDE.md`](../CLAUDE.md) — it tracks which phases are done, deviations, and remaining work.

**Audience:** Claude Code (or any agent) executing this refactor.
**Source repo:** `VIXAL-OS/Opus-Deipseek` — a single-file (`bot.py`, ~4,600 lines) multi-model Discord bot (Claude Opus 4.8 + DeepSeek V4-Pro + Gemini 3.1 Pro), MAGI-themed, with heuristic routing, two-tier memory, prompt caching, bookclub mode, and cost tracking.
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
5. **(Optional — lab research variant) Hermes Agent layer** — for the funds-strapped, sensitive-data lab deployment, delegate long-horizon and scheduled research workflows to a co-located, model-agnostic [Hermes Agent](https://hermes-agent.org/) instance pointed at the **same** hardened endpoints (Fireworks US/ZDR, self-hosted DeepSeek, or Vertex). Adds self-improving skills, persistent project memory, scheduled automations, and parallel subagents. Entirely skippable for the default chat-only deployment. See §9 and Phase 5.

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
Stand up a co-located Hermes agent pointed at the hardened endpoints (Fireworks US/ZDR, self-hosted DeepSeek, or Vertex); add the delegation hand-off (command → local Hermes → result back to channel) and, optionally, cron delivery. Do not merge codebases. See §9.
*Acceptance:* a `/research`-class command runs a multi-step Hermes task against hardened/no-egress models and posts the result in-channel with **no sensitive prompt leaving the lab**; a scheduled Hermes job can post to the lab channel; Hydra still runs fully with Hermes absent.

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

---

## 9. Optional — Hermes Agent integration (lab research variant)
§§1–8 turn Hydra into a configurable, hardenable multi-provider chat bot. This section is **optional and lab-specific**: it adds a research-automation layer for the funds-strapped, sensitive-data lab deployment (hardened / no-train models, potentially patentable work). Skip it for the default chat deployment; adopt it when researchers need long-horizon workflows, scheduled jobs, and reusable skills rather than turn-by-turn chat.

[Hermes Agent](https://hermes-agent.org/) is Nous Research's open-source (MIT), model-agnostic agent harness — not a coding CLI but a long-running personal-assistant/automation harness with a built-in self-improving skill loop. It is adopted **alongside** Hydra, never merged into it.

### 9.1 Why Hermes for the lab (capability → need)
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

### 9.2 Integration architecture
**Pattern: Hydra stays the chat front-end + provider router (§§1–8); Hermes is a co-located agentic engine; a thin bridge delegates heavy/long-horizon/scheduled tasks to Hermes and surfaces results back in the channel.** Do **not** merge the codebases.

Three seams (adopt per need):
1. **Delegation hand-off (primary).** Add a command tier — route the existing `!research`, or add `/task` / `/deep` — that hands the prompt to the local Hermes agent (via its CLI / socket / local API), streams the trajectory and final result back to the channel through Hydra's existing `send_text` / `send_file` path. Hermes runs model-agnostic against the **same registry endpoints** (Fireworks US/ZDR for Qwen/Mistral/GLM/DeepSeek, self-hosted DeepSeek, or Vertex). Hydra stays the I/O surface; Hermes does the multi-step work.
2. **Scheduled delivery (cron).** Use Hermes's built-in cron + platform delivery to run scheduled lab jobs and post results directly into the lab channel. Register Hydra's channel as the delivery target (or relay through it).
3. **Shared skills/memory (optional).** Adopt the agentskills.io skill format so skills are portable between Hermes and any Hydra-side tooling; key Hermes memory per project so BandR context persists.

### 9.3 Boundaries & constraints
- **Two systems, one bridge.** Hydra = front-end/router (the §§1–8 work). Hermes = separate co-located service. The integration is a thin delegation/delivery bridge, not a fork or a rewrite of either.
- **Provider-egress discipline (critical for sensitive work).** Hermes defaults can route to Nous Portal / OpenRouter. For the lab variant, **force Hermes onto the same hardened endpoints as the Hydra registry** — Fireworks (US, ZDR), self-hosted DeepSeek (no egress), or Vertex (residency) — and gate any genuinely patentable prompt to **self-hosted only**, since that's the one tier where nothing leaves the building. Treat Hermes's endpoint config as part of the data-security surface, not a convenience setting. Note: Fireworks US/ZDR is the right default for *most* lab work (no hardware, US, no-retention); reserve self-hosted for the prompts you can't let touch any third party at all.
- **Cost containment.** The self-improvement loop and parallel subagents can burn tokens. Cap subagent parallelism, keep the cheap-model tier (Fireworks DeepSeek V4-Flash, Qwen Plus) for routine sub-tasks, lean on `execute_code` pipeline-collapsing to cut round-trips, and remember Fireworks prepaid auto-reload is your hard spend cap.
- **Optional and isolated.** None of this touches the default (chat-only) Hydra. It is the research-variant add-on; the bot must run fully with Hermes absent.

### 9.4 Phase 5 acceptance (see also §6)
A `/research`-class command runs a multi-step Hermes task against hardened endpoints (Fireworks US/ZDR or self-hosted), streams progress, and posts the result in-channel; **patentable prompts egress to no third party** (self-hosted tier); a scheduled Hermes job can post into the lab channel; the default Hydra deployment still runs with Hermes absent.
