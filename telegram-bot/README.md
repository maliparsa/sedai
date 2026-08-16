# Sedai Telegram Bot

A private Telegram bot backed by Gemini: transcribes voice notes in whatever language
they're spoken in, and doubles as a general AI assistant — summarizing, drafting
replies, and plain chat.

## Features

- **Transcription**: send a voice note or audio file, get a transcript back, in whatever
  language/script it was spoken in.
- **Summarize / Draft reply**: every transcript comes with inline buttons to summarize it
  or draft a reply to it.
- **Reply with instructions**: reply to a transcript with a voice note of your own, and
  the bot treats it as spoken instructions for drafting a reply to the original message.
- **Plain chat**: send the bot text and it responds via Gemini, keeping a running
  conversation per chat. `/reset` clears it.
- **Model fallback**: each task tries a chain of Gemini models, falling back on
  rate limits or server errors instead of failing outright.
- **Per-user model settings**: `/settings` lets any user choose their preferred audio
  and text models. Admin can set default chains, manage the user list, update the API
  key, and view status — all applied live without restart.
- **Standing instructions**: each user can set per-user instructions (up to 500 chars)
  that shape how the bot writes for them — one for draft replies, one for plain chat,
  one for summaries. Call a style command with no argument to see the current instruction
  in full and send a reply to update it; call with text to set it inline. Reply with
  exactly `clear` to remove an instruction. They apply live without restart, and can be
  overridden per-message (voice-dictated reply instructions rank above the standing reply
  instruction).
- **Reply-based input**: menu buttons and no-argument commands that need user input now
  send a prompt instead of telling you to retype a command. Simply reply to the prompt
  with your input — there is no timeout and no risk of accidentally supplying text meant
  for chat instead.
- **Command discovery**: `/help` and `/start` list all available commands in role-aware
  format. The Telegram command menu (/) is populated at startup and shows only commands
  relevant to your role.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and get its token.
2. Get a Gemini API key from [Google AI Studio](https://aistudio.google.com/).
3. Find your Telegram numeric user ID (e.g. via [@userinfobot](https://t.me/userinfobot)).
4. Copy `.env.example` to `.env` and fill in `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, and
   `ALLOWED_USER_ID` (comma-separated, with the **first** ID as the admin who can manage
   global settings; additional IDs are regular users).
5. Install dependencies:
   ```sh
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt
   ```
6. Run it directly for testing:
   ```sh
   venv/bin/python sedai_bot.py
   ```

## Commands

| Command | Who | What it does |
|---|---|---|
| `/help` | all allowed users | lists available commands and how to use the bot |
| `/start` | all allowed users | same as `/help`, shown on first contact |
| `/settings` | all allowed users | configure your audio and text model preferences, or view/clear your standing instructions |
| `/replystyle [text]` | all allowed users | view your current standing instruction for draft replies, or set a new one (optionally inline); reply with `clear` to remove |
| `/chatstyle [text]` | all allowed users | view your current standing instruction for plain-text chat, or set a new one (optionally inline); reply with `clear` to remove |
| `/summarystyle [text]` | all allowed users | view your current standing instruction for summaries, or set a new one (optionally inline); reply with `clear` to remove |
| `/reset` | all allowed users | clear your chat history |
| `/setkey <key>` | admin only | update the Gemini API key |
| `/adduser <id>` | admin only | allow another Telegram user ID |

Regular users do not see admin commands in their Telegram "/" command menu.

## Running as a systemd service

`sedai-bot.service` is a template unit file. Adjust `WorkingDirectory`,
`EnvironmentFile`, `ExecStart`, and `User`/`Group` to match your deployment path and
user, then:

```sh
sudo cp sedai-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sedai-bot
```

## Testing

- **Offline test suites** (`tests/`): run `python3 tests/run_all.py` to execute the
  offline tests against mock Telegram and Gemini stubs. These run anywhere with no
  network or real credentials required.
- **Smoke tests** (`smoke_test.py`): run `python3 smoke_test.py` (inside the venv on
  the server) to test the real `python-telegram-bot` library. This covers command
  parsing for non-ASCII scripts (e.g., Farsi) and Telegram message filters that the
  offline stubs cannot fully emulate. Requires no network or real credentials.

## Notes

- Only the Telegram user IDs listed in `ALLOWED_USER_ID` can use the bot; everyone else
  is silently ignored. The first ID in the list is the admin.
- `.env` is gitignored — never commit real tokens/keys.
- `settings.json` (created alongside `.env` on first run) persists per-user model
  preferences and global settings at chmod 600. It overrides `.env` values where
  present, allowing live configuration updates without restarting.
- `/settings`: every allowed user can configure their own audio and text model choices.
  The admin additionally manages default model chains, the allowed user list, the
  Gemini API key, and views system status.
- `/setkey <key>`: admin-only command to update the API key. The bot immediately
  deletes the message containing the key and replies with a confirmation showing only
  the fingerprint. The key still transits Telegram's servers, so this is intended for
  freshly minted keys with plans to revoke the old one afterwards.
- Unknown commands: allowed users receive "Unknown command — try /help". Non-allowed
  users are silently ignored.
