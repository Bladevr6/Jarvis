#!/usr/bin/env python3
"""Diagnostic : micro -> openwakeword, affiche niveau sonore et tout événement reçu.
Usage : python3 wake_test.py [nom_du_modele]"""
import asyncio
import sys
import time

import numpy as np
from wyoming.audio import AudioChunk, AudioStart
from wyoming.client import AsyncTcpClient
from wyoming.wake import Detect

from jarvis import CHUNK_BYTES, MIC_RATE, env, load_env


async def main():
    load_env()
    word = sys.argv[1] if len(sys.argv) > 1 else env("WAKE_WORD", "hey_jarvis")
    host, port = env("WAKE_HOST", "127.0.0.1"), int(env("WAKE_PORT", "10400"))
    proc = await asyncio.create_subprocess_exec(
        "arecord", "-q", "-D", env("MIC_DEVICE", "plughw:2,0"), "-r", str(MIC_RATE), "-c", "1",
        "-f", "S16_LE", "-t", "raw", stdout=asyncio.subprocess.PIPE)
    async with AsyncTcpClient(host, port) as client:
        await client.write_event(Detect(names=[word]).event())
        await client.write_event(AudioStart(rate=MIC_RATE, width=2, channels=1).event())
        print(f"Connecté à {host}:{port}, modèle demandé : {word}. Dites « Hey Jarvis ». Ctrl+C pour quitter.")

        async def reader():
            while True:
                ev = await client.read_event()
                if ev is None:
                    print("!! connexion fermée par le serveur")
                    return
                print(f"<< {ev.type} {ev.data}")

        asyncio.create_task(reader())
        peak, last, sent = 0, time.monotonic(), 0
        while True:
            chunk = await proc.stdout.readexactly(CHUNK_BYTES)
            sent += 1
            peak = max(peak, int(np.abs(np.frombuffer(chunk, dtype=np.int16)).max()))
            await client.write_event(AudioChunk(rate=MIC_RATE, width=2, channels=1, audio=chunk).event())
            if time.monotonic() - last > 2:
                print(f"   niveau micro max (2 s) : {peak:5d} / 32767   chunks envoyés : {sent}")
                peak, last = 0, time.monotonic()


try:
    asyncio.run(main())
except KeyboardInterrupt:
    pass
