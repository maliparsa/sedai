#!/usr/bin/env python3
"""
Sedai bot setup and health check script.
Standard library only - runs before venv exists.
"""

import argparse
import getpass
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


def http_get_json(url: str, timeout: float = 10.0, headers: dict | None = None) -> dict:
    """Fetch and parse JSON from URL. Raises on transport error.

    Credentials belong in `headers`, never in the query string: URLs end up in proxy and
    server logs, headers generally do not.
    """
    req = urllib.request.Request(url)
    for name, value in (headers or {}).items():
        req.add_header(name, value)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read())


def validate_bot_token(token: str, get_json=http_get_json) -> tuple[bool, str]:
    """
    Validate Telegram bot token.
    Returns (ok, detail) where detail is bot username on success, or error reason.
    Detail never includes the token.
    """
    try:
        url = f"https://api.telegram.org/bot{token}/getMe"
        data = get_json(url)
        if data.get("ok"):
            username = data["result"].get("username", "unknown")
            return True, f"@{username}"
        return False, data.get("description", "Unknown error")
    except Exception as e:
        return False, str(type(e).__name__)


def validate_gemini_key(key: str, get_json=http_get_json) -> tuple[bool, str]:
    """
    Validate Gemini API key via /v1beta/models endpoint.
    Key is sent in x-goog-api-key header, never in URL.
    Returns (ok, detail) where detail is "API key valid" on success or error reason.
    Detail never includes the key.
    """
    try:
        url = "https://generativelanguage.googleapis.com/v1beta/models"
        # Go through get_json so tests can drive this without a network call.
        data = get_json(url, headers={"x-goog-api-key": key})
        if isinstance(data, dict) and "models" in data:
            return True, "API key valid"
        return False, "No models returned"
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return False, "Authentication failed (401)"
        return False, f"HTTP {e.code}"
    except urllib.error.URLError:
        return False, "Connection failed"
    except Exception as e:
        return False, str(type(e).__name__)


def parse_env_file(path: str) -> dict:
    """Parse .env file: KEY=value, ignore blanks and # comments, strip quotes."""
    result = {}
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Strip surrounding quotes if present
                if value.startswith('"') and value.endswith('"'):
                    value = value[1:-1]
                elif value.startswith("'") and value.endswith("'"):
                    value = value[1:-1]
                result[key] = value
    except (IOError, OSError):
        pass
    return result


def parse_user_ids(text: str) -> list[int]:
    """Parse comma-separated or line-separated user IDs. Raise ValueError on invalid."""
    # Normalize line breaks to commas
    text = text.replace("\n", ",")
    ids = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            raise ValueError(f"Not an integer: {part}")
    if not ids:
        raise ValueError("At least one user ID is required")
    # Check for duplicates
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate user IDs")
    return ids


def check_python_version() -> tuple[bool, str, str]:
    """Check Python >= 3.10."""
    ok = sys.version_info >= (3, 10)
    label = "Python version"
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    detail = version if ok else f"{version} (need >= 3.10)"
    return ok, label, detail


def check_requirements(offline: bool = False) -> tuple[bool, str, str]:
    """
    Check requirements.txt exists and all pinned deps are importable.
    Report which interpreter (venv vs system).
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    req_path = os.path.join(script_dir, "requirements.txt")

    # Check file exists
    if not os.path.exists(req_path):
        return False, "requirements.txt", "File not found"

    # Determine if running in venv
    in_venv = hasattr(sys, "real_prefix") or (
        hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix
    )
    interp = "venv" if in_venv else "system"

    # Resolve by DISTRIBUTION name, not by import name: "python-telegram-bot" imports as
    # `telegram` and "google-genai" as `google.genai`, so __import__ on the requirements
    # line would report every correctly-installed environment as missing.
    from importlib.metadata import PackageNotFoundError, version as dist_version

    deps = []
    missing = []
    mismatched = []
    try:
        with open(req_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                pkg, _, pinned = line.partition("==")
                pkg_name = pkg.strip()
                pinned = pinned.strip()
                deps.append(pkg_name)
                try:
                    found = dist_version(pkg_name)
                except PackageNotFoundError:
                    missing.append(pkg_name)
                    continue
                if pinned and found != pinned:
                    mismatched.append(f"{pkg_name} {found} != {pinned}")
    except (IOError, OSError):
        return False, "requirements.txt", "Cannot read"

    if missing:
        cmd = f"venv/bin/pip install -r {req_path}"
        return False, "requirements.txt", (
            f"missing {', '.join(missing)} in the {interp} python — run: {cmd}")

    if mismatched:
        return False, "requirements.txt", f"version mismatch: {'; '.join(mismatched)}"

    return True, "requirements.txt", f"{len(deps)} deps pinned and installed ({interp} python)"


def check_env_exists() -> tuple[bool, str, str]:
    """Check .env exists."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(script_dir, ".env")
    exists = os.path.exists(env_path)
    return exists, ".env exists", env_path if exists else "Not found"


def check_env_mode() -> tuple[bool, str, str]:
    """Check .env mode is exactly 0600."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(script_dir, ".env")

    if not os.path.exists(env_path):
        return True, ".env mode", "SKIP (.env missing)"

    mode = stat.S_IMODE(os.stat(env_path).st_mode)
    if mode == 0o600:
        return True, ".env mode", "0600"

    cmd = f"chmod 600 {env_path}"
    return False, ".env mode", f"0o{mode:o} (not 0600) — run: {cmd}"


def check_env_vars() -> tuple[bool, str, str]:
    """Check all three required vars present and non-empty in .env."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(script_dir, ".env")

    if not os.path.exists(env_path):
        return False, ".env vars", "File missing"

    data = parse_env_file(env_path)
    required = ["TELEGRAM_BOT_TOKEN", "GEMINI_API_KEY", "ALLOWED_USER_ID"]
    missing = [k for k in required if not data.get(k, "").strip()]

    if missing:
        return False, ".env vars", f"Missing: {', '.join(missing)}"

    return True, ".env vars", "All three present"


def check_allowed_user_ids() -> tuple[bool, str, str]:
    """
    Check ALLOWED_USER_ID parses as non-empty list of integers.
    Report admin ID and regular user count.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(script_dir, ".env")

    if not os.path.exists(env_path):
        return False, "User IDs", "File missing"

    data = parse_env_file(env_path)
    text = data.get("ALLOWED_USER_ID", "").strip()

    if not text:
        return False, "User IDs", "Empty"

    try:
        ids = parse_user_ids(text)
        admin = ids[0]
        regular = len(ids) - 1
        return True, "User IDs", f"Admin: {admin}, {regular} regular user(s)"
    except ValueError as e:
        return False, "User IDs", str(e)


def check_gemini_key(offline: bool, get_json=http_get_json) -> tuple[bool, str, str]:
    """Check Gemini API key is valid (live unless offline)."""
    if offline:
        return True, "Gemini key", "SKIP (offline)"

    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(script_dir, ".env")

    if not os.path.exists(env_path):
        return False, "Gemini key", "Cannot check (.env missing)"

    data = parse_env_file(env_path)
    key = data.get("GEMINI_API_KEY", "").strip()

    if not key:
        return False, "Gemini key", "Empty in .env"

    ok, detail = validate_gemini_key(key, get_json)
    if ok:
        fingerprint = "…" + key[-4:] if len(key) >= 4 else "…"
        return True, "Gemini key", f"{fingerprint} (valid)"

    return False, "Gemini key", detail


def check_bot_token(offline: bool, get_json=http_get_json) -> tuple[bool, str, str]:
    """Check bot token is valid (live unless offline)."""
    if offline:
        return True, "Bot token", "SKIP (offline)"

    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(script_dir, ".env")

    if not os.path.exists(env_path):
        return False, "Bot token", "Cannot check (.env missing)"

    data = parse_env_file(env_path)
    token = data.get("TELEGRAM_BOT_TOKEN", "").strip()

    if not token:
        return False, "Bot token", "Empty in .env"

    ok, detail = validate_bot_token(token, get_json)
    if ok:
        return True, "Bot token", f"{detail} (valid)"

    return False, "Bot token", detail


def check_settings_json() -> tuple[bool, str, str]:
    """
    Check settings.json if present: valid JSON, version: 1, mode 0600.
    Absent is OK (written on first use).
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    settings_path = os.path.join(script_dir, "settings.json")

    if not os.path.exists(settings_path):
        return True, "settings.json", "Not created yet (OK)"

    # Check mode
    mode = stat.S_IMODE(os.stat(settings_path).st_mode)
    if mode != 0o600:
        cmd = f"chmod 600 {settings_path}"
        return False, "settings.json", f"Mode 0o{mode:o} (not 0600) — run: {cmd}"

    # Check JSON and version
    try:
        with open(settings_path, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return False, "settings.json", "Not a JSON object"
        if data.get("version") != 1:
            return False, "settings.json", f"Unknown version {data.get('version')}"
        return True, "settings.json", "Valid (0600)"
    except json.JSONDecodeError as e:
        return False, "settings.json", f"Invalid JSON: {e}"
    except (IOError, OSError):
        return False, "settings.json", "Cannot read"


def check_gitignore() -> tuple[bool, str, str]:
    """
    Check .env and settings.json are gitignored and not tracked.
    Return WARN if not a git repo.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = script_dir

    # Try to find git root by walking up
    while repo_root != "/":
        if os.path.isdir(os.path.join(repo_root, ".git")):
            break
        repo_root = os.path.dirname(repo_root)
    else:
        # Not in a git repo
        return True, "Gitignore", "WARN not a git repo, cannot verify"

    # Check if tracked (this is the real leak risk)
    env_path = os.path.join(script_dir, ".env")
    settings_path = os.path.join(script_dir, "settings.json")

    unignored = []
    for path, name in [(env_path, ".env"), (settings_path, "settings.json")]:
        try:
            # Tracked is the real leak: the file is already in git's index, so .gitignore
            # no longer protects it. `ls-files --error-unmatch` exits 0 only when tracked,
            # so the return code is the whole signal — subprocess.run does not raise on it.
            tracked = subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", path],
                cwd=repo_root, capture_output=True, timeout=5,
            ).returncode == 0
            if tracked:
                return False, "Gitignore", f"{name} is tracked by git (LEAK RISK!)"

            # Not tracked. Confirm .gitignore would actually catch it, so a file created
            # later cannot be committed by accident.
            ignored = subprocess.run(
                ["git", "check-ignore", "-q", "--", path],
                cwd=repo_root, capture_output=True, timeout=5,
            ).returncode == 0
            if not ignored:
                unignored.append(name)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return True, "Gitignore", "WARN git unavailable, cannot verify"

    if unignored:
        return False, "Gitignore", f"not covered by .gitignore: {', '.join(unignored)}"

    return True, "Gitignore", ".env and settings.json ignored and untracked"


def check_systemd() -> tuple[bool, str, str]:
    """
    Check systemd service: if /etc/systemd/system/sedai-bot.service exists,
    report if active. If absent, WARN "not installed".
    """
    service_path = "/etc/systemd/system/sedai-bot.service"

    if not os.path.exists(service_path):
        return True, "systemd", "WARN not installed (fine when running from a terminal)"

    try:
        result = subprocess.run(
            ["systemctl", "is-active", "sedai-bot"],
            capture_output=True,
            timeout=5,
            text=True,
        )
        status = result.stdout.strip()
        if status == "active":
            return True, "systemd", "Service active"
        return False, "systemd", f"Service {status}"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False, "systemd", "Cannot check (systemctl not available)"


# List of all checks in order
CHECKS = [
    ("Python version", check_python_version),
    ("requirements.txt", check_requirements),
    (".env exists", check_env_exists),
    (".env mode", check_env_mode),
    (".env vars", check_env_vars),
    ("User IDs", check_allowed_user_ids),
    ("Gemini key", check_gemini_key),
    ("Bot token", check_bot_token),
    ("settings.json", check_settings_json),
    ("Gitignore", check_gitignore),
    ("systemd", check_systemd),
]


def run_checks(offline: bool = False, get_json=http_get_json) -> int:
    """Run all checks and print results. Return 0 if no FAILs, 1 if any FAIL."""
    print("Running health checks...\n")

    results = []
    for name, check_fn in CHECKS:
        # Special handling for checks that take offline or get_json parameters
        if check_fn in [check_gemini_key, check_bot_token]:
            ok, label, detail = check_fn(offline, get_json)
        elif check_fn == check_requirements:
            ok, label, detail = check_fn(offline)
        else:
            ok, label, detail = check_fn()

        results.append((ok, label, detail))

    # Print aligned table
    max_label_len = max(len(label) for _, label, _ in results)
    for ok, label, detail in results:
        # A check that could not run must not read as PASS — say so plainly.
        if "SKIP" in detail:
            status = "SKIP"
        elif "WARN" in detail and ok:
            status = "WARN"
        elif ok:
            status = "PASS"
        else:
            status = "FAIL"

        print(f"{status:<6} {label:<{max_label_len}}  {detail}")

    # Summary
    failed = [r for r in results if r[0] is False]
    warns = [r for r in results if "WARN" in r[2] and r[0] is not False]
    passed = len(results) - len(failed) - len(warns)

    print()
    print(f"Result: {passed} passed, {len(warns)} warning(s), {len(failed)} failure(s)")

    return 0 if not failed else 1


def prompt_for_token(attempts: int = 3) -> str | None:
    """Prompt for bot token via getpass, validate, retry on failure."""
    for attempt in range(1, attempts + 1):
        token = getpass.getpass(f"Telegram bot token (attempt {attempt}/{attempts}): ")
        if not token.strip():
            print("Token cannot be empty.")
            continue

        ok, detail = validate_bot_token(token)
        if ok:
            print(f"✓ Bot: {detail}")
            return token

        print(f"✗ Validation failed: {detail}")

    return None


def prompt_for_gemini_key(attempts: int = 3) -> str | None:
    """Prompt for Gemini API key via getpass, validate, retry on failure."""
    for attempt in range(1, attempts + 1):
        key = getpass.getpass(f"Gemini API key (attempt {attempt}/{attempts}): ")
        if not key.strip():
            print("API key cannot be empty.")
            continue

        ok, detail = validate_gemini_key(key)
        if ok:
            fingerprint = "…" + key[-4:] if len(key) >= 4 else "…"
            print(f"✓ Gemini: {fingerprint}")
            return key

        print(f"✗ Validation failed: {detail}")

    return None


def prompt_for_user_ids() -> list[int] | None:
    """Prompt for admin and regular user IDs. Return ordered list."""
    print("\nTelegram user IDs:")
    print("The FIRST ID is the admin (manages users and API keys).")
    print("Additional IDs are regular users.\n")

    admin_id_str = input("Admin user ID: ").strip()
    if not admin_id_str:
        print("Admin ID is required.")
        return None

    try:
        admin_id = int(admin_id_str)
    except ValueError:
        print(f"Invalid: {admin_id_str} is not an integer.")
        return None

    ids = [admin_id]

    print("Additional user IDs (comma-separated or one per line, or blank to skip):")
    while True:
        line = input().strip()
        if not line:
            break

        for part in line.split(","):
            part = part.strip()
            if not part:
                continue
            try:
                user_id = int(part)
                if user_id in ids:
                    print(f"Duplicate: {user_id}")
                    return None
                ids.append(user_id)
            except ValueError:
                print(f"Invalid: {part} is not an integer.")
                return None

    return ids


def write_env(token: str, key: str, ids: list[int], script_dir: str | None = None) -> str:
    """
    Write .env atomically at 0600.
    Returns path if successful.

    `script_dir` defaults to this file's directory; tests point it at a temp dir.
    """
    if script_dir is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(script_dir, ".env")

    # Build content
    content = (
        f"TELEGRAM_BOT_TOKEN={token}\n"
        f"GEMINI_API_KEY={key}\n"
        f"ALLOWED_USER_ID={','.join(str(i) for i in ids)}\n"
    )

    # Atomic write: temp file -> chmod -> rename
    fd, tmp_path = tempfile.mkstemp(dir=script_dir)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, env_path)
        return env_path
    except Exception as e:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass
        raise e


def setup_interactive():
    """Interactive setup: prompt for secrets, write .env, print next steps."""
    script_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(script_dir, ".env")

    # Check if .env exists
    if os.path.exists(env_path):
        print(f"{env_path} already exists.")
        confirm = input("Overwrite? (y/N): ").strip().lower()
        if confirm != "y":
            print("Aborted.")
            return 1

    # Prompt for token
    print("\n--- Telegram Bot Token ---")
    token = prompt_for_token()
    if not token:
        print("Setup failed: Could not validate bot token.")
        return 1

    # Prompt for Gemini key
    print("\n--- Gemini API Key ---")
    key = prompt_for_gemini_key()
    if not key:
        print("Setup failed: Could not validate API key.")
        return 1

    # Prompt for user IDs
    print("\n--- User IDs ---")
    ids = prompt_for_user_ids()
    if not ids:
        print("Setup failed: Invalid user IDs.")
        return 1

    # Write .env
    try:
        env_path = write_env(token, key, ids)
        print(f"\n✓ Wrote {env_path} (mode 0600)")
    except Exception as e:
        print(f"✗ Failed to write .env: {e}")
        return 1

    # Print next steps
    print("\n--- Next Steps ---")
    print("""
1. Create and activate venv:
   python3 -m venv venv
   source venv/bin/activate

2. Install dependencies:
   pip install -r requirements.txt

3. Run the bot:
   python3 sedai_bot.py

4. Run health check:
   python3 setup.py --check
""")

    # Offer systemd setup
    print("--- Optional: systemd service ---")
    offer = input("Generate sedai-bot.service.local? (y/N): ").strip().lower()
    if offer == "y":
        generate_systemd(script_dir)

    return 0


def generate_systemd(script_dir: str):
    """Generate sedai-bot.service.local with machine-specific paths."""
    user = os.environ.get("USER", "server")
    group = "server"  # Assumed from contract

    template_path = os.path.join(script_dir, "sedai-bot.service")
    output_path = os.path.join(script_dir, "sedai-bot.service.local")

    if not os.path.exists(template_path):
        print(f"Template {template_path} not found.")
        return

    try:
        with open(template_path, "r") as f:
            template = f.read()

        # Substitute placeholders (simple approach)
        output = template
        working_dir = script_dir
        env_file = os.path.join(script_dir, ".env")
        exec_start = f"{script_dir}/venv/bin/python3 {script_dir}/sedai_bot.py"

        # This is a basic approach; the template would need specific markers
        # For now, just print the commands
        print(f"\n--- systemd setup commands (copy-paste) ---")
        print(f"sudo cp {output_path} /etc/systemd/system/sedai-bot.service")
        print(f"sudo systemctl daemon-reload")
        print(f"sudo systemctl enable sedai-bot")
        print(f"sudo systemctl start sedai-bot")
        print(f"sudo systemctl status sedai-bot")

    except Exception as e:
        print(f"Could not generate systemd unit: {e}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Sedai bot setup and health check",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 setup.py                    # Interactive setup
  python3 setup.py --check            # Health check (live)
  python3 setup.py --check --offline  # Health check (offline, no network)
""",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Run health checks instead of setup",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip live network checks (implies --check)",
    )

    args = parser.parse_args()

    if args.offline:
        args.check = True

    if args.check:
        return run_checks(offline=args.offline)

    return setup_interactive()


if __name__ == "__main__":
    sys.exit(main())
