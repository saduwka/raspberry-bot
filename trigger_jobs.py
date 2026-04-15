import asyncio
import logging
from telegram.ext import Application
from job_handlers import job_fetch_job
from config import BOT_TOKEN

logging.basicConfig(level=logging.INFO)

async def trigger_jobs():
    app = Application.builder().token(BOT_TOKEN).build()
    # We just need the context for job_fetch_job, but it's easier to just call it if we mock the context
    # or just use the function directly.
    # Actually, job_fetch_job needs context.bot.send_message
    
    class MockContext:
        def __init__(self, bot):
            self.bot = bot

    await app.initialize()
    await job_fetch_job(MockContext(app.bot))
    await app.shutdown()

if __name__ == "__main__":
    asyncio.run(trigger_jobs())
