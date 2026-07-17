# Offline validation for the Kimi K3 head (2026-07-17).
# Run from repo root: PYTHONIOENCODING=utf-8 python scratchpad/test_kimi_provider.py
#
# Drives ProviderRegistry.from_config twice — once with the real env (no
# MOONSHOT_API_KEY => kimi disabled, everything else untouched) and once with a
# fake key (kimi enabled, client wired to api.moonshot.ai) — plus static checks
# on the constant, themes, prefixes, labels, and the reasoning_effort quirk.

import inspect
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# --- Scenario B runs in a SUBPROCESS -----------------------------------------
# from_config resolves the module-level provider constants IN PLACE, so a
# no-key build followed by a with-key build in one process would read the first
# build's "disabled" as a config-level disable. Production only ever calls it
# once per process; the fresh-interpreter subprocess mirrors that.
if len(sys.argv) > 1 and sys.argv[1] == "--scenario-b":
    os.environ["MOONSHOT_API_KEY"] = "sk-fake-for-offline-test"
    import bot
    reg = bot.ProviderRegistry.from_config({})
    kimi = reg.by_id("kimi")
    assert kimi is not None and kimi.enabled is True, "kimi not enabled with key present"
    client = reg.clients.get("kimi")
    assert client is not None, "kimi client not built"
    assert "api.moonshot.ai" in str(client.base_url), f"bad base_url: {client.base_url}"
    assert reg.openai_compatible_clients.get("Kimi") is client, "no name-keyed Kimi client"
    print("scenario-b OK")
    sys.exit(0)

# Scenario A must see no Moonshot key even if the operator's .env gains one later.
os.environ.pop("MOONSHOT_API_KEY", None)

import bot

PASS = 0
FAIL = 0

def check(name: str, cond: bool, detail: str = ""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {detail}")


print("== constant shape ==")
K = bot.KIMI_PROVIDER
check("name/id are Kimi/kimi", K.name == "Kimi" and K.id == "kimi")
check("openai_compatible on api.moonshot.ai/v1",
      K.sdk_type == "openai_compatible" and K.base_url == "https://api.moonshot.ai/v1")
check("key env is MOONSHOT_API_KEY", K.api_key_env == "MOONSHOT_API_KEY")
check("model slug kimi-k3", K.model_id == "kimi-k3")
check("pricing 3/15, cached 0.30",
      K.input_cost_per_million == 3.0 and K.output_cost_per_million == 15.0
      and K.cached_input_cost_per_million == 0.30)
check("1M context", K.max_context_tokens == 1_000_000)
check("vision off, tavily search", K.supports_vision is False and K.search_backend == "tavily")
check("think quirk: reasoning_effort=max, no thinking-param quirks",
      K.think_reasoning_effort == "max"
      and K.requires_reasoning_echo is False
      and K.disables_thinking_by_default is False)
check("fireworks backend stub present (not default)",
      K.backend == "api" and "fireworks" in K.backends
      and K.backends["fireworks"]["model"].startswith("accounts/fireworks/models/"))

print("== canonical order + labels ==")
ids = [p.id for p in bot._PROVIDER_CONSTANTS]
check("kimi sits between glm and sim",
      ids.index("glm") < ids.index("kimi") < ids.index("sim"), str(ids))
check("MODEL_LABEL_NAMES includes Kimi", "|Kimi|" in f"|{bot.MODEL_LABEL_NAMES}|")
import re
strip_re = re.compile(rf"^\[({bot.MODEL_LABEL_NAMES})\]\s*")
check("label regex strips a [Kimi] echo", strip_re.sub("", "[Kimi] hello") == "hello")

print("== cost math ==")
K.record_usage(1_000_000, 1_000_000, cached_input_tokens=1_000_000)
check("1M in + 1M out + 1M cached = $18.30", abs(K.get_cost() - 18.30) < 1e-9,
      f"got {K.get_cost()}")
K.total_input_tokens = K.total_output_tokens = K.total_cached_input_tokens = 0

print("== themes ==")
for tname, alias in (("eva", "!kaworu"), ("isaic", "!issachar"), ("nightvale", "!glowcloud")):
    fl = bot.THEMES[tname].flavors.get("kimi")
    check(f"{tname} has a kimi flavor with {alias}",
          fl is not None and alias in fl.aliases,
          str(fl))
check("nightvale also answers !allhail",
      "!allhail" in bot.THEMES["nightvale"].flavors["kimi"].aliases)
check("eva keeps canonical display name (skin invariant)",
      bot.THEMES["eva"].flavors["kimi"].display_name == "Kimi")

print("== ClaudeBot wiring (static) ==")
check("CANONICAL_PREFIXES kimi -> !kimi/!k3",
      bot.ClaudeBot.CANONICAL_PREFIXES.get("kimi") == ("!kimi", "!k3"))
check("HELP_ROLES has kimi", "kimi" in bot.ClaudeBot.HELP_ROLES)
src = inspect.getsource(bot.ClaudeBot)
check("guard tuple includes kimi",
      "\"glm\", \"kimi\")" in src or "'glm', 'kimi')" in src
      or '"glm", "kimi", "sim")' in src)
check("panel_members_all includes Kimi", '"GLM", "Kimi",' in src)
check("_estimate_confidence has an override-only Kimi branch",
      'provider.name == "Kimi"' in inspect.getsource(bot.ClaudeBot._estimate_confidence))
shim_src = inspect.getsource(bot.ClaudeBot._generate_openai_compatible_response)
check("shim sends reasoning_effort when thinking + quirk set",
      'api_kwargs["reasoning_effort"] = provider.think_reasoning_effort' in shim_src)

print("== registry, scenario A: no MOONSHOT_API_KEY ==")
regA = bot.ProviderRegistry.from_config({})
kimiA = regA.by_id("kimi")
check("kimi present but disabled", kimiA is not None and kimiA.enabled is False)
check("kimi client is None when disabled", regA.clients.get("kimi") is None)
others_ok = all(
    p.enabled == bool(os.getenv(p.api_key_env))
    for p in regA.providers
    if p.id not in ("kimi", "sim") and p.api_key_env
)
check("other heads' enabled state still tracks their own keys", others_ok,
      str([(p.id, p.enabled, p.api_key_env, bool(os.getenv(p.api_key_env or ''))) for p in regA.providers]))

print("== registry, scenario B: fake MOONSHOT_API_KEY (fresh interpreter) ==")
result = subprocess.run(
    [sys.executable, os.path.abspath(__file__), "--scenario-b"],
    capture_output=True, text=True,
    cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    env={**os.environ, "PYTHONIOENCODING": "utf-8"},
)
check("kimi enabled + client wired to api.moonshot.ai with key present",
      result.returncode == 0 and "scenario-b OK" in result.stdout,
      (result.stdout + result.stderr)[-500:])

print()
print(f"{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
