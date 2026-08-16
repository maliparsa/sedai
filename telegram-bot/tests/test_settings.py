"""Offline verification of settings.py against CONTRACT.md. Run with the stubs dir on sys.path."""

import importlib
import json
import logging
import os
import stat
import sys
import tempfile

# Derive paths from __file__ so the suite runs from any checkout
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_DIR = os.path.dirname(TEST_DIR)
STUBS = os.path.join(TEST_DIR, "stubs")

GOOD = "good-key-aaaa1111"
GOOD2 = "good-key-bbbb2222"
BAD = "bad-key-zzzz9999"

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def fresh(settings_path, env=None):
    """Import settings.py fresh with a given settings.json path and env."""
    for mod in list(sys.modules):
        if mod == "settings":
            del sys.modules[mod]
    os.environ["SEDAI_SETTINGS_PATH"] = settings_path
    os.environ["TELEGRAM_BOT_TOKEN"] = "123:tg-token-secret"
    os.environ["GEMINI_API_KEY"] = GOOD
    os.environ["ALLOWED_USER_ID"] = "111,222,333"
    for k, v in (env or {}).items():
        os.environ[k] = v
    import settings
    importlib.reload(settings)
    if hasattr(settings, "load"):
        settings.load()
    return settings


def main():
    sys.path.insert(0, STUBS)
    sys.path.insert(0, BOT_DIR)
    logging.basicConfig(level=logging.DEBUG)

    tmp = tempfile.mkdtemp()
    p = os.path.join(tmp, "settings.json")

    # --- bootstrap from env, no settings.json present
    s = fresh(p)
    check("admin is first allowed id", s.admin_id() == 111, s.admin_id())
    check("allowed ids ordered", s.allowed_user_ids() == [111, 222, 333], s.allowed_user_ids())
    check("is_admin true for first", s.is_admin(111) and not s.is_admin(222))
    check("is_allowed gate", s.is_allowed(333) and not s.is_allowed(999))
    check("audio default chain head", s.audio_models()[0] == "gemini-flash-latest", s.audio_models())
    check("text default chain head", s.text_models()[0] == "gemini-flash-lite-latest", s.text_models())

    # --- per-user preference promotes to head, keeps fallback, dedupes
    s.set_user_model(222, "text", "gemini-flash-latest")
    chain = s.text_models(222)
    check("user pref promoted to head", chain[0] == "gemini-flash-latest", chain)
    check("user chain deduped", len(chain) == len(set(chain)), chain)
    check("user chain keeps fallback", len(chain) > 1, chain)
    check("other user unaffected", s.text_models(333)[0] == "gemini-flash-lite-latest", s.text_models(333))
    check("get_user_model roundtrip", s.get_user_model(222, "text") == "gemini-flash-latest")
    s.set_user_model(222, "text", None)
    check("clearing pref restores default", s.text_models(222)[0] == "gemini-flash-lite-latest", s.text_models(222))
    check("get_user_model None after clear", s.get_user_model(222, "text") is None)

    # --- user management
    check("add_user new", s.add_user(444) is True)
    check("add_user duplicate", s.add_user(444) is False)
    check("remove_user works", s.remove_user(444) is True)
    try:
        s.remove_user(111)
        check("remove admin raises", False, "no exception")
    except ValueError:
        check("remove admin raises", True)
    check("admin still present", 111 in s.allowed_user_ids())

    # --- persistence + permissions
    s.set_default_model("audio", "gemini-3.5-flash")
    s.set_user_model(333, "audio", "gemini-flash-latest")
    s.add_user(555)
    check("settings.json written", os.path.exists(p))
    if os.path.exists(p):
        mode = stat.S_IMODE(os.stat(p).st_mode)
        check("settings.json is 0600", mode == 0o600, oct(mode))
        raw = open(p).read()
        check("token not persisted", "tg-token-secret" not in raw)
        try:
            json.loads(raw)
            check("settings.json is valid json", True)
        except Exception as e:
            check("settings.json is valid json", False, str(e))

    s2 = fresh(p)
    check("default model survives reload", s2.audio_models()[0] == "gemini-3.5-flash", s2.audio_models())
    check("user pref survives reload", s2.get_user_model(333, "audio") == "gemini-flash-latest")
    check("added user survives reload", 555 in s2.allowed_user_ids(), s2.allowed_user_ids())
    check("admin still first after reload", s2.admin_id() == 111, s2.allowed_user_ids())

    # --- available_models
    av = s2.available_models(force=True)
    check("available strips models/ prefix", all(not m.startswith("models/") for m in av), av)
    check("available filters non-generateContent", "text-embedding-004" not in av, av)
    check("available has real models", "gemini-flash-latest" in av, av)

    # --- api key handling
    before_client = s2.gemini_client()
    try:
        s2.set_api_key(BAD)
        check("bad key rejected", False, "no exception raised")
    except ValueError as e:
        msg = str(e)
        check("bad key rejected", True)
        check("error has no key material", BAD not in msg, msg)
        check("error has no raw api text", "REDACTME" not in msg, msg)
    check("old client kept after bad key", s2.gemini_client() is before_client)
    check("old key still active", s2.gemini_client().api_key == GOOD)

    s2.set_api_key(GOOD2)
    check("client rebuilt on good key", s2.gemini_client().api_key == GOOD2)
    fp = s2.api_key_fingerprint()
    check("fingerprint is redacted", GOOD2 not in fp and fp.endswith("2222"), fp)
    s3 = fresh(p, env={"GEMINI_API_KEY": GOOD})
    check("stored key overrides env on reload", s3.gemini_client().api_key == GOOD2, s3.gemini_client().api_key)

    # --- snapshot leaks nothing
    snap = json.dumps(s3.snapshot(), default=str)
    check("snapshot has no key", GOOD2 not in snap and GOOD not in snap, snap[:200])

    # --- available_models never raises even when the API errors
    s3.gemini_client().api_key = "now-invalid"
    try:
        av2 = s3.available_models(force=True)
        check("available_models survives api failure", isinstance(av2, list) and len(av2) > 0, av2)
    except Exception as e:
        check("available_models survives api failure", False, repr(e))

    # --- corrupt settings file must not crash startup
    bad_path = os.path.join(tmp, "corrupt.json")
    open(bad_path, "w").write("{not json at all")
    try:
        s4 = fresh(bad_path)
        check("corrupt settings.json tolerated", s4.admin_id() == 111, s4.admin_id())
    except Exception as e:
        check("corrupt settings.json tolerated", False, repr(e))

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n===== settings.py verification =====")
    for name, ok, detail in results:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {detail}"))
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
