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
        self.agy_process = None

    async def start(self):
        # Start agy process
        self.agy_process = await asyncio.create_subprocess_exec(
            'agy',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        await asyncio.gather(
            self.read_agy_stdout(),
            self.read_local_stdin(),
            client.start(TOKEN)
        )

    async def read_agy_stdout(self):
        while True:
            line = await self.agy_process.stdout.readline()
            if not line:
                break
            text = line.decode('utf-8', errors='replace')
            sys.stdout.write(text)
            sys.stdout.flush()

        print("[Dishell] agy process terminated.")
        await client.close()

    async def read_local_stdin(self):
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        while True:
            line = await reader.readline()
            if not line:
                break
            
            text = line.decode('utf-8').strip()
            
            if text == "/rc on":
                self.rc_enabled = True
                print("\n[Dishell] Remote control enabled. Connected to Discord DM.\n")
            elif text == "/rc off":
                self.rc_enabled = False
                print("\n[Dishell] Remote control disabled.\n")
            else:
                # Forward to agy
                if self.agy_process and self.agy_process.stdin:
                    self.agy_process.stdin.write(line)
                    await self.agy_process.stdin.drain()

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
