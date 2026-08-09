import os
import re
import time
import random
import asyncio

from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.handlers import MessageHandler, EditedMessageHandler

from config import API_ID, API_HASH, BOT_USER_ID
from database import get_tg_account, get_all_tg_accounts


WORKERS = {}
STARTING = set()

_GLOBAL_LOOP = None

MEOW_NEXT_RUN = {}
PISHI_NEXT_RUN = {}
FISHING_NEXT_RUN = {}

TRACKED_FISHING_MESSAGES = {}
CLICKED_FISHING_MESSAGES = set()
FISHING_CLICK_TASKS = {}

PISHI_CLICKED_MESSAGES = set()
PISHI_CLICK_TASKS = {}

try:
    PISHI_CLICK_DELAY = float(os.getenv("PISHI_CLICK_DELAY", "1.0"))
except:
    PISHI_CLICK_DELAY = 1.0

FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")

COOLDOWN_RE = re.compile(
    r"(?:بعد از|باید)\s*(?P<time>\d{1,3}(?::\d{1,2}(?::\d{1,2})?)?)"
)

try:
    FISHING_CLICK_DELAY = float(os.getenv("FISHING_CLICK_DELAY", "2.0"))
except:
    FISHING_CLICK_DELAY = 2.0


# -------------------------
# Loop helpers
# -------------------------

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
            target_loop = asyncio.get_event_loop()
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


# -------------------------
# Text / cooldown helpers
# -------------------------

def _remember_clicked(storage, key, limit=5000):
    storage.add(key)

    if len(storage) > limit:
        storage.clear()


def normalize_text(text: str) -> str:
    if not text:
        return ""

    text = text.replace("\u200c", " ")
    text = text.replace("\u200e", "")
    text = text.replace("\u200f", "")
    text = text.translate(FA_DIGITS)

    return text


def parse_cooldown_seconds(text: str):
    """
    Parses:
      بعد از 4:30
      باید 59:02 صبر کنی
      1:02:03
      30 دقیقه
      2 ساعت
      45 ثانیه

    Two-part time is minutes:seconds.
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

    match = COOLDOWN_RE.search(text)
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
        # single number: assume minutes
        hours, minutes, secs = 0, parts[0], 0

    return max(0, hours * 3600 + minutes * 60 + secs)


def set_next_run(storage, phone: str, seconds: int, jitter: int = 10):
    storage[str(phone)] = time.time() + max(3, int(seconds)) + random.randint(2, max(2, jitter) + 2)


def get_selected_chat_ids(user):
    chat_ids = []

    for chat_id in user.get("selected_groups", []):
        try:
            chat_ids.append(int(chat_id))
        except:
            continue

    return chat_ids


def flood_seconds(e):
    for attr in ("value", "x"):
        value = getattr(e, attr, None)
        if value:
            try:
                return int(value)
            except:
                pass

    return 60


# -------------------------
# Buttons
# -------------------------

async def click_inline_button(message, button, row_index, col_index):
    """
    Clicks a button safely.

    Priority:
      1. Click by callback_data
      2. Click by corrected coordinates
      3. Fallback coordinates for forks
    """
    callback_data = getattr(button, "callback_data", None)

    if callback_data:
        try:
            await message.click(callback_data=callback_data)
            return True
        except TypeError:
            # Older fork/version may not support callback_data keyword
            pass
        except Exception as e:
            print(f"❌ callback click error: {e}")

    # Pyrogram/Kurigram normally expects x=column, y=row
    try:
        await message.click(col_index, row_index)
        return True
    except Exception as e:
        error_text = str(e).lower()

        if "doesn't exist" in error_text:
            try:
                # Fallback for forks expecting row,column
                await message.click(row_index, col_index)
                return True
            except Exception as e2:
                print(f"❌ reversed coordinate click error: {e2}")

        print(f"❌ coordinate click error: {e}")
        return False


async def click_second_button(message):
    """
    Flattens inline keyboard and clicks the 2nd button overall.
    Each button is clicked only by the caller using message-ID tracking.
    """
    try:
        reply_markup = getattr(message, "reply_markup", None)
        if not reply_markup:
            return False

        rows = getattr(reply_markup, "inline_keyboard", None)
        if not rows:
            return False

        buttons = []

        for row_index, row in enumerate(rows):
            for col_index, button in enumerate(row):
                buttons.append((row_index, col_index, button))

        if len(buttons) < 2:
            return False

        target_row, target_col, target_button = buttons[1]

        return await click_inline_button(
            message,
            target_button,
            target_row,
            target_col
        )

    except Exception as e:
        print(f"❌ click_second_button error: {e}")
        return False


async def click_claim_buttons(message):
    """
    Clicks Pishi claim/target buttons.
    Each message should be tracked by caller so it is clicked only once.
    """
    try:
        reply_markup = getattr(message, "reply_markup", None)
        if not reply_markup:
            return False

        rows = getattr(reply_markup, "inline_keyboard", None)
        if not rows:
            return False

        target_texts = [
            "برداشت میو پوینت ها",
            "برداشت",
            "دریافت",
            "نجات",
            "شروع",
            "ادامه",
        ]

        for row_index, row in enumerate(rows):
            for col_index, button in enumerate(row):
                button_text = getattr(button, "text", "") or ""

                if any(text in button_text for text in target_texts):
                    return await click_inline_button(
                        message,
                        button,
                        row_index,
                        col_index
                    )

        return False

    except Exception as e:
        print(f"❌ click_claim_buttons error: {e}")
        return False


async def delayed_click_fishing(client, message, msg_key: str):
    current_task = asyncio.current_task()

    try:
        await asyncio.sleep(FISHING_CLICK_DELAY)

        if msg_key in CLICKED_FISHING_MESSAGES:
            return

        try:
            fresh_message = await client.get_messages(message.chat.id, message.id)
        except:
            fresh_message = message

        if await click_second_button(fresh_message):
            _remember_clicked(CLICKED_FISHING_MESSAGES, msg_key)
            print(f"🎣 [{msg_key}] Clicked 2nd fishing button")

    except asyncio.CancelledError:
        return

    except Exception as e:
        print(f"❌ delayed_click_fishing error: {e}")

    finally:
        if FISHING_CLICK_TASKS.get(msg_key) is current_task:
            FISHING_CLICK_TASKS.pop(msg_key, None)


async def delayed_click_pishi(client, message, msg_key: str):
    current_task = asyncio.current_task()

    try:
        await asyncio.sleep(PISHI_CLICK_DELAY)

        if msg_key in PISHI_CLICKED_MESSAGES:
            return

        try:
            fresh_message = await client.get_messages(message.chat.id, message.id)
        except:
            fresh_message = message

        if await click_claim_buttons(fresh_message):
            _remember_clicked(PISHI_CLICKED_MESSAGES, msg_key)
            print(f"🐱 [{msg_key}] Pishi button clicked once")

    except asyncio.CancelledError:
        return

    except Exception as e:
        print(f"❌ delayed_click_pishi error: {e}")

    finally:
        if PISHI_CLICK_TASKS.get(msg_key) is current_task:
            PISHI_CLICK_TASKS.pop(msg_key, None)


async def rescue_loop(client, message):
    end_time = time.time() + 40

    while time.time() < end_time:
        try:
            fresh_message = await client.get_messages(message.chat.id, message.id)

            if not fresh_message:
                break

            clicked = await click_claim_buttons(fresh_message)

            if not clicked:
                break

        except Exception as e:
            print(f"❌ rescue_loop error: {e}")
            break

        await asyncio.sleep(2)


# -------------------------
# Bot message handler
# -------------------------

async def handle_bot_message(client, phone: str, message):
    try:
        user = get_tg_account(phone)

        if not user or not user.get("is_active"):
            return

        selected_chat_ids = get_selected_chat_ids(user)

        if message.chat.id not in selected_chat_ids:
            return

        raw_text = message.text or message.caption or ""
        normalized = normalize_text(raw_text).lower()

        # -------------------------
        # Dynamic cooldown parsing
        # -------------------------
        cooldown = parse_cooldown_seconds(raw_text)

        if cooldown is not None:
            if user.get("fishing_enabled") and ("ماهی" in normalized or "ماهیا" in normalized):
                set_next_run(FISHING_NEXT_RUN, phone, cooldown)
                print(f"⏱ [{phone}] Fishing cooldown parsed: {cooldown}s")

            elif user.get("meow_enabled") and ("میو" in normalized or "میوت" in normalized) and "پیشی" not in normalized:
                set_next_run(MEOW_NEXT_RUN, phone, cooldown)
                print(f"⏱ [{phone}] Meow cooldown parsed: {cooldown}s")

            elif user.get("pishi_enabled") and "پیشی" in normalized and "ماهی" not in normalized:
                set_next_run(PISHI_NEXT_RUN, phone, cooldown)
                print(f"⏱ [{phone}] Pishi cooldown parsed: {cooldown}s")

        # -------------------------
        # Fishing reply / edited reply
        # -------------------------
        if user.get("fishing_enabled") and getattr(message, "reply_to_message_id", None):
            tracked_message_id = TRACKED_FISHING_MESSAGES.get(phone, {}).get(message.chat.id)

            if tracked_message_id and message.reply_to_message_id == tracked_message_id:
                reply_markup = getattr(message, "reply_markup", None)
                inline_keyboard = getattr(reply_markup, "inline_keyboard", None) if reply_markup else None

                if inline_keyboard:
                    msg_key = f"{phone}:{message.chat.id}:{message.id}"

                    if msg_key not in CLICKED_FISHING_MESSAGES:
                        existing_task = FISHING_CLICK_TASKS.get(msg_key)

                        if existing_task and not existing_task.done():
                            existing_task.cancel()

                        FISHING_CLICK_TASKS[msg_key] = asyncio.create_task(
                            delayed_click_fishing(client, message, msg_key)
                        )

        # -------------------------
        # Pishi rescue / claim buttons
        # Click each unique message only once
        # -------------------------
        if user.get("pishi_enabled"):
            pishi_msg_key = f"{phone}:{message.chat.id}:{message.id}"

            pishi_target = (
                "نجات پیشی خیابونی" in raw_text
                or "پیشی" in normalized
                or "میو" in normalized
            )

            if pishi_target and pishi_msg_key not in PISHI_CLICKED_MESSAGES:
                existing_task = PISHI_CLICK_TASKS.get(pishi_msg_key)

                if existing_task and not existing_task.done():
                    existing_task.cancel()

                PISHI_CLICK_TASKS[pishi_msg_key] = asyncio.create_task(
                    delayed_click_pishi(client, message, pishi_msg_key)
                )

    except Exception as e:
        print(f"❌ handle_bot_message error: {e}")


# -------------------------
# Scheduler
# -------------------------

async def smart_scheduler_loop(client, phone: str):
    while True:
        try:
            worker = WORKERS.get(phone)

            if not worker or not worker.get("running"):
                break

            user = get_tg_account(phone)

            if not user or not user.get("is_active"):
                await asyncio.sleep(15)
                continue

            chat_ids = get_selected_chat_ids(user)

            if not chat_ids:
                await asyncio.sleep(15)
                continue

            now = time.time()

            # -------------------------
            # Meow
            # -------------------------
            if user.get("meow_enabled") and now >= MEOW_NEXT_RUN.get(phone, 0):
                fallback = int(os.getenv("MEOW_FALLBACK_SECONDS", "300"))

                MEOW_NEXT_RUN[phone] = now + fallback + random.randint(2, 8)

                for chat_id in chat_ids:
                    try:
                        await client.send_message(chat_id, "میو")
                        print(f"😺 [{phone}] Meow sent to {chat_id}")

                    except FloodWait as e:
                        wait_seconds = flood_seconds(e)
                        MEOW_NEXT_RUN[phone] = time.time() + wait_seconds + random.randint(5, 20)
                        print(f"⏳ FloodWait Meow [{phone}]: {wait_seconds}s")
                        break

                    except Exception as e:
                        print(f"❌ Meow error [{phone}]: {e}")
                        MEOW_NEXT_RUN[phone] = time.time() + 60

                    await asyncio.sleep(random.uniform(1.0, 3.0))

            # -------------------------
            # Pishi
            # -------------------------
            if user.get("pishi_enabled") and now >= PISHI_NEXT_RUN.get(phone, 0):
                interval = int(os.getenv("PISHI_INTERVAL_SECONDS", "600"))

                PISHI_NEXT_RUN[phone] = now + interval + random.randint(2, 20)

                for chat_id in chat_ids:
                    try:
                        await client.send_message(chat_id, "پیشی")
                        print(f"🐱 [{phone}] Pishi sent to {chat_id}")

                    except FloodWait as e:
                        wait_seconds = flood_seconds(e)
                        PISHI_NEXT_RUN[phone] = time.time() + wait_seconds + random.randint(5, 20)
                        print(f"⏳ FloodWait Pishi [{phone}]: {wait_seconds}s")
                        break

                    except Exception as e:
                        print(f"❌ Pishi error [{phone}]: {e}")
                        PISHI_NEXT_RUN[phone] = time.time() + 60

                    await asyncio.sleep(random.uniform(1.0, 3.0))

            # -------------------------
            # Fishing
            # -------------------------
            if user.get("fishing_enabled") and now >= FISHING_NEXT_RUN.get(phone, 0):
                interval = int(os.getenv("FISHING_INTERVAL_SECONDS", "600"))

                FISHING_NEXT_RUN[phone] = now + interval + random.randint(2, 20)

                for chat_id in chat_ids:
                    try:
                        sent_message = await client.send_message(chat_id, "ماهی")

                        if phone not in TRACKED_FISHING_MESSAGES:
                            TRACKED_FISHING_MESSAGES[phone] = {}

                        TRACKED_FISHING_MESSAGES[phone][chat_id] = sent_message.id

                        print(f"🎣 [{phone}] Fishing sent to {chat_id}, tracking message {sent_message.id}")

                    except FloodWait as e:
                        wait_seconds = flood_seconds(e)
                        FISHING_NEXT_RUN[phone] = time.time() + wait_seconds + random.randint(5, 20)
                        print(f"⏳ FloodWait Fishing [{phone}]: {wait_seconds}s")
                        break

                    except Exception as e:
                        print(f"❌ Fishing error [{phone}]: {e}")
                        FISHING_NEXT_RUN[phone] = time.time() + 60

                    await asyncio.sleep(random.uniform(1.0, 3.0))

        except Exception as e:
            print(f"❌ scheduler_loop error [{phone}]: {e}")

        await asyncio.sleep(15)


# -------------------------
# Worker management
# -------------------------

async def _start_worker(phone: str):
    phone = str(phone)

    if phone in STARTING:
        return

    worker = WORKERS.get(phone)

    if worker and worker.get("running"):
        return

    user = get_tg_account(phone)

    if not user:
        print(f"❌ Worker start failed: user not found {phone}")
        return

    if not user.get("session_string"):
        print(f"❌ Worker start failed: no session {phone}")
        return

    STARTING.add(phone)

    try:
        safe_name = "sb_" + re.sub(r"\W+", "_", phone)

        client = Client(
            name=safe_name,
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

        client.add_handler(
            EditedMessageHandler(
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

    accounts = get_all_tg_accounts()

    for account in accounts:
        if account.get("is_active"):
            start_worker(account["phone"], loop)