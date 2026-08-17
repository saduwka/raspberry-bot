from core.db import connect


async def set_oled_config(key: str, value: str):
    async with connect(commit=True) as db:
        await db.execute(
            "INSERT OR REPLACE INTO oled_config (key, value) VALUES (?, ?)",
            (key, value),
        )
