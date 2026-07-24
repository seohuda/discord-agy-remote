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
        self.output_buffer = ""
        self.buffer_lock = asyncio.Lock()

    async def start(self):
        # Start agy process
        self.agy_process = await asyncio.create_subprocess_exec(
            'agy',
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        # Run concurrent tasks
        await asyncio.gather(
            self.read_agy_stdout(),
            self.read_local_stdin(),
            self.flush_buffer_task(),
            client.start(TOKEN)
        )

    async def read_agy_stdout(self):
        while True:
            line = await self.agy_process.stdout.readline()
            if not line:
                break
            text = line.decode('utf-8', errors='replace')
            # Print to local console
            sys.stdout.write(text)
            sys.stdout.flush()

            # Buffer for discord
            if self.rc_enabled:
                async with self.buffer_lock:
                    self.output_buffer += text

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
                print("\n[Dishell] Remote control enabled. Forwarding output to Discord DM.\n")
                await self.notify_discord("🟢 **Remote Control Enabled**\nYou are now connected to the local Antigravity CLI session.")
            elif text == "/rc off":
                self.rc_enabled = False
                print("\n[Dishell] Remote control disabled.\n")
                await self.notify_discord("🔴 **Remote Control Disabled**")
            else:
                # Forward to agy
                if self.agy_process and self.agy_process.stdin:
                    self.agy_process.stdin.write(line)
                    await self.agy_process.stdin.drain()

    async def notify_discord(self, message):
        if not USER_ID:
            return
        try:
            user = await client.fetch_user(USER_ID)
            if user:
                await user.send(message)
        except Exception as e:
            print(f"\n[Dishell] Failed to send Discord notification: {e}\n")

    async def flush_buffer_task(self):
        while True:
            await asyncio.sleep(1.0)
            if not self.rc_enabled:
                continue
            
            async with self.buffer_lock:
                if not self.output_buffer:
                    continue
                
                # Split if larger than discord limits
                chunk = self.output_buffer[:1900]
                self.output_buffer = self.output_buffer[len(chunk):]

            if chunk.strip() and USER_ID:
                try:
                    user = await client.fetch_user(USER_ID)
                    if user:
                        await user.send(f"```text\n{chunk}\n```")
                except Exception as e:
                    print(f"\n[Dishell] Discord API error: {e}\n")

manager = DishellManager()

@client.event
async def on_ready():
    print(f"\n[Dishell] Discord Bot logged in as {client.user}")

@client.event
async def on_message(message):
    if message.author == client.user:
        return
    
    # Check if DM and authorized user
    if isinstance(message.channel, discord.DMChannel) and message.author.id == USER_ID:
        if manager.rc_enabled and manager.agy_process:
            input_line = message.content + "\n"
            manager.agy_process.stdin.write(input_line.encode('utf-8'))
            await manager.agy_process.stdin.drain()
            
            # Optionally echo it to local console
            sys.stdout.write(f"[Discord RC] {input_line}")
            sys.stdout.flush()

if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_TOKEN is missing in .env")
        sys.exit(1)
    
    try:
        asyncio.run(manager.start())
    except KeyboardInterrupt:
        print("\n[Dishell] Shutting down...")
