"""Outils Home Assistant appelables par le modèle (function calling).

Pour ajouter un outil : 1) une fonction python, 2) sa description dans TOOLS,
3) une entrée dans HANDLERS.
"""
import json
import os

import requests

# Domaines et services que Jarvis a le droit d'utiliser
ALLOWED_SERVICES = {
    "light": {"turn_on", "turn_off", "toggle"},
    "cover": {"open_cover", "close_cover", "stop_cover", "set_cover_position"},
    "switch": {"turn_on", "turn_off", "toggle"},
    "scene": {"turn_on"},
    "script": {"turn_on"},
    "media_player": {"media_play", "media_pause", "media_stop", "volume_set", "turn_on", "turn_off"},
    "climate": {"set_temperature", "turn_on", "turn_off"},
    "fan": {"turn_on", "turn_off"},
}
LIST_DOMAINS = set(ALLOWED_SERVICES) | {"weather", "sensor", "binary_sensor", "calendar", "input_text"}
MAX_LINES = 60


def _ha(method, path, payload=None):
    url = os.environ.get("HA_URL", "http://127.0.0.1:8123").rstrip("/") + path
    headers = {"Authorization": "Bearer " + os.environ["HA_TOKEN"]}
    r = requests.request(method, url, headers=headers, json=payload, timeout=10)
    r.raise_for_status()
    return r


def lister_appareils(domaine, filtre=""):
    if domaine not in LIST_DOMAINS:
        return {"erreur": f"domaine non autorisé, choisir parmi {sorted(LIST_DOMAINS)}"}
    template = (
        "{%- for s in states." + domaine + " -%}"
        "{{ s.entity_id }} | {{ s.name }} | {{ s.state }} | {{ area_name(s.entity_id) or '' }}\n"
        "{% endfor -%}"
    )
    lines = _ha("POST", "/api/template", {"template": template}).text.splitlines()
    if filtre:
        f = filtre.lower()
        lines = [l for l in lines if f in l.lower()]
    return {"format": "entity_id | nom | état | pièce", "appareils": lines[:MAX_LINES]}


INVENTORY_DOMAINS = ["light", "cover", "switch", "scene", "script", "media_player", "climate", "fan"]


def inventaire():
    """Texte compact de tous les appareils pilotables, injecté dans les consignes du modèle."""
    template = "".join(
        "{%- for s in states." + d + " -%}"
        "{{ s.entity_id }} | {{ s.name }} | {{ area_name(s.entity_id) or '?' }} | {{ s.state }}\n"
        "{% endfor -%}" for d in INVENTORY_DOMAINS)
    lines = [l for l in _ha("POST", "/api/template", {"template": template}).text.splitlines() if l.strip()]
    return "\n".join(lines[:150])


def etat(entity_id):
    s = _ha("GET", f"/api/states/{entity_id}").json()
    attrs = {k: v for k, v in s.get("attributes", {}).items()
             if k not in ("entity_picture", "supported_features", "icon", "supported_color_modes")}
    return {"entity_id": entity_id, "etat": s.get("state"), "attributs": attrs}


def commander(domaine, service, entity_id, donnees=None, confirme=False):
    if service not in ALLOWED_SERVICES.get(domaine, set()):
        return {"erreur": f"service {domaine}.{service} non autorisé"}
    ids = [entity_id] if isinstance(entity_id, str) else list(entity_id)
    if not ids or not all(i.startswith(domaine + ".") for i in ids):
        return {"erreur": "entity_id ne correspond pas au domaine"}
    if domaine == "cover" and service == "open_cover" and any("garage" in i for i in ids) and not confirme:
        return {"erreur": "Confirmation requise : demande à l'utilisateur s'il confirme "
                          "l'ouverture du garage, puis rappelle avec confirme=true."}
    payload = dict(donnees or {})
    payload["entity_id"] = ids
    _ha("POST", f"/api/services/{domaine}/{service}", payload)
    return {"ok": True, "appareils": len(ids)}


TOOLS = [
    {
        "type": "function",
        "name": "lister_appareils",
        "description": "Liste les appareils Home Assistant d'un domaine (entity_id, nom, état, pièce). "
                       "À utiliser pour trouver le bon entity_id avant de commander.",
        "parameters": {
            "type": "object",
            "properties": {
                "domaine": {"type": "string", "enum": sorted(LIST_DOMAINS)},
                "filtre": {"type": "string", "description": "Texte à chercher (pièce, nom), optionnel"},
            },
            "required": ["domaine"],
        },
    },
    {
        "type": "function",
        "name": "etat",
        "description": "Donne l'état détaillé d'une entité Home Assistant (ex. weather.forecast_home).",
        "parameters": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
        },
    },
    {
        "type": "function",
        "name": "commander",
        "description": "Appelle un service Home Assistant : allumer/éteindre une lumière, ouvrir/fermer un store, etc. "
                       "Exemples : light.turn_on avec donnees {\"brightness_pct\": 50}, "
                       "cover.set_cover_position avec donnees {\"position\": 30}.",
        "parameters": {
            "type": "object",
            "properties": {
                "domaine": {"type": "string", "enum": sorted(ALLOWED_SERVICES)},
                "service": {"type": "string"},
                "entity_id": {"type": "array", "items": {"type": "string"},
                              "description": "Un ou plusieurs entity_id (toutes les lampes d'une pièce en un seul appel)"},
                "donnees": {"type": "object", "description": "Paramètres optionnels du service"},
                "confirme": {"type": "boolean", "description": "true seulement si l'utilisateur a confirmé"},
            },
            "required": ["domaine", "service", "entity_id"],
        },
    },
]

HANDLERS = {
    "lister_appareils": lister_appareils,
    "etat": etat,
    "commander": commander,
}


def all_tools():
    import extra_tools
    return TOOLS + extra_tools.TOOLS


def execute(name, args):
    import extra_tools
    fn = HANDLERS.get(name) or extra_tools.HANDLERS.get(name)
    if fn is None:
        return {"erreur": f"outil inconnu : {name}"}
    try:
        return fn(**args)
    except requests.HTTPError as e:
        return {"erreur": f"Home Assistant a répondu {e.response.status_code}"}
    except Exception as e:  # le modèle doit toujours recevoir une réponse
        return {"erreur": str(e)}


if __name__ == "__main__":
    # Test rapide sans OpenAI : python3 ha_tools.py light
    import sys
    from jarvis import load_env
    load_env()
    print(json.dumps(lister_appareils(sys.argv[1] if len(sys.argv) > 1 else "light"),
                     ensure_ascii=False, indent=1))
