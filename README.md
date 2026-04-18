# MDP2P — Protocole md:// (Markdown Peer-to-Peer) over libp2p

Un web decentralise basé sur Markdown. Chaque visiteur devient seeder.

## Concept

```
Client tape md://blog.alice
       │
       ▼
  ┌───────────────┐
  │ Naming server │   "blog.alice" → {author_pubkey, manifest_ref}
  └───────────────┘        (record signé par l'auteur, non-trusté)
       │
       ▼
       DHT Kademlia       ADD_PROVIDER / GET_PROVIDERS
       /mdp2p/<sha256>    (peer zero = bootstrap + relais + naming)
       │
       ▼
   Stream /mdp2p/bundle/1.0.0 (direct, ou via Circuit Relay v2 si NAT)
   DCUtR tente une connexion directe dès que possible
       │
       ▼
   Vérification signature ed25519 + hash manifest + SHA-256 fichiers
   Pin TOFU de la clé publique
       │
       ▼
   Rendu local du Markdown
   Le client s'annonce comme nouveau seeder → le swarm grandit
```

## Architecture

| Module              | Rôle                                                          |
|---------------------|---------------------------------------------------------------|
| `bundle.py`         | Création, signature et vérification des bundles Markdown + URI|
| `wire.py`           | Framing JSON à longueur préfixée sur streams libp2p           |
| `naming.py`         | Résolution `uri → (author_pubkey, manifest_ref)` sur libp2p   |
| `peer.py`           | Peer libp2p : publie, télécharge, seed, DHT, Circuit Relay v2 |
| `peer_zero.py`      | Combo naming + relay HOP + bootstrap DHT pour un VPS public   |
| `publish.py`        | CLI pour publier un site et rester seeder                     |
| `pinstore.py`       | TOFU key pinning (similaire à `known_hosts` SSH)              |
| `demo.py`           | Démonstration end-to-end in-process                           |
| `mdp2p_client/`     | Client interactif terminal (i18n : fr/en/zh/ar/hi)            |

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Sur macOS ARM la dépendance transitive `fastecdsa` doit compiler avec
GMP : `brew install gmp` puis
`CFLAGS="-I$(brew --prefix gmp)/include" LDFLAGS="-L$(brew --prefix gmp)/lib" pip install ...`.

Sur Linux `apt install libgmp-dev build-essential` (déjà dans le
Dockerfile).

## Utilisation

### Déployer le peer-zero (VPS public)

```bash
docker compose up -d
```

Le service `peer-zero` expose sur le port 1707 :
  - le protocole naming `/mdp2p/naming/1.0.0`
  - Circuit Relay v2 (HOP) pour les peers NAT'd
  - un point d'entrée DHT pour les nouveaux venus

Le PeerID est persistant via un volume `peer_zero_data`.

### Publier un site

```bash
python publish.py \
    --uri blog --author alice \
    --site ./mon_site \
    --naming /dns4/relay.mdp2p.net/tcp/1707/p2p/<PEER_ZERO_ID>
```

### Client interactif

```bash
mdp2p setup --author alice \
    --naming /dns4/relay.mdp2p.net/tcp/1707/p2p/<PEER_ZERO_ID>
mdp2p                    # mode interactif
mdp2p publish --uri blog --site ./mon_site
mdp2p browse
mdp2p status
```

### Lancer la démo locale

```bash
python demo.py
```

1. Lance un naming server + 3 peers trio/libp2p
2. Alice publie un site sur `md://demo`
3. Bob découvre Alice via la DHT, télécharge, devient seeder
4. Charlie découvre Alice ET Bob, devient seeder à son tour
5. Vérification que les 3 copies ont des signatures identiques

## Sécurité

- **Identité éditoriale** : chaque site a une paire de clés ed25519 (l'auteur), séparée de l'identité libp2p (PeerID) qui ne sert qu'au transport.
- **Authenticité** : le manifeste est signé — impossible d'injecter du contenu.
- **Intégrité** : chaque fichier est hashé en SHA-256, les fichiers non déclarés sont détectés.
- **Non-trust du naming** : les records sont signés par l'auteur, donc le serveur de noms ne peut pas forger de record ; il n'est qu'un cache hautement disponible.
- **Anti-hijack URI** : un second register sur une URI avec une autre pubkey est refusé.
- **Versioning monotone** : timestamps strictement croissants pour chaque update.
- **Anti-MITM** : TOFU key pinning (`~/.mdp2p/known_keys.json`).
- **Anti-traversal** : validation stricte des URI et des chemins de fichiers.
- **Résilience** : le site survit tant qu'un seul peer est en ligne.
- **Limites de relais** : `RelayLimits` plafonne la bande passante gratuite (100 MiB/réservation, 10 circuits concurrents).

## Tests

```bash
pytest tests/ -v
```

84 tests couvrent : bundle (crypto + URI), pinstore (TOFU), naming
(register/resolve/persistance/anti-hijack), peer (publish/fetch/TOFU/
update), DHT (swarm découverte), relay (smoke).

## Dépendances

- Python 3.10+
- `cryptography>=42.0.0` (ed25519 + serialization)
- `libp2p>=0.6.0` (Kademlia DHT + Circuit Relay v2 + DCUtR)
- `trio>=0.27.0` (framework async utilisé par py-libp2p)
- `multiaddr>=0.0.9`

## Prochaines étapes

- [ ] Test NAT traversal sur deux box résidentielles distinctes (Circuit Relay v2 + DCUtR valide en localhost via le prototype mais doit être confirmé en conditions réelles).
- [ ] Navigateur Tauri avec rendu Markdown natif.
- [ ] Support des fichiers non-Markdown (images, assets).
- [ ] Chunking/streaming des bundles pour les sites > quelques Mo.
- [ ] Protocole de diff pour mises à jour incrémentales.
- [ ] Résolveur IPNS-like signé dans la DHT (option A du naming), pour se passer entièrement du peer-zero.

## Validation prototypes

Le dossier `prototypes/libp2p/` contient trois démos jetables qui ont
servi de preuve de faisabilité avant la migration :

- `hello.py` : transport d'un manifeste signé via un stream libp2p custom.
- `dht_demo.py` : découverte sans tracker via provider records.
- `nat_demo.py` : circuit relay + DCUtR.

Ils tournent via leur propre `venv` dans `prototypes/libp2p/.venv`.
