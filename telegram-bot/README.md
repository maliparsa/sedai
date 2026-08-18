# Sedai Telegram Bot

**English** | [فارسی](#farsi)

A personal Telegram bot backed by Gemini: transcribes voice notes in whatever language
they're spoken in, and doubles as a general AI assistant — summarizing, drafting
replies, and plain chat.

Access is limited to a list of Telegram user IDs you control; everyone else is silently
ignored.

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
  and text models. The admin can set default chains, manage the user list, update the API
  key, and view status — all applied live without a restart.
- **Standing instructions**: each user can set their own instructions (up to 500
  characters) that shape how the bot writes for them — one for draft replies, one for
  plain chat, one for summaries, and one for transcripts. Call a style command with no argument to see the current
  instruction in full and reply to update it; call it with text to set it inline. Reply
  with exactly `clear` to remove one. They apply live, and a voice-dictated per-message
  instruction outranks the standing reply instruction.
- **Automatic timestamps on long recordings**: anything longer than 10 minutes is
  transcribed as `[MM:SS]` caption cues of roughly 10-15 words, so you can find your place
  in it; shorter recordings are left as plain prose. The threshold is per-user and
  adjustable under `/settings` (5 min to 1 hour, or off). A `/transcriptstyle` instruction
  of your own takes precedence over it.
- **Transcript instructions**: `/transcriptstyle` shapes transcription itself — for
  example `add [MM:SS] timestamps`, useful as closed-captioning on long forwarded audio.
  It is the one standing instruction that overrides its task's defaults rather than
  deferring to them, because transcription is otherwise pinned to verbatim output. That
  cuts both ways: asking it to tidy, shorten, or translate changes what the transcript
  says, not just how it reads, so a verbatim record is no longer guaranteed. Timestamps
  are the model's own estimate from the audio, not forced alignment — close on short
  notes, approximate on long recordings.
- **Reply-based input**: menu buttons and no-argument commands that need input send a
  prompt you reply to, instead of telling you to retype a command. There is no timeout,
  and a message that isn't a reply to a prompt is never mistaken for one.
- **Command discovery**: `/help` and `/start` list the available commands for your role.
  Telegram's "/" menu is populated at startup, and admin commands appear only in the
  admin's own chat.

## Setup

### Quick start

```sh
python3 telegram-bot/setup.py          # guided setup: prompts, validates, writes .env
python3 telegram-bot/setup.py --check  # health check: verify an existing install
```

The setup script guides you through configuration, validates your keys, and writes `.env` atomically at mode 0600. It never echoes secrets and never runs `sudo`. See `AGENTS.md` for additional notes.

### Manual setup

If you prefer to configure by hand:

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
| `/replystyle [text]` | all allowed users | view your standing instruction for draft replies, or set a new one; reply with `clear` to remove |
| `/chatstyle [text]` | all allowed users | view your standing instruction for plain chat, or set a new one; reply with `clear` to remove |
| `/summarystyle [text]` | all allowed users | view your standing instruction for summaries, or set a new one; reply with `clear` to remove |
| `/transcriptstyle [text]` | all allowed users | view your standing instruction for transcripts (e.g. `add [MM:SS] timestamps`), or set a new one; reply with `clear` to remove |
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

- **Offline suites** (`telegram-bot/tests/`): `python3 telegram-bot/tests/run_all.py` runs every suite against
  Telegram and Gemini stubs. No network and no real credentials required, so they run
  anywhere.
- **Smoke test** (`smoke_test.py`): run it with the venv python to exercise the real
  `python-telegram-bot` library — command parsing for non-ASCII scripts (Farsi), emoji,
  multi-line and `@botname` arguments, plus the message filters the reply-based input
  flow depends on. The offline stubs implement command matching themselves and so cannot
  cover this. Also needs no network or credentials.

## Notes

- Only the Telegram user IDs in `ALLOWED_USER_ID` can use the bot; everyone else is
  silently ignored. The first ID in the list is the admin.
- `.env` is gitignored — never commit real tokens or keys.
- `settings.json` (created alongside `.env` on first run, chmod 600) persists per-user
  preferences and global settings, and overrides `.env` where present. This is what lets
  configuration change live, without a restart or a redeploy.
- `/setkey <key>`: the bot deletes the message containing the key immediately and replies
  with a redacted fingerprint only. The key still passes through Telegram's servers, so
  use it with a freshly minted key and revoke the old one afterwards.
- Unknown commands: allowed users get "Unknown command — try /help". Everyone else is
  ignored.

---

<a id="farsi"></a>

<div dir="rtl">

# ربات تلگرام Sedai

[English](#sedai-telegram-bot) | **فارسی**

یک ربات شخصی تلگرام مبتنی بر Gemini: پیام‌های صوتی را به هر زبانی که گفته شده‌اند
رونویسی می‌کند و در کنار آن نقش یک دستیار هوش مصنوعی را دارد — خلاصه‌سازی، نوشتن
پیش‌نویس پاسخ، و گفت‌وگوی متنی.

دسترسی محدود به فهرستی از شناسه‌های کاربری تلگرام است که خودتان تعیین می‌کنید؛ پیام
دیگران بدون هیچ پاسخی نادیده گرفته می‌شود.

## قابلیت‌ها

- **رونویسی**: یک پیام صوتی یا فایل صوتی بفرستید و متن آن را دریافت کنید، به همان زبان و
  خطی که صحبت شده است.
- **خلاصه‌سازی / پیش‌نویس پاسخ**: زیر هر متن رونویسی‌شده دو دکمه برای خلاصه کردن آن یا
  نوشتن پیش‌نویس پاسخ قرار دارد.
- **پاسخ همراه با دستور**: اگر به یک متن رونویسی‌شده با پیام صوتی خودتان پاسخ دهید، ربات
  آن را به عنوان دستور شفاهی برای نوشتن پاسخ به پیام اصلی در نظر می‌گیرد.
- **گفت‌وگوی متنی**: هر پیام متنی به Gemini فرستاده می‌شود و تاریخچهٔ گفت‌وگو برای هر چت
  جداگانه نگه داشته می‌شود. دستور `/reset` آن را پاک می‌کند.
- **زنجیرهٔ مدل‌های جایگزین**: برای هر کار، فهرستی از مدل‌های Gemini به ترتیب امتحان
  می‌شود؛ در صورت محدودیت نرخ درخواست یا خطای سرور، مدل بعدی جایگزین می‌شود تا کار
  بی‌نتیجه نماند.
- **تنظیم مدل برای هر کاربر**: با `/settings` هر کاربر می‌تواند مدل صوتی و متنی دلخواه
  خود را انتخاب کند. مدیر علاوه بر این می‌تواند زنجیرهٔ پیش‌فرض مدل‌ها، فهرست کاربران
  مجاز و کلید API را مدیریت کند و وضعیت سیستم را ببیند — همهٔ تغییرات بدون راه‌اندازی
  مجدد اعمال می‌شوند.
- **دستورهای دائمی**: هر کاربر می‌تواند دستورهای شخصی خودش را (حداکثر ۵۰۰ نویسه) تعیین
  کند تا لحن و شیوهٔ نوشتن ربات را تغییر دهد — یکی برای پیش‌نویس پاسخ‌ها، یکی برای
  گفت‌وگوی متنی، یکی برای خلاصه‌ها و یکی برای متن‌های رونویسی‌شده. اگر دستور مربوطه را بدون متن بفرستید، مقدار فعلی به
  طور کامل نمایش داده می‌شود و می‌توانید با پاسخ دادن آن را تغییر دهید؛ اگر همراه با متن
  بفرستید، مستقیماً تنظیم می‌شود. پاسخ دادن با واژهٔ `clear` آن را حذف می‌کند. این
  دستورها بی‌درنگ اعمال می‌شوند، و دستور صوتیِ مخصوصِ یک پیام بر دستور دائمیِ پاسخ اولویت
  دارد.
- **برچسب زمانی خودکار برای فایل‌های طولانی**: هر فایل صوتی بلندتر از ۱۰ دقیقه به صورت
  زیرنویس با برچسب `[MM:SS]` و قطعه‌های حدوداً ۱۰ تا ۱۵ کلمه‌ای رونویسی می‌شود تا بتوانید
  جای خود را در متن پیدا کنید؛ فایل‌های کوتاه‌تر به شکل متن ساده می‌مانند. این آستانه برای
  هر کاربر جداگانه است و در `/settings` قابل تنظیم (از ۵ دقیقه تا ۱ ساعت، یا خاموش) است.
  اگر خودتان دستور `/transcriptstyle` تنظیم کرده باشید، آن دستور اولویت دارد.
- **دستور دائمی رونویسی**: دستور `/transcriptstyle` روی خودِ رونویسی اثر می‌گذارد — برای
  نمونه «برچسب زمانی [MM:SS] اضافه کن»، که برای فایل‌های صوتی طولانی حکم زیرنویس را دارد.
  این تنها دستور دائمی است که بر تنظیمات پیش‌فرضِ کارِ خودش اولویت دارد، چون در حالت عادی
  رونویسی به خروجی واژه‌به‌واژه مقید است. همین موضوع دو رو دارد: اگر بخواهید متن را
  مرتب، کوتاه یا ترجمه کند، محتوای رونویسی تغییر می‌کند نه فقط شکل آن، و دیگر
  واژه‌به‌واژه بودنش تضمین نیست. برچسب‌های زمانی تخمین خودِ مدل از روی صداست، نه
  هم‌ترازیِ دقیق — در پیام‌های کوتاه نزدیک و در فایل‌های طولانی تقریبی است.
- **ورودی از طریق پاسخ**: دکمه‌های منو و دستورهایی که به ورودی نیاز دارند، به جای اینکه
  از شما بخواهند دستور را دوباره تایپ کنید، پیامی می‌فرستند که به آن پاسخ می‌دهید. هیچ
  محدودیت زمانی وجود ندارد و پیامی که پاسخ به آن درخواست نباشد، هرگز به اشتباه به عنوان
  ورودی برداشت نمی‌شود.
- **معرفی دستورها**: دستورهای `/help` و `/start` فهرست دستورهای متناسب با نقش شما را
  نشان می‌دهند. منوی «/» تلگرام هنگام راه‌اندازی پر می‌شود و دستورهای مدیریتی فقط در چت
  مدیر دیده می‌شوند.

## راه‌اندازی

### شروع سریع

```sh
python3 telegram-bot/setup.py          # راه‌اندازی گام‌به‌گام: پرسش، اعتبارسنجی، ساخت .env
python3 telegram-bot/setup.py --check  # بررسی سلامت: وارسی یک نصب موجود
```

اسکریپت راه‌اندازی شما را گام‌به‌گام در پیکربندی همراهی می‌کند، درستی توکن ربات و کلید
Gemini را پیش از ذخیره بررسی می‌کند و فایل `.env` را به‌صورت اتمی با دسترسی ۶۰۰
می‌نویسد. این اسکریپت هرگز مقدار توکن یا کلید را نمایش نمی‌دهد و هرگز `sudo` اجرا
نمی‌کند؛ دستورهای مربوط به systemd را فقط چاپ می‌کند تا خودتان اجرا کنید. برای
مشارکت‌کنندگان، فایل `AGENTS.md` نکته‌های بیشتری دارد.

### راه‌اندازی دستی

اگر ترجیح می‌دهید پیکربندی را دستی انجام دهید:

۱. با [@BotFather](https://t.me/BotFather) یک ربات بسازید و توکن آن را بگیرید.

۲. از [Google AI Studio](https://aistudio.google.com/) یک کلید API برای Gemini بگیرید.

۳. شناسهٔ عددی کاربری تلگرام خود را پیدا کنید (مثلاً با
[@userinfobot](https://t.me/userinfobot)).

۴. فایل `.env.example` را به `.env` کپی کنید و مقادیر `TELEGRAM_BOT_TOKEN`،
`GEMINI_API_KEY` و `ALLOWED_USER_ID` را پر کنید. مقدار آخر فهرستی از شناسه‌ها با
جداکنندهٔ ویرگول است و **اولین شناسه، مدیر** است که تنظیمات کلی را در اختیار دارد؛
بقیه کاربران عادی هستند.

۵. وابستگی‌ها را نصب کنید:

```sh
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

۶. برای آزمایش، مستقیم اجرا کنید:

```sh
venv/bin/python sedai_bot.py
```

## دستورها

| دستور | چه کسی | کارکرد |
|---|---|---|
| `/help` | همهٔ کاربران مجاز | فهرست دستورها و نحوهٔ استفاده از ربات |
| `/start` | همهٔ کاربران مجاز | مانند `/help`، در نخستین تماس نمایش داده می‌شود |
| `/settings` | همهٔ کاربران مجاز | تنظیم مدل صوتی و متنی، و دیدن یا پاک کردن دستورهای دائمی |
| `/replystyle [متن]` | همهٔ کاربران مجاز | دیدن یا تنظیم دستور دائمی برای پیش‌نویس پاسخ‌ها؛ پاسخ با `clear` آن را حذف می‌کند |
| `/chatstyle [متن]` | همهٔ کاربران مجاز | دیدن یا تنظیم دستور دائمی برای گفت‌وگوی متنی؛ پاسخ با `clear` آن را حذف می‌کند |
| `/summarystyle [متن]` | همهٔ کاربران مجاز | دیدن یا تنظیم دستور دائمی برای خلاصه‌ها؛ پاسخ با `clear` آن را حذف می‌کند |
| `/transcriptstyle [متن]` | همهٔ کاربران مجاز | دیدن یا تنظیم دستور دائمی برای رونویسی (مثلاً «برچسب زمانی [MM:SS] اضافه کن»)؛ پاسخ با `clear` آن را حذف می‌کند |
| `/reset` | همهٔ کاربران مجاز | پاک کردن تاریخچهٔ گفت‌وگو |
| `/setkey <کلید>` | فقط مدیر | به‌روزرسانی کلید API مربوط به Gemini |
| `/adduser <شناسه>` | فقط مدیر | افزودن یک شناسهٔ کاربری تلگرام به فهرست مجاز |

کاربران عادی دستورهای مدیریتی را در منوی «/» تلگرام خود نمی‌بینند.

## اجرا به صورت سرویس systemd

فایل `sedai-bot.service` یک نمونهٔ آماده است. مقادیر `WorkingDirectory`،
`EnvironmentFile`، `ExecStart` و `User`/`Group` را متناسب با مسیر و کاربر خودتان تغییر
دهید و سپس:

```sh
sudo cp sedai-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sedai-bot
```

## آزمون‌ها

- **آزمون‌های آفلاین** (`telegram-bot/tests/`): با اجرای `python3 telegram-bot/tests/run_all.py` همهٔ مجموعه‌ها در
  برابر نسخه‌های شبیه‌سازی‌شدهٔ تلگرام و Gemini اجرا می‌شوند. به اینترنت و کلید واقعی
  نیازی ندارند و همه‌جا قابل اجرا هستند.
- **آزمون دود** (`smoke_test.py`): این آزمون را با پایتونِ venv اجرا کنید تا کتابخانهٔ
  واقعی `python-telegram-bot` آزموده شود — تجزیهٔ دستورها برای خط‌های غیرلاتین (فارسی)،
  ایموجی، متن چندخطی و پسوند `@botname`، و همچنین فیلترهایی که سازوکار ورودی از طریق
  پاسخ به آن‌ها متکی است. نسخه‌های شبیه‌سازی‌شده خودشان تطبیق دستورها را پیاده کرده‌اند و
  به همین دلیل نمی‌توانند این موارد را پوشش دهند. این آزمون هم به اینترنت و کلید واقعی
  نیاز ندارد.

## نکته‌ها

- تنها شناسه‌های موجود در `ALLOWED_USER_ID` می‌توانند از ربات استفاده کنند؛ پیام بقیه
  بدون پاسخ نادیده گرفته می‌شود. اولین شناسهٔ فهرست، مدیر است.
- فایل `.env` در `.gitignore` قرار دارد — هرگز توکن یا کلید واقعی را در مخزن ثبت نکنید.
- فایل `settings.json` (که در نخستین اجرا کنار `.env` ساخته می‌شود و دسترسی آن ۶۰۰ است)
  تنظیمات هر کاربر و تنظیمات کلی را نگه می‌دارد و بر مقادیر `.env` اولویت دارد. همین
  موضوع باعث می‌شود پیکربندی بدون راه‌اندازی مجدد و بدون استقرار دوباره تغییر کند.
- دستور `/setkey <کلید>`: ربات بی‌درنگ پیام حاوی کلید را حذف می‌کند و تنها یک اثر انگشت
  کوتاه از کلید را در پاسخ نشان می‌دهد. با این حال کلید از سرورهای تلگرام عبور کرده است،
  بنابراین بهتر است کلیدی تازه‌ساخته را وارد کنید و کلید قبلی را پس از آن باطل کنید.
- دستور ناشناخته: کاربران مجاز پیام «Unknown command — try /help» را می‌گیرند و بقیه
  نادیده گرفته می‌شوند.

</div>
