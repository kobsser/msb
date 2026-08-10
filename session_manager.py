import asyncio
import re
import time

from pyrogram import Client

try:
    from pyrogram.enums import ChatType
except ImportError:
    ChatType = None

from config import API_ID, API_HASH
from database import get_tg_account, get_setting_int
import optimizations


CLIENT_STATES = {}

_manager_lock = None


def _get_manager_lock():
    global _manager_lock

    if _manager_lock is None:
        _manager_lock = asyncio.Lock()

    return _manager_lock


def _safe_name(phone: str) -> str:
    return "sb_" + re.sub(r"\W+", "_", str(phone))


def _is_connected(client) -> bool:
    try:
        value = getattr(client, "is_connected", False)

        if callable(value):
            return bool(value())

        return bool(value)

    except:
        return False


async def get_client(phone: str, setup=None):
    """
    Returns a shared Pyrogram client for this Telegram account.

    Only one client is allowed per phone number.

    setup:
        Optional sync/async function that receives the client.
        Used by workers to add message handlers.
    """
    phone = str(phone)
    manager_lock = _get_manager_lock()

    async with manager_lock:
        state = CLIENT_STATES.get(phone)

        if not state:
            state = {
                "client": None,
                "lock": asyncio.Lock(),
                "refs": 0,
                "setup_done": False,
                "last_stop": 0.0,
            }
            CLIENT_STATES[phone] = state

    async with state["lock"]:
        # If no client exists, respect stop cooldown to avoid fast session reuse
        if state.get("client") is None:
            try:
                stop_cooldown = get_setting_int("STOP_COOLDOWN_SECONDS", 5)
            except:
                stop_cooldown = 5

            stop_cooldown = max(0, int(stop_cooldown))

            elapsed = time.time() - state.get("last_stop", 0.0)
            wait_seconds = stop_cooldown - elapsed

            if wait_seconds > 0:
                print(f"⏳ Waiting {wait_seconds:.1f}s before starting client for {phone}")
                await asyncio.sleep(wait_seconds)

        client = state.get("client")

        # If client exists but disconnected, try reconnecting
        if client is not None and not _is_connected(client):
            try:
                await client.connect()
            except Exception as e:
                print(f"⚠️ Reconnect failed for {phone}: {e}")

                # If nobody is using it, destroy and recreate
                if state.get("refs", 0) == 0:
                    try:
                        await client.stop()
                    except:
                        pass

                    state["client"] = None
                    state["setup_done"] = False
                    state["last_stop"] = time.time()
                    client = None
                else:
                    raise

        created = False

        # Create client if needed
        if client is None:
            user = get_tg_account(phone)

            if not user:
                raise ValueError(f"Account not found: {phone}")

            if not user.get("session_string"):
                raise ValueError(f"No session string for account: {phone}")

            client = Client(
                name=_safe_name(phone),
                session_string=user["session_string"],
                api_id=API_ID,
                api_hash=API_HASH,
                in_memory=True
            )

            try:
                await client.start()
            except Exception as e:
                try:
                    await client.stop()
                except:
                    pass

                state["last_stop"] = time.time()
                raise e

            state["client"] = client
            state["setup_done"] = False
            created = True

        # Run optional setup, e.g. adding handlers
        if setup is not None and not state.get("setup_done"):
            try:
                result = setup(client)

                if asyncio.iscoroutine(result):
                    await result

                state["setup_done"] = True

            except Exception as e:
                if created:
                    try:
                        await client.stop()
                    except:
                        pass

                    state["client"] = None
                    state["setup_done"] = False
                    state["last_stop"] = time.time()

                raise e

        state["refs"] = state.get("refs", 0) + 1

        return state["client"]


async def release_client(phone: str, stop_if_idle: bool = True):
    """
    Releases one reference to the shared client.

    If no references remain and stop_if_idle is True, the client is stopped.
    """
    phone = str(phone)
    state = CLIENT_STATES.get(phone)

    if not state:
        return

    async with state["lock"]:
        state["refs"] = max(0, state.get("refs", 0) - 1)

        if state["refs"] == 0 and stop_if_idle:
            client = state.get("client")

            if client:
                try:
                    await client.stop()
                except:
                    pass

            state["client"] = None
            state["setup_done"] = False
            state["last_stop"] = time.time()
            optimizations.force_gc(f"client_released:{phone}")


async def stop_and_cleanup_client(phone: str):
    """
    Forcefully stops a client, removes it from memory entirely, and calls GC.
    This should be used when an account is deleted, toggled off, or failed permanently.
    """
    phone = str(phone)
    state = CLIENT_STATES.pop(phone, None)

    if not state:
        return

    async with state["lock"]:
        client = state.get("client")

        if client:
            try:
                await client.stop()
            except Exception:
                pass

        state["client"] = None
        state["refs"] = 0
        state["setup_done"] = False
        state["last_stop"] = time.time()

    optimizations.force_gc(f"client_cleaned:{phone}")


async def run_with_client(phone: str, job, setup=None):
    """
    Runs a one-off job using the shared client.

    job:
        Async function that receives the client.
    """
    client = await get_client(phone, setup=setup)

    try:
        return await job(client)
    finally:
        await release_client(phone)


async def get_groups_managed(phone: str):
    """
    Gets groups using the shared client pool.

    This avoids creating a second temporary client for the same session.
    """
    async def job(client):
        groups = []

        async for dialog in client.get_dialogs(limit=500):
            chat = dialog.chat

            if not chat:
                continue

            chat_type = getattr(chat, "type", None)
            is_group = False

            if ChatType:
                if chat_type in (ChatType.GROUP, ChatType.SUPERGROUP):
                    is_group = True
            else:
                if str(chat_type).lower() in ("group", "supergroup"):
                    is_group = True

            if is_group:
                groups.append({
                    "id": str(chat.id),
                    "title": getattr(chat, "title", None) or "بدون نام",
                    "members": getattr(chat, "members_count", 0) or 0
                })

        return groups

    return await run_with_client(phone, job)


async def get_me_name(phone: str):
    """
    Returns the Telegram display name for this account (checked once when missing).
    """
    async def job(client):
        me = await client.get_me()
        return (getattr(me, "first_name", None) or getattr(me, "username", None) or "").strip()

    return await run_with_client(phone, job)


async def stop_all_clients():
    """
    Force stops all shared clients.
    Used during shutdown.
    """
    for phone in list(CLIENT_STATES.keys()):
        state = CLIENT_STATES.get(phone)

        if not state:
            continue

        async with state["lock"]:
            client = state.get("client")

            if client:
                try:
                    await client.stop()
                except:
                    pass

            state["client"] = None
            state["refs"] = 0
            state["setup_done"] = False
            state["last_stop"] = time.time()