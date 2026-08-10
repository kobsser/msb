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
    get_tg_accounts_for_user,
    get_web_user_by_id,
    update_account_next_run,
    update_account_meta,
    update_account_profile,
    get_setting,
    get_setting_int,
    get_setting_float,
    claim_dynamic_feature,
    claim_interval_feature,
    update_fishing_status_check_at,
    claim_fishing_status_check,
    claim_fishing_periodic_check
)

import session_manager
import optimizations


# ============================================================
# Globals
# ============================================================

WORKERS = {}
STARTING = set()
START_ATTEMPTS = {}

_GLOBAL_LOOP = None

MEOW_TRACKED_MESSAGES = {}
TRACKED_FISHING_MESSAGES = {}
BACKUP_TRACKED_MESSAGES = {}

CLICKED_FISHING_MESSAGES = set()
PISHI_CLICKED_MESSAGES = set()
MEOW_CLICKED_MESSAGES = set()

FISHING_CLICK_TASKS = {}
PISHI_CLICK_TASKS = {}
MEOW_CLICK_TASKS = {}

ACCOUNT_SELF_IDS = {}
REPLY_OWNER_CACHE = {}

FA_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")

COOLDOWN_RE = re.compile(
    r"(?:بعد از|باید|تا)\s*(?P<time>\d{1,3}(?:[:：.]\d{1,2}(?:[:：.]\d{1,2})?)?)"
)

# Profile (میوهام) parsing
PROFILE_NAME_RE = re.compile(r"کاربر\s*[:：]\s*([^\n]+)")
PROFILE_BALANCE_RE = re.compile(r"میو\s*پوینت\s*ها\s*[:：]\s*([\d,]+)")
PROFILE_MEOW_RE = re.compile(r"میو\s*میو\s*ها\s*[:：]\s*([\d,]+)")
PROFILE_CATS_RE = re.compile(r"پیشی\s*های\s*خیابونی\s*[:：]\s*([\d,]+)")
PROFILE_RANK_RE = re.compile(r"رتبه\s*\(\s*([\d,]+)\s*\)")
PROFILE_LEVEL_RE = re.compile(r"سطح\s*[:：]\s*(\d+)\s*\|\s*([\d,]+)\s*/\s*([\d,]+)")


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
    text = normalize_text(text)

    if not text:
        return None

    match = COOLDOWN_RE.search(text)

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

    if match:
        try:
            value = int(match.group("time"))
            return max(0, value * 60)
        except:
            return None

    return None


def schedule_feature(phone: str, feature: str, seconds: int, jitter: int = 10):
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
    next_run = int(float(account.get(f"{feature}_next_run") or 0))
    now = int(float(now))

    if next_run == 0:
        return "send_initial"

    if next_run < 0:
        timeout = setting_int("DYNAMIC_WAIT_TIMEOUT_SECONDS", 0)
        timeout = max(0, int(timeout))

        waiting_since = -next_run

        if timeout > 0 and now - waiting_since > timeout:
            return "retry_after_parse_timeout"

        return "waiting"

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
    if "ماهی" in normalized or "ماهیا" in normalized:
        return False

    tracked_meow_id = MEOW_TRACKED_MESSAGES.get(phone, {}).get(message.chat.id)

    if (
        getattr(message, "reply_to_message_id", None)
        and tracked_meow_id
        and message.reply_to_message_id == tracked_meow_id
    ):
        return True

    if "پیشی" in normalized:
        return False

    return "میو" in normalized or "میوت" in normalized


def schedule_fishing_status_check(phone: str):
    try:
        delay = setting_int("FISHING_STATUS_CHECK_DELAY", 300)
        delay = max(0, int(delay))

        timestamp = int(time.time()) + delay

        update_fishing_status_check_at(phone, timestamp)

        print(f"🎣 [{phone}] Fishing status check scheduled in {delay}s")

    except Exception as e:
        print(f"❌ schedule_fishing_status_check error [{phone}]: {e}")


# ============================================================
# Backup-group tracking (میوهام / انتقال)
# ============================================================

def _track_backup_message(phone, chat_id, message_id):
    phone_map = BACKUP_TRACKED_MESSAGES.setdefault(phone, {})
    ids = phone_map.setdefault(chat_id, set())
    ids.add(message_id)

    if len(ids) > 200:
        ids.clear()
        ids.add(message_id)


def _int_clean(value):
    try:
        return int(str(value).replace(",", "").strip())
    except Exception:
        return None


def parse_profile_text(text):
    """
    Parses the bot profile response (reply to میوهام).
    Works with both text and caption messages.
    """
    if not text:
        return None

    t = normalize_text(text)

    if "میو پوینت" not in t:
        return None

    profile = {}

    m = PROFILE_NAME_RE.search(t)
    if m:
        name = m.group(1).strip()
        if name:
            profile["account_name"] = name

    m = PROFILE_BALANCE_RE.search(t)
    if m:
        profile["balance"] = _int_clean(m.group(1)) or 0

    m = PROFILE_MEOW_RE.search(t)
    if m:
        profile["meow_count"] = _int_clean(m.group(1)) or 0

    m = PROFILE_CATS_RE.search(t)
    if m:
        profile["street_cats"] = _int_clean(m.group(1)) or 0

    ranks = PROFILE_RANK_RE.findall(t)

    if len(ranks) >= 1:
        profile["balance_rank"] = _int_clean(ranks[0]) or 0
    if len(ranks) >= 2:
        profile["meow_rank"] = _int_clean(ranks[1]) or 0
    if len(ranks) >= 3:
        profile["street_cats_rank"] = _int_clean(ranks[2]) or 0

    m = PROFILE_LEVEL_RE.search(t)
    if m:
        profile["level"] = _int_clean(m.group(1)) or 0
        profile["level_progress"] = f"{m.group(2).replace(',', '')} / {m.group(3).replace(',', '')}"

    return profile


async def _wait_for_bot_reply(client, chat_id, sent_id, timeout=20):
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    async def _handler(_, message):
        try:
            if getattr(message, "reply_to_message_id", None) == sent_id and not future.done():
                future.set_result(message)
        except Exception:
            pass

    handler = MessageHandler(
        _handler,
        filters.chat(chat_id) & filters.user(BOT_USER_ID)
    )

    client.add_handler(handler, group=-10)

    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        try:
            client.remove_handler(handler, group=-10)
        except Exception:
            pass


async def _ensure_in_backup(client, phone, chat_id):
    try:
        await client.get_chat_member(chat_id, "me")
        update_account_meta(phone, in_backup_group=1)
        return True
    except Exception:
        update_account_meta(phone, in_backup_group=0)
        return False


async def _job_name_and_backup(client, phone, backup_group_id):
    result = {"name": None, "in_backup": None}

    try:
        me = await client.get_me()
        name = (getattr(me, "first_name", None) or getattr(me, "username", None) or "").strip()
        if name:
            result["name"] = name
    except Exception as e:
        print(f"❌ get_me error [{phone}]: {e}")

    if backup_group_id:
        try:
            await client.get_chat_member(int(backup_group_id), "me")
            result["in_backup"] = 1
        except Exception:
            result["in_backup"] = 0

    return result


async def _job_fetch_profile(client, phone, backup_group_id):
    chat_id = int(backup_group_id)

    if not await _ensure_in_backup(client, phone, chat_id):
        print(f"⚠️ [{phone}] Not in backup group, profile skipped")
        return None

    sent = await client.send_message(chat_id, "میوهام")
    _track_backup_message(phone, chat_id, sent.id)

    timeout = setting_int("PROFILE_FETCH_TIMEOUT", 20)
    reply = await _wait_for_bot_reply(client, chat_id, sent.id, timeout=timeout)

    if not reply:
        print(f"⚠️ [{phone}] میوهام reply timeout")
        return None

    profile = parse_profile_text(reply.text or reply.caption or "")

    if profile:
        update_account_profile(phone, **profile)
        print(f"🍬 [{phone}] Profile updated: balance={profile.get('balance')}")

    return profile


async def _job_transfer(client, phone, backup_group_id, target_user_id):
    chat_id = int(backup_group_id)

    profile = await _job_fetch_profile(client, phone, backup_group_id)

    balance = (profile or {}).get("balance") or 0

    if balance <= 0:
        print(f"⚠️ [{phone}] Transfer skipped: balance={balance}")
        return {"phone": phone, "status": "skipped", "amount": 0}

    # 1-3 seconds delay between commands
    await asyncio.sleep(random.uniform(1.0, 3.0))

    command = f"انتقال میویی {balance} {target_user_id}"
    sent = await client.send_message(chat_id, command)
    _track_backup_message(phone, chat_id, sent.id)

    print(f"💸 [{phone}] Transfer command sent: {command}")

    return {"phone": phone, "status": "ok", "amount": balance}


async def update_status_for_user(user_id):
    """
    Updates account names + backup group membership for all accounts of a user.
    """
    user = get_web_user_by_id(user_id)
    backup_group_id = (user or {}).get("backup_group_id") or ""

    accounts = get_tg_accounts_for_user(user_id)

    for acc in accounts:
        phone = acc["phone"]

        try:
            result = await session_manager.run_with_client(
                phone,
                lambda client: _job_name_and_backup(client, phone, backup_group_id)
            )

            update_account_meta(
                phone,
                account_name=result.get("name"),
                in_backup_group=result.get("in_backup")
            )

            print(f"🔄 [{phone}] status updated: name={result.get('name')} in_backup={result.get('in_backup')}")

        except Exception as e:
            print(f"❌ update_status error [{phone}]: {e}")

        await asyncio.sleep(random.uniform(1.0, 2.0))


async def update_profiles_for_user(user_id):
    """
    Fetches میوهام profile (balance etc.) for all accounts of a user.
    """
    user = get_web_user_by_id(user_id)
    backup_group_id = (user or {}).get("backup_group_id") or ""

    if not backup_group_id:
        print("⚠️ update_profiles: no backup group set")
        return

    accounts = get_tg_accounts_for_user(user_id)

    for acc in accounts:
        phone = acc["phone"]

        try:
            await session_manager.run_with_client(
                phone,
                lambda client: _job_fetch_profile(client, phone, backup_group_id)
            )
        except Exception as e:
            print(f"❌ update_profiles error [{phone}]: {e}")
            update_account_meta(phone, in_backup_group=0)

        await asyncio.sleep(random.uniform(1.0, 3.0))


async def transfer_for_user(user_id, target_user_id):
    """
    Makes every account transfer its whole balance to target_user_id.
    """
    user = get_web_user_by_id(user_id)
    backup_group_id = (user or {}).get("backup_group_id") or ""

    if not backup_group_id:
        print("⚠️ transfer: no backup group set")
        return

    accounts = get_tg_accounts_for_user(user_id)

    for acc in accounts:
        phone = acc["phone"]

        try:
            await session_manager.run_with_client(
                phone,
                lambda client: _job_transfer(client, phone, backup_group_id, target_user_id)
            )
        except Exception as e:
            print(f"❌ transfer error [{phone}]: {e}")

        await asyncio.sleep(random.uniform(1.0, 3.0))


# ============================================================
# Button helpers
# ============================================================

async def click_inline_button(message, button, row_index, col_index):
    max_retries = setting_int("BUTTON_CLICK_MAX_RETRIES", 10)
    max_retries = max(1, int(max_retries))

    retry_delay = setting_float("BUTTON_CLICK_RETRY_DELAY", 1.0)
    retry_delay = max(0.0, float(retry_delay))

    callback_data = getattr(button, "callback_data", None)
    last_error = None

    for attempt in range(1, max_retries + 1):
        if callback_data:
            try:
                await message.click(callback_data=callback_data)
                return True

            except TypeError:
                callback_data = None

            except Exception as e:
                last_error = e
                error_text = str(e).lower()

                if "doesn't exist" in error_text:
                    callback_data = None
                else:
                    if attempt < max_retries:
                        print(f"⚠️ Button click retry {attempt}/{max_retries}: {e}")
                        await asyncio.sleep(retry_delay)
                        continue

                    break

        try:
            await message.click(col_index, row_index)
            return True

        except Exception as e:
            last_error = e
            error_text = str(e).lower()

            if "doesn't exist" in error_text:
                try:
                    await message.click(row_index, col_index)
                    return True
                except Exception as e2:
                    last_error = e2
                    break

            if attempt < max_retries:
                print(f"⚠️ Coordinate click retry {attempt}/{max_retries}: {e}")
                await asyncio.sleep(retry_delay)
                continue

            break

    print(f"❌ coordinate click error: {last_error}")
    return False


async def click_meow_claim_buttons(message):
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

async def delayed_click_fishing(client, phone, message, msg_key: str):
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

        fishing_btn = optimizations.find_fishing_button(fresh_message)
        if fishing_btn:
            success = await click_inline_button(
                fresh_message,
                fishing_btn,
                getattr(fishing_btn, "_row_index", 0),
                getattr(fishing_btn, "_col_index", 0)
            )
            if success:
                _remember_clicked(CLICKED_FISHING_MESSAGES, msg_key)
                print(f"🎣 [{msg_key}] Clicked fishing button (feed_cat)")

                schedule_fishing_status_check(phone)

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

        pishi_btn = optimizations.find_pishi_button(fresh_message)
        if pishi_btn:
            success = await click_inline_button(
                fresh_message,
                pishi_btn,
                getattr(pishi_btn, "_row_index", 0),
                getattr(pishi_btn, "_col_index", 0)
            )
            if success:
                _remember_clicked(PISHI_CLICKED_MESSAGES, msg_key)
                print(f"🐱 [{msg_key}] Pishi button clicked (collect_cat)")

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
# Fishing probe sender
# ============================================================

async def send_fishing_probe(client, phone: str, chat_ids, reason: str):
    print(f"🎣 [{phone}] Fishing status probe: {reason}")

    for chat_id in chat_ids:
        try:
            sent_message = await client.send_message(chat_id, "ماهی")

            if phone not in TRACKED_FISHING_MESSAGES:
                TRACKED_FISHING_MESSAGES[phone] = {}

            TRACKED_FISHING_MESSAGES[phone][chat_id] = sent_message.id

            print(f"🎣 [{phone}] Fishing probe sent to {chat_id}, tracking message {sent_message.id}")

        except FloodWait as e:
            wait_seconds = flood_seconds(e)
            schedule_feature(phone, "fishing", wait_seconds, jitter=20)
            print(f"⏳ FloodWait Fishing probe [{phone}]: {wait_seconds}s")
            break

        except Exception as e:
            print(f"❌ Fishing probe error [{phone}]: {e}")

        await asyncio.sleep(random.uniform(1.0, 3.0))


# ============================================================
# Bot message handler
# ============================================================

async def is_reply_to_self(client, message, self_id):
    reply_to_message_id = getattr(message, "reply_to_message_id", None)
    replied_message = getattr(message, "reply_to_message", None)

    if not reply_to_message_id and not replied_message:
        return False

    if replied_message:
        sender = getattr(replied_message, "from_user", None)

        if sender and getattr(sender, "id", None) == self_id:
            return True

    if not reply_to_message_id:
        return False

    cache_key = f"{message.chat.id}:{reply_to_message_id}"

    cached_owner = REPLY_OWNER_CACHE.get(cache_key)

    if cached_owner is not None:
        return cached_owner == self_id

    owner_id = None

    try:
        fetched_message = await client.get_messages(
            message.chat.id,
            reply_to_message_id
        )

        if fetched_message:
            sender = getattr(fetched_message, "from_user", None)

            if sender:
                owner_id = getattr(sender, "id", None)

    except Exception as e:
        print(f"❌ is_reply_to_self fetch error: {e}")
        owner_id = None

    if owner_id is not None:
        REPLY_OWNER_CACHE[cache_key] = owner_id

        if len(REPLY_OWNER_CACHE) > 10000:
            REPLY_OWNER_CACHE.clear()

    return owner_id == self_id


async def handle_bot_message(client, phone: str, message):
    try:
        user = get_tg_account(phone)

        if not user or not user.get("is_active"):
            return

        if not optimizations.message_is_from_selected_group(message, user.get("selected_groups", [])):
            return

        # Ignore replies to backup-group commands (میوهام / انتقال)
        tracked_backup = BACKUP_TRACKED_MESSAGES.get(phone, {}).get(message.chat.id)
        if tracked_backup and getattr(message, "reply_to_message_id", None) in tracked_backup:
            return

        self_id = ACCOUNT_SELF_IDS.get(phone)

        if not self_id:
            try:
                self_user = getattr(client, "me", None)

                if self_user is None:
                    self_user = await client.get_me()

                self_id = self_user.id
                ACCOUNT_SELF_IDS[phone] = self_id

            except:
                return

        if not await is_reply_to_self(client, message, self_id):
            return

        raw_text = message.text or message.caption or ""
        normalized = normalize_text(raw_text).lower()

        cooldown = parse_cooldown_seconds(raw_text)
        meow_response = user.get("meow_enabled") and is_meow_response(phone, message, normalized)

        if cooldown is not None:
            if user.get("fishing_enabled") and ("ماهی" in normalized or "ماهیا" in normalized):
                schedule_feature(phone, "fishing", cooldown)

            elif meow_response:
                schedule_feature(phone, "meow", cooldown)

            elif user.get("pishi_enabled") and "پیشی" in normalized and "ماهی" not in normalized:
                schedule_feature(phone, "pishi", cooldown)

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
                            delayed_click_fishing(client, phone, message, msg_key)
                        )

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

            fishing_sent_this_cycle = False

            if account.get("fishing_enabled"):
                fishing_next_run = int(float(account.get("fishing_next_run") or 0))

                action = None

                if fishing_next_run == 0:
                    action = "send_initial"
                elif fishing_next_run > 0 and now >= fishing_next_run:
                    action = "send_due"

                if action:
                    waiting_timestamp = -int(time.time())

                    claimed = claim_dynamic_feature(
                        phone,
                        "fishing",
                        action,
                        waiting_timestamp,
                        now,
                        0
                    )

                    if not claimed:
                        print(f"⚠️ [{phone}] Fishing trigger skipped: already claimed or state changed")
                    else:
                        try:
                            update_fishing_status_check_at(phone, 0)
                        except:
                            pass

                        if action == "send_initial":
                            print(f"🎣 [{phone}] No saved Fishing time. Sending trigger once.")
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
                                print(f"⏳ FloodWait Fishing probe [{phone}]: {wait_seconds}s")
                                break

                            except Exception as e:
                                print(f"❌ Fishing error [{phone}]: {e}")
                                schedule_feature(phone, "fishing", 60, jitter=10)

                            await asyncio.sleep(random.uniform(1.0, 3.0))

                        fishing_sent_this_cycle = True

                if not fishing_sent_this_cycle:
                    status_check_at = int(float(account.get("fishing_status_check_at") or 0))

                    if status_check_at > 0 and now >= status_check_at:
                        claimed_status = claim_fishing_status_check(phone, now)

                        if claimed_status:
                            await send_fishing_probe(
                                client,
                                phone,
                                chat_ids,
                                "post-click status check"
                            )

                            fishing_sent_this_cycle = True

                if not fishing_sent_this_cycle:
                    periodic_interval = setting_int("FISHING_TIME_CHECK_INTERVAL", 900)
                    periodic_interval = max(60, int(periodic_interval))

                    periodic_check_at = int(float(account.get("fishing_periodic_check_at") or 0))

                    if now >= periodic_check_at:
                        next_periodic_check_at = now + periodic_interval

                        claimed_periodic = claim_fishing_periodic_check(
                            phone,
                            next_periodic_check_at,
                            now
                        )

                        if claimed_periodic:
                            fresh_account = get_tg_account(phone) or account

                            current_next_run = int(float(fresh_account.get("fishing_next_run") or 0))
                            status_check_at = int(float(fresh_account.get("fishing_status_check_at") or 0))

                            has_valid_parsed_time = current_next_run > now
                            has_future_status_check = status_check_at > now

                            if not has_valid_parsed_time and not has_future_status_check:
                                await send_fishing_probe(
                                    client,
                                    phone,
                                    chat_ids,
                                    "periodic time check"
                                )

                                fishing_sent_this_cycle = True
                            else:
                                if has_valid_parsed_time:
                                    print(f"🎣 [{phone}] Periodic check: parsed Fishing time already exists.")
                                elif has_future_status_check:
                                    print(f"🎣 [{phone}] Periodic check: waiting for scheduled Fishing status check.")

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

        try:
            self_user = getattr(client, "me", None)

            if self_user is None:
                self_user = await client.get_me()

            ACCOUNT_SELF_IDS[phone] = self_user.id

            # Save the account name only once (when missing in DB)
            if not account.get("account_name"):
                name = (
                    getattr(self_user, "first_name", None)
                    or getattr(self_user, "username", None)
                    or ""
                ).strip()

                if name:
                    update_account_meta(phone, account_name=name)

        except Exception as e:
            print(f"❌ Could not get self ID for {phone}: {e}")

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
                await session_manager.stop_and_cleanup_client(phone)
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
        await session_manager.stop_and_cleanup_client(phone)
    except Exception as e:
        print(f"❌ stop_and_cleanup_client error [{phone}]: {e}")

    _cancel_phone_click_tasks(phone)
    optimizations.force_gc(f"worker_stopped:{phone}")

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