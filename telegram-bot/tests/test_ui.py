"""Offline verification of settings_ui.py: menu crawl + authorization + /setkey hygiene."""

import asyncio
import importlib
import os
import sys
import tempfile

# Derive paths from __file__ so the suite runs from any checkout
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_DIR = os.path.dirname(TEST_DIR)
STUBS = os.path.join(TEST_DIR, "stubs")

ADMIN, USER, OUTSIDER = 111, 222, 999
GOOD2 = "good-key-bbbb2222"
BAD = "bad-key-zzzz9999"

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def setup():
    for m in ("settings", "settings_ui"):
        sys.modules.pop(m, None)
    tmp = tempfile.mkdtemp()
    os.environ["SEDAI_SETTINGS_PATH"] = os.path.join(tmp, "settings.json")
    os.environ["TELEGRAM_BOT_TOKEN"] = "123:tg-token-secret"
    os.environ["GEMINI_API_KEY"] = "good-key-aaaa1111"
    os.environ["ALLOWED_USER_ID"] = f"{ADMIN},{USER}"
    import settings
    import settings_ui
    importlib.reload(settings)
    importlib.reload(settings_ui)
    settings.load()
    from telegram.ext import Application
    app = Application.builder().token("x").build()
    settings_ui.register(app)
    return settings, settings_ui, app


def keyboards_from(actions):
    out = []
    for a in actions:
        for item in a:
            if hasattr(item, "inline_keyboard"):
                out.append(item)
    return out


async def press(app, data, user_id):
    import telegram
    telegram.ACTIONS.clear()
    from telegram import CallbackQuery, Update
    from telegram.ext import _Ctx
    cb = app.callback_for(data)
    if cb is None:
        return [], []
    q = CallbackQuery(data=data, user_id=user_id)
    await cb(Update(callback_query=q), _Ctx())
    return list(telegram.ACTIONS), keyboards_from(telegram.ACTIONS)


async def open_root(app, user_id):
    import telegram
    telegram.ACTIONS.clear()
    from telegram import Message, Update
    from telegram.ext import _Ctx
    cb = app.command("settings")
    msg = Message(message_id=1, chat_id=user_id, text="/settings", user_id=user_id)
    await cb(Update(message=msg, user_id=user_id), _Ctx())
    return list(telegram.ACTIONS), keyboards_from(telegram.ACTIONS)


async def crawl(app, user_id, max_nodes=60):
    """Breadth-first walk of every callback reachable by this user. Returns {data: label}."""
    _, kbs = await open_root(app, user_id)
    seen = {}
    queue = []
    for kb in kbs:
        for b in kb.buttons():
            if b.callback_data:
                queue.append((b.callback_data, b.text))
    while queue and len(seen) < max_nodes:
        data, label = queue.pop(0)
        if data in seen:
            continue
        seen[data] = label
        _, kbs = await press(app, data, user_id)
        for kb in kbs:
            for b in kb.buttons():
                if b.callback_data and b.callback_data not in seen:
                    queue.append((b.callback_data, b.text))
    return seen


async def setkey(app, key, user_id):
    import telegram
    telegram.ACTIONS.clear()
    from telegram import Message, Update
    from telegram.ext import _Ctx
    cb = app.command("setkey")
    msg = Message(message_id=42, chat_id=user_id, text=f"/setkey {key}", user_id=user_id)
    ctx = _Ctx(args=[key] if key else [])
    await cb(Update(message=msg, user_id=user_id), ctx)
    return list(telegram.ACTIONS)


def texts(actions):
    return " ".join(str(i) for a in actions for i in a if isinstance(i, str))


async def main():
    sys.path.insert(0, STUBS)
    sys.path.insert(0, BOT_DIR)
    settings, settings_ui, app = setup()

    # --- registration
    check("registers /settings", app.command("settings") is not None)
    check("registers /setkey", app.command("setkey") is not None)
    pats = app.callback_patterns()
    check("registers a callback handler", len(pats) > 0, pats)
    check("all patterns namespaced to set:", all("set:" in (p or "") for p in pats), pats)
    for legacy in ("summarize:5", "reply:12"):
        check(f"no collision with {legacy}", app.callback_for(legacy) is None)

    # --- menu visibility. Crawl the regular user FIRST: the admin crawl presses every
    # admin callback, which includes "remove user", and would delete them mid-test.
    user_nodes = await crawl(app, USER)
    admin_nodes = await crawl(app, ADMIN)
    check("admin crawl actually removed the regular user (remove works)",
          USER not in settings.allowed_user_ids(), settings.allowed_user_ids())
    settings.add_user(USER)  # restore for the remaining checks
    check("removed user can be re-added", USER in settings.allowed_user_ids())
    check("admin menu is richer than user menu", len(admin_nodes) > len(user_nodes),
          f"admin={len(admin_nodes)} user={len(user_nodes)}")
    check("regular user gets a menu at all", len(user_nodes) > 0)

    all_cb = list(admin_nodes) + list(user_nodes)
    check("all callback_data under 64 bytes",
          all(len(d.encode()) <= 64 for d in all_cb),
          [d for d in all_cb if len(d.encode()) > 64])
    check("all callback_data in set: namespace",
          all(d.startswith("set:") for d in all_cb),
          [d for d in all_cb if not d.startswith("set:")])
    check("no model name embedded in callback_data",
          not any("gemini" in d for d in all_cb),
          [d for d in all_cb if "gemini" in d])

    # --- outsider gets nothing
    out_actions, out_kbs = await open_root(app, OUTSIDER)
    check("outsider gets no menu", len(out_kbs) == 0, texts(out_actions)[:120])

    # --- privilege escalation: admin-only callbacks pressed by a regular user
    admin_only = [d for d in admin_nodes if d not in user_nodes]
    check("found admin-only callbacks to probe", len(admin_only) > 0, len(admin_only))
    before = (settings.default_models("audio"), settings.default_models("text"),
              settings.allowed_user_ids(), settings.api_key_fingerprint())
    for d in admin_only:
        await press(app, d, USER)
    after = (settings.default_models("audio"), settings.default_models("text"),
             settings.allowed_user_ids(), settings.api_key_fingerprint())
    check("regular user cannot mutate global state via admin callbacks", before == after,
          f"{before} -> {after}")

    for d in admin_only:
        await press(app, d, OUTSIDER)
    after2 = (settings.default_models("audio"), settings.default_models("text"),
              settings.allowed_user_ids(), settings.api_key_fingerprint())
    check("outsider cannot mutate global state via admin callbacks", before == after2,
          f"{before} -> {after2}")

    # --- admin cannot be removed, list cannot be emptied
    for d in admin_nodes:
        await press(app, d, ADMIN)
    check("admin survives pressing every admin callback", ADMIN in settings.allowed_user_ids(),
          settings.allowed_user_ids())
    check("allowed list never emptied", len(settings.allowed_user_ids()) >= 1)

    # --- status screen must render REAL values, not silently-missing snapshot keys
    st_actions, _ = await press(app, "set:status", ADMIN)
    st = texts(st_actions)
    if st:
        snap = settings.snapshot()
        check("status shows the real default text chain",
              settings.default_models("text")[0] in st, st[:200])
        check("status shows the key fingerprint",
              snap["api_key_fingerprint"] in st, st[:200])
        check("status shows the settings path", snap["settings_path"] in st, st[:200])
        check("status has no unresolved placeholders", "unknown" not in st, st[:200])
        check("status shows a nonzero user count",
              str(snap["allowed_user_count"]) in st, st[:200])
        check("status leaks no key material", "good-key" not in st, st[:200])

    # --- stale/garbage callback data must not crash
    for junk in ("set:", "set:zzz", "set:model:audio:99999", "set:page:-1"):
        try:
            await press(app, junk, ADMIN)
            check(f"garbage callback {junk!r} handled", True)
        except Exception as e:
            check(f"garbage callback {junk!r} handled", False, repr(e))

    # --- /setkey
    fp_before = settings.api_key_fingerprint()
    acts = await setkey(app, GOOD2, USER)
    check("non-admin /setkey rejected", settings.api_key_fingerprint() == fp_before,
          settings.api_key_fingerprint())
    check("non-admin /setkey leaks no key", GOOD2 not in texts(acts))

    acts = await setkey(app, BAD, ADMIN)
    kinds = [a[0] for a in acts]
    check("bad key: message deleted", "delete_message" in kinds or "delete" in kinds, kinds)
    check("bad key: previous key still active", settings.api_key_fingerprint() == fp_before)
    check("bad key: key not echoed", BAD not in texts(acts), texts(acts)[:200])
    check("bad key: raw api error not echoed", "REDACTME" not in texts(acts), texts(acts)[:200])

    hook_fired = []
    settings_ui.set_key_change_hook(lambda: hook_fired.append(True))
    acts = await setkey(app, GOOD2, ADMIN)
    kinds = [a[0] for a in acts]
    check("good key: message deleted", "delete_message" in kinds or "delete" in kinds, kinds)
    check("good key: delete happens before the confirmation is sent",
          kinds.index("delete_message" if "delete_message" in kinds else "delete")
          < max([i for i, k in enumerate(kinds) if k in ("reply_text", "send_message")] or [99]),
          kinds)
    check("good key: applied", settings.api_key_fingerprint() == "…2222",
          settings.api_key_fingerprint())
    check("good key: full key never echoed", GOOD2 not in texts(acts), texts(acts)[:200])
    check("good key: fingerprint shown", "2222" in texts(acts), texts(acts)[:200])
    check("good key: chat-clear hook fired", hook_fired == [True], hook_fired)

    acts = await setkey(app, "", ADMIN)
    check("empty /setkey handled gracefully", len(acts) > 0)

    passed = sum(1 for _, ok, _ in results if ok)
    print("\n===== settings_ui.py verification =====")
    for name, ok, detail in results:
        print(("PASS  " if ok else "FAIL  ") + name + ("" if ok else f"   -> {detail}"))
    print(f"\n{passed}/{len(results)} passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
