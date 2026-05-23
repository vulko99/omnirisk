import asyncio
import aiohttp

async def test():
    url = "https://quote-api.jup.ag/v6/quote"
    params = {
        "inputMint":  "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "outputMint": "So11111111111111111111111111111111111111112",
        "amount":     "500000000",
        "slippageBps": "50",
    }
    async with aiohttp.ClientSession() as session:
        async with session.get(url, params=params) as r:
            print(f"Status: {r.status}")
            data = await r.json()
            print(data)

asyncio.run(test())