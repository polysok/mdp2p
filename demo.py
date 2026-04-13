"""
MDP2P Demo — Full demonstration of the md:// protocol.

This script simulates the complete scenario:
  1. Creates a demo Markdown site
  2. Starts two federated trackers
  3. The author (Peer A) publishes the site → becomes 1st seeder
  4. A visitor (Peer B) resolves md://demo, downloads, verifies
  5. Peer B automatically becomes a seeder (swarm = 2 peers)
  6. A 2nd visitor (Peer C) downloads — now 3 seeders
  7. Renders the site in the terminal
  8. Demonstrates federation by checking registration propagated to secondary tracker
"""

import asyncio
import logging
import shutil
from pathlib import Path

from bundle import generate_keypair
from protocol import send_msg, recv_msg
from tracker import Tracker
from peer import Peer

DEMO_DIR = Path("./demo_data")
SITE_DIR = DEMO_DIR / "author_site"
KEYS_DIR = DEMO_DIR / "keys"
TRACKER_A_PORT = 1707
TRACKER_B_PORT = 4002
PEER_A_PORT = 5001
PEER_B_PORT = 5002
PEER_C_PORT = 5003


def create_demo_site():
    """Create a small demo Markdown site."""
    if DEMO_DIR.exists():
        shutil.rmtree(DEMO_DIR)

    SITE_DIR.mkdir(parents=True)
    (SITE_DIR / "posts").mkdir()

    (SITE_DIR / "index.md").write_text(
        """# Bienvenue sur mon site P2P

Ce site est servi via le protocole **md://** — un web décentralisé
basé sur Markdown.

## Comment ça marche ?

Chaque visiteur reçoit une copie complète du site et devient
automatiquement un *seeder*. Plus un site est populaire, plus
il est résilient.

## Pages

- [À propos](about.md)
- [Premier article](posts/hello.md)
- [Deuxième article](posts/decentralisation.md)
""",
        encoding="utf-8",
    )

    (SITE_DIR / "about.md").write_text(
        """# À propos

Ce site est un prototype du protocole **MDP2P**.

- **Auteur** : Alice
- **Clé publique** : (vérifiable via le manifeste signé)
- **Licence** : Domaine public

Le contenu de ce site pèse quelques kilo-octets et peut être
répliqué instantanément par n'importe quel peer du réseau.
""",
        encoding="utf-8",
    )

    (SITE_DIR / "posts" / "hello.md").write_text(
        """# Hello, Markdown Web!

*Publié par Alice*

Ceci est le premier article publié sur le réseau md://.

Le web n'a pas besoin d'être complexe. Un fichier Markdown,
une signature cryptographique, et un réseau de pairs suffisent
pour publier et distribuer du contenu.

> Le meilleur format est celui qui survit au serveur qui l'a créé.
""",
        encoding="utf-8",
    )

    (SITE_DIR / "posts" / "decentralisation.md").write_text(
        """# Pourquoi la décentralisation ?

*Publié par Alice*

## Le problème

Un site web classique dépend d'un seul serveur. Si ce serveur
tombe, le contenu disparaît.

## La solution md://

Avec md://, chaque visiteur possède une copie du site. Le contenu
survit tant qu'au moins un peer est en ligne.

## Les avantages du Markdown

- **Léger** : un site entier tient en quelques Ko
- **Lisible** : pas besoin de navigateur pour lire le source
- **Universel** : aucune dépendance, aucun framework
- **Pérenne** : du texte brut ne devient jamais obsolète
""",
        encoding="utf-8",
    )

    count = len(list(SITE_DIR.rglob("*.md")))
    print(f"[DEMO] Site created in {SITE_DIR} ({count} files)")


async def check_tracker_registration(tracker_port: int, name: str) -> list:
    """Query a tracker for registered sites."""
    reader, writer = await asyncio.open_connection("127.0.0.1", tracker_port)
    try:
        await send_msg(writer, {"type": "list"})
        response = await recv_msg(reader)
        if response and response.get("type") == "site_list":
            sites = response.get("sites", [])
            print(f"[DEMO] {name} has {len(sites)} registration(s):")
            for site in sites:
                print(f"       → {site['uri']} (ts: {site['timestamp']})")
            return sites
    finally:
        writer.close()
        await writer.wait_closed()
    return []


async def run_demo():
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║           MDP2P — Protocol demonstration                 ║")
    print("║              md:// (Markdown peer-to-peer)              ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    print("=" * 60)
    print("STEP 1: Creating the demo site")
    print("=" * 60)
    create_demo_site()

    priv_path, pub_path = generate_keypair(str(KEYS_DIR), "demo")
    print(f"[DEMO] Keys generated: {priv_path}")
    print()

    print("=" * 60)
    print("STEP 2: Starting federated trackers")
    print("=" * 60)
    tracker_a = Tracker(
        port=TRACKER_A_PORT,
        peer_trackers=[("127.0.0.1", TRACKER_B_PORT)],
        name=f"tracker_a:{TRACKER_A_PORT}",
        redis_enabled=False,
    )
    tracker_b = Tracker(
        port=TRACKER_B_PORT,
        peer_trackers=[("127.0.0.1", TRACKER_A_PORT)],
        name=f"tracker_b:{TRACKER_B_PORT}",
        redis_enabled=False,
    )
    await tracker_a.start()
    await tracker_b.start()
    print(f"[DEMO] Tracker A listening on port {TRACKER_A_PORT}")
    print(f"[DEMO] Tracker B listening on port {TRACKER_B_PORT} (federated)")
    print()

    print("=" * 60)
    print("STEP 3: Alice (Peer A) publishes md://demo")
    print("=" * 60)
    peer_a = Peer(
        data_dir=str(DEMO_DIR / "peer_a"),
        host="127.0.0.1",
        port=PEER_A_PORT,
        tracker_host="127.0.0.1",
        tracker_port=TRACKER_A_PORT,
    )
    await peer_a.start_seeding()
    await peer_a.publish("demo", "alice", str(SITE_DIR), priv_path)
    print()

    print("[DEMO] Waiting for federation sync...")
    await asyncio.sleep(2)
    print()

    print("=" * 60)
    print("STEP 4: Federation verification")
    print("=" * 60)
    sites_a = await check_tracker_registration(TRACKER_A_PORT, "Tracker A")
    sites_b = await check_tracker_registration(TRACKER_B_PORT, "Tracker B")
    federation_ok = len(sites_a) > 0 and len(sites_b) > 0
    status = "SYNCED" if federation_ok else "FAILED"
    print(f"[DEMO] Federation status: {status}")
    print()

    print("=" * 60)
    print("STEP 5: Bob (Peer B) visits md://demo")
    print("=" * 60)
    peer_b = Peer(
        data_dir=str(DEMO_DIR / "peer_b"),
        host="127.0.0.1",
        port=PEER_B_PORT,
        tracker_host="127.0.0.1",
        tracker_port=TRACKER_A_PORT,
    )
    await peer_b.start_seeding()
    success = await peer_b.fetch_site("demo")
    print(f"[DEMO] Download {'succeeded' if success else 'FAILED'}")
    print()

    print("=" * 60)
    print("STEP 6: Charlie (Peer C) visits md://demo")
    print("=" * 60)
    peer_c = Peer(
        data_dir=str(DEMO_DIR / "peer_c"),
        host="127.0.0.1",
        port=PEER_C_PORT,
        tracker_host="127.0.0.1",
        tracker_port=TRACKER_A_PORT,
    )
    await peer_c.start_seeding()
    success = await peer_c.fetch_site("demo")
    print(f"[DEMO] Download {'succeeded' if success else 'FAILED'}")
    print()

    print("=" * 60)
    print("STEP 7: Network state")
    print("=" * 60)
    from bundle import load_bundle

    resolution = await peer_c.resolve("demo")
    peers = resolution.get("peers", [])
    print(f"[DEMO] Site md://demo served by {len(peers)} peers:")
    for p in peers:
        print(f"       → {p['host']}:{p['port']}")
    print()

    print("=" * 60)
    print("STEP 8: Site rendering (from Bob's cache)")
    print("=" * 60)
    print(peer_b.render_site("demo"))

    print("=" * 60)
    print("STEP 9: Cross-verification")
    print("=" * 60)
    _, sig_a = load_bundle(peer_a.sites["demo"])
    _, sig_b = load_bundle(peer_b.sites["demo"])
    _, sig_c = load_bundle(peer_c.sites["demo"])
    all_match = sig_a == sig_b == sig_c
    print(f"[DEMO] Identical signatures across all 3 peers: {'YES' if all_match else 'NO'}")
    print()

    await peer_a.close()
    await peer_b.close()
    await peer_c.close()
    await tracker_a.close()
    await tracker_b.close()

    print("╔══════════════════════════════════════════════════════════╗")
    print("║                  Demo complete!                         ║")
    print("╚══════════════════════════════════════════════════════════╝")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        asyncio.run(run_demo())
    except KeyboardInterrupt:
        print("\n[DEMO] Stopped.")