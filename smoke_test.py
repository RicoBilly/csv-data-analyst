"""End-to-end smoke test with the real model (needs MISTRAL_API_KEY): loads an inline CSV, asks for a group aggregate, then a follow-up question to check session memory."""

import asyncio

from mistralai import agents

from main import agent

CSV = "region;client;montant\nNord;Acme;100\nSud;Beta;200\nNord;Acme;50\nSud;Gamma;75\n"


async def main() -> None:
    async with agents.local_runtime():
        session = await agent.start_session()
        s1 = await session.run(f"Charge ce CSV :\n{CSV}\nPuis donne-moi la somme des montants par region.")
        print("TURN1 type:", type(s1).__name__)
        print("TURN1 text:", getattr(s1, "text", s1))
        print("TURN1 tool_calls:", [c.name for c in getattr(s1, "tool_calls", [])])
        s2 = await session.run("Et combien de lignes au total dans le fichier charge ?")
        print("TURN2 text:", getattr(s2, "text", s2))
        print("TURN2 tool_calls:", [c.name for c in getattr(s2, "tool_calls", [])])
        await session.cancel()


if __name__ == "__main__":
    asyncio.run(main())
