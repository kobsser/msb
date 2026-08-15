import re
import time
import asyncio
import random

from pyrogram import filters
from pyrogram.errors import FloodWait
from pyrogram.handlers import EditedMessageHandler, MessageHandler

from config import BOT_USER_ID

from database import (
    get_tg_account,
    get_heist_config,
    get_heist_accounts,
    get_heist_state,
    set_heist_state,
    update_heist_state,
    reset_heist_state,
    get_heist_cooldown,
    set_heist_cooldown,
    get_all_heist_cooldowns,
    add_heist_log,
    add_account_log,
    set_account_jail,
    mask_phone,
    get_setting_int,
)

import session_manager
import optimizations


# ============================================================
# Constants
# ============================================================

HEIST_TRIGGER = "سرقت میویی"
HEIST_JAIL_CHECK = "زندان میویی"

CB_SEL_LOC = "mrob_sel_loc"
CB_START_LOC = "mrob_start_loc:"
CB_CD_LOC = "mrob_cd_loc:"
CB_IGNORE = "mrob_ignore"
CB_JOIN = "mrob_join"
CB_LEAVE = "mrob_leave"
CB_CONFIRM = "mrob_confirm"
CB_ACT_OPEN = "mrob_act_open"
CB_ACT_STEAL = "mrob_act_steal"
CB_ACT_STOP_ST = "mrob_act_stop_st"
CB_ACT_MOVE = "mrob_act_move"
CB_ACT_STOP_MV = "mrob_act_stop_mv"
CB_ACT_RUN = "mrob_act_run"

WIN_TEXT = "سرقت میویی با موفقیت به اتمام رسید"
LOSE_TEXT = "شما پیشی بدی بودین و زندانی شدید"
JAIL_TEXT_MARKER = "زندان میویی"

STEAL_PHASE_TEXT = "در گاو صندوق باز شد"
MOVE_PHASE_TEXT = "وسایل رو برداشتید"
RUN_PHASE_TEXT = "وسایل رو جا به جا کردید"

JAIL_DURATION_RE = re.compile(
    r"مدت حبس\s*[:：]\s*(\d{1,3}(?:[:：]\d{1,2}){1,2})"
)
CD_TIME_RE = re.compile(
    r"(\d{1,3}(?:[:：]\d{1,2}){1,2})"
)
STEAL_COMPLETED_RE = re.compile(r"وسایل برداشته شده\s*[:：]\s*(\d+)")
MOVE_COMPLETED_RE = re.compile(r"وسایل جا به جا شده\s*[:：]\s*(\d+)")
REMAINING_RE = re.compile(r"وسایل موجود\s*[:：]\s*(\d+)")

HEIST_COOLDOWN_SECONDS = {
    1: 2 * 3600,
    2: 5 * 3600,
    3: 8 * 3600,
}

PHASE_TIMEOUTS = {
    "trigger": 30,
    "select_loc": 15,
    "select_level": 30,
    "join": 60,
    "confirm": 15,
    "open": 120,
    "steal": 300,
    "move": 300,
    "run": 120,
    "listen": 600,
}

NEXT_PHASE_INDICATORS = {
    "steal": {"button": CB_ACT_MOVE, "text": MOVE_PHASE_TEXT},
    "move": {"button": CB_ACT_RUN, "text": RUN_PHASE_TEXT},
    "run": {"button": None, "text": None},
}

CLICK_DELAY = 0.1
EDIT_WAIT_TIMEOUT = 1.0
CLICK_RETRY_DELAY = 0.5
CLICK_MAX_RETRIES = 2

HEIST_TRACKED_MESSAGES = set()
HEIST_ACTIVE_MESSAGES = {}


# ============================================================
# Time parsing
# ============================================================

def parse_hms_to_seconds(text):
    text = text.replace("：", ":").strip()
    parts = [p for p in text.split(":") if p.strip()]
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    elif len(parts) == 2:
        return parts[0] * 60 + parts[1]
    elif len(parts) == 1:
        return parts[0]
    return 0


def parse_jail_duration(text):
    m = JAIL_DURATION_RE.search(text)
    if m:
        return parse_hms_to_seconds(m.group(1))
    return 0


def parse_cd_time(text):
    m = CD_TIME_RE.search(text)
    if m:
        return parse_hms_to_seconds(m.group(1))
    return 0


# ============================================================
# Button helpers
# ============================================================

def find_button_by_prefix(message, prefix):
    if not message:
        return None
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
            cb = optimizations.normalize_callback_data(
                getattr(button, "callback_data", None)
            )
            if cb.startswith(prefix):
                button._row_index = row_index
                button._col_index = col_index
                button._callback_data = cb
                return button
    return None


def find_all_buttons_by_prefix(message, prefix):
    results = []
    if not message:
        return results
    reply_markup = getattr(message, "reply_markup", None)
    if not reply_markup:
        return results
    rows = getattr(reply_markup, "inline_keyboard", None)
    if not rows:
        return results
    for row_index, row in enumerate(rows):
        if not row:
            continue
        for col_index, button in enumerate(row):
            cb = optimizations.normalize_callback_data(
                getattr(button, "callback_data", None)
            )
            if cb.startswith(prefix):
                button._row_index = row_index
                button._col_index = col_index
                button._callback_data = cb
                results.append(button)
    return results


def has_button_with_prefix(message, prefix):
    return find_button_by_prefix(message, prefix) is not None


async def click_heist_button(client, message, button):
    cb = getattr(button, "callback_data", None)
    if cb:
        try:
            await message.click(callback_data=cb)
            return True
        except TypeError:
            pass
        except FloodWait as e:
            wait = getattr(e, "value", None) or getattr(e, "x", 60)
            print(f"⏳ Heist FloodWait: {wait}s")
            await asyncio.sleep(int(wait))
            try:
                await message.click(callback_data=cb)
                return True
            except Exception:
                return False
        except Exception:
            pass
    try:
        await message.click(
            getattr(button, "_col_index", 0),
            getattr(button, "_row_index", 0)
        )
        return True
    except Exception:
        try:
            await message.click(
                getattr(button, "_row_index", 0),
                getattr(button, "_col_index", 0)
            )
            return True
        except Exception:
            return False


# ============================================================
# Heist Orchestrator
# ============================================================

class HeistOrchestrator:

    def __init__(self, user_id):
        self.user_id = user_id
        self._loop = None
        self._edit_queue = None
        self._running = False
        self._abort_requested = False
        self._starter_client = None
        self._cached_message = None
        self._message_id = 0
        self._chat_id = 0
        self._start_time = 0
        self._heist_phones = []
        self._edit_handler = None
        self._message_handler = None
        self._trigger_message_id = 0

    # ----------------------------------------------------------
    # Public API
    # ----------------------------------------------------------

    async def start(self, level=None, steal_count=None, move_count=None):
        state = get_heist_state(self.user_id)
        if state["state"] not in ("idle", "cooldown", "error", "won", "lost", "timeout"):
            print(f"⚠️ Heist already running for user {self.user_id}")
            return False

        config = get_heist_config(self.user_id)
        accounts = get_heist_accounts(self.user_id)

        if len(accounts) != 4:
            print(f"❌ Heist requires exactly 4 accounts, got {len(accounts)}")
            return False

        if not await self._preflight_check(accounts):
            return False

        effective_level = level if level is not None else config.get("selected_level", 1)
        effective_steal = steal_count if steal_count is not None else config.get("steal_count", 0)
        effective_move = move_count if move_count is not None else config.get("move_count", 0)
        listen_timeout = config.get("listen_timeout", 600)
        click_count_mode = config.get("click_count_mode", "local")

        chat_id = await self._resolve_chat_id(config)
        if not chat_id:
            print(f"❌ No valid heist chat for user {self.user_id}")
            return False

        self._chat_id = chat_id
        self._running = True
        self._abort_requested = False
        self._start_time = int(time.time())
        self._cached_message = None
        self._message_id = 0
        self._loop = asyncio.get_event_loop()
        self._edit_queue = asyncio.Queue()

        phones = [a["phone"] for a in accounts]
        self._heist_phones = phones

        for p in phones:
            HEIST_TRACKED_MESSAGES.add((p, chat_id))

        set_heist_state(self.user_id, "trigger_sent", chat_id=chat_id, level=effective_level)

        try:
            starter_phone = phones[0]
            self._starter_client = await session_manager.get_client(starter_phone)

            try:
                await self._run_heist(
                    phones=phones,
                    level=effective_level,
                    steal_count=effective_steal,
                    move_count=effective_move,
                    listen_timeout=listen_timeout,
                    chat_id=chat_id,
                    config=config,
                    click_count_mode=click_count_mode,
                )
            finally:
                self._unregister_handlers()
                if self._starter_client:
                    try:
                        await session_manager.release_client(starter_phone)
                    except Exception:
                        pass
                    self._starter_client = None

        except Exception as e:
            print(f"❌ Heist orchestrator error: {e}")
            import traceback
            traceback.print_exc()
            set_heist_state(self.user_id, "error", error=str(e))

        finally:
            self._running = False
            self._trigger_message_id = 0
            for p in phones:
                HEIST_TRACKED_MESSAGES.discard((p, chat_id))
                HEIST_ACTIVE_MESSAGES.pop((p, chat_id), None)

        return True

    async def abort(self):
        self._abort_requested = True
        print(f"⏹ Heist abort requested for user {self.user_id}")

    def is_running(self):
        return self._running and not self._abort_requested

    # ----------------------------------------------------------
    # Pre-flight
    # ----------------------------------------------------------

    async def _preflight_check(self, accounts):
        now = int(time.time())
        for acc in accounts:
            phone = acc["phone"]
            account = get_tg_account(phone)
            if not account:
                print(f"❌ Account not found: {mask_phone(phone)}")
                return False
            if not account.get("is_active"):
                print(f"❌ Account inactive: {mask_phone(phone)}")
                return False
            if int(account.get("jail_until") or 0) > now:
                print(f"❌ Account jailed: {mask_phone(phone)}")
                return False

        cooldowns = get_all_heist_cooldowns(self.user_id)
        all_on_cooldown = True
        for lvl in (1, 2, 3):
            if cooldowns.get(lvl, 0) <= now:
                all_on_cooldown = False
                break
        if all_on_cooldown:
            print(f"❌ All levels on cooldown for user {self.user_id}")
            return False
        return True

    async def _resolve_chat_id(self, config):
        if config.get("use_backup_group"):
            from database import get_web_user_by_id
            user = get_web_user_by_id(self.user_id)
            backup = (user or {}).get("backup_group_id") or ""
            if backup:
                try:
                    return int(backup)
                except ValueError:
                    pass
            return 0
        chat_id = config.get("chat_id", 0)
        return int(chat_id) if chat_id else 0

    # ----------------------------------------------------------
    # Handler registration
    # ----------------------------------------------------------

    def _register_handlers(self, chat_id, trigger_message_id):
        async def _on_message(_, message):
            if (message.chat.id == chat_id and
                    message.from_user and
                    message.from_user.id == BOT_USER_ID):

                # Once trigger ID is known, only accept replies to it
                if self._trigger_message_id > 0:
                    if getattr(message, "reply_to_message_id", None) != self._trigger_message_id:
                        return

                try:
                    asyncio.run_coroutine_threadsafe(
                        self._edit_queue.put(message),
                        self._loop
                    )
                except Exception:
                    pass

        async def _on_edit(_, edited_message):
            if (edited_message.chat.id == chat_id and
                    self._message_id > 0 and
                    edited_message.id == self._message_id):
                try:
                    asyncio.run_coroutine_threadsafe(
                        self._edit_queue.put(edited_message),
                        self._loop
                    )
                except Exception:
                    pass

        self._message_handler = MessageHandler(
            _on_message,
            filters.chat(chat_id) & filters.user(BOT_USER_ID)
        )
        self._edit_handler = EditedMessageHandler(
            _on_edit,
            filters.chat(chat_id) & filters.user(BOT_USER_ID)
        )

        self._starter_client.add_handler(self._message_handler, group=-98)
        self._starter_client.add_handler(self._edit_handler, group=-99)

    def _unregister_handlers(self):
        if self._starter_client:
            if self._message_handler:
                try:
                    self._starter_client.remove_handler(self._message_handler, group=-98)
                except Exception:
                    pass
                self._message_handler = None
            if self._edit_handler:
                try:
                    self._starter_client.remove_handler(self._edit_handler, group=-99)
                except Exception:
                    pass
                self._edit_handler = None

    # ----------------------------------------------------------
    # Queue helpers
    # ----------------------------------------------------------

    async def _get_latest_from_queue(self, timeout=2.0):
        latest = None
        try:
            latest = await asyncio.wait_for(self._edit_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        while not self._edit_queue.empty():
            try:
                newer = self._edit_queue.get_nowait()
                latest = newer
            except asyncio.QueueEmpty:
                break
        return latest

    async def _wait_for_trigger_reply(self, timeout=30):
        """Wait for the bot's reply to the trigger message, discarding unrelated messages."""
        deadline = time.time() + timeout

        while time.time() < deadline:
            if self._abort_requested:
                return None

            remaining = max(0.1, deadline - time.time())
            msg = await self._get_latest_from_queue(timeout=min(2.0, remaining))

            if msg:
                reply_to = getattr(msg, "reply_to_message_id", None)

                if reply_to == self._trigger_message_id:
                    return msg

                # Not the trigger reply, discard
                print(f"  → Discarding non-trigger message (id={msg.id}, reply_to={reply_to})")
                continue

        return None

    async def _get_current_message(self):
        msg = await self._get_latest_from_queue(timeout=0.5)
        if msg:
            self._cached_message = msg
            return msg
        if self._starter_client and self._message_id:
            try:
                msg = await self._starter_client.get_messages(
                    self._chat_id, self._message_id
                )
                if msg:
                    self._cached_message = msg
                    return msg
            except Exception:
                pass
        return self._cached_message

    async def _wait_for_button(self, prefix, timeout=None, client=None):
        if timeout is None:
            timeout = PHASE_TIMEOUTS.get("open", 120)
        use_client = client or self._starter_client
        deadline = time.time() + timeout
        print(f"  ⏳ Waiting for button [{prefix}] (timeout={timeout}s)...")

        while time.time() < deadline:
            if self._abort_requested:
                return None

            msg = await self._get_latest_from_queue(timeout=min(2.0, max(0.1, deadline - time.time())))
            if msg:
                self._cached_message = msg
            else:
                try:
                    msg = await use_client.get_messages(self._chat_id, self._message_id)
                    if msg:
                        self._cached_message = msg
                except Exception:
                    pass

            if self._cached_message:
                btn = find_button_by_prefix(self._cached_message, prefix)
                if btn:
                    print(f"  ✅ Button [{prefix}] found")
                    return btn

        print(f"  ❌ Button [{prefix}] not found after {timeout}s")
        return None

    def _is_next_phase_up(self, current_phase):
        indicator = NEXT_PHASE_INDICATORS.get(current_phase)
        if not indicator:
            return False
        if not self._cached_message:
            return False
        text = self._cached_message.text or self._cached_message.caption or ""
        if indicator.get("button") and has_button_with_prefix(self._cached_message, indicator["button"]):
            return True
        if indicator.get("text") and indicator["text"] in text:
            return True
        return False

    def _get_completed_count(self, phase):
        if not self._cached_message:
            return None
        text = self._cached_message.text or self._cached_message.caption or ""
        if phase == "steal":
            m = STEAL_COMPLETED_RE.search(text)
        elif phase == "move":
            m = MOVE_COMPLETED_RE.search(text)
        else:
            return None
        if m:
            return int(m.group(1))
        return None

    # ----------------------------------------------------------
    # Main heist flow
    # ----------------------------------------------------------

    async def _run_heist(self, phones, level, steal_count, move_count,
                         listen_timeout, chat_id, config, click_count_mode):

        heartbeat_enabled = bool(config.get("heartbeat_log", 0))
        heartbeat_task = None
        if heartbeat_enabled:
            heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        try:
            if self._abort_requested:
                return
            await asyncio.wait_for(
                self._phase_trigger(phones[0], chat_id),
                timeout=PHASE_TIMEOUTS["trigger"]
            )

            if self._abort_requested:
                return
            await asyncio.wait_for(
                self._phase_select_loc(),
                timeout=PHASE_TIMEOUTS["select_loc"]
            )

            if self._abort_requested:
                return
            selected = await asyncio.wait_for(
                self._phase_select_level(level),
                timeout=PHASE_TIMEOUTS["select_level"]
            )
            if not selected:
                set_heist_state(self.user_id, "error", error="No levels available")
                return

            if self._abort_requested:
                return
            await asyncio.wait_for(
                self._phase_join(phones),
                timeout=PHASE_TIMEOUTS["join"]
            )

            if self._abort_requested:
                return
            await asyncio.wait_for(
                self._phase_confirm(),
                timeout=PHASE_TIMEOUTS["confirm"]
            )

            if self._abort_requested:
                return
            await asyncio.wait_for(
                self._phase_open(),
                timeout=PHASE_TIMEOUTS["open"]
            )

            if self._abort_requested:
                return
            await asyncio.wait_for(
                self._phase_steal(phones[1], steal_count, click_count_mode),
                timeout=PHASE_TIMEOUTS["steal"]
            )

            if self._abort_requested:
                return
            await asyncio.wait_for(
                self._phase_move(phones[2], move_count, click_count_mode),
                timeout=PHASE_TIMEOUTS["move"]
            )

            if self._abort_requested:
                return
            await asyncio.wait_for(
                self._phase_run(phones[3]),
                timeout=PHASE_TIMEOUTS["run"]
            )

            if self._abort_requested:
                return
            result = await asyncio.wait_for(
                self._phase_listen(listen_timeout),
                timeout=listen_timeout + 30
            )

            await self._phase_result(result, phones, level)

        except asyncio.TimeoutError:
            print(f"❌ Heist phase timeout for user {self.user_id}")
            set_heist_state(self.user_id, "timeout", error="Phase timeout")
            self._record_result("timeout", phones, level)

        except asyncio.CancelledError:
            print(f"⏹ Heist cancelled for user {self.user_id}")
            reset_heist_state(self.user_id)

        except Exception as e:
            print(f"❌ Heist error: {e}")
            import traceback
            traceback.print_exc()
            set_heist_state(self.user_id, "error", error=str(e))
            self._record_result("error", phones, level)

        finally:
            if heartbeat_task and not heartbeat_task.done():
                heartbeat_task.cancel()
            if self._abort_requested:
                reset_heist_state(self.user_id)
                add_account_log(
                    phones[0], "heist", "aborted", "warning",
                    "Heist aborted by user",
                    account_uid=get_tg_account(phones[0]).get("uid", "")
                )

    # ----------------------------------------------------------
    # Phase handlers
    # ----------------------------------------------------------

    async def _phase_trigger(self, starter_phone, chat_id):
        update_heist_state(self.user_id, state="trigger_sent")
        print(f"🎯 [{mask_phone(starter_phone)}] Sending heist trigger")

        # Register handlers BEFORE sending trigger
        self._register_handlers(chat_id, 0)

        # Send trigger
        sent = await self._starter_client.send_message(chat_id, HEIST_TRIGGER)

        # Set trigger ID for filtering
        self._trigger_message_id = sent.id

        # Wait for the actual trigger reply (filtered by reply_to_message_id)
        reply = await self._wait_for_trigger_reply(timeout=PHASE_TIMEOUTS["trigger"])

        if not reply:
            raise Exception("No bot reply to heist trigger")

        self._message_id = reply.id
        self._cached_message = reply
        update_heist_state(self.user_id, message_id=reply.id, chat_id=chat_id)

        for p in self._heist_phones:
            HEIST_ACTIVE_MESSAGES[(p, chat_id)] = reply.id

        add_account_log(
            starter_phone, "heist", "trigger_sent", "success",
            f"Heist triggered in chat {chat_id}",
            account_uid=get_tg_account(starter_phone).get("uid", "")
        )
        print(f"🎯 Bot reply received: message_id={reply.id}")

    async def _phase_select_loc(self):
        update_heist_state(self.user_id, state="loc_selected")
        print("🎯 Clicking mrob_sel_loc")
        await asyncio.sleep(random.uniform(0.5, 1.5))

        msg = await self._get_current_message()
        btn = find_button_by_prefix(msg, CB_SEL_LOC)
        if not btn:
            raise Exception("mrob_sel_loc button not found")

        cb = getattr(btn, "callback_data", None)
        if cb:
            asyncio.create_task(self._fire_click(msg, cb))
        print("✅ Location selected")

        # Wait for edit (levels shown)
        edit_msg = await self._get_latest_from_queue(timeout=PHASE_TIMEOUTS["select_loc"])
        if edit_msg:
            self._cached_message = edit_msg

    async def _phase_select_level(self, level):
        update_heist_state(self.user_id, state="level_shown")
        print(f"🎯 Selecting level {level}")
        await asyncio.sleep(random.uniform(0.5, 1.0))

        msg = await self._get_current_message()

        available_levels = []
        for btn in find_all_buttons_by_prefix(msg, CB_START_LOC):
            try:
                lvl = int(btn._callback_data.split(":")[-1])
                available_levels.append(lvl)
            except (ValueError, IndexError):
                pass

        for btn in find_all_buttons_by_prefix(msg, CB_CD_LOC):
            text = getattr(btn, "text", "") or ""
            try:
                lvl = int(btn._callback_data.split(":")[-1])
                cd_seconds = parse_cd_time(text)
                if cd_seconds > 0:
                    set_heist_cooldown(self.user_id, lvl, int(time.time()) + cd_seconds)
            except (ValueError, IndexError):
                pass

        if level in available_levels:
            target_btn = None
            for btn in find_all_buttons_by_prefix(msg, CB_START_LOC):
                try:
                    lvl = int(btn._callback_data.split(":")[-1])
                    if lvl == level:
                        target_btn = btn
                        break
                except (ValueError, IndexError):
                    pass

            if target_btn:
                cb = getattr(target_btn, "callback_data", None)
                if cb:
                    asyncio.create_task(self._fire_click(msg, cb))
                update_heist_state(self.user_id, state="level_selected", level=level)
                print(f"✅ Level {level} selected")

                await asyncio.sleep(random.uniform(0.5, 1.0))
                edit_msg = await self._get_latest_from_queue(timeout=10)
                if edit_msg:
                    self._cached_message = edit_msg
                return True

        print(f"❌ Level {level} not available. Available: {available_levels}")
        return False

    async def _phase_join(self, phones):
        update_heist_state(self.user_id, state="waiting_joins")
        print("🎯 Waiting for accounts to join")

        for i in range(1, 4):
            if self._abort_requested:
                return
            phone = phones[i]
            print(f"  → {mask_phone(phone)} joining...")

            async def join_job(client):
                msg = await client.get_messages(self._chat_id, self._message_id)
                btn = find_button_by_prefix(msg, CB_JOIN)
                if btn:
                    cb = getattr(btn, "callback_data", None)
                    if cb:
                        try:
                            await asyncio.wait_for(msg.click(callback_data=cb), timeout=5.0)
                        except Exception:
                            pass
                    return True
                return False

            try:
                result = await session_manager.run_with_client(phone, join_job)
                if result:
                    print(f"  ✅ {mask_phone(phone)} joined")
                else:
                    print(f"  ⚠️ {mask_phone(phone)} join button not found")
            except Exception as e:
                print(f"  ❌ {mask_phone(phone)} join error: {e}")

            await asyncio.sleep(random.uniform(0.5, 1.5))

        update_heist_state(self.user_id, state="all_joined")
        print("✅ All accounts joined")

    async def _phase_confirm(self):
        update_heist_state(self.user_id, state="confirmed")
        print("🎯 Confirming heist start")
        await asyncio.sleep(random.uniform(0.5, 1.0))

        msg = await self._get_current_message()
        btn = find_button_by_prefix(msg, CB_CONFIRM)
        if not btn:
            raise Exception("mrob_confirm button not found")

        cb = getattr(btn, "callback_data", None)
        if cb:
            asyncio.create_task(self._fire_click(msg, cb))
        print("✅ Heist confirmed")

        await asyncio.sleep(random.uniform(0.5, 1.0))
        edit_msg = await self._get_latest_from_queue(timeout=10)
        if edit_msg:
            self._cached_message = edit_msg

    async def _phase_open(self):
        update_heist_state(self.user_id, state="phase_open")
        print("🎯 Account 1: Opening (mrob_act_open)")

        btn = await self._wait_for_button(CB_ACT_OPEN, timeout=PHASE_TIMEOUTS["open"])
        if not btn:
            raise Exception("mrob_act_open button never appeared")

        msg = await self._get_current_message()
        open_btn = find_button_by_prefix(msg, CB_ACT_OPEN)
        if open_btn:
            cb = getattr(open_btn, "callback_data", None)
            if cb:
                asyncio.create_task(self._fire_click(msg, cb))
            print("✅ Opened")

            # Wait for bot to edit message (show steal button)
            await asyncio.sleep(random.uniform(1.0, 2.0))
            edit_msg = await self._get_latest_from_queue(timeout=10)
            if edit_msg:
                self._cached_message = edit_msg
            return

        raise Exception("mrob_act_open button not found in message")

    async def _phase_steal(self, phone, steal_count, click_count_mode):
        update_heist_state(self.user_id, state="phase_steal")
        print(f"🎯 Account 2: Stealing (count={steal_count or 'unlimited'})")

        async def steal_job(client):
            clicks = await self._click_loop(
                client=client,
                phase="steal",
                button_prefix=CB_ACT_STEAL,
                stop_prefix=CB_ACT_STOP_ST,
                max_clicks=steal_count,
                click_count_mode=click_count_mode,
            )
            return clicks

        try:
            clicks = await session_manager.run_with_client(phone, steal_job)
            print(f"✅ Steal done ({clicks} clicks)")
        except Exception as e:
            print(f"❌ Steal error: {e}")

        await asyncio.sleep(random.uniform(1.0, 2.0))

    async def _phase_move(self, phone, move_count, click_count_mode):
        update_heist_state(self.user_id, state="phase_move")
        print(f"🎯 Account 3: Moving (count={move_count or 'unlimited'})")

        async def move_job(client):
            clicks = await self._click_loop(
                client=client,
                phase="move",
                button_prefix=CB_ACT_MOVE,
                stop_prefix=CB_ACT_STOP_MV,
                max_clicks=move_count,
                click_count_mode=click_count_mode,
            )
            return clicks

        try:
            clicks = await session_manager.run_with_client(phone, move_job)
            print(f"✅ Move done ({clicks} clicks)")
        except Exception as e:
            print(f"❌ Move error: {e}")

        await asyncio.sleep(random.uniform(1.0, 2.0))

    async def _phase_run(self, phone):
        update_heist_state(self.user_id, state="phase_run")
        print("🎯 Account 4: Running (mrob_act_run)")

        async def run_job(client):
            btn = await self._wait_for_button(
                CB_ACT_RUN,
                timeout=PHASE_TIMEOUTS["run"],
                client=client
            )
            if btn:
                msg = await client.get_messages(self._chat_id, self._message_id)
                run_btn = find_button_by_prefix(msg, CB_ACT_RUN)
                if run_btn:
                    cb = getattr(run_btn, "callback_data", None)
                    if cb:
                        try:
                            await asyncio.wait_for(msg.click(callback_data=cb), timeout=5.0)
                        except Exception:
                            pass
                    print("✅ Ran")
                    return True
            print("❌ mrob_act_run never appeared")
            return False

        try:
            result = await session_manager.run_with_client(phone, run_job)
            if not result:
                raise Exception("mrob_act_run button never appeared")
        except Exception as e:
            print(f"❌ Run error: {e}")
            raise

    async def _phase_listen(self, listen_timeout):
        update_heist_state(self.user_id, state="listening")
        print(f"🎯 Listening for result (timeout={listen_timeout}s)")

        deadline = time.time() + listen_timeout

        while time.time() < deadline:
            if self._abort_requested:
                return "aborted"

            msg = await self._get_latest_from_queue(timeout=2.0)
            if msg:
                self._cached_message = msg
            else:
                try:
                    msg = await self._starter_client.get_messages(
                        self._chat_id, self._message_id
                    )
                    if msg:
                        self._cached_message = msg
                except Exception:
                    pass

            if self._cached_message:
                text = self._cached_message.text or self._cached_message.caption or ""

                if WIN_TEXT in text:
                    print("🏆 HEIST WON!")
                    return "won"

                if LOSE_TEXT in text:
                    print("💀 HEIST LOST!")
                    return "lost"

                if JAIL_TEXT_MARKER in text and "زندانی" in text:
                    print("💀 HEIST LOST (detected via jail marker)!")
                    return "lost"

        print("⏰ Listen timeout")
        return "timeout"

    async def _phase_result(self, result, phones, level):
        duration = int(time.time()) - self._start_time

        if result == "won":
            update_heist_state(self.user_id, state="won")
        elif result == "lost":
            update_heist_state(self.user_id, state="lost")
            msg = await self._get_current_message()
            if msg:
                text = msg.text or msg.caption or ""
                jail_seconds = parse_jail_duration(text)
                if jail_seconds > 0:
                    jail_until = int(time.time()) + jail_seconds
                    for phone in phones:
                        set_account_jail(phone, jail_until, "heist_loss")
                    print(f"⛓️ All accounts jailed for {jail_seconds}s")
        elif result == "timeout":
            update_heist_state(self.user_id, state="timeout")
        else:
            update_heist_state(self.user_id, state="error")

        cooldown_seconds = HEIST_COOLDOWN_SECONDS.get(level, 2 * 3600)
        cooldown_until = int(time.time()) + cooldown_seconds
        set_heist_cooldown(self.user_id, level, cooldown_until)

        self._record_result(result, phones, level, duration)

        starter_account = get_tg_account(phones[0])
        if starter_account:
            add_account_log(
                phones[0], "heist", "result",
                "success" if result == "won" else "error",
                f"Heist {result} | Level {level} | Duration {duration}s",
                account_uid=starter_account.get("uid", "")
            )

        print(f"🎯 Heist finished: {result} | Level {level} | Duration {duration}s")

    async def _fire_click(self, message, callback_data):
        """Fire-and-forget click. Sends the click, ignores the response."""
        try:
            await message.click(callback_data=callback_data)
        except Exception:
            pass

    # ----------------------------------------------------------
    # Click loop with verification
    # ----------------------------------------------------------

    async def _click_loop(self, client, phase, button_prefix, stop_prefix,
                          max_clicks=0, click_count_mode="local"):
        phase_timeout = PHASE_TIMEOUTS.get(phase, 120)

        # Wait for the button to appear before starting
        btn = await self._wait_for_button(
            button_prefix,
            timeout=phase_timeout,
            client=client
        )
        if not btn:
            print(f"  ❌ {button_prefix} never appeared for phase [{phase}]")
            return 0

        clicks = 0
        last_completed = self._get_completed_count(phase)

        while True:
            if self._abort_requested:
                break

            # Check if next phase is up
            if self._is_next_phase_up(phase):
                print(f"  → Next phase detected, ending {phase}")
                break

            # Find button in cached message
            btn = find_button_by_prefix(self._cached_message, button_prefix)

            if not btn:
                if clicks >= 1:
                    print(f"  → {button_prefix} gone after {clicks} clicks")
                    break
                else:
                    # Button gone before any clicks, wait for it
                    btn = await self._wait_for_button(
                        button_prefix, timeout=15, client=client
                    )
                    if not btn:
                        print(f"  → {button_prefix} never appeared")
                        break

            # Check stop condition BEFORE clicking
            if max_clicks > 0 and clicks >= max_clicks:
                print(f"  → Reached {max_clicks} clicks, clicking stop")
                stop_btn = find_button_by_prefix(self._cached_message, stop_prefix)
                if stop_btn:
                    cb = getattr(stop_btn, "callback_data", None)
                    if cb:
                        asyncio.create_task(self._fire_click(self._cached_message, cb))
                break

            # Fire click, don't wait for response
            cb = getattr(btn, "callback_data", None)
            if cb:
                asyncio.create_task(self._fire_click(self._cached_message, cb))
            clicks += 1

            # Tiny gap between clicks
            await asyncio.sleep(CLICK_DELAY)

            # Check state from queue
            msg = await self._get_latest_from_queue(timeout=EDIT_WAIT_TIMEOUT)
            if msg:
                self._cached_message = msg

            # Verify click worked
            new_completed = self._get_completed_count(phase)
            if new_completed is not None and last_completed is not None:
                if new_completed <= last_completed and clicks >= 2:
                    # Click might have failed, retry once
                    btn = find_button_by_prefix(self._cached_message, button_prefix)
                    if btn:
                        cb = getattr(btn, "callback_data", None)
                        if cb:
                            asyncio.create_task(self._fire_click(self._cached_message, cb))
                        await asyncio.sleep(CLICK_DELAY)
                        msg = await self._get_latest_from_queue(timeout=EDIT_WAIT_TIMEOUT)
                        if msg:
                            self._cached_message = msg
                        new_completed = self._get_completed_count(phase)

                if new_completed is not None:
                    last_completed = new_completed

            # Unlimited mode: check if button is gone
            if max_clicks == 0:
                btn = find_button_by_prefix(self._cached_message, button_prefix)
                if not btn and clicks >= 1:
                    if self._is_next_phase_up(phase):
                        print(f"  → Next phase up, ending {phase}")
                        break
                    # Wait a moment and recheck
                    await asyncio.sleep(0.5)
                    msg = await self._get_latest_from_queue(timeout=2.0)
                    if msg:
                        self._cached_message = msg
                    btn = find_button_by_prefix(self._cached_message, button_prefix)
                    if not btn:
                        print(f"  → Button gone after {clicks} clicks (unlimited)")
                        break

        return clicks

    # ----------------------------------------------------------
    # Heartbeat
    # ----------------------------------------------------------

    async def _heartbeat_loop(self):
        while self._running and not self._abort_requested:
            state = get_heist_state(self.user_id)
            add_account_log(
                "", "heist", "heartbeat", "info",
                f"state={state['state']} steal={state['steal_clicks_done']} move={state['move_clicks_done']}",
            )
            await asyncio.sleep(30)

    # ----------------------------------------------------------
    # Cooldown check
    # ----------------------------------------------------------

    async def check_cooldowns(self):
        config = get_heist_config(self.user_id)
        accounts = get_heist_accounts(self.user_id)
        if len(accounts) < 1:
            return {}
        chat_id = await self._resolve_chat_id(config)
        if not chat_id:
            return {}
        starter_phone = accounts[0]["phone"]

        try:
            client = await session_manager.get_client(starter_phone)
            try:
                sent = await client.send_message(chat_id, HEIST_TRIGGER)

                # Wait for reply
                reply = None
                deadline = time.time() + 30
                while time.time() < deadline:
                    try:
                        async for msg in client.get_chat_history(chat_id, limit=10):
                            if (getattr(msg, "reply_to_message_id", None) == sent.id and
                                    msg.from_user and msg.from_user.id == BOT_USER_ID):
                                reply = msg
                                break
                    except Exception:
                        pass
                    if reply:
                        break
                    await asyncio.sleep(0.5)

                if not reply:
                    return {}

                btn = find_button_by_prefix(reply, CB_SEL_LOC)
                if btn:
                    await click_heist_button(client, reply, btn)
                    await asyncio.sleep(2)

                msg = await client.get_messages(chat_id, reply.id)
                cooldowns = {}

                for cb_btn in find_all_buttons_by_prefix(msg, CB_CD_LOC):
                    cb = cb_btn._callback_data
                    text = getattr(cb_btn, "text", "") or ""
                    try:
                        lvl = int(cb.split(":")[-1])
                        cd_seconds = parse_cd_time(text)
                        cooldowns[lvl] = cd_seconds
                        if cd_seconds > 0:
                            set_heist_cooldown(self.user_id, lvl, int(time.time()) + cd_seconds)
                    except (ValueError, IndexError):
                        pass

                for cb_btn in find_all_buttons_by_prefix(msg, CB_START_LOC):
                    try:
                        lvl = int(cb_btn._callback_data.split(":")[-1])
                        cooldowns[lvl] = 0
                    except (ValueError, IndexError):
                        pass

                return cooldowns
            finally:
                await session_manager.release_client(starter_phone)
        except Exception as e:
            print(f"❌ Cooldown check error: {e}")
            return {}

    # ----------------------------------------------------------
    # Helpers
    # ----------------------------------------------------------

    def _record_result(self, result, phones, level, duration=0):
        if not duration:
            duration = int(time.time()) - self._start_time
        account_uids = []
        for p in phones:
            acc = get_tg_account(p)
            if acc:
                account_uids.append(acc.get("uid", ""))
        add_heist_log(self.user_id, level, result, duration, account_uids)


# ============================================================
# Orchestrator registry
# ============================================================

_orchestrators = {}


def get_orchestrator(user_id):
    if user_id not in _orchestrators:
        _orchestrators[user_id] = HeistOrchestrator(user_id)
    return _orchestrators[user_id]


def is_heist_running(user_id):
    orch = _orchestrators.get(user_id)
    return orch is not None and orch.is_running()


# ============================================================
# Auto mode loop
# ============================================================

async def heist_auto_loop(user_id, check_interval=60):
    while True:
        await asyncio.sleep(check_interval)
        try:
            config = get_heist_config(user_id)
            if not config.get("auto_enabled"):
                continue
            if is_heist_running(user_id):
                continue
            now = int(time.time())
            cooldowns = get_all_heist_cooldowns(user_id)
            auto_mode = config.get("auto_level_mode", "best_available")
            target_level = None

            if auto_mode == "best_available":
                for lvl in (1, 2, 3):
                    if cooldowns.get(lvl, 0) <= now:
                        target_level = lvl
                        break
            else:
                lvl = config.get("selected_level", 1)
                if cooldowns.get(lvl, 0) <= now:
                    target_level = lvl

            if target_level is None:
                continue

            accounts = get_heist_accounts(user_id)
            if len(accounts) != 4:
                continue

            all_available = True
            for acc in accounts:
                account = get_tg_account(acc["phone"])
                if not account or not account.get("is_active"):
                    all_available = False
                    break
                if int(account.get("jail_until") or 0) > now:
                    all_available = False
                    break

            if not all_available:
                continue

            jitter = random.uniform(5, 15)
            print(f"🎯 Auto heist triggering in {jitter:.1f}s (level {target_level})")
            await asyncio.sleep(jitter)

            orch = get_orchestrator(user_id)
            await orch.start(level=target_level)

        except Exception as e:
            print(f"❌ heist_auto_loop error: {e}")
            await asyncio.sleep(30)


# ============================================================
# Jail check
# ============================================================

async def check_jail_status(phone, chat_id):
    try:
        async def jail_job(client):
            sent = await client.send_message(chat_id, HEIST_JAIL_CHECK)
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    async for msg in client.get_chat_history(chat_id, limit=5):
                        if (getattr(msg, "reply_to_message_id", None) == sent.id and
                                msg.from_user and msg.from_user.id == BOT_USER_ID):
                            text = msg.text or msg.caption or ""
                            if JAIL_TEXT_MARKER in text:
                                jail_seconds = parse_jail_duration(text)
                                return True, jail_seconds
                            else:
                                return False, 0
                except Exception:
                    pass
                await asyncio.sleep(0.5)
            return False, 0

        result = await session_manager.run_with_client(phone, jail_job)
        return result
    except Exception as e:
        print(f"❌ check_jail_status error: {e}")
        return False, 0