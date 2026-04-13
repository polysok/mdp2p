# MDP2P — Protocole md:// (Markdown Peer-to-Peer)

Un web decentralise base sur Markdown. Chaque visiteur devient seeder.

## Concept

```
Client tape md://blog.alice
       |
       v
   +-----------+
   |  Tracker   |  "blog.alice" -> cle publique + liste de peers
   +-----------+
       |
       v
   Telecharge le bundle signe depuis un peer
   Verifie signature ed25519 + integrite SHA-256
       |
       v
   Rendu local du Markdown
   S'annonce comme nouveau seeder -> le swarm grandit
```

## Architecture

| Module              | Role                                                    |
|---------------------|---------------------------------------------------------|
| `protocol.py`       | Messages JSON prefixes sur TCP, validation URI, rate limiting |
| `bundle.py`         | Creation, signature et verification de bundles Markdown |
| `tracker.py`        | Serveur de resolution URI -> peers, federation, Redis   |
| `peer.py`           | Noeud P2P : telecharge, verifie, seed, met a jour      |
| `publish.py`        | CLI pour publier un site et rester seeder               |
| `demo.py`           | Demonstration end-to-end avec federation                |
| `mdp2p_client/`     | Client interactif terminal (i18n : fr/en/zh/ar/hi)     |

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .

# Pour le developpement et les tests
pip install -e ".[dev]"
```

## Utilisation

### Lancer le tracker

```bash
python tracker.py --port 1707
python tracker.py --port 1707 --peers 192.168.1.10:1707  # avec federation
python tracker.py --port 1707 --no-redis                  # sans persistence Redis
```

### Publier un site

```bash
python publish.py --uri blog --author alice --site ./mon_site --tracker localhost:1707
```

### Client interactif

```bash
mdp2p                    # mode interactif
mdp2p setup --author alice --tracker localhost:1707
mdp2p publish --uri blog --site ./mon_site
mdp2p list
mdp2p status
```

### Lancer la demo

```bash
python demo.py
```

La demo simule :
1. Deux trackers federes demarrent et se synchronisent
2. Alice publie un site sur `md://demo` via le tracker A
3. La registration se propage au tracker B (federation)
4. Bob telecharge, verifie la signature et le contenu, devient seeder (swarm = 2)
5. Charlie fait de meme (swarm = 3)
6. Verification que les 3 copies sont identiques (meme signature)

## Securite

- **Identite** : chaque site a une paire de cles ed25519
- **Authenticite** : le manifeste est signe — impossible d'injecter du contenu
- **Integrite** : chaque fichier est hashe en SHA-256, les fichiers non declares sont detectes
- **Resilience** : le site survit tant qu'un seul peer est en ligne
- **Anti-traversal** : validation stricte des URI et des chemins de fichiers
- **Anti-spoofing** : le tracker utilise l'IP reelle de connexion, pas celle declaree
- **Anti-abus** : rate limiting par IP, timeouts sur toutes les connexions
- **Expiration** : les peers inactifs sont supprimes apres 10 minutes (TTL)
- **Freshness** : les manifestes ont une date d'expiration (TTL 30 jours)

## Tests

```bash
pytest tests/ -v
```

72 tests couvrent les modules critiques : crypto, validation, protocole, tracker (y compris federation, TTL, rate limiting).

## Dependances

- Python 3.10+
- `cryptography>=42.0.0` (ed25519 + serialization)
- `redis>=5.0.0` (optionnel, pour la persistence du tracker)

## Prochaines etapes

- [ ] DHT pour resolution sans tracker central
- [ ] NAT traversal (hole punching)
- [ ] Navigateur Tauri avec rendu Markdown natif
- [ ] Support des fichiers non-Markdown (images, assets)
- [ ] Protocole de diff pour mises a jour incrementales
