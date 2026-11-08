"""Paced PCM16 mono 48 kHz output; generation-based interruption."""
import asyncio


class AudioOutput:
    def __init__(self, send):
        self.send = send
        self.generation = 0

    def interrupt(self):
        self.generation += 1

    async def play(self, pcm: bytes):
        if not isinstance(pcm, bytes) or len(pcm) % 2:
            raise ValueError('Expected PCM16 bytes')
        generation = self.generation
        loop = asyncio.get_running_loop()
        deadline = loop.time()
        for offset in range(0, len(pcm), 960):
            if generation != self.generation:
                return
            await self.send(pcm[offset:offset+960].ljust(960, b'\0'))
            deadline += 0.01
            await asyncio.sleep(max(0, deadline-loop.time()))
