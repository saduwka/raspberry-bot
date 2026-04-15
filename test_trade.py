import asyncio
import logging
import sys
import pandas as pd
from trade_engine import fetch_ohlcv, calc_indicators, get_signal

logging.basicConfig(level=logging.INFO)

async def test_signal():
    print("Fetching data...")
    df = await fetch_ohlcv()
    if df is None:
        print("Failed to fetch data")
        return
    
    print("Calculating indicators...")
    df = calc_indicators(df)
    
    print("Last 5 rows:")
    print(df[['timestamp', 'close', 'ema_fast', 'ema_slow', 'rsi']].tail())
    
    signal = get_signal(df)
    print(f"\nCurrent signal: {signal}")

if __name__ == "__main__":
    asyncio.run(test_signal())
