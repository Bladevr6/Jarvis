"""Détection du mot d'activation dans le script, sans Docker.

Version allégée du pipeline openWakeWord (melspectrogramme -> embeddings Google -> modèle hey_jarvis)
qui n'a besoin que de numpy et onnxruntime. Les trois modèles ONNX sont téléchargés au premier
lancement dans le dossier models/.

Test : python3 wake.py   (affiche le score en direct, Ctrl+C pour quitter)
"""
import logging
import os
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np

log = logging.getLogger("wake")

MODELS_DIR = Path(__file__).with_name("models")
RELEASE = "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/"
SHARED = ["melspectrogram.onnx", "embedding_model.onnx"]
CHUNK = 1280  # 80 ms à 16 kHz, comme openWakeWord


def _download(name):
    path = MODELS_DIR / name
    if path.exists():
        return path
    import requests
    MODELS_DIR.mkdir(exist_ok=True)
    log.info("Téléchargement du modèle %s", name)
    r = requests.get(RELEASE + name, timeout=60)
    r.raise_for_status()
    path.write_bytes(r.content)
    return path


class WakeDetector:
    def __init__(self, model="hey_jarvis_v0.1", threshold=0.5, patience=1, cooldown=2.0):
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        sess = lambda name: ort.InferenceSession(str(_download(name)), opts, providers=["CPUExecutionProvider"])
        self.mel = sess("melspectrogram.onnx")
        self.emb = sess("embedding_model.onnx")
        self.ww = sess(model + ".onnx")
        self.name = model
        self.threshold = threshold
        self.patience = patience
        self.cooldown = cooldown
        self.raw = deque(maxlen=16000 * 2)   # 2 s d'audio brut
        self.mels = np.ones((76, 32), dtype=np.float32)
        self.feats = np.zeros((16, 96), dtype=np.float32)
        self.pending = b""
        self.above = 0
        self.last_detection = 0.0
        self.score = 0.0
        self.started = time.monotonic()

    def _melspec(self, samples):
        x = np.asarray(samples, dtype=np.float32)[None, :]
        out = self.mel.run(None, {"input": x})[0]
        return np.squeeze(out) / 10 + 2

    def _step(self, chunk):
        """Un chunk de 1280 échantillons int16 -> score."""
        self.raw.extend(chunk)
        if len(self.raw) < CHUNK + 480:
            return 0.0
        new = self._melspec(list(self.raw)[-(CHUNK + 480):])
        self.mels = np.vstack((self.mels, new))[-76 * 2:]
        window = self.mels[-76:].astype(np.float32)[None, :, :, None]
        emb = self.emb.run(None, {"input_1": window})[0].reshape(1, 96)
        self.feats = np.vstack((self.feats, emb))[-16:]
        return float(self.ww.run(None, {"x.1": self.feats[None, :, :]})[0][0][0])

    def process(self, pcm: bytes) -> bool:
        """Reçoit du PCM 16 bits mono 16 kHz (taille quelconque). Renvoie True sur détection."""
        self.pending += pcm
        detected = False
        while len(self.pending) >= CHUNK * 2:
            chunk = np.frombuffer(self.pending[:CHUNK * 2], dtype=np.int16)
            self.pending = self.pending[CHUNK * 2:]
            self.score = self._step(chunk)
            if time.monotonic() - self.started < 2:
                continue  # le tampon se remplit, scores non significatifs
            if self.score >= self.threshold:
                self.above += 1
            else:
                self.above = 0
            now = time.monotonic()
            if self.above >= self.patience and now - self.last_detection > self.cooldown:
                self.last_detection = now
                self.above = 0
                detected = True
        return detected


def bench():
    """python3 wake.py --bench : chronomètre chaque étape, sans micro."""
    t = time.time()
    det = WakeDetector()
    print(f"chargement des modèles : {time.time() - t:.1f} s", flush=True)
    rng = np.random.default_rng(0)
    for i in range(12):
        chunk = (rng.standard_normal(CHUNK) * 500).astype(np.int16).tobytes()
        t = time.time()
        det.process(chunk)
        print(f"chunk {i + 1:2d} : {(time.time() - t) * 1000:6.0f} ms  (budget 80 ms)", flush=True)


if __name__ == "__main__":
    # Test en direct : affiche le score à chaque instant et signale les détections
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if "--bench" in sys.argv:
        bench()
        sys.exit()
    sys.path.insert(0, str(Path(__file__).parent))
    from jarvis import load_env, env
    load_env()
    det = WakeDetector(threshold=float(env("WAKE_THRESHOLD", "0.5")))
    proc = subprocess.Popen(["arecord", "-q", "-D", env("MIC_DEVICE", "plughw:2,0"), "-r", "16000",
                             "-c", "1", "-f", "S16_LE", "-t", "raw"], stdout=subprocess.PIPE)
    print("Dites « Hey Jarvis ». Score en direct (seuil %.2f). Ctrl+C pour quitter." % det.threshold)
    best, t_last, t0 = 0.0, time.monotonic(), time.monotonic()
    try:
        while True:
            pcm = proc.stdout.read(CHUNK * 2)
            t = time.monotonic()
            hit = det.process(pcm)
            cost = (time.monotonic() - t) * 1000
            best = max(best, det.score)
            if hit:
                print(f"*** DÉTECTÉ (score {det.score:.2f}) ***", flush=True)
            if t - t_last > 1:
                bar = "#" * int(best * 40)
                print(f"max 1s {best:.2f} {bar:<40} calcul {cost:.0f} ms / 80 ms", flush=True)
                best, t_last = 0.0, t
    except KeyboardInterrupt:
        proc.kill()
