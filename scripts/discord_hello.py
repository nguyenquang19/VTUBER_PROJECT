import os, discord

class Hello(discord.Client):
    async def on_ready(self): print(f"OK logged in: {self.user}")
    async def on_message(self, m):
        if m.author == self.user: return
        print(f"[{m.author.display_name}] {m.content}")

if __name__ == "__main__":
    i = discord.Intents.default(); i.message_content = True
    Hello(intents=i).run(os.environ["DISCORD_TOKEN"])