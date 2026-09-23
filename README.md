# Channel Rewriter

Watches Telegram channels. When a new post shows up, Gemini writes a fresh
version of it and the bot posts that to your group, with the original's
photos/videos. Published posts carry no links. In review mode, the drafts you
get in DMs link to the original, so you can check the source.

```
source channels ──> reader account (Telethon) ──> Gemini (free API) ──> your group
                                                        │
                                           review mode: drafts to your DMs
                                           with ✅ Post / ✏️ Edit / 🗑 Skip / 🔄 Rewrite
```

## Why two Telegram accounts?

- **Reader account.** Normal bots can't read channels they aren't admins of,
  so a real Telegram account reads the sources. **Use a spare account, not
  your main one.** Heavy automation can get an account limited.
- **Poster bot (optional, recommended).** A normal @BotFather bot that posts
  into your group and sends you drafts in review mode. Without it, the
  reader account posts by itself.

## 1. Get the keys

| What | Where |
|---|---|
| `TG_API_ID`, `TG_API_HASH` | Log in to [my.telegram.org](https://my.telegram.org) **with the reader account**, then *API development tools* and create an app (any name). |
| `GEMINI_API_KEY` | [aistudio.google.com](https://aistudio.google.com), then *Get API key*. Free, no card needed. |
| `BOT_TOKEN` (optional) | [@BotFather](https://t.me/BotFather), then `/newbot`. |
| `ADMIN_ID` (optional) | DM [@userinfobot](https://t.me/userinfobot) from **your own** account. |

With the reader account, **join every source channel**. It only sees
channels it is subscribed to.

If you use a bot, add it to your group as an admin who can post messages.
Then send any message in the group, so the bot "sees" the group once.

## 2. Install

**Windows:** double-click `Windows\setup.bat`. It creates the venv, installs
packages, opens `.env` in Notepad for you to fill in, then logs in the reader
account.

**Linux:**
```bash
chmod +x Linux/*.sh
Linux/setup.sh      # creates .env, then stops
nano .env           # fill it in
Linux/setup.sh      # installs and logs in the reader account
```

When login asks for a phone number, enter the **reader account's** number in
international format (`+7...`). Telegram sends the code to that account's
Telegram app. After logging in, `login.py` lists the account's channels and
groups with their ids. Copy the ones you want into `SOURCE_CHANNELS` and
`TARGET_CHAT`.

Login creates `reader.session`. **Treat it like a password.** Anyone with this
file can use that account. Don't send it to anyone or commit it
(`.gitignore` already excludes it).

## 3. Run

| | |
|---|---|
| Console (Windows) | `Windows\run.bat` |
| Tray, no console (Windows) | `Windows\run_tray.bat`. The tray icon has *Open log* and *Quit*. |
| Console (Linux) | `Linux/run.sh` |
| WolfCave panel | Drop the folder in with the other bots. `manifest.json` is already there. Run the setup script once first, because the login step needs a console. |
| Always on (Linux) | See the systemd section below. |

Everything is logged to `bot.log` in the project folder.

## 4. Change the writing style

Edit **`prompt.txt`**. This is the whole "personality" of the bot: tone,
length, language, what to skip. It is re-read for every post, so changes apply
without a restart.

To make Gemini drop a post, the prompt tells it to answer `SKIP`. By default it
skips ads, giveaways and posts with no content. Add your own rules there,
e.g. *"skip anything about crypto"*.

## 5. Review mode

In `.env`, set `BOT_TOKEN`, set `ADMIN_ID` to your own id, and set
`REVIEW_MODE=1`. Press **Start** in the bot's chat once. From then on, every
draft comes to your DMs first:

- The images come first as a numbered album, then the draft with a 🔗 link
  to the original post, a 🕒 line showing how old that post is (so you can
  tell fresh news from a backlog of "olds"), and a line saying how many
  images will be posted.
- ✅ **Post** publishes it to the group (text and chosen images, no link).
- ✏️ **Edit** opens a two-step editor:
  1. **Images.** Tap the numbers to include (✅) or exclude (⬜) each one,
     then **Next**. Excluding all of them makes a text-only post. Posts with
     no images skip this step.
  2. **Text.** The bot sends the current text as a monospace block. Tap it
     to copy, change what you want, and send it back as a normal message.
     Or tap **Keep current text**.

  You then get the updated draft with the same buttons, so you can post it,
  edit again or skip. **Cancel** at any step restores the draft as it was.
  Only one draft is edited at a time: starting an edit on another draft puts
  the first one back unchanged.
- 🗑 **Skip** drops it.
- 🔄 **Rewrite with Gemini** asks for a whole new version.

`**bold**` and `__italic__` in your text are formatted when posted.

Send `/status` to the bot at any time to see uptime, counts, queue size, and
(if any drafts are waiting) each one's age, oldest first.

## 6. Daily summary

With `BOT_TOKEN` and `ADMIN_ID` set, the bot DMs you once a day (default
18:00 UTC, set with `DAILY_SUMMARY_HOUR_UTC`): **📰 Show summary** /
**Not today**. Tapping **Show summary** reposts every post published since
UTC midnight back to you as its own DM — full text, with a 📢 link to where
it landed in your channel and a 🔗 link to the original source (the 📢 link
only appears if `TARGET_CHAT` is a public channel/group with a username).
If nothing was posted, you get `No new posts today` instead. Set
`DAILY_SUMMARY=0` to turn this off.

Note: original images/videos aren't re-sent here (they're deleted from disk
right after posting) — the 📢 link takes you straight to the published post,
media included.

## 7. `/settings` — changing things without editing files

With `BOT_TOKEN` and `ADMIN_ID` set, send `/settings` to the bot for a menu
you can run entirely from Telegram — no `.env` or `prompt.txt` editing, no
restart. Good for handing the bot off to someone non-technical.

- **📡 Channels** — lists what's being watched, with a ❌ button to remove
  each one, and **➕ Add channel**: send an `@username`, `t.me` link, or just
  forward any message from the channel. If it's public, the reader account
  joins it automatically. (The last remaining channel can't be removed.)
- **📝 Writing style** — shows the current `prompt.txt` and lets you replace
  it by sending new text — this is the same file described in section 4,
  just editable from chat instead of a text editor.
- **🔄 Review mode** / **🖼 Media** — one-tap on/off toggles.
- **⏰ Daily summary** — turn the daily prompt on/off and change what hour
  (UTC) it's sent at.

Every change here is written straight into `.env`, so it survives a
restart, and takes effect immediately without one.

## 8. Language

The bot's own messages and buttons (not the rewritten posts themselves,
which stay in whatever language `prompt.txt` and the source posts are in)
can be English or Russian. Set `LANG=en` or `LANG=ru` in `.env`, or leave it
blank: the first time the bot starts with `BOT_TOKEN` + `ADMIN_ID` set, it
DMs you 🌐 **Choose language**. Change it later anytime from **🌐 Language**
in `/settings`. Translations live in `locales/en.json` and `locales/ru.json`.

The first time a language is chosen, the bot also sends a short one-time
credits message. The same info is always available from **ℹ️ About** in
`/settings`.

## Credits

Made by [LunaTheWolf](https://github.com/Vov4ik3), built with
[Claude](https://claude.com) (Anthropic).

Drafts are kept in memory, so restarting the bot expires any that haven't been
answered yet.

## Settings (`.env`)

| Setting | Default | |
|---|---|---|
| `SOURCE_CHANNELS` | | `@name`, `t.me/name` or numeric id, comma-separated |
| `TARGET_CHAT` | | `@name` or numeric id (`-100…`) |
| `GEMINI_MODEL` | `gemini-flash-latest` | Any free-tier Flash model works |
| `MIN_TEXT_LENGTH` | `50` | Shorter posts are ignored |
| `INCLUDE_MEDIA` | `1` | Re-post the original's photos/videos |
| `MAX_MEDIA_MB` | `45` | Bigger files are dropped (bot upload limit is 50 MB) |
| `GEMINI_DELAY_SECONDS` | `6` | Pause between Gemini calls |

## Always on with systemd (Linux)

```bash
mkdir -p ~/.config/systemd/user
cp channel-rewriter.service ~/.config/systemd/user/
# edit the paths in it if the project isn't at ~/channel-rewriter
systemctl --user daemon-reload
systemctl --user enable --now channel-rewriter.service
loginctl enable-linger $USER      # keep running after logout
journalctl --user -u channel-rewriter.service -f
```

## How it behaves

- **Only new posts.** It doesn't go back through old ones. Edited and deleted
  posts are ignored.
- **Duplicates.** When the same text appears in two channels (common with
  reposts), only the first copy is used. Posts are remembered for 30 days in
  `seen.db`.
- **Media-only posts** (a photo with no caption) are skipped, because there's
  nothing to rewrite.
- **Albums** are re-posted as albums, with the text as the caption. If the text
  is longer than Telegram's 1024-character caption limit, it goes as a
  separate message right after the media.
- **Rate limits.** The free tier allows roughly 10–15 requests a minute.
  Posts are processed one at a time with a pause between them. On a "429"
  error the bot waits and retries.
- **Privacy.** On the free tier Google may use requests to improve its
  models. That's fine for public channel posts.

## Troubleshooting

| Log says | Fix |
|---|---|
| `Reader account isn't logged in` | Run `login.py` (or setup again). |
| `Can't open source channel` | The reader account isn't subscribed, or the name is wrong. `login.py` lists the right ids. |
| `Can't open TARGET_CHAT` | For a bot: add it to the group, post any message there, restart. For the reader account: it must be a member. |
| `Gemini error 429` again and again | You've hit the daily free limit. Switch `GEMINI_MODEL` to a Flash-Lite model (AI Studio lists the current names; its limits are higher) or raise `GEMINI_DELAY_SECONDS`. |
| `Gemini returned an empty answer` | The post tripped Gemini's safety filter, so it was dropped. |
| Nothing happens at all | Check that new posts actually appeared. Only posts published *after* the bot started count. |

## A note on content

Rewriting other channels' posts is common, but it can annoy their owners, and
close copies are a copyright gray area. Posts no longer credit the source,
so real rewriting (not paraphrasing sentence by sentence) matters more:
combining sources, adding your own take, and not reposting others' original
photos wholesale.
