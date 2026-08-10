import asyncio
import gc
import logging
import os
from typing import Optional, Any, Iterable, Set

logger = logging.getLogger("msb.optimizations")

PISHI_CALLBACK_TOKEN = "collect_cat"
FISHING_CALLBACK_TOKEN = "feed_cat"

GC_INTERVAL_SECONDS = int(os.getenv("GC_INTERVAL_SECONDS", "600"))
GC_DEBUG = os.getenv("GC_DEBUG", "false").strip().lower() in {"1", "true", "yes", "on"}

_gc_task: Optional[asyncio.Task] = None

def normalize_callback_data(callback_data: Any) -> str:
    if callback_data is None:
        return ""
    if isinstance(callback_data, bytes):
        try:
            return callback_data.decode("utf-8", errors="ignore")
        except Exception:
            return ""
    try:
        return str(callback_data)
    except Exception:
        return ""

def find_button_by_callback_contains(message: Any, token: str) -> Optional[Any]:
    if not message:
        return None
    reply_markup = getattr(message, "reply_markup", None)
    if not reply_markup:
        return None
    inline_keyboard = getattr(reply_markup, "inline_keyboard", None)
    if not inline_keyboard:
        return None
    try:
        for row_index, row in enumerate(inline_keyboard):
            if not row:
                continue
            for col_index, button in enumerate(row):
                callback_data = normalize_callback_data(getattr(button, "callback_data", None))
                if token and token in callback_data:
                    # attach indices for clicking
                    button._row_index = row_index
                    button._col_index = col_index
                    return button
    except Exception as e:
        logger.warning(f"Error while searching inline button: {e}")
        return None
    return None

def find_pishi_button(message: Any) -> Optional[Any]:
    return find_button_by_callback_contains(message, PISHI_CALLBACK_TOKEN)

def find_fishing_button(message: Any) -> Optional[Any]:
    return find_button_by_callback_contains(message, FISHING_CALLBACK_TOKEN)

def normalize_group_ids(group_ids: Iterable[Any]) -> Set[int]:
    normalized = set()
    if not group_ids:
        return normalized
    for group_id in group_ids:
        try:
            normalized.add(int(str(group_id).strip()))
        except Exception:
            continue
    return normalized

def message_is_from_selected_group(message: Any, selected_group_ids: Iterable[Any]) -> bool:
    if not message:
        return False
    chat = getattr(message, "chat", None)
    if not chat:
        return False
    chat_id = getattr(chat, "id", None)
    if chat_id is None:
        return False
    try:
        chat_id = int(chat_id)
    except Exception:
        return False
    selected = normalize_group_ids(selected_group_ids)
    return chat_id in selected

def force_gc(reason: str = "manual") -> None:
    collected = gc.collect()
    if GC_DEBUG:
        logger.info(f"Forced garbage collection ({reason}): collected={collected}")

async def periodic_gc_task() -> None:
    while True:
        try:
            await asyncio.sleep(GC_INTERVAL_SECONDS)
            collected = gc.collect()
            if GC_DEBUG:
                logger.info(f"Periodic garbage collection completed: collected={collected}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Error in periodic GC task: {e}")
            await asyncio.sleep(60)

def ensure_gc_task() -> Optional[asyncio.Task]:
    global _gc_task
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    if _gc_task is None or _gc_task.done():
        _gc_task = loop.create_task(periodic_gc_task())
        logger.info(f"Periodic garbage collection task started (interval={GC_INTERVAL_SECONDS}s)")
    return _gc_task