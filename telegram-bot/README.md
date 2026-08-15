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

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and get its token.
2. Get a Gemini API key from [Google AI Studio](https://aistudio.google.com/).
3. Find your Telegram numeric user ID (e.g. via [@userinfobot](https://t.me/userinfobot)).
4. Copy `.env.example` to `.env` and fill in `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, and
   `ALLOWED_USER_ID` (comma-separated for multiple users).
5. Install dependencies:
   ```sh
   python3 -m venv venv
   venv/bin/pip install -r requirements.txt
   ```
6. Run it directly for testing:
   ```sh
   venv/bin/python sedai_bot.py
   ```

## Running as a systemd service

`sedai-bot.service` is a template unit file. Adjust `WorkingDirectory`,
`EnvironmentFile`, `ExecStart`, and `User`/`Group` to match your deployment path and
user, then:

```sh
sudo cp sedai-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sedai-bot
```

## Notes

- Only the Telegram user IDs listed in `ALLOWED_USER_ID` can use the bot; everyone else
  is silently ignored.
- `.env` is gitignored — never commit real tokens/keys.
