# Hydra Discord Bot

A multi-model Discord bot powered by Claude Opus 4.7 and Deepseek, with smart routing, shared memory, and web search.

## Features

- 🐉 **Multi-model (Hydra)** - Claude + Deepseek with automatic routing
- 🧵 **Thread-based conversations** - keeps channels clean
- 📷 **Image understanding** - upload images and Claude can see them
- 🔍 **Web search** - Claude searches natively, Deepseek searches via Tavily
- 🧠 **Two-tier memory** - working notes (auto-decay) + long-term (permanent)
- 😀 **Emoji reactions** - the bot can react to your messages
- 📎 **File attachments** - long code becomes downloadable files
- 💰 **Cost tracking** - per-model usage and cost breakdown
- 🀄 **Chinese language specialist** - Deepseek translates and teaches CJK

## The Hydra System

Two AI models share one Discord bot, taking turns "fronting" like a plural system:

```
User message arrives
        ↓
   [Router] ← heuristic scoring, no LLM call
   /      \
[Claude]  [Deepseek]
   ↓          ↓
**[Claude]** response   **[Deepseek]** response
```

**How routing works:**
- Images → Claude (only model with vision)
- CJK text → Deepseek (deeper Chinese training data)
- Complex/creative/code → Claude (higher capability)
- Short/factual/casual → Deepseek (50-100x cheaper)
- Ties → Deepseek (cost advantage)
- Users can override with `!claude`, `!opus`, or `!deepseek`
- Per-channel preferences with `!prefer`

**Models know who they are** — each gets a tailored system prompt with its identity, capabilities, why it was selected, and can see labeled messages from the other model in conversation history.

## Commands

### General
| Command | Description |
|---------|-------------|
| `!help` | Show all commands |
| `!context` | Show current context size and cost estimate |
| `!cost` | Show total API usage and cost per model |
| `!memories` | List all memories (both types) |
| `!threads` | Show other recent threads |
| `!search <query>` | Web search with citations |

### Multi-model
| Command | Description |
|---------|-------------|
| `!claude <msg>` | Force Claude to respond |
| `!opus <msg>` | Force Claude Opus to respond |
| `!deepseek <msg>` | Force Deepseek to respond |
| `!models` | Show available models and usage stats |
| `!prefer [claude\|deepseek\|auto]` | Set model preference for this channel |
| `!calibration` | Show confidence calibration stats |

React with 👍❤️🔥✅😂💖💯 (positive) or 👎❌😕 (negative) to bot responses to improve model selection over time.

### Memory
| Command | Description |
|---------|-------------|
| `!remember <key> <value>` | Store a permanent memory |
| `!forget <key>` | Remove a memory |
| `!keep <key>` | Promote a working note to permanent |
| `!summarize <key>` | Auto-summarize thread to memory |
| `!summarize <key> <text>` | Save your own summary |

## The Memory System

Two types, like an actual brain:

**Working Memory** (auto-managed) — Claude/Deepseek automatically jot down notes during conversation. Notes fade after ~48h if not referenced, stick around longer if relevant. Max 10 notes. See them with `!memories`, promote with `!keep`.

**Long-Term Memory** (permanent) — explicit facts created with `!remember`. Never decay until `!forget`. Shared across all threads in the server.

Freshness indicators: 🟢 Fresh (>70%) · 🟡 Fading (30-70%) · 🔴 Almost gone (<30%)

## Web Search

Both models can search the web:

- **Claude**: Built-in web search tool — can search organically during conversation or via `!search`
- **Deepseek**: Tavily function calling — can search organically or via `!search` (requires `TAVILY_API_KEY`)

`!search` routes to whichever model is preferred for the channel.

## Setup

### 1. Create Discord Bot

1. Go to [Discord Developer Portal](https://discord.com/developers/applications)
2. New Application → Bot section → Reset Token (save it)
3. Enable **MESSAGE CONTENT INTENT** under Privileged Gateway Intents
4. OAuth2 → URL Generator → Scopes: `bot` → Permissions: Send Messages, Read Message History, Create Public Threads, Send Messages in Threads, Add Reactions, Attach Files, Embed Links
5. Open generated URL to invite bot

### 2. Get API Keys

- **Anthropic** (required or optional if Deepseek-only): [console.anthropic.com](https://console.anthropic.com/)
- **Deepseek** (optional): [platform.deepseek.com](https://platform.deepseek.com/)
- **Tavily** (optional, enables Deepseek web search): [tavily.com](https://tavily.com/) — free 1,000 searches/month

At least one of Anthropic or Deepseek API keys is required.

### 3. Configure

```bash
pip install -r requirements.txt

cp .env.example .env       # Edit with your API keys
cp config.example.json config.json  # Edit with your channel IDs
```

**.env:**
```
DISCORD_TOKEN=your_discord_token
ANTHROPIC_API_KEY=your_anthropic_key      # Optional if Deepseek-only
DEEPSEEK_API_KEY=your_deepseek_key        # Optional if Claude-only
TAVILY_API_KEY=your_tavily_key            # Optional
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

The bot gracefully degrades — runs Claude-only, Deepseek-only, or both depending on which API keys are present.

## Cost Comparison

| Model | Input | Output | Typical message |
|-------|-------|--------|-----------------|
| Claude Opus 4.7 | $15/M tokens | $75/M tokens | ~$0.02-0.05 |
| Deepseek V4 Pro | $0.435/M tokens | $0.87/M tokens | ~$0.0005-0.002 |

Deepseek handles routine chat at ~50-100x less cost. Claude handles complex tasks that justify the premium. Use `!cost` to see real-time breakdown.

## Architecture

```
bot.py (single file, ~2000 lines)
├── ModelProvider          - per-model config, pricing, runtime stats
├── CalibrationTracker     - confidence bid tracking with emoji feedback
├── WorkingMemory          - auto-decay notes (48h)
├── LongTermMemory         - permanent user-managed facts
├── ClaudeBot              - main bot class
│   ├── _select_model()    - heuristic routing (no LLM call)
│   ├── _estimate_confidence() - per-model scoring with CJK detection
│   ├── _generate_response()   - dispatches to Claude or Deepseek
│   ├── _generate_deepseek_response() - OpenAI-compatible API + Tavily tool loop
│   ├── _web_search()      - Claude native web search (!search command)
│   └── _tavily_search()   - Tavily wrapper for Deepseek
└── memories.json          - persistence (memory, calibration, model stats)
```

**Context sources per message:**
1. System prompt (identity, capabilities, routing reason)
2. Thread index (read-only list of other threads)
3. Long-term memory (permanent facts)
4. Working memory (auto-notes with decay)
5. Current thread (last 20 messages from Discord)

## File Structure

```
├── bot.py              # Everything
├── config.json         # Allowed channels, model preferences
├── config.example.json # Example config
├── requirements.txt    # discord.py, anthropic, openai, tavily-python
├── .env                # API keys (don't commit!)
├── .env.example        # Example env
├── memories.json       # Auto-generated persistence
└── README.md           # You are here
```

## License

Do whatever you want with this. It's a Discord bot, not a spaceship.
