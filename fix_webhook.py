import asyncio
from telegram import Bot

async def main():
    token = '8035529032:AAHSuuoYiVE8xGDbHkdUR6iVWEwTGrznLTA'
    bot = Bot(token)
    try:
        result = await bot.delete_webhook()
        print(f"Delete webhook result: {result}")
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
