import re
import time
import asyncio
import random

from pyrogram.errors import FloodWait

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
LOSE_TEXT = "شما پیشی بدی بودین و زندانی شدین"
JAIL_TEXT_MARKER = "زندان میویی"

JAIL_DURATION_RE = re.compile(
    r"مدت حبس\s*[:：]\s*(\d{1,3}(?:[:：]\d{1,2}){1,2})"
)

CD_TIME_RE = re.compile(
    r"(\d{1,3}(?:[:：]\d{1,2}){1,2})"
)

HEIST_COOLDOWN_SECONDS = {
    1: 2 * 3600,
    2: 5 * 3600,
    3: 8 * 3600,
}

HEIST_STATES = [
    "idle", "trigger_sent", "loc_selected", "level_shown",
    "level_selected", "waiting_joins", "all_joined", "confirmed",
    "phase_open", "phase_steal", "phase_move", "phase_run",
    "listening", "won", "lost", "timeout", "error", "cooldown",
]

# Tracks messages actively used by heist so normal handlers skip them
HEIST_TRACKED_MESSAGES = set()

# Maps (phone, chat_id) -> message_id for active heist messages
HEIST_ACTIVE_MESSAGES = {}


# ============================================================
# Time parsing helpers
# ============================================================

def parse_hms_to_seconds(text):
    """Parse 'H:MM:SS' or 'MM:SS' to total seconds."""
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
    """Extract jail duration in seconds from loss message text."""
    m = JAIL_DURATION_RE.search(text)
    if m:
        return parse_hms_to_seconds(m.group(1))
    return 0


def parse_cd_time(text):
    """Extract cooldown time from mrob_cd_loc button text."""
    m = CD_TIME_RE.search(text)
    if m:
        return parse_hms_to_seconds(m.group(1))
    return 0


# ============================================================
# Button helpers
# ============================================================

def find_button_by_prefix(message, prefix):
    """Find first inline button whose callback_data starts with prefix."""
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
    """Find all inline buttons whose callback_data starts with prefix."""
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
    """Click a button with FloodWait handling."""
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
    """
    Drives the heist state machine for a single user.
    Only one orchestrator runs at a time per user.
    """

    def __init__(self, user_id, loop=None):
        self.user_id = user_id
        self._running = False
        self._abort_requested = False
        self._starter_client = None
        self._cached_message = None
        self._message_id = 0
        self._chat_id = 0
        self._start_time = 0
        self._heist_phones = []

    # ----------------------------------------------------------
    # Public API
    # ----------------------------------------------------------

    async def start(self, level=None, steal_count=None, move_count=None):
        """Start the heist with optional per-run overrides."""
        state = get_heist_state(self.user_id)

        if state["state"] not in ("idle", "cooldown", "error", "won", "lost", "timeout"):
            print(f"⚠️ Heist already running for user {self.user_id}")
            return False

        config = get_heist_config(self.user_id)
        accounts = get_heist_accounts(self.user_id)

        if len(accounts) != 4:
            print(f"❌ Heist requires exactly 4 accounts, got {len(accounts)}")
            return False

        # Pre-flight validation
        if not await self._preflight_check(accounts):
            return False

        # Apply overrides
        effective_level = level if level is not None else config.get("selected_level", 1)
        effective_steal = steal_count if steal_count is not None else config.get("steal_count", 0)
        effective_move = move_count if move_count is not None else config.get("move_count", 0)
        phase_timeout = config.get("phase_timeout", 300)
        listen_timeout = config.get("listen_timeout", 600)

        # Determine chat_id
        chat_id = await self._resolve_chat_id(config)
        if not chat_id:
            print(f"❌ No valid heist chat for user {self.user_id}")
            return False

        self._chat_id = chat_id
        self._running = True
        self._abort_requested = False
        self._start_time = int(time.time())
        self._cached_message = None

        phones = [a["phone"] for a in accounts]

        # Track heist messages so normal handlers skip them
        for p in phones:
            HEIST_TRACKED_MESSAGES.add((p, chat_id))

        set_heist_state(self.user_id, "trigger_sent", chat_id=chat_id, level=effective_level)

        # Will be populated when trigger message is received
        self._heist_phones = phones

        try:
            # Acquire starter client
            starter_phone = phones[0]
            self._starter_client = await session_manager.get_client(starter_phone)

            try:
                await self._run_heist(
                    phones=phones,
                    level=effective_level,
                    steal_count=effective_steal,
                    move_count=effective_move,
                    phase_timeout=phase_timeout,
                    listen_timeout=listen_timeout,
                    chat_id=chat_id,
                    config=config,
                )
            finally:
                # Release starter client
                if self._starter_client:
                    try:
                        await session_manager.release_client(starter_phone)
                    except Exception:
                        pass
                    self._starter_client = None

        except Exception as e:
            print(f"❌ Heist orchestrator error: {e}")
            set_heist_state(self.user_id, "error", error=str(e))

        finally:
            self._running = False

            # Remove tracking
            for p in phones:
                HEIST_TRACKED_MESSAGES.discard((p, chat_id))
                HEIST_ACTIVE_MESSAGES.pop((p, chat_id), None)

        return True

    async def abort(self):
        """Gracefully abort the heist."""
        self._abort_requested = True
        print(f"⏹ Heist abort requested for user {self.user_id}")

    def is_running(self):
        return self._running and not self._abort_requested

    # ----------------------------------------------------------
    # Pre-flight checks
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

        # Check shared cooldown for all levels
        cooldowns = get_all_heist_cooldowns(self.user_id)
        all_on_cooldown = True

        for lvl in (1, 2, 3):
            cd_until = cooldowns.get(lvl, 0)
            if cd_until <= now:
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
    # Main heist flow
    # ----------------------------------------------------------

    async def _run_heist(self, phones, level, steal_count, move_count,
                         phase_timeout, listen_timeout, chat_id, config):

        heartbeat_enabled = bool(config.get("heartbeat_log", 0))
        heartbeat_task = None

        if heartbeat_enabled:
            heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        try:
            # Phase 1: Trigger
            if self._abort_requested:
                return
            await asyncio.wait_for(
                self._phase_trigger(phones[0], chat_id),
                timeout=phase_timeout
            )

            # Phase 2: Select location
            if self._abort_requested:
                return
            await asyncio.wait_for(
                self._phase_select_loc(),
                timeout=phase_timeout
            )

            # Phase 3: Select level
            if self._abort_requested:
                return
            selected = await asyncio.wait_for(
                self._phase_select_level(level),
                timeout=phase_timeout
            )

            if not selected:
                set_heist_state(self.user_id, "error", error="No levels available")
                return

            # Phase 4: Join
            if self._abort_requested:
                return
            await asyncio.wait_for(
                self._phase_join(phones),
                timeout=phase_timeout
            )

            # Phase 5: Confirm
            if self._abort_requested:
                return
            await asyncio.wait_for(
                self._phase_confirm(),
                timeout=phase_timeout
            )

            # Phase 6: Open (account 1)
            if self._abort_requested:
                return
            await asyncio.wait_for(
                self._phase_open(),
                timeout=phase_timeout
            )

            # Phase 7: Steal (account 2)
            if self._abort_requested:
                return
            await asyncio.wait_for(
                self._phase_steal(phones[1], steal_count),
                timeout=phase_timeout
            )

            # Phase 8: Move (account 3)
            if self._abort_requested:
                return
            await asyncio.wait_for(
                self._phase_move(phones[2], move_count),
                timeout=phase_timeout
            )

            # Phase 9: Run (account 4)
            if self._abort_requested:
                return
            await asyncio.wait_for(
                self._phase_run(phones[3]),
                timeout=phase_timeout
            )

            # Phase 10: Listen for result
            if self._abort_requested:
                return
            result = await asyncio.wait_for(
                self._phase_listen(listen_timeout),
                timeout=listen_timeout + 30
            )

            # Phase 11: Handle result
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

        sent = await self._starter_client.send_message(chat_id, HEIST_TRIGGER)

        # Wait for bot reply
        reply = await self._wait_for_reply_to(sent.id, chat_id, timeout=30)

        if not reply:
            raise Exception("No bot reply to heist trigger")

        self._message_id = reply.id
        self._cached_message = reply
        update_heist_state(self.user_id, message_id=reply.id, chat_id=chat_id)

        # Track heist message
        for p in getattr(self, '_heist_phones', []):
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

        await click_heist_button(self._starter_client, msg, btn)

        # Wait for message edit (levels shown)
        await self._wait_for_edit(timeout=15)

    async def _phase_select_level(self, level):
        update_heist_state(self.user_id, state="level_shown")
        print(f"🎯 Selecting level {level}")

        await asyncio.sleep(random.uniform(0.5, 1.0))

        msg = await self._get_current_message()

        # Check if the desired level is available
        start_prefix = f"{CB_START_LOC}"
        cd_prefix = f"{CB_CD_LOC}"

        # Parse all level buttons
        available_levels = []
        cooldown_levels = {}

        for btn in find_all_buttons_by_prefix(msg, CB_START_LOC):
            cb = btn._callback_data
            try:
                lvl = int(cb.split(":")[-1])
                available_levels.append(lvl)
            except (ValueError, IndexError):
                pass

        for btn in find_all_buttons_by_prefix(msg, CB_CD_LOC):
            cb = btn._callback_data
            text = getattr(btn, "text", "") or ""
            try:
                lvl = int(cb.split(":")[-1])
                cd_seconds = parse_cd_time(text)
                cooldown_levels[lvl] = cd_seconds

                # Store cooldown in DB
                if cd_seconds > 0:
                    set_heist_cooldown(
                        self.user_id, lvl,
                        int(time.time()) + cd_seconds
                    )
            except (ValueError, IndexError):
                pass

        if level in available_levels:
            # Click the desired level
            target_btn = None
            for btn in find_all_buttons_by_prefix(msg, CB_START_LOC):
                cb = btn._callback_data
                try:
                    lvl = int(cb.split(":")[-1])
                    if lvl == level:
                        target_btn = btn
                        break
                except (ValueError, IndexError):
                    pass

            if target_btn:
                await click_heist_button(self._starter_client, msg, target_btn)
                update_heist_state(self.user_id, state="level_selected", level=level)
                print(f"✅ Level {level} selected")
                await asyncio.sleep(random.uniform(0.5, 1.0))
                return True

        # Desired level not available
        print(f"❌ Level {level} not available. Available: {available_levels}")
        return False

    async def _phase_join(self, phones):
        update_heist_state(self.user_id, state="waiting_joins")
        print("🎯 Waiting for accounts to join")

        # Accounts 2, 3, 4 join in order
        for i in range(1, 4):
            if self._abort_requested:
                return

            phone = phones[i]
            print(f"  → {mask_phone(phone)} joining...")

            async def join_job(client):
                msg = await client.get_messages(self._chat_id, self._message_id)
                btn = find_button_by_prefix(msg, CB_JOIN)
                if btn:
                    await click_heist_button(client, msg, btn)
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

        await click_heist_button(self._starter_client, msg, btn)
        print("✅ Heist confirmed")
        await asyncio.sleep(random.uniform(0.5, 1.0))

    async def _phase_open(self):
        update_heist_state(self.user_id, state="phase_open")
        print("🎯 Account 1: Opening (mrob_act_open)")

        await self._wait_for_edit(timeout=15)

        msg = await self._get_current_message()
        btn = find_button_by_prefix(msg, CB_ACT_OPEN)

        if btn:
            await click_heist_button(self._starter_client, msg, btn)
            print("✅ Opened")
        else:
            print("⚠️ mrob_act_open not found, continuing")

        await asyncio.sleep(random.uniform(0.5, 1.0))

    async def _phase_steal(self, phone, steal_count):
        update_heist_state(self.user_id, state="phase_steal")
        print(f"🎯 Account 2: Stealing (count={steal_count})")

        clicks_done = 0

        async def steal_job(client):
            nonlocal clicks_done

            while True:
                if self._abort_requested:
                    break

                msg = await client.get_messages(self._chat_id, self._message_id)

                steal_btn = find_button_by_prefix(msg, CB_ACT_STEAL)

                if not steal_btn:
                    print("  → mrob_act_steal gone, steal phase done")
                    break

                await click_heist_button(client, msg, steal_btn)
                clicks_done += 1
                update_heist_state(self.user_id, steal_clicks_done=clicks_done)

                if steal_count > 0 and clicks_done >= steal_count:
                    print(f"  → Reached steal count ({steal_count}), stopping")
                    # Click stop button
                    msg = await client.get_messages(self._chat_id, self._message_id)
                    stop_btn = find_button_by_prefix(msg, CB_ACT_STOP_ST)
                    if stop_btn:
                        await click_heist_button(client, msg, stop_btn)
                    break

                await asyncio.sleep(random.uniform(0.3, 0.8))

        try:
            await session_manager.run_with_client(phone, steal_job)
        except Exception as e:
            print(f"❌ Steal error: {e}")

        print(f"✅ Steal done ({clicks_done} clicks)")
        await asyncio.sleep(random.uniform(0.5, 1.0))

    async def _phase_move(self, phone, move_count):
        update_heist_state(self.user_id, state="phase_move")
        print(f"🎯 Account 3: Moving (count={move_count})")

        clicks_done = 0

        async def move_job(client):
            nonlocal clicks_done

            while True:
                if self._abort_requested:
                    break

                msg = await client.get_messages(self._chat_id, self._message_id)

                move_btn = find_button_by_prefix(msg, CB_ACT_MOVE)

                if not move_btn:
                    print("  → mrob_act_move gone, move phase done")
                    break

                await click_heist_button(client, msg, move_btn)
                clicks_done += 1
                update_heist_state(self.user_id, move_clicks_done=clicks_done)

                if move_count > 0 and clicks_done >= move_count:
                    print(f"  → Reached move count ({move_count}), stopping")
                    msg = await client.get_messages(self._chat_id, self._message_id)
                    stop_btn = find_button_by_prefix(msg, CB_ACT_STOP_MV)
                    if stop_btn:
                        await click_heist_button(client, msg, stop_btn)
                    break

                await asyncio.sleep(random.uniform(0.3, 0.8))

        try:
            await session_manager.run_with_client(phone, move_job)
        except Exception as e:
            print(f"❌ Move error: {e}")

        print(f"✅ Move done ({clicks_done} clicks)")
        await asyncio.sleep(random.uniform(0.5, 1.0))

    async def _phase_run(self, phone):
        update_heist_state(self.user_id, state="phase_run")
        print("🎯 Account 4: Running (mrob_act_run)")

        async def run_job(client):
            # Wait for the run button to appear
            for _ in range(30):
                if self._abort_requested:
                    return

                msg = await client.get_messages(self._chat_id, self._message_id)
                run_btn = find_button_by_prefix(msg, CB_ACT_RUN)

                if run_btn:
                    await click_heist_button(client, msg, run_btn)
                    print("✅ Ran")
                    return

                await asyncio.sleep(0.5)

            print("⚠️ mrob_act_run not found after waiting")

        try:
            await session_manager.run_with_client(phone, run_job)
        except Exception as e:
            print(f"❌ Run error: {e}")

    async def _phase_listen(self, listen_timeout):
        update_heist_state(self.user_id, state="listening")
        print(f"🎯 Listening for result (timeout={listen_timeout}s)")

        deadline = time.time() + listen_timeout

        while time.time() < deadline:
            if self._abort_requested:
                return "aborted"

            msg = await self._get_current_message()

            if msg:
                text = msg.text or msg.caption or ""

                if WIN_TEXT in text:
                    print("🏆 HEIST WON!")
                    return "won"

                if LOSE_TEXT in text:
                    print("💀 HEIST LOST!")
                    return "lost"

            await asyncio.sleep(1)

        print("⏰ Listen timeout")
        return "timeout"

    async def _phase_result(self, result, phones, level):
        duration = int(time.time()) - self._start_time

        if result == "won":
            update_heist_state(self.user_id, state="won")

        elif result == "lost":
            update_heist_state(self.user_id, state="lost")

            # Parse jail time from the message
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

        # Set shared cooldown
        cooldown_seconds = HEIST_COOLDOWN_SECONDS.get(level, 2 * 3600)
        cooldown_until = int(time.time()) + cooldown_seconds
        set_heist_cooldown(self.user_id, level, cooldown_until)

        # Record log
        self._record_result(result, phones, level, duration)

        # Notification
        starter_account = get_tg_account(phones[0])
        if starter_account:
            add_account_log(
                phones[0], "heist", "result", 
                "success" if result == "won" else "error",
                f"Heist {result} | Level {level} | Duration {duration}s",
                account_uid=starter_account.get("uid", "")
            )

        print(f"🎯 Heist finished: {result} | Level {level} | Duration {duration}s")

    # ----------------------------------------------------------
    # Cooldown check (probe)
    # ----------------------------------------------------------

    async def check_cooldowns(self):
        """
        Send trigger, click through to level selection,
        parse cooldowns, then abort without starting.
        """
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
                reply = await self._wait_for_reply_to_with_client(
                    client, sent.id, chat_id, timeout=30
                )

                if not reply:
                    return {}

                # Click sel_loc
                btn = find_button_by_prefix(reply, CB_SEL_LOC)
                if btn:
                    await click_heist_button(client, reply, btn)
                    await asyncio.sleep(2)

                # Re-fetch message
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
                            set_heist_cooldown(
                                self.user_id, lvl,
                                int(time.time()) + cd_seconds
                            )
                    except (ValueError, IndexError):
                        pass

                # Check available levels
                for cb_btn in find_all_buttons_by_prefix(msg, CB_START_LOC):
                    cb = cb_btn._callback_data
                    try:
                        lvl = int(cb.split(":")[-1])
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
    # Message cache / edit handler  →  REPLACE THIS ENTIRE SECTION
    # ----------------------------------------------------------

    def _register_edit_handler(self, chat_id, message_id):
        # No longer needed — polling replaces the handler
        pass

    def _unregister_edit_handler(self):
        # No longer needed
        pass

    async def _get_current_message(self):
        """Always re-fetch the message to get the latest state."""
        if self._starter_client and self._message_id:
            try:
                msg = await self._starter_client.get_messages(
                    self._chat_id, self._message_id
                )
                if msg:
                    self._cached_message = msg
                    return msg
            except Exception as e:
                print(f"⚠️ Failed to fetch heist message: {e}")

        return self._cached_message

    async def _wait_for_edit(self, timeout=15):
        """
        Wait for the message to be edited by polling.
        Detects changes by comparing button count and text.
        """
        initial_buttons = self._count_buttons(self._cached_message)
        initial_text = ""
        if self._cached_message:
            initial_text = self._cached_message.text or self._cached_message.caption or ""

        deadline = time.time() + timeout

        while time.time() < deadline:
            if self._abort_requested:
                return

            try:
                msg = await self._starter_client.get_messages(
                    self._chat_id, self._message_id
                )

                if msg:
                    current_buttons = self._count_buttons(msg)
                    current_text = msg.text or msg.caption or ""

                    if current_buttons != initial_buttons or current_text != initial_text:
                        self._cached_message = msg
                        print(f"  → Message edit detected (buttons: {initial_buttons}→{current_buttons})")
                        return
            except Exception:
                pass

            await asyncio.sleep(0.5)

        print(f"  → Wait for edit timed out after {timeout}s")

    def _count_buttons(self, message):
        """Count total inline buttons on a message."""
        if not message:
            return 0

        rm = getattr(message, "reply_markup", None)
        if not rm:
            return 0

        rows = getattr(rm, "inline_keyboard", None)
        if not rows:
            return 0

        return sum(len(row) for row in rows if row)

    async def _wait_for_reply_to(self, message_id, chat_id, timeout=30):
        """Wait for a bot reply to a specific message using starter client."""
        return await self._wait_for_reply_to_with_client(
            self._starter_client, message_id, chat_id, timeout
        )

    async def _wait_for_reply_to_with_client(self, client, message_id, chat_id, timeout=30):
        """Wait for a bot reply to a specific message."""
        deadline = time.time() + timeout

        while time.time() < deadline:
            try:
                # Fetch recent messages and look for a reply
                async for msg in client.get_chat_history(chat_id, limit=20):
                    reply_to = getattr(msg, "reply_to_message_id", None)

                    if reply_to == message_id:
                        from_user = getattr(msg, "from_user", None)
                        if from_user and from_user.id == BOT_USER_ID:
                            return msg
            except Exception as e:
                print(f"⚠️ _wait_for_reply error: {e}")

            await asyncio.sleep(0.5)

        return None

    # ----------------------------------------------------------
    # Heartbeat
    # ----------------------------------------------------------

    async def _heartbeat_loop(self):
        """Periodic heartbeat log during active heist."""
        while self._running and not self._abort_requested:
            state = get_heist_state(self.user_id)
            add_account_log(
                "", "heist", "heartbeat", "info",
                f"state={state['state']} steal={state['steal_clicks_done']} move={state['move_clicks_done']}",
            )
            await asyncio.sleep(30)

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

        add_heist_log(
            self.user_id, level, result, duration, account_uids
        )


# ============================================================
# Orchestrator registry (one per user)
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
    """
    Background loop that checks if auto heist should trigger.
    Runs when auto_enabled is set in heist_config.
    """
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

            # Determine which level to attempt
            auto_mode = config.get("auto_level_mode", "best_available")
            target_level = None

            if auto_mode == "best_available":
                for lvl in (1, 2, 3):
                    cd_until = cooldowns.get(lvl, 0)
                    if cd_until <= now:
                        target_level = lvl
                        break
            else:
                lvl = config.get("selected_level", 1)
                cd_until = cooldowns.get(lvl, 0)
                if cd_until <= now:
                    target_level = lvl

            if target_level is None:
                continue

            # Check accounts
            accounts = get_heist_accounts(user_id)
            if len(accounts) != 4:
                continue

            all_available = True
            for acc in accounts:
                account = get_tg_account(acc["phone"])
                if not account:
                    all_available = False
                    break
                if not account.get("is_active"):
                    all_available = False
                    break
                if int(account.get("jail_until") or 0) > now:
                    all_available = False
                    break

            if not all_available:
                continue

            # Jitter
            jitter = random.uniform(5, 15)
            print(f"🎯 Auto heist triggering in {jitter:.1f}s (level {target_level})")
            await asyncio.sleep(jitter)

            orch = get_orchestrator(user_id)
            await orch.start(level=target_level)

        except Exception as e:
            print(f"❌ heist_auto_loop error: {e}")
            await asyncio.sleep(30)


# ============================================================
# Jail check via probe
# ============================================================

async def check_jail_status(phone, chat_id):
    """
    Send 'زندان میویی' to check if account is in jail.
    Returns (is_jailed, jail_seconds).
    """
    try:
        async def jail_job(client):
            sent = await client.send_message(chat_id, HEIST_JAIL_CHECK)

            # Wait for reply
            deadline = time.time() + 15
            while time.time() < deadline:
                try:
                    async for msg in client.get_chat_history(chat_id, limit=5):
                        if (
                            getattr(msg, "reply_to_message_id", None) == sent.id
                            and msg.from_user
                            and msg.from_user.id == BOT_USER_ID
                        ):
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