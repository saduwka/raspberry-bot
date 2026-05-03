
import asyncio
import logging
from ai_utils import process_job_scoring

logging.basicConfig(level=logging.INFO)

async def test():
    title = "Frontend-разработчик (vanilla JS, high-load)"
    company = "Karma8"
    description = "Frontend-разработчик (vanilla JS, high-load). Стек: vanilla JS, high-load."
    
    print(f"Scoring {title} @ {company}...")
    result = await process_job_scoring(title, company, description)
    print(f"Result: {result}")

if __name__ == "__main__":
    asyncio.run(test())
