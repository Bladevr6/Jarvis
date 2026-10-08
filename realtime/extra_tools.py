"""Outils supplémentaires : météo, agenda, dernier mail, annonces, minuteurs, « demander à Claude ».

Chaque outil = une fonction + sa description dans TOOLS + une entrée dans HANDLERS.
"""
import logging
import os
import threading
import time
from datetime import datetime, timedelta

from ha_tools import _ha

log = logging.getLogger("outils")
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def env(key, default=None):
    return os.environ.get(key, default)


# ---------------------------------------------------------------- météo
def _weather_entity():
    forced = env("WEATHER_ENTITY", "")
    if forced:
        return forced
    ids = [s["entity_id"] for s in _ha("GET", "/api/states").json() if s["entity_id"].startswith("weather.")]
    if not ids:
        raise RuntimeError("aucune entité weather.* dans Home Assistant")
    return ids[0]


def meteo(quand="aujourd_hui"):
    entity = _weather_entity()
    actuel = _ha("GET", f"/api/states/{entity}").json()
    a = actuel.get("attributes", {})
    out = {"maintenant": {"etat": actuel.get("state"), "temperature": a.get("temperature"),
                          "humidite": a.get("humidity"), "vent_kmh": a.get("wind_speed")}}
    kind = "hourly" if quand == "heures" else "daily"
    r = _ha("POST", f"/api/services/weather/get_forecasts?return_response", {
        "entity_id": entity, "type": kind}).json()
    previsions = r.get("service_response", {}).get(entity, {}).get("forecast", [])
    lignes = []
    for p in previsions[:8 if kind == "hourly" else 4]:
        d = datetime.fromisoformat(p["datetime"]).astimezone()
        label = f"{d:%H}h" if kind == "hourly" else f"{JOURS[d.weekday()]} {d:%d/%m}"
        lignes.append({"quand": label, "etat": p.get("condition"), "max": p.get("temperature"),
                       "min": p.get("templow"), "pluie_mm": p.get("precipitation"),
                       "proba_pluie_pct": p.get("precipitation_probability")})
    out["previsions"] = lignes
    return out


# ---------------------------------------------------------------- agenda
def _calendars():
    forced = env("CALENDAR_ENTITIES", "")
    if forced:
        return [c.strip() for c in forced.split(",") if c.strip()]
    return [s["entity_id"] for s in _ha("GET", "/api/states").json() if s["entity_id"].startswith("calendar.")]


def agenda(jours=1):
    debut = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    fin = debut + timedelta(days=int(jours))
    evenements = []
    for cal in _calendars():
        r = _ha("POST", "/api/services/calendar/get_events?return_response", {
            "entity_id": cal, "start_date_time": debut.isoformat(), "end_date_time": fin.isoformat()}).json()
        for e in r.get("service_response", {}).get(cal, {}).get("events", []):
            start = e.get("start", "")
            if "T" in start:
                d = datetime.fromisoformat(start).astimezone()
                quand = f"{JOURS[d.weekday()]} {d:%d/%m} à {d:%H:%M}"
            else:
                d = datetime.fromisoformat(start)
                quand = f"{JOURS[d.weekday()]} {d:%d/%m} (toute la journée)"
            evenements.append({"_t": start, "quand": quand, "titre": e.get("summary"),
                               "lieu": e.get("location"), "agenda": cal.split(".", 1)[1]})
    evenements.sort(key=lambda e: e.pop("_t"))
    return {"periode": f"{jours} jour(s) à partir d'aujourd'hui", "evenements": evenements or "aucun événement"}


EXCLUS_ECRITURE = ("semaine", "holiday", "ferie", "férié", "birthday", "anniversaire", "contacts")


def _calendar_for_write():
    forced = env("CALENDAR_WRITE_ENTITY", "")
    if forced:
        return forced
    for cal in _calendars():
        if not any(x in cal.lower() for x in EXCLUS_ECRITURE):
            return cal
    return None


def ajouter_evenement(titre, debut, duree_minutes=60, agenda_entity=""):
    cal = agenda_entity or _calendar_for_write()
    if not cal:
        return {"erreur": "aucun agenda modifiable trouvé"}
    d = datetime.fromisoformat(debut)
    f = d + timedelta(minutes=int(duree_minutes))
    _ha("POST", "/api/services/calendar/create_event", {
        "entity_id": cal, "summary": titre, "start_date_time": d.strftime("%Y-%m-%d %H:%M:%S"),
        "end_date_time": f.strftime("%Y-%m-%d %H:%M:%S")})
    return {"ok": True, "agenda": cal, "quand": f"{JOURS[d.weekday()]} {d:%d/%m} à {d:%H:%M}"}


# ---------------------------------------------------------------- mail
def dernier_mail():
    entity = env("MAIL_ENTITY", "input_text.dernier_mail")
    s = _ha("GET", f"/api/states/{entity}").json()
    return {"dernier_mail": s.get("state"), "mis_a_jour": s.get("last_changed")}


# ---------------------------------------------------------------- annonces
def _notify_services():
    for domain in _ha("GET", "/api/services").json():
        if domain["domain"] == "notify":
            return sorted(domain["services"].keys())
    return []


def annoncer(message, cible="sonos"):
    """cible : 'sonos' (enceinte du salon), 'partout', ou le nom d'un service notify.alexa_media_* / un Echo."""
    cible = cible.lower().strip()
    if cible == "sonos":
        _ha("POST", "/api/services/tts/speak", {
            "entity_id": env("TTS_ENTITY", "tts.openai_tts_jarvis"),
            "media_player_entity_id": env("SONOS_ENTITY", "media_player.sonos_sonos"),
            "message": message})
        return {"ok": True, "cible": "sonos"}
    services = [s for s in _notify_services() if s.startswith("alexa_media")]
    if cible == "partout":
        voulus = [e.strip() for e in env("ALEXA_ECHOS", "bureau,echo_salon,echo_spot,mia").split(",")]
        choisis = [s for s in services if any(v and v in s for v in voulus)]
    else:
        key = cible.replace(" ", "_").replace("-", "_")
        choisis = [s for s in services if key in s]
    if not choisis:
        return {"erreur": f"aucun Echo ne correspond à « {cible} »", "disponibles": services}
    for s in choisis:
        _ha("POST", f"/api/services/notify/{s}", {"message": message, "data": {"type": "announce"}})
    return {"ok": True, "cibles": choisis}


# ---------------------------------------------------------------- minuteurs / rappels
_timers = {}


def _fire(nom, message):
    _timers.pop(nom, None)
    log.info("Minuteur « %s » terminé : annonce « %s »", nom, message)
    try:
        annoncer(message, "sonos")
    except Exception as e:  # on ne veut pas perdre le fil
        log.error("Annonce du minuteur impossible : %s", e)


def minuteur(minutes=0, heure="", nom="minuteur", message=""):
    if heure:
        h, m = (heure.split(":") + ["0"])[:2]
        quand = datetime.now().replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        if quand < datetime.now():
            quand += timedelta(days=1)
        delai = (quand - datetime.now()).total_seconds()
    else:
        delai = float(minutes) * 60
    if delai <= 0:
        return {"erreur": "durée invalide"}
    if nom in _timers:
        _timers[nom].cancel()
    texte = message or f"Monsieur, votre {nom} est terminé."
    t = threading.Timer(delai, _fire, args=(nom, texte))
    t.daemon = True
    t.start()
    _timers[nom] = t
    fin = datetime.now() + timedelta(seconds=delai)
    return {"ok": True, "nom": nom, "sonne_a": f"{fin:%H:%M}", "dans_minutes": round(delai / 60, 1)}


def annuler_minuteur(nom=""):
    cibles = [nom] if nom else list(_timers)
    for n in cibles:
        if n in _timers:
            _timers.pop(n).cancel()
    return {"ok": True, "annules": cibles, "restants": list(_timers)}


# ---------------------------------------------------------------- Claude
def demander_a_claude(question, recherche_web=True):
    if not env("ANTHROPIC_API_KEY"):
        return {"erreur": "ANTHROPIC_API_KEY absente de config.env"}
    import anthropic
    client = anthropic.Anthropic()
    tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}] if recherche_web else []
    messages = [{"role": "user", "content": question}]
    system = ("Tu réponds à une question posée à voix haute à un assistant vocal domestique en France. "
              "Réponds en français, en 2 à 4 phrases, sans listes ni mise en forme, prêt à être lu à voix haute. "
              f"Nous sommes le {datetime.now():%d/%m/%Y %H:%M}.")
    for _ in range(3):
        r = client.messages.create(model=env("CLAUDE_MODEL", "claude-opus-5-5"), max_tokens=1024,
                                   system=system, messages=messages, tools=tools)
        if r.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": r.content})
            continue
        if r.stop_reason == "refusal":
            return {"reponse": "Je préfère ne pas répondre à cela."}
        texte = " ".join(b.text for b in r.content if b.type == "text").strip()
        return {"reponse": texte}
    return {"erreur": "réponse trop longue à obtenir"}


TOOLS = [
    {"type": "function", "name": "meteo",
     "description": "Météo actuelle et prévisions (Met.no). quand = aujourd_hui (4 prochains jours) ou heures (prochaines heures).",
     "parameters": {"type": "object", "properties": {
         "quand": {"type": "string", "enum": ["aujourd_hui", "heures"]}}}},
    {"type": "function", "name": "agenda",
     "description": "Événements de l'agenda familial (Google Calendar) pour les N prochains jours à partir d'aujourd'hui.",
     "parameters": {"type": "object", "properties": {"jours": {"type": "integer", "minimum": 1, "maximum": 30}}}},
    {"type": "function", "name": "ajouter_evenement",
     "description": "Ajoute un rendez-vous à l'agenda familial. debut au format ISO local AAAA-MM-JJTHH:MM. "
                    "Ne demande pas la durée : 60 minutes par défaut sauf si l'utilisateur la précise.",
     "parameters": {"type": "object", "properties": {
         "titre": {"type": "string"}, "debut": {"type": "string"},
         "duree_minutes": {"type": "integer"}}, "required": ["titre", "debut"]}},
    {"type": "function", "name": "dernier_mail",
     "description": "Résumé du dernier e-mail reçu (Gmail).",
     "parameters": {"type": "object", "properties": {}}},
    {"type": "function", "name": "annoncer",
     "description": "Fait une annonce vocale. cible = sonos (salon), partout (tous les Echos), ou le nom d'une pièce/Echo "
                    "(bureau, salon, mia, spot). Ex. « dis à Mia de descendre » -> cible mia.",
     "parameters": {"type": "object", "properties": {
         "message": {"type": "string"}, "cible": {"type": "string"}}, "required": ["message"]}},
    {"type": "function", "name": "minuteur",
     "description": "Programme un minuteur (minutes) ou un rappel à une heure (HH:MM). Annonce le message sur le Sonos à l'échéance.",
     "parameters": {"type": "object", "properties": {
         "minutes": {"type": "number"}, "heure": {"type": "string"},
         "nom": {"type": "string", "description": "ex. pâtes, poubelles"},
         "message": {"type": "string", "description": "phrase à annoncer"}}}},
    {"type": "function", "name": "annuler_minuteur",
     "description": "Annule un minuteur par son nom, ou tous si nom vide.",
     "parameters": {"type": "object", "properties": {"nom": {"type": "string"}}}},
    {"type": "function", "name": "demander_a_claude",
     "description": "Pose une question complexe (culture générale, actualité, calcul, conseil, explication) à Claude, "
                    "qui peut chercher sur le web. À utiliser quand la réponse demande des connaissances précises ou récentes.",
     "parameters": {"type": "object", "properties": {
         "question": {"type": "string"}, "recherche_web": {"type": "boolean"}}, "required": ["question"]}},
]

HANDLERS = {
    "meteo": meteo, "agenda": agenda, "ajouter_evenement": ajouter_evenement, "dernier_mail": dernier_mail,
    "annoncer": annoncer, "minuteur": minuteur, "annuler_minuteur": annuler_minuteur,
    "demander_a_claude": demander_a_claude,
}


if __name__ == "__main__":
    # Tests : python3 extra_tools.py meteo | agenda | mail | notify | claude "question"
    import json
    import sys
    from jarvis import load_env
    load_env()
    what = sys.argv[1] if len(sys.argv) > 1 else "meteo"
    if what == "notify":
        res = _notify_services()
    elif what == "claude":
        res = demander_a_claude(" ".join(sys.argv[2:]) or "Quelle est la capitale de l'Australie ?")
    elif what == "mail":
        res = dernier_mail()
    elif what == "agenda":
        res = agenda(7)
    else:
        res = meteo()
    print(json.dumps(res, ensure_ascii=False, indent=1, default=str))
