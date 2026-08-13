import re
import time
import json
import random
import asyncio

from pyrogram.handlers import MessageHandler, EditedMessageHandler
from pyrogram import filters

from config import BOT_USER_ID

from database import (
    get_tg_account,
    get_web_user_by_id,
    get_setting_int,
    mask_phone,
    add_account_log,
    get_heist_config,
    save_heist_config,
    get_heist_accounts,
    save_heist_accounts,
    get_heist_state,
    update_heist_state,
    get_heist_cooldown,
    set_heist_cooldown,
    create_heist_log,
    update_account_jail,
)

import session_manager


# ============================================================
# Constants
# ============================================================

HEIST_TRIGGER = "سرقت میویی"
HEIST_JAIL_CHECK = "زندان میویی"
HEIST_WIN_TEXT = "سرقت میویی با موفقیت به اتمام رسید"
HEIST_LOSE_TEXT = "شما پیشی بدی بودین و زندانی شدین"
HEIST_JAIL_TEXT = "زندان میویی"

HEIST_COOLDOWNS = {
    1: 2 * 3600,
    2: 5 * 3600,
    3: 8 * 3600,
}

PHASE_TIMEOUT = 300  # 5 minutes default per phase

# ============================================================
# In-memory state
# ============================================================

HEIST_LOCKS = {}
HEIST_ABORT_FLAGS = {}
HEIST_MESSAGE_CACHE = {}

# ============================================================
# Regex
# ============================================================

JAIL_DURATION_RE = re.compile(r"مدت حبس\s*[:：]\s*(\d{1,2}(?:[:：]\d{1,2}){1,2})")
COOLDOWN_TIME_RE = re.compile(r"(\d{1,2}(?:[:：]\d{1,2}){1,2})")


# ============================================================
# Utility functions
# ============================================================

def parse_time_to_seconds(time_str):
    time_str = str(time_str).replace("：", ":")
    parts = []
    for p in time_str.split(":"):
        p = p.strip()
        if p:
            try:
                parts.append(int(p))
            except:
                pass

    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    elif len(parts) == 2:
        return parts[0] * 60 + parts[1]
    elif len(parts) == 1:
        return parts[0]
    return 0


def normalize_heist_text(text):
    if not text:
        return ""
    text = text.replace("\u200c", " ")
    text = text.replace("\u200e", "")
    text = text.replace("\u200f", "")
    text = text.replace("：", ":")
    return text


def find_heist_button(message, prefix):
    reply_markup = getattr(message, "reply_markup", None)
    if not reply_markup:
        return None

    rows = getattr(reply_markup, "inline_keyboard", None)
    if not rows:
        return None

    for row_index, row in enumerate(rows):
        if not row:
            continue
        for col_index, button in enumerate(row):
            callback_data = getattr(button, "callback_data", None)
            if callback_data:
                cd_str = str(callback_data) if not isinstance(callback_data, bytes) else callback_data.decode("utf-8", errors="ignore")
                if cd_str.startswith(prefix):
                    button._row_index = row_index
                    button._col_index = col_index
                    return button

    return None


def parse_level_buttons(message, user_id):
    levels = {}

    for level in [1, 2, 3]:
        start_prefix = f"mrob_start_loc:{user_id}:{level}"
        cd_prefix = f"mrob_cd_loc:{user_id}:{level}"

        start_btn = find_heist_button(message, start_prefix)
        if start_btn:
            levels[level] = {"status": "available", "cooldown_seconds": 0}
            continue

        cd_btn = find_heist_button(message, cd_prefix)
        if cd_btn:
            btn_text = getattr(cd_btn, "text", "") or ""
            cd_match = COOLDOWN_TIME_RE.search(btn_text)
            cd_seconds = parse_time_to_seconds(cd_match.group(1)) if cd_match else 0
            levels[level] = {"status": "cooldown", "cooldown_seconds": cd_seconds}
            continue

        levels[level] = {"status": "locked", "cooldown_seconds": 0}

    return levels


def parse_jail_duration(text):
    normalized = normalize_heist_text(text)
    match = JAIL_DURATION_RE.search(normalized)
    if match:
        return parse_time_to_seconds(match.group(1))
    return 0


# ============================================================
# Abort control
# ============================================================

def request_abort(user_id):
    HEIST_ABORT_FLAGS[user_id] = True


def clear_abort(user_id):
    HEIST_ABORT_FLAGS[user_id] = False


def check_abort(user_id):
    return HEIST_ABORT_FLAGS.get(user_id, False)


# ============================================================
# Message helpers
# ============================================================

async def get_heist_message(client, chat_id, message_id):
    try:
        message = await client.get_messages(chat_id, message_id)
        if message:
            HEIST_MESSAGE_CACHE[(chat_id, message_id)] = message
        return message
    except Exception:
        return HEIST_MESSAGE_CACHE.get((chat_id, message_id))


async def heist_wait_for_bot_reply(client, chat_id, sent_id, timeout=30):
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

    client.add_handler(handler, group=-20)

    try:
        return await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        return None
    finally:
        try:
            client.remove_handler(handler, group=-20)
        except Exception:
            pass


async def heist_click_button(client, chat_id, message_id, prefix, max_retries=3):
    for attempt in range(max_retries):
        try:
            message = await get_heist_message(client, chat_id, message_id)
            if not message:
                return False

            button = find_heist_button(message, prefix)
            if not button:
                if attempt < max_retries - 1:
                    await asyncio.sleep(1)
                    continue
                return False

            callback_data = getattr(button, "callback_data", None)
            if callback_data:
                try:
                    await message.click(callback_data=callback_data)
                    return True
                except TypeError:
                    pass
                except Exception as e:
                    error_text = str(e).lower()
                    if "floodwait" in error_text:
                        wait = 60
                        for attr in ("value", "x"):
                            val = getattr(e, attr, None)
                            if val:
                                try:
                                    wait = int(val)
                                    break
                                except:
                                    pass
                        await asyncio.sleep(wait + 1)
                        continue

            try:
                await message.click(button._col_index, button._row_index)
                return True
            except Exception:
                try:
                    await message.click(button._row_index, button._col_index)
                    return True
                except Exception:
                    pass

            if attempt < max_retries - 1:
                await asyncio.sleep(1)

        except Exception as e:
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
                continue
            return False

    return False


async def heist_wait_for_button(client, chat_id, message_id, prefix, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        message = await get_heist_message(client, chat_id, message_id)
        if message:
            button = find_heist_button(message, prefix)
            if button:
                return True
        await asyncio.sleep(1)
    return False


async def heist_wait_for_button_gone(client, chat_id, message_id, prefix, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        message = await get_heist_message(client, chat_id, message_id)
        if message:
            button = find_heist_button(message, prefix)
            if not button:
                return True
        await asyncio.sleep(1)
    return False


# ============================================================
# Heartbeat
# ============================================================

async def heist_heartbeat(user_id, state, message=""):
    try:
        config = get_heist_config(user_id)
        if config and config.get("heartbeat_enabled"):
            add_account_log("", "heist", state, "info", message)
    except Exception:
        pass


# ============================================================
# Main heist orchestrator
# ============================================================

async def run_heist(user_id, config_override=None):
    if user_id not in HEIST_LOCKS:
        HEIST_LOCKS[user_id] = asyncio.Lock()

    lock = HEIST_LOCKS[user_id]

    if lock.locked():
        return {"error": "heist_already_running"}

    async with lock:
        clear_abort(user_id)

        try:
            return await _execute_heist(user_id, config_override)
        except asyncio.CancelledError:
            update_heist_state(user_id, state='aborted')
            return {"error": "cancelled"}
        except Exception as e:
            update_heist_state(user_id, state='error', error_message=str(e))
            return {"error": str(e)}


async def _execute_heist(user_id, config_override=None):
    # ── Config ──
    config = config_override or get_heist_config(user_id)
    if not config:
        return {"error": "no_heist_config"}

    chat_id = int(config.get("chat_id", 0))
    level = int(config.get("selected_level", 1))
    steal_count = int(config.get("steal_count", 0))
    move_count = int(config.get("move_count", 0))
    listen_timeout = int(config.get("listen_timeout", 600))
    phase_timeout = PHASE_TIMEOUT

    if not chat_id:
        return {"error": "no_chat_id"}

    # ── Accounts ──
    heist_accounts = get_heist_accounts(user_id)
    if len(heist_accounts) != 4:
        return {"error": "need_exactly_4_accounts"}

    heist_accounts.sort(key=lambda a: a["position"])
    phones = [a["phone"] for a in heist_accounts]
    starter_phone = phones[0]

    # ── Pre-flight ──
    now = int(time.time())

    for phone in phones:
        account = get_tg_account(phone)
        if not account:
            return {"error": f"account_not_found"}
        if not account.get("is_active"):
            return {"error": f"account_inactive"}
        if int(account.get("jail_until") or 0) > now:
            return {"error": f"account_jailed"}

    cooldown_until = get_heist_cooldown(user_id, level)
    if cooldown_until > now:
        return {"error": f"cooldown_active", "remaining": cooldown_until - now}

    # ── Get clients ──
    clients = {}
    for phone in phones:
        try:
            client = await session_manager.get_client(phone)
            clients[phone] = client
        except Exception as e:
            return {"error": f"client_error: {e}"}

    starter_client = clients[starter_phone]
    started_at = int(time.time())

    try:
        # ══════════════════════════════════════
        # PHASE 1: Trigger
        # ══════════════════════════════════════
        update_heist_state(user_id, state='trigger_sent', chat_id=chat_id, level=level)
        await heist_heartbeat(user_id, "trigger_sent", f"Level {level}")

        trigger_msg = await starter_client.send_message(chat_id, HEIST_TRIGGER)

        if check_abort(user_id):
            update_heist_state(user_id, state='aborted')
            return {"error": "aborted"}

        reply = await heist_wait_for_bot_reply(starter_client, chat_id, trigger_msg.id, timeout=phase_timeout)
        if not reply:
            update_heist_state(user_id, state='error', error_message="no_bot_reply")
            return {"error": "no_bot_reply"}

        heist_message_id = reply.id
        update_heist_state(user_id, state='loc_selected', message_id=heist_message_id)

        # ══════════════════════════════════════
        # PHASE 2: Select location
        # ══════════════════════════════════════
        await heist_heartbeat(user_id, "loc_selected", "Clicking mrob_sel_loc")

        clicked = await heist_click_button(starter_client, chat_id, heist_message_id, "mrob_sel_loc")
        if not clicked:
            update_heist_state(user_id, state='error', error_message="mrob_sel_loc_not_found")
            return {"error": "mrob_sel_loc_not_found"}

        if check_abort(user_id):
            update_heist_state(user_id, state='aborted')
            return {"error": "aborted"}

        await asyncio.sleep(2)

        # ══════════════════════════════════════
        # PHASE 3: Select level
        # ══════════════════════════════════════
        update_heist_state(user_id, state='level_shown')
        await heist_heartbeat(user_id, "level_shown", f"Selecting level {level}")

        me = await starter_client.get_me()
        starter_self_id = me.id

        message = await get_heist_message(starter_client, chat_id, heist_message_id)
        if message:
            levels = parse_level_buttons(message, starter_self_id)
            available = [l for l, info in levels.items() if info["status"] == "available"]

            if not available:
                update_heist_state(user_id, state='error', error_message="no_levels_available")
                return {"error": "no_levels_available"}

            if level not in available:
                level = min(available)

        level_prefix = f"mrob_start_loc:{starter_self_id}:{level}"
        clicked = await heist_click_button(starter_client, chat_id, heist_message_id, level_prefix)
        if not clicked:
            update_heist_state(user_id, state='error', error_message=f"level_click_failed")
            return {"error": "level_click_failed"}

        update_heist_state(user_id, state='level_selected', level=level)

        if check_abort(user_id):
            update_heist_state(user_id, state='aborted')
            return {"error": "aborted"}

        await asyncio.sleep(2)

        # ══════════════════════════════════════
        # PHASE 4: Join (accounts 2, 3, 4)
        # ══════════════════════════════════════
        update_heist_state(user_id, state='waiting_joins')
        await heist_heartbeat(user_id, "waiting_joins", "Waiting for joins")

        for i in range(1, 4):
            phone = phones[i]
            client = clients[phone]

            if check_abort(user_id):
                update_heist_state(user_id, state='aborted')
                return {"error": "aborted"}

            join_found = await heist_wait_for_button(client, chat_id, heist_message_id, "mrob_join", timeout=phase_timeout)
            if not join_found:
                update_heist_state(user_id, state='error', error_message="join_button_not_found")
                return {"error": "join_button_not_found"}

            clicked = await heist_click_button(client, chat_id, heist_message_id, "mrob_join")
            if not clicked:
                update_heist_state(user_id, state='error', error_message="join_click_failed")
                return {"error": "join_click_failed"}

            add_account_log(phone, "heist", "join", "success", f"Joined (position {i+1})")
            await asyncio.sleep(random.uniform(0.5, 1.5))

        update_heist_state(user_id, state='all_joined')
        await heist_heartbeat(user_id, "all_joined", "All joined")

        if check_abort(user_id):
            update_heist_state(user_id, state='aborted')
            return {"error": "aborted"}

        await asyncio.sleep(1)

        # ══════════════════════════════════════
        # PHASE 5: Confirm
        # ══════════════════════════════════════
        update_heist_state(user_id, state='confirmed')
        await heist_heartbeat(user_id, "confirmed", "Confirming")

        confirm_found = await heist_wait_for_button(starter_client, chat_id, heist_message_id, "mrob_confirm", timeout=phase_timeout)
        if not confirm_found:
            update_heist_state(user_id, state='error', error_message="confirm_not_found")
            return {"error": "confirm_not_found"}

        clicked = await heist_click_button(starter_client, chat_id, heist_message_id, "mrob_confirm")
        if not clicked:
            update_heist_state(user_id, state='error', error_message="confirm_click_failed")
            return {"error": "confirm_click_failed"}

        await asyncio.sleep(2)

        # ══════════════════════════════════════
        # PHASE 6: Open (Account 1)
        # ══════════════════════════════════════
        update_heist_state(user_id, state='phase_open')
        await heist_heartbeat(user_id, "phase_open", "Account 1: Opening")

        if check_abort(user_id):
            update_heist_state(user_id, state='aborted')
            return {"error": "aborted"}

        open_found = await heist_wait_for_button(starter_client, chat_id, heist_message_id, "mrob_act_open", timeout=phase_timeout)
        if not open_found:
            update_heist_state(user_id, state='error', error_message="open_not_found")
            return {"error": "open_not_found"}

        clicked = await heist_click_button(starter_client, chat_id, heist_message_id, "mrob_act_open")
        if not clicked:
            update_heist_state(user_id, state='error', error_message="open_click_failed")
            return {"error": "open_click_failed"}

        add_account_log(starter_phone, "heist", "open", "success", "Lock opened")
        await asyncio.sleep(2)

        # ══════════════════════════════════════
        # PHASE 7: Steal (Account 2)
        # ══════════════════════════════════════
        update_heist_state(user_id, state='phase_steal', steal_clicks_done=0)
        await heist_heartbeat(user_id, "phase_steal", f"Account 2: Stealing (target: {steal_count or 'unlimited'})")

        if check_abort(user_id):
            update_heist_state(user_id, state='aborted')
            return {"error": "aborted"}

        steal_client = clients[phones[1]]
        steal_clicks = 0

        if steal_count == 0:
            while True:
                if check_abort(user_id):
                    update_heist_state(user_id, state='aborted')
                    return {"error": "aborted"}

                message = await get_heist_message(steal_client, chat_id, heist_message_id)
                if not message:
                    break

                if not find_heist_button(message, "mrob_act_steal"):
                    break

                clicked = await heist_click_button(steal_client, chat_id, heist_message_id, "mrob_act_steal")
                if clicked:
                    steal_clicks += 1
                    update_heist_state(user_id, steal_clicks_done=steal_clicks)

                await asyncio.sleep(random.uniform(0.5, 1.5))
        else:
            for _ in range(steal_count):
                if check_abort(user_id):
                    update_heist_state(user_id, state='aborted')
                    return {"error": "aborted"}

                message = await get_heist_message(steal_client, chat_id, heist_message_id)
                if not message:
                    break

                if not find_heist_button(message, "mrob_act_steal"):
                    break

                clicked = await heist_click_button(steal_client, chat_id, heist_message_id, "mrob_act_steal")
                if clicked:
                    steal_clicks += 1
                    update_heist_state(user_id, steal_clicks_done=steal_clicks)

                await asyncio.sleep(random.uniform(0.5, 1.5))

            stop_found = await heist_wait_for_button(steal_client, chat_id, heist_message_id, "mrob_act_stop_st", timeout=10)
            if stop_found:
                await heist_click_button(steal_client, chat_id, heist_message_id, "mrob_act_stop_st")

        add_account_log(phones[1], "heist", "steal", "success", f"Steal: {steal_clicks} clicks")
        await asyncio.sleep(2)

        # ══════════════════════════════════════
        # PHASE 8: Move (Account 3)
        # ══════════════════════════════════════
        update_heist_state(user_id, state='phase_move', move_clicks_done=0)
        await heist_heartbeat(user_id, "phase_move", f"Account 3: Moving (target: {move_count or 'unlimited'})")

        if check_abort(user_id):
            update_heist_state(user_id, state='aborted')
            return {"error": "aborted"}

        move_client = clients[phones[2]]
        move_clicks = 0

        if move_count == 0:
            while True:
                if check_abort(user_id):
                    update_heist_state(user_id, state='aborted')
                    return {"error": "aborted"}

                message = await get_heist_message(move_client, chat_id, heist_message_id)
                if not message:
                    break

                if not find_heist_button(message, "mrob_act_move"):
                    break

                clicked = await heist_click_button(move_client, chat_id, heist_message_id, "mrob_act_move")
                if clicked:
                    move_clicks += 1
                    update_heist_state(user_id, move_clicks_done=move_clicks)

                await asyncio.sleep(random.uniform(0.5, 1.5))
        else:
            for _ in range(move_count):
                if check_abort(user_id):
                    update_heist_state(user_id, state='aborted')
                    return {"error": "aborted"}

                message = await get_heist_message(move_client, chat_id, heist_message_id)
                if not message:
                    break

                if not find_heist_button(message, "mrob_act_move"):
                    break

                clicked = await heist_click_button(move_client, chat_id, heist_message_id, "mrob_act_move")
                if clicked:
                    move_clicks += 1
                    update_heist_state(user_id, move_clicks_done=move_clicks)

                await asyncio.sleep(random.uniform(0.5, 1.5))

            stop_found = await heist_wait_for_button(move_client, chat_id, heist_message_id, "mrob_act_stop_mv", timeout=10)
            if stop_found:
                await heist_click_button(move_client, chat_id, heist_message_id, "mrob_act_stop_mv")

        add_account_log(phones[2], "heist", "move", "success", f"Move: {move_clicks} clicks")
        await asyncio.sleep(2)

        # ══════════════════════════════════════
        # PHASE 9: Run (Account 4)
        # ══════════════════════════════════════
        update_heist_state(user_id, state='phase_run')
        await heist_heartbeat(user_id, "phase_run", "Account 4: Running")

        if check_abort(user_id):
            update_heist_state(user_id, state='aborted')
            return {"error": "aborted"}

        run_client = clients[phones[3]]

        run_found = await heist_wait_for_button(run_client, chat_id, heist_message_id, "mrob_act_run", timeout=phase_timeout)
        if not run_found:
            update_heist_state(user_id, state='error', error_message="run_not_found")
            return {"error": "run_not_found"}

        clicked = await heist_click_button(run_client, chat_id, heist_message_id, "mrob_act_run")
        if not clicked:
            update_heist_state(user_id, state='error', error_message="run_click_failed")
            return {"error": "run_click_failed"}

        add_account_log(phones[3], "heist", "run", "success", "Escape initiated")

        # ══════════════════════════════════════
        # PHASE 10: Listen for result
        # ══════════════════════════════════════
        update_heist_state(user_id, state='listening')
        await heist_heartbeat(user_id, "listening", f"Listening (timeout: {listen_timeout}s)")

        listen_deadline = time.time() + listen_timeout
        result = None

        while time.time() < listen_deadline:
            if check_abort(user_id):
                update_heist_state(user_id, state='aborted')
                return {"error": "aborted"}

            message = await get_heist_message(starter_client, chat_id, heist_message_id)
            if message:
                text = message.text or message.caption or ""

                if HEIST_WIN_TEXT in text:
                    result = "won"
                    break

                if HEIST_LOSE_TEXT in text:
                    result = "lost"
                    break

            await asyncio.sleep(2)

        if result is None:
            result = "timeout"

        # ══════════════════════════════════════
        # PHASE 11: Handle result
        # ══════════════════════════════════════
        finished_at = int(time.time())
        duration = finished_at - started_at

        # Shared cooldown
        cooldown_seconds = HEIST_COOLDOWNS.get(level, 7200)
        set_heist_cooldown(user_id, level, finished_at + cooldown_seconds)

        # Jail on loss
        if result == "lost":
            message = await get_heist_message(starter_client, chat_id, heist_message_id)
            if message:
                text = message.text or message.caption or ""
                jail_seconds = parse_jail_duration(text)

                if jail_seconds > 0:
                    jail_until = finished_at + jail_seconds
                    for phone in phones:
                        update_account_jail(phone, jail_until, "heist_loss")
                        add_account_log(phone, "heist", "jail", "warning", f"Jailed {jail_seconds}s")

        update_heist_state(user_id, state=result)

        create_heist_log(
            user_id=user_id,
            level=level,
            result=result,
            duration_seconds=duration,
            accounts_used=phones,
            started_at=started_at,
            finished_at=finished_at
        )

        add_account_log(starter_phone, "heist", "result",
                       "success" if result == "won" else "error",
                       f"Heist {result}: level={level}, duration={duration}s")

        await heist_heartbeat(user_id, result, f"Heist {result}")

        return {"result": result, "level": level, "duration": duration}

    finally:
        for phone in phones:
            try:
                await session_manager.release_client(phone)
            except Exception:
                pass


# ============================================================
# Cooldown probe
# ============================================================

async def check_heist_cooldowns(user_id):
    config = get_heist_config(user_id)
    if not config or not config.get("chat_id"):
        return {"error": "no_heist_config"}

    chat_id = int(config["chat_id"])

    heist_accounts = get_heist_accounts(user_id)
    if not heist_accounts:
        return {"error": "no_accounts"}

    heist_accounts.sort(key=lambda a: a["position"])
    starter_phone = heist_accounts[0]["phone"]

    starter_account = get_tg_account(starter_phone)
    if not starter_account or not starter_account.get("is_active"):
        return {"error": "starter_not_available"}

    if int(starter_account.get("jail_until") or 0) > int(time.time()):
        return {"error": "starter_jailed"}

    try:
        client = await session_manager.get_client(starter_phone)
    except Exception as e:
        return {"error": f"client_error: {e}"}

    try:
        trigger_msg = await client.send_message(chat_id, HEIST_TRIGGER)
        reply = await heist_wait_for_bot_reply(client, chat_id, trigger_msg.id, timeout=30)

        if not reply:
            return {"error": "no_bot_reply"}

        heist_message_id = reply.id

        clicked = await heist_click_button(client, chat_id, heist_message_id, "mrob_sel_loc")
        if not clicked:
            return {"error": "mrob_sel_loc_not_found"}

        await asyncio.sleep(2)

        me = await client.get_me()
        starter_id = me.id

        message = await get_heist_message(client, chat_id, heist_message_id)
        if not message:
            return {"error": "message_not_found"}

        levels = parse_level_buttons(message, starter_id)

        now = int(time.time())
        for level_num, info in levels.items():
            if info["status"] == "cooldown":
                set_heist_cooldown(user_id, level_num, now + info["cooldown_seconds"])
            elif info["status"] == "available":
                set_heist_cooldown(user_id, level_num, 0)

        return {"levels": levels}

    finally:
        try:
            await session_manager.release_client(starter_phone)
        except Exception:
            pass


# ============================================================
# Jail check
# ============================================================

async def check_jail_status(user_id, phone):
    config = get_heist_config(user_id)
    if not config or not config.get("chat_id"):
        return {"in_jail": False}

    chat_id = int(config["chat_id"])

    try:
        client = await session_manager.get_client(phone)
    except Exception:
        return {"in_jail": False}

    try:
        sent = await client.send_message(chat_id, HEIST_JAIL_CHECK)
        reply = await heist_wait_for_bot_reply(client, chat_id, sent.id, timeout=15)

        if not reply:
            return {"in_jail": False}

        text = reply.text or reply.caption or ""

        if HEIST_JAIL_TEXT in text:
            jail_seconds = parse_jail_duration(text)
            if jail_seconds > 0:
                jail_until = int(time.time()) + jail_seconds
                update_account_jail(phone, jail_until, "checked")
                return {"in_jail": True, "jail_until": jail_until, "seconds": jail_seconds}

        update_account_jail(phone, 0, "")
        return {"in_jail": False}

    finally:
        try:
            await session_manager.release_client(phone)
        except Exception:
            pass


# ============================================================
# Common groups
# ============================================================

def get_common_groups_sync(user_id):
    heist_accounts = get_heist_accounts(user_id)
    if not heist_accounts:
        return []

    group_sets = []

    for acc_info in heist_accounts:
        account = get_tg_account(acc_info["phone"])
        if not account:
            continue

        cached_groups = account.get("cached_groups", [])
        group_ids = set()

        for group in cached_groups:
            try:
                group_ids.add(int(group.get("id", 0)))
            except Exception:
                pass

        group_sets.append(group_ids)

    if not group_sets:
        return []

    common = group_sets[0]
    for gs in group_sets[1:]:
        common = common.intersection(gs)

    first_account = get_tg_account(heist_accounts[0]["phone"])
    if not first_account:
        return []

    result = []
    for group in first_account.get("cached_groups", []):
        try:
            gid = int(group.get("id", 0))
            if gid in common:
                result.append(group)
        except Exception:
            pass

    return result


# ============================================================
# Auto heist loop
# ============================================================

async def auto_heist_loop(user_id):
    while True:
        try:
            config = get_heist_config(user_id)

            if not config or not config.get("auto_enabled"):
                await asyncio.sleep(60)
                continue

            state = get_heist_state(user_id)
            if state:
                active_states = ('trigger_sent', 'loc_selected', 'level_shown', 'level_selected',
                                'waiting_joins', 'all_joined', 'confirmed', 'phase_open',
                                'phase_steal', 'phase_move', 'phase_run', 'listening')
                if state.get("state") in active_states:
                    await asyncio.sleep(60)
                    continue

            now = int(time.time())
            level = int(config.get("selected_level", 1))
            auto_mode = config.get("auto_level_mode", "best_available")

            target_level = None

            if auto_mode == "best_available":
                for lvl in [1, 2, 3]:
                    cd_until = get_heist_cooldown(user_id, lvl)
                    if cd_until <= now:
                        target_level = lvl
                        break
            else:
                cd_until = get_heist_cooldown(user_id, level)
                if cd_until <= now:
                    target_level = level

            if target_level is None:
                await asyncio.sleep(60)
                continue

            heist_accounts = get_heist_accounts(user_id)
            if len(heist_accounts) != 4:
                await asyncio.sleep(60)
                continue

            all_available = True
            for acc_info in heist_accounts:
                account = get_tg_account(acc_info["phone"])
                if not account or not account.get("is_active"):
                    all_available = False
                    break
                if int(account.get("jail_until") or 0) > now:
                    all_available = False
                    break

            if not all_available:
                await asyncio.sleep(60)
                continue

            delay_min = int(config.get("auto_delay_min", 5))
            delay_max = int(config.get("auto_delay_max", 15))
            await asyncio.sleep(random.uniform(delay_min, delay_max))

            override = dict(config)
            override["selected_level"] = target_level

            print(f"🎯 Auto heist triggered for user {user_id}: level {target_level}")

            await run_heist(user_id, config_override=override)

        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"❌ auto_heist_loop error: {e}")

        await asyncio.sleep(60)