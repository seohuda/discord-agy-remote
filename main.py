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

class DishellBot(discord.Client):
    def __init__(self, manager, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.manager = manager

    async def on_ready(self):
        print(f"\n[Dishell] Discord Bot logged in as {self.user}")

    async def on_message(self, message):
        if message.author == self.user:
            return
        
        # Check if DM and authorized user
        if isinstance(message.channel, discord.DMChannel) and message.author.id == USER_ID:
            if self.manager.rc_enabled and self.manager.agy_process:
                input_line = message.content + "\n"
                self.manager.agy_process.stdin.write(input_line.encode('utf-8'))
                await self.manager.agy_process.stdin.drain()
                
                # Optionally echo it to local console
                sys.stdout.write(f"[Discord RC] {input_line}")
                sys.stdout.flush()

class DishellManager:
    def __init__(self):
        self.rc_enabled = False
        self.agy_process = None
        self.output_buffer = ""
        self.buffer_lock = asyncio.Lock()
        self.bot_task = None
        self.client = None

    async def start(self):
        # Forward sys.argv to agy
        args = sys.argv[1:]
        
        # Start agy process
        self.agy_process = await asyncio.create_subprocess_exec(
            'agy', *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        # Run concurrent tasks
        await asyncio.gather(
            self.read_agy_stdout(),
            self.read_local_stdin(),
            self.flush_buffer_task(),
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
        if self.bot_task and self.client:
            await self.client.close()

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
                if not self.rc_enabled:
                    self.rc_enabled = True
                    print("\n[Dishell] Starting Discord Bot...\n")
                    self.client = DishellBot(manager=self, intents=intents)
                    self.bot_task = asyncio.create_task(self.client.start(TOKEN))
                    # Wait a bit for bot to be ready before notifying
                    await asyncio.sleep(2)
                    print("[Dishell] Remote control enabled. Connected to Discord DM.\n")
                    await self.notify_discord("🟢 **Remote Control Enabled**\nYou are now connected to the local Antigravity CLI session.")
                else:
                    print("\n[Dishell] Remote control is already enabled.\n")
            elif text == "/rc off":
                if self.rc_enabled:
                    self.rc_enabled = False
                    await self.notify_discord("🔴 **Remote Control Disabled**")
                    print("\n[Dishell] Stopping Discord Bot...\n")
                    if self.client:
                        await self.client.close()
                        self.client = None
                    if self.bot_task:
                        self.bot_task.cancel()
                        self.bot_task = None
                    print("[Dishell] Remote control disabled.\n")
                else:
                    print("\n[Dishell] Remote control is already disabled.\n")
            else:
                # Forward to agy
                if self.agy_process and self.agy_process.stdin:
                    self.agy_process.stdin.write(line)
                    await self.agy_process.stdin.drain()

    async def notify_discord(self, message):
        if not USER_ID or not self.client:
            return
        try:
            user = await self.client.fetch_user(USER_ID)
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

            if chunk.strip() and USER_ID and self.client:
                try:
                    user = await self.client.fetch_user(USER_ID)
                    if user:
                        await user.send(f"```text\n{chunk}\n```")
                except Exception as e:
                    print(f"\n[Dishell] Discord API error: {e}\n")

manager = DishellManager()

if __name__ == "__main__":
    if not TOKEN:
        print("Error: DISCORD_TOKEN is missing in .env")
        sys.exit(1)
    
    try:
        asyncio.run(manager.start())
    except KeyboardInterrupt:
        print("\n[Dishell] Shutting down...")
