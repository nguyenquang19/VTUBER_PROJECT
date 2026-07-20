import asyncio, os, discord
from shared.utils.logger import get_logger

_log = get_logger("voice")
VOICE_CHANNEL_ID = int(os.getenv("DISCORD_VOICE_CHANNEL_ID", "0"))

# Phát .wav tuần tự vào 1 voice channel. Hàng đợi nội bộ để không chồng tiếng.
# Hỗ trợ SKIP: bỏ các câu còn lại của 1 turn khi nhận stop.
class VoicePlayer:
    def __init__(self, client: discord.Client):
        self._client = client
        self._vc = None                          # voice client
        self._queue = asyncio.Queue()
        self._cancelled = set()                  # turn_id bị skip
        self._current_turn = None

    async def connect(self):
        if VOICE_CHANNEL_ID == 0:
            _log.info("chưa set DISCORD_VOICE_CHANNEL_ID -> không phát voice"); return
        ch = self._client.get_channel(VOICE_CHANNEL_ID)
        if ch is None:
            _log.info(f"không thấy voice channel {VOICE_CHANNEL_ID}"); return
        self._vc = await ch.connect()
        asyncio.create_task(self._play_loop())
        _log.info(f"đã vào voice channel {ch.name}")

    def enqueue(self, turn_id, seq, wav_path, is_last):
        # gọi từ luồng khác -> đẩy vào event loop an toàn
        self._client.loop.call_soon_threadsafe(
            self._queue.put_nowait, (turn_id, seq, wav_path, is_last))

    def cancel(self, turn_id):
        self._cancelled.add(turn_id)
        # dừng câu đang phát nếu thuộc turn bị skip
        if self._vc and self._vc.is_playing() and self._current_turn == turn_id:
            self._vc.stop()

    async def _play_loop(self):
        while True:
            turn_id, seq, wav, is_last = await self._queue.get()
            if turn_id in self._cancelled:        # turn đã bị skip -> bỏ
                continue
            if not os.path.exists(wav):
                continue
            self._current_turn = turn_id
            src = discord.FFmpegPCMAudio(wav)
            done = asyncio.Event()
            self._vc.play(src, after=lambda e: self._client.loop.call_soon_threadsafe(done.set))
            await done.wait()                     # chờ phát xong câu này rồi phát câu tiếp
            if is_last and turn_id in self._cancelled:
                self._cancelled.discard(turn_id)