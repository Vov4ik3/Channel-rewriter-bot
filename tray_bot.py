"""
Runs the bot with a system tray icon and no console window.
Windows:  venv\\Scripts\\pythonw.exe tray_bot.py   (or run_tray.bat)

Tray menu: Open log / Quit. Errors go to bot.log.
"""
import os
import subprocess
import sys
import threading
import asyncio

import pystray
from PIL import Image, ImageDraw

import bot


def make_icon() -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((2, 2, 62, 62), fill=(38, 120, 220, 255))
    # a little "pencil stroke" + lines, for "rewriter"
    for y in (20, 31, 42):
        d.rounded_rectangle((16, y, 48 if y != 42 else 38, y + 5), radius=2, fill="white")
    return img


def open_log() -> None:
    path = str(bot.LOG_PATH)
    if sys.platform.startswith("win"):
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def main() -> None:
    try:
        app = bot.build_app()
    except bot.ConfigError as e:
        bot.log.error("Config problem: %s", e)
        return

    icon = pystray.Icon("channel-rewriter", make_icon(), "Channel Rewriter")

    def run_bot() -> None:
        try:
            asyncio.run(app.run())
        except bot.ConfigError as e:
            bot.log.error("Config problem: %s", e)
        except Exception:
            bot.log.exception("Bot crashed")
        finally:
            icon.stop()  # bot ended on its own -> remove the tray icon too

    thread = threading.Thread(target=run_bot, daemon=True)

    def quit_app(icon_, _item) -> None:
        app.stop_threadsafe()
        thread.join(timeout=10)
        icon_.stop()

    icon.menu = pystray.Menu(
        pystray.MenuItem("Open log", lambda *_: open_log(), default=True),
        pystray.MenuItem("Quit", quit_app),
    )
    thread.start()
    icon.run()


if __name__ == "__main__":
    main()
