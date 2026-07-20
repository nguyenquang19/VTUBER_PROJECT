import discord
from shared.contracts.payloads import IngestionRecord, EventType
from shared.contracts.timing import TimingTrace
from shared.utils.clock import now_ms
from shared.utils.timing_recorder import stamp
from .rate_limiter import RateLimiter
from .scorer import tier1_score
from .sink import TcpNode2Sink, Node2Sink
from .config import DISCORD_TOKEN
import uuid
from shared.utils.logger import get_logger
_log = get_logger("node1")

class IngestionBot(discord.Client):
    def __init__(self, sink: Node2Sink, **kw):
        super().__init__(**kw)
        self._sink = sink; self._rl = RateLimiter()

    async def on_message(self, m: discord.Message):
        print(f"NODE1_MSG {m.content[:20]}", flush=True)
        if m.author == self.user: return
        # v1 Discord: coi viewer > 0, bỏ qua OFFLINE detection.
        if not self._rl.allow(str(m.author.id)): return
        trace = TimingTrace(turn_id=str(uuid.uuid4()))
        stamp(trace, "t0_received")  # ngay khi nhận, in-process
        rec = IngestionRecord(
            EventType.CHAT, str(m.author.id), m.author.display_name,
            m.content, now_ms(), tier1_score(m.content), trace)
        try: self._sink.send(rec)
        except Exception: pass  # không để lỗi sink chặn bot

    async def on_ready(self):
        from .voice import VoicePlayer
        from .voice_server import serve_voice
        import threading
        self.voice = VoicePlayer(self)
        await self.voice.connect()
        threading.Thread(target=lambda: serve_voice(self.voice), daemon=True).start()
        _log.info(f"bot ready: {self.user}")
def main():
    i = discord.Intents.default(); i.message_content = True
    IngestionBot(TcpNode2Sink(), intents=i).run(DISCORD_TOKEN)

if __name__ == "__main__": main()