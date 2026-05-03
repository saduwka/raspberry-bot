
import asyncio
import logging
from job_fetcher import fetch_all_jobs

logging.basicConfig(level=logging.INFO)

async def test():
    print("Fetching jobs...")
    jobs = await fetch_all_jobs()
    print(f"Found {len(jobs)} jobs in total (before Gemini scoring)")
    for i, job in enumerate(jobs[:5]):
        print(f"{i+1}. {job['title']} @ {job['company']} ({job['url']})")

if __name__ == "__main__":
    asyncio.run(test())
