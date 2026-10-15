# TICKET — Fonctionnalités non implémentées / limitations

## Contexte
Projet : Jarvis Voice (assistant vocal Windows) + préparation `.exe` standalone.
Branche : `arena/d565c389-jarvis`

## 1. Compilation croisée du `.exe` Windows
- **Statut** : non réalisable dans cet environnement (sandbox Linux).
- **Raison** : pas d'interpréteur Python Windows, pas de `wine`, pas de headers/lib Python de développement, permissions GitHub Actions insuffisantes pour déclencher le workflow.
- **Solution** : lancer `build_windows.bat` sur un PC Windows (ou runner `windows-latest`). Le projet est prêt (spec, script, workflow).

## 2. Interface 3D holographique (prompt fourni)
- **Statut** : partiellement implémenté (UI 2D tkinter améliorée).
- **Non fait** :
  - Visualisation 3D du corps humain (squelette, muscles, organes) → nécessite Three.js / modèles 3D.
  - Reconnaissance gestuelle par webcam → nécessite MediaPipe/OpenCV.
  - Scanner 3D sur objet réel.
  - Effets WebGL/shaders.
- **Raison** : le projet existant est en tkinter 2D ; passer au 3D demande une réécriture de la stack (React + Three.js ou moteur natif).
- **Piste** : créer une app web qui se connecte au backend Python, puis packager avec Electron/Tauri.

## 3. Connexion Ollama (Qwen 3.5 4B)
- **Statut** : non implémenté.
- **Raison** : pas de serveur Ollama disponible dans le sandbox ; l'assistant utilise OpenRouter.
- **Piste** : ajouter un client HTTP vers `http://localhost:11434` (Ollama) avec fallback OpenRouter.

## 4. Tests automatisés
- **Statut** : non réalisés.
- **Raison** : le code est fortement couplé à Windows (pycaw, win32, sounddevice). Pas d'environnement Windows de test.
- **Piste** : mocks pour les modules Windows + tests unitaires sur les handlers de commandes.

## 5. Reconnaissance vocale réelle
- **Statut** : non testable ici (pas de micro / pas de Windows).
- **Raison** : dépend de SpeechRecognition + micro + Windows.
- **Piste** : tests manuels sur PC Windows.

## 6. Zoom exact comme dans la vidéo
- **Statut** : implémenté de manière générique (molette, +/-, 0).
- **Raison** : la vidéo n'a pas pu être visionnée (pas de capacité de vision).
- **Piste** : ajuster selon description utilisateur (zoom automatique vs interactif, cible, amplitude).

## 7. Domotique réelle
- **Statut** : mode démonstration + intégration Home Assistant (si configuré).
- **Raison** : pas de serveur Home Assistant dans le sandbox.
- **Piste** : configurer `HOME_ASSISTANT_URL` et `HOME_ASSISTANT_TOKEN` sur le PC.

## 8. Agent agentique avancé
- **Statut** : implémenté (planification + exécution + rapport).
- **Limitation** : l'exécution des étapes utilise OpenRouter (pas d'accès web réel, pas d'outils système). Pas de mémoire persistante entre sessions.
- **Piste** : ajouter des outils réels (recherche web, lecture de fichiers workspace, calcul) et une mémoire.

## 9. Icône / signature du `.exe`
- **Statut** : non fait.
- **Piste** : ajouter `icon='assets/jarvis.ico'` dans `Jarvis.spec` et signer avec un certificat.

## 10. UI tkinter non disponible dans le sandbox
- **Statut** : le code compile mais ne s'exécute pas ici (pas de tkinter).
- **Raison** : `python3-tk` non installable sans réseau apt.
- **Piste** : tester sur Windows.

---
Généré automatiquement. Mettre à jour au fur et à mesure de l'avancement.
