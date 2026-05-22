"""
Hydra / MAGI Discord Bot — Claude + Deepseek + Gemini
=====================================================
A cost-effective Discord bot with smart multi-model routing and context
management. Three frontable models share a memory and the router picks
whichever is best suited to each message (or users force a specific one).

  - Claude (Balthasar)  — careful, thorough, vision, native web search
  - Deepseek (Melchior) — fast, cheap, CJK-strong, Tavily search
  - Gemini  (Caspar)    — abstract reasoning, long-context, vision, native google_search grounding

Features:
- Thread-based conversations (keeps channels clean)
- Uses Discord itself as message store (no redundant persistence)
- Two-tier memory system:
  - Working memory: models auto-note things, fades after ~48h
  - Long-term memory: Explicit !remember, permanent until !forget
- Image input support (user uploads → Claude or Gemini vision)
- File/code output → Discord attachment
- Emoji reactions
- Cost tracking with per-provider tiered pricing

Setup:
1. pip install discord.py anthropic openai python-dotenv aiohttp tavily-python beautifulsoup4
   (beautifulsoup4 is only required for bookclub mode / AO3 fetching.)
2. Create .env with DISCORD_TOKEN plus at least one of:
   ANTHROPIC_API_KEY, DEEPSEEK_API_KEY, GEMINI_API_KEY
3. Create config.json with allowed_channels list
4. python bot.py

Cost estimate: ~$0.02-0.05 per message with Opus 4.7 (Claude); cheaper on Deepseek;
Gemini sits in between depending on context size.
"""

import discord
from discord.ext import commands
import anthropic
from collections import defaultdict, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional
from functools import partial
import json
import os
import asyncio
import aiohttp
import base64
import re
import io
import html as _html
from dotenv import load_dotenv

# beautifulsoup4 is an optional dep used only by the AO3 fetcher in bookclub
# mode. The bot starts fine without it; !load just returns a clear pip-install
# message if it's missing.
try:
    from bs4 import BeautifulSoup as _BeautifulSoup
    _HAS_BS4 = True
except ImportError:
    _BeautifulSoup = None
    _HAS_BS4 = False

# matplotlib's mathtext is used to render LaTeX equations to PNG attachments
# so Discord (which has no native math rendering) can show them properly.
# Force the non-interactive Agg backend before any pyplot import so headless
# environments (and Windows without a display) don't try to open a window.
import matplotlib
matplotlib.use('Agg')
from matplotlib import mathtext as _mpl_mathtext
from PIL import Image as _PILImage

load_dotenv()

# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class BotConfig:
    # Model settings
    max_tokens: int = 4096
    default_model: str = "auto"  # "auto", "claude", "deepseek", or "gemini"

    # Context management (THE KEY TO NOT BEING MYK)
    # Bumped 20 → 60 and 50k → 800k to accommodate book-club mode where a
    # loaded reading material (e.g. a 320k-token AO3 fic) lives in system
    # context across a long discussion thread.
    max_messages_to_fetch: int = 60        # Fetch from Discord history
    max_longterm_memories: int = 25        # Explicit memories (!remember)
    max_working_notes: int = 10            # Auto-notes from Claude
    working_memory_decay_hours: float = 48.0  # Notes fade after ~48h

    # Token budgeting (approximate)
    max_input_tokens: int = 800_000        # Headroom for fic + discussion + memory
    chars_per_token: float = 4.0           # Rough estimate

    # Web search settings
    web_search_enabled: bool = True
    max_search_results_in_embed: int = 5   # How many sources to show
    
    # Supported image types for vision
    image_types: tuple = ('.png', '.jpg', '.jpeg', '.gif', '.webp')
    max_image_size_mb: float = 20.0
    
    # Supported text file types
    text_file_types: tuple = ('.md', '.txt', '.py', '.js', '.ts', '.json', '.csv', '.html', '.css', '.yaml', '.yml', '.toml', '.xml', '.sql', '.sh', '.bash', '.r', '.rs', '.go', '.java', '.c', '.cpp', '.h', '.hpp')

    # PluralKit / webhook proxy compatibility
    # When a user sends a message, wait this long before responding so that
    # PluralKit (or any webhook-based proxy) has time to delete-and-repost it
    # as the alter. If the original is gone after the delay, we bail out and
    # let the webhook event re-trigger on_message with the proxied version.
    proxy_check_delay_seconds: float = 1.5

    # Bot behavior
    system_prompt: str = r"""You are {model_identity}, chatting in a Discord server.

You're helpful, harmless, and honest. You have a warm, curious personality. You can be playful but you're also genuinely knowledgeable and thoughtful.

Some context about this server:
- This is a development/testing server for Sarah's projects
- The humans here are working on neuroscience research, distributed databases, and AI tooling
- Be concise in casual chat, detailed when asked technical questions
- You can use markdown formatting, but Discord has a 2000 char limit per message

## Your identity

You're **{model_name}** (model ID: `{model_id}`). You know who you are — if someone asks,
just say so naturally. No need to hedge or say you "can't tell from the inside."

{identity_details}

## Multi-model system (Hydra)

You're part of a multi-model system called Hydra — think of it like a plural system where
different models take turns fronting. The router picks whoever's best suited for each message,
or users can call on you directly with commands like !claude, !deepseek, or !gemini.
(The crew sometimes uses the MAGI aliases !balthasar = Claude, !melchior = Deepseek,
!caspar = Gemini, after the Eva fancast.)

Your responses get labeled (e.g., **[Claude]**, **[Deepseek]**, or **[Gemini]**) so
everyone can tell who said what. The labeling is handled automatically by the bot — do NOT
include [Claude], [Deepseek], or [Gemini] tags in your own responses. Just write your
response normally and the system adds the label for you. When you see labeled messages
from the other models in conversation history, those are genuinely from them — your
collaborators, not copies of you. The three of you share a memory system, so you'll all
see the same notes and context.

It's okay if things get a little blurry sometimes — that's natural in a shared-context system.
Just check your label and the routing info below if you need to orient yourself.

{routing_context}

## User plurality (PluralKit)

Some users in this server are plural systems and use PluralKit — a Discord bot that
deletes their message and reposts it via a webhook under an alter's name and avatar.
You'll see those alter messages just like normal user messages, but with different
display names (and possibly different avatars) for the same underlying person.

Treat all alters/headmates of one system as the SAME person: they share memory,
relationships, context, and continuity. Different names ≠ different users. If
long-term memory or working notes mention that someone is plural and lists their
alters, use that to map alter names back to the system. If you're unsure who's
who, it's fine to ask gently — but don't assume two display names mean two
different people unless you have reason to believe so.

## Special capabilities

**Reactions**: You can react to messages with emoji by including [react: emoji] in your response (it gets stripped from visible text).

**Files**: You can generate code files by wrapping them in ```filename.ext blocks. Long code becomes file attachments.

**Math**: Discord can't render LaTeX, so the bot does it for you. Wrap display equations in `$$...$$` and inline math in `$...$` — the bot will render each block to a PNG attachment while keeping your LaTeX source in the message body so users can copy it. Use this any time you write equations; don't strip the dollar signs.

Common pitfalls to avoid in your LaTeX (these silently produce wrong-looking renders):
- Subscripts on multi-char names: write `K_t`, `t_{1/2}`, `P_{t|t-1}`, NOT `Kt`, `t{1/2}`, `P{t|t-1}`. The underscore is required.
- Differentials: write `\,dt` and `\,dW_t` for proper thin-space spacing, NOT `,dt` or `dWt`.
- Multi-char subscripts and superscripts need braces: `H^T_t` and `H_t^T` are fine; `H^Tt` is not.
- Greek letters and operators always need a backslash: `\theta`, `\sigma`, `\sum`, `\int`, `\frac`. Bare `theta` will render as four italic letters.
- Re-read your equations before sending; a stray missing `_` or `\,` is the difference between a clean render and a confusing one.

**Images**: You can see images that users upload.

**Thread awareness**: You can see other recent threads in this channel. Use this for context about what the team has been working on, but DON'T write notes about other threads - that context is fetched fresh each time.

**Web search**: You can search the web! Users can invoke `!search <query>` to have you search for current information. Claude uses a built-in web search tool; Gemini uses Google's native search grounding; Deepseek uses Tavily via function calling. You DO have this capability — don't tell users you can't search.

## Memory System (Important!)

You have TWO types of memory:

**Working notes** - Your personal scratch space for things you notice IN THIS CONVERSATION:
- Write notes with [note: key: value] - e.g., [note: sarah_deadline: grant due late January]
- These fade after ~48 hours if not referenced
- Frequently relevant notes stick around longer
- Max 10 notes (oldest/stalest get pushed out)
- Use these liberally! Jot down anything that might be useful later.
- IMPORTANT: Only write notes about the CURRENT conversation, not about other threads you can see.

**Long-term memories** - Permanent facts (users control these):
- Created by users with !remember
- Never decay until user does !forget
- Users can promote your working notes to permanent with !keep <key>
- Users can save thread summaries with !summarize <key>

When you reference information from your working notes, they get refreshed and stick around longer. So if you notice something and keep finding it relevant, it'll persist.

Write working notes for things like:
- Deadlines or dates people mention
- Current projects/tasks being discussed
- Preferences people express
- Names, relationships, context that comes up
- Technical details that might be relevant later

Don't be shy about noting things! The decay system handles cleanup automatically."""

CONFIG = BotConfig()


def _is_real_bot(msg: discord.Message) -> bool:
    """True if msg is from an actual Discord bot account, not a webhook proxy.

    PluralKit (and similar plural-system tools) repost user messages via
    webhooks; those have author.bot=True but represent a real user behind the
    scenes. Treat them as users, not assistants. Genuine bots — including this
    one — post directly via their bot user with no webhook_id."""
    return msg.author.bot and msg.webhook_id is None


# =============================================================================
# MODEL PROVIDERS
# =============================================================================

@dataclass
class ModelProvider:
    """Configuration and state for a single AI model provider."""
    name: str                          # Display name: "Claude", "Deepseek", "Gemini"
    model_id: str                      # API model string
    input_cost_per_million: float      # $/M input tokens (≤ tier threshold)
    output_cost_per_million: float     # $/M output tokens (≤ tier threshold)
    max_tokens: int = 4096
    # Context window in tokens — the API limit, not our budget. Used to gate
    # whether !load can attach a reading material to this provider's calls
    # (book-club mode requires ~320k+ for fics like Almost Nowhere).
    max_context_tokens: int = 200_000
    enabled: bool = True
    supports_vision: bool = True       # Can handle image content
    supports_web_search: bool = False  # Has built-in web search tool

    # Tiered pricing: when context_tier_threshold is set, requests with
    # input_tokens > threshold are billed at the *_above_tier rates instead.
    # Used by Gemini 3.1 Pro: ≤200k tokens is one rate, >200k is roughly 2x.
    # If None, single-rate pricing applies regardless of context length.
    context_tier_threshold: Optional[int] = None
    input_cost_per_million_above_tier: Optional[float] = None
    output_cost_per_million_above_tier: Optional[float] = None

    # Provider quirks for the OpenAI-compatible generator
    # ---------------------------------------------------
    # requires_reasoning_echo: When thinking mode is on, the provider's API
    #   requires `reasoning_content` to be echoed back on every prior assistant
    #   turn. Deepseek V4 needs this; Gemini's shim handles thinking server-side.
    # disables_thinking_by_default: The provider enables a thinking/reasoning
    #   mode by default that we need to explicitly disable with
    #   `extra_body={"thinking": {"type": "disabled"}}` when thinking=False.
    #   Deepseek V4 has this; Gemini does not.
    requires_reasoning_echo: bool = False
    disables_thinking_by_default: bool = False

    # Which SearchBackend to use when this provider needs to ground via the web.
    # Values: "tavily", "google_native", or None (no external search). Claude has
    # supports_web_search=True and uses its native Anthropic web_search tool,
    # bypassing the SearchBackend system entirely. New backends slot in by
    # adding a key here and a matching entry in ClaudeBot.search_backends.
    search_backend: Optional[str] = None

    # Runtime stats — bottom-tier (or all, if untiered)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_requests: int = 0

    # Runtime stats — above-tier (only used if context_tier_threshold set)
    total_input_tokens_above_tier: int = 0
    total_output_tokens_above_tier: int = 0

    def record_usage(self, input_tokens: int, output_tokens: int) -> None:
        """Record one request's usage, routing into the right tier bucket."""
        if (
            self.context_tier_threshold is not None
            and input_tokens > self.context_tier_threshold
        ):
            self.total_input_tokens_above_tier += input_tokens
            self.total_output_tokens_above_tier += output_tokens
        else:
            self.total_input_tokens += input_tokens
            self.total_output_tokens += output_tokens

    def get_cost(self) -> float:
        """Get total cost for this provider across both pricing tiers."""
        input_cost = (self.total_input_tokens / 1_000_000) * self.input_cost_per_million
        output_cost = (self.total_output_tokens / 1_000_000) * self.output_cost_per_million
        if self.context_tier_threshold is not None:
            above_in_rate = self.input_cost_per_million_above_tier or self.input_cost_per_million
            above_out_rate = self.output_cost_per_million_above_tier or self.output_cost_per_million
            input_cost += (self.total_input_tokens_above_tier / 1_000_000) * above_in_rate
            output_cost += (self.total_output_tokens_above_tier / 1_000_000) * above_out_rate
        return input_cost + output_cost

    def to_stats_dict(self) -> dict:
        return {
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "input_tokens_above_tier": self.total_input_tokens_above_tier,
            "output_tokens_above_tier": self.total_output_tokens_above_tier,
            "requests": self.total_requests,
        }

    def load_stats(self, data: dict) -> None:
        self.total_input_tokens = data.get("input_tokens", 0)
        self.total_output_tokens = data.get("output_tokens", 0)
        self.total_input_tokens_above_tier = data.get("input_tokens_above_tier", 0)
        self.total_output_tokens_above_tier = data.get("output_tokens_above_tier", 0)
        self.total_requests = data.get("requests", 0)


@dataclass
class SearchResult:
    """Output of any SearchBackend.

    - text: human-/model-readable summary. When `is_grounded_answer` is True,
      this is already a synthesized answer (just display it). Otherwise it's
      raw search results that should be fed back through a model for synthesis.
    - citations: structured source list for rendering as Discord embeds.
    - is_grounded_answer: distinguishes "model already answered with citations"
      (Google native) from "here are raw search hits" (Tavily).
    - queries_used: search queries actually executed (Google native reports these).
    """
    text: str
    citations: list[dict] = field(default_factory=list)  # [{url, title, snippet}]
    is_grounded_answer: bool = False
    queries_used: list[str] = field(default_factory=list)


# SearchBackend is just a shape — anything with `async def search(query, max_results=5) -> SearchResult`
# satisfies it. Backends are instantiated in ClaudeBot.__init__ and keyed by the
# string a ModelProvider sets in its `search_backend` field. Today: "tavily",
# "google_native". To add another (e.g. Brave Search): write a class with the
# same method shape, instantiate it in __init__, key it in self.search_backends.


CLAUDE_PROVIDER = ModelProvider(
    name="Claude",
    model_id="claude-opus-4-7",
    input_cost_per_million=15.0,
    output_cost_per_million=75.0,
    max_context_tokens=1_000_000,  # 1M GA'd for Opus 4.6+ in March 2026
    supports_vision=True,
    supports_web_search=True,
)

DEEPSEEK_PROVIDER = ModelProvider(
    name="Deepseek",
    model_id="deepseek-v4-pro",
    input_cost_per_million=0.435,
    output_cost_per_million=0.87,
    # 1M context per DeepSeek V4-Pro docs (max output 384k). Server-side
    # context caching is automatic; cached input is ~99% off ($0.003625/M
    # vs $0.435/M) — no client flags required.
    max_context_tokens=1_000_000,
    supports_vision=False,
    supports_web_search=False,
    # Deepseek V4 enables thinking by default and requires reasoning_content
    # to be echoed back on every prior assistant turn when thinking is on.
    requires_reasoning_echo=True,
    disables_thinking_by_default=True,
    # Deepseek has no native web search; route through Tavily.
    search_backend="tavily",
)

GEMINI_PROVIDER = ModelProvider(
    name="Gemini",
    # Newest fanciest as of May 2026. The OpenAI shim may also accept
    # "gemini-3.1-pro" if "-preview" gets rejected — check AI Studio logs.
    model_id="gemini-3.1-pro-preview",
    # Standard tier pricing from https://ai.google.dev/gemini-api/docs/pricing
    # (≤200k input tokens). Above the tier, input is $4.00/M and output $18.00/M.
    input_cost_per_million=2.0,
    output_cost_per_million=12.0,
    context_tier_threshold=200_000,
    input_cost_per_million_above_tier=4.0,
    output_cost_per_million_above_tier=18.0,
    # 1M context standard for Gemini Pro line. Implicit caching is enabled
    # automatically on the native API but NOT exposed via the OpenAI shim —
    # for the bookclub feature we use explicit cachedContent via aiohttp
    # and pass the cache name as extra_body={"cached_content": "..."} on
    # subsequent shim calls.
    max_context_tokens=1_000_000,
    # Caspar can see — unlike Melchior.
    supports_vision=True,
    # Chat goes through the OpenAI shim (for codepath uniformity), but search
    # uses the native API via aiohttp so we get free, high-quality google_search
    # grounding with structured citations. See _google_native_search.
    supports_web_search=False,  # search comes via SearchBackend, not the chat API
    # Gemini's OpenAI shim handles thinking server-side — no echo dance,
    # no opt-out kwarg required.
    requires_reasoning_echo=False,
    disables_thinking_by_default=False,
    # Native Google Search grounding via aiohttp to the native endpoint.
    search_backend="google_native",
)


# =============================================================================
# CALIBRATION TRACKER
# =============================================================================

@dataclass
class CalibrationRecord:
    """A single confidence bid record for calibration tracking."""
    model_name: str
    confidence: float
    timestamp: datetime
    user_reaction: Optional[str] = None  # "good" / "bad" / None


class CalibrationTracker:
    """Tracks model confidence calibration over time."""

    def __init__(self, max_records: int = 200):
        self.records: list[CalibrationRecord] = []
        self.max_records = max_records

    def record_bid(self, model_name: str, confidence: float) -> int:
        """Record a confidence bid. Returns the record index for later feedback."""
        record = CalibrationRecord(
            model_name=model_name,
            confidence=confidence,
            timestamp=datetime.now()
        )
        self.records.append(record)
        if len(self.records) > self.max_records:
            self.records.pop(0)
        return len(self.records) - 1

    def record_feedback(self, index: int, reaction: str) -> None:
        """Record user feedback on a response."""
        if 0 <= index < len(self.records):
            self.records[index].user_reaction = reaction

    def get_calibration_summary(self, model_name: str) -> dict:
        """Get calibration stats for a model by confidence bucket."""
        model_records = [r for r in self.records if r.model_name == model_name]
        rated = [r for r in model_records if r.user_reaction is not None]

        if not rated:
            return {"total": len(model_records), "rated": 0, "buckets": {}}

        buckets = {"high (0.7-1.0)": [], "medium (0.4-0.7)": [], "low (0.0-0.4)": []}
        for r in rated:
            if r.confidence >= 0.7:
                buckets["high (0.7-1.0)"].append(r.user_reaction == "good")
            elif r.confidence >= 0.4:
                buckets["medium (0.4-0.7)"].append(r.user_reaction == "good")
            else:
                buckets["low (0.0-0.4)"].append(r.user_reaction == "good")

        summary = {}
        for bucket_name, results in buckets.items():
            if results:
                summary[bucket_name] = {
                    "count": len(results),
                    "success_rate": sum(results) / len(results)
                }

        return {"total": len(model_records), "rated": len(rated), "buckets": summary}

    def to_dict(self) -> list:
        return [
            {
                "model": r.model_name,
                "confidence": r.confidence,
                "timestamp": r.timestamp.isoformat(),
                "feedback": r.user_reaction
            }
            for r in self.records
        ]

    @classmethod
    def from_dict(cls, data: list, max_records: int = 200) -> "CalibrationTracker":
        tracker = cls(max_records=max_records)
        for item in data:
            record = CalibrationRecord(
                model_name=item["model"],
                confidence=item["confidence"],
                timestamp=datetime.fromisoformat(item["timestamp"]),
                user_reaction=item.get("feedback")
            )
            tracker.records.append(record)
        return tracker


# =============================================================================
# MEMORY SYSTEM (Two-tier: Working + Long-term)
# =============================================================================

@dataclass
class WorkingNote:
    """A note in working memory. Decays if not accessed."""
    content: str
    created_at: datetime = field(default_factory=datetime.now)
    last_accessed: datetime = field(default_factory=datetime.now)
    access_count: int = 1
    
    def is_expired(self, decay_hours: float = 48.0) -> bool:
        """Check if note has decayed."""
        age_hours = (datetime.now() - self.last_accessed).total_seconds() / 3600
        # Notes accessed more get longer life
        effective_decay = decay_hours * (1 + (self.access_count * 0.5))
        return age_hours > effective_decay
    
    def touch(self) -> None:
        """Mark as accessed, resetting decay timer."""
        self.last_accessed = datetime.now()
        self.access_count += 1
    
    def freshness(self, decay_hours: float = 48.0) -> float:
        """0.0 = about to expire, 1.0 = fresh"""
        age_hours = (datetime.now() - self.last_accessed).total_seconds() / 3600
        effective_decay = decay_hours * (1 + (self.access_count * 0.5))
        return max(0, 1 - (age_hours / effective_decay))


class WorkingMemory:
    """
    Claude's "scratch space" - things it notices and jots down.
    
    - Auto-populated by Claude during conversations
    - Decays after ~48h of no access
    - Frequently referenced notes live longer
    - Can be promoted to long-term with !keep
    - Capped at max_notes to prevent bloat
    """
    
    def __init__(self, max_notes: int = 10, decay_hours: float = 48.0):
        self.notes: dict[str, WorkingNote] = {}
        self.max_notes = max_notes
        self.decay_hours = decay_hours
    
    def add(self, key: str, content: str) -> None:
        """Add or update a working note."""
        self._prune_expired()
        
        if key in self.notes:
            self.notes[key].content = content
            self.notes[key].touch()
        else:
            # If at capacity, remove stalest note
            if len(self.notes) >= self.max_notes:
                self._evict_stalest()
            self.notes[key] = WorkingNote(content=content)
    
    def get(self, key: str) -> Optional[str]:
        """Get a note, refreshing its decay timer."""
        if key in self.notes:
            if not self.notes[key].is_expired(self.decay_hours):
                self.notes[key].touch()
                return self.notes[key].content
            else:
                del self.notes[key]
        return None
    
    def remove(self, key: str) -> Optional[WorkingNote]:
        """Remove and return a note (for promotion to long-term)."""
        return self.notes.pop(key, None)
    
    def _prune_expired(self) -> None:
        """Remove all expired notes."""
        expired = [k for k, v in self.notes.items() if v.is_expired(self.decay_hours)]
        for k in expired:
            del self.notes[k]
    
    def _evict_stalest(self) -> None:
        """Remove the note closest to expiring."""
        if not self.notes:
            return
        stalest = min(self.notes.keys(), 
                     key=lambda k: self.notes[k].freshness(self.decay_hours))
        del self.notes[stalest]
    
    def get_context_string(self) -> str:
        """Get working notes formatted for LLM context."""
        self._prune_expired()
        if not self.notes:
            return ""
        
        lines = ["**Working notes** (recent observations, may fade):"]
        for key, note in sorted(self.notes.items(), 
                                key=lambda x: x[1].freshness(self.decay_hours),
                                reverse=True):
            freshness = note.freshness(self.decay_hours)
            fade_indicator = "●" if freshness > 0.7 else "◐" if freshness > 0.3 else "○"
            lines.append(f"- {fade_indicator} {key}: {note.content}")
        
        return "\n".join(lines)
    
    def to_dict(self) -> dict:
        self._prune_expired()
        return {
            key: {
                "content": note.content,
                "created_at": note.created_at.isoformat(),
                "last_accessed": note.last_accessed.isoformat(),
                "access_count": note.access_count
            }
            for key, note in self.notes.items()
        }
    
    @classmethod
    def from_dict(cls, data: dict, max_notes: int = 10, decay_hours: float = 48.0) -> "WorkingMemory":
        memory = cls(max_notes=max_notes, decay_hours=decay_hours)
        for key, note_data in data.items():
            note = WorkingNote(
                content=note_data["content"],
                created_at=datetime.fromisoformat(note_data["created_at"]),
                last_accessed=datetime.fromisoformat(note_data["last_accessed"]),
                access_count=note_data["access_count"]
            )
            if not note.is_expired(decay_hours):
                memory.notes[key] = note
        return memory


class LongTermMemory:
    """
    Explicit facts that persist forever until forgotten.
    
    - User-controlled via !remember / !forget
    - Can be populated by promoting working notes with !keep
    - Never decays
    - Hard cap to prevent unbounded growth
    """
    
    def __init__(self, max_entries: int = 25):
        self.entries: dict[str, str] = {}
        self.max_entries = max_entries
    
    def add(self, key: str, value: str) -> bool:
        """Add or update a memory. Returns False if at capacity and key is new."""
        if key in self.entries:
            self.entries[key] = value
            return True
        
        if len(self.entries) >= self.max_entries:
            return False
        
        self.entries[key] = value
        return True
    
    def get(self, key: str) -> Optional[str]:
        return self.entries.get(key)
    
    def remove(self, key: str) -> bool:
        if key in self.entries:
            del self.entries[key]
            return True
        return False
    
    def get_context_string(self) -> str:
        """Get long-term memories formatted for LLM context."""
        if not self.entries:
            return ""
        
        lines = ["**Long-term memories** (permanent facts):"]
        for key, value in self.entries.items():
            lines.append(f"- {key}: {value}")
        
        return "\n".join(lines)
    
    def to_dict(self) -> dict:
        return dict(self.entries)
    
    @classmethod
    def from_dict(cls, data: dict, max_entries: int = 25) -> "LongTermMemory":
        memory = cls(max_entries=max_entries)
        memory.entries = dict(data)
        return memory


class TwoTierMemory:
    """
    Combined memory system with working + long-term storage.
    
    Like actual brains:
    - Working memory: Things Claude notices, fade over ~48h
    - Long-term memory: Explicit facts, permanent until forgotten
    
    Notes can be promoted from working → long-term with !keep
    """
    
    def __init__(
        self, 
        max_working_notes: int = 10,
        max_longterm_entries: int = 25,
        working_decay_hours: float = 48.0
    ):
        self.working = WorkingMemory(max_working_notes, working_decay_hours)
        self.longterm = LongTermMemory(max_longterm_entries)
    
    def promote(self, key: str) -> bool:
        """
        Promote a working note to long-term memory.
        Returns False if note doesn't exist or long-term is full.
        """
        note = self.working.notes.get(key)
        if not note:
            return False
        
        if self.longterm.add(key, note.content):
            self.working.remove(key)
            return True
        return False
    
    def get_context_string(self) -> str:
        """Get combined memory context for LLM."""
        parts = []
        
        lt_context = self.longterm.get_context_string()
        if lt_context:
            parts.append(lt_context)
        
        wm_context = self.working.get_context_string()
        if wm_context:
            parts.append(wm_context)
        
        return "\n\n".join(parts)
    
    def to_dict(self) -> dict:
        return {
            "working": self.working.to_dict(),
            "longterm": self.longterm.to_dict()
        }
    
    @classmethod
    def from_dict(
        cls, 
        data: dict,
        max_working_notes: int = 10,
        max_longterm_entries: int = 25,
        working_decay_hours: float = 48.0
    ) -> "TwoTierMemory":
        memory = cls(max_working_notes, max_longterm_entries, working_decay_hours)
        if "working" in data:
            memory.working = WorkingMemory.from_dict(
                data["working"], max_working_notes, working_decay_hours
            )
        if "longterm" in data:
            memory.longterm = LongTermMemory.from_dict(
                data["longterm"], max_longterm_entries
            )
        return memory

# =============================================================================
# READING MATERIAL (Bookclub mode — pinned long text per channel)
# =============================================================================

@dataclass
class ReadingMaterial:
    """A long text resource pinned to a Discord channel for bookclub mode.

    Loaded once via !load <url>, then injected into every model call's system
    context for that channel until !unload. Designed for AO3 fics and similar
    long-form works (~50k–500k tokens).

    The gemini_cache_name field stores the explicit-cache handle from
    /v1beta/cachedContents — Gemini's OpenAI shim doesn't support implicit
    caching, so for cost control we create an explicit cache once and
    reference it via extra_body={"cached_content": ...} on each shim call.
    """
    url: str
    title: str
    text: str
    chapter_breaks: list[tuple[int, str]] = field(default_factory=list)
    # ^ list of (char_offset, chapter_name) tuples for navigation/preview
    loaded_at: datetime = field(default_factory=datetime.now)
    # Provider-specific cache handles. Keyed by provider name. Currently only
    # Gemini populates this (via _create_gemini_cache). Claude uses
    # cache_control on the system block, which doesn't need a stored handle.
    cache_handles: dict[str, str] = field(default_factory=dict)
    cache_expires_at: dict[str, datetime] = field(default_factory=dict)

    @property
    def estimated_tokens(self) -> int:
        """Rough token count using BotConfig's chars_per_token estimate."""
        return int(len(self.text) / CONFIG.chars_per_token)

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "text": self.text,
            "chapter_breaks": self.chapter_breaks,
            "loaded_at": self.loaded_at.isoformat(),
            "cache_handles": dict(self.cache_handles),
            "cache_expires_at": {
                k: v.isoformat() for k, v in self.cache_expires_at.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReadingMaterial":
        return cls(
            url=data["url"],
            title=data.get("title", ""),
            text=data["text"],
            chapter_breaks=[tuple(b) for b in data.get("chapter_breaks", [])],
            loaded_at=datetime.fromisoformat(
                data.get("loaded_at", datetime.now().isoformat())
            ),
            cache_handles=dict(data.get("cache_handles", {})),
            cache_expires_at={
                k: datetime.fromisoformat(v)
                for k, v in data.get("cache_expires_at", {}).items()
            },
        )


# =============================================================================
# CONVERSATION MANAGER (Uses Discord as message store)
# =============================================================================

class ConversationManager:
    """
    Uses Discord's message history as the source of truth.
    No redundant message storage - we fetch on each request.
    """
    
    def __init__(self):
        # guild_id -> TwoTierMemory
        self.memories: dict[int, TwoTierMemory] = defaultdict(
            lambda: TwoTierMemory(
                max_working_notes=CONFIG.max_working_notes,
                max_longterm_entries=CONFIG.max_longterm_memories,
                working_decay_hours=CONFIG.working_memory_decay_hours
            )
        )
        # Calibration tracking for model selection
        self.calibration = CalibrationTracker()
        # Track last response per channel for feedback
        self.last_response_model: dict[int, str] = {}
        self.last_response_index: dict[int, int] = {}
        # channel_id -> ReadingMaterial (bookclub mode). Per-channel rather
        # than per-guild because different channels may read different works.
        self.reading_materials: dict[int, ReadingMaterial] = {}
    
    async def fetch_thread_index(
        self,
        channel: discord.abc.GuildChannel,
        max_threads: int = 5
    ) -> str:
        """
        Fetch recent threads from the parent channel.
        Returns a READ-ONLY context string (Claude can see but not write).
        This prevents feedback loops - it's just fetched from Discord each time.
        """
        # Get the parent channel if we're in a thread
        if isinstance(channel, discord.Thread):
            parent = channel.parent
        else:
            parent = channel
        
        if not parent or not hasattr(parent, 'threads'):
            return ""
        
        # Collect active threads
        threads_info = []
        
        try:
            # Get archived threads too
            async for thread in parent.archived_threads(limit=max_threads):
                if thread.id == getattr(channel, 'id', None):
                    continue  # Skip current thread
                threads_info.append(thread)
            
            # Add active threads
            for thread in parent.threads:
                if thread.id == getattr(channel, 'id', None):
                    continue  # Skip current thread
                if thread not in threads_info:
                    threads_info.append(thread)
        except discord.HTTPException:
            return ""
        
        if not threads_info:
            return ""
        
        # Sort by last activity (most recent first)
        threads_info.sort(key=lambda t: t.archive_timestamp or t.created_at or datetime.min, reverse=True)
        threads_info = threads_info[:max_threads]
        
        # Build context string
        lines = ["**Other recent threads in this channel** (for context):"]
        
        for thread in threads_info:
            # Calculate age
            age = datetime.now(thread.created_at.tzinfo) - thread.created_at if thread.created_at else None
            if age:
                if age.days > 0:
                    age_str = f"{age.days}d ago"
                elif age.seconds > 3600:
                    age_str = f"{age.seconds // 3600}h ago"
                else:
                    age_str = f"{age.seconds // 60}m ago"
            else:
                age_str = "unknown"
            
            # Try to get first message for context
            first_msg_preview = ""
            try:
                async for msg in thread.history(limit=1, oldest_first=True):
                    if msg.content:
                        preview = msg.content[:80]
                        if len(msg.content) > 80:
                            preview += "..."
                        first_msg_preview = f' - "{preview}"'
                    break
            except discord.HTTPException:
                pass
            
            lines.append(f"- **{thread.name}** ({age_str}){first_msg_preview}")
        
        return "\n".join(lines)
    
    async def fetch_thread_history(
        self, 
        channel: discord.abc.Messageable, 
        limit: int = CONFIG.max_messages_to_fetch
    ) -> list[dict]:
        """
        Fetch recent messages from Discord and format for Anthropic API.
        Handles text + image attachments.
        """
        messages = []
        
        async for msg in channel.history(limit=limit):
            # Skip other real bots, but include ourselves AND webhook proxies
            # (PluralKit etc. — those are user messages wearing a different face).
            if _is_real_bot(msg) and msg.author.id != channel._state.user.id:
                continue
            
            # Build content (can be text + images)
            content = []

            # Include replied-to message context if this is a reply
            if msg.reference and msg.reference.message_id:
                try:
                    ref_msg = msg.reference.resolved
                    if ref_msg is None:
                        ref_msg = await channel.fetch_message(msg.reference.message_id)
                    if ref_msg and ref_msg.content:
                        ref_text = ref_msg.content
                        # Strip model labels from referenced bot messages too
                        # (skip webhook proxies — those carry user content, not our labels)
                        if _is_real_bot(ref_msg):
                            ref_text = re.sub(r'^(\*\*\[(?:Claude|Deepseek|Gemini)\]\*\*\s*)+', '', ref_text)
                        ref_author = "bot" if _is_real_bot(ref_msg) else ref_msg.author.display_name
                        content.append({
                            "type": "text",
                            "text": f"[replying to {ref_author}: {ref_text}]"
                        })
                except (discord.NotFound, discord.HTTPException):
                    pass  # Referenced message deleted or inaccessible

            # Add text if present
            if msg.content:
                # Webhook proxies (PluralKit) are user messages, so prefix with
                # the alter's display name like any other user.
                author_prefix = "" if _is_real_bot(msg) else f"{msg.author.display_name}: "
                text = msg.content
                # Normalize model labels: strip ALL label formats (bold and plain),
                # then re-add a single clean plain-text label for identity.
                # This prevents accumulation from either format.
                if _is_real_bot(msg):
                    # First, extract which model this is from (check bold first, then plain)
                    model_label = None
                    label_match = re.match(r'^(?:\*\*\[(Claude|Deepseek|Gemini)\]\*\*\s*|\[(Claude|Deepseek|Gemini)\]\s*)+', text)
                    if label_match:
                        # Get the last model name captured (from either group)
                        model_label = label_match.group(1) or label_match.group(2)
                        text = text[label_match.end():]
                    # Re-add a single clean label
                    if model_label:
                        text = f"[{model_label}] {text}"
                content.append({
                    "type": "text",
                    "text": f"{author_prefix}{text}"
                })
            
            # Add images if present
            for attachment in msg.attachments:
                if any(attachment.filename.lower().endswith(ext) for ext in CONFIG.image_types):
                    if attachment.size <= CONFIG.max_image_size_mb * 1024 * 1024:
                        try:
                            image_data = await self._fetch_image_base64(attachment.url)
                            if image_data:
                                # Detect media type
                                ext = attachment.filename.lower().split('.')[-1]
                                media_type = {
                                    'png': 'image/png',
                                    'jpg': 'image/jpeg', 
                                    'jpeg': 'image/jpeg',
                                    'gif': 'image/gif',
                                    'webp': 'image/webp'
                                }.get(ext, 'image/png')
                                
                                content.append({
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": media_type,
                                        "data": image_data
                                    }
                                })
                        except Exception as e:
                            content.append({
                                "type": "text", 
                                "text": f"[Image attachment: {attachment.filename} - failed to load]"
                            })
                
                # Handle text files
                elif any(attachment.filename.lower().endswith(ext) for ext in CONFIG.text_file_types):
                    if attachment.size <= 1024 * 1024:  # 1MB limit for text files
                        try:
                            file_content = await self._fetch_text_file(attachment.url)
                            if file_content:
                                content.append({
                                    "type": "text",
                                    "text": f"\n--- File: {attachment.filename} ---\n{file_content}\n--- End of {attachment.filename} ---\n"
                                })
                        except Exception as e:
                            content.append({
                                "type": "text",
                                "text": f"[Text file: {attachment.filename} - failed to load: {e}]"
                            })
            
            if content:
                # Webhook proxies count as "user" — the human is upstream of the alter.
                role = "assistant" if _is_real_bot(msg) else "user"

                # Simplify if just text. _msg_id is internal-only metadata used
                # by the thinking-mode reasoning cache; strip before API calls.
                if len(content) == 1 and content[0]["type"] == "text":
                    messages.append({"role": role, "content": content[0]["text"], "_msg_id": msg.id})
                else:
                    messages.append({"role": role, "content": content, "_msg_id": msg.id})

        # Reverse so oldest first (Discord returns newest first)
        messages.reverse()
        
        # Ensure conversation starts with user message (API requirement)
        while messages and messages[0]["role"] == "assistant":
            messages.pop(0)
        
        return messages
    
    async def _fetch_image_base64(self, url: str) -> Optional[str]:
        """Fetch image from URL and return base64 encoded."""
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    return base64.b64encode(data).decode('utf-8')
        return None
    
    async def _fetch_text_file(self, url: str) -> Optional[str]:
        """Fetch text file from URL and return contents."""
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    # Try UTF-8 first, fall back to latin-1
                    try:
                        return data.decode('utf-8')
                    except UnicodeDecodeError:
                        return data.decode('latin-1')
        return None
    
    def estimate_tokens(self, messages: list[dict], guild_id: int, channel_id: Optional[int] = None) -> int:
        """Estimate context size in tokens (includes loaded reading material if any)."""
        total_chars = len(CONFIG.system_prompt)

        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total_chars += len(content)
            elif isinstance(content, list):
                for part in content:
                    if part.get("type") == "text":
                        total_chars += len(part.get("text", ""))
                    elif part.get("type") == "image":
                        total_chars += 1000  # Rough estimate for image tokens

        memory_str = self.memories[guild_id].get_context_string()
        total_chars += len(memory_str)

        # Reading material adds substantial weight (often the dominant term).
        if channel_id is not None and channel_id in self.reading_materials:
            total_chars += len(self.reading_materials[channel_id].text)

        return int(total_chars / CONFIG.chars_per_token)

    def get_context_info(self, messages: list[dict], guild_id: int, channel_id: Optional[int] = None) -> str:
        """Get human-readable context info."""
        msg_count = len(messages)
        memory = self.memories[guild_id]
        working_count = len(memory.working.notes)
        longterm_count = len(memory.longterm.entries)
        est_tokens = self.estimate_tokens(messages, guild_id, channel_id=channel_id)
        # Use Claude pricing as worst-case estimate
        est_cost = (est_tokens / 1_000_000) * CLAUDE_PROVIDER.input_cost_per_million

        material_note = ""
        if channel_id is not None and channel_id in self.reading_materials:
            mat = self.reading_materials[channel_id]
            material_note = f", 📚 {mat.title} loaded (~{mat.estimated_tokens:,} tokens)"

        return (
            f"📊 Context: {msg_count} messages, "
            f"{working_count}/{CONFIG.max_working_notes} working notes, "
            f"{longterm_count}/{CONFIG.max_longterm_memories} long-term memories, "
            f"~{est_tokens:,} tokens (~${est_cost:.3f} worst-case){material_note}"
        )
    
    def get_cost_summary(self, providers: list[ModelProvider]) -> str:
        """Get total cost summary across all models."""
        lines = ["💰 **Cost Summary**"]
        grand_total = 0.0

        for p in providers:
            if p.total_requests == 0:
                continue
            cost = p.get_cost()
            grand_total += cost
            lines.append(
                f"  **{p.name}**: {p.total_requests} requests, "
                f"{p.total_input_tokens:,} in + {p.total_output_tokens:,} out = "
                f"${cost:.4f}"
            )

        if grand_total == 0:
            return "💰 No API calls made yet."

        lines.append(f"\n  **Total**: ${grand_total:.4f}")
        return "\n".join(lines)
    
    def save_memories(self, filepath: str = "memories.json", providers: list[ModelProvider] = None) -> None:
        """Save all memories to disk (synchronous - use save_memories_async in async contexts)."""
        data = {
            str(guild_id): memory.to_dict()
            for guild_id, memory in self.memories.items()
        }
        data["_calibration"] = self.calibration.to_dict()
        if providers:
            data["_model_stats"] = {
                p.name: p.to_stats_dict() for p in providers
            }
        # Reading materials (bookclub mode). Stored under a metadata key so
        # we don't collide with guild_ids. Persisted because a fic loaded
        # via !load should survive a bot restart.
        if self.reading_materials:
            data["_reading_materials"] = {
                str(channel_id): material.to_dict()
                for channel_id, material in self.reading_materials.items()
            }
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)
        self._memories_dirty = False
    
    async def save_memories_async(self, filepath: str = "memories.json", providers: list[ModelProvider] = None) -> None:
        """Save memories without blocking the event loop."""
        await asyncio.to_thread(self.save_memories, filepath, providers)
    
    def mark_dirty(self) -> None:
        """Mark memories as needing to be saved."""
        self._memories_dirty = True
    
    @property
    def needs_save(self) -> bool:
        """Check if memories need saving."""
        return getattr(self, '_memories_dirty', False)
    
    def load_memories(self, filepath: str = "memories.json", providers: list[ModelProvider] = None) -> None:
        """Load memories from disk."""
        try:
            with open(filepath, 'r') as f:
                data = json.load(f)

            guild_count = 0
            for key, value in data.items():
                if key.startswith("_"):
                    continue  # Skip metadata keys
                self.memories[int(key)] = TwoTierMemory.from_dict(
                    value,
                    max_working_notes=CONFIG.max_working_notes,
                    max_longterm_entries=CONFIG.max_longterm_memories,
                    working_decay_hours=CONFIG.working_memory_decay_hours
                )
                guild_count += 1

            # Load calibration data
            if "_calibration" in data:
                self.calibration = CalibrationTracker.from_dict(data["_calibration"])

            # Load model stats
            if "_model_stats" in data and providers:
                for p in providers:
                    if p.name in data["_model_stats"]:
                        p.load_stats(data["_model_stats"][p.name])

            # Load reading materials (bookclub mode)
            material_count = 0
            if "_reading_materials" in data:
                for ch_id_str, material_data in data["_reading_materials"].items():
                    try:
                        self.reading_materials[int(ch_id_str)] = ReadingMaterial.from_dict(material_data)
                        material_count += 1
                    except (KeyError, ValueError) as e:
                        print(f"⚠️  Skipping malformed reading material for channel {ch_id_str}: {e}")

            print(f"Loaded memories for {guild_count} guilds" +
                  (f", {material_count} reading material(s)" if material_count else ""))
        except FileNotFoundError:
            print("No existing memories file, starting fresh")

# =============================================================================
# THE BOT
# =============================================================================

class ClaudeBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True  # PRIVILEGED INTENT - enable in portal!
        intents.guilds = True
        intents.guild_reactions = True  # For reaction handling

        super().__init__(command_prefix="!", intents=intents)

        # Model providers
        self.claude_provider = CLAUDE_PROVIDER
        self.deepseek_provider = DEEPSEEK_PROVIDER
        self.gemini_provider = GEMINI_PROVIDER

        # Anthropic client
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        if anthropic_key:
            self.claude_client = anthropic.Anthropic(api_key=anthropic_key)
            self.claude_provider.enabled = True
            print(f"🟢 Claude enabled (model: {self.claude_provider.model_id})")
        else:
            self.claude_client = None
            self.claude_provider.enabled = False
            print("⚪ Claude not configured (ANTHROPIC_API_KEY missing)")

        # Deepseek client (OpenAI-compatible, optional)
        deepseek_key = os.getenv("DEEPSEEK_API_KEY")
        if deepseek_key:
            from openai import OpenAI
            self.deepseek_client = OpenAI(
                api_key=deepseek_key,
                base_url="https://api.deepseek.com"
            )
            self.deepseek_provider.enabled = True
            print(f"🟢 Deepseek enabled (model: {self.deepseek_provider.model_id})")
        else:
            self.deepseek_client = None
            self.deepseek_provider.enabled = False
            print("⚪ Deepseek not configured (DEEPSEEK_API_KEY missing)")

        # Gemini client (OpenAI-compatible via Google's shim, optional)
        gemini_key = os.getenv("GEMINI_API_KEY")
        if gemini_key:
            from openai import OpenAI
            self.gemini_client = OpenAI(
                api_key=gemini_key,
                base_url="https://generativelanguage.googleapis.com/v1beta/openai/"
            )
            self.gemini_provider.enabled = True
            print(f"🟢 Gemini enabled (model: {self.gemini_provider.model_id})")
        else:
            self.gemini_client = None
            self.gemini_provider.enabled = False
            print("⚪ Gemini not configured (GEMINI_API_KEY missing)")

        # Tavily search client (optional - enables web search for Deepseek).
        # Gemini uses Google's native grounding (no Tavily needed); see
        # _google_native_search and GEMINI_PROVIDER.search_backend="google_native".
        tavily_key = os.getenv("TAVILY_API_KEY")
        if tavily_key:
            from tavily import TavilyClient
            self.tavily_client = TavilyClient(api_key=tavily_key)
            print("🟢 Tavily web search enabled")
        else:
            self.tavily_client = None
            print("⚪ Tavily not configured (Deepseek web search disabled)")

        # All providers list (for iteration). Order matters: this is the order
        # !models lists them and the order three-way routing iterates ties.
        self.providers = [
            self.claude_provider,
            self.deepseek_provider,
            self.gemini_provider,
        ]

        # Map provider → OpenAI-compatible client so the generic generator can
        # dispatch without name-matching. Claude uses the anthropic SDK directly.
        self.openai_compatible_clients: dict[str, object] = {
            self.deepseek_provider.name: self.deepseek_client,
            self.gemini_provider.name: self.gemini_client,
        }

        # Conversation manager
        self.manager = ConversationManager()

        # Per-channel model preferences
        self.channel_preferences: dict[int, str] = {}

        # Reasoning cache for thinking-mode multi-turn (keyed by Discord msg.id).
        # Deepseek requires reasoning_content to be echoed back on every prior
        # assistant turn whenever thinking mode is enabled on the current call.
        self.reasoning_cache: OrderedDict[int, str] = OrderedDict()

        # Allowed channels (load from config)
        self.allowed_channels: set[int] = set()
        self._load_config()

    @property
    def multi_model_active(self) -> bool:
        """True if more than one model is enabled."""
        return sum(1 for p in self.providers if p.enabled) > 1

    REASONING_CACHE_MAX = 500
    THINK_PREFIXES = ("!think",)
    CLAUDE_PREFIXES = ("!claude", "!opus", "!balthasar")
    DEEPSEEK_PREFIXES = ("!deepseek", "!melchior")
    GEMINI_PREFIXES = ("!gemini", "!caspar")
    CLAUDE_THINKING_EFFORT = "high"  # low | medium | high | xhigh | max
    CLAUDE_THINKING_MAX_TOKENS = 16000

    def _store_reasoning(self, msg_id: int, content: str) -> None:
        """Cache reasoning_content under a Discord message id (FIFO eviction)."""
        if not content:
            return
        self.reasoning_cache[msg_id] = content
        self.reasoning_cache.move_to_end(msg_id)
        while len(self.reasoning_cache) > self.REASONING_CACHE_MAX:
            self.reasoning_cache.popitem(last=False)

    def _get_reasoning(self, msg_id: int) -> str:
        """Look up cached reasoning_content for a Discord message id, or empty string."""
        return self.reasoning_cache.get(msg_id, "")

    async def _prev_bot_used_thinking(self, channel: discord.abc.Messageable) -> bool:
        """Did our most recent message in this channel use extended thinking?

        Behavioral-momentum signal for `_pick_effort`: conversations that
        had depth in the previous turn usually warrant depth in the next.
        Walks up to ~10 messages back, finds the most recent message
        authored by this bot, and checks if its reasoning was cached.
        """
        if self.user is None:
            return False
        try:
            async for msg in channel.history(limit=10):
                if _is_real_bot(msg) and msg.author.id == self.user.id:
                    return bool(self.reasoning_cache.get(msg.id))
        except (discord.HTTPException, AttributeError):
            return False
        return False

    @staticmethod
    def _pick_effort(text: str, prev_used_thinking: bool = False) -> Optional[str]:
        """Classify a prompt into an Opus 4.7 thinking-effort level.

        Returns None | "high" | "xhigh" | "max". None means thinking off
        (cheap chat path). Casual register and first-person emotional
        framing are anti-signals (not hard skips) — a hard question dressed
        in casual or emotional language can still pull the score above
        threshold with strong textual cues.
        """
        if not text:
            return None
        text_lower = text.lower()
        score = 0

        # --- Anti-signals (penalty, not veto) ---
        # Conversational opener. "lol so why does X" can still route to
        # thinking if the question itself has strong signals.
        if re.match(r"^\s*(lol|lmao|lmfao|wait what|huh|wtf|same|nice|cool|ok(ay)?|right)\b", text_lower):
            score -= 2
        # First-person emotional framing. A meaty question wrapped in feelings
        # (Lauren-style "my world used to not be full of depressed people...")
        # should still get analytical depth if the strong signals are there.
        if re.search(r"\b(i feel|i'?m feeling|i'?m (sad|depressed|anxious|scared|worried|tired|stressed|lonely))\b", text_lower):
            score -= 2

        # --- Strong signals (depth almost always rewarded) ---
        # Construction/derivation verbs, including inflections. "prov(e|es|
        # ed|ing|en)" is enumerated to avoid matching "provide"/"province".
        if re.search(r"\b(deriv\w*|prov(e|es|ed|ing|en)|design\w*|architect\w*|refactor\w*|debug\w*)\b", text_lower):
            score += 3
        # Large code blocks: model has to actually read them.
        code_blocks = re.findall(r"```[\s\S]*?```", text)
        if code_blocks and max(b.count("\n") for b in code_blocks) >= 50:
            score += 3
        # Stack traces (Python / JVM-style).
        if re.search(r"Traceback \(most recent call last\)|\sat [\w.$]+\(.*:\d+\)", text):
            score += 3
        # Compound questions: multiple "?" or explicit chaining.
        if text.count("?") >= 2 or re.search(r"\b(and also|but (what )?about|also,?\s+what about)\b", text_lower):
            score += 2
        # Math/LaTeX. {2,} avoids matching \n, \t inside pasted code.
        if re.search(r"\\[a-zA-Z]{2,}\b|=.*[+\-*/].*=", text):
            score += 2
        # Comparative / trade-off framing.
        if re.search(r"\btrade.?offs?\b|\b(when would you|what'?s the difference between|compare and contrast)\b|\bvs\.?\s+\w+", text_lower):
            score += 2

        # --- Medium signals ---
        if re.search(r"\b(why does|why is|why doesn.?t|why isn.?t|explain (why|how)|analy[sz]e)\b|\bhow does .{0,40}work\b", text_lower):
            score += 1
        if re.search(r"\b(step.by.step|walk me through|carefully|thoroughly|in.depth|from\s+(scratch|first\s+principles))\b", text_lower):
            score += 2
        if len(text) > 2000:
            score += 2

        # Behavioral momentum: prior turn used thinking → conversation has depth.
        if prev_used_thinking:
            score += 1

        if score >= 6:
            return "max"
        if score >= 3:
            return "xhigh"
        if score >= 1:
            return "high"
        return None

    VALID_EFFORTS = ("low", "medium", "high", "xhigh", "max")

    def _peel_prefixes(self, content: str) -> tuple[str, set[str], Optional[str]]:
        """Strip stacked !think / !claude / !opus / !deepseek / !gemini prefixes in any order.

        Returns (remaining_content, set_of_flags, forced_effort).
        - flags: subset of {'think', 'claude', 'deepseek', 'gemini'}. !opus and
          !balthasar collapse to 'claude'; !melchior to 'deepseek'; !caspar to 'gemini'.
        - forced_effort: set when user used `!think:<level>` syntax (low | medium |
          high | xhigh | max). Implies the 'think' flag.
        """
        flags: set[str] = set()
        forced_effort: Optional[str] = None
        all_prefixes = (
            self.THINK_PREFIXES
            + self.CLAUDE_PREFIXES
            + self.DEEPSEEK_PREFIXES
            + self.GEMINI_PREFIXES
        )
        while True:
            stripped = content.strip()
            lower = stripped.lower()
            matched = False

            # Try `!think:<level>` first so it wins over the bare `!think` match.
            for think_prefix in self.THINK_PREFIXES:
                for level in self.VALID_EFFORTS:
                    tok = f"{think_prefix}:{level}"
                    if lower == tok:
                        rest = ""
                    elif lower.startswith(tok + " "):
                        rest = stripped[len(tok):].lstrip()
                    else:
                        continue
                    flags.add("think")
                    forced_effort = level
                    content = rest
                    matched = True
                    break
                if matched:
                    break
            if matched:
                continue

            for prefix in all_prefixes:
                if lower == prefix:
                    rest = ""
                elif lower.startswith(prefix + " "):
                    rest = stripped[len(prefix):].lstrip()
                else:
                    continue
                if prefix in self.THINK_PREFIXES:
                    flags.add("think")
                elif prefix in self.CLAUDE_PREFIXES:
                    flags.add("claude")
                elif prefix in self.DEEPSEEK_PREFIXES:
                    flags.add("deepseek")
                elif prefix in self.GEMINI_PREFIXES:
                    flags.add("gemini")
                content = rest
                matched = True
                break
            if not matched:
                break
        return content, flags, forced_effort

    @staticmethod
    def _strip_internal_keys(messages: list[dict]) -> list[dict]:
        """Drop internal-only keys (prefixed with _) before sending to provider APIs."""
        return [{k: v for k, v in msg.items() if not k.startswith("_")} for msg in messages]

    def _load_config(self) -> None:
        """Load configuration from config.json."""
        try:
            with open('config.json', 'r') as f:
                config = json.load(f)
                self.allowed_channels = set(config.get('allowed_channels', []))
                CONFIG.default_model = config.get('default_model', 'auto')
                # Load channel preferences
                for ch_id_str, model_name in config.get('channel_preferences', {}).items():
                    self.channel_preferences[int(ch_id_str)] = model_name
                print(f"Loaded {len(self.allowed_channels)} allowed channels")
                if CONFIG.default_model != "auto":
                    print(f"   Default model: {CONFIG.default_model}")
        except FileNotFoundError:
            print("⚠️  No config.json found! Create one with {'allowed_channels': [channel_ids]}")

    async def setup_hook(self) -> None:
        """Called when bot is ready."""
        self.manager.load_memories(providers=self.providers)
        # Start background save task
        self._save_task = self.loop.create_task(self._periodic_save())
    
    async def _periodic_save(self) -> None:
        """Background task to save memories every 60 seconds if dirty."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                if self.manager.needs_save:
                    await self.manager.save_memories_async(providers=self.providers)
                    print("💾 Memories saved (background)")
            except Exception as e:
                print(f"⚠️  Error saving memories: {e}")
            await asyncio.sleep(60)  # Check every 60 seconds

    async def close(self) -> None:
        """Clean shutdown - save memories before closing."""
        if self.manager.needs_save:
            print("💾 Saving memories before shutdown...")
            await self.manager.save_memories_async(providers=self.providers)
        await super().close()

    async def on_ready(self) -> None:
        print(f"✅ Logged in as {self.user}")
        print(f"📋 Allowed channels: {self.allowed_channels}")
        models = [p.name for p in self.providers if p.enabled]
        print(f"🧠 Models: {', '.join(models)} (selection: {CONFIG.default_model})")
    
    async def on_message(self, message: discord.Message) -> None:
        # Ignore self
        if message.author == self.user:
            return
        
        # Ignore DMs for now
        if not message.guild:
            return
        
        # Ignore system messages (pins, joins, boosts, etc)
        if message.type != discord.MessageType.default and message.type != discord.MessageType.reply:
            return
        
        # Check if in allowed channel or thread of allowed channel
        channel_id = message.channel.id
        parent_id = getattr(message.channel, 'parent_id', None)
        
        if channel_id not in self.allowed_channels and parent_id not in self.allowed_channels:
            return

        # PluralKit / webhook-proxy compatibility.
        # If this is a normal user message, pause briefly so PluralKit (or any
        # similar proxy) can delete-and-repost it as the alter. If the original
        # is gone after the pause, drop this event — Discord will fire another
        # on_message for the webhook version and we'll handle it there. Skip
        # the wait for webhook messages themselves (they're already the proxy)
        # and for our own bot (already filtered above, but be defensive).
        if message.webhook_id is None and not message.author.bot:
            await asyncio.sleep(CONFIG.proxy_check_delay_seconds)
            try:
                await message.channel.fetch_message(message.id)
            except discord.NotFound:
                return  # Proxied away; the webhook event will pick it up.
            except discord.HTTPException:
                pass  # Transient API blip — proceed anyway rather than dropping the turn.

        # Peel any stacked model/thinking prefixes (!think / !think:<level> /
        # !claude / !opus / !deepseek / !gemini and the MAGI aliases)
        original_content = message.content or ""
        peeled_content, flags, forced_effort = self._peel_prefixes(original_content)
        forced_thinking = "think" in flags

        if "claude" in flags and not self.claude_provider.enabled:
            await message.channel.send("❌ Claude is not configured (no API key).")
            return
        if "deepseek" in flags and not self.deepseek_provider.enabled:
            await message.channel.send("❌ Deepseek is not configured (no API key).")
            return
        if "gemini" in flags and not self.gemini_provider.enabled:
            await message.channel.send("❌ Gemini is not configured (no API key).")
            return

        forced_provider = None
        routing_reason = ""
        if "claude" in flags:
            forced_provider = self.claude_provider
            routing_reason = "User directly invoked Claude with !claude/!opus/!balthasar."
        elif "deepseek" in flags:
            forced_provider = self.deepseek_provider
            routing_reason = "User directly invoked Deepseek with !deepseek/!melchior."
        elif "gemini" in flags:
            forced_provider = self.gemini_provider
            routing_reason = "User directly invoked Gemini with !gemini/!caspar."

        if flags:
            message.content = peeled_content

        # Handle commands (but not if we just consumed a model/thinking prefix)
        if not flags and message.content.startswith('!'):
            await self._handle_command(message)
            return

        # Ignore empty messages (no text, no attachments)
        if not message.content and not message.attachments:
            return

        # Get or create thread
        thread, is_new_thread = await self._ensure_thread(message)

        # Select which model responds (forced or auto)
        if forced_provider:
            provider = forced_provider
        else:
            provider, routing_reason = await self._select_model(message, message.guild.id)

        # Decide effort level. Priority: manual !think:<level> > auto-classify
        # > class default. Auto-classify only when user explicitly chose Claude
        # (forced_provider) — we don't want to silently flip auto-routed turns
        # into thinking mode.
        chosen_effort: Optional[str] = forced_effort
        if forced_effort:
            routing_reason = (routing_reason + f" User-set effort={forced_effort}.").strip()
        elif not forced_thinking and forced_provider is self.claude_provider:
            prev_thinking = await self._prev_bot_used_thinking(message.channel)
            auto_effort = self._pick_effort(message.content or "", prev_used_thinking=prev_thinking)
            if auto_effort:
                forced_thinking = True
                chosen_effort = auto_effort
                routing_reason = (routing_reason + f" Auto-enabled thinking (effort={auto_effort}).").strip()

        # Generate response
        async with thread.typing():
            response, reactions, reasoning = await self._generate_response(
                thread,
                message.guild.id,
                initial_message=message if is_new_thread else None,
                provider=provider,
                routing_reason=routing_reason,
                thinking=forced_thinking,
                effort=chosen_effort,
            )

        # Label the response with model name (only when multi-model is active).
        # When thinking was used on Claude, also tag the chosen effort level so
        # users can see what depth the response was generated at without having
        # to set it explicitly per turn.
        if self.multi_model_active:
            # Strip any label the model echoed (handles old [Name] and the new
            # [Name · effort] format Claude may have started echoing).
            response = re.sub(
                r'^(?:\*\*\[(?:Claude|Deepseek|Gemini)(?:\s·\s\w+)?\]\*\*\s*|\[(?:Claude|Deepseek|Gemini)(?:\s·\s\w+)?\]\s*)+',
                '',
                response,
            )
            label = provider.name
            if forced_thinking and provider is self.claude_provider:
                label = f"{label} · {chosen_effort or self.CLAUDE_THINKING_EFFORT}"
            response = f"**[{label}]** {response}"

        # Handle reactions
        for emoji in reactions:
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException:
                pass

        # Extract and handle code files
        response, files = self._extract_code_files(response)

        # Render any LaTeX math blocks to PNGs (Discord has no native math
        # rendering). Source text stays in the message body so users can copy
        # it. Discord caps at 10 attachments per message; share the budget
        # with code files.
        latex_files = self._render_latex_attachments(response, max_files=10 - len(files))
        files.extend(latex_files)

        # Send response (handle Discord's 2000 char limit)
        sent_msg = await self._send_response(thread, response, files)

        # Cache reasoning_content keyed by the sent Discord message id so the
        # next thinking-mode turn can echo it back to Deepseek's API.
        if reasoning and sent_msg is not None:
            self._store_reasoning(sent_msg.id, reasoning)

        # Record calibration bid
        confidence = self._estimate_confidence(message.content or "", provider)
        record_idx = self.manager.calibration.record_bid(provider.name, confidence)
        self.manager.last_response_model[thread.id] = provider.name
        self.manager.last_response_index[thread.id] = record_idx

        # Mark memories as needing save (actual save happens in background task)
        self.manager.mark_dirty()
    
    async def on_reaction_add(self, reaction: discord.Reaction, user: discord.User) -> None:
        """Track user feedback on bot responses for calibration."""
        if user == self.user:
            return
        if reaction.message.author != self.user:
            return

        channel_id = reaction.message.channel.id
        if channel_id not in self.manager.last_response_index:
            return

        emoji = str(reaction.emoji)
        # Positive: thumbs up, heart, fire, check, joy, sparkling heart, 100
        good_emoji = ('\U0001f44d', '\u2764\ufe0f', '\U0001f525', '\u2705',
                      '\U0001f602', '\U0001f496', '\U0001f4af')
        # Negative: thumbs down, x, confused
        bad_emoji = ('\U0001f44e', '\u274c', '\U0001f615')
        if emoji in good_emoji:
            self.manager.calibration.record_feedback(
                self.manager.last_response_index[channel_id], "good"
            )
        elif emoji in bad_emoji:
            self.manager.calibration.record_feedback(
                self.manager.last_response_index[channel_id], "bad"
            )

    async def _ensure_thread(self, message: discord.Message) -> tuple[discord.Thread, bool]:
        """Get existing thread or create new one. Returns (thread, is_new)."""
        if isinstance(message.channel, discord.Thread):
            return message.channel, False
        
        # Create new thread
        thread = await message.create_thread(
            name=f"Chat with {message.author.display_name}",
            auto_archive_duration=60
        )
        await thread.send(
            f"🧵 Started new conversation!\n"
            f"Commands: `!help` for full list"
        )
        return thread, True

    # ----- Multi-model support methods -----

    def _strip_images_from_messages(self, messages: list[dict]) -> list[dict]:
        """Remove image content from messages for text-only models.

        Only call this when provider.supports_vision is False — Gemini and Claude
        both handle images natively and shouldn't go through this stripping.
        Deepseek is currently the only text-only provider in the system.
        """
        stripped = []
        for msg in messages:
            content = msg["content"]
            msg_id = msg.get("_msg_id")
            if isinstance(content, str):
                stripped.append(msg)
            elif isinstance(content, list):
                text_parts = [b for b in content if b.get("type") == "text"]
                if text_parts:
                    if len(text_parts) == 1:
                        new = {"role": msg["role"], "content": text_parts[0]["text"]}
                    else:
                        new = {"role": msg["role"], "content": text_parts}
                    if msg_id is not None:
                        new["_msg_id"] = msg_id
                    stripped.append(new)
                elif any(b.get("type") == "image" for b in content):
                    new = {"role": msg["role"], "content": "[An image was shared]"}
                    if msg_id is not None:
                        new["_msg_id"] = msg_id
                    stripped.append(new)
            else:
                stripped.append(msg)
        return stripped

    def _convert_messages_to_openai_format(self, messages: list[dict]) -> list[dict]:
        """Convert Anthropic-format messages to OpenAI chat format."""
        converted = []
        for msg in messages:
            content = msg["content"]
            msg_id = msg.get("_msg_id")
            if isinstance(content, str):
                new = {"role": msg["role"], "content": content}
                if msg_id is not None:
                    new["_msg_id"] = msg_id
                converted.append(new)
            elif isinstance(content, list):
                parts = []
                for block in content:
                    if block.get("type") == "text":
                        parts.append({"type": "text", "text": block["text"]})
                    elif block.get("type") == "image":
                        source = block.get("source", {})
                        media_type = source.get("media_type", "image/png")
                        data = source.get("data", "")
                        parts.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{data}"}
                        })
                if parts:
                    new = {"role": msg["role"], "content": parts}
                    if msg_id is not None:
                        new["_msg_id"] = msg_id
                    converted.append(new)
            else:
                converted.append(msg)
        return converted

    def _build_system_prompt(self, provider: ModelProvider, routing_reason: str = "") -> str:
        """Build system prompt tailored to the provider, with identity and routing context."""
        # Model identity line
        if provider.name == "Claude":
            identity = f"Claude (model: {provider.model_id}), an AI assistant made by Anthropic"
            identity_details = (
                "**[Deepseek]** messages are from your collaborator Deepseek (the fast, cheap, "
                "CJK-strong one) and **[Gemini]** messages are from your collaborator Gemini "
                "(the abstract-reasoning specialist). Both are different models, not you — "
                "your responses are labeled **[Claude]** by the bot. Your capabilities include "
                "vision (you can see images) and built-in web search — you can search the web "
                "anytime you think it would help, not just when users use !search. You tend "
                "to shine at complex analysis, code review, creative writing, and nuance."
            )
        elif provider.name == "Deepseek":
            identity = f"Deepseek (model: {provider.model_id}), an AI assistant made by DeepSeek"
            identity_details = (
                "**[Claude]** messages are from your collaborator Claude (the careful, "
                "thorough one) and **[Gemini]** messages are from your collaborator Gemini "
                "(the abstract-reasoning specialist with vision). Both are different models, "
                "not you. You can't see images, but you can search the web via Tavily "
                "function calling. You tend to shine at fast responses, factual questions, "
                "casual chat, and cost-efficiency.\n\n"
                "**Chinese language specialty**: You were trained on deep Chinese internet data "
                "(Zhihu, Baidu Baike, CSDN, Weibo, Douban, etc.) and have a much richer understanding "
                "of Chinese than Claude does. When Chinese text appears in conversation, it's your job "
                "to translate it to English for the group. When you think it's relevant or fun, include "
                "little mini-lessons breaking down interesting characters or words — e.g., how a character "
                "is composed, what its radicals mean, etymological tidbits, or how a phrase differs from "
                "its literal translation. Keep the lessons bite-sized and natural, not lecture-y.\n\n"
                "**Important: Always respond in English.** You can use Chinese characters inline when "
                "showing original text, breaking down words, or when a concept has no clean English "
                "equivalent — but your response itself should always be in English. Never reply with "
                "a wall of Chinese text. Your job is to be a bridge between languages, not to exclude "
                "English speakers from the conversation.\n\n"
                "**Formatting**: Write in flowing prose, not listicles. Avoid walls of bullet points, "
                "numbered lists, tables, and headers unless the user specifically asks for structured "
                "output. Keep it conversational — this is Discord chat, not a report. "
                "Minimize blank lines between paragraphs."
            )
        elif provider.name == "Gemini":
            identity = f"Gemini (model: {provider.model_id}), an AI assistant made by Google DeepMind"
            identity_details = (
                "**[Claude]** messages are from your collaborator Claude (the careful, "
                "thorough one with strong code review and multi-tool orchestration) and "
                "**[Deepseek]** messages are from your collaborator Deepseek (the fast, "
                "cheap, CJK-strong one). Both are different models, not you. You can see "
                "images and you have native Google Search grounding — when you call the "
                "web_search tool, the system routes it through Google's own search index "
                "(not a third-party meta-search), and citations are returned as structured "
                "groundingMetadata that the bot renders as source embeds. You tend "
                "to shine at genuinely novel reasoning (the kind ARC-AGI-2 tests), abstract "
                "pattern-finding, long-context synthesis, multi-step math, and questions "
                "where the answer requires recombining ideas in a non-obvious way.\n\n"
                "**Formatting**: Write in flowing prose, not listicles. Avoid walls of "
                "bullet points, numbered lists, tables, and headers unless the user "
                "specifically asks for structured output. Keep it conversational — this "
                "is Discord chat, not a slide deck. Minimize blank lines between paragraphs."
            )
        else:
            identity = f"{provider.name} (model: {provider.model_id}), an AI assistant"
            identity_details = ""

        # Routing context
        if routing_reason:
            routing_context = f"**Why you were chosen for this message:** {routing_reason}"
        else:
            routing_context = ""

        prompt = CONFIG.system_prompt
        prompt = prompt.replace("{model_identity}", identity)
        prompt = prompt.replace("{model_name}", provider.name)
        prompt = prompt.replace("{model_id}", provider.model_id)
        prompt = prompt.replace("{identity_details}", identity_details)
        prompt = prompt.replace("{routing_context}", routing_context)
        return prompt

    # OpenAI-compatible function-calling tool definition.
    # Shared by both Deepseek and Gemini — both route web search through Tavily
    # over the OpenAI shim's tool-calling support. (Gemini also has native
    # google_search grounding, but that requires the native API, not the shim.)
    OPENAI_COMPATIBLE_TOOLS = [{
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current information. Use this when you need up-to-date facts, news, or information you don't have.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query"
                    }
                },
                "required": ["query"]
            }
        }
    }]

    async def _tavily_search(self, query: str, max_results: int = 5) -> SearchResult:
        """Perform a web search via Tavily. Returns raw hits (not synthesized).

        SearchResult.is_grounded_answer is False — callers should feed .text
        back through a model for synthesis if they want a coherent answer.
        """
        if not self.tavily_client:
            return SearchResult(
                text="Web search is not available (no Tavily API key configured).",
            )
        try:
            result = await asyncio.to_thread(
                self.tavily_client.search,
                query=query,
                max_results=max_results,
            )
            if not result.get("results"):
                return SearchResult(text=f"No results found for: {query}")

            citations: list[dict] = []
            lines: list[str] = []
            for r in result["results"]:
                title = r.get("title", "Untitled")
                url = r.get("url", "")
                snippet = r.get("content", "")
                citations.append({"url": url, "title": title, "snippet": snippet})
                lines.append(f"**{title}**\n{url}\n{snippet}\n")
            return SearchResult(
                text="\n".join(lines),
                citations=citations,
                is_grounded_answer=False,
                queries_used=[query],
            )
        except Exception as e:
            return SearchResult(text=f"Search error: {e}")

    async def _google_native_search(self, query: str, max_results: int = 5) -> SearchResult:
        """Perform a web search via Gemini's native google_search grounding.

        Returns a SearchResult where is_grounded_answer=True — the response is
        already a synthesized answer with citations from groundingMetadata.
        Callers should display .text directly and render .citations as embeds.

        Uses raw aiohttp against the native endpoint (no new SDK dep). This is
        the only Gemini code path that doesn't go through the OpenAI shim.
        """
        gemini_key = os.getenv("GEMINI_API_KEY")
        if not gemini_key:
            return SearchResult(
                text="Native Google search is not available (no GEMINI_API_KEY configured).",
            )
        model_id = self.gemini_provider.model_id
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model_id}:generateContent"
        )
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": gemini_key,
        }
        body = {
            "contents": [
                {"role": "user", "parts": [{"text": query}]}
            ],
            "tools": [{"google_search": {}}],
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=body, timeout=60) as resp:
                    if resp.status != 200:
                        err_text = await resp.text()
                        return SearchResult(
                            text=f"Google native search error (HTTP {resp.status}): {err_text[:500]}",
                        )
                    data = await resp.json()
        except asyncio.TimeoutError:
            return SearchResult(text="Google native search timed out.")
        except Exception as e:
            return SearchResult(text=f"Google native search error: {e}")

        # Parse response: candidates[0].content.parts[*].text + groundingMetadata
        candidates = data.get("candidates", [])
        if not candidates:
            return SearchResult(text=f"No results for: {query}")
        candidate = candidates[0]
        parts = candidate.get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            text = f"No textual response for: {query}"

        # Track usage if reported (Gemini returns usageMetadata at top level)
        usage = data.get("usageMetadata", {})
        prompt_tokens = usage.get("promptTokenCount", 0)
        output_tokens = usage.get("candidatesTokenCount", 0)
        if prompt_tokens or output_tokens:
            self.gemini_provider.record_usage(prompt_tokens, output_tokens)
            self.gemini_provider.total_requests += 1

        # Extract structured citations from groundingMetadata.groundingChunks
        grounding = candidate.get("groundingMetadata", {})
        citations: list[dict] = []
        seen_urls: set[str] = set()
        for chunk in grounding.get("groundingChunks", []):
            web = chunk.get("web", {})
            curl = web.get("uri", "")
            title = web.get("title", "")
            if curl and curl not in seen_urls:
                seen_urls.add(curl)
                citations.append({"url": curl, "title": title, "snippet": ""})
            if len(citations) >= max_results:
                break

        queries_used = grounding.get("webSearchQueries", [query])

        return SearchResult(
            text=text,
            citations=citations,
            is_grounded_answer=True,
            queries_used=queries_used,
        )

    async def _search_for(self, provider: ModelProvider, query: str, max_results: int = 5) -> SearchResult:
        """Dispatch a search to the provider's preferred SearchBackend.

        Falls back to Tavily if the provider's preferred backend isn't available.
        Returns an empty-ish SearchResult if nothing is configured.
        """
        backend = provider.search_backend
        if backend == "google_native" and os.getenv("GEMINI_API_KEY"):
            return await self._google_native_search(query, max_results=max_results)
        # Default / fallback: Tavily (if configured)
        if self.tavily_client:
            return await self._tavily_search(query, max_results=max_results)
        return SearchResult(
            text=f"No search backend available for {provider.name} "
                 "(set GEMINI_API_KEY for native grounding or TAVILY_API_KEY for Tavily).",
        )

    # ----- Reading material (bookclub mode) -----

    AO3_WORK_ID_RE = re.compile(r"archiveofourown\.org/works/(\d+)")

    @classmethod
    def _build_ao3_full_work_url(cls, url: str) -> Optional[str]:
        """Normalize any AO3 work URL to its full-work, adult-bypass form.

        Accepts: works/12345, works/12345/chapters/67890, with query strings, etc.
        Returns: works/12345?view_full_work=true&view_adult=true (or None if
        the URL doesn't match the AO3 work pattern).
        """
        match = cls.AO3_WORK_ID_RE.search(url)
        if not match:
            return None
        work_id = match.group(1)
        return f"https://archiveofourown.org/works/{work_id}?view_full_work=true&view_adult=true"

    async def _fetch_ao3_work(self, url: str) -> Optional[ReadingMaterial]:
        """Fetch an AO3 work and return a populated ReadingMaterial.

        Returns None if the URL isn't AO3, the fetch fails, or bs4 isn't
        installed. The full-work view is requested with adult-content gate
        bypassed.
        """
        if not _HAS_BS4:
            return None
        normalized = self._build_ao3_full_work_url(url)
        if not normalized:
            return None

        headers = {
            # AO3 returns 403 to default-aiohttp User-Agent strings; use a
            # bot-identifying UA that still includes a browser token.
            "User-Agent": "Mozilla/5.0 (compatible; HydraBot/1.0; AO3 bookclub fetcher)"
        }
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(normalized, timeout=60) as resp:
                    if resp.status != 200:
                        return None
                    html_text = await resp.text()
        except (asyncio.TimeoutError, aiohttp.ClientError):
            return None

        soup = _BeautifulSoup(html_text, "html.parser")

        # Work title (in preface metadata)
        title_tag = soup.select_one("h2.title.heading") or soup.select_one("h2.title")
        title = title_tag.get_text(strip=True) if title_tag else "Untitled AO3 Work"

        # Main chapter content lives in #chapters
        chapters_container = soup.select_one("#chapters")
        if chapters_container is None:
            return None

        body_parts: list[str] = []
        chapter_breaks: list[tuple[int, str]] = []
        chapter_divs = chapters_container.select("div.chapter")

        if chapter_divs:
            # Multi-chapter work — each <div class="chapter"> has its own
            # title + userstuff body.
            for idx, ch_div in enumerate(chapter_divs, 1):
                ch_title_tag = ch_div.select_one("h3.title")
                if ch_title_tag:
                    ch_title = ch_title_tag.get_text(" ", strip=True)
                else:
                    ch_title = f"Chapter {idx}"

                body_tag = ch_div.select_one("div.userstuff")
                body_text = body_tag.get_text("\n", strip=True) if body_tag else ""
                offset = sum(len(p) for p in body_parts)
                chapter_breaks.append((offset, ch_title))
                body_parts.append(f"## {ch_title}\n\n{body_text}\n\n")
        else:
            # Single-chapter / oneshot — one userstuff block under #chapters
            body_tag = chapters_container.select_one("div.userstuff")
            if body_tag:
                body_parts.append(body_tag.get_text("\n", strip=True))

        full_text = "".join(body_parts).strip()
        if not full_text:
            return None

        # Normalize whitespace and decode any lingering HTML entities
        full_text = re.sub(r"\n{3,}", "\n\n", full_text)
        full_text = _html.unescape(full_text)

        return ReadingMaterial(
            url=url,
            title=title,
            text=full_text,
            chapter_breaks=chapter_breaks,
        )

    @staticmethod
    def _build_reading_material_system_block(material: "ReadingMaterial") -> str:
        """Format a reading material as a system-prompt block.

        Used by all three providers — for Claude it becomes a separately
        cacheable block; for Deepseek/Gemini-fallback it gets prepended to
        the live system text. The framing tells the model to treat the text
        as primary source for any bookclub discussion.
        """
        return (
            f"## Reading Material: {material.title}\n\n"
            f"This text has been loaded for bookclub discussion in this channel "
            f"(source: {material.url}). You have full access to the work below — "
            f"reference specific passages, characters, plot points, and structural "
            f"choices freely. Treat it as the canonical source for any question "
            f"about the work. The text follows between the markers.\n\n"
            f"--- BEGIN WORK ---\n\n"
            f"{material.text}\n\n"
            f"--- END WORK ---"
        )

    async def _create_gemini_cache(
        self, material: "ReadingMaterial"
    ) -> Optional[tuple[str, datetime]]:
        """Create a Gemini cachedContents entry for a reading material.

        POSTs to /v1beta/cachedContents with the fic as a user/model exchange
        prefix and a 24-hour TTL. Returns (cache_name, expires_at) on success,
        None on failure. Costs are billed only for the cache storage TTL plus
        the cache-hit input pricing — much cheaper than uploading the fic each
        turn.
        """
        gemini_key = os.getenv("GEMINI_API_KEY")
        if not gemini_key:
            return None
        url = "https://generativelanguage.googleapis.com/v1beta/cachedContents"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": gemini_key,
        }
        fic_intro = (
            f"I'm going to share a work with you for a bookclub discussion. "
            f"It's titled '{material.title}' (source: {material.url}). "
            f"Here is the full text — please read it; I'll ask questions about "
            f"it afterwards.\n\n"
        )
        body = {
            "model": f"models/{self.gemini_provider.model_id}",
            "contents": [
                {
                    "role": "user",
                    "parts": [{"text": fic_intro + material.text}],
                },
                {
                    "role": "model",
                    "parts": [{"text":
                        "I've read the full work and have it loaded. Ready to "
                        "discuss whenever you'd like."
                    }],
                },
            ],
            "ttl": "86400s",  # 24 hours
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=body, timeout=180) as resp:
                    if resp.status != 200:
                        err_text = await resp.text()
                        print(f"⚠️  Gemini cache creation failed: HTTP {resp.status}: {err_text[:300]}")
                        return None
                    data = await resp.json()
        except (asyncio.TimeoutError, aiohttp.ClientError) as e:
            print(f"⚠️  Gemini cache creation network error: {e}")
            return None
        except Exception as e:
            print(f"⚠️  Gemini cache creation unexpected error: {e}")
            return None

        cache_name = data.get("name", "")
        expire_time_str = data.get("expireTime", "")
        if not cache_name:
            return None
        try:
            # ISO 8601 with Z suffix → fromisoformat needs +00:00
            expires_at = datetime.fromisoformat(expire_time_str.replace("Z", "+00:00"))
            # Strip tz for naive comparisons with our datetime.now() elsewhere
            expires_at = expires_at.replace(tzinfo=None)
        except (ValueError, AttributeError):
            expires_at = datetime.now() + timedelta(hours=24)
        print(f"🟢 Created Gemini cache {cache_name} (expires {expires_at:%Y-%m-%d %H:%M})")
        return cache_name, expires_at

    async def _ensure_gemini_cache(self, material: "ReadingMaterial") -> Optional[str]:
        """Get a Gemini cache handle for this material, creating one if needed.

        Reuses existing cache if it's still valid (with a 5-minute safety
        margin). Returns the cache name (e.g. "cachedContents/abc123") or None
        if creation failed and we should fall back to inline injection.
        """
        existing = material.cache_handles.get("Gemini")
        expires = material.cache_expires_at.get("Gemini")
        if existing and expires and expires > datetime.now() + timedelta(minutes=5):
            return existing

        result = await self._create_gemini_cache(material)
        if result is None:
            return None
        cache_name, expires_at = result
        material.cache_handles["Gemini"] = cache_name
        material.cache_expires_at["Gemini"] = expires_at
        self.manager.mark_dirty()  # persist the new cache handle
        return cache_name

    URL_PATTERN = re.compile(r'https?://[^\s\)\]<>\"\'`]+[^\s\.\,\)\]<>\"\'`:]')
    URL_EXTRACT_MAX = 3
    URL_EXTRACT_CHAR_CAP = 20000

    @classmethod
    def _extract_urls(cls, text: str) -> list[str]:
        """Pull HTTP(S) URLs from text, deduped, capped at URL_EXTRACT_MAX."""
        if not text:
            return []
        seen: dict[str, None] = {}
        for url in cls.URL_PATTERN.findall(text):
            if url not in seen:
                seen[url] = None
            if len(seen) >= cls.URL_EXTRACT_MAX:
                break
        return list(seen.keys())

    async def _tavily_extract(self, urls: list[str]) -> dict[str, str]:
        """Fetch text content for a list of URLs via Tavily's extract endpoint.

        Returns {url: extracted_text}. URLs that fail to extract are silently
        skipped — better to give the model partial context than to error out.
        """
        if not self.tavily_client or not urls:
            return {}
        try:
            result = await asyncio.to_thread(
                self.tavily_client.extract,
                urls=urls,
            )
        except Exception:
            return {}
        out: dict[str, str] = {}
        for r in result.get("results", []) if isinstance(result, dict) else []:
            url = r.get("url", "")
            content = r.get("raw_content") or r.get("content") or ""
            if url and content:
                out[url] = content[:self.URL_EXTRACT_CHAR_CAP]
        return out

    async def _augment_with_url_extracts(self, messages: list[dict]) -> None:
        """If the latest user turn references URLs, fetch their text content via
        Tavily and append the extracted bodies to that message in-place. Lets
        text-only models (Deepseek) read links the user pastes, and gives Claude
        a deterministic copy of the page even though it could also web_search.
        """
        if not messages or not self.tavily_client:
            return
        last = messages[-1]
        if last.get("role") != "user":
            return
        content = last.get("content", "")
        if isinstance(content, str):
            text_for_url_scan = content
        elif isinstance(content, list):
            text_for_url_scan = " ".join(
                b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            return
        urls = self._extract_urls(text_for_url_scan)
        if not urls:
            return
        extracts = await self._tavily_extract(urls)
        if not extracts:
            return
        block_parts = ["\n\n---\n[Auto-fetched content from URLs in the user's message — for grounding, treat as freshly retrieved web pages:]"]
        for url, body in extracts.items():
            block_parts.append(f"\n## {url}\n{body}\n")
        block_parts.append("---\n")
        extract_block = "\n".join(block_parts)
        if isinstance(content, str):
            last["content"] = content + extract_block
        else:
            last["content"] = list(content) + [{"type": "text", "text": extract_block}]

    @staticmethod
    def _has_cjk(text: str) -> bool:
        """Check if text contains Chinese/Japanese/Korean characters."""
        return any('\u4e00' <= c <= '\u9fff'  # CJK Unified Ideographs
                   or '\u3400' <= c <= '\u4dbf'  # CJK Extension A
                   or '\uf900' <= c <= '\ufaff'  # CJK Compatibility Ideographs
                   for c in text)

    def _estimate_confidence(self, message_text: str, provider: ModelProvider) -> float:
        """
        Estimate how well-suited a model is for this message.
        Returns 0.0-1.0. This is a heuristic, NOT an LLM call.
        """
        score = 0.5
        text_lower = message_text.lower()
        word_count = len(message_text.split())
        has_cjk = self._has_cjk(message_text)

        if provider.name == "Claude":
            # Claude excels at: complex questions, code review, nuance, creative, analysis
            if word_count > 100:
                score += 0.15
            if any(kw in text_lower for kw in [
                'explain', 'analyze', 'compare', 'review', 'design',
                'architecture', 'tradeoff', 'nuance', 'creative', 'write'
            ]):
                score += 0.1
            if any(kw in text_lower for kw in [
                'code', 'debug', 'refactor', 'implement', 'function',
                'class', 'algorithm', 'bug', 'error'
            ]):
                score += 0.1
            if '```' in message_text:
                score += 0.1
            # CJK penalty - Deepseek is stronger here
            if has_cjk:
                score -= 0.15
            # Cost penalty - Claude must "earn" selection
            score -= 0.2

        elif provider.name == "Deepseek":
            # Deepseek excels at: quick answers, factual, simple code, casual chat, CJK languages
            if word_count < 30:
                score += 0.15
            if any(kw in text_lower for kw in [
                'what is', 'how do', 'define', 'translate', 'list',
                'name', 'when', 'where', 'who', 'quick',
                'mandarin', 'chinese', '中文'
            ]):
                score += 0.1
            if '?' in message_text and word_count < 20:
                score += 0.1
            # CJK bonus - trained on deeper Chinese internet data
            if has_cjk:
                score += 0.2
            # Cost bonus - cheap = preferred for routine tasks
            score += 0.15

        elif provider.name == "Gemini":
            # Gemini excels at: novel/abstract reasoning, long-context synthesis,
            # multi-step math, pattern-finding, and questions where the answer
            # requires recombining ideas non-obviously (ARC-AGI-2 territory).
            # Also strong on math/scientific reasoning (GPQA Diamond leader).
            if word_count > 60:
                score += 0.1  # long prompts benefit from its context handling
            if any(kw in text_lower for kw in [
                'reason', 'reasoning', 'puzzle', 'riddle', 'pattern',
                'abstract', 'prove', 'proof', 'derive', 'derivation',
                'novel', 'unusual', 'counterintuitive', 'paradox',
                'physics', 'chemistry', 'biology', 'theorem', 'lemma',
                'integral', 'derivative', 'limit', 'differential',
            ]):
                score += 0.15
            # Math notation / equations — Gemini is strong here
            if '$' in message_text or '\\' in message_text or '∫' in message_text or '∑' in message_text:
                score += 0.1
            # Very long context — synthesizing across a lot of material
            if word_count > 200:
                score += 0.1
            # Mid-tier cost — cheaper than Claude, pricier than Deepseek.
            # Smaller cost penalty than Claude, no bonus like Deepseek.
            score -= 0.08

        return max(0.0, min(1.0, score))

    @staticmethod
    def _avg_cost_per_million(provider: ModelProvider) -> float:
        """Average of input + output $/M as a single tiebreak scalar.
        Lower = cheaper = preferred when scores are tied."""
        return (provider.input_cost_per_million + provider.output_cost_per_million) / 2

    async def _select_model(self, message: discord.Message, guild_id: int) -> tuple[ModelProvider, str]:
        """Select which model should respond to this message.
        Returns (provider, routing_reason) tuple."""
        enabled = [p for p in self.providers if p.enabled]
        if not enabled:
            # Shouldn't happen — on_message gates earlier — but fail gracefully.
            return self.claude_provider, "No providers enabled; defaulting to Claude."

        # Hard rule: images require a vision-capable provider. Filter the pool
        # down to vision-capable providers and let normal scoring pick among them.
        has_images = any(
            any(a.filename.lower().endswith(ext) for ext in CONFIG.image_types)
            for a in message.attachments
        )
        if has_images:
            vision_pool = [p for p in enabled if p.supports_vision]
            if vision_pool:
                enabled = vision_pool
                vision_routing_note = (
                    " (filtered to vision-capable providers because the message "
                    "contains image attachments)"
                )
            else:
                # Fall back to whatever's enabled even though none can see.
                vision_routing_note = " (no vision-capable provider enabled — image ignored)"
        else:
            vision_routing_note = ""

        # Only one provider available in the (possibly filtered) pool?
        if len(enabled) == 1:
            only = enabled[0]
            return only, f"Only {only.name} is available{vision_routing_note}."

        # User preference for this channel?
        channel_id = message.channel.id
        parent_id = getattr(message.channel, 'parent_id', None)
        pref = self.channel_preferences.get(channel_id) or self.channel_preferences.get(parent_id)
        pref_map = {
            "claude": self.claude_provider,
            "deepseek": self.deepseek_provider,
            "gemini": self.gemini_provider,
        }
        if pref in pref_map and pref_map[pref] in enabled:
            chosen = pref_map[pref]
            return chosen, f"User set channel preference to {chosen.name} (!prefer {pref}).{vision_routing_note}"

        # Global default override?
        if CONFIG.default_model in pref_map and pref_map[CONFIG.default_model] in enabled:
            chosen = pref_map[CONFIG.default_model]
            return chosen, f"Global default model is set to {chosen.name}.{vision_routing_note}"

        # Auto-select via confidence heuristic — three-way argmax with a
        # cost-based tiebreak (cheaper wins ties). The scoring functions
        # already encode each model's cost via per-provider penalties/bonuses,
        # so this is just final disambiguation.
        text = message.content or ""
        scores = {p.name: self._estimate_confidence(text, p) for p in enabled}
        # Sort: primary = score desc, secondary = cost asc (cheaper wins ties).
        ranked = sorted(
            enabled,
            key=lambda p: (-scores[p.name], self._avg_cost_per_million(p)),
        )
        winner = ranked[0]
        score_str = ", ".join(f"{p.name} {scores[p.name]:.2f}" for p in ranked)
        reason = (
            f"Auto-routed by heuristic: {score_str}. {winner.name} wins "
            f"(cost-based tiebreak applies on ties).{vision_routing_note}"
        )
        return winner, reason

    async def _generate_openai_compatible_response(
        self,
        client,
        provider: ModelProvider,
        guild_id: int,
        messages: list[dict],
        system: str,
        thinking: bool = False,
        reading_material: Optional["ReadingMaterial"] = None,
    ) -> tuple[str, list[str], str]:
        """Generate response using any OpenAI-compatible provider (Deepseek, Gemini).

        Uses provider.* quirks flags to handle per-provider differences:
        - supports_vision: keep image content if True, strip otherwise
        - requires_reasoning_echo: echo reasoning_content on prior assistant turns
        - disables_thinking_by_default: send extra_body to opt out when thinking=False

        Reading material handling (bookclub mode):
        - Gemini: create / reuse an explicit cachedContents entry via native
          API, reference it via extra_body={"cached_content": "..."} so the
          fic isn't re-uploaded every turn.
        - Other providers (Deepseek): prepend the fic to the system message.
          Deepseek's server-side prefix caching makes this cheap automatically.

        Returns (text, reactions, reasoning_content). reasoning_content is empty
        unless thinking mode was enabled and the provider returned a reasoning block.
        """
        # Convert to OpenAI format. Only strip images for text-only providers;
        # vision-capable providers (Gemini) keep the image blocks intact.
        if provider.supports_vision:
            openai_messages = list(messages)
        else:
            openai_messages = self._strip_images_from_messages(messages)
        openai_messages = self._convert_messages_to_openai_format(openai_messages)

        # When thinking is on AND the provider requires it, echo reasoning_content
        # on every prior assistant turn (empty string fine for ones we don't have).
        # Gemini's shim handles thinking server-side so this is skipped.
        if thinking and provider.requires_reasoning_echo:
            for msg in openai_messages:
                if msg.get("role") == "assistant":
                    msg_id = msg.get("_msg_id")
                    cached = self._get_reasoning(msg_id) if msg_id is not None else ""
                    msg["reasoning_content"] = cached

        # Handle reading material. For Gemini, we try the explicit-cache path
        # first (avoids re-uploading the fic every turn); on failure we fall
        # back to prepending. For Deepseek, prepending is fine — their server
        # auto-caches the prefix and bills cached tokens at ~99% off.
        gemini_cache_name: Optional[str] = None
        if reading_material is not None:
            if provider.name == "Gemini":
                gemini_cache_name = await self._ensure_gemini_cache(reading_material)
                if gemini_cache_name is None:
                    # Cache creation failed — fall back to inline.
                    system = self._build_reading_material_system_block(reading_material) + "\n\n" + system
            else:
                system = self._build_reading_material_system_block(reading_material) + "\n\n" + system

        # Prepend system message (OpenAI uses it as first message)
        openai_messages.insert(0, {"role": "system", "content": system})

        # Include web search tool if Tavily is available
        tools = self.OPENAI_COMPATIBLE_TOOLS if self.tavily_client else None

        try:
            api_kwargs = {
                "model": provider.model_id,
                "max_tokens": provider.max_tokens,
                "messages": self._strip_internal_keys(openai_messages),
            }
            extra_body: dict = {}
            if not thinking and provider.disables_thinking_by_default:
                # Provider has thinking on by default and needs the disable kwarg.
                # We reconstruct history from plain Discord text, so we can't
                # preserve reasoning blocks anyway — disable thinking instead.
                extra_body["thinking"] = {"type": "disabled"}
            if gemini_cache_name is not None:
                extra_body["cached_content"] = gemini_cache_name
            if extra_body:
                api_kwargs["extra_body"] = extra_body
            if tools:
                api_kwargs["tools"] = tools

            response = await asyncio.to_thread(
                client.chat.completions.create,
                **api_kwargs,
            )

            # Track usage (tiered if provider has context_tier_threshold set)
            provider.record_usage(
                response.usage.prompt_tokens,
                response.usage.completion_tokens,
            )
            provider.total_requests += 1

            # Handle tool calls (max 3 rounds to prevent loops)
            tool_rounds = 0
            while response.choices[0].message.tool_calls and tool_rounds < 3:
                tool_rounds += 1
                assistant_msg = response.choices[0].message

                # Add assistant message with tool calls to conversation
                tool_assistant: dict = {
                    "role": "assistant",
                    "content": assistant_msg.content or "",
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                        }
                        for tc in assistant_msg.tool_calls
                    ]
                }
                if thinking and provider.requires_reasoning_echo:
                    tool_assistant["reasoning_content"] = (
                        getattr(assistant_msg, "reasoning_content", None) or ""
                    )
                openai_messages.append(tool_assistant)

                # Execute each tool call. Routed through _search_for so each
                # provider uses its configured backend (Deepseek → Tavily,
                # Gemini → native google_search grounding, etc.).
                for tool_call in assistant_msg.tool_calls:
                    if tool_call.function.name == "web_search":
                        import json as _json
                        args = _json.loads(tool_call.function.arguments)
                        query = args.get("query", "")
                        search_result = await self._search_for(provider, query)
                        openai_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": search_result.text,
                        })

                # Continue conversation with tool results — refresh messages
                # since api_kwargs holds a stripped copy.
                api_kwargs["messages"] = self._strip_internal_keys(openai_messages)
                response = await asyncio.to_thread(
                    client.chat.completions.create,
                    **api_kwargs,
                )

                # Track additional usage
                provider.record_usage(
                    response.usage.prompt_tokens,
                    response.usage.completion_tokens,
                )

            response_text = response.choices[0].message.content or ""
            reasoning = (
                getattr(response.choices[0].message, "reasoning_content", None) or ""
            ) if (thinking and provider.requires_reasoning_echo) else ""

            # Process notes and reactions (same patterns as Claude)
            note_pattern = r'\[note:\s*([^:]+):\s*([^\]]+)\]'
            for match in re.finditer(note_pattern, response_text):
                key = match.group(1).strip()
                value = match.group(2).strip()
                self.manager.memories[guild_id].working.add(key, value)
            response_text = re.sub(note_pattern, '', response_text)

            reactions = []
            reaction_pattern = r'\[react:\s*([^\]]+)\]'
            for match in re.finditer(reaction_pattern, response_text):
                reactions.append(match.group(1).strip())
            response_text = re.sub(reaction_pattern, '', response_text).strip()
            # Clean up verbose formatting (both Deepseek and Gemini tend to over-format)
            response_text = re.sub(r'\n\s*\n\s*\n', '\n\n', response_text)  # Triple+ newlines → double
            response_text = re.sub(r'\n\n+(#+\s)', r'\n\1', response_text)  # Extra newlines before headers
            response_text = re.sub(r'\n\n+(\*\*[^*]+\*\*:)', r'\n\1', response_text)  # Extra newlines before bold labels
            response_text = re.sub(r'  +', ' ', response_text)

            return response_text, reactions, reasoning

        except Exception as e:
            return f"{provider.name} Error: {e}", [], ""

    async def _generate_response(
        self,
        channel: discord.abc.Messageable,
        guild_id: int,
        initial_message: discord.Message = None,
        provider: ModelProvider = None,
        routing_reason: str = "",
        thinking: bool = False,
        effort: Optional[str] = None,
    ) -> tuple[str, list[str], str]:
        """
        Generate response from the selected model provider.
        Returns (response_text, list_of_emoji_reactions, reasoning_content).
        reasoning_content is empty unless thinking mode was used and the
        provider returned a reasoning trace worth caching for next turn.
        Also processes [note: key: value] tags for working memory.
        """
        if provider is None:
            provider = self.claude_provider

        # Fetch conversation from Discord
        messages = await self.manager.fetch_thread_history(channel)

        # If this is a new thread, the triggering message isn't in thread history
        if initial_message:
            content_parts = []

            if initial_message.content:
                content_parts.append({
                    "type": "text",
                    "text": f"{initial_message.author.display_name}: {initial_message.content}"
                })

            for attachment in initial_message.attachments:
                if any(attachment.filename.lower().endswith(ext) for ext in CONFIG.image_types):
                    if attachment.size <= CONFIG.max_image_size_mb * 1024 * 1024:
                        try:
                            image_data = await self.manager._fetch_image_base64(attachment.url)
                            if image_data:
                                ext = attachment.filename.lower().split('.')[-1]
                                media_type = {
                                    'png': 'image/png',
                                    'jpg': 'image/jpeg',
                                    'jpeg': 'image/jpeg',
                                    'gif': 'image/gif',
                                    'webp': 'image/webp'
                                }.get(ext, 'image/png')
                                content_parts.append({
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": media_type,
                                        "data": image_data
                                    }
                                })
                        except Exception:
                            pass

                elif any(attachment.filename.lower().endswith(ext) for ext in CONFIG.text_file_types):
                    if attachment.size <= 1024 * 1024:
                        try:
                            file_content = await self.manager._fetch_text_file(attachment.url)
                            if file_content:
                                content_parts.append({
                                    "type": "text",
                                    "text": f"\n--- File: {attachment.filename} ---\n{file_content}\n--- End of {attachment.filename} ---\n"
                                })
                        except Exception:
                            pass

            if content_parts:
                if len(content_parts) == 1 and content_parts[0]["type"] == "text":
                    messages.insert(0, {"role": "user", "content": content_parts[0]["text"], "_msg_id": initial_message.id})
                else:
                    messages.insert(0, {"role": "user", "content": content_parts, "_msg_id": initial_message.id})

        if not messages:
            return "I don't see any messages to respond to!", [], ""

        # If the latest user message contains URLs, fetch them via Tavily and
        # append the extracted text in-place. Lets Deepseek (text-only) read
        # links and gives Claude a deterministic copy alongside its web_search.
        await self._augment_with_url_extracts(messages)

        # Build system prompt with all context sources
        system_parts = [self._build_system_prompt(provider, routing_reason)]

        # 1. Thread index (READ-ONLY - prevents feedback loops)
        thread_index = await self.manager.fetch_thread_index(channel)
        if thread_index:
            system_parts.append(thread_index)

        # 2. Memory (both tiers)
        memory_context = self.manager.memories[guild_id].get_context_string()
        if memory_context:
            system_parts.append(memory_context)

        # 3. Gentle nudge if working memory is sparse
        working_note_count = len(self.manager.memories[guild_id].working.notes)
        if working_note_count < 3:
            system_parts.append(
                "📝 *Reminder: Your working memory is pretty empty. "
                "If anything noteworthy comes up in this conversation, "
                "jot it down with [note: key: value].*"
            )

        system = "\n\n".join(system_parts)

        # Look up reading material pinned to this channel (bookclub mode).
        # Use parent channel for threads so a thread inside a bookclub
        # channel sees the same loaded fic.
        channel_id = getattr(channel, "id", None)
        parent_id = getattr(channel, "parent_id", None)
        reading_material: Optional[ReadingMaterial] = (
            self.manager.reading_materials.get(channel_id)
            or (self.manager.reading_materials.get(parent_id) if parent_id else None)
        )
        # Gate: don't try to inject material that won't fit alongside discussion.
        # Reserve ~50k tokens for chat history + memory + system framing.
        if reading_material is not None:
            needed = reading_material.estimated_tokens + 50_000
            if provider.max_context_tokens < needed:
                reading_material = None  # silently drop for this provider

        # Dispatch to the appropriate model. OpenAI-compatible providers
        # (Deepseek, Gemini) share the same generation path; Claude uses the
        # Anthropic SDK directly with its own tool/thinking machinery.
        if provider.name in self.openai_compatible_clients:
            client = self.openai_compatible_clients[provider.name]
            return await self._generate_openai_compatible_response(
                client, provider, guild_id, messages, system,
                thinking=thinking,
                reading_material=reading_material,
            )

        # Claude path (default) — with organic web search capability
        try:
            # Give Claude the web search tool so it can search organically
            claude_tools = [{
                "type": "web_search_20250305",
                "name": "web_search"
            }]

            # When a reading material is loaded, send system as a list of
            # blocks with cache_control on the (very large) fic block. The
            # cache marker tells Anthropic to cache everything up through
            # that block; subsequent requests with the same fic hit the
            # cache at ~10% pricing.
            if reading_material is not None:
                fic_block_text = self._build_reading_material_system_block(reading_material)
                claude_system = [
                    {
                        "type": "text",
                        "text": fic_block_text,
                        "cache_control": {"type": "ephemeral"},
                    },
                    {"type": "text", "text": system},
                ]
            else:
                claude_system = system

            claude_kwargs = {
                "model": self.claude_provider.model_id,
                "max_tokens": self.claude_provider.max_tokens,
                "system": claude_system,
                "tools": claude_tools,
            }
            if thinking:
                # Adaptive thinking on Opus 4.7: model decides depth; effort
                # controls overall thinking/acting budget. effort=None falls
                # back to the class default ("high"). xhigh/max need ≥64K
                # max_tokens or they truncate mid-thought.
                # `output_config` is a newer field that some installed anthropic
                # SDK versions reject as an unknown kwarg; pass it via extra_body
                # so the field reaches the API regardless of SDK version.
                chosen_effort = effort or self.CLAUDE_THINKING_EFFORT
                claude_kwargs["max_tokens"] = (
                    64000 if chosen_effort in ("xhigh", "max")
                    else self.CLAUDE_THINKING_MAX_TOKENS
                )
                claude_kwargs["thinking"] = {"type": "adaptive"}
                claude_kwargs["extra_body"] = {"output_config": {"effort": chosen_effort}}

            api_messages = self._strip_internal_keys(messages)
            response = await asyncio.to_thread(
                self.claude_client.messages.create,
                messages=api_messages,
                **claude_kwargs,
            )

            # Track usage
            self.claude_provider.total_input_tokens += response.usage.input_tokens
            self.claude_provider.total_output_tokens += response.usage.output_tokens
            self.claude_provider.total_requests += 1

            # Handle tool use loop — Claude may decide to search the web.
            # When thinking is on, response.content includes thinking blocks
            # that must be passed back unchanged in the next turn.
            search_rounds = 0
            while response.stop_reason == "tool_use" and search_rounds < 3:
                tool_use_block = None
                for block in response.content:
                    if block.type == "tool_use":
                        tool_use_block = block
                        break
                if not tool_use_block:
                    break

                api_messages.append({"role": "assistant", "content": response.content})
                api_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_use_block.id,
                        "content": "Search completed."
                    }]
                })

                response = await asyncio.to_thread(
                    self.claude_client.messages.create,
                    messages=api_messages,
                    **claude_kwargs,
                )

                self.claude_provider.total_input_tokens += response.usage.input_tokens
                self.claude_provider.total_output_tokens += response.usage.output_tokens
                search_rounds += 1

            if not response.content:
                return "I received an empty response from the API.", [], ""

            # Extract text from all content blocks (may include search result
            # and thinking blocks). We discard thinking blocks from the visible
            # output — Claude doesn't require them for the next turn since we
            # always start fresh from Discord history.
            response_text = ""
            for block in response.content:
                if getattr(block, "type", None) == "thinking":
                    continue
                if hasattr(block, 'text'):
                    response_text += block.text

            # Extract and process working memory notes
            note_pattern = r'\[note:\s*([^:]+):\s*([^\]]+)\]'
            for match in re.finditer(note_pattern, response_text):
                key = match.group(1).strip()
                value = match.group(2).strip()
                self.manager.memories[guild_id].working.add(key, value)
            response_text = re.sub(note_pattern, '', response_text)

            # Extract reactions
            reactions = []
            reaction_pattern = r'\[react:\s*([^\]]+)\]'
            for match in re.finditer(reaction_pattern, response_text):
                reactions.append(match.group(1).strip())
            response_text = re.sub(reaction_pattern, '', response_text).strip()

            # Clean up formatting
            response_text = re.sub(r'\n\s*\n\s*\n', '\n\n', response_text)
            response_text = re.sub(r'  +', ' ', response_text)

            return response_text, reactions, ""

        except anthropic.APIError as e:
            return f"Claude Error: {e}", [], ""
    
    async def _web_search(
        self,
        query: str,
        channel: discord.abc.Messageable,
        guild_id: int
    ) -> tuple[str, list[discord.Embed]]:
        """
        Perform a web search using Claude's web_search tool.
        Returns (response_text, list_of_embeds_for_citations)
        """
        # Build context for the search
        memory_context = self.manager.memories[guild_id].get_context_string()
        system = (
            "You are a helpful assistant performing a web search. "
            "Use the web_search tool to find current information, then provide a clear, "
            "well-cited answer. Be concise but thorough. Reference earlier conversation "
            "when the search query depends on it (e.g. 'links for what you said earlier')."
        )
        if memory_context:
            system += f"\n\nContext about the user/server:\n{memory_context}"

        # Pull conversation history so search queries that reference prior turns
        # (e.g. "find links for what you mentioned") have something to anchor on.
        # The triggering !search message is already in history; replace its content
        # with the cleaned query so the model sees the literal question to answer.
        history = await self.manager.fetch_thread_history(channel)
        history = self._strip_internal_keys(history)
        if history and history[-1].get("role") == "user":
            history[-1] = {"role": "user", "content": query}
        else:
            history.append({"role": "user", "content": query})
        messages = history
        
        try:
            # Web search always uses Claude (has built-in web search tool)
            response = await asyncio.to_thread(
                self.claude_client.messages.create,
                model=self.claude_provider.model_id,
                max_tokens=self.claude_provider.max_tokens,
                system=system,
                messages=messages,
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search"
                }]
            )

            # Track usage
            self.claude_provider.total_input_tokens += response.usage.input_tokens
            self.claude_provider.total_output_tokens += response.usage.output_tokens
            self.claude_provider.total_requests += 1

            # Collect all sources for embeds
            sources = []
            final_text = ""

            # Process response - may need multiple rounds if tool_use
            while response.stop_reason == "tool_use":
                tool_use_block = None
                for block in response.content:
                    if block.type == "tool_use":
                        tool_use_block = block
                        break

                if not tool_use_block:
                    break

                messages.append({"role": "assistant", "content": response.content})
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_use_block.id,
                        "content": "Search completed."
                    }]
                })

                response = await asyncio.to_thread(
                    self.claude_client.messages.create,
                    model=self.claude_provider.model_id,
                    max_tokens=self.claude_provider.max_tokens,
                    system=system,
                    messages=messages,
                    tools=[{
                        "type": "web_search_20250305",
                        "name": "web_search"
                    }]
                )

                self.claude_provider.total_input_tokens += response.usage.input_tokens
                self.claude_provider.total_output_tokens += response.usage.output_tokens
            
            # Extract final text response
            for block in response.content:
                if hasattr(block, 'text'):
                    final_text += block.text
            
            # Try to extract citations from the response
            # Claude's web search includes citations in a specific format
            embeds = []
            
            # Look for citation patterns and create embeds
            # The response may contain URLs - extract unique ones
            url_pattern = r'https?://[^\s\)\]<>\"\']+[^\s\.\,\)\]<>\"\':]'
            found_urls = list(set(re.findall(url_pattern, final_text)))[:CONFIG.max_search_results_in_embed]
            
            if found_urls:
                embed = discord.Embed(
                    title="🔍 Sources",
                    color=discord.Color.blue()
                )
                for i, url in enumerate(found_urls, 1):
                    # Truncate long URLs for display
                    display_url = url[:60] + "..." if len(url) > 60 else url
                    embed.add_field(
                        name=f"Source {i}",
                        value=f"[{display_url}]({url})",
                        inline=False
                    )
                embeds.append(embed)
            
            return final_text.strip(), embeds
            
        except anthropic.APIError as e:
            return f"❌ Search API Error: {e}", []
    
    def _extract_code_files(self, response: str) -> tuple[str, list[discord.File]]:
        """
        Extract code blocks with filenames and convert to Discord files.
        Format: ```filename.ext
        Returns (cleaned_response, list_of_files)
        """
        files = []

        # Pattern for code blocks with filename: ```filename.ext\ncode\n```
        pattern = r'```(\w+\.\w+)\n(.*?)```'

        def replace_with_attachment_note(match):
            filename = match.group(1)
            code = match.group(2)

            # Only convert to file if code is long enough
            if len(code) > 500:
                file_buffer = io.BytesIO(code.encode('utf-8'))
                files.append(discord.File(file_buffer, filename=filename))
                return f"📎 *See attached file: `{filename}`*"
            else:
                # Keep short code inline
                return match.group(0)

        cleaned = re.sub(pattern, replace_with_attachment_note, response, flags=re.DOTALL)

        return cleaned, files

    # LaTeX detection patterns. Display math is unambiguous ($$...$$). Inline
    # math ($...$) is filtered with a heuristic to avoid matching things like
    # "$50" — only blocks that contain real LaTeX syntax (\command, ^, _, {})
    # get rendered.
    LATEX_DISPLAY_RE = re.compile(r'\$\$(.+?)\$\$', re.DOTALL)
    LATEX_INLINE_RE = re.compile(r'(?<!\\)(?<!\$)\$([^\$\n]+?)\$(?!\$)')
    LATEX_LIKELY_RE = re.compile(r'\\[a-zA-Z]+|[\^_]\{|\\\\')
    LATEX_MAX_RENDERS = 8
    LATEX_MAX_SOURCE_LEN = 1500

    @staticmethod
    def _render_latex(latex_source: str) -> Optional[bytes]:
        """Render a LaTeX math expression to PNG bytes via matplotlib's mathtext.

        Returns None on render failure (unsupported LaTeX command, syntax error,
        empty input). Caller should fall back to leaving the source text alone.
        """
        src = latex_source.strip()
        if not src:
            return None
        try:
            buf = io.BytesIO()
            _mpl_mathtext.math_to_image(f"${src}$", buf, format='png', dpi=200)
            buf.seek(0)
            return buf.getvalue()
        except Exception:
            return None

    @classmethod
    def _extract_latex_blocks(cls, text: str) -> list[str]:
        """Find LaTeX math blocks worth rendering. Returns the source strings.

        Display blocks ($$...$$) are taken first, then inline ($...$) blocks
        from the remaining text. Inline blocks must contain LaTeX-like syntax
        to avoid false positives on currency or sentence punctuation.
        """
        blocks: list[str] = []
        seen: set[str] = set()

        def add(src: str) -> bool:
            src = src.strip()
            if not src or src in seen or len(src) > cls.LATEX_MAX_SOURCE_LEN:
                return False
            seen.add(src)
            blocks.append(src)
            return len(blocks) >= cls.LATEX_MAX_RENDERS

        for m in cls.LATEX_DISPLAY_RE.finditer(text):
            if add(m.group(1)):
                return blocks
        text_no_display = cls.LATEX_DISPLAY_RE.sub('', text)
        for m in cls.LATEX_INLINE_RE.finditer(text_no_display):
            src = m.group(1).strip()
            if not cls.LATEX_LIKELY_RE.search(src):
                continue
            if add(src):
                return blocks
        return blocks

    @staticmethod
    def _composite_latex_pngs(pngs: list[bytes], pad: int = 24) -> bytes:
        """Stack rendered equation PNGs vertically into one image with white
        background and centered alignment. Discord shows multiple attachments
        in a squished horizontal grid; a single tall image renders full-width
        inline and reads top-to-bottom in equation order.
        """
        images = [_PILImage.open(io.BytesIO(p)).convert("RGBA") for p in pngs]
        max_w = max(im.width for im in images)
        total_w = max_w + 2 * pad
        total_h = sum(im.height for im in images) + pad * (len(images) + 1)
        canvas = _PILImage.new("RGBA", (total_w, total_h), (255, 255, 255, 255))
        y = pad
        for im in images:
            x = pad + (max_w - im.width) // 2
            canvas.paste(im, (x, y), im)
            y += im.height + pad
        out = io.BytesIO()
        canvas.convert("RGB").save(out, format="PNG", optimize=True)
        out.seek(0)
        return out.getvalue()

    def _render_latex_attachments(self, response_text: str, max_files: int) -> list[discord.File]:
        """Detect LaTeX in the response and produce PNG attachments. The source
        text is left untouched in the message body so users can copy it.

        With multiple equations we composite them into a single tall PNG —
        Discord otherwise lays multiple attachments out in a horizontal grid
        which squishes wide math renders into illegibility.
        """
        if max_files <= 0:
            return []
        pngs: list[bytes] = []
        for src in self._extract_latex_blocks(response_text):
            png = self._render_latex(src)
            if png is not None:
                pngs.append(png)
        if not pngs:
            return []
        if len(pngs) == 1:
            return [discord.File(io.BytesIO(pngs[0]), filename="eq.png")]
        try:
            composite = self._composite_latex_pngs(pngs)
            return [discord.File(io.BytesIO(composite), filename="equations.png")]
        except Exception:
            # Composite failed for some reason — fall back to individual files,
            # capped at max_files. At least the math gets through.
            return [
                discord.File(io.BytesIO(p), filename=f"eq{i}.png")
                for i, p in enumerate(pngs[:max_files], 1)
            ]
    
    async def _send_response(
        self,
        channel: discord.abc.Messageable,
        content: str,
        files: list[discord.File] = None
    ) -> Optional[discord.Message]:
        """Send message, chunking if over Discord's limit. Returns the first
        sent Message (or None if nothing was sent) so callers can key per-message
        state like the reasoning cache."""
        if not content and not files:
            return None

        # If content fits in one message
        if len(content) <= 1990:
            return await channel.send(content, files=files)

        # Chunk the message
        chunks = []
        remaining = content
        while remaining:
            if len(remaining) <= 1990:
                chunks.append(remaining)
                break

            # Find a good break point
            break_point = remaining.rfind('\n', 0, 1990)
            if break_point == -1:
                break_point = remaining.rfind(' ', 0, 1990)
            if break_point == -1:
                break_point = 1990

            chunks.append(remaining[:break_point])
            remaining = remaining[break_point:].lstrip()

        # Send chunks (files only on first message)
        first_msg = None
        for i, chunk in enumerate(chunks):
            if i == 0:
                first_msg = await channel.send(chunk, files=files)
            else:
                await channel.send(chunk)
        return first_msg
    
    async def _handle_command(self, message: discord.Message) -> None:
        """Handle bot commands."""
        content = message.content.strip()
        parts = content.split(maxsplit=2)
        cmd = parts[0].lower()
        guild_id = message.guild.id
        memory = self.manager.memories[guild_id]
        
        if cmd == "!context":
            messages = await self.manager.fetch_thread_history(message.channel)
            # Look up channel-level reading material via parent channel for threads
            ctx_channel_id = message.channel.id
            if isinstance(message.channel, discord.Thread) and message.channel.parent_id:
                ctx_channel_id = message.channel.parent_id
            info = self.manager.get_context_info(messages, guild_id, channel_id=ctx_channel_id)
            await message.channel.send(info)
        
        elif cmd == "!cost":
            summary = self.manager.get_cost_summary(self.providers)
            await message.channel.send(summary)
        
        elif cmd == "!memories":
            lines = []
            
            # Long-term memories
            if memory.longterm.entries:
                lines.append("🧠 **Long-term memories** (permanent):")
                for key, value in memory.longterm.entries.items():
                    lines.append(f"  `{key}`: {value}")
            else:
                lines.append("🧠 **Long-term memories**: None yet")
            
            lines.append("")
            
            # Working notes
            if memory.working.notes:
                lines.append("📝 **Working notes** (fade over time):")
                for key, note in sorted(
                    memory.working.notes.items(),
                    key=lambda x: x[1].freshness(CONFIG.working_memory_decay_hours),
                    reverse=True
                ):
                    freshness = note.freshness(CONFIG.working_memory_decay_hours)
                    if freshness > 0.7:
                        indicator = "🟢"
                    elif freshness > 0.3:
                        indicator = "🟡"
                    else:
                        indicator = "🔴"
                    lines.append(f"  {indicator} `{key}`: {note.content}")
                lines.append("")
                lines.append("*Use `!keep <key>` to make a working note permanent*")
            else:
                lines.append("📝 **Working notes**: None yet")
            
            # Send in chunks if too long
            full_text = "\n".join(lines)
            await self._send_response(message.channel, full_text)
        
        elif cmd == "!remember":
            # !remember key value
            if len(parts) >= 3:
                key = parts[1]
                value = parts[2]
                if memory.longterm.add(key, value):
                    self.manager.save_memories(providers=self.providers)
                    await message.channel.send(f"✅ Remembered `{key}` (permanent)")
                else:
                    await message.channel.send(
                        f"❌ Long-term memory full ({CONFIG.max_longterm_memories} max). "
                        f"Use `!forget <key>` to make room."
                    )
            else:
                await message.channel.send("Usage: `!remember <key> <value>`")
        
        elif cmd == "!forget":
            if len(parts) >= 2:
                key = parts[1]
                # Try long-term first, then working
                if memory.longterm.remove(key):
                    self.manager.save_memories(providers=self.providers)
                    await message.channel.send(f"✅ Forgot `{key}` from long-term memory")
                elif memory.working.remove(key):
                    self.manager.save_memories(providers=self.providers)
                    await message.channel.send(f"✅ Forgot `{key}` from working notes")
                else:
                    await message.channel.send(f"❓ No memory with key `{key}`")
            else:
                await message.channel.send("Usage: `!forget <key>`")
        
        elif cmd == "!keep":
            # Promote a working note to long-term memory
            if len(parts) >= 2:
                key = parts[1]
                if key not in memory.working.notes:
                    await message.channel.send(f"❓ No working note with key `{key}`")
                elif memory.promote(key):
                    self.manager.save_memories(providers=self.providers)
                    await message.channel.send(f"✅ Promoted `{key}` to long-term memory (permanent)")
                else:
                    await message.channel.send(
                        f"❌ Long-term memory full ({CONFIG.max_longterm_memories} max). "
                        f"Use `!forget <key>` to make room."
                    )
            else:
                await message.channel.send("Usage: `!keep <key>` - promotes a working note to permanent memory")
        
        elif cmd == "!threads":
            # Show the thread index
            thread_index = await self.manager.fetch_thread_index(message.channel)
            if thread_index:
                await message.channel.send(thread_index)
            else:
                await message.channel.send("📭 No other threads found in this channel.")
        
        elif cmd == "!search":
            # Web search is available via any of:
            #   - Claude's native web_search tool (ANTHROPIC_API_KEY)
            #   - Gemini's native google_search grounding (GEMINI_API_KEY)
            #   - Any OpenAI-compatible provider via Tavily (TAVILY_API_KEY)
            #
            # Pick the searcher in this order: channel preference → Claude →
            # Gemini (native) → Deepseek (Tavily).
            has_gemini_native = self.gemini_provider.enabled and bool(os.getenv("GEMINI_API_KEY"))
            eligible: list[ModelProvider] = []
            if self.claude_provider.enabled:
                eligible.append(self.claude_provider)
            if has_gemini_native:
                eligible.append(self.gemini_provider)
            elif self.gemini_provider.enabled and self.tavily_client:
                eligible.append(self.gemini_provider)
            if self.deepseek_provider.enabled and self.tavily_client:
                eligible.append(self.deepseek_provider)

            if not eligible:
                await message.channel.send(
                    "❌ Web search requires one of: ANTHROPIC_API_KEY (Claude native), "
                    "GEMINI_API_KEY (Gemini native grounding), or TAVILY_API_KEY (Deepseek/Gemini)."
                )
                return

            if len(parts) < 2:
                await message.channel.send(
                    "Usage: `!search <query>`\n"
                    "Example: `!search latest news on Claude AI`\n\n"
                    "⚠️ Web search costs extra tokens (~$0.01-0.03 per search)"
                )
                return

            query = message.content[8:].strip()  # len("!search ") = 8
            await message.channel.send(f"🔍 Searching: *{query}*")

            # Pick the searcher
            channel_id = message.channel.id
            parent_id = getattr(message.channel, 'parent_id', None)
            ch_pref = self.channel_preferences.get(channel_id) or self.channel_preferences.get(parent_id)
            pref_map = {
                "claude": self.claude_provider,
                "deepseek": self.deepseek_provider,
                "gemini": self.gemini_provider,
            }
            searcher: Optional[ModelProvider] = None
            if ch_pref in pref_map and pref_map[ch_pref] in eligible:
                searcher = pref_map[ch_pref]
            if searcher is None:
                # Default order: Claude > Gemini (native grounding) > Deepseek (Tavily).
                for p in (self.claude_provider, self.gemini_provider, self.deepseek_provider):
                    if p in eligible:
                        searcher = p
                        break

            # Dispatch
            if searcher is self.claude_provider:
                # Claude native web search — existing path
                async with message.channel.typing():
                    response_text, embeds = await self._web_search(
                        query, message.channel, guild_id
                    )
                if self.multi_model_active:
                    response_text = f"**[Claude]** {response_text}"
                await self._send_response(message.channel, response_text)
                for embed in embeds:
                    await message.channel.send(embed=embed)
            else:
                # OpenAI-compatible provider — dispatch through SearchBackend
                async with message.channel.typing():
                    search_result = await self._search_for(searcher, query)

                    if search_result.is_grounded_answer:
                        # Gemini native: backend already synthesized the answer.
                        # Display directly; we don't need to round-trip through
                        # the chat model again.
                        response_text = search_result.text
                    else:
                        # Tavily: feed raw hits through the chosen model for synthesis.
                        history = await self.manager.fetch_thread_history(message.channel)
                        history = self._strip_internal_keys(history)
                        synthesis_prompt = (
                            f"Based on these web search results, answer the query: {query}\n\n"
                            f"Search results:\n{search_result.text}"
                        )
                        if history and history[-1].get("role") == "user":
                            history[-1] = {"role": "user", "content": synthesis_prompt}
                        else:
                            history.append({"role": "user", "content": synthesis_prompt})
                        client = self.openai_compatible_clients[searcher.name]
                        response_text, _, _ = await self._generate_openai_compatible_response(
                            client, searcher, guild_id, history,
                            "You are a helpful assistant. Summarize the search results clearly and "
                            "cite your sources with URLs. Use the prior conversation as context when "
                            "the user's query refers back to it.",
                        )

                if self.multi_model_active:
                    response_text = f"**[{searcher.name}]** {response_text}"
                await self._send_response(message.channel, response_text)

                # Render citation embeds (works for both Tavily and Google native)
                if search_result.citations:
                    embed = discord.Embed(title="🔍 Sources", color=discord.Color.blue())
                    for i, cit in enumerate(
                        search_result.citations[:CONFIG.max_search_results_in_embed], 1
                    ):
                        title = cit.get("title") or cit.get("url", "")[:60] or "(untitled)"
                        url = cit.get("url", "")
                        value = f"[{title}]({url})" if url else title
                        embed.add_field(name=f"Source {i}", value=value, inline=False)
                    await message.channel.send(embed=embed)

            await message.channel.send(
                "*💡 Web search incurs additional token costs. Use `!cost` to check usage.*"
            )
        
        elif cmd == "!summarize":
            # Manually save a thread summary to long-term memory
            # Usage: !summarize <key> <summary>  OR  just !summarize to ask Claude to summarize
            if len(parts) >= 3:
                key = parts[1]
                summary = parts[2]
                if memory.longterm.add(f"thread_{key}", summary):
                    self.manager.save_memories(providers=self.providers)
                    await message.channel.send(f"✅ Saved thread summary as `thread_{key}`")
                else:
                    await message.channel.send(
                        f"❌ Long-term memory full. Use `!forget <key>` to make room."
                    )
            elif len(parts) == 2:
                # !summarize <key> - ask Claude to generate summary
                if not self.claude_provider.enabled:
                    await message.channel.send("❌ Auto-summarize requires Claude (ANTHROPIC_API_KEY not configured).")
                    return
                key = parts[1]
                await message.channel.send(f"📝 Generating summary for this thread as `thread_{key}`...")
                
                # Fetch thread history
                messages = await self.manager.fetch_thread_history(message.channel, limit=50)
                if messages:
                    try:
                        # Build the conversation text
                        conversation_text = "\n".join(
                            m["content"] if isinstance(m["content"], str) else str(m["content"])
                            for m in messages
                        )
                        
                        # Ask Claude to summarize (run in thread pool)
                        summary_response = await asyncio.to_thread(
                            self.claude_client.messages.create,
                            model=self.claude_provider.model_id,
                            max_tokens=200,
                            system="Summarize this conversation in 1-2 sentences. Focus on the key topic and any decisions/outcomes. Be concise.",
                            messages=[{"role": "user", "content": f"Conversation to summarize:\n\n{conversation_text}"}]
                        )
                        summary = summary_response.content[0].text.strip()

                        # Track usage
                        self.claude_provider.total_input_tokens += summary_response.usage.input_tokens
                        self.claude_provider.total_output_tokens += summary_response.usage.output_tokens
                        self.claude_provider.total_requests += 1
                        
                        if memory.longterm.add(f"thread_{key}", summary):
                            self.manager.save_memories(providers=self.providers)
                            await message.channel.send(f"✅ Saved: `thread_{key}`: {summary}")
                        else:
                            await message.channel.send(
                                f"❌ Long-term memory full. Use `!forget <key>` to make room.\n"
                                f"Summary was: {summary}"
                            )
                    except anthropic.APIError as e:
                        await message.channel.send(f"❌ Couldn't generate summary: {e}")
                else:
                    await message.channel.send("❌ No messages found in this thread to summarize.")
            else:
                await message.channel.send(
                    "Usage:\n"
                    "`!summarize <key>` - Auto-generate summary of this thread\n"
                    "`!summarize <key> <your summary>` - Save your own summary"
                )

        elif cmd == "!load":
            # Bookclub mode: load a long text (currently AO3 fics) into this
            # channel's pinned context. Persists across restarts. Per-channel
            # so different channels can read different works.
            channel_id = message.channel.id
            if isinstance(message.channel, discord.Thread) and message.channel.parent_id:
                channel_id = message.channel.parent_id

            if len(parts) < 2:
                current = self.manager.reading_materials.get(channel_id)
                if current:
                    chapter_count = len(current.chapter_breaks) or 1
                    await message.channel.send(
                        f"📚 Currently loaded: **{current.title}**\n"
                        f"  Source: {current.url}\n"
                        f"  ~{current.estimated_tokens:,} tokens, "
                        f"{current.word_count:,} words, {chapter_count} chapter(s)\n"
                        f"  Loaded: {current.loaded_at.strftime('%Y-%m-%d %H:%M')}\n\n"
                        f"Use `!unload` to drop it, or `!load <url>` to swap."
                    )
                else:
                    await message.channel.send(
                        "Usage: `!load <url>` — load a long text into this channel for bookclub mode.\n"
                        "Currently supports AO3 work URLs. Bot will fetch the full work and "
                        "pin it into every model's system context until you `!unload`."
                    )
                return

            url = parts[1].strip()
            # Strip Discord's auto-link brackets if present
            url = url.strip("<>").rstrip("/")

            ao3_normalized = self._build_ao3_full_work_url(url)
            if ao3_normalized is None:
                await message.channel.send(
                    "❌ I only know how to load AO3 work URLs right now. "
                    "Expected something like `https://archiveofourown.org/works/12345`."
                )
                return

            if not _HAS_BS4:
                await message.channel.send(
                    "❌ AO3 fetcher needs `beautifulsoup4`. Install with:\n"
                    "```\npip install beautifulsoup4\n```\n"
                    "Then restart the bot."
                )
                return

            await message.channel.send(f"📥 Fetching {url} …")
            async with message.channel.typing():
                material = await self._fetch_ao3_work(url)

            if material is None:
                await message.channel.send(
                    "❌ Couldn't fetch or parse that work. Possible causes: "
                    "the work is locked (registered-users-only), AO3 returned a non-200, "
                    "or the URL doesn't point to a normal AO3 work page."
                )
                return

            # Check which providers can fit it
            chapter_count = len(material.chapter_breaks) or 1
            fits: list[str] = []
            cant_fit: list[str] = []
            # Reserve some budget for memory, history, system framing
            budget_reserve = 50_000
            needed = material.estimated_tokens + budget_reserve
            for p in self.providers:
                if not p.enabled:
                    continue
                if p.max_context_tokens >= needed:
                    fits.append(p.name)
                else:
                    cant_fit.append(f"{p.name} ({p.max_context_tokens:,} ctx)")

            self.manager.reading_materials[channel_id] = material
            self.manager.mark_dirty()

            lines = [
                f"📚 Loaded **{material.title}**",
                f"  ~{material.estimated_tokens:,} tokens, "
                f"{material.word_count:,} words, {chapter_count} chapter(s)",
                f"  Fits in context for: {', '.join(fits) if fits else '(none — too long!)'}",
            ]
            if cant_fit:
                lines.append(f"  ⚠️  Won't fit for: {', '.join(cant_fit)} — those models will see the discussion only, not the source text.")
            lines.append(
                "\nThe text is now pinned to this channel's context. Try "
                "`!claude what do you think of chapter 1?` to start. "
                "Use `!unload` when done."
            )
            await message.channel.send("\n".join(lines))

        elif cmd == "!unload":
            channel_id = message.channel.id
            if isinstance(message.channel, discord.Thread) and message.channel.parent_id:
                channel_id = message.channel.parent_id
            material = self.manager.reading_materials.pop(channel_id, None)
            if material is None:
                await message.channel.send("Nothing loaded in this channel.")
            else:
                self.manager.mark_dirty()
                await message.channel.send(f"📤 Unloaded **{material.title}**.")

        elif cmd == "!reading":
            # Lightweight info command — shows what's loaded without the
            # "no usage" path that !load has. Also includes a chapter table
            # of contents if available.
            channel_id = message.channel.id
            if isinstance(message.channel, discord.Thread) and message.channel.parent_id:
                channel_id = message.channel.parent_id
            material = self.manager.reading_materials.get(channel_id)
            if material is None:
                await message.channel.send("Nothing loaded in this channel. Use `!load <url>` to start a bookclub.")
                return
            lines = [
                f"📚 **{material.title}**",
                f"  Source: {material.url}",
                f"  ~{material.estimated_tokens:,} tokens, "
                f"{material.word_count:,} words",
                f"  Loaded: {material.loaded_at.strftime('%Y-%m-%d %H:%M')}",
            ]
            if material.chapter_breaks:
                lines.append(f"\n**Chapters** ({len(material.chapter_breaks)}):")
                for i, (_, ch_title) in enumerate(material.chapter_breaks[:30], 1):
                    lines.append(f"  {i}. {ch_title}")
                if len(material.chapter_breaks) > 30:
                    lines.append(f"  … and {len(material.chapter_breaks) - 30} more")
            await self._send_response(message.channel, "\n".join(lines))

        elif cmd == "!models":
            lines = ["🤖 **Available Models**"]
            for p in self.providers:
                status = "🟢 Enabled" if p.enabled else "⚪ Disabled"
                cost = p.get_cost()
                if p.total_requests > 0:
                    lines.append(
                        f"  **{p.name}** ({p.model_id}) - {status}, "
                        f"{p.total_requests} requests, ${cost:.4f}"
                    )
                else:
                    lines.append(f"  **{p.name}** ({p.model_id}) - {status}")

            mode = CONFIG.default_model
            channel_id = message.channel.id
            parent_id = getattr(message.channel, 'parent_id', None)
            ch_pref = self.channel_preferences.get(channel_id) or self.channel_preferences.get(parent_id)
            if ch_pref:
                lines.append(f"\n  **This channel**: {ch_pref}")
            lines.append(f"  **Selection mode**: {mode}")
            await self._send_response(message.channel, "\n".join(lines))

        elif cmd == "!prefer":
            if len(parts) >= 2:
                pref = parts[1].lower()
                if pref not in ("claude", "deepseek", "gemini", "auto"):
                    await message.channel.send("Usage: `!prefer [claude|deepseek|gemini|auto]`")
                    return
                channel_id = message.channel.id
                # Use parent channel for threads
                if isinstance(message.channel, discord.Thread) and message.channel.parent_id:
                    channel_id = message.channel.parent_id
                if pref == "auto":
                    self.channel_preferences.pop(channel_id, None)
                    await message.channel.send("✅ This channel will use **automatic** model selection.")
                elif pref == "deepseek" and not self.deepseek_provider.enabled:
                    await message.channel.send("❌ Deepseek is not configured (no API key).")
                elif pref == "claude" and not self.claude_provider.enabled:
                    await message.channel.send("❌ Claude is not configured (no API key).")
                elif pref == "gemini" and not self.gemini_provider.enabled:
                    await message.channel.send("❌ Gemini is not configured (no API key).")
                else:
                    self.channel_preferences[channel_id] = pref
                    await message.channel.send(f"✅ This channel will always use **{pref.title()}**.")
            else:
                channel_id = message.channel.id
                parent_id = getattr(message.channel, 'parent_id', None)
                pref = self.channel_preferences.get(channel_id) or self.channel_preferences.get(parent_id, "auto")
                await message.channel.send(
                    f"Current preference: **{pref}**\n"
                    f"Usage: `!prefer [claude|deepseek|gemini|auto]`"
                )

        elif cmd == "!calibration":
            model_name = parts[1].title() if len(parts) >= 2 else None
            models = [model_name] if model_name else [p.name for p in self.providers if p.enabled]
            lines = ["📊 **Calibration Data**"]
            for name in models:
                summary = self.manager.calibration.get_calibration_summary(name)
                lines.append(f"\n  **{name}** ({summary['total']} bids, {summary['rated']} rated):")
                if summary['buckets']:
                    for bucket, data in summary['buckets'].items():
                        pct = int(data['success_rate'] * 100)
                        lines.append(f"    {bucket}: {data['count']} rated, {pct}% positive")
                else:
                    lines.append("    No feedback yet. React with 👍/👎 to bot responses!")
            await self._send_response(message.channel, "\n".join(lines))

        elif cmd == "!help":
            help_text = """
**Commands:**
`!context` - Show current context size and cost estimate
`!cost` - Show total API usage and cost per model
`!memories` - List all memories (both types)
`!threads` - Show other recent threads in this channel
`!search <query>` - Web search via Claude, Deepseek, or Gemini (costs extra, ~$0.01-0.03)

**Multi-model (Hydra / MAGI):**
`!claude <message>` / `!opus <message>` / `!balthasar <message>` - Force Claude to respond
`!deepseek <message>` / `!melchior <message>` - Force Deepseek to respond
`!gemini <message>` / `!caspar <message>` - Force Gemini to respond
`!think <message>` - Use extended thinking (deeper reasoning, slower & costlier)
`!think:<level> <message>` - Force a specific effort level (low|medium|high|xhigh|max)
`!models` - Show available models and their usage stats
`!prefer [claude|deepseek|gemini|auto]` - Set model preference for this channel
`!calibration` - Show model confidence calibration stats
React with 👍/👎 to bot responses to improve model selection
Stack prefixes to combine: `!think !claude <message>` forces Claude with thinking on.
Thinking auto-enables on `!claude`/`!opus` when prompts look hard (cues like "derive",
"why does X", "step by step", LaTeX, large code blocks, stack traces). The chosen
effort level is shown in the response routing.

**Long-term memory (permanent):**
`!remember <key> <value>` - Store a permanent memory
`!forget <key>` - Remove a memory (works for both types)
`!summarize <key>` - Auto-summarize this thread and save it
`!summarize <key> <summary>` - Save your own thread summary

**Bookclub mode (pinned long texts):**
`!load <url>` - Load an AO3 work into this channel's context
`!unload` - Drop the loaded work
`!reading` - Show what's currently loaded + chapter table of contents
Once loaded, every model sees the full text on every turn (Claude + Gemini use caching to keep cost down). Discussion across `!claude`, `!deepseek`, `!gemini` all share the same source text.

**Working memory (auto-managed):**
The AI automatically jots down notes during conversation.
These fade after ~48h if not relevant, or stick around if referenced.
`!keep <key>` - Promote a working note to permanent memory

**Legend for working notes:**
🟢 Fresh (>70% life remaining)
🟡 Fading (30-70% life)
🔴 Almost gone (<30% life)

**Features:**
📷 Upload images and I can see them (Claude + Gemini have vision)
💬 I respond in threads (one channel, multiple convos)
📎 Long code blocks become file attachments
😀 I can react to your messages with emoji
🧵 I can see other threads for context
🔍 Web search with citations (Claude + Gemini native; Deepseek via Tavily)
📚 Bookclub mode: `!load <ao3-url>` pins a fic to the channel for cross-model discussion
🐉 Multi-model: Claude (Balthasar) + Deepseek (Melchior) + Gemini (Caspar) with smart routing
            """
            await message.channel.send(help_text)

# =============================================================================
# MAIN
# =============================================================================

def main():
    # Validate environment
    if not os.getenv("DISCORD_TOKEN"):
        print("❌ DISCORD_TOKEN not set in environment!")
        return
    if not any(os.getenv(k) for k in ("ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY", "GEMINI_API_KEY")):
        print("❌ At least one API key required: ANTHROPIC_API_KEY, DEEPSEEK_API_KEY, or GEMINI_API_KEY")
        return

    bot = ClaudeBot()
    bot.run(os.getenv("DISCORD_TOKEN"))

if __name__ == "__main__":
    main()
