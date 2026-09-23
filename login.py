"""
One-time login for the reader account (the Telegram account that reads the
source channels). Asks for the phone number and the code Telegram sends, then
saves reader.session next to this file. After that bot.py runs without asking.

Run it again only if you log that session out, or delete reader.session.
"""
import asyncio

from dotenv import load_dotenv
from telethon import TelegramClient

import bot


async def main() -> None:
    load_dotenv(bot.BASE_DIR / ".env")
    api_id = int(bot._env("TG_API_ID", required=True))
    api_hash = bot._env("TG_API_HASH", required=True)

    client = TelegramClient(bot.USER_SESSION, api_id, api_hash)
    await client.start()  # interactive: phone, code, 2FA password if set
    me = await client.get_me()
    print(f"\nLogged in as {me.first_name} (id {me.id}). Session saved to reader.session.")
    print("Keep that file private: it works like a password for this account.\n")

    print("Channels this account is subscribed to (use @username or the id in SOURCE_CHANNELS):")
    async for dialog in client.iter_dialogs():
        if dialog.is_channel and not dialog.is_group:
            username = getattr(dialog.entity, "username", None)
            ref = f"@{username}" if username else str(dialog.id)
            print(f"  {ref:<32} {dialog.name}")
    print("\nGroups (for TARGET_CHAT):")
    async for dialog in client.iter_dialogs():
        if dialog.is_group:
            print(f"  {dialog.id:<32} {dialog.name}")
    await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except bot.ConfigError as e:
        print(f"Config problem: {e}")
