"""Offline verification of setup.py against CONTRACT.md. Run with no external network calls."""

import json
import os
import stat
import subprocess
import sys
import tempfile

# Derive paths from __file__ so the suite runs from any checkout
TEST_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_DIR = os.path.join(TEST_DIR, "..", "telegram-bot")

FAKE_BOT_TOKEN = "123456:ABCDEFabcdef-ThisIsAFakeToken"
# Deliberately does NOT match the real AIza… shape: a realistic-looking key in a
# public repo trips secret scanners and wastes a reader's time deciding if it is real.
FAKE_GEMINI_KEY = "fake-gemini-key-for-tests-only"
FAKE_GEMINI_BAD_KEY = "InvalidKeyForTesting"

results = []
captured_urls = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))


def fake_get_json_success_bot(url, timeout=10.0):
    """Fake Telegram API that returns bot info."""
    captured_urls.append(url)
    # Token is embedded in URL for Telegram API (this is normal, tested separately)
    if "getMe" in url:
        return {
            "ok": True,
            "result": {
                "id": 123456789,
                "is_bot": True,
                "first_name": "TestBot",
                "username": "test_bot",
            }
        }
    return {"ok": False, "error_code": 404}


def fake_get_json_failure_bot(url, timeout=10.0):
    """Fake Telegram API that fails."""
    captured_urls.append(url)
    return {"ok": False, "error_code": 401, "description": "Unauthorized"}


def test_env_writing():
    """Test .env file writing with proper mode and content."""
    from setup import write_env, parse_env_file

    with tempfile.TemporaryDirectory() as tmp:
        # Simulate write_env - patch os.path.dirname
        original_dirname = os.path.dirname

        def fake_dirname(path):
            if "setup.py" in path:
                return tmp
            return original_dirname(path)

        os.path.dirname = fake_dirname

        try:
            env_path = write_env(FAKE_BOT_TOKEN, FAKE_GEMINI_KEY, [111, 222, 333])
            check("env file created", os.path.exists(env_path))

            if os.path.exists(env_path):
                mode = stat.S_IMODE(os.stat(env_path).st_mode)
                check("env mode is 0600", mode == 0o600, oct(mode))

                # Parse and verify content
                parsed = parse_env_file(env_path)
                check("bot token roundtrip", parsed.get("TELEGRAM_BOT_TOKEN") == FAKE_BOT_TOKEN)
                check("gemini key roundtrip", parsed.get("GEMINI_API_KEY") == FAKE_GEMINI_KEY)
                check("allowed ids roundtrip", parsed.get("ALLOWED_USER_ID") == "111,222,333")
        finally:
            os.path.dirname = original_dirname


def test_env_parsing():
    """Test .env parser with various edge cases."""
    from setup import parse_env_file

    with tempfile.TemporaryDirectory() as tmp:
        env_path = os.path.join(tmp, ".env")

        # Test with comments, blank lines, quoted values, equals in values
        content = """# This is a comment

TELEGRAM_BOT_TOKEN=token_with_equals=inside
GEMINI_API_KEY="quoted_key_value"
ALLOWED_USER_ID=111,222,333
# Another comment
SETTING_WITH_SPACES=value with spaces

# Blank line above this
"""
        open(env_path, "w").write(content)

        parsed = parse_env_file(env_path)
        check("comments ignored", "This is a comment" not in str(parsed))
        check("quoted values handled", parsed.get("GEMINI_API_KEY") == "quoted_key_value")
        check("equals in values", parsed.get("TELEGRAM_BOT_TOKEN") == "token_with_equals=inside")
        check("spaces in values", parsed.get("SETTING_WITH_SPACES") == "value with spaces")
        check("blank lines handled", "SETTING_WITH_SPACES" in parsed)

        # Test CRLF
        crlf_path = os.path.join(tmp, ".env.crlf")
        crlf_content = "KEY1=value1\r\nKEY2=value2\r\n"
        open(crlf_path, "wb").write(crlf_content.encode())
        parsed_crlf = parse_env_file(crlf_path)
        check("CRLF endings handled", parsed_crlf.get("KEY1") == "value1")


def test_id_parsing():
    """Test user ID parsing."""
    from setup import parse_user_ids

    # Single ID
    ids = parse_user_ids("111")
    check("single id", ids == [111], str(ids))

    # Multiple comma-separated
    ids = parse_user_ids("111,222,333")
    check("multiple ids", ids == [111, 222, 333], str(ids))

    # With whitespace
    ids = parse_user_ids("  111  ,  222  ,  333  ")
    check("ids with whitespace", ids == [111, 222, 333], str(ids))

    # Multiple lines
    ids = parse_user_ids("111\n222\n333")
    check("ids one per line", ids == [111, 222, 333], str(ids))

    # Admin is first
    ids = parse_user_ids("555,111,333")
    check("first id is admin", ids[0] == 555, str(ids))

    # Duplicates rejected
    try:
        ids = parse_user_ids("111,222,111")
        check("duplicates rejected", False, "no exception raised")
    except ValueError:
        check("duplicates rejected", True)

    # Non-integer rejected
    try:
        ids = parse_user_ids("111,abc,333")
        check("non-integer rejected", False, "no exception raised")
    except ValueError:
        check("non-integer rejected", True)

    # Empty rejected
    try:
        ids = parse_user_ids("")
        check("empty rejected", False, "no exception raised")
    except ValueError:
        check("empty rejected", True)


def test_validate_bot_token():
    """Test bot token validation and secret handling."""
    from setup import validate_bot_token

    # Success path - token gets embedded in URL but should NOT appear in detail
    captured_urls.clear()
    ok, detail = validate_bot_token(FAKE_BOT_TOKEN, fake_get_json_success_bot)
    check("bot token success path ok=True", ok, detail if not ok else "")
    check("bot token returns @username", detail.startswith("@"), detail)
    check("bot token not in detail string", FAKE_BOT_TOKEN not in detail, "Token leaked in detail!")

    # Failure path - token should NOT appear in detail
    captured_urls.clear()
    ok, detail = validate_bot_token(FAKE_BOT_TOKEN, fake_get_json_failure_bot)
    check("bot token failure ok=False", not ok, detail if ok else "")
    check("bot token failure has detail", len(detail) > 0, detail)
    check("bot token not in failure detail", FAKE_BOT_TOKEN not in detail, "Token leaked in failure detail!")


def test_validate_gemini_key():
    """Test Gemini key validation - key must not leak in detail."""
    from setup import validate_gemini_key
    import urllib.request
    import urllib.error

    # Patch urlopen rather than injecting get_json, so this also proves the real
    # default transport puts the key in a header and never in the URL.
    original_urlopen = urllib.request.urlopen
    header_found = []

    def fake_urlopen_success(request, timeout=None):
        # Verify the request has the header and URL doesn't have the key
        captured_urls.append(request.full_url)
        if FAKE_GEMINI_KEY in request.full_url:
            raise AssertionError(f"Key in URL: {request.full_url}")
        # Check header was added (case-insensitive)
        for key, value in request.headers.items():
            if key.lower() == "x-goog-api-key" and value == FAKE_GEMINI_KEY:
                header_found.append(True)

        # Mock response
        class MockResponse:
            def read(self):
                return b'{"models": [{"name": "models/gemini-flash-latest"}]}'
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
        return MockResponse()

    def fake_urlopen_fail(request, timeout=None):
        if FAKE_GEMINI_BAD_KEY in request.full_url:
            raise AssertionError(f"Key in URL: {request.full_url}")
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    # Test success path
    urllib.request.urlopen = fake_urlopen_success
    captured_urls.clear()
    header_found.clear()
    ok, detail = validate_gemini_key(FAKE_GEMINI_KEY)
    check("gemini key success path ok=True", ok, detail if not ok else "")
    check("gemini key not in success detail", FAKE_GEMINI_KEY not in detail, f"Key leaked: {detail}")
    check("gemini key sent as x-goog-api-key header", len(header_found) > 0, f"Header not found")
    urllib.request.urlopen = original_urlopen

    # Test failure path
    urllib.request.urlopen = fake_urlopen_fail
    ok, detail = validate_gemini_key(FAKE_GEMINI_BAD_KEY)
    check("gemini key failure ok=False", not ok, detail if ok else "")
    check("gemini key not in failure detail", FAKE_GEMINI_BAD_KEY not in detail, f"Key leaked: {detail}")
    urllib.request.urlopen = original_urlopen


def test_doctor_checks_env_exists():
    """Test doctor check for .env existence."""
    from setup import check_env_exists

    with tempfile.TemporaryDirectory() as tmp:
        # Patch to use tmp
        original_abspath = os.path.abspath

        def fake_abspath(path):
            if "setup.py" in path:
                return os.path.join(tmp, "setup.py")
            return original_abspath(path)

        os.path.abspath = fake_abspath

        try:
            # .env doesn't exist
            ok, label, detail = check_env_exists()
            check("env missing check fails", not ok, detail)

            # Create .env
            env_path = os.path.join(tmp, ".env")
            open(env_path, "w").write("KEY=value\n")

            ok, label, detail = check_env_exists()
            check("env present check passes", ok, detail)
        finally:
            os.path.abspath = original_abspath


def test_doctor_checks_env_mode():
    """Test doctor checks for .env mode."""
    from setup import check_env_mode

    with tempfile.TemporaryDirectory() as tmp:
        env_path = os.path.join(tmp, ".env")
        open(env_path, "w").write("KEY=value\n")

        original_abspath = os.path.abspath

        def fake_abspath(path):
            if "setup.py" in path:
                return os.path.join(tmp, "setup.py")
            return original_abspath(path)

        os.path.abspath = fake_abspath

        try:
            os.chmod(env_path, 0o644)
            ok, label, detail = check_env_mode()
            check("env mode 0644 fails", not ok, detail)
            check("env mode detail helpful", "chmod" in detail.lower() or "600" in detail, detail)

            # Fix and verify
            os.chmod(env_path, 0o600)
            ok, label, detail = check_env_mode()
            check("env mode 0600 passes", ok, detail)
        finally:
            os.path.abspath = original_abspath


def test_doctor_checks_env_variables():
    """Test doctor checks for required .env variables."""
    from setup import check_env_vars

    with tempfile.TemporaryDirectory() as tmp:
        env_path = os.path.join(tmp, ".env")

        original_abspath = os.path.abspath

        def fake_abspath(path):
            if "setup.py" in path:
                return os.path.join(tmp, "setup.py")
            return original_abspath(path)

        os.path.abspath = fake_abspath

        try:
            # Missing bot token
            open(env_path, "w").write("GEMINI_API_KEY=key\nALLOWED_USER_ID=111\n")
            ok, label, detail = check_env_vars()
            check("missing bot token fails", not ok, detail)

            # Missing gemini key
            open(env_path, "w").write("TELEGRAM_BOT_TOKEN=token\nALLOWED_USER_ID=111\n")
            ok, label, detail = check_env_vars()
            check("missing gemini key fails", not ok, detail)

            # Missing allowed users
            open(env_path, "w").write("TELEGRAM_BOT_TOKEN=token\nGEMINI_API_KEY=key\n")
            ok, label, detail = check_env_vars()
            check("missing allowed users fails", not ok, detail)

            # Empty value
            open(env_path, "w").write("TELEGRAM_BOT_TOKEN=\nGEMINI_API_KEY=key\nALLOWED_USER_ID=111\n")
            ok, label, detail = check_env_vars()
            check("empty token fails", not ok, detail)

            # All present
            open(env_path, "w").write("TELEGRAM_BOT_TOKEN=token\nGEMINI_API_KEY=key\nALLOWED_USER_ID=111\n")
            ok, label, detail = check_env_vars()
            check("all vars present passes", ok, detail)
        finally:
            os.path.abspath = original_abspath


def test_doctor_checks_allowed_user_ids():
    """Test doctor checks for ALLOWED_USER_ID parsing."""
    from setup import check_allowed_user_ids

    with tempfile.TemporaryDirectory() as tmp:
        env_path = os.path.join(tmp, ".env")

        original_abspath = os.path.abspath

        def fake_abspath(path):
            if "setup.py" in path:
                return os.path.join(tmp, "setup.py")
            return original_abspath(path)

        os.path.abspath = fake_abspath

        try:
            # Bad format - non-integer
            open(env_path, "w").write("TELEGRAM_BOT_TOKEN=t\nGEMINI_API_KEY=k\nALLOWED_USER_ID=111,abc,333\n")
            ok, label, detail = check_allowed_user_ids()
            check("non-integer ids fail", not ok, detail)

            # Duplicates
            open(env_path, "w").write("TELEGRAM_BOT_TOKEN=t\nGEMINI_API_KEY=k\nALLOWED_USER_ID=111,222,111\n")
            ok, label, detail = check_allowed_user_ids()
            check("duplicate ids fail", not ok, detail)

            # Good case
            open(env_path, "w").write("TELEGRAM_BOT_TOKEN=t\nGEMINI_API_KEY=k\nALLOWED_USER_ID=111,222,333\n")
            ok, label, detail = check_allowed_user_ids()
            check("valid ids pass", ok, detail)
            check("admin id reported", "111" in detail, detail)
            check("regular user count in detail", "2" in detail, detail)
        finally:
            os.path.abspath = original_abspath


def test_doctor_checks_settings_json():
    """Test doctor checks for settings.json."""
    from setup import check_settings_json

    with tempfile.TemporaryDirectory() as tmp:
        settings_path = os.path.join(tmp, "settings.json")

        original_abspath = os.path.abspath

        def fake_abspath(path):
            if "setup.py" in path:
                return os.path.join(tmp, "setup.py")
            return original_abspath(path)

        os.path.abspath = fake_abspath

        try:
            # Not present is OK
            ok, label, detail = check_settings_json()
            check("missing settings.json is OK", ok, detail)
            check("not created yet in detail", "not created" in detail.lower(), detail)

            # Invalid JSON
            open(settings_path, "w").write("{not valid json")
            os.chmod(settings_path, 0o600)
            ok, label, detail = check_settings_json()
            check("invalid JSON fails", not ok, detail)

            # Wrong version
            open(settings_path, "w").write(json.dumps({"version": 2, "data": {}}))
            os.chmod(settings_path, 0o600)
            ok, label, detail = check_settings_json()
            check("wrong version fails", not ok, detail)

            # Wrong mode
            open(settings_path, "w").write(json.dumps({"version": 1, "data": {}}))
            os.chmod(settings_path, 0o644)
            ok, label, detail = check_settings_json()
            check("wrong mode fails", not ok, detail)
            check("mode fix hint in detail", "chmod" in detail.lower() or "600" in detail, detail)

            # Perfect
            open(settings_path, "w").write(json.dumps({"version": 1, "data": {}}))
            os.chmod(settings_path, 0o600)
            ok, label, detail = check_settings_json()
            check("valid settings.json passes", ok, detail)
        finally:
            os.path.abspath = original_abspath


def test_doctor_gitignore_tracked_vs_untracked():
    """The tracked-file check must distinguish a real leak from a healthy install.

    Regression guard: an earlier version ignored `git ls-files`' return code, so every
    existing .env was reported as "tracked by git (LEAK RISK!)" — a false alarm that also
    made a genuine leak indistinguishable from normal. Both directions are asserted here.
    """
    import shutil
    import subprocess

    def build_repo(track_env):
        root = tempfile.mkdtemp()
        bot = os.path.join(root, "telegram-bot")
        os.makedirs(bot)
        shutil.copy(os.path.join(BOT_DIR, "setup.py"), bot)
        with open(os.path.join(root, ".gitignore"), "w") as f:
            f.write(".env\nsettings.json\n")
        with open(os.path.join(bot, ".env"), "w") as f:
            f.write("TELEGRAM_BOT_TOKEN=x\nGEMINI_API_KEY=y\nALLOWED_USER_ID=1\n")
        q = {"cwd": root, "capture_output": True}
        subprocess.run(["git", "init", "-q", "."], **q)
        add = ["git", "add"] + (["-f"] if track_env else []) + \
              (["telegram-bot/.env"] if track_env else [".gitignore"])
        subprocess.run(add, **q)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "x"], **q)
        return root, bot

    def run_check(bot_dir):
        # Import the copied setup.py under its own path so check_gitignore resolves the
        # temp repo, not this checkout.
        out = subprocess.run(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0,'.'); import setup; print(setup.check_gitignore())"],
            cwd=bot_dir, capture_output=True, text=True, timeout=20)
        return out.stdout.strip()

    root, bot = build_repo(track_env=False)
    healthy = run_check(bot)
    check("healthy install does not raise a false leak alarm",
          "LEAK RISK" not in healthy and "True" in healthy, healthy)
    shutil.rmtree(root, ignore_errors=True)

    root, bot = build_repo(track_env=True)
    leaked = run_check(bot)
    check("a genuinely tracked .env is reported as a leak",
          "LEAK RISK" in leaked and "False" in leaked, leaked)
    shutil.rmtree(root, ignore_errors=True)


def main():
    sys.path.insert(0, BOT_DIR)

    try:
        import setup
    except ImportError as e:
        print("ERROR: setup.py not found in telegram-bot/")
        print(str(e))
        return 1

    # Run all test functions
    test_env_writing()
    test_env_parsing()
    test_id_parsing()
    test_validate_bot_token()
    test_validate_gemini_key()
    test_doctor_checks_env_exists()
    test_doctor_checks_env_mode()
    test_doctor_checks_env_variables()
    test_doctor_checks_allowed_user_ids()
    test_doctor_checks_settings_json()
    test_doctor_gitignore_tracked_vs_untracked()

    # Print results
    passed = sum(1 for _, ok, _ in results if ok)
    print("\n===== setup.py verification =====")
    for name, ok, detail in results:
        status = "PASS  " if ok else "FAIL  "
        detail_str = "" if ok else f"   -> {detail}"
        print(status + name + detail_str)
    print(f"\n{passed}/{len(results)} passed")

    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
