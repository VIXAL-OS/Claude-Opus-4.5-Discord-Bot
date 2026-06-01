# Hydra Discord Bot (Opus-Deipseek)

A multi-model Discord bot powered by **Claude Opus 4.8**, **DeepSeek V4-Pro**, and **Gemini 3.1 Pro** — three frontier models sharing one bot with smart routing, shared memory, native web search/grounding, and bookclub mode for discussing long texts (fics, papers, contracts) across all three.

Affectionately maps to the EVA *MAGI* trinity:
- **Claude / Balthasar** — careful, thorough, vision, native Anthropic web search, multi-tool orchestration
- **DeepSeek / Melchior** — fast, cheap, CJK-strong, Tavily-backed search, automatic server-side prompt caching
- **Gemini / Caspar** — abstract reasoning, long-context synthesis, vision, native Google Search grounding

## Features

- 🐉 **Multi-model (Hydra)** — Claude + DeepSeek + Gemini with automatic routing
- 📚 **Bookclub mode** — pin long texts to a channel; discuss across all three models with per-thread chapter scoping
- 🧵 **Thread-based conversations** — keeps channels clean
- 📷 **Image understanding** — Claude and Gemini both see images
- 🔗 **URL reading** — paste a link and the bot fetches the page contents (via Tavily extract)
- 🔍 **Web search** — Claude and Gemini ground natively; DeepSeek uses Tavily; all three return source citations
- 💾 **Prompt caching** — Claude ephemeral cache, Gemini explicit `cachedContent`, DeepSeek auto server-side; per-provider hit-rate reporting
- 🧮 **LaTeX rendering** — `$$...$$` and `$...$` blocks render to PNG attachments (source kept inline)
- 🧠 **Two-tier memory** — working notes (auto-decay) + long-term (permanent)
- 😀 **Emoji reactions** — bot reacts to your messages
- 📎 **File attachments** — long code becomes downloadable files
- 💰 **Cache-aware cost tracking** — per-model usage, cache hit rates, true cost breakdown
- 🀄 **Chinese language specialist** — DeepSeek translates and teaches CJK

## The Hydra System

Three AI models share one Discord bot, taking turns "fronting" like a plural system:

```
       User message arrives
              ↓
         [Router] ← heuristic scoring, no LLM call
       /    |    \
 [Claude] [Gemini] [DeepSeek]
    ↓        ↓         ↓
**[Claude]** **[Gemini]** **[Deepseek]**
```

**How routing works (three-way argmax with cost tiebreaks):**
- Images → Claude or Gemini (DeepSeek is text-only)
- CJK text → DeepSeek (deeper Chinese training data)
- Novel reasoning / abstract patterns / long-context synthesis → Gemini (ARC-AGI-2 strength)
- Complex code / multi-tool orchestration / careful epistemics → Claude
- Short factual / casual chat → DeepSeek (50-100× cheaper)
- Ties → cheaper model wins (DeepSeek > Gemini > Claude on cost)
- Users override with `!claude` / `!opus` / `!balthasar`, `!deepseek` / `!melchior`, `!gemini` / `!caspar`
- Per-channel preferences via `!prefer`

**Models know who they are** — each gets a tailored system prompt with its identity, capabilities, why it was selected, and can see labeled messages from the other two in conversation history.

## Commands

### General
| Command | Description |
|---------|-------------|
| `!help` | Show all commands |
| `!context` | Show current context size and cost estimate |
| `!cost` | Per-model usage, cost, and cache hit rate |
| `!memories` | List all memories (both types) |
| `!threads` | Show other recent threads |
| `!search <query>` | Web search with citations |

### Multi-model
| Command | Description |
|---------|-------------|
| `!claude <msg>` / `!opus <msg>` / `!balthasar <msg>` | Force Claude to respond |
| `!deepseek <msg>` / `!melchior <msg>` | Force DeepSeek to respond |
| `!gemini <msg>` / `!caspar <msg>` | Force Gemini to respond |
| `!think <msg>` | Use extended thinking (deeper reasoning, slower & costlier) |
| `!think:<level> <msg>` | Force a specific Opus effort level (`low`/`medium`/`high`/`xhigh`/`max`) |
| `!models` | Show available models and usage stats |
| `!prefer [claude\|deepseek\|gemini\|auto]` | Set model preference for this channel |
| `!calibration` | Show confidence calibration stats |

Prefixes can stack in any order: `!think !claude <msg>` forces Claude with thinking on. Thinking auto-enables on `!claude`/`!opus` when prompts look hard (cues like "derive", "why does X", "step by step", LaTeX, large code blocks, stack traces) and picks an Opus effort level (`high`/`xhigh`/`max`) from the same signals. For DeepSeek and Gemini, thinking is opt-in only via `!think`. Reasoning content is cached per Discord message so multi-turn thinking conversations work across providers.

React with 👍❤️🔥✅😂💖💯 (positive) or 👎❌😕 (negative) to bot responses to improve model selection over time.

### Memory
| Command | Description |
|---------|-------------|
| `!remember <key> <value>` | Store a permanent memory |
| `!forget <key>` | Remove a memory |
| `!keep <key>` | Promote a working note to permanent |
| `!summarize <key>` | Auto-summarize thread to memory |
| `!summarize <key> <text>` | Save your own summary |

### Bookclub Mode (pinned long texts)
| Command | Description |
|---------|-------------|
| `!load <ao3-url>` | Fetch an AO3 work and pin it to this channel |
| `!load_text [title]` | (with `.txt`/`.html`/`.md` attachment) load from a local file — works when AO3 is shields-up |
| `!unload` | Drop the loaded work |
| `!reading` | Show what's currently loaded |
| `!chapters` | Show chapter TOC with per-chapter token counts |
| `!scope chapter N` / `!scope chapters N-M` | (in a thread) restrict to a chapter range — spoiler-safe + much cheaper per turn |
| `!unscope` | (in a thread) drop the scope and use the parent channel's full work |

See the [Bookclub Mode](#bookclub-mode) section below for the full workflow.

## The Memory System

Two types, like an actual brain:

**Working Memory** (auto-managed) — Claude/DeepSeek/Gemini automatically jot down notes during conversation. Notes fade after ~48h if not referenced, stick around longer if relevant. Max 10 notes. See them with `!memories`, promote with `!keep`.

**Long-Term Memory** (permanent) — explicit facts created with `!remember`. Never decay until `!forget`. Shared across all threads in the server.

Freshness indicators: 🟢 Fresh (>70%) · 🟡 Fading (30-70%) · 🔴 Almost gone (<30%)

## Web Search

All three models can search the web:

- **Claude** — built-in `web_search_20250305` tool; can search organically during conversation or via `!search`
- **Gemini** — native `google_search` grounding via the native Gemini API; returns structured `groundingMetadata` with source citations
- **DeepSeek** — Tavily function calling; requires `TAVILY_API_KEY`

`!search` routes to whichever model is preferred for the channel (Claude first if available, then Gemini-native, then DeepSeek-via-Tavily). All paths render a "🔍 Sources" embed with cited URLs.

## Bookclub Mode

Pin a long text (fic, paper, contract) to a channel; all three models discuss it across turns with prompt caching keeping cost manageable.

### Workflow

1. **Load the whole work** in a channel:
   ```
   !load https://archiveofourown.org/works/12345678
   ```
   Or with a local file:
   ```
   !load_text Almost Nowhere
   [attach Almost_Nowhere.html or .txt]
   ```

2. **Browse the structure**:
   ```
   !chapters
   ```
   Shows the TOC with per-chapter token counts. The HTML loader detects chapter headings (matching `Chapter N`, `Prologue`, `Epilogue`, `Interlude N`, `Part N`) and reports each chapter's size.

3. **Create a thread per chapter for focused discussion**:
   ```
   [Create thread: "Chapter 1 discussion"]
   !scope chapter 1
   ```
   Models in that thread only see Chapter 1 — no spoilers from later chapters, and per-turn cost drops by ~95% since the scoped slice is much smaller than the full work.

4. **Discuss across models**:
   ```
   !gemini What's your read on the notebook structure?
   !claude What do you think?
   !deepseek Quick take on the prose?
   ```

### How caching works under the hood

Each provider handles prompt caching differently — the bot abstracts over all three:

- **Claude**: `cache_control: {"type": "ephemeral"}` on the reading-material system block. ~10% pricing on cache hits ($1.50/M vs $15/M input).
- **Gemini**: Explicit `cachedContents` created via aiohttp to the native API at first turn; subsequent turns reference it via `cachedContent` (the OpenAI shim doesn't support this, so we route Gemini bookclub chat through the native endpoint). ~25% pricing on cache hits.
- **DeepSeek**: Automatic server-side prefix caching — no client flags required. ~99% pricing discount on cache hits ($0.003625/M vs $0.435/M).

Each scope (full work, chapter 1, chapters 1-3, etc.) gets its own cache automatically (content-hashed). Switching between scoped threads doesn't invalidate other threads' caches.

### When AO3 is shields-up

AO3 sheds anonymous traffic under load with HTTP 403 "Shields are up!" — affects all anonymous requests site-wide. Options when this happens:

1. **Wait** — usually resolves in hours
2. **Set `AO3_COOKIE`** in `.env` with your logged-in `_otwarchive_session` cookie value — bypasses shields-up AND unlocks registered-only works
3. **Use `!load_text`** with a manually-downloaded `.txt`/`.html` of the work (AO3's "Download" button on the work page gives you HTML/EPUB/PDF/MOBI)

## Setup

### 1. Create Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. New Application → Bot section → Reset Token (save it)
3. Enable **MESSAGE CONTENT INTENT** under Privileged Gateway Intents
4. OAuth2 → URL Generator → Scopes: `bot` → Permissions: Send Messages, Read Message History, Create Public Threads, Send Messages in Threads, Add Reactions, Attach Files, Embed Links
5. Open generated URL to invite bot

### 2. Get API Keys

- **Anthropic** ([console.anthropic.com](https://console.anthropic.com/)) — required or optional
- **DeepSeek** ([platform.deepseek.com](https://platform.deepseek.com/)) — required or optional
- **Gemini** ([aistudio.google.com/apikey](https://aistudio.google.com/apikey)) — required or optional; AI Studio key, not Vertex
- **Tavily** ([tavily.com](https://tavily.com/)) — optional, enables DeepSeek web search (free 1,000 searches/month)
- **AO3 cookie** — optional, bypasses shields-up for bookclub mode (see [Bookclub Mode](#bookclub-mode))

At least one of Anthropic / DeepSeek / Gemini API keys is required.

### 3. Configure

```bash
pip install -r requirements.txt

cp .env.example .env       # Edit with your API keys
cp config.example.json config.json  # Edit with your channel IDs
```

**.env:**
```
DISCORD_TOKEN=your_discord_token
ANTHROPIC_API_KEY=your_anthropic_key      # Optional if Gemini or DeepSeek only
DEEPSEEK_API_KEY=your_deepseek_key        # Optional
GEMINI_API_KEY=your_gemini_key            # Optional
TAVILY_API_KEY=your_tavily_key            # Optional
AO3_COOKIE=                               # Optional, for bookclub mode
```

**config.json:**
```json
{
  "allowed_channels": [123456789012345678],
  "default_model": "auto"
}
```

### 4. Run

```bash
python bot.py
```

The bot gracefully degrades — runs with any subset of {Claude, DeepSeek, Gemini} depending on which API keys are present.

## Cost Comparison

| Model | Input | Cached input | Output | Typical chat | Bookclub (320k cached) |
|-------|-------|--------------|--------|--------------|------------------------|
| Claude Opus 4.8 | $5/M | $0.50/M (10%) | $25/M | ~$0.02-0.05 | ~$0.16/turn after cache |
| Gemini 3.1 Pro | $2-4/M (tiered ≤/>200k) | $0.50-1.00/M (25%) | $12-18/M | ~$0.01-0.02 | ~$0.40/turn after cache |
| DeepSeek V4 Pro | $0.435/M | $0.003625/M (~99%) | $0.87/M | ~$0.0005-0.002 | ~$0.005/turn after cache |

DeepSeek handles routine chat at ~10-30× less cost. Gemini specializes in long-context synthesis and novel reasoning. Claude handles complex tasks that justify the premium. Use `!cost` to see real-time breakdown including cache hit rates.

Caching is the difference between $5/turn and $0.50/turn for Claude bookclub mode, so it matters. The bot tracks cached and uncached tokens separately and reports the hit rate per provider.

## Architecture

```
bot.py (single file, ~4600 lines)
├── BotConfig                 - per-bot settings (context budget, message fetch limit)
├── ModelProvider             - per-model config: pricing (with cached + tiered rates),
│                              context window, search backend, runtime stats
├── SearchResult              - text + structured citations + grounded-answer flag
├── ReadingMaterial           - bookclub mode: pinned text, chapter breaks,
│                              per-provider cache handles, 24h TTL tracking
├── CalibrationTracker        - confidence bid tracking with emoji feedback
├── WorkingMemory             - auto-decay notes (48h)
├── LongTermMemory            - permanent user-managed facts
├── ConversationManager       - Discord history fetching, per-guild memories,
│                              per-channel/thread reading materials, persistence
└── ClaudeBot                 - main bot class
    ├── _select_model()                  - heuristic routing (no LLM call)
    ├── _estimate_confidence()           - per-model scoring with CJK detection
    ├── _generate_response()             - dispatches to Claude/DeepSeek/Gemini
    ├── _generate_openai_compatible_response()
    │                                    - DeepSeek + Gemini (non-bookclub) shim path
    ├── _generate_gemini_native_response()
    │                                    - Gemini bookclub path via native API
    │                                      (cachedContent + google_search grounding)
    ├── _web_search()                    - Claude native web_search_20250305 tool
    ├── _search_for(provider, query)     - dispatches to provider's SearchBackend
    ├── _tavily_search()                 - Tavily backend (DeepSeek + fallback)
    ├── _google_native_search()          - Google grounding via native Gemini API
    ├── _fetch_ao3_work()                - AO3 work fetcher with shields-up detection,
    │                                      cookie auth, retry-with-backoff
    ├── _detect_chapter_breaks()         - heading-pattern regex for !chapters/!scope
    ├── _slice_material_to_chapters()    - per-thread scope helper
    ├── _create_gemini_cache() / _ensure_gemini_cache()
    │                                    - explicit cachedContents lifecycle
    └── _record_claude_usage()           - cache-aware token accounting
```

**Context sources per message:**
1. System prompt (identity, capabilities, routing reason)
2. Thread index (read-only list of other threads)
3. Long-term memory (permanent facts)
4. Working memory (auto-notes with decay)
5. Reading material (if loaded — full work or thread-scoped chapter range)
6. Current thread (last 60 messages from Discord)

## File Structure

```
├── bot.py              # Everything
├── config.json         # Allowed channels, model preferences
├── config.example.json # Example config
├── requirements.txt    # discord.py, anthropic, openai, tavily-python, beautifulsoup4, matplotlib
├── .env                # API keys (don't commit!)
├── .env.example        # Example env
├── memories.json       # Auto-generated persistence (memories, calibration, reading materials, cache handles)
└── README.md           # You are here
```

## License

Do whatever you want with this. It's a Discord bot, not a spaceship.
