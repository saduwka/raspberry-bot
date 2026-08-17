import logging
from functools import wraps

from telegram import Update
from telegram.ext import ContextTypes

from config import ADMIN_ID

logger = logging.getLogger(__name__)


def admin_only(func):
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id if update.effective_user else None
        if user_id != ADMIN_ID:
            logger.warning("Unauthorized access attempt by ID: %s", user_id)
            return
        return await func(update, context, *args, **kwargs)
    return wrapped
