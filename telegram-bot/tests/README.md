# Offline Test Suites

These test suites verify the Sedai bot's modules against specifications without requiring network access, real credentials, or external dependencies beyond Python.

## Running the Tests

Run all suites at once:
```sh
python3 run_all.py
```

Or run a single suite:
```sh
python3 test_settings.py
python3 test_ui.py
python3 test_integration.py
python3 test_help.py
python3 test_styles.py
python3 test_input_flow.py
python3 test_setup.py
```

## How They Work

Each suite uses the `stubs/` directory, which provides mock implementations of Telegram and Google Gemini APIs. No real bot token or API key is required — the tests use hardcoded test values.

- **test_settings.py** (40 assertions): Verifies settings persistence, per-user model preferences, API key handling, and the admin/user authorization model.
- **test_ui.py** (42 assertions): Tests the settings menu UI, callback routing, privilege escalation barriers, and the /setkey command's safety guards.
- **test_integration.py** (20 assertions): End-to-end verification that sedai_bot.py wires everything correctly, live settings apply without restart, and cross-chat privacy is maintained.
- **test_help.py** (33 assertions): Confirms role-aware help, command registration order, the unknown-command fallback, and scoped command menus.
- **test_styles.py** (77 assertions): Comprehensive check of standing instructions (reply, chat, summary styles), instruction isolation, and the per-user chat system prompts.
- **test_input_flow.py** (30 assertions): Reply-based input collection — that a reply to a pending prompt is consumed exactly once, and that any other reply falls through to normal chat untouched, including cross-user and cross-chat isolation.
- **test_setup.py** (54 assertions): `setup.py` and its `--check` doctor, with the network injected: `.env` writing and permissions, ID parsing, and that neither credential validator leaks its secret.

**Total: 212 assertions**

## Requirements

- Python 3.7+
- No external packages (the stubs simulate python-telegram-bot and google-genai)
