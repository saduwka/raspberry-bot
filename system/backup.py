import logging
import os
import shutil
from datetime import datetime

from telegram.ext import ContextTypes

from config import ADMIN_ID, DB_PATH

logger = logging.getLogger(__name__)


async def backup_db_job(context: ContextTypes.DEFAULT_TYPE):
    """Отправляет резервную копию базы данных админу."""
    logger.info("Creating database backup...")
    try:
        backup_path = f"{DB_PATH}.backup"
        shutil.copy2(DB_PATH, backup_path)

        with open(backup_path, "rb") as f:
            await context.bot.send_document(
                chat_id=ADMIN_ID,
                document=f,
                filename=f"bot_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db",
                caption=f"📦 Резервная копия базы данных\n📅 {datetime.now().strftime('%d.%m.%Y %H:%M')}",
            )

        os.remove(backup_path)
        logger.info("Backup sent successfully.")
    except Exception as e:
        logger.error("Backup failed: %s", e)
        await context.bot.send_message(chat_id=ADMIN_ID, text=f"❌ Ошибка при создании бэкапа: {e}")
