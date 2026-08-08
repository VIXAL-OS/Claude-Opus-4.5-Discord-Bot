"""Offline checks for the session/ambient participation gate (no live APIs).

Covers state transitions, six-hour solo reset, PluralKit speaker identity,
deterministic silence/invitation rules, stale/cooldown protection, Haiku JSON
thresholding, usage accounting, and persistence round-trips.
"""
import asyncio
import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import bot

B = bot.ClaudeBot
ok = fail = 0


def check(name, got, want):
    global ok, fail
    if got == want:
        ok += 1
    else:
        fail += 1
        print(f"  FAIL {name}: got {got!r}, want {want!r}")


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


print("ParticipationState transitions")
state = bot.ParticipationState()
state.observe("user:1", "Sarah", NOW, 1, 6)
check("one speaker is session", state.auto_mode(NOW, 6), "session")
state.observe("proxy:9:xandra", "Xandra", NOW + timedelta(minutes=1), 2, 6)
check("second speaker triggers ambient", state.auto_mode(NOW + timedelta(minutes=1), 6), "ambient")
check(
    "resume is six hours after multi-speaker evidence",
    state.session_resumes_at(NOW + timedelta(minutes=1), 6),
    NOW + timedelta(hours=6, minutes=1),
)
state.observe("proxy:9:xandra", "Xandra", NOW + timedelta(hours=5), 3, 6)
check("lone active speaker does not refresh hold", state.session_resumes_at(NOW + timedelta(hours=5), 6), NOW + timedelta(hours=6, minutes=1))
check("ambient expires after six quiet hours", state.auto_mode(NOW + timedelta(hours=6, minutes=1), 6), "session")

distant = bot.ParticipationState()
distant.observe("user:1", "Sarah", NOW, 1, 6)
distant.observe("user:2", "Bob", NOW + timedelta(minutes=16), 2, 6)
check("second speaker outside activation window stays session", distant.auto_mode(NOW + timedelta(minutes=16), 6), "session")

switching = bot.ParticipationState()
switching.observe("user:1", "Sarah", NOW, 1, 6)
switching.observe("user:2", "Bob", NOW + timedelta(minutes=1), 2, 6)
switching.observe("user:1", "Sarah", NOW + timedelta(hours=5), 3, 6)
check("later speaker switch renews hold", switching.session_resumes_at(NOW + timedelta(hours=5), 6), NOW + timedelta(hours=11))

reply_state = bot.ParticipationState()
reply_state.observe(
    "user:2", "Bob", NOW, 10, 6,
    reply_target=("user:1", "Alice"),
)
check("cross-human reply is immediate ambient", reply_state.auto_mode(NOW, 6), "ambient")

roundtrip = bot.ParticipationState.from_dict(reply_state.to_dict())
check("state persistence keys", set(roundtrip.speaker_last_seen), {"user:1", "user:2"})
check("state persistence mode", roundtrip.auto_mode(NOW, 6), "ambient")
check("malformed persistence fails soft", bot.ParticipationState.from_dict("oops").speaker_last_seen, {})

fd, state_path = tempfile.mkstemp(suffix=".json")
os.close(fd)
try:
    usage = bot.ModelProvider(
        name="Haiku participation gate", model_id="claude-haiku-4-5",
        input_cost_per_million=1.0, output_cost_per_million=5.0,
    )
    usage.record_usage(10, 2)
    usage.total_requests = 1
    manager = bot.ConversationManager()
    manager.participation_states[123] = reply_state
    manager.save_memories(state_path, providers=[usage])
    loaded_usage = bot.ModelProvider(
        name="Haiku participation gate", model_id="claude-haiku-4-5",
        input_cost_per_million=1.0, output_cost_per_million=5.0,
    )
    loaded = bot.ConversationManager()
    loaded.load_memories(state_path, providers=[loaded_usage])
    check("manager persists participation", loaded.participation_states[123].auto_mode(NOW, 6), "ambient")
    check("manager persists gate stats", loaded_usage.total_requests, 1)
finally:
    os.remove(state_path)


print("Speaker identity + deterministic backchannels")


def fake_message(author_id=1, name="Alice", bot_account=False, webhook_id=None, **extra):
    author = types.SimpleNamespace(id=author_id, display_name=name, bot=bot_account)
    values = dict(
        id=extra.pop("id", author_id),
        author=author,
        webhook_id=webhook_id,
        content=extra.pop("content", "hello"),
        attachments=extra.pop("attachments", []),
        mentions=extra.pop("mentions", []),
        created_at=extra.pop("created_at", NOW),
        reference=extra.pop("reference", None),
    )
    values.update(extra)
    return types.SimpleNamespace(**values)


normal = fake_message(author_id=42, name="Alice")
proxy_a = fake_message(author_id=9, name="Xandra", bot_account=True, webhook_id=999)
proxy_b = fake_message(author_id=9, name="Viksalos", bot_account=True, webhook_id=999)
real_bot = fake_message(author_id=8, name="OtherBot", bot_account=True, webhook_id=None)
check("normal identity uses user id", B._speaker_identity(normal), ("user:42", "Alice"))
check("proxy members differ", B._speaker_identity(proxy_a)[0] != B._speaker_identity(proxy_b)[0], True)
check("real bot is not a speaker", B._speaker_identity(real_bot), None)
for text in ("Certainly.", "lol", "sounds good", "👍"):
    check(f"backchannel {text!r}", B._is_obvious_backchannel(text), True)
check("question is not a backchannel", B._is_obvious_backchannel("Could you check this?"), False)
check("attachment is not auto-silenced", B._is_obvious_backchannel("nice", True), False)


def gate_stub(classifier_result=(False, 0.1, "humans_conversing")):
    obj = object.__new__(B)
    obj._connection = types.SimpleNamespace(user=types.SimpleNamespace(id=9999))
    obj.participation_enabled = True
    obj.participation_channel_modes = {}
    obj.participation_solo_reset_hours = 6.0
    obj.participation_cooldown_minutes = 15.0
    obj._participation_classifier_locks = {}
    obj.manager = types.SimpleNamespace(mark_dirty=lambda: None)

    async def classify(_message):
        return classifier_result

    obj._classify_ambient_intervention = classify
    return obj


async def decision_checks():
    print("Participation decisions")
    ambient = bot.ParticipationState()
    ambient.observe("user:1", "Alice", NOW, 1, 6)
    ambient.observe("user:2", "Bob", NOW, 2, 6)
    obj = gate_stub()

    msg = fake_message(author_id=2, id=2, name="Bob", content="Certainly.")
    got = await B._should_participate(obj, msg, 123, ambient, None, False)
    check("ambient acknowledgement silent", (got.should_respond, got.reason), (False, "obvious_backchannel"))

    mentioned = fake_message(
        author_id=2, id=2, name="Bob", content="hey bot",
        mentions=[types.SimpleNamespace(id=9999)],
    )
    got = await B._should_participate(obj, mentioned, 123, ambient, None, False)
    check("mention bypass", (got.should_respond, got.reason), (True, "bot_mention"))

    got = await B._should_participate(obj, msg, 123, ambient, None, True)
    check("prefix bypass", got.should_respond, True)

    bot_target = fake_message(author_id=9999, name="Hydra", bot_account=True)
    got = await B._should_participate(obj, msg, 123, ambient, bot_target, False)
    check("reply-to-bot bypass", got.should_respond, True)

    human_target = fake_message(author_id=1, name="Alice")
    discussion = fake_message(author_id=2, id=2, name="Bob", content="I agree with that analysis")
    got = await B._should_participate(obj, discussion, 123, ambient, human_target, False)
    check("human reply hard silent", (got.should_respond, got.reason), (False, "reply_to_human"))

    single = bot.ParticipationState()
    single.observe("user:2", "Bob", NOW, 2, 6)
    got = await B._should_participate(obj, discussion, 123, single, None, False)
    check("single speaker session replies", got.should_respond, True)

    obj.participation_channel_modes[123] = "tags"
    got = await B._should_participate(obj, discussion, 123, single, None, False)
    check("manual tags silent", (got.should_respond, got.reason), (False, "tag_only_mode"))
    obj.participation_channel_modes.clear()

    approving = gate_stub((True, 0.96, "important_correction"))
    got = await B._should_participate(approving, discussion, 123, ambient, None, False)
    check("high-value Haiku approval", (got.should_respond, got.unsolicited), (True, True))
    check("approval starts cooldown", ambient.last_unsolicited_reply_at, NOW)

    later = fake_message(
        author_id=2, id=3, name="Bob", content="A different substantive observation",
        created_at=NOW + timedelta(minutes=2),
    )
    ambient.latest_human_message_id = 3
    ambient.latest_human_message_at = later.created_at
    got = await B._should_participate(approving, later, 123, ambient, None, False)
    check("unsolicited cooldown", (got.should_respond, got.reason), (False, "unsolicited_reply_cooldown"))

    stale = bot.ParticipationState()
    stale.observe("user:1", "Alice", NOW, 1, 6)
    stale.observe("user:2", "Bob", NOW, 99, 6)
    old = fake_message(author_id=1, id=1, name="Alice", content="Substantive but stale")
    got = await B._should_participate(obj, old, 123, stale, None, False)
    check("stale turn suppressed", (got.should_respond, got.reason), (False, "conversation_advanced"))


async def classifier_checks():
    print("Haiku JSON + usage accounting")

    class FakeMessages:
        def __init__(self, payload):
            self.payload = payload

        def create(self, **_kwargs):
            return types.SimpleNamespace(
                usage=types.SimpleNamespace(input_tokens=120, output_tokens=18),
                content=[types.SimpleNamespace(type="text", text=self.payload)],
            )

    obj = object.__new__(B)
    obj.participation_classifier_model = "claude-haiku-4-5"
    obj.participation_classifier_threshold = 0.9
    obj.participation_client = types.SimpleNamespace(
        messages=FakeMessages('{"action":"respond","intervention_value":0.93,"reason":"urgent correction"}')
    )
    obj.participation_usage = bot.ModelProvider(
        name="Haiku participation gate", model_id="claude-haiku-4-5",
        input_cost_per_million=1.0, output_cost_per_million=5.0,
    )
    dirty = []
    obj.manager = types.SimpleNamespace(mark_dirty=lambda: dirty.append(True))

    async def context(_message):
        return "Alice: claim\nBob: correction?"

    obj._ambient_classifier_context = context
    got = await B._classify_ambient_intervention(obj, fake_message())
    check("valid approval parsed", got, (True, 0.93, "urgent_correction"))
    check("classifier request counted", obj.participation_usage.total_requests, 1)
    check("classifier tokens counted", (obj.participation_usage.total_input_tokens, obj.participation_usage.total_output_tokens), (120, 18))
    check("classifier usage dirties persistence", bool(dirty), True)

    obj.participation_client.messages.payload = '{"action":"respond","intervention_value":0.70,"reason":"merely interesting"}'
    got = await B._classify_ambient_intervention(obj, fake_message())
    check("below threshold stays silent", got, (False, 0.7, "merely_interesting"))


async def main():
    await decision_checks()
    await classifier_checks()


asyncio.run(main())
print(f"\n{ok} passed, {fail} failed")
if fail:
    raise SystemExit(1)
