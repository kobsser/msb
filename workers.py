import os
import re
import time
import random
import asyncio

from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.handlers import MessageHandler

from config import API_ID, API_HASH, BOT_USER_ID
from database import get_user, get_all_users


# ------------------------------------------------------------------
# Globals
# ------------------------------------------------------------------

WORKERS = {}
STARTING = set()

_GLOBAL_LOOP = None

MEOW_NEXT_RUN = {}
PISHI_NEXT_RUN = {}

FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")

# Matches:
# بعد از 4:30
# باید 4:26 صبر کنی
MEOW_COOLDOWN_RE = re.compile(
    r"(?:بعد از|باید)\s*(?P<time>\d{1,3}(?::\d{1,2}(?::\d{1,2})?)?)"
)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _get_loop(loop=None):
    global _GLOBAL_LOOP

    if loop is not None:
        _GLOBAL_LOOP = loop
        return loop

    if _GLOBAL_LOOP is not None:
        return _GLOBAL_LOOP

    try:
        return asyncio.get_running_loop()
    except:
        return None


def _schedule_coroutine(coro, loop=None):
    target_loop = _get_loop(loop)

    if target_loop is None:
        try:
            target_loop = asyncio.get_running_loop()
        except:
            return

    try:
        running_loop = asyncio.get_running_loop()
        if running_loop == target_loop:
            target_loop.create_task(coro)
            return
    except:
        pass

    asyncio.run_coroutine_threadsafe(coro, target_loop)


def normalize_text(text: str) -> str:
    if not text:
        return ""

    text = text.replace("\u200c", " ")
    text = text.replace("\u200e", "")
    text = text.replace("\u200f", "")
    text = text.translate(FA_DIGITS)

    return text


def get_selected_chat_ids(user):
    chat_ids = []

    for chat_id in user.get("selected_groups", []):
        try:
            chat_ids.append(int(chat_id))
        except:
            continue

    return chat_ids


def parse_meow_cooldown_seconds(text: str):
    """
    Parses raw bot response text.

    Supports:
      - بعد از 4:30
      - باید 4:26 صبر کنی
      - بعد از 1:02:03
      - بعد از 30 دقیقه
      - بعد از 2 ساعت
      - بعد از 45 ثانیه

    Two-part time is treated as minutes:seconds.
    Example:
      4:26 = 4 minutes 26 seconds
    """
    text = normalize_text(text)

    if not text:
        return None

    seconds = 0
    found = False

    patterns = [
        (r"(\d+)\s*(?:days?|روز)", 86400),
        (r"(\d+)\s*(?:hours?|hour|ساعت)", 3600),
        (r"(\d+)\s*(?:minutes?|minute|mins?|min|دقیقه)", 60),
        (r"(\d+)\s*(?:seconds?|second|secs?|sec|ثانیه)", 1),
    ]

    for pattern, multiplier in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            try:
                seconds += int(match.group(1)) * multiplier
                found = True
            except:
                pass

    if found:
        return max(0, seconds)

    match = MEOW_COOLDOWN_RE.search(text)
    if not match:
        return None

    parts = []

    for part in match.group("time").split(":"):
        if part == "":
            continue

        try:
            parts.append(int(part))
        except:
            pass

    if not parts:
        return None

    if len(parts) == 3:
        hours, minutes, secs = parts

    elif len(parts) == 2:
        # minutes:seconds
        hours, minutes, secs = 0, parts[0], parts[1]

    else:
        # Single number: assume minutes
        hours, minutes, secs = 0, parts[0], 0

    return max(0, hours * 3600 + minutes * 60 + secs)


def is_meow_cooldown_text(text: str) -> bool:
    text = normalize_text(text).lower()

    # If it is clearly a Pishi message, do not treat it as Meow cooldown
    if "پیشی" in text:
        return False

    return (
        "میو" in text
        or "میوت" in text
        or "meow" in text
    )


def set_meow_next(phone: str, seconds: int):
    jitter_max = max(1, int(os.getenv("MEOW_JITTER_SECONDS", "10")))
    delay = max(3, int(seconds)) + random.randint(2, jitter_max + 2)
    MEOW_NEXT_RUN[str(phone)] = time.time() + delay


def set_pishi_next(phone: str, seconds: int):
    delay = max(3, int(seconds)) + random.randint(2, 20)
    PISHI_NEXT_RUN[str(phone)] = time.time() + delay


# ------------------------------------------------------------------
# Buttons / rescue
# ------------------------------------------------------------------

async def click_button(message, texts):
    try:
        if not message:
            return False

        reply_markup = getattr(message, "reply_markup", None)
        if not reply_markup:
            return False

        inline_keyboard = getattr(reply_markup, "inline_keyboard", None)
        if not inline_keyboard:
            return False

        for row_index, row in enumerate(inline_keyboard):
            for col_index, button in enumerate(row):
                button_text = getattr(button, "text", "") or ""

                if any(text in button_text for text in texts):
                    try:
                        await message.click(row_index, col_index)
                        return True
                    except Exception as e:
                        print(f"❌ click button error: {e}")
                        return False

        return False

    except Exception as e:
        print(f"❌ click_button error: {e}")
        return False


async def rescue_loop(client, message):
    """
    Clicks rescue-like buttons repeatedly for a short time.
    """
    button_texts = [
        "نجات",
        "دریافت",
        "برداشت",
        "شروع",
        "ادامه",
        "بازی",
        "🐱",
        "🐾",
    ]

    end_time = time.time() + 40

    while time.time() < end_time:
        try:
            msg = await client.get_messages(message.chat.id, message.id)
            clicked = await click_button(msg, button_texts)

            if not clicked:
                break

        except Exception as e:
            print(f"❌ rescue_loop error: {e}")
            break

        await asyncio.sleep(2)


# ------------------------------------------------------------------
# Bot message handler
# ------------------------------------------------------------------

async def handle_bot_message(client, phone: str, message):
    try:
        user = get_user(phone)

        if not user or not user.get("is_active"):
            return

        selected_chat_ids = get_selected_chat_ids(user)

        if message.chat.id not in selected_chat_ids:
            return

        raw_text = message.text or message.caption or ""
        normalized = normalize_text(raw_text)

        print(f"📩 [{phone}] received: {raw_text[:80]}")

        # ------------------------------------------------------
        # Dynamic Meow cooldown
        # ------------------------------------------------------
        if user.get("meow_enabled"):
            cooldown = parse_meow_cooldown_seconds(raw_text)

            if cooldown is not None and is_meow_cooldown_text(normalized):
                set_meow_next(phone, cooldown)
                print(f"⏱ [{phone}] Meow cooldown parsed: {cooldown}s")

        # ------------------------------------------------------
        # Pishi actions
        # ------------------------------------------------------
        pishi_enabled = user.get("fish_enabled", False)

        if pishi_enabled:
            if "نجات پیشی خیابونی" in raw_text:
                asyncio.create_task(rescue_loop(client, message))

            if "پیشی" in normalized or "میو" in normalized:
                await click_button(
                    message,
                    [
                        "برداشت میو پوینت ها",
                        "برداشت",
                    ]
                )

    except Exception as e:
        print(f"❌ handle_bot_message error: {e}")


# ------------------------------------------------------------------
# Dynamic scheduler
# ------------------------------------------------------------------

async def smart_scheduler_loop(client, phone: str):
    while True:
        try:
            worker = WORKERS.get(phone)

            if not worker or not worker.get("running"):
                break

            user = get_user(phone)

            if not user or not user.get("is_active"):
                await asyncio.sleep(15)
                continue

            chat_ids = get_selected_chat_ids(user)

            if not chat_ids:
                await asyncio.sleep(15)
                continue

            now = time.time()

            # ------------------------------------------------------
            # Meow scheduler
            # ------------------------------------------------------
            if user.get("meow_enabled"):
                if now >= MEOW_NEXT_RUN.get(phone, 0):
                    fallback = int(os.getenv("MEOW_FALLBACK_SECONDS", "300"))

                    # Set fallback before sending.
                    # Bot response will overwrite this with the parsed cooldown.
                    MEOW_NEXT_RUN[phone] = now + fallback + random.randint(2, 8)

                    for chat_id in chat_ids:
                        try:
                            await client.send_message(chat_id, "میو")
                            print(f"😺 [{phone}] Meow sent to {chat_id}")

                        except FloodWait as e:
                            wait_seconds = getattr(e, "value", 60)
                            MEOW_NEXT_RUN[phone] = time.time() + wait_seconds + random.randint(5, 20)
                            print(f"⏳ FloodWait Meow [{phone}]: {wait_seconds}s")
                            break

                        except Exception as e:
                            print(f"❌ Meow error [{phone}]: {e}")
                            MEOW_NEXT_RUN[phone] = time.time() + 60

                        await asyncio.sleep(random.uniform(1.0, 3.0))

            # ------------------------------------------------------
            # Pishi scheduler
            # ------------------------------------------------------
            if user.get("fish_enabled", False):
                interval = int(os.getenv("PISHI_INTERVAL_SECONDS", "600"))

                if now >= PISHI_NEXT_RUN.get(phone, 0):
                    PISHI_NEXT_RUN[phone] = now + interval + random.randint(2, 20)

                    for chat_id in chat_ids:
                        try:
                            await client.send_message(chat_id, "پیشی")
                            print(f"🐱 [{phone}] Pishi sent to {chat_id}")

                        except FloodWait as e:
                            wait_seconds = getattr(e, "value", 60)
                            PISHI_NEXT_RUN[phone] = time.time() + wait_seconds + random.randint(5, 20)
                            print(f"⏳ FloodWait Pishi [{phone}]: {wait_seconds}s")
                            break

                        except Exception as e:
                            print(f"❌ Pishi error [{phone}]: {e}")
                            PISHI_NEXT_RUN[phone] = time.time() + 60

                        await asyncio.sleep(random.uniform(1.0, 3.0))

        except Exception as e:
            print(f"❌ scheduler_loop error [{phone}]: {e}")

        await asyncio.sleep(15)


# ------------------------------------------------------------------
# Worker management
# ------------------------------------------------------------------

async def _start_worker(phone: str):
    phone = str(phone)

    if phone in STARTING:
        return

    worker = WORKERS.get(phone)

    if worker and worker.get("running"):
        return

    user = get_user(phone)

    if not user:
        print(f"❌ Worker start failed: user not found {phone}")
        return

    if not user.get("session_string"):
        print(f"❌ Worker start failed: no session {phone}")
        return

    STARTING.add(phone)

    try:
        client = Client(
            name=f"sb_{phone}",
            session_string=user["session_string"],
            api_id=API_ID,
            api_hash=API_HASH,
            in_memory=True
        )

        async def handler(_, message):
            await handle_bot_message(client, phone, message)

        client.add_handler(
            MessageHandler(
                handler,
                filters.user(BOT_USER_ID)
            )
        )

        await client.start()

        scheduler_task = asyncio.create_task(
            smart_scheduler_loop(client, phone)
        )

        WORKERS[phone] = {
            "client": client,
            "tasks": [scheduler_task],
            "running": True
        }

        print(f"✅ Worker started for {phone}")

    except Exception as e:
        print(f"❌ Worker start error [{phone}]: {e}")

    finally:
        STARTING.discard(phone)


async def _stop_worker(phone: str):
    phone = str(phone)

    worker = WORKERS.pop(phone, None)

    if not worker:
        return

    worker["running"] = False

    tasks = worker.get("tasks", [])

    for task in tasks:
        try:
            task.cancel()
        except:
            pass

    try:
        await asyncio.gather(*tasks, return_exceptions=True)
    except:
        pass

    try:
        await worker["client"].stop()
    except:
        pass

    print(f"⏹ Worker stopped for {phone}")


def start_worker(phone: str, loop=None):
    _schedule_coroutine(_start_worker(phone), loop)


def stop_worker(phone: str, loop=None):
    _schedule_coroutine(_stop_worker(phone), loop)


def start_all_active(loop=None):
    global _GLOBAL_LOOP

    if loop is not None:
        _GLOBAL_LOOP = loop

    users = get_all_users()

    for user in users:
        if user.get("is_active"):
            start_worker(user["phone"], loop)