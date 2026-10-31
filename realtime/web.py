#!/usr/bin/env python3
"""Jarvis sur la tablette : serveur web léger (page + jetons OpenAI + exécution des outils).

La page (web/) parle directement à OpenAI Realtime en WebRTC depuis le navigateur de la tablette.
Ce serveur ne fait que : servir la page, fabriquer une clé temporaire OpenAI avec les consignes
et les outils de Jarvis, et exécuter les outils Home Assistant demandés par le modèle.

Il écoute en local (127.0.0.1) ; on l'expose en HTTPS sur le réseau privé Tailscale :
    sudo tailscale serve --bg 8810
"""
import json
import logging
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import requests

import ha_tools
from jarvis import FIN_CONVERSATION, INSTRUCTIONS, JOURS, env, load_env

log = logging.getLogger("web")
WEB_DIR = Path(__file__).with_name("web")
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "media-src 'self' blob:; connect-src 'self' https://api.openai.com; base-uri 'none'; form-action 'none'")
WEB_EXTRA = """
Tu es sur la tablette de la maison : l'utilisateur vient d'appuyer sur le bouton pour te parler et se trouve devant toi.
Ne salue pas le premier, attends sa demande. Quand il a fini (« merci », « c'est tout », « bonne nuit »), réponds en quelques mots puis appelle fin_conversation."""


def session_update(inventaire):
    now = datetime.now()
    date = f"{JOURS[now.weekday()]} {now:%d/%m/%Y}, il est {now:%H:%M}"
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": INSTRUCTIONS.format(date=date, inventaire=inventaire) + WEB_EXTRA,
            "output_modalities": ["audio"],
            "audio": {"input": {
                "noise_reduction": {"type": env("WEB_NOISE", "near_field")},
                "transcription": {"model": "gpt-4o-transcribe", "language": "fr"},
                "turn_detection": {"type": "server_vad", "threshold": 0.5, "prefix_padding_ms": 300,
                                   "silence_duration_ms": int(env("VAD_SILENCE_MS", "600")),
                                   "create_response": True, "interrupt_response": True},
            }},
            "tools": ha_tools.all_tools() + [FIN_CONVERSATION],
            "tool_choice": "auto",
        },
    }


def mint_session():
    """Crée une clé temporaire OpenAI (valable ~1 minute) pour que le navigateur se connecte seul."""
    model = env("REALTIME_MODEL", "gpt-realtime")
    try:
        inventaire = ha_tools.inventaire()
    except Exception as e:
        log.warning("Inventaire HA indisponible : %s", e)
        inventaire = "(indisponible, utilise lister_appareils)"
    r = requests.post(
        "https://api.openai.com/v1/realtime/client_secrets",
        headers={"Authorization": "Bearer " + env("OPENAI_API_KEY", ""), "Content-Type": "application/json",
                 "OpenAI-Safety-Identifier": "jarvis-maison"},
        json={"session": {"type": "realtime", "model": model,
                          "audio": {"output": {"voice": env("VOICE", "cedar")}}}},
        timeout=15)
    if r.status_code >= 300:
        raise RuntimeError(f"OpenAI {r.status_code} : {r.text[:300]}")
    data = r.json()
    key = data.get("value") or (data.get("client_secret") or {}).get("value")
    if not key:
        raise RuntimeError("Réponse OpenAI sans clé : " + json.dumps(data)[:300])
    return {"key": key, "model": model, "update": session_update(inventaire)}


class Handler(BaseHTTPRequestHandler):
    server_version = "JarvisWeb"

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Permissions-Policy", "microphone=(self)")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json")

    def _allowed(self):
        users = [u.strip().lower() for u in env("WEB_ALLOWED_USERS", "").split(",") if u.strip()]
        return not users or self.headers.get("Tailscale-User-Login", "").lower() in users

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/health":
            return self._json(200, {"ok": True})
        if not self._allowed():
            return self._json(403, {"error": "Accès refusé"})
        if path in FILES:
            name, ctype = FILES[path]
            return self._send(200, (WEB_DIR / name).read_bytes(), ctype)
        self._json(404, {"error": "introuvable"})

    def do_POST(self):
        path = urlparse(self.path).path
        origin = self.headers.get("Origin")
        # Un en-tête personnalisé + même origine : une autre page web ne peut pas piloter la maison
        if (origin and urlparse(origin).netloc != self.headers.get("Host")) or not self.headers.get("X-Jarvis"):
            return self._json(403, {"error": "Requête refusée"})
        if not self._allowed():
            return self._json(403, {"error": "Accès refusé"})
        length = int(self.headers.get("Content-Length") or 0)
        if length > 200_000:
            return self._json(413, {"error": "trop gros"})
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "JSON invalide"})
        if path == "/api/session":
            try:
                return self._json(200, mint_session())
            except Exception as e:
                log.error("Session impossible : %s", e)
                return self._json(502, {"error": str(e)})
        if path == "/api/tool":
            name, args = body.get("name"), body.get("args") or {}
            if name not in {t["name"] for t in ha_tools.all_tools()} or not isinstance(args, dict):
                return self._json(400, {"error": "outil inconnu"})
            result = ha_tools.execute(name, args)
            log.info("Outil  : %s(%s) -> %s", name, json.dumps(args, ensure_ascii=False),
                     json.dumps(result, ensure_ascii=False)[:200])
            return self._json(200, result)
        self._json(404, {"error": "introuvable"})


def main():
    load_env()
    logging.basicConfig(level=env("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if not env("OPENAI_API_KEY", "").startswith("sk-"):
        sys.exit("OPENAI_API_KEY manquante dans config.env")
    host, port = env("WEB_HOST", "127.0.0.1"), int(env("WEB_PORT", "8810"))
    server = ThreadingHTTPServer((host, port), Handler)
    log.info("Jarvis tablette prêt sur http://%s:%d (à exposer avec : sudo tailscale serve --bg %d)", host, port, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
