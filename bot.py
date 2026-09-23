"""
Channel Rewriter: watches Telegram channels, rewrites new posts with Gemini,
and publishes them to your group.

    python login.py   # once, to log the reader account in
    python bot.py     # run the bot (or tray_bot.py on Windows for no console)

All settings live in .env; the rewriting style lives in prompt.txt.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from telethon import Button, TelegramClient, events
from telethon.tl.types import MessageEntityPre

BASE_DIR = Path(__file__).resolve().parent
TMP_DIR = BASE_DIR / "tmp"
DB_PATH = BASE_DIR / "seen.db"
LOG_PATH = BASE_DIR / "bot.log"
PROMPT_PATH = BASE_DIR / "prompt.txt"
USER_SESSION = str(BASE_DIR / "reader")    # -> reader.session
BOT_SESSION = str(BASE_DIR / "poster")     # -> poster.session

CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096

log = logging.getLogger("rewriter")


def utf16_len(text: str) -> int:
    """Telegram measures entity offsets in UTF-16 code units (emoji count as 2)."""
    return len(text.encode("utf-16-le")) // 2


def format_age(dt: datetime) -> str:
    """'23 Sep, 14:32 UTC (2h ago)', so stale drafts stand out in review."""
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 60:
        age = "just now"
    elif secs < 3600:
        age = f"{int(secs // 60)}m ago"
    elif secs < 86400:
        age = f"{int(secs // 3600)}h ago"
    else:
        age = f"{int(secs // 86400)}d ago"
    return f"{dt.strftime('%d %b, %H:%M UTC')} ({age})"


# ---------------------------------------------------------------- config ---

class ConfigError(Exception):
    pass


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        if required:
            raise ConfigError(f"{name} is missing in .env")
        return default or ""
    return value


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if not value:
        return default
    return value.lower() in ("1", "true", "yes", "on")


def _chat_ref(value: str) -> int | str:
    """'-100123' -> int, '@name' / 'name' / t.me link -> username string."""
    value = value.strip()
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    value = re.sub(r"^(https?://)?t\.me/", "", value)
    return value.lstrip("@")


@dataclass
class Config:
    api_id: int
    api_hash: str
    gemini_key: str
    sources: list[int | str]
    target: int | str
    bot_token: str = ""
    review_mode: bool = False
    admin_id: int = 0
    model: str = "gemini-flash-latest"
    min_length: int = 50
    include_media: bool = True
    max_media_mb: int = 45
    gemini_delay: float = 6.0
    daily_summary: bool = True
    daily_summary_hour: int = 18

    @classmethod
    def load(cls) -> "Config":
        load_dotenv(BASE_DIR / ".env")
        try:
            api_id = int(_env("TG_API_ID", required=True))
        except ValueError:
            raise ConfigError("TG_API_ID must be a number")
        sources = [_chat_ref(s) for s in _env("SOURCE_CHANNELS", required=True).split(",") if s.strip()]
        if not sources:
            raise ConfigError("SOURCE_CHANNELS is empty")
        cfg = cls(
            api_id=api_id,
            api_hash=_env("TG_API_HASH", required=True),
            gemini_key=_env("GEMINI_API_KEY", required=True),
            sources=sources,
            target=_chat_ref(_env("TARGET_CHAT", required=True)),
            bot_token=_env("BOT_TOKEN"),
            review_mode=_env_bool("REVIEW_MODE", False),
            admin_id=int(_env("ADMIN_ID", "0") or 0),
            model=_env("GEMINI_MODEL", "gemini-flash-latest"),
            min_length=int(_env("MIN_TEXT_LENGTH", "50")),
            include_media=_env_bool("INCLUDE_MEDIA", True),
            max_media_mb=int(_env("MAX_MEDIA_MB", "45")),
            gemini_delay=float(_env("GEMINI_DELAY_SECONDS", "6")),
            daily_summary=_env_bool("DAILY_SUMMARY", True),
            daily_summary_hour=int(_env("DAILY_SUMMARY_HOUR_UTC", "18")),
        )
        if cfg.review_mode and not (cfg.bot_token and cfg.admin_id):
            raise ConfigError("REVIEW_MODE=1 needs both BOT_TOKEN and ADMIN_ID")
        if not PROMPT_PATH.exists():
            raise ConfigError("prompt.txt is missing next to bot.py")
        return cfg


def setup_logging() -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)
    if sys.stdout is not None:  # pythonw.exe has no console
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        root.addHandler(console)
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)


# ------------------------------------------------------------ dedupe db ---

class SeenStore:
    """Remembers what was already processed, so reposts across channels and
    restarts don't produce duplicates."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS seen (hash TEXT PRIMARY KEY, created INTEGER)")
        self.db.execute("DELETE FROM seen WHERE created < ?", (int(time.time()) - 30 * 86400,))
        self.db.commit()

    @staticmethod
    def key(text: str) -> str:
        normalized = re.sub(r"\W+", "", text.lower())
        return hashlib.sha1(normalized.encode()).hexdigest()

    def check_and_add(self, text: str) -> bool:
        """True if new (and records it), False if seen before."""
        try:
            self.db.execute("INSERT INTO seen VALUES (?, ?)", (self.key(text), int(time.time())))
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False


# ------------------------------------------------------------- rewriter ---

class Rewriter:
    SKIP = "SKIP"

    def __init__(self, cfg: Config):
        self.client = genai.Client(api_key=cfg.gemini_key)
        self.model = cfg.model

    @staticmethod
    def _prompt() -> str:
        # re-read every time, so edits to prompt.txt apply without a restart
        return PROMPT_PATH.read_text(encoding="utf-8").strip()

    async def rewrite(self, text: str, source_title: str) -> str | None:
        """Returns new text, "SKIP", or None if Gemini failed."""
        user_msg = f"Source channel: {source_title}\n\nOriginal post:\n{text}"
        for attempt in range(4):
            try:
                resp = await self.client.aio.models.generate_content(
                    model=self.model,
                    contents=user_msg,
                    config=genai_types.GenerateContentConfig(
                        system_instruction=self._prompt(),
                        temperature=0.8,
                    ),
                )
                out = (resp.text or "").strip()
                if not out:
                    log.warning("Gemini returned an empty answer (possibly blocked by safety filters)")
                    return None
                return self.SKIP if out.strip(" .").upper() == self.SKIP else out
            except genai_errors.APIError as e:
                if e.code == 429 or (e.code and e.code >= 500):
                    wait = 20 * (attempt + 1)
                    log.warning("Gemini error %s, retrying in %ss", e.code, wait)
                    await asyncio.sleep(wait)
                    continue
                log.error("Gemini error %s: %s", e.code, e)
                return None
            except Exception:
                log.exception("Gemini request failed")
                await asyncio.sleep(10)
        return None


# ------------------------------------------------------------------ app ---

@dataclass
class Post:
    text: str
    source_title: str
    link: str
    post_date: datetime
    files: list[Path] = field(default_factory=list)
    workdir: Path | None = None
    new_text: str = ""
    excluded: set[int] = field(default_factory=set)   # indexes of files not to post
    _backup: tuple[str, set[int]] | None = None

    @property
    def chosen_files(self) -> list[Path]:
        return [f for i, f in enumerate(self.files) if i not in self.excluded]

    def snapshot(self) -> None:
        self._backup = (self.new_text, set(self.excluded))

    def restore(self) -> None:
        if self._backup:
            self.new_text, self.excluded = self._backup[0], set(self._backup[1])
            self._backup = None

    def cleanup(self) -> None:
        if self.workdir:
            shutil.rmtree(self.workdir, ignore_errors=True)


class App:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.user = TelegramClient(USER_SESSION, cfg.api_id, cfg.api_hash)
        self.bot = TelegramClient(BOT_SESSION, cfg.api_id, cfg.api_hash) if cfg.bot_token else None
        self.rewriter = Rewriter(cfg)
        self.seen = SeenStore(DB_PATH)
        self.queue: asyncio.Queue[Post] = asyncio.Queue()
        self.pending: dict[str, Post] = {}      # review mode drafts
        self.editing: str | None = None         # draft id waiting for new text
        self.target_entity = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stats = {"posted": 0, "skipped": 0, "failed": 0, "started": time.time()}
        self.posted_log: list[tuple[datetime, str, str, str, str]] = []
        # (posted_at, source_title, new_text, source_link, posted_link)

    @property
    def poster(self) -> TelegramClient:
        return self.bot or self.user

    # ---- startup / shutdown

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        shutil.rmtree(TMP_DIR, ignore_errors=True)
        TMP_DIR.mkdir(exist_ok=True)

        await self.user.connect()
        if not await self.user.is_user_authorized():
            raise ConfigError("Reader account isn't logged in. Run login.py once first.")
        me = await self.user.get_me()
        log.info("Reader account: %s (id %s)", me.first_name, me.id)

        if self.bot:
            await self.bot.start(bot_token=self.cfg.bot_token)
            log.info("Posting through bot @%s", (await self.bot.get_me()).username)
            self.bot.add_event_handler(self.on_callback, events.CallbackQuery())
            self.bot.add_event_handler(self.on_command, events.NewMessage(pattern=r"^/(start|status)"))
            self.bot.add_event_handler(self.on_admin_message, events.NewMessage(incoming=True))

        # resolve chats up front so config mistakes show immediately
        source_entities = []
        for src in self.cfg.sources:
            try:
                ent = await self.user.get_entity(src)
                source_entities.append(ent)
                log.info("Watching: %s", getattr(ent, "title", src))
            except Exception as e:
                log.error("Can't open source channel %r (%s). Is the reader account subscribed?", src, e)
        if not source_entities:
            raise ConfigError("None of the SOURCE_CHANNELS could be opened")

        try:
            self.target_entity = await self.poster.get_entity(self.cfg.target)
        except Exception as e:
            hint = (" For a bot: add it to the group, post any message there, then restart."
                    if self.bot else " Is the reader account a member of it?")
            raise ConfigError(f"Can't open TARGET_CHAT {self.cfg.target!r} ({e}).{hint}")
        log.info("Publishing to: %s", getattr(self.target_entity, "title", self.cfg.target))

        self.user.add_event_handler(self.on_message, events.NewMessage(chats=source_entities))
        self.user.add_event_handler(self.on_album, events.Album(chats=source_entities))

    async def run(self) -> None:
        await self.start()
        mode = "review (drafts go to admin first)" if self.cfg.review_mode else "auto-post"
        log.info("Running in %s mode. Model: %s", mode, self.cfg.model)
        if self.cfg.daily_summary and self.bot and self.cfg.admin_id:
            log.info("Daily summary prompt at %02d:00 UTC", self.cfg.daily_summary_hour)
        elif self.cfg.daily_summary:
            log.info("Daily summary is on but needs BOT_TOKEN and ADMIN_ID to send it")
        worker = asyncio.create_task(self.worker())
        summary_task = asyncio.create_task(self.daily_summary_loop())
        try:
            waits = [self.user.run_until_disconnected()]
            if self.bot:
                waits.append(self.bot.run_until_disconnected())
            await asyncio.gather(*waits)
        finally:
            worker.cancel()
            summary_task.cancel()
            for post in self.pending.values():
                post.cleanup()
            log.info("Stopped")

    async def shutdown(self) -> None:
        for client in (self.user, self.bot):
            if client and client.is_connected():
                await client.disconnect()

    def stop_threadsafe(self) -> None:
        """For tray_bot.py: stop the bot from another thread."""
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.shutdown(), self.loop)

    # ---- incoming posts

    async def on_message(self, event) -> None:
        if event.message.grouped_id:  # albums are handled by on_album
            return
        await self.enqueue([event.message], event.message.message or "")

    async def on_album(self, event) -> None:
        await self.enqueue(list(event.messages), event.text or "")

    async def enqueue(self, messages, text: str) -> None:
        text = text.strip()
        if len(text) < self.cfg.min_length:
            return
        if not self.seen.check_and_add(text):
            log.info("Duplicate post ignored")
            return

        first = messages[0]
        chat = await first.get_chat()
        title = getattr(chat, "title", "") or "channel"
        username = getattr(chat, "username", None)
        link = f"https://t.me/{username}/{first.id}" if username else ""
        post = Post(text=text, source_title=title, link=link, post_date=first.date)

        if self.cfg.include_media:
            await self.download_media(post, messages)

        log.info("New post from %s queued (%d media)", title, len(post.files))
        await self.queue.put(post)

    async def download_media(self, post: Post, messages) -> None:
        limit = self.cfg.max_media_mb * 1024 * 1024
        for msg in messages:
            if not (msg.photo or msg.video):
                continue
            size = msg.file.size if msg.file else 0
            if size and size > limit:
                log.info("Media skipped: %.1f MB is over MAX_MEDIA_MB", size / 1048576)
                continue
            if post.workdir is None:
                post.workdir = TMP_DIR / uuid.uuid4().hex[:10]
                post.workdir.mkdir(parents=True)
            try:
                path = await self.user.download_media(msg, file=str(post.workdir) + os.sep)
                if path:
                    post.files.append(Path(path))
            except Exception as e:
                log.warning("Media download failed: %s", e)

    # ---- processing

    async def worker(self) -> None:
        while True:
            post = await self.queue.get()
            try:
                await self.process(post)
            except Exception:
                self.stats["failed"] += 1
                log.exception("Failed to process post from %s", post.source_title)
                post.cleanup()
            await asyncio.sleep(self.cfg.gemini_delay)

    async def process(self, post: Post) -> None:
        new = await self.rewriter.rewrite(post.text, post.source_title)
        if new is None:
            self.stats["failed"] += 1
            log.warning("Rewrite failed, post from %s dropped", post.source_title)
            post.cleanup()
            return
        if new == Rewriter.SKIP:
            self.stats["skipped"] += 1
            log.info("Gemini skipped a post from %s (ad / no content)", post.source_title)
            post.cleanup()
            return
        post.new_text = new
        if self.cfg.review_mode:
            await self.send_for_review(post)
        else:
            await self.publish(post)

    @staticmethod
    def fix_formatting(text: str) -> str:
        """Telethon markdown is **bold** and __italic__. Gemini often writes
        *italic* or _italic_, which would show up as literal symbols."""
        text = re.sub(r"(?<![*\w])\*(?![*\s])(.+?)(?<![*\s])\*(?![*\w])", r"__\1__", text)
        text = re.sub(r"(?<![_\w])_(?![_\s])(.+?)(?<![_\s])_(?![_\w])", r"__\1__", text)
        return text

    def posted_link(self, sent) -> str:
        """Link to the just-published message in TARGET_CHAT, if it's public."""
        username = getattr(self.target_entity, "username", None)
        if not username:
            return ""
        msg = sent[0] if isinstance(sent, list) else sent
        mid = getattr(msg, "id", None)
        return f"https://t.me/{username}/{mid}" if mid else ""

    async def publish(self, post: Post) -> None:
        text = self.fix_formatting(post.new_text)
        files = [str(p) for p in post.chosen_files]
        target = self.target_entity
        try:
            if files:
                if len(text) <= CAPTION_LIMIT:
                    if len(files) > 1:
                        sent = await self.poster.send_file(target, files, caption=[text] + [""] * (len(files) - 1))
                    else:
                        sent = await self.poster.send_file(target, files[0], caption=text)
                else:  # too long for a caption: media first, then the text
                    sent = await self.poster.send_file(target, files)
                    sent = await self.poster.send_message(target, text[:MESSAGE_LIMIT], link_preview=False)
            else:
                sent = await self.poster.send_message(target, text[:MESSAGE_LIMIT], link_preview=False)
            self.stats["posted"] += 1
            self.posted_log.append((
                datetime.now(timezone.utc), post.source_title, post.new_text,
                post.link, self.posted_link(sent),
            ))
            log.info("Posted rewrite of a post from %s", post.source_title)
        finally:
            post.cleanup()

    # ---- review mode
    #
    # Draft message: source link + text, buttons Post / Edit / Skip / Rewrite.
    # Edit: 1) pick which images to keep (toggle buttons), 2) bot sends the
    # current text as a copyable block, admin sends back the new text.

    @staticmethod
    def review_buttons(draft_id: str):
        return [[Button.inline("✅ Post", f"ok:{draft_id}"),
                 Button.inline("✏️ Edit", f"ed:{draft_id}"),
                 Button.inline("🗑 Skip", f"no:{draft_id}")],
                [Button.inline("🔄 Rewrite with Gemini", f"re:{draft_id}")]]

    @staticmethod
    def media_buttons(draft_id: str, post: Post):
        toggles = [Button.inline(f"{'✅' if i not in post.excluded else '⬜'} {i + 1}", f"tg:{draft_id}:{i}")
                   for i in range(len(post.files))]
        rows = [toggles[i:i + 5] for i in range(0, len(toggles), 5)]
        rows.append([Button.inline("Next ➡️", f"md:{draft_id}"), Button.inline("✖ Cancel", f"cx:{draft_id}")])
        return rows

    @staticmethod
    def text_buttons(draft_id: str):
        return [[Button.inline("↩️ Keep current text", f"ek:{draft_id}"),
                 Button.inline("✖ Cancel", f"cx:{draft_id}")]]

    def review_text(self, post: Post) -> str:
        title = post.source_title.replace("[", "(").replace("]", ")")
        source = f"🔗 [{title}]({post.link})" if post.link else f"🔗 {title}"
        when = f"\n🕒 {format_age(post.post_date)}"
        media = ""
        if post.files:
            media = f"\n🖼 {len(post.chosen_files)} of {len(post.files)} images will be posted"
        body = self.fix_formatting(post.new_text)
        return f"{source}{when}{media}\n\n{body}"[:MESSAGE_LIMIT]

    async def send_media_preview(self, post: Post) -> None:
        files = [str(p) for p in post.files]
        captions = [f"{i + 1}" for i in range(len(files))]
        try:
            if len(files) > 1:
                await self.bot.send_file(self.cfg.admin_id, files, caption=captions)
            else:
                await self.bot.send_file(self.cfg.admin_id, files[0], caption=captions[0])
        except Exception as e:
            log.warning("Couldn't send media preview: %s", e)

    async def send_draft(self, draft_id: str, post: Post) -> None:
        await self.bot.send_message(self.cfg.admin_id, self.review_text(post),
                                    buttons=self.review_buttons(draft_id), link_preview=False)

    async def send_for_review(self, post: Post) -> None:
        draft_id = uuid.uuid4().hex[:8]
        self.pending[draft_id] = post
        if post.files:
            await self.send_media_preview(post)
        await self.send_draft(draft_id, post)
        log.info("Draft %s sent for review", draft_id)

    async def ask_for_text(self, draft_id: str, post: Post) -> None:
        self.editing = draft_id
        await self.bot.send_message(
            self.cfg.admin_id,
            "✏️ Send the new text as a message.\n"
            "The current text is below: tap it to copy, edit, and send it back.",
            buttons=self.text_buttons(draft_id))
        text = post.new_text
        await self.bot.send_message(self.cfg.admin_id, text, parse_mode=None,
                                    formatting_entities=[MessageEntityPre(0, utf16_len(text), "")])

    async def cancel_other_edit(self, draft_id: str) -> None:
        """Only one draft can be edited at a time; put the previous one back."""
        old_id = self.editing
        if old_id and old_id != draft_id and old_id in self.pending:
            old = self.pending[old_id]
            old.restore()
            await self.bot.send_message(self.cfg.admin_id, "Previous edit cancelled, here's that draft again:")
            await self.send_draft(old_id, old)
        self.editing = None

    async def on_callback(self, event) -> None:
        if event.sender_id != self.cfg.admin_id:
            await event.answer("Not allowed")
            return
        action, _, rest = event.data.decode().partition(":")

        if action == "ds":
            if rest == "skip":
                await event.answer("Skipped")
                await event.edit("Not today, got it.", buttons=None)
            else:
                await event.answer()
                await self.send_daily_summary(event)
            return

        draft_id, _, arg = rest.partition(":")
        post = self.pending.get(draft_id)
        if post is None:
            await event.answer("This draft expired (bot was restarted?)", alert=True)
            await event.edit(buttons=None)
            return

        if action == "ok":
            self.pending.pop(draft_id)
            if self.editing == draft_id:
                self.editing = None
            await event.answer("Posting…")
            try:
                await self.publish(post)
                await event.edit("✅ Posted\n\n" + self.review_text(post), buttons=None, link_preview=False)
            except Exception as e:
                log.exception("Publishing draft %s failed", draft_id)
                await event.edit(f"❌ Failed to post: {e}"[:MESSAGE_LIMIT], buttons=None)

        elif action == "no":
            self.pending.pop(draft_id)
            if self.editing == draft_id:
                self.editing = None
            post.cleanup()
            self.stats["skipped"] += 1
            await event.answer("Skipped")
            await event.edit("🗑 Skipped\n\n" + self.review_text(post), buttons=None, link_preview=False)

        elif action == "re":
            await event.answer("Rewriting…")
            new = await self.rewriter.rewrite(post.text, post.source_title)
            if new and new != Rewriter.SKIP:
                post.new_text = new
            await event.edit(self.review_text(post), buttons=self.review_buttons(draft_id), link_preview=False)

        elif action == "ed":
            await event.answer()
            await self.cancel_other_edit(draft_id)
            post.snapshot()
            await event.edit("✏️ Editing…\n\n" + self.review_text(post), buttons=None, link_preview=False)
            if post.files:
                await self.send_media_preview(post)
                await self.bot.send_message(
                    self.cfg.admin_id,
                    "🖼 Which images should be posted? Tap a number to include or exclude it.",
                    buttons=self.media_buttons(draft_id, post))
            else:
                await self.ask_for_text(draft_id, post)

        elif action == "tg":
            i = int(arg)
            post.excluded.symmetric_difference_update({i})
            await event.answer(f"Image {i + 1} {'excluded' if i in post.excluded else 'included'}")
            await event.edit(buttons=self.media_buttons(draft_id, post))

        elif action == "md":
            await event.answer()
            chosen = [str(i + 1) for i in range(len(post.files)) if i not in post.excluded]
            await event.edit(f"🖼 Images: {', '.join(chosen) if chosen else 'none (text only)'}", buttons=None)
            await self.ask_for_text(draft_id, post)

        elif action == "ek":
            await event.answer("Text kept")
            self.editing = None
            await event.edit("↩️ Kept the current text", buttons=None)
            await self.send_draft(draft_id, post)

        elif action == "cx":
            await event.answer("Edit cancelled")
            self.editing = None
            post.restore()
            await event.edit("✖ Edit cancelled", buttons=None)
            await self.send_draft(draft_id, post)

    async def on_admin_message(self, event) -> None:
        """Receives the new text while a draft is being edited."""
        if not event.is_private or event.sender_id != self.cfg.admin_id or not self.editing:
            return
        text = (event.text or "").strip()
        if not text or text.startswith("/"):
            return
        # a pasted copy of the code block may keep its ``` or ` wrapping
        for fence in ("```", "`"):
            if text.startswith(fence) and text.endswith(fence) and len(text) > 2 * len(fence):
                text = text[len(fence):-len(fence)].strip()
                break
        draft_id, self.editing = self.editing, None
        post = self.pending.get(draft_id)
        if post is None:
            return
        post.new_text = text
        log.info("Draft %s text edited by admin", draft_id)
        await self.send_draft(draft_id, post)

    # ---- daily summary
    #
    # Once a day, DMs the admin "Show summary" / "Not today". Tapping Show
    # summary lists what got posted since UTC midnight (or says nothing did).

    async def daily_summary_loop(self) -> None:
        if not (self.bot and self.cfg.admin_id and self.cfg.daily_summary):
            return
        while True:
            now = datetime.now(timezone.utc)
            target = now.replace(hour=self.cfg.daily_summary_hour, minute=0, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            await asyncio.sleep((target - now).total_seconds())
            try:
                await self.bot.send_message(
                    self.cfg.admin_id,
                    "📰 Daily summary time. Want to see what got posted today?",
                    buttons=[[Button.inline("📰 Show summary", "ds:show"),
                              Button.inline("Not today", "ds:skip")]])
            except Exception:
                log.exception("Daily summary prompt failed")
            cutoff = datetime.now(timezone.utc) - timedelta(days=2)
            self.posted_log = [e for e in self.posted_log if e[0] >= cutoff]

    async def send_daily_summary(self, event) -> None:
        """Edits the prompt to a header, then reposts each of today's posts
        as its own DM (full text, links to source and to the live post)."""
        today = datetime.now(timezone.utc).date()
        todays = [e for e in self.posted_log if e[0].date() == today]
        if not todays:
            await event.edit("📰 No new posts today.", buttons=None)
            return
        await event.edit(f"📰 {len(todays)} post{'s' if len(todays) != 1 else ''} today:", buttons=None)
        for _, title, text, source_link, target_link in todays:
            title = title.replace("[", "(").replace("]", ")")
            links = [f"📢 [Posted]({target_link})"] if target_link else []
            links.append(f"🔗 [{title}]({source_link})" if source_link else f"🔗 {title}")
            body = self.fix_formatting(text)
            msg = f"{' · '.join(links)}\n\n{body}"[:MESSAGE_LIMIT]
            await self.bot.send_message(self.cfg.admin_id, msg, link_preview=False)

    async def on_command(self, event) -> None:
        if not event.is_private or event.sender_id != self.cfg.admin_id:
            return
        s = self.stats
        uptime = int(time.time() - s["started"])
        lines = [
            f"🟢 Running for {uptime // 3600}h {uptime % 3600 // 60}m",
            f"Mode: {'review' if self.cfg.review_mode else 'auto-post'}",
            f"Posted: {s['posted']} · Skipped: {s['skipped']} · Failed: {s['failed']}",
            f"In queue: {self.queue.qsize()} · Awaiting review: {len(self.pending)}",
            f"Posted today: {sum(1 for e in self.posted_log if e[0].date() == datetime.now(timezone.utc).date())}",
        ]
        if self.pending:
            drafts = sorted(self.pending.values(), key=lambda p: p.post_date)
            lines.append("")
            lines.append("Awaiting review, oldest first:")
            for post in drafts[:10]:
                lines.append(f"  • {format_age(post.post_date)} — {post.source_title}")
            if len(drafts) > 10:
                lines.append(f"  …and {len(drafts) - 10} more")
        await event.reply("\n".join(lines))


# ----------------------------------------------------------------- main ---

def build_app() -> App:
    setup_logging()
    return App(Config.load())


def main() -> None:
    try:
        app = build_app()
        asyncio.run(app.run())
    except ConfigError as e:
        log.error("Config problem: %s", e)
        sys.exit(1)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
