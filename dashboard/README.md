# Tableau de bord Jarvis

Trois fichiers :

- `jarvis_theme.yaml` : thème sombre « verre fumé » (dossier `themes/` de HA)
- `jarvis_package.yaml` : helpers + scripts (annonce Alexa, bonne nuit, on part, mode cinéma)
- `jarvis.yaml` : le tableau de bord lui-même (à coller dans l'éditeur brut d'un nouveau tableau de bord)

## 1. Cartes HACS à installer (HACS → Frontend → Explorer)

- Bubble Card
- Mushroom
- button-card
- card-mod
- ApexCharts Card (optionnel, pour plus tard)

Après installation, recharger la page du navigateur (Ctrl+F5).

## 2. Copier le thème et le package sur le Pi

```
cd ~/jarvis && git pull
sudo mkdir -p /home/jarvis/ha/themes /home/jarvis/ha/packages
sudo cp dashboard/jarvis_theme.yaml /home/jarvis/ha/themes/jarvis.yaml
sudo cp dashboard/jarvis_package.yaml /home/jarvis/ha/packages/jarvis.yaml
```

Puis déclarer les deux dossiers dans `configuration.yaml` (une seule fois) :

```
sudo nano /home/jarvis/ha/configuration.yaml
```

Ajouter / compléter :

```yaml
frontend:
  themes: !include_dir_merge_named themes

homeassistant:
  packages: !include_dir_named packages
```

(si une ligne `homeassistant:` ou `frontend:` existe déjà, ajouter seulement la sous-ligne dedans).

Redémarrer HA : Paramètres → Système → Redémarrer. Ensuite, dans le profil utilisateur (clic sur le
nom en bas à gauche) → Thème : choisir **Jarvis**, mode sombre.

## 3. Créer le tableau de bord

Paramètres → Tableaux de bord → **Ajouter un tableau de bord** → « Nouveau tableau de bord à partir
de zéro », titre `Jarvis`, icône `mdi:robot-outline`. L'ouvrir → crayon (en haut à droite) → menu ⋮ →
**Éditeur de configuration brute** → tout effacer, coller le contenu de `jarvis.yaml` → Enregistrer.

Pour afficher le fichier sur le Pi afin de le copier : `cat ~/jarvis/dashboard/jarvis.yaml`
(ou l'ouvrir sur GitHub depuis le PC et copier).

## 4. À adapter

- `person.jose` / `person.maman` : vos vraies entités `person.*` (Paramètres → Personnes), avec une
  photo chacune.
- `camera.*` : noms réels des caméras Blink.
- `todo.liste_de_courses` : créer une liste de tâches (Paramètres → Appareils et services → Helpers →
  Créer → Liste de tâches locale, nom « Liste de courses »).
