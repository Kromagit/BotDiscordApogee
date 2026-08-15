# Apogee Raid-Helper Bot

Bot Discord destiné à faire le lien entre les inscriptions Raid-Helper et Kromaddon.

## Ce que fait le bot

- Chaque joueur poste uniquement le nom exact de son main dans `#Main`.
- Le bot utilise l'ID Discord de l'auteur comme clé permanente.
- Sur un message Raid-Helper : clic droit → **Apps → RH List**.
- Le bot récupère les inscriptions de cet event Raid-Helper.
- Il affiche les mains reconnus séparés par statut :
  - inscrit ;
  - bench ;
  - retard ;
  - tentative ;
  - absent.
- Un inscrit non reconnu reçoit automatiquement un DM.
- Le rapport signale si le DM est impossible.
- Le bouton **Export Kromaddon** produit l'export destiné à Kromaddon.
- `/main-audit` contrôle le contenu du salon `#Main`.

## Format d'export Kromaddon

Exemple :

```text
RH|Kromatisme:S|Jeanmage:B|Roger:L|Marcel:T|Paul:A
```

Codes :

- `S` = inscrit
- `B` = bench
- `L` = retard
- `T` = tentative
- `A` = absent
- `U` = statut inconnu

## Sécurisation du salon #Main

Le bot :

- accepte uniquement un nom simple composé de 2 à 12 lettres ;
- supprime les messages qui contiennent du texte autour du nom ;
- interdit que deux IDs Discord déclarent le même main ;
- lorsqu'un membre publie un nouveau main valide, son ancienne déclaration est supprimée.

## Compiler automatiquement l'EXE avec GitHub Actions

Le dépôt contient :

```text
.github/workflows/build-windows.yml
```

Le workflow compile automatiquement :

```text
ApogeeRaidHelperBot.exe
```

sur une machine Windows GitHub Actions avec PyInstaller.

### Méthode la plus simple

1. Crée un dépôt GitHub vide.
2. Envoie tout le contenu de ce dossier à la racine du dépôt.
3. Ouvre l'onglet **Actions** du dépôt.
4. Choisis **Build Windows EXE**.
5. Clique **Run workflow**.
6. Une fois terminé, ouvre le run.
7. Dans **Artifacts**, télécharge :

```text
ApogeeRaidHelperBot-Windows
```

L'archive générée par GitHub contient :

```text
ApogeeRaidHelperBot.exe
.env.example
README.md
start_exe.bat
```

Le workflow se relance aussi automatiquement lorsqu'une modification du bot est poussée sur `main` ou `master`.

## Configuration Discord

### Créer le bot

Dans le Discord Developer Portal :

1. crée une Application ;
2. ouvre **Bot** ;
3. crée/copier le token ;
4. active **Message Content Intent** ;
5. active **Server Members Intent**.

Ne publie jamais ton token dans GitHub.

### Inviter le bot

Scopes OAuth2 :

```text
bot
applications.commands
```

Permissions recommandées :

```text
View Channels
Send Messages
Read Message History
Manage Messages
Use Application Commands
```

Le bot doit pouvoir lire et gérer le salon `#Main`.

## Préparer le fichier .env

Après avoir téléchargé l'EXE, renomme :

```text
.env.example
```

en :

```text
.env
```

et complète :

```env
DISCORD_TOKEN=TON_TOKEN
DISCORD_GUILD_ID=ID_DU_SERVEUR
MAIN_CHANNEL_ID=ID_DU_SALON_MAIN
ADMIN_ROLE_ID=ID_DU_ROLE_ADMIN
```

`ADMIN_ROLE_ID` est optionnel.

Pour récupérer les IDs, active le mode développeur Discord puis clic droit → **Copier l'identifiant**.

## Démarrer la version EXE

Place dans le même dossier :

```text
ApogeeRaidHelperBot.exe
.env
start_exe.bat
```

Puis double-clique sur :

```text
start_exe.bat
```

Aucune installation Python n'est nécessaire pour utiliser l'EXE compilé.

## Utiliser RH List

Sur le message de l'event Raid-Helper :

```text
clic droit
→ Apps
→ RH List
```

Le bot utilise directement l'ID du message ciblé comme ID de l'event.

## DM envoyé aux inscrits non reconnus

```text
Message automatique Apogee :
Tu es inscrit pour un évent Apogee mais tu n'as pas ou mal saisi le nom de ton main en guilde dans le #Main.
```

Si le membre bloque les messages privés, le rapport indique `DM impossible`.

## Audit du salon

Commande :

```text
/main-audit
```

Elle affiche le nombre de mains valides et les éventuelles anomalies.

## Développement local facultatif

Python n'est nécessaire que si tu veux lancer le code source directement :

```bat
pip install -r requirements.txt
python bot.py
```
