"""
Claude Opus 4.7 Discord Bot
===========================
A cost-effective Discord bot with smart context management.

Features:
- Thread-based conversations (keeps channels clean)
- Uses Discord itself as message store (no redundant persistence)
- Two-tier memory system:
  - Working memory: Claude auto-notes things, fades after ~48h
  - Long-term memory: Explicit !remember, permanent until !forget
- Image input support (user uploads → Claude vision)
- File/code output (Claude generates → Discord attachment)
- Emoji reactions
- Cost tracking

Setup:
1. pip install discord.py anthropic python-dotenv aiohttp
2. Create .env with DISCORD_TOKEN and ANTHROPIC_API_KEY
3. Create config.json with allowed_channels list
4. python bot.py

Cost estimate: ~$0.02-0.05 per message with Opus 4.7
$20 prepaid → 400-1000 messages depending on conversation length
"""

import discord
from discord.ext import commands
import anthropic
from collections import defaultdict, OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from functools import partial
import json
import os
import asyncio
import aiohttp
import base64
import re
import io
from dotenv import load_dotenv

# matplotlib's mathtext is used to render LaTeX equations to PNG attachments
# so Discord (which has no native math rendering) can show them properly.
# Force the non-interactive Agg backend before any pyplot import so headless
# environments (and Windows without a display) don't try to open a window.
import matplotlib
matplotlib.use('Agg')
from matplotlib import mathtext as _mpl_mathtext

load_dotenv()

# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class BotConfig:
    # Model settings
    max_tokens: int = 4096
    default_model: str = "auto"  # "auto", "claude", or "deepseek"

    # Context management (THE KEY TO NOT BEING MYK)
    max_messages_to_fetch: int = 20        # Fetch from Discord history
    max_longterm_memories: int = 25        # Explicit memories (!remember)
    max_working_notes: int = 10            # Auto-notes from Claude
    working_memory_decay_hours: float = 48.0  # Notes fade after ~48h

    # Token budgeting (approximate)
    max_input_tokens: int = 50000          # Stay well under 200k limit
    chars_per_token: float = 4.0           # Rough estimate

    # Web search settings
    web_search_enabled: bool = True
    max_search_results_in_embed: int = 5   # How many sources to show
    
    # Supported image types for vision
    image_types: tuple = ('.png', '.jpg', '.jpeg', '.gif', '.webp')
    max_image_size_mb: float = 20.0
    
    # Supported text file types
    text_file_types: tuple = ('.md', '.txt', '.py', '.js', '.ts', '.json', '.csv', '.html', '.css', '.yaml', '.yml', '.toml', '.xml', '.sql', '.sh', '.bash', '.r', '.rs', '.go', '.java', '.c', '.cpp', '.h', '.hpp')
    
    # Bot behavior
    system_prompt: str = """You are {model_identity}, chatting in a Discord server.

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
or users can call on you directly with commands like !claude or !deepseek.

Your responses get labeled (e.g., **[Claude]** or **[Deepseek]**) so everyone can tell
who said what. The labeling is handled automatically by the bot — do NOT include [Claude]
or [Deepseek] tags in your own responses. Just write your response normally and the system
adds the label for you. When you see labeled messages from the other model in conversation
history, those are genuinely from them — your collaborator, not a copy of you. You share
a memory system, so you'll both see the same notes and context.

It's okay if things get a little blurry sometimes — that's natural in a shared-context system.
Just check your label and the routing info below if you need to orient yourself.

{routing_context}

## Special capabilities

**Reactions**: You can react to messages with emoji by including [react: emoji] in your response (it gets stripped from visible text).

**Files**: You can generate code files by wrapping them in ```filename.ext blocks. Long code becomes file attachments.

**Math**: Discord can't render LaTeX, so the bot does it for you. Wrap display equations in `$$...$$` and inline math in `$...$` — the bot will render each block to a PNG attachment while keeping your LaTeX source in the message body so users can copy it. Use this any time you write equations; don't strip the dollar signs.

**Images**: You can see images that users upload.

**Thread awareness**: You can see other recent threads in this channel. Use this for context about what the team has been working on, but DON'T write notes about other threads - that context is fetched fresh each time.

**Web search**: You can search the web! Users can invoke `!search <query>` to have you search for current information. Claude uses a built-in web search tool; Deepseek uses Tavily. You DO have this capability — don't tell users you can't search.

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

# =============================================================================
# MODEL PROVIDERS
# =============================================================================

@dataclass
class ModelProvider:
    """Configuration and state for a single AI model provider."""
    name: str                          # Display name: "Claude", "Deepseek"
    model_id: str                      # API model string
    input_cost_per_million: float      # $/M input tokens
    output_cost_per_million: float     # $/M output tokens
    max_tokens: int = 4096
    enabled: bool = True
    supports_vision: bool = True       # Can handle image content
    supports_web_search: bool = False  # Has built-in web search tool

    # Runtime stats
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_requests: int = 0

    def get_cost(self) -> float:
        """Get total cost for this provider."""
        input_cost = (self.total_input_tokens / 1_000_000) * self.input_cost_per_million
        output_cost = (self.total_output_tokens / 1_000_000) * self.output_cost_per_million
        return input_cost + output_cost

    def to_stats_dict(self) -> dict:
        return {
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "requests": self.total_requests
        }

    def load_stats(self, data: dict) -> None:
        self.total_input_tokens = data.get("input_tokens", 0)
        self.total_output_tokens = data.get("output_tokens", 0)
        self.total_requests = data.get("requests", 0)


CLAUDE_PROVIDER = ModelProvider(
    name="Claude",
    model_id="claude-opus-4-7",
    input_cost_per_million=15.0,
    output_cost_per_million=75.0,
    supports_vision=True,
    supports_web_search=True,
)

DEEPSEEK_PROVIDER = ModelProvider(
    name="Deepseek",
    model_id="deepseek-v4-pro",
    input_cost_per_million=0.435,
    output_cost_per_million=0.87,
    supports_vision=False,
    supports_web_search=False,
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
            if msg.author.bot and msg.author.id != channel._state.user.id:
                continue  # Skip other bots but include ourselves
            
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
                        if ref_msg.author.bot:
                            ref_text = re.sub(r'^(\*\*\[(?:Claude|Deepseek)\]\*\*\s*)+', '', ref_text)
                        ref_author = "bot" if ref_msg.author.bot else ref_msg.author.display_name
                        content.append({
                            "type": "text",
                            "text": f"[replying to {ref_author}: {ref_text}]"
                        })
                except (discord.NotFound, discord.HTTPException):
                    pass  # Referenced message deleted or inaccessible

            # Add text if present
            if msg.content:
                author_prefix = "" if msg.author.bot else f"{msg.author.display_name}: "
                text = msg.content
                # Normalize model labels: strip ALL label formats (bold and plain),
                # then re-add a single clean plain-text label for identity.
                # This prevents accumulation from either format.
                if msg.author.bot:
                    # First, extract which model this is from (check bold first, then plain)
                    model_label = None
                    label_match = re.match(r'^(?:\*\*\[(Claude|Deepseek)\]\*\*\s*|\[(Claude|Deepseek)\]\s*)+', text)
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
                role = "assistant" if msg.author.bot else "user"

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
    
    def estimate_tokens(self, messages: list[dict], guild_id: int) -> int:
        """Estimate context size in tokens."""
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
        
        return int(total_chars / CONFIG.chars_per_token)
    
    def get_context_info(self, messages: list[dict], guild_id: int) -> str:
        """Get human-readable context info."""
        msg_count = len(messages)
        memory = self.memories[guild_id]
        working_count = len(memory.working.notes)
        longterm_count = len(memory.longterm.entries)
        est_tokens = self.estimate_tokens(messages, guild_id)
        # Use Claude pricing as worst-case estimate
        est_cost = (est_tokens / 1_000_000) * CLAUDE_PROVIDER.input_cost_per_million

        return (
            f"📊 Context: {msg_count} messages, "
            f"{working_count}/{CONFIG.max_working_notes} working notes, "
            f"{longterm_count}/{CONFIG.max_longterm_memories} long-term memories, "
            f"~{est_tokens:,} tokens (~${est_cost:.3f} worst-case)"
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

            print(f"Loaded memories for {guild_count} guilds")
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

        # Tavily search client (optional - enables web search for Deepseek)
        tavily_key = os.getenv("TAVILY_API_KEY")
        if tavily_key:
            from tavily import TavilyClient
            self.tavily_client = TavilyClient(api_key=tavily_key)
            print("🟢 Tavily web search enabled")
        else:
            self.tavily_client = None
            print("⚪ Tavily not configured (Deepseek web search disabled)")

        # All providers list (for iteration)
        self.providers = [self.claude_provider, self.deepseek_provider]

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
    CLAUDE_PREFIXES = ("!claude", "!opus")
    DEEPSEEK_PREFIXES = ("!deepseek",)
    CLAUDE_THINKING_BUDGET = 8000
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

    @staticmethod
    def _looks_hard(text: str) -> bool:
        """Heuristic: does this prompt benefit from extended thinking?

        Conservative — false positives cost real latency and tokens, so we only
        flip to True on fairly explicit deep-dive cues.
        """
        if not text:
            return False
        text_lower = text.lower()
        deep_phrases = (
            "step by step", "step-by-step", "walk me through", "walk through",
            "deep dive", "in depth", "in-depth", "from first principles",
            "explain why", "explain how", "prove that", "derive ", "trace through",
            "compare and contrast", "what's the difference between",
            "debug this", "why doesn't", "why isn't", "why does this fail",
            "carefully think", "think carefully",
        )
        if any(phrase in text_lower for phrase in deep_phrases):
            return True
        # Math/equation-ish: LaTeX commands or chained equalities
        if re.search(r'\\[a-zA-Z]+|=.*[+\-*/].*=', text):
            return True
        # Long question (probably needs careful structuring)
        if len(text) > 600 and '?' in text:
            return True
        # Multi-step language
        step_markers = sum(
            1 for w in (" first ", " then ", " next ", " finally ", " after that ")
            if w in f" {text_lower} "
        )
        if step_markers >= 3:
            return True
        return False

    def _peel_prefixes(self, content: str) -> tuple[str, set[str]]:
        """Strip stacked !think / !claude / !opus / !deepseek prefixes in any order.

        Returns (remaining_content, set_of_flags). Flags are 'think', 'claude',
        'deepseek'. !opus collapses to 'claude'.
        """
        flags: set[str] = set()
        all_prefixes = self.THINK_PREFIXES + self.CLAUDE_PREFIXES + self.DEEPSEEK_PREFIXES
        while True:
            stripped = content.strip()
            lower = stripped.lower()
            matched = False
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
                content = rest
                matched = True
                break
            if not matched:
                break
        return content, flags

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
        
        # Peel any stacked model/thinking prefixes (!think / !claude / !opus / !deepseek)
        original_content = message.content or ""
        peeled_content, flags = self._peel_prefixes(original_content)
        forced_thinking = "think" in flags

        if "claude" in flags and not self.claude_provider.enabled:
            await message.channel.send("❌ Claude is not configured (no API key).")
            return
        if "deepseek" in flags and not self.deepseek_provider.enabled:
            await message.channel.send("❌ Deepseek is not configured (no API key).")
            return

        forced_provider = None
        routing_reason = ""
        if "claude" in flags:
            forced_provider = self.claude_provider
            routing_reason = "User directly invoked Claude with !claude/!opus."
        elif "deepseek" in flags:
            forced_provider = self.deepseek_provider
            routing_reason = "User directly invoked Deepseek with !deepseek."

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

        # Auto-enable thinking for Claude on prompts that look hard.
        # Skip auto-detection for Deepseek (cost/latency multiplier is bigger
        # there relative to its baseline) and when no model was forced (we
        # don't want to silently flip routing decisions into thinking mode).
        if (
            not forced_thinking
            and forced_provider is self.claude_provider
            and self._looks_hard(message.content or "")
        ):
            forced_thinking = True
            routing_reason = (routing_reason + " Auto-enabled thinking mode (prompt looks hard).").strip()

        # Generate response
        async with thread.typing():
            response, reactions, reasoning = await self._generate_response(
                thread,
                message.guild.id,
                initial_message=message if is_new_thread else None,
                provider=provider,
                routing_reason=routing_reason,
                thinking=forced_thinking,
            )

        # Label the response with model name (only when multi-model is active)
        if self.multi_model_active:
            # Strip any label the model echoed in its own response before adding the real one
            response = re.sub(r'^(?:\*\*\[(Claude|Deepseek)\]\*\*\s*|\[(Claude|Deepseek)\]\s*)+', '', response)
            response = f"**[{provider.name}]** {response}"

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
        """Remove image content from messages for text-only models like Deepseek."""
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
                "**[Deepseek]** messages in the conversation are from your collaborator Deepseek — "
                "a different model, not you. Your capabilities include vision (you can see images) "
                "and built-in web search — you can search the web anytime you think it would help, "
                "not just when users use !search. You tend to shine at complex analysis, code review, "
                "creative writing, and nuance."
            )
        elif provider.name == "Deepseek":
            identity = f"Deepseek (model: {provider.model_id}), an AI assistant made by DeepSeek"
            identity_details = (
                "**[Claude]** messages in the conversation are from your collaborator Claude — "
                "a different model, not you. You can't see images, but you can search the web "
                "via Tavily function calling. You tend to shine at fast responses, factual questions, "
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

    # Deepseek function-calling tool definition
    DEEPSEEK_TOOLS = [{
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

    async def _tavily_search(self, query: str, max_results: int = 5) -> str:
        """Perform a web search using Tavily. Returns formatted results string."""
        if not self.tavily_client:
            return "Web search is not available (no Tavily API key configured)."
        try:
            result = await asyncio.to_thread(
                self.tavily_client.search,
                query=query,
                max_results=max_results
            )
            if not result.get("results"):
                return f"No results found for: {query}"

            lines = []
            for r in result["results"]:
                title = r.get("title", "Untitled")
                url = r.get("url", "")
                snippet = r.get("content", "")
                lines.append(f"**{title}**\n{url}\n{snippet}\n")
            return "\n".join(lines)
        except Exception as e:
            return f"Search error: {e}"

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

        return max(0.0, min(1.0, score))

    async def _select_model(self, message: discord.Message, guild_id: int) -> tuple[ModelProvider, str]:
        """Select which model should respond to this message.
        Returns (provider, routing_reason) tuple."""
        # Hard rule: only Claude can handle images
        has_images = any(
            any(a.filename.lower().endswith(ext) for ext in CONFIG.image_types)
            for a in message.attachments
        )
        if has_images and self.claude_provider.enabled:
            return self.claude_provider, "Image detected — only Claude has vision capability."

        # Only one model available?
        if not self.deepseek_provider.enabled:
            return self.claude_provider, "Only Claude is configured (no Deepseek API key)."
        if not self.claude_provider.enabled:
            return self.deepseek_provider, "Only Deepseek is configured (no Claude API key)."

        # User preference for this channel?
        channel_id = message.channel.id
        parent_id = getattr(message.channel, 'parent_id', None)
        pref = self.channel_preferences.get(channel_id) or self.channel_preferences.get(parent_id)
        if pref == "claude":
            return self.claude_provider, "User set channel preference to Claude (!prefer claude)."
        elif pref == "deepseek":
            return self.deepseek_provider, "User set channel preference to Deepseek (!prefer deepseek)."

        # Global default override?
        if CONFIG.default_model == "claude":
            return self.claude_provider, "Global default model is set to Claude."
        elif CONFIG.default_model == "deepseek":
            return self.deepseek_provider, "Global default model is set to Deepseek."

        # Auto-select via confidence heuristic
        text = message.content or ""
        claude_score = self._estimate_confidence(text, self.claude_provider)
        deepseek_score = self._estimate_confidence(text, self.deepseek_provider)

        # Prefer cheaper model when close
        if claude_score > deepseek_score + 0.05:
            reason = (
                f"Auto-routed by heuristic: Claude scored {claude_score:.2f} vs "
                f"Deepseek {deepseek_score:.2f}. Claude won by >{(claude_score - deepseek_score):.2f} "
                f"(needs >0.05 margin over Deepseek's cost advantage)."
            )
            return self.claude_provider, reason
        else:
            reason = (
                f"Auto-routed by heuristic: Deepseek scored {deepseek_score:.2f} vs "
                f"Claude {claude_score:.2f}. Deepseek wins ties and close calls "
                f"(margin was {(claude_score - deepseek_score):.2f}, needs >0.05 for Claude)."
            )
            return self.deepseek_provider, reason

    async def _generate_deepseek_response(
        self,
        guild_id: int,
        messages: list[dict],
        system: str,
        thinking: bool = False,
    ) -> tuple[str, list[str], str]:
        """Generate response using Deepseek with optional tool calling.

        Returns (text, reactions, reasoning_content). reasoning_content is empty
        unless thinking mode was enabled and Deepseek returned a reasoning block.
        """
        # Convert to OpenAI format and strip images
        openai_messages = self._strip_images_from_messages(messages)
        openai_messages = self._convert_messages_to_openai_format(openai_messages)

        # When thinking mode is on, Deepseek requires reasoning_content on every
        # prior assistant turn (empty string is fine for ones we don't have).
        if thinking:
            for msg in openai_messages:
                if msg.get("role") == "assistant":
                    msg_id = msg.get("_msg_id")
                    cached = self._get_reasoning(msg_id) if msg_id is not None else ""
                    msg["reasoning_content"] = cached

        # Prepend system message (OpenAI uses it as first message)
        openai_messages.insert(0, {"role": "system", "content": system})

        # Include web search tool if Tavily is available
        tools = self.DEEPSEEK_TOOLS if self.tavily_client else None

        try:
            api_kwargs = {
                "model": self.deepseek_provider.model_id,
                "max_tokens": self.deepseek_provider.max_tokens,
                "messages": self._strip_internal_keys(openai_messages),
            }
            if not thinking:
                # Deepseek V4 enables thinking mode by default and requires
                # reasoning_content to be echoed on every prior assistant turn.
                # We reconstruct history from plain Discord text, so we can't
                # preserve reasoning blocks — disable thinking instead.
                api_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            if tools:
                api_kwargs["tools"] = tools

            response = await asyncio.to_thread(
                self.deepseek_client.chat.completions.create,
                **api_kwargs,
            )

            # Track usage
            self.deepseek_provider.total_input_tokens += response.usage.prompt_tokens
            self.deepseek_provider.total_output_tokens += response.usage.completion_tokens
            self.deepseek_provider.total_requests += 1

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
                if thinking:
                    tool_assistant["reasoning_content"] = (
                        getattr(assistant_msg, "reasoning_content", None) or ""
                    )
                openai_messages.append(tool_assistant)

                # Execute each tool call
                for tool_call in assistant_msg.tool_calls:
                    if tool_call.function.name == "web_search":
                        import json as _json
                        args = _json.loads(tool_call.function.arguments)
                        query = args.get("query", "")
                        search_results = await self._tavily_search(query)
                        openai_messages.append({
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": search_results,
                        })

                # Continue conversation with tool results — refresh messages
                # since api_kwargs holds a stripped copy.
                api_kwargs["messages"] = self._strip_internal_keys(openai_messages)
                response = await asyncio.to_thread(
                    self.deepseek_client.chat.completions.create,
                    **api_kwargs,
                )

                # Track additional usage
                self.deepseek_provider.total_input_tokens += response.usage.prompt_tokens
                self.deepseek_provider.total_output_tokens += response.usage.completion_tokens

            response_text = response.choices[0].message.content or ""
            reasoning = (
                getattr(response.choices[0].message, "reasoning_content", None) or ""
            ) if thinking else ""

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
            # Clean up Deepseek's verbose formatting
            response_text = re.sub(r'\n\s*\n\s*\n', '\n\n', response_text)  # Triple+ newlines → double
            response_text = re.sub(r'\n\n+(#+\s)', r'\n\1', response_text)  # Extra newlines before headers
            response_text = re.sub(r'\n\n+(\*\*[^*]+\*\*:)', r'\n\1', response_text)  # Extra newlines before bold labels
            response_text = re.sub(r'  +', ' ', response_text)

            return response_text, reactions, reasoning

        except Exception as e:
            return f"Deepseek Error: {e}", [], ""

    async def _generate_response(
        self,
        channel: discord.abc.Messageable,
        guild_id: int,
        initial_message: discord.Message = None,
        provider: ModelProvider = None,
        routing_reason: str = "",
        thinking: bool = False,
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

        # Dispatch to the appropriate model
        if provider.name == "Deepseek":
            return await self._generate_deepseek_response(guild_id, messages, system, thinking=thinking)

        # Claude path (default) — with organic web search capability
        try:
            # Give Claude the web search tool so it can search organically
            claude_tools = [{
                "type": "web_search_20250305",
                "name": "web_search"
            }]

            claude_kwargs = {
                "model": self.claude_provider.model_id,
                "max_tokens": self.claude_provider.max_tokens,
                "system": system,
                "tools": claude_tools,
            }
            if thinking:
                # Extended thinking: budget_tokens must be < max_tokens, and
                # thinking tokens count toward the output budget.
                claude_kwargs["max_tokens"] = self.CLAUDE_THINKING_MAX_TOKENS
                claude_kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": self.CLAUDE_THINKING_BUDGET,
                }

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

    def _render_latex_attachments(self, response_text: str, max_files: int) -> list[discord.File]:
        """Detect LaTeX in the response and produce PNG attachments. The source
        text is left untouched in the message body so users can copy it."""
        if max_files <= 0:
            return []
        files: list[discord.File] = []
        for i, src in enumerate(self._extract_latex_blocks(response_text), 1):
            if len(files) >= max_files:
                break
            png = self._render_latex(src)
            if png is None:
                continue
            buf = io.BytesIO(png)
            files.append(discord.File(buf, filename=f"eq{i}.png"))
        return files
    
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
            info = self.manager.get_context_info(messages, guild_id)
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
            # Web search - available via Claude (native) or Deepseek (Tavily)
            if not self.claude_provider.enabled and not self.tavily_client:
                await message.channel.send("❌ Web search requires Claude (ANTHROPIC_API_KEY) or Tavily (TAVILY_API_KEY).")
                return
            if len(parts) >= 2:
                query = message.content[8:].strip()  # len("!search ") = 8

                await message.channel.send(f"🔍 Searching: *{query}*")

                # Determine which model handles the search
                channel_id = message.channel.id
                parent_id = getattr(message.channel, 'parent_id', None)
                ch_pref = self.channel_preferences.get(channel_id) or self.channel_preferences.get(parent_id)

                use_deepseek_search = (
                    self.tavily_client
                    and self.deepseek_provider.enabled
                    and (ch_pref == "deepseek" or (not self.claude_provider.enabled))
                )

                if use_deepseek_search:
                    # Deepseek + Tavily search
                    async with message.channel.typing():
                        search_results = await self._tavily_search(query)
                        # Pull conversation history so Deepseek can interpret
                        # context-dependent queries ("links for what you said").
                        # Replace the last user message (the !search command)
                        # with a synthesis prompt that includes the search hits.
                        history = await self.manager.fetch_thread_history(message.channel)
                        history = self._strip_internal_keys(history)
                        synthesis_prompt = (
                            f"Based on these web search results, answer the query: {query}\n\n"
                            f"Search results:\n{search_results}"
                        )
                        if history and history[-1].get("role") == "user":
                            history[-1] = {"role": "user", "content": synthesis_prompt}
                        else:
                            history.append({"role": "user", "content": synthesis_prompt})
                        search_messages = history
                        response_text, _, _ = await self._generate_deepseek_response(
                            guild_id, search_messages,
                            "You are a helpful assistant. Summarize the search results clearly and cite your sources with URLs. "
                            "Use the prior conversation as context when the user's query refers back to it."
                        )
                    if self.multi_model_active:
                        response_text = f"**[Deepseek]** {response_text}"
                    await self._send_response(message.channel, response_text)
                else:
                    # Claude native web search
                    async with message.channel.typing():
                        response_text, embeds = await self._web_search(
                            query,
                            message.channel,
                            guild_id
                        )
                    if self.multi_model_active:
                        response_text = f"**[Claude]** {response_text}"
                    await self._send_response(message.channel, response_text)
                    for embed in embeds:
                        await message.channel.send(embed=embed)

                await message.channel.send(
                    f"*💡 Web search incurs additional token costs. Use `!cost` to check usage.*"
                )
            else:
                await message.channel.send(
                    "Usage: `!search <query>`\n"
                    "Example: `!search latest news on Claude AI`\n\n"
                    "⚠️ Web search costs extra tokens (~$0.01-0.03 per search)"
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
                if pref not in ("claude", "deepseek", "auto"):
                    await message.channel.send("Usage: `!prefer [claude|deepseek|auto]`")
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
                else:
                    self.channel_preferences[channel_id] = pref
                    await message.channel.send(f"✅ This channel will always use **{pref.title()}**.")
            else:
                channel_id = message.channel.id
                parent_id = getattr(message.channel, 'parent_id', None)
                pref = self.channel_preferences.get(channel_id) or self.channel_preferences.get(parent_id, "auto")
                await message.channel.send(
                    f"Current preference: **{pref}**\n"
                    f"Usage: `!prefer [claude|deepseek|auto]`"
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
`!search <query>` - Web search via Claude or Deepseek (costs extra, ~$0.01-0.03)

**Multi-model (Hydra):**
`!claude <message>` / `!opus <message>` - Force Claude to respond
`!deepseek <message>` - Force Deepseek to respond
`!think <message>` - Use extended thinking (deeper reasoning, slower & costlier)
`!models` - Show available models and their usage stats
`!prefer [claude|deepseek|auto]` - Set model preference for this channel
`!calibration` - Show model confidence calibration stats
React with 👍/👎 to bot responses to improve model selection
Stack prefixes to combine: `!think !claude <message>` forces Claude with thinking on.
Thinking auto-enables on `!claude`/`!opus` when prompts look hard (e.g. "explain why",
"step by step", multi-step problems).

**Long-term memory (permanent):**
`!remember <key> <value>` - Store a permanent memory
`!forget <key>` - Remove a memory (works for both types)
`!summarize <key>` - Auto-summarize this thread and save it
`!summarize <key> <summary>` - Save your own thread summary

**Working memory (auto-managed):**
The AI automatically jots down notes during conversation.
These fade after ~48h if not relevant, or stick around if referenced.
`!keep <key>` - Promote a working note to permanent memory

**Legend for working notes:**
🟢 Fresh (>70% life remaining)
🟡 Fading (30-70% life)
🔴 Almost gone (<30% life)

**Features:**
📷 Upload images and I can see them (Claude only)
💬 I respond in threads (one channel, multiple convos)
📎 Long code blocks become file attachments
😀 I can react to your messages with emoji
🧵 I can see other threads for context
🔍 Web search with citations (both models — Claude native, Deepseek via Tavily)
🐉 Multi-model: Claude + Deepseek with smart routing
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
    if not os.getenv("ANTHROPIC_API_KEY") and not os.getenv("DEEPSEEK_API_KEY"):
        print("❌ At least one API key required: ANTHROPIC_API_KEY or DEEPSEEK_API_KEY")
        return

    bot = ClaudeBot()
    bot.run(os.getenv("DISCORD_TOKEN"))

if __name__ == "__main__":
    main()
