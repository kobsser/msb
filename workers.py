import os
import re
import time
import random
import asyncio

from pyrogram import filters
from pyrogram.errors import FloodWait
from pyrogram.handlers import MessageHandler, EditedMessageHandler

from config import BOT_USER_ID

from database import (
    get_tg_account,
    get_all_tg_accounts,
    update_account_next_run,
    get_setting,
    get_setting_int,
    get_setting_float,
    claim_dynamic_feature,
    claim_interval_feature
)

import session_manager


# ============================================================
# Globals
# ============================================================

WORKERS = {}
STARTING = set()
START_ATTEMPTS = {}

_GLOBAL_LOOP = None

MEOW_TRACKED_MESSAGES = {}
TRACKED_FISHING_MESSAGES = {}

CLICKED_FISHING_MESSAGES = set()
PISHI_CLICKED_MESSAGES = set()
MEOW_CLICKED_MESSAGES = set()

FISHING_CLICK_TASKS = {}
PISHI_CLICK_TASKS = {}
MEOW_CLICK_TASKS = {}

FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")

COOLDOWN_RE = re.compile(
    r"(?:بعد از|باید|تا)\s*(?P<time>\d{1,3}(?:[:：.]\d{1,2}(?:[:：.]\d{1,2})?)?)"
)


# ============================================================
# Loop helpers
# ============================================================

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


# ============================================================
# Safe settings helpers
# ============================================================

def setting_int(key, default):
    try:
        return get_setting_int(key, default)
    except:
        return default


def setting_float(key, default):
    try:
        return get_setting_float(key, default)
    except:
        return default


def setting_str(key, default):
    try:
        value = get_setting(key, default)
        return value if value is not None else default
    except:
        return default


# ============================================================
# Text / cooldown helpers
# ============================================================

def normalize_text(text: str) -> str:
    if not text:
        return ""

    text = text.replace("\u200c", " ")
    text = text.replace("\u200e", "")
    text = text.replace("\u200f", "")
    text = text.replace("：", ":")
    text = text.translate(FA_DIGITS)

    return text


def get_two_part_time_mode():
    mode = str(setting_str("TWO_PART_TIME_MODE", "ms")).lower()

    if mode not in ("ms", "hm"):
        return "ms"

    return mode


def parse_cooldown_seconds(text: str):
    """
    Parses cooldown time from raw bot text.
    """
    text = normalize_text(text)

    if not text:
        return None

    match = COOLDOWN_RE.search(text)

    # If it's a colon/dot time like 4:30 or 1:02:03, parse it first
    if match and re.search(r"[:：.]", match.group("time")):
        parts = []

        for part in re.split(r"[:：.]", match.group("time")):
            if part == "":
                continue

            try:
                parts.append(int(part))
            except:
                pass

        if parts:
            if len(parts) == 3:
                hours, minutes, secs = parts
            elif len(parts) == 2:
                mode = get_two_part_time_mode()

                if mode == "hm":
                    hours, minutes, secs = parts[0], parts[1], 0
                else:
                    hours, minutes, secs = 0, parts[0], parts[1]
            else:
                hours, minutes, secs = 0, parts[0], 0

            return max(0, hours * 3600 + minutes * 60 + secs)

    # Try explicit unit parsing: 2 ساعت، 30 دقیقه، 45 ثانیه
    seconds = 0
    found = False

    patterns = [
        (r"(\d+)\s*(?:days?|روز)", 86400),
        (r"(\d+)\s*(?:hours?|hour|ساعت)", 3600),
        (r"(\d+)\s*(?:minutes?|minute|mins?|min|دقیقه)", 60),
        (r"(\d+)\s*(?:seconds?|second|secs?|sec|ثانیه)", 1),
    ]

    for pattern, multiplier in patterns:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            try:
                seconds += int(m.group(1)) * multiplier
                found = True
            except:
                pass

    if found:
        return max(0, seconds)

    # If only a single number was found, assume minutes
    if match:
        try:
            value = int(match.group("time"))
            return max(0, value * 60)
        except:
            return None

    return None


def schedule_feature(phone: str, feature: str, seconds: int, jitter: int = 10):
    """
    Persists next run timestamp in database as integer Unix time.
    """
    try:
        delay = max(3, int(seconds)) + random.randint(2, max(2, jitter) + 2)
        timestamp = int(time.time()) + delay

        if feature == "meow":
            update_account_next_run(phone, meow_next_run=timestamp)
        elif feature == "pishi":
            update_account_next_run(phone, pishi_next_run=timestamp)
        elif feature == "fishing":
            update_account_next_run(phone, fishing_next_run=timestamp)

        print(f"⏱ [{phone}] {feature} scheduled in {delay}s")

        return timestamp

    except Exception as e:
        print(f"❌ schedule_feature error [{phone}] [{feature}]: {e}")
        return None


def dynamic_action(account, feature: str, now: float):
    """
    Returns:
      send_initial
      send_due
      retry_after_parse_timeout
      waiting
      not_due
    """
    next_run = int(float(account.get(f"{feature}_next_run") or 0))
    now = int(float(now))

    # No saved parsed time yet
    if next_run == 0:
        return "send_initial"

    # Waiting for parsed bot response
    if next_run < 0:
        timeout = setting_int("DYNAMIC_WAIT_TIMEOUT_SECONDS", 0)
        timeout = max(0, int(timeout))

        waiting_since = -next_run

        if timeout > 0 and now - waiting_since > timeout:
            return "retry_after_parse_timeout"

        return "waiting"

    # Parsed time reached
    if now >= next_run:
        return "send_due"

    return "not_due"


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


def _remember_clicked(storage, key, limit=5000):
    storage.add(key)

    if len(storage) > limit:
        storage.clear()


def is_meow_response(phone, message, normalized):
    # Fishing messages should never be treated as Meow
    if "ماهی" in normalized or "ماهیا" in normalized:
        return False

    tracked_meow_id = MEOW_TRACKED_MESSAGES.get(phone, {}).get(message.chat.id)

    # Best case: bot replied directly to our sent "میو"
    if (
        getattr(message, "reply_to_message_id", None)
        and tracked_meow_id
        and message.reply_to_message_id == tracked_meow_id
    ):
        return True

    # Do not treat Pishi messages as Meow unless it replied to our Meow
    if "پیشی" in normalized:
        return False

    return "میو" in normalized or "میوت" in normalized


# ============================================================
# Button helpers
# ============================================================

async def click_inline_button(message, button, row_index, col_index):
    """
    Clicks a button safely.

    Priority:
      1. Click by callback_data
      2. Click by corrected coordinates: x=column, y=row
      3. Fallback coordinates for forks expecting row,column
    """
    callback_data = getattr(button, "callback_data", None)

    if callback_data:
        try:
            await message.click(callback_data=callback_data)
            return True
        except TypeError:
            pass
        except Exception as e:
            print(f"❌ callback click error: {e}")

    try:
        await message.click(col_index, row_index)
        return True
    except Exception as e:
        error_text = str(e).lower()

        if "doesn't exist" in error_text:
            try:
                await message.click(row_index, col_index)
                return True
            except Exception as e2:
                print(f"❌ reversed coordinate click error: {e2}")

        print(f"❌ coordinate click error: {e}")
        return False


async def click_second_button(message):
    """
    Flattens inline keyboard and clicks the 2nd button overall.
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


async def click_meow_claim_buttons(message):
    """
    Clicks Meow claim buttons only.
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
            "برداشت میو",
            "دریافت میو",
            "برداشت",
            "دریافت",
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
        print(f"❌ click_meow_claim_buttons error: {e}")
        return False


# ============================================================
# Delayed click tasks
# ============================================================

async def delayed_click_fishing(client, message, msg_key: str):
    current_task = asyncio.current_task()

    try:
        delay = setting_float("FISHING_CLICK_DELAY", 2.0)
        delay = max(0.0, float(delay))

        await asyncio.sleep(delay)

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
        delay = setting_float("PISHI_CLICK_DELAY", 1.0)
        delay = max(0.0, float(delay))

        await asyncio.sleep(delay)

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


async def delayed_click_meow(client, message, msg_key: str):
    current_task = asyncio.current_task()

    try:
        delay = setting_float("MEOW_CLICK_DELAY", 1.0)
        delay = max(0.0, float(delay))

        await asyncio.sleep(delay)

        if msg_key in MEOW_CLICKED_MESSAGES:
            return

        try:
            fresh_message = await client.get_messages(message.chat.id, message.id)
        except:
            fresh_message = message

        if await click_meow_claim_buttons(fresh_message):
            _remember_clicked(MEOW_CLICKED_MESSAGES, msg_key)
            print(f"🍬 [{msg_key}] Meow claim button clicked once")

    except asyncio.CancelledError:
        return

    except Exception as e:
        print(f"❌ delayed_click_meow error: {e}")

    finally:
        if MEOW_CLICK_TASKS.get(msg_key) is current_task:
            MEOW_CLICK_TASKS.pop(msg_key, None)


# ============================================================
# Bot message handler
# ============================================================

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

        # ============================================================
        # Dynamic cooldown parsing
        # ============================================================
        cooldown = parse_cooldown_seconds(raw_text)
        meow_response = user.get("meow_enabled") and is_meow_response(phone, message, normalized)

        if cooldown is not None:
            if user.get("fishing_enabled") and ("ماهی" in normalized or "ماهیا" in normalized):
                schedule_feature(phone, "fishing", cooldown)

            elif meow_response:
                schedule_feature(phone, "meow", cooldown)

            elif user.get("pishi_enabled") and "پیشی" in normalized and "ماهی" not in normalized:
                schedule_feature(phone, "pishi", cooldown)

        # ============================================================
        # Fishing reply / edited reply
        # ============================================================
        if user.get("fishing_enabled") and getattr(message, "reply_to_message_id", None):
            tracked_fishing_id = TRACKED_FISHING_MESSAGES.get(phone, {}).get(message.chat.id)

            if tracked_fishing_id and message.reply_to_message_id == tracked_fishing_id:
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

        # ============================================================
        # Meow claim button
        # If Pishi is enabled, Pishi handles generic claim clicks.
        # If Pishi is disabled, Meow handles its own claim clicks.
        # ============================================================
        if meow_response and not user.get("pishi_enabled"):
            reply_markup = getattr(message, "reply_markup", None)
            inline_keyboard = getattr(reply_markup, "inline_keyboard", None) if reply_markup else None

            if inline_keyboard:
                meow_msg_key = f"{phone}:{message.chat.id}:{message.id}"

                if meow_msg_key not in MEOW_CLICKED_MESSAGES:
                    existing_task = MEOW_CLICK_TASKS.get(meow_msg_key)

                    if existing_task and not existing_task.done():
                        existing_task.cancel()

                    MEOW_CLICK_TASKS[meow_msg_key] = asyncio.create_task(
                        delayed_click_meow(client, message, meow_msg_key)
                    )

        # ============================================================
        # Pishi claim buttons
        # Click each unique message only once
        # ============================================================
        if user.get("pishi_enabled"):
            is_fishing_message = "ماهی" in normalized or "ماهیا" in normalized

            pishi_target = (
                not is_fishing_message
                and (
                    "نجات پیشی خیابونی" in raw_text
                    or "پیشی" in normalized
                    or "میو" in normalized
                )
            )

            if pishi_target:
                pishi_msg_key = f"{phone}:{message.chat.id}:{message.id}"

                if pishi_msg_key not in PISHI_CLICKED_MESSAGES:
                    existing_task = PISHI_CLICK_TASKS.get(pishi_msg_key)

                    if existing_task and not existing_task.done():
                        existing_task.cancel()

                    PISHI_CLICK_TASKS[pishi_msg_key] = asyncio.create_task(
                        delayed_click_pishi(client, message, pishi_msg_key)
                    )

    except Exception as e:
        print(f"❌ handle_bot_message error: {e}")


# ============================================================
# Scheduler
# ============================================================

def _client_is_connected(client) -> bool:
    try:
        value = getattr(client, "is_connected", False)

        if callable(value):
            return bool(value())

        return bool(value)

    except:
        return False


async def smart_scheduler_loop(client, phone: str):
    while True:
        try:
            worker = WORKERS.get(phone)

            if not worker or not worker.get("running"):
                break

            account = get_tg_account(phone)

            if not account or not account.get("is_active"):
                await asyncio.sleep(15)
                continue

            chat_ids = get_selected_chat_ids(account)

            if not chat_ids:
                await asyncio.sleep(15)
                continue

            try:
                if not _client_is_connected(client):
                    await client.connect()
            except:
                pass

            now = int(time.time())

            # ============================================================
            # Meow — dynamic parsed timing only
            # ============================================================
            if account.get("meow_enabled"):
                action = dynamic_action(account, "meow", now)

                if action in ("send_initial", "send_due", "retry_after_parse_timeout"):
                    timeout = 0

                    if action == "retry_after_parse_timeout":
                        timeout = setting_int("DYNAMIC_WAIT_TIMEOUT_SECONDS", 0)

                    waiting_timestamp = -int(time.time())

                    claimed = claim_dynamic_feature(
                        phone,
                        "meow",
                        action,
                        waiting_timestamp,
                        now,
                        timeout
                    )

                    if not claimed:
                        print(f"⚠️ [{phone}] Meow trigger skipped: already claimed or state changed")
                    else:
                        if action == "send_initial":
                            print(f"😺 [{phone}] No saved Meow time. Sending trigger once.")
                        elif action == "retry_after_parse_timeout":
                            print(f"⚠️ [{phone}] Meow parse timeout. Sending trigger again.")
                        else:
                            print(f"😺 [{phone}] Meow due from parsed bot time.")

                        for chat_id in chat_ids:
                            try:
                                sent_message = await client.send_message(chat_id, "میو")

                                if phone not in MEOW_TRACKED_MESSAGES:
                                    MEOW_TRACKED_MESSAGES[phone] = {}

                                MEOW_TRACKED_MESSAGES[phone][chat_id] = sent_message.id

                                print(f"😺 [{phone}] Meow sent to {chat_id}, tracking message {sent_message.id}")

                            except FloodWait as e:
                                wait_seconds = flood_seconds(e)
                                schedule_feature(phone, "meow", wait_seconds, jitter=20)
                                print(f"⏳ FloodWait Meow [{phone}]: {wait_seconds}s")
                                break

                            except Exception as e:
                                print(f"❌ Meow error [{phone}]: {e}")
                                schedule_feature(phone, "meow", 60, jitter=10)

                            await asyncio.sleep(random.uniform(1.0, 3.0))

            # ============================================================
            # Pishi — interval based
            # ============================================================
            if account.get("pishi_enabled") and now >= int(float(account.get("pishi_next_run") or 0)):
                interval = setting_int("PISHI_INTERVAL_SECONDS", 1800)

                delay = max(60, int(interval)) + random.randint(2, 20)
                scheduled_timestamp = int(time.time()) + delay

                claimed = claim_interval_feature(
                    phone,
                    "pishi",
                    scheduled_timestamp,
                    now
                )

                if not claimed:
                    print(f"⚠️ [{phone}] Pishi trigger skipped: already claimed or state changed")
                else:
                    print(f"🐱 [{phone}] Pishi due. Next run in {delay}s")

                    for chat_id in chat_ids:
                        try:
                            await client.send_message(chat_id, "پیشی")
                            print(f"🐱 [{phone}] Pishi sent to {chat_id}")

                        except FloodWait as e:
                            wait_seconds = flood_seconds(e)
                            schedule_feature(phone, "pishi", wait_seconds, jitter=20)
                            print(f"⏳ FloodWait Pishi [{phone}]: {wait_seconds}s")
                            break

                        except Exception as e:
                            print(f"❌ Pishi error [{phone}]: {e}")
                            schedule_feature(phone, "pishi", 60, jitter=10)

                        await asyncio.sleep(random.uniform(1.0, 3.0))

            # ============================================================
            # Fishing — dynamic parsed timing only
            # ============================================================
            if account.get("fishing_enabled"):
                action = dynamic_action(account, "fishing", now)

                if action in ("send_initial", "send_due", "retry_after_parse_timeout"):
                    timeout = 0

                    if action == "retry_after_parse_timeout":
                        timeout = setting_int("DYNAMIC_WAIT_TIMEOUT_SECONDS", 0)

                    waiting_timestamp = -int(time.time())

                    claimed = claim_dynamic_feature(
                        phone,
                        "fishing",
                        action,
                        waiting_timestamp,
                        now,
                        timeout
                    )

                    if not claimed:
                        print(f"⚠️ [{phone}] Fishing trigger skipped: already claimed or state changed")
                    else:
                        if action == "send_initial":
                            print(f"🎣 [{phone}] No saved Fishing time. Sending trigger once.")
                        elif action == "retry_after_parse_timeout":
                            print(f"⚠️ [{phone}] Fishing parse timeout. Sending trigger again.")
                        else:
                            print(f"🎣 [{phone}] Fishing due from parsed bot time.")

                        for chat_id in chat_ids:
                            try:
                                sent_message = await client.send_message(chat_id, "ماهی")

                                if phone not in TRACKED_FISHING_MESSAGES:
                                    TRACKED_FISHING_MESSAGES[phone] = {}

                                TRACKED_FISHING_MESSAGES[phone][chat_id] = sent_message.id

                                print(f"🎣 [{phone}] Fishing sent to {chat_id}, tracking message {sent_message.id}")

                            except FloodWait as e:
                                wait_seconds = flood_seconds(e)
                                schedule_feature(phone, "fishing", wait_seconds, jitter=20)
                                print(f"⏳ FloodWait Fishing [{phone}]: {wait_seconds}s")
                                break

                            except Exception as e:
                                print(f"❌ Fishing error [{phone}]: {e}")
                                schedule_feature(phone, "fishing", 60, jitter=10)

                            await asyncio.sleep(random.uniform(1.0, 3.0))

        except Exception as e:
            print(f"❌ scheduler_loop error [{phone}]: {e}")

        await asyncio.sleep(15)


# ============================================================
# Worker management
# ============================================================

def _cancel_phone_click_tasks(phone: str):
    for task_dict in (FISHING_CLICK_TASKS, PISHI_CLICK_TASKS, MEOW_CLICK_TASKS):
        for key, task in list(task_dict.items()):
            if key.startswith(phone + ":"):
                try:
                    task.cancel()
                except:
                    pass


async def _start_worker(phone: str):
    phone = str(phone)

    if phone in STARTING:
        return

    worker = WORKERS.get(phone)

    if worker and worker.get("running"):
        return

    account = get_tg_account(phone)

    if not account:
        print(f"❌ Worker start failed: account not found {phone}")
        return

    if not account.get("session_string"):
        print(f"❌ Worker start failed: no session {phone}")
        return

    if not account.get("is_active"):
        print(f"⚠️ Worker not started because account is inactive: {phone}")
        return

    STARTING.add(phone)
    acquired = False

    try:
        async def setup(client):
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

        client = await session_manager.get_client(phone, setup=setup)
        acquired = True

        scheduler_task = asyncio.create_task(
            smart_scheduler_loop(client, phone)
        )

        WORKERS[phone] = {
            "client": client,
            "tasks": [scheduler_task],
            "running": True
        }

        START_ATTEMPTS.pop(phone, None)

        print(f"✅ Worker started for {phone}")

    except Exception as e:
        error_text = str(e)

        print(f"❌ Worker start error [{phone}]: {error_text}")

        if acquired:
            try:
                await session_manager.release_client(phone)
            except:
                pass

        if "AUTH_KEY_DUPLICATED" in error_text.upper() and START_ATTEMPTS.get(phone, 0) < 2:
            START_ATTEMPTS[phone] = START_ATTEMPTS.get(phone, 0) + 1

            retry_delay = 30 + (START_ATTEMPTS[phone] * 10)

            print(f"⚠️ AUTH_KEY_DUPLICATED for {phone}. Retrying in {retry_delay}s...")

            async def retry_later():
                await asyncio.sleep(retry_delay)
                await _start_worker(phone)

            asyncio.create_task(retry_later())

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
        await session_manager.release_client(phone)
    except Exception as e:
        print(f"❌ release_client error [{phone}]: {e}")

    _cancel_phone_click_tasks(phone)

    print(f"⏹ Worker stopped for {phone}")


def start_worker(phone: str, loop=None):
    _schedule_coroutine(_start_worker(phone), loop)


def stop_worker(phone: str, loop=None):
    _schedule_coroutine(_stop_worker(phone), loop)


async def _start_all_active_delayed(delay=None):
    if delay is None:
        delay = setting_int("STARTUP_DELAY", 10)

    delay = max(0, int(delay))

    if delay > 0:
        print(f"⏳ Waiting {delay} seconds before starting active workers...")
        await asyncio.sleep(delay)

    START_ATTEMPTS.clear()

    accounts = get_all_tg_accounts()
    active_accounts = [account for account in accounts if account.get("is_active")]

    print(f"🚀 Starting {len(active_accounts)} active worker(s)...")

    interval = setting_int("ACCOUNT_START_INTERVAL", 3)
    interval = max(0, int(interval))

    for index, account in enumerate(active_accounts):
        try:
            await _start_worker(account["phone"])
        except Exception as e:
            print(f"❌ start_all_active error [{account.get('phone')}]: {e}")

        if index < len(active_accounts) - 1:
            await asyncio.sleep(interval)


def start_all_active(loop=None, delay=None):
    _schedule_coroutine(_start_all_active_delayed(delay), loop)


async def _stop_all_workers():
    for phone in list(WORKERS.keys()):
        try:
            await _stop_worker(phone)
        except Exception as e:
            print(f"❌ stop_all_workers error: {e}")


async def _shutdown_sequence():
    await _stop_all_workers()

    try:
        await session_manager.stop_all_clients()
    except Exception as e:
        print(f"❌ stop_all_clients error: {e}")


def shutdown_all_workers(*args):
    loop = _get_loop()

    if not loop:
        return

    try:
        running_loop = asyncio.get_running_loop()

        if running_loop == loop:
            loop.create_task(_shutdown_sequence())
            return

    except:
        pass

    try:
        future = asyncio.run_coroutine_threadsafe(_shutdown_sequence(), loop)
        future.result(timeout=20)
    except Exception as e:
        print(f"❌ shutdown_all_workers error: {e}")