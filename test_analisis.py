import asyncio
import analyzer

async def main():
    resultado = await analyzer.analyze_match("BK Hacken", "AIK Stockholm")
    print(resultado)

asyncio.run(main())