# Jarvis

Assistant vocal maison : « Hey Jarvis » → OpenAI Realtime → haut-parleur du Pi, avec pilotage de Home Assistant.

- `realtime/jarvis.py` : programme principal (micro, mot d'activation, session Realtime, lecture audio)
- `realtime/ha_tools.py` : outils Home Assistant (lister_appareils, etat, commander)
- `realtime/config.env.example` : réglages à copier en `config.env`
- `realtime/jarvis-realtime.service` : service systemd

Logs : `journalctl -u jarvis-realtime -f`
