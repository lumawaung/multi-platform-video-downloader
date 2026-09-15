"""
One-time login for the uploader Telegram account.

Run it on the server as the bot's user (not root):

    cd /opt/video-downloader
    sudo -u videobot venv/bin/python setup_session.py

It asks for API_ID and API_HASH if .env doesn't have them yet, logs the
account in (phone number, login code, and the 2FA password if the account
has one), lists the account's channels so you can pick the relay channel,
and saves your choices to .env. The session is saved as
uploader.session; the bot reuses it without logging in again.
"""

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

if hasattr(os, "geteuid") and os.geteuid() == 0:
    sys.exit(
        "Don't run this as root: the bot's user couldn't use the session file.\n"
        "Run: sudo -u videobot /opt/video-downloader/venv/bin/python /opt/video-downloader/setup_session.py"
    )


def _bot_service_running() -> bool:
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", "video-bot"], timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# A running bot would use (and could spoil) the session file while you log in
if _bot_service_running():
    sys.exit("The bot is running. Stop it first, then run this again:\n  sudo systemctl stop video-bot")

from dotenv import load_dotenv  # noqa: E402
from pyrogram import Client  # noqa: E402
from pyrogram.enums import ChatType  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"


def set_env(key: str, value: str) -> None:
    """Set KEY=value in .env, replacing an existing line or adding one."""
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i, line in enumerate(lines):
        if pattern.match(line):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(ENV_PATH, 0o600)


def ask(prompt: str, valid) -> str:
    while True:
        value = input(prompt).strip()
        if valid(value):
            return value
        print("  That doesn't look right, please try again.")


async def main() -> None:
    load_dotenv(ENV_PATH)
    print("=== Multi-Platform Video Downloader — uploader account login ===\n")

    api_id = os.environ.get("API_ID", "").strip()
    api_hash = os.environ.get("API_HASH", "").strip()
    if not api_id or not api_hash:
        print("Get API_ID and API_HASH at https://my.telegram.org → 'API development tools',")
        print("logged in with the account that will upload files.\n")
        api_id = ask("API_ID: ", str.isdigit)
        api_hash = ask("API_HASH: ", lambda v: re.fullmatch(r"[0-9a-fA-F]{32}", v) is not None)
        set_env("API_ID", api_id)
        set_env("API_HASH", api_hash)
        print("Saved API_ID and API_HASH to .env\n")

    print("Telegram will ask for the account's phone number (with country code, e.g. +959…),")
    print("then the login code it sends to your Telegram app, then the 2FA password if you set one.\n")

    async with Client("uploader", api_id=int(api_id), api_hash=api_hash, workdir=str(BASE_DIR)) as app:
        me = await app.get_me()
        premium = " — has Telegram Premium: uploads up to 4000 MiB" if me.is_premium else ""
        print(f"\n✅ Logged in as {me.first_name} (ID {me.id}){premium}")

        channels = []
        async for dialog in app.get_dialogs():
            if dialog.chat.type == ChatType.CHANNEL:
                channels.append(dialog.chat)

        current = os.environ.get("RELAY_CHANNEL_ID", "").strip()
        if channels:
            print("\nChannels this account is in:")
            for number, chat in enumerate(channels, 1):
                notes = (" (owner)" if chat.is_creator else "") + ("  ← current relay channel" if str(chat.id) == current else "")
                print(f"  {number}. {chat.title}   [{chat.id}]{notes}")
            picked = input("\nNumber of the relay channel (press Enter to keep the current setting): ").strip()
            if picked.isdigit() and 1 <= int(picked) <= len(channels):
                chat = channels[int(picked) - 1]
                current = str(chat.id)
                set_env("RELAY_CHANNEL_ID", current)
                print(f"Saved RELAY_CHANNEL_ID={current} ({chat.title}) to .env")
        else:
            print("\nThis account isn't in any channel yet: create a private channel, add the bot")
            print("as an admin, then run this script again to pick it.")

        if current:
            try:
                chat = await app.get_chat(int(current))
                print(f"✅ Relay channel reachable: {chat.title}")
            except Exception as e:
                print(f"⚠️  This account can't open the relay channel {current}: {e}")

    print(f"\nSession saved to: {BASE_DIR / 'uploader.session'}")
    print("Make sure the bot is an admin of the relay channel (Post and Delete messages),")
    print("then start it: sudo systemctl enable --now video-bot")


if __name__ == "__main__":
    asyncio.run(main())
