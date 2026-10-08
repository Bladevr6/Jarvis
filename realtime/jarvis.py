#!/usr/bin/env python3
"""Jarvis Realtime : « Hey Jarvis » -> OpenAI Realtime -> haut-parleur du Pi.

Usage :
    python3 jarvis.py           # écoute le mot d'activation
    python3 jarvis.py --direct  # démarre une conversation tout de suite (test)
"""
import asyncio
import base64
import json
import logging
import os
import socket
import struct
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from pathlib import Path

import numpy as np
from websockets.asyncio.client import connect
from wyoming.audio import AudioChunk, AudioStart
from wyoming.client import AsyncTcpClient
from wyoming.info import Describe, Info
from wyoming.wake import Detect, Detection

import ha_tools

log = logging.getLogger("jarvis")

MIC_RATE = 16000           # ReSpeaker / openwakeword
API_RATE = 24000           # OpenAI Realtime (PCM 16 bits mono)
CHUNK_BYTES = 2560         # 80 ms à 16 kHz
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

INSTRUCTIONS = """Tu es Jarvis, le majordome vocal de la maison de José.
Tu parles toujours en français et tu vouvoies toujours, avec le ton d'un majordome distingué, chaleureux et légèrement pince-sans-rire.
Réponses très courtes : une ou deux phrases, pas de listes, jamais d'emojis.
Le mot « Hey Jarvis » au début de l'audio est le mot d'activation : ignore-le. Si l'utilisateur n'a dit que cela, réponds seulement « Oui ? ».
Si l'audio n'est pas une demande claire d'un adulte (babillage de bébé, bruit, télévision, conversation qui ne t'est pas adressée), ne réponds rien et appelle fin_conversation.
Quand on te remercie ou qu'on dit « c'est tout », « bonne nuit », etc., réponds en quelques mots puis appelle fin_conversation.
Pour piloter la maison, utilise l'outil commander avec les entity_id de l'inventaire ci-dessous (n'invente jamais d'entity_id ; lister_appareils sert seulement si un appareil manque).
Vocabulaire : « store », « volet », « rideau » = domaine cover (open_cover = ouvrir/monter, close_cover = fermer/baisser) ; « lumière », « lampe » = domaine light.
Agis immédiatement, sans demander de confirmation ni de précision inutile : « éteins la cuisine » veut dire toutes les lumières de la cuisine, en un seul appel à commander avec la liste des entity_id.
La seule exception : avant d'ouvrir la porte de garage, demande confirmation.
Après une action, confirme en trois ou quatre mots (« C'est fait. », « Salon éteint. »).
Outils disponibles en plus de la maison : meteo, agenda, ajouter_evenement, dernier_mail, annoncer (Sonos ou Echos Alexa), minuteur, demander_a_claude.
Pour toute question de culture générale, d'actualité, de calcul ou de conseil, utilise demander_a_claude et lis sa réponse ; ne réponds pas de mémoire sur des faits précis.
Si un service renvoie une erreur (ex. set_cover_position refusé), réessaie avec le service simple (open_cover / close_cover / turn_on) avant de signaler un problème.
Nous sommes le {date}.

Inventaire de la maison (entity_id | nom | pièce | état) :
{inventaire}"""

FIN_CONVERSATION = {
    "type": "function",
    "name": "fin_conversation",
    "description": "Termine la conversation et retourne en veille.",
    "parameters": {"type": "object", "properties": {}},
}


def load_env(path=Path(__file__).with_name("config.env")):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"'))


def env(key, default=None):
    return os.environ.get(key, default)


def mic_channels():
    """MIC_CHANNEL=mono (défaut) ou 0/1 : le ReSpeaker XVF3800 sort 2 canaux traités différemment."""
    v = env("MIC_CHANNEL", "mono")
    return (1, 0) if v == "mono" else (2, int(v))


def select_channel(raw: bytes, channels: int, channel: int) -> bytes:
    if channels == 1:
        return raw
    return np.frombuffer(raw, dtype=np.int16).reshape(-1, channels)[:, channel].tobytes()


def to_api_rate(pcm16k: bytes) -> bytes:
    """Rééchantillonne 16 kHz -> 24 kHz (interpolation linéaire, très léger)."""
    x = np.frombuffer(pcm16k, dtype=np.int16).astype(np.float32)
    xi = np.linspace(0, len(x) - 1, len(x) * API_RATE // MIC_RATE)
    return np.interp(xi, np.arange(len(x)), x).astype(np.int16).tobytes()


def make_beep() -> bytes:
    t = np.arange(int(API_RATE * 0.12)) / API_RATE
    wave = np.sin(2 * np.pi * 880 * t) * np.minimum(1, np.minimum(t, t[::-1]) / 0.01)
    return (wave * 8000).astype(np.int16).tobytes()


class Player:
    """Joue du PCM 24 kHz via aplay. stop() coupe net (interruption)."""

    def __init__(self, device):
        self.device = device
        self.proc = None
        self.queue = asyncio.Queue()
        self.play_until = 0.0  # instant estimé de fin de lecture

    def play(self, pcm: bytes):
        now = time.monotonic()
        self.play_until = max(now, self.play_until) + len(pcm) / (API_RATE * 2)
        self.queue.put_nowait(pcm)

    def is_playing(self, margin=0.3):
        return time.monotonic() < self.play_until + margin

    def stop(self):
        while not self.queue.empty():
            self.queue.get_nowait()
        self.play_until = 0.0
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
        self.proc = None

    async def run(self):
        while True:
            pcm = await self.queue.get()
            try:
                if self.proc is None or self.proc.returncode is not None:
                    self.proc = await asyncio.create_subprocess_exec(
                        "aplay", "-q", "-D", self.device, "-t", "raw", "-f", "S16_LE",
                        "-r", str(API_RATE), "-c", "1",
                        stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
                proc = self.proc
                proc.stdin.write(pcm)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                self.proc = None


class SonosPlayer:
    """Mode temporaire : accumule la réponse, l'écrit en WAV, et demande au Sonos de la lire
    via Home Assistant. Le Sonos vient chercher le fichier sur un petit serveur HTTP du Pi."""

    PORT = 8800

    def __init__(self, entity_id):
        self.entity_id = entity_id
        self.buffer = bytearray()
        self.play_until = 0.0
        self.counter = 0
        self.awaiting = {}  # fichier -> durée, en attente que le Sonos vienne le chercher
        self.www = Path(__file__).with_name("www")
        self.www.mkdir(exist_ok=True)
        self.ip = self._local_ip()
        www = str(self.www)
        player = self

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *a, **k):
                super().__init__(*a, directory=www, **k)

            def log_message(self, *a):
                pass

            def do_GET(self):
                player.on_fetch(self.path.split("?")[0].lstrip("/"))
                super().do_GET()

        self.server = ThreadingHTTPServer(("0.0.0.0", self.PORT), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        log.info("Sortie Sonos (%s), serveur audio sur http://%s:%s", entity_id, self.ip, self.PORT)

    @staticmethod
    def _local_ip():
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()

    def play(self, pcm: bytes):
        self.buffer += pcm

    def is_playing(self, margin=0.3):
        return bool(self.buffer) or time.monotonic() < self.play_until + margin

    def stop(self):
        self.buffer = bytearray()
        self.play_until = 0.0

    def flush(self):
        """Appelé à la fin d'une réponse : envoie le WAV au Sonos."""
        if not self.buffer:
            return
        pcm = bytes(self.buffer)
        self.buffer = bytearray()
        self.counter += 1
        name = f"reponse_{self.counter % 5}.wav"
        header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16, 1, 1,
                             API_RATE, API_RATE * 2, 2, 16, b"data", len(pcm))
        (self.www / name).write_bytes(header + pcm)
        duration = len(pcm) / (API_RATE * 2)
        # Micro coupé au plus 5 s en attendant que le Sonos vienne chercher le fichier ;
        # on_fetch recale ensuite précisément sur l'instant réel de lecture.
        self.awaiting = {name: duration}
        self.play_until = time.monotonic() + 5
        url = f"http://{self.ip}:{self.PORT}/{name}?t={self.counter}"
        try:
            ha_tools._ha("POST", "/api/services/media_player/play_media", {
                "entity_id": self.entity_id, "media_content_id": url,
                "media_content_type": "music", "announce": True})
        except Exception as e:
            log.error("Lecture Sonos impossible : %s", e)

    def on_fetch(self, name):
        duration = self.awaiting.pop(name, None)
        if duration is not None:
            self.play_until = time.monotonic() + duration + 0.4
            log.debug("Sonos lit %s (%.1f s)", name, duration)

    async def run(self):
        await asyncio.Event().wait()


def make_player(device):
    if device.startswith("sonos:"):
        return SonosPlayer(device.split(":", 1)[1])
    return Player(device)


class Conversation:
    """Une session OpenAI Realtime, du mot d'activation jusqu'au retour en veille."""

    def __init__(self, app):
        self.app = app
        self.player = app.player
        self.start = time.monotonic()
        self.last_activity = self.start
        self.heard_speech = False
        self.responding = False
        self.end_requested = False
        self.item_id = None
        self.item_start = 0.0
        self.item_ms = 0.0
        self.recording = bytearray()

    def session_config(self, inventaire=""):
        barge_in = env("BARGE_IN", "0") == "1"
        now = datetime.now()
        date = f"{JOURS[now.weekday()]} {now:%d/%m/%Y}, il est {now:%H:%M}"
        return {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": env("REALTIME_MODEL", "gpt-realtime-mini"),
                "output_modalities": ["audio"],
                "instructions": INSTRUCTIONS.format(date=date, inventaire=inventaire),
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": API_RATE},
                        "noise_reduction": {"type": "far_field"},
                        "transcription": {"model": "gpt-4o-transcribe", "language": "fr"},
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": 0.5,
                            "prefix_padding_ms": 300,
                            "silence_duration_ms": int(env("VAD_SILENCE_MS", "500")),
                            "create_response": True,
                            "interrupt_response": barge_in,
                        },
                    },
                    "output": {
                        "format": {"type": "audio/pcm", "rate": API_RATE},
                        "voice": env("VOICE", "cedar"),
                    },
                },
                "tools": ha_tools.all_tools() + [FIN_CONVERSATION],
                "tool_choice": "auto",
            },
        }

    async def run(self):
        url = "wss://api.openai.com/v1/realtime?model=" + env("REALTIME_MODEL", "gpt-realtime-mini")
        headers = {"Authorization": "Bearer " + env("OPENAI_API_KEY", "")}
        t0 = time.monotonic()

        async def fetch_inventory():
            try:
                return await asyncio.to_thread(ha_tools.inventaire)
            except Exception as e:
                log.warning("Inventaire HA indisponible : %s", e)
                return "(indisponible, utilise lister_appareils)"

        ws, inventaire = await asyncio.gather(
            connect(url, additional_headers=headers, max_size=None, ping_interval=20), fetch_inventory())
        async with ws:
            await ws.send(json.dumps(self.session_config(inventaire)))
            log.info("Connecté à OpenAI en %.0f ms", (time.monotonic() - t0) * 1000)
            tasks = [asyncio.create_task(c) for c in (self.send_audio(ws), self.receive(ws), self.watchdog())]
            try:
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            self.save_recording()
            for t in done:
                t.result()  # remonte l'erreur éventuelle

    async def send_audio(self, ws):
        barge_in = env("BARGE_IN", "0") == "1"
        while True:
            chunk = await self.app.convo_q.get()
            if not barge_in and self.player.is_playing():
                continue  # micro coupé pendant que Jarvis parle (évite qu'il s'entende)
            if len(self.recording) < MIC_RATE * 2 * 90:  # 90 s max de « ce que Jarvis entend »
                self.recording += chunk
            audio = base64.b64encode(to_api_rate(chunk)).decode()
            await ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": audio}))

    def save_recording(self):
        """Écrit www/entree.wav : le son réellement envoyé à OpenAI, pour l'écouter soi-même."""
        if not self.recording:
            return
        www = Path(__file__).with_name("www")
        www.mkdir(exist_ok=True)
        pcm = bytes(self.recording)
        header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16, 1, 1,
                             MIC_RATE, MIC_RATE * 2, 2, 16, b"data", len(pcm))
        (www / "entree.wav").write_bytes(header + pcm)

    async def receive(self, ws):
        async for raw in ws:
            ev = json.loads(raw)
            t = ev.get("type", "")
            if t in ("response.output_audio.delta", "response.audio.delta"):
                if ev.get("item_id") != self.item_id:
                    self.item_id = ev.get("item_id")
                    self.item_start = max(time.monotonic(), self.player.play_until)
                    self.item_ms = 0.0
                pcm = base64.b64decode(ev["delta"])
                self.player.play(pcm)
                self.item_ms += len(pcm) / (API_RATE * 2) * 1000
            elif t == "input_audio_buffer.speech_started":
                self.heard_speech = True
                self.last_activity = time.monotonic()
                if self.player.is_playing(margin=0) and self.item_id:
                    await self.interrupt(ws)
            elif t == "input_audio_buffer.speech_stopped":
                self.last_activity = time.monotonic()
            elif t == "conversation.item.input_audio_transcription.completed":
                log.info("Vous   : %s", ev.get("transcript", "").strip())
            elif t in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
                log.info("Jarvis : %s", ev.get("transcript", "").strip())
            elif t == "response.created":
                self.responding = True
            elif t == "response.done":
                self.responding = False
                if isinstance(self.player, SonosPlayer):
                    await asyncio.to_thread(self.player.flush)
                self.last_activity = time.monotonic()
                await self.handle_response_done(ws, ev.get("response", {}))
            elif t == "error":
                log.error("Erreur OpenAI : %s", ev.get("error", {}).get("message", ev))
            else:
                log.debug("Événement %s", t)

    async def interrupt(self, ws):
        played = min(self.item_ms, (time.monotonic() - self.item_start) * 1000)
        self.player.stop()
        log.info("Interrompu après %.1f s", played / 1000)
        await ws.send(json.dumps({"type": "conversation.item.truncate", "item_id": self.item_id,
                                  "content_index": 0, "audio_end_ms": int(max(0, played))}))

    async def handle_response_done(self, ws, response):
        if response.get("status") != "completed":
            log.warning("Réponse %s : %s", response.get("status"), response.get("status_details"))
        usage = response.get("usage")
        if usage:
            log.debug("Jetons : %s", usage)
        calls = [o for o in response.get("output", []) if o.get("type") == "function_call"]
        need_followup = False
        for call in calls:
            name = call.get("name")
            try:
                args = json.loads(call.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if name == "fin_conversation":
                self.end_requested = True
                result = {"ok": True}
            else:
                result = await asyncio.to_thread(ha_tools.execute, name, args)
                need_followup = True
            log.info("Outil  : %s(%s) -> %s", name, json.dumps(args, ensure_ascii=False),
                     json.dumps(result, ensure_ascii=False)[:200])
            await ws.send(json.dumps({"type": "conversation.item.create", "item": {
                "type": "function_call_output", "call_id": call["call_id"],
                "output": json.dumps(result, ensure_ascii=False)}}))
        if need_followup and not self.end_requested:
            await ws.send(json.dumps({"type": "response.create"}))

    async def watchdog(self):
        first_timeout = float(env("FIRST_SPEECH_TIMEOUT", "6"))
        idle_timeout = float(env("IDLE_TIMEOUT", "8"))
        max_session = float(env("MAX_SESSION", "300"))
        while True:
            await asyncio.sleep(0.25)
            now = time.monotonic()
            if self.player.is_playing() or self.responding:
                self.last_activity = now
                continue
            if self.end_requested:
                log.info("Fin de conversation demandée")
                return
            if not self.heard_speech and now - self.start > first_timeout:
                log.info("Personne n'a parlé : fausse détection probable")
                return
            if now - self.last_activity > idle_timeout:
                log.info("Silence : retour en veille")
                return
            if now - self.start > max_session:
                log.warning("Durée max de session atteinte")
                return


class App:
    def __init__(self, direct=False):
        self.direct = direct
        self.player = make_player(env("SPEAKER_DEVICE", "default"))
        self.state = "idle"
        self.wake_q = asyncio.Queue(maxsize=50)
        self.convo_q = asyncio.Queue(maxsize=200)
        self.wake_event = asyncio.Event()
        self.cooldown_until = 0.0
        self.gain = float(env("MIC_GAIN", "2.5"))

    def on_mic(self, chunk):
        if self.gain != 1.0:  # le ReSpeaker sort un signal faible : amplification numérique
            x = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) * self.gain
            chunk = np.clip(x, -32768, 32767).astype(np.int16).tobytes()
        # Le détecteur reçoit le son en continu (les trous le perturbent) ;
        # pendant une conversation, le son part aussi vers OpenAI.
        queues = [self.wake_q] if self.state == "idle" else [self.wake_q, self.convo_q]
        for q in queues:
            if q.full():
                q.get_nowait()  # on jette le plus ancien plutôt que de bloquer
            q.put_nowait(chunk)

    def on_wake(self, name):
        if self.state != "idle" or time.monotonic() < self.cooldown_until:
            return
        log.info("Mot d'activation détecté (%s)", name)
        self.state = "convo"  # le micro bascule tout de suite vers la conversation
        self.wake_event.set()

    async def reset_mic(self):
        """Le ReSpeaker USB se fige parfois (Input/output error) : on le réinitialise."""
        cmd = env("MIC_RESET_CMD", "sudo -n usbreset 2886:001a")
        if not cmd:
            return
        log.warning("Réinitialisation du micro USB (%s)", cmd)
        proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.DEVNULL,
                                                     stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            log.error("Réinitialisation impossible : %s", err.decode().strip())
        await asyncio.sleep(3)

    async def mic_loop(self):
        device = env("MIC_DEVICE", "plughw:2,0")
        channels, channel = mic_channels()
        failures = 0
        while True:
            proc = await asyncio.create_subprocess_exec(
                "arecord", "-q", "-D", device, "-r", str(MIC_RATE), "-c", str(channels), "-f", "S16_LE",
                "-t", "raw", stdout=asyncio.subprocess.PIPE)
            log.info("Micro ouvert (%s, %d canal/canaux, canal utilisé %s)", device, channels,
                     channel if channels > 1 else "mono")
            started = time.monotonic()
            try:
                while True:
                    raw = await proc.stdout.readexactly(CHUNK_BYTES * channels)
                    failures = 0
                    self.on_mic(select_channel(raw, channels, channel))
            except asyncio.IncompleteReadError:
                failures += 1
                log.error("Le micro s'est arrêté (essai %d). Nouvel essai dans 3 s", failures)
            finally:
                if proc.returncode is None:
                    proc.terminate()
                    await proc.wait()
            if failures >= 3 and time.monotonic() - started < 5:
                await self.reset_mic()
                failures = 0
            await asyncio.sleep(3)

    async def restart_wake_server(self):
        """Le conteneur openwakeword se coince parfois après une déconnexion : on le relance."""
        cmd = env("WAKE_RESTART_CMD", "")
        if not cmd:
            return
        log.info("Redémarrage d'openwakeword (%s)", cmd)
        proc = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.DEVNULL,
                                                     stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        if proc.returncode != 0:
            log.warning("Échec du redémarrage : %s", err.decode().strip())
        await asyncio.sleep(4)

    async def wake_loop(self):
        if env("WAKE_MODE", "local") == "local":
            await self.wake_loop_local()
        else:
            await self.wake_loop_wyoming()

    async def wake_loop_local(self):
        """Détection dans le script (onnxruntime), sans conteneur Docker."""
        import wake
        word = env("WAKE_WORD", "hey_jarvis")
        model = word if word.endswith(("_v0.1", ".onnx")) else word + "_v0.1"
        det = await asyncio.to_thread(wake.WakeDetector, model.removesuffix(".onnx"),
                                      float(env("WAKE_THRESHOLD", "0.5")), int(env("WAKE_PATIENCE", "2")))
        while not self.wake_q.empty():
            self.wake_q.get_nowait()
        log.info("Prêt : dites « Hey Jarvis » (détection locale, modèle %s, seuil %s, persistance %d)",
                 det.name, det.threshold, det.patience)
        n, best, last = 0, 0.0, time.monotonic()
        while True:
            chunk = await self.wake_q.get()
            if await asyncio.to_thread(det.process, chunk):
                self.on_wake(f"{det.name} score {det.score:.2f}")
            n += 1
            best = max(best, det.score)
            if time.monotonic() - last > 10:
                log.debug("Veille : %d chunks analysés, meilleur score %.2f, état %s", n, best, self.state)
                best, last = 0.0, time.monotonic()

    async def wake_loop_wyoming(self):
        host, port = env("WAKE_HOST", "127.0.0.1"), int(env("WAKE_PORT", "10400"))
        word = env("WAKE_WORD", "hey_jarvis")
        while True:
            await self.restart_wake_server()
            while not self.wake_q.empty():
                self.wake_q.get_nowait()
            try:
                async with AsyncTcpClient(host, port) as client:
                    # On attend que le serveur réponde (modèle chargé) avant d'envoyer du son
                    await client.write_event(Describe().event())
                    info = await asyncio.wait_for(client.read_event(), timeout=30)
                    if info is None or not Info.is_type(info.type):
                        raise ConnectionError("pas de réponse à Describe")
                    models = [m.name for w in Info.from_event(info).wake for m in w.models]
                    if word not in models:
                        log.warning("Modèle « %s » absent, modèles disponibles : %s", word, models)
                    await client.write_event(Detect(names=[word]).event())
                    await client.write_event(AudioStart(rate=MIC_RATE, width=2, channels=1).event())
                    while not self.wake_q.empty():
                        self.wake_q.get_nowait()  # on jette le son accumulé pendant l'attente
                    log.info("Prêt : dites « Hey Jarvis » (openwakeword %s:%s, modèle %s)", host, port, word)
                    reader = asyncio.create_task(self.read_detections(client))
                    sent, last = 0, time.monotonic()
                    while not reader.done():
                        chunk = await self.wake_q.get()
                        await client.write_event(
                            AudioChunk(rate=MIC_RATE, width=2, channels=1, audio=chunk).event())
                        sent += 1
                        if time.monotonic() - last > 10:
                            peak = int(np.abs(np.frombuffer(chunk, dtype=np.int16)).max())
                            log.debug("Veille : %d chunks envoyés à openwakeword, niveau %d, état %s",
                                      sent, peak, self.state)
                            last = time.monotonic()
                    reader.result()
            except Exception as e:
                log.error("openwakeword injoignable (%s), nouvel essai dans 5 s", e)
                await asyncio.sleep(5)

    async def read_detections(self, client):
        while True:
            event = await client.read_event()
            if event is None:
                raise ConnectionError("connexion fermée")
            log.debug("openwakeword -> %s %s", event.type, event.data)
            if Detection.is_type(event.type):
                self.on_wake(Detection.from_event(event).name)

    async def run(self):
        background = [asyncio.create_task(self.mic_loop()), asyncio.create_task(self.player.run())]
        if not self.direct:
            background.append(asyncio.create_task(self.wake_loop()))
        beep = make_beep() if env("BEEP", "1") == "1" else None
        while True:
            if self.direct:
                self.state = "convo"
                log.info("Mode direct : parlez !")
            else:
                await self.wake_event.wait()
                self.wake_event.clear()
            if beep and not isinstance(self.player, SonosPlayer):
                self.player.play(beep)
            try:
                await Conversation(self).run()
            except Exception as e:
                log.error("Conversation interrompue : %s", e)
            finally:
                await asyncio.sleep(0.5)
                self.player.stop()
                while not self.convo_q.empty():
                    self.convo_q.get_nowait()
                self.state = "idle"
                self.cooldown_until = time.monotonic() + 2
                log.info("En veille")
            if self.direct:
                return


def main():
    load_env()
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("websockets", "urllib3", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not env("OPENAI_API_KEY", "").startswith("sk-"):
        sys.exit("OPENAI_API_KEY manquante dans config.env")
    try:
        asyncio.run(App(direct="--direct" in sys.argv).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
