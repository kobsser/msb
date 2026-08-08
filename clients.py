import asyncio
import uuid

from pyrogram import Client
from pyrogram.errors import (
    PhoneNumberInvalid,
    PhoneCodeInvalid,
    PhoneCodeExpired,
    SessionPasswordNeeded
)

# Safely import ChatType enum for Pyrogram v2 / Kurigram
try:
    from pyrogram.enums import ChatType
except ImportError:
    ChatType = None

from config import API_ID, API_HASH

pending = {}

async def send_code(phone: str):
    phone = str(phone).strip()

    if phone in pending:
        try:
            await pending[phone]["client"].disconnect()
        except:
            pass
        pending.pop(phone, None)

    # Added uuid to prevent session state clashes in Kurigram
    client = Client(
        f"temp_{uuid.uuid4().hex[:8]}",
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True
    )

    await client.connect()

    try:
        sent = await client.send_code(phone)
        pending[phone] = {
            "client": client,
            "phone_code_hash": sent.phone_code_hash
        }
        return True

    except PhoneNumberInvalid:
        await client.disconnect()
        return "error: شماره نامعتبر است"

    except Exception as e:
        await client.disconnect()
        return f"error: {str(e)}"

async def sign_in(phone: str, code: str):
    phone = str(phone).strip()

    if phone not in pending:
        return "error: نشست منقضی شده، دوباره شماره وارد کنید"

    data = pending[phone]
    client = data["client"]

    try:
        if not client.is_connected:
            await client.connect()

        await client.sign_in(
            phone_number=phone,
            phone_code_hash=data["phone_code_hash"],
            phone_code=str(code).strip()
        )

        session_string = await client.export_session_string()
        await client.disconnect()
        pending.pop(phone, None)

        return session_string

    except SessionPasswordNeeded:
        return "need_password"

    except PhoneCodeInvalid:
        return "error: کد اشتباه است"

    except PhoneCodeExpired:
        await client.disconnect()
        pending.pop(phone, None)
        return "error: کد منقضی شده"

    except Exception as e:
        await client.disconnect()
        pending.pop(phone, None)
        return f"error: {str(e)}"

async def check_password(phone: str, password: str):
    phone = str(phone).strip()

    if phone not in pending:
        return "error: نشست منقضی شده"

    data = pending[phone]
    client = data["client"]

    try:
        if not client.is_connected:
            await client.connect()

        await client.check_password(str(password).strip())

        session_string = await client.export_session_string()
        await client.disconnect()
        pending.pop(phone, None)

        return session_string

    except Exception as e:
        await client.disconnect()
        pending.pop(phone, None)
        return f"error: {str(e)}"

async def get_groups(session_string: str):
    # Use a unique name to prevent session file/state clashes in Kurigram
    client = Client(
        name=f"tmp_{uuid.uuid4().hex[:8]}",
        session_string=session_string,
        api_id=API_ID,
        api_hash=API_HASH,
        in_memory=True,
        no_updates=True
    )

    try:
        await client.start()

        groups = []

        # Increased limit to 500 to ensure we catch all groups
        async for dialog in client.get_dialogs(limit=500):
            chat = dialog.chat

            if not chat:
                continue

            chat_type = getattr(chat, "type", None)
            is_group = False

            # FIX: Properly check for Pyrogram v2 / Kurigram Enum types
            if ChatType:
                if chat_type in (ChatType.GROUP, ChatType.SUPERGROUP):
                    is_group = True
            else:
                # Fallback for older versions
                if str(chat_type).lower() in ("group", "supergroup"):
                    is_group = True

            if is_group:
                groups.append({
                    "id": str(chat.id),
                    "title": getattr(chat, "title", None) or "بدون نام",
                    "members": getattr(chat, "members_count", 0) or 0
                })

        return groups

    except Exception as e:
        import traceback
        traceback.print_exc()
        return f"error: {str(e)}"

    finally:
        try:
            await client.stop()
        except:
            pass