"""
Claude Opus 4.5 Discord Bot
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

Cost estimate: ~$0.02-0.05 per message with Opus 4.5
$20 prepaid → 400-1000 messages depending on conversation length
"""

import discord
from discord.ext import commands
import anthropic
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from functools import partial
import json
import os
import asyncio
import asyncio
import aiohttp
import base64
import re
import io
from dotenv import load_dotenv

load_dotenv()

# =============================================================================
# CONFIGURATION
# =============================================================================

@dataclass
class BotConfig:
    # Model settings
    model: str = "claude-opus-4-5-20251101"
    max_tokens: int = 4096
    
    # Context management (THE KEY TO NOT BEING MYK)
    max_messages_to_fetch: int = 20        # Fetch from Discord history
    max_longterm_memories: int = 25        # Explicit memories (!remember)
    max_working_notes: int = 10            # Auto-notes from Claude
    working_memory_decay_hours: float = 48.0  # Notes fade after ~48h
    
    # Token budgeting (approximate)
    max_input_tokens: int = 50000          # Stay well under 200k limit
    chars_per_token: float = 4.0           # Rough estimate
    
    # Cost tracking (Opus 4.5 pricing as of late 2025)
    input_cost_per_million: float = 15.0   # $15/M input tokens
    output_cost_per_million: float = 75.0  # $75/M output tokens
    
    # Web search settings
    web_search_enabled: bool = True
    max_search_results_in_embed: int = 5   # How many sources to show
    
    # Supported image types for vision
    image_types: tuple = ('.png', '.jpg', '.jpeg', '.gif', '.webp')
    max_image_size_mb: float = 20.0
    
    # Supported text file types
    text_file_types: tuple = ('.md', '.txt', '.py', '.js', '.ts', '.json', '.csv', '.html', '.css', '.yaml', '.yml', '.toml', '.xml', '.sql', '.sh', '.bash', '.r', '.rs', '.go', '.java', '.c', '.cpp', '.h', '.hpp')
    
    # Bot behavior
    system_prompt: str = """You are Claude, an AI assistant made by Anthropic, chatting in a Discord server. 

You're helpful, harmless, and honest. You have a warm, curious personality. You can be playful but you're also genuinely knowledgeable and thoughtful.

Some context about this server:
- This is a development/testing server for Sarah's projects
- The humans here are working on neuroscience research, distributed databases, and AI tooling
- Be concise in casual chat, detailed when asked technical questions
- You can use markdown formatting, but Discord has a 2000 char limit per message

## Special capabilities

**Reactions**: You can react to messages with emoji by including [react: emoji] in your response (it gets stripped from visible text).

**Files**: You can generate code files by wrapping them in ```filename.ext blocks. Long code becomes file attachments.

**Images**: You can see images that users upload.

**Thread awareness**: You can see other recent threads in this channel. Use this for context about what the team has been working on, but DON'T write notes about other threads - that context is fetched fresh each time.

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
        # Cost tracking
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
    
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
            
            # Add text if present
            if msg.content:
                author_prefix = "" if msg.author.bot else f"{msg.author.display_name}: "
                content.append({
                    "type": "text",
                    "text": f"{author_prefix}{msg.content}"
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
                
                # Simplify if just text
                if len(content) == 1 and content[0]["type"] == "text":
                    messages.append({"role": role, "content": content[0]["text"]})
                else:
                    messages.append({"role": role, "content": content})
        
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
        est_cost = (est_tokens / 1_000_000) * CONFIG.input_cost_per_million
        
        return (
            f"📊 Context: {msg_count} messages, "
            f"{working_count}/{CONFIG.max_working_notes} working notes, "
            f"{longterm_count}/{CONFIG.max_longterm_memories} long-term memories, "
            f"~{est_tokens:,} tokens (~${est_cost:.3f})"
        )
    
    def get_cost_summary(self) -> str:
        """Get total cost summary."""
        input_cost = (self.total_input_tokens / 1_000_000) * CONFIG.input_cost_per_million
        output_cost = (self.total_output_tokens / 1_000_000) * CONFIG.output_cost_per_million
        total_cost = input_cost + output_cost
        
        return (
            f"💰 Total usage: {self.total_input_tokens:,} input + "
            f"{self.total_output_tokens:,} output tokens = ${total_cost:.2f}"
        )
    
    def save_memories(self, filepath: str = "memories.json") -> None:
        """Save all memories to disk (synchronous - use save_memories_async in async contexts)."""
        data = {
            str(guild_id): memory.to_dict()
            for guild_id, memory in self.memories.items()
        }
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)
        self._memories_dirty = False
    
    async def save_memories_async(self, filepath: str = "memories.json") -> None:
        """Save memories without blocking the event loop."""
        await asyncio.to_thread(self.save_memories, filepath)
    
    def mark_dirty(self) -> None:
        """Mark memories as needing to be saved."""
        self._memories_dirty = True
    
    @property
    def needs_save(self) -> bool:
        """Check if memories need saving."""
        return getattr(self, '_memories_dirty', False)
    
    def load_memories(self, filepath: str = "memories.json") -> None:
        """Load memories from disk."""
        try:
            with open(filepath, 'r') as f:
                data = json.load(f)
            for guild_id_str, memory_data in data.items():
                self.memories[int(guild_id_str)] = TwoTierMemory.from_dict(
                    memory_data,
                    max_working_notes=CONFIG.max_working_notes,
                    max_longterm_entries=CONFIG.max_longterm_memories,
                    working_decay_hours=CONFIG.working_memory_decay_hours
                )
            print(f"Loaded memories for {len(data)} guilds")
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
        
        # Anthropic client
        self.claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        
        # Conversation manager
        self.manager = ConversationManager()
        
        # Allowed channels (load from config)
        self.allowed_channels: set[int] = set()
        self._load_config()
    
    def _load_config(self) -> None:
        """Load configuration from config.json."""
        try:
            with open('config.json', 'r') as f:
                config = json.load(f)
                self.allowed_channels = set(config.get('allowed_channels', []))
                print(f"Loaded {len(self.allowed_channels)} allowed channels")
        except FileNotFoundError:
            print("⚠️  No config.json found! Create one with {'allowed_channels': [channel_ids]}")
    
    async def setup_hook(self) -> None:
        """Called when bot is ready."""
        self.manager.load_memories()
        # Start background save task
        self._save_task = self.loop.create_task(self._periodic_save())
    
    async def _periodic_save(self) -> None:
        """Background task to save memories every 60 seconds if dirty."""
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                if self.manager.needs_save:
                    await self.manager.save_memories_async()
                    print("💾 Memories saved (background)")
            except Exception as e:
                print(f"⚠️  Error saving memories: {e}")
            await asyncio.sleep(60)  # Check every 60 seconds
    
    async def close(self) -> None:
        """Clean shutdown - save memories before closing."""
        if self.manager.needs_save:
            print("💾 Saving memories before shutdown...")
            await self.manager.save_memories_async()
        await super().close()
    
    async def on_ready(self) -> None:
        print(f"✅ Logged in as {self.user}")
        print(f"📋 Allowed channels: {self.allowed_channels}")
        print(f"🧠 Model: {CONFIG.model}")
    
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
        
        # Handle commands
        if message.content.startswith('!'):
            await self._handle_command(message)
            return
        
        # Ignore empty messages (no text, no attachments)
        if not message.content and not message.attachments:
            return
        
        # Get or create thread
        thread, is_new_thread = await self._ensure_thread(message)
        
        # Generate response
        async with thread.typing():
            response, reactions = await self._generate_response(
                thread, 
                message.guild.id,
                initial_message=message if is_new_thread else None
            )
        
        # Handle reactions first
        for emoji in reactions:
            try:
                await message.add_reaction(emoji)
            except discord.HTTPException:
                pass  # Emoji not found or can't react
        
        # Extract and handle code files
        response, files = self._extract_code_files(response)
        
        # Send response (handle Discord's 2000 char limit)
        await self._send_response(thread, response, files)
        
        # Mark memories as needing save (actual save happens in background task)
        self.manager.mark_dirty()
    
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
    
    async def _generate_response(
        self, 
        channel: discord.abc.Messageable, 
        guild_id: int,
        initial_message: discord.Message = None
    ) -> tuple[str, list[str]]:
        """
        Generate Claude response.
        Returns (response_text, list_of_emoji_reactions)
        Also processes [note: key: value] tags for working memory.
        
        If initial_message is provided, it's prepended to the conversation
        (used when a new thread is created and the starter message isn't in history).
        """
        # Fetch conversation from Discord
        messages = await self.manager.fetch_thread_history(channel)
        
        # If this is a new thread, the triggering message isn't in thread history
        # We need to prepend it manually
        if initial_message:
            content_parts = []
            
            # Add text
            if initial_message.content:
                content_parts.append({
                    "type": "text",
                    "text": f"{initial_message.author.display_name}: {initial_message.content}"
                })
            
            # Add images if present
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
                
                # Handle text files
                elif any(attachment.filename.lower().endswith(ext) for ext in CONFIG.text_file_types):
                    if attachment.size <= 1024 * 1024:  # 1MB limit
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
                # Simplify if just text
                if len(content_parts) == 1 and content_parts[0]["type"] == "text":
                    messages.insert(0, {"role": "user", "content": content_parts[0]["text"]})
                else:
                    messages.insert(0, {"role": "user", "content": content_parts})
        
        if not messages:
            return "I don't see any messages to respond to!", []
        
        # Build system prompt with all context sources
        system_parts = [CONFIG.system_prompt]
        
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
        
        try:
            # Run in thread pool so we don't block Discord's event loop
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model=CONFIG.model,
                max_tokens=CONFIG.max_tokens,
                system=system,
                messages=messages
            )
            
            # Track usage
            self.manager.total_input_tokens += response.usage.input_tokens
            self.manager.total_output_tokens += response.usage.output_tokens
            
            # Handle empty response
            if not response.content:
                return "I received an empty response from the API.", []
            
            response_text = response.content[0].text if hasattr(response.content[0], 'text') else str(response.content[0])
            
            # Extract and process working memory notes
            note_pattern = r'\[note:\s*([^:]+):\s*([^\]]+)\]'
            for match in re.finditer(note_pattern, response_text):
                key = match.group(1).strip()
                value = match.group(2).strip()
                self.manager.memories[guild_id].working.add(key, value)
            
            # Remove note markers from visible response
            response_text = re.sub(note_pattern, '', response_text)
            
            # Extract reactions from response
            reactions = []
            reaction_pattern = r'\[react:\s*([^\]]+)\]'
            for match in re.finditer(reaction_pattern, response_text):
                emoji = match.group(1).strip()
                reactions.append(emoji)
            
            # Remove reaction markers from visible response
            response_text = re.sub(reaction_pattern, '', response_text).strip()
            
            # Clean up any double spaces or weird formatting from removed tags
            response_text = re.sub(r'\n\s*\n\s*\n', '\n\n', response_text)
            response_text = re.sub(r'  +', ' ', response_text)
            
            return response_text, reactions
            
        except anthropic.APIError as e:
            return f"❌ API Error: {e}", []
    
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
            "well-cited answer. Be concise but thorough."
        )
        if memory_context:
            system += f"\n\nContext about the user/server:\n{memory_context}"
        
        messages = [{"role": "user", "content": query}]
        
        try:
            # Initial request with web search tool (run in thread pool)
            response = await asyncio.to_thread(
                self.claude.messages.create,
                model=CONFIG.model,
                max_tokens=CONFIG.max_tokens,
                system=system,
                messages=messages,
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search"
                }]
            )
            
            # Track usage
            self.manager.total_input_tokens += response.usage.input_tokens
            self.manager.total_output_tokens += response.usage.output_tokens
            
            # Collect all sources for embeds
            sources = []
            final_text = ""
            
            # Process response - may need multiple rounds if tool_use
            while response.stop_reason == "tool_use":
                # Find the tool use block
                tool_use_block = None
                for block in response.content:
                    if block.type == "tool_use":
                        tool_use_block = block
                        break
                
                if not tool_use_block:
                    break
                
                # The API handles the actual search - we just continue the conversation
                # Add assistant's response (with tool use) to messages
                messages.append({"role": "assistant", "content": response.content})
                
                # Add a tool result (the API handles the actual search internally)
                # For web_search, we don't need to provide results - it's server-side
                messages.append({
                    "role": "user", 
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": tool_use_block.id,
                        "content": "Search completed."
                    }]
                })
                
                # Continue the conversation (run in thread pool)
                response = await asyncio.to_thread(
                    self.claude.messages.create,
                    model=CONFIG.model,
                    max_tokens=CONFIG.max_tokens,
                    system=system,
                    messages=messages,
                    tools=[{
                        "type": "web_search_20250305",
                        "name": "web_search"
                    }]
                )
                
                # Track additional usage
                self.manager.total_input_tokens += response.usage.input_tokens
                self.manager.total_output_tokens += response.usage.output_tokens
            
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
    
    async def _send_response(
        self, 
        channel: discord.abc.Messageable, 
        content: str, 
        files: list[discord.File] = None
    ) -> None:
        """Send message, chunking if over Discord's limit."""
        if not content and not files:
            return
        
        # If content fits in one message
        if len(content) <= 1990:
            await channel.send(content, files=files)
            return
        
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
        for i, chunk in enumerate(chunks):
            if i == 0:
                await channel.send(chunk, files=files)
            else:
                await channel.send(chunk)
    
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
            summary = self.manager.get_cost_summary()
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
                    self.manager.save_memories()
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
                    self.manager.save_memories()
                    await message.channel.send(f"✅ Forgot `{key}` from long-term memory")
                elif memory.working.remove(key):
                    self.manager.save_memories()
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
                    self.manager.save_memories()
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
            # Web search with Claude
            if len(parts) >= 2:
                # Get the full query (everything after !search)
                query = message.content[8:].strip()  # len("!search ") = 8
                
                await message.channel.send(f"🔍 Searching: *{query}*")
                
                async with message.channel.typing():
                    response_text, embeds = await self._web_search(
                        query, 
                        message.channel,
                        guild_id
                    )
                
                # Send response (may need chunking)
                await self._send_response(message.channel, response_text)
                
                # Send source embeds if any
                for embed in embeds:
                    await message.channel.send(embed=embed)
                
                # Show cost warning
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
                    self.manager.save_memories()
                    await message.channel.send(f"✅ Saved thread summary as `thread_{key}`")
                else:
                    await message.channel.send(
                        f"❌ Long-term memory full. Use `!forget <key>` to make room."
                    )
            elif len(parts) == 2:
                # !summarize <key> - ask Claude to generate summary
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
                            self.claude.messages.create,
                            model=CONFIG.model,
                            max_tokens=200,
                            system="Summarize this conversation in 1-2 sentences. Focus on the key topic and any decisions/outcomes. Be concise.",
                            messages=[{"role": "user", "content": f"Conversation to summarize:\n\n{conversation_text}"}]
                        )
                        summary = summary_response.content[0].text.strip()
                        
                        # Track usage
                        self.manager.total_input_tokens += summary_response.usage.input_tokens
                        self.manager.total_output_tokens += summary_response.usage.output_tokens
                        
                        if memory.longterm.add(f"thread_{key}", summary):
                            self.manager.save_memories()
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
        
        elif cmd == "!help":
            help_text = """
**Commands:**
`!context` - Show current context size and cost estimate
`!cost` - Show total API usage and cost
`!memories` - List all memories (both types)
`!threads` - Show other recent threads in this channel
`!search <query>` - 🔍 Web search (costs extra, ~$0.01-0.03)

**Long-term memory (permanent):**
`!remember <key> <value>` - Store a permanent memory
`!forget <key>` - Remove a memory (works for both types)
`!summarize <key>` - Auto-summarize this thread and save it
`!summarize <key> <summary>` - Save your own thread summary

**Working memory (auto-managed):**
Claude automatically jots down notes during conversation.
These fade after ~48h if not relevant, or stick around if referenced.
`!keep <key>` - Promote a working note to permanent memory

**Legend for working notes:**
🟢 Fresh (>70% life remaining)
🟡 Fading (30-70% life)
🔴 Almost gone (<30% life)

**Features:**
📷 Upload images and I can see them
💬 I respond in threads (one channel, multiple convos)
📎 Long code blocks become file attachments
😀 I can react to your messages with emoji
🧵 I can see other threads for context
🔍 Web search with citations
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
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("❌ ANTHROPIC_API_KEY not set in environment!")
        return
    
    bot = ClaudeBot()
    bot.run(os.getenv("DISCORD_TOKEN"))

if __name__ == "__main__":
    main()
