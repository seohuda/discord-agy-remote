import asyncio
import os
import sys
import discord
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv('DISCORD_TOKEN')
USER_ID_STR = os.getenv('DISCORD_USER_ID', '')
USER_ID = int(USER_ID_STR) if USER_ID_STR.isdigit() else 0

intents = discord.Intents.default()
intents.message_content = True

client = discord.Client(intents=intents)

class DishellManager:
    def __init__(self):
        self.rc_enabled = False

    async def start(self):
        await client.start(TOKEN)

manager = DishellManager()

@client.event
async def on_ready():
    print(f"\n[Dishell] Discord Bot logged in as {client.user}")

if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_TOKEN is missing in .env")
        sys.exit(1)
    
    try:
        asyncio.run(manager.start())
    except KeyboardInterrupt:
        print("\n[Dishell] Shutting down...")
