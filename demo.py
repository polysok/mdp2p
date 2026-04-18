"""
MDP2P Demo — Full demonstration of the md:// protocol over libp2p.

This script simulates the complete scenario:
  1. Creates a demo Markdown site
  2. Starts a naming server and three libp2p peers
  3. The author (Alice) publishes md://demo → becomes the 1st seeder
  4. A visitor (Bob) resolves via the naming server, finds Alice through the
     DHT, downloads, verifies, and becomes the 2nd seeder
  5. Charlie does the same and finds BOTH Alice and Bob through the DHT
  6. Renders the site in the terminal
  7. Verifies that all three peers hold identical signatures
"""

import logging
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

import multiaddr
import trio
from libp2p import new_host
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.peer.peerinfo import info_from_p2p_addr
from libp2p.utils.address_validation import find_free_port

from bundle import generate_keypair, load_bundle
from naming import NameStore, NamingServer, load_or_create_peer_seed
from peer import link_peers_dht, run_peer

DEMO_DIR = Path("./demo_run")
SITE_DIR = DEMO_DIR / "author_site"
KEYS_DIR = DEMO_DIR / "keys"


def create_demo_site():
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
""",
        encoding="utf-8",
    )

    (SITE_DIR / "posts" / "hello.md").write_text(
        """# Hello, Markdown Web!

*Publié par Alice*

Ceci est le premier article publié sur le réseau md://.
""",
        encoding="utf-8",
    )

    (SITE_DIR / "posts" / "decentralisation.md").write_text(
        """# Pourquoi la décentralisation ?

*Publié par Alice*

Avec md://, chaque visiteur possède une copie du site. Le contenu
survit tant qu'au moins un peer est en ligne.
""",
        encoding="utf-8",
    )

    count = len(list(SITE_DIR.rglob("*.md")))
    print(f"[DEMO] Site created in {SITE_DIR} ({count} files)")


@asynccontextmanager
async def run_naming(data_dir: Path):
    """Run a naming server in-process; yields its PeerInfo."""
    port = find_free_port()
    key_path = data_dir / "naming.key"
    seed = load_or_create_peer_seed(str(key_path))
    host = new_host(key_pair=create_new_key_pair(seed))

    async with (
        host.run(
            listen_addrs=[multiaddr.Multiaddr(f"/ip4/127.0.0.1/tcp/{port}")]
        ),
        trio.open_nursery() as nursery,
    ):
        nursery.start_soon(host.get_peerstore().start_cleanup_task, 60)

        store = NameStore(str(data_dir / "naming_records.json"))
        server = NamingServer(host, store)
        server.attach()

        maddr_str = (
            f"/ip4/127.0.0.1/tcp/{port}/p2p/{host.get_id().to_string()}"
        )
        info = info_from_p2p_addr(multiaddr.Multiaddr(maddr_str))
        print(f"[DEMO] Naming server listening at {maddr_str}")

        try:
            yield info
        finally:
            nursery.cancel_scope.cancel()


async def run_demo():
    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║           MDP2P — Protocol demonstration                 ║")
    print("║              md:// over libp2p + DHT                    ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    print("=" * 60)
    print("STEP 1: Creating the demo site")
    print("=" * 60)
    create_demo_site()

    priv_path, _pub_path = generate_keypair(str(KEYS_DIR), "demo")
    print(f"[DEMO] Author key generated: {priv_path}")
    print()

    print("=" * 60)
    print("STEP 2: Starting naming server + 3 peers")
    print("=" * 60)

    async with run_naming(DEMO_DIR) as naming_info:

        peer_ctxs = [
            run_peer(
                data_dir=str(DEMO_DIR / f"peer_{name}"),
                port=0,
                listen_host="127.0.0.1",
                naming_info=naming_info,
                pinstore_path=str(DEMO_DIR / f"pin_{name}.json"),
            )
            for name in ("alice", "bob", "charlie")
        ]
        alice = await peer_ctxs[0].__aenter__()
        bob = await peer_ctxs[1].__aenter__()
        charlie = await peer_ctxs[2].__aenter__()

        print(f"[DEMO] alice   PeerID: {alice.host.get_id()}")
        print(f"[DEMO] bob     PeerID: {bob.host.get_id()}")
        print(f"[DEMO] charlie PeerID: {charlie.host.get_id()}")

        # In a real deployment these peers would bootstrap off relay.mdp2p.net;
        # here we mesh them manually so the DHT routing tables are populated.
        await link_peers_dht(alice, bob, charlie)
        await trio.sleep(0.5)
        print()

        print("=" * 60)
        print("STEP 3: Alice publishes md://demo")
        print("=" * 60)
        await alice.publish("demo", "alice", str(SITE_DIR), priv_path)
        await trio.sleep(1.0)
        print()

        print("=" * 60)
        print("STEP 4: Bob fetches md://demo (via DHT)")
        print("=" * 60)
        ok = await bob.fetch_site("demo")
        print(f"[DEMO] Bob fetch: {'SUCCESS' if ok else 'FAILED'}")
        await trio.sleep(2.0)  # let Bob's provider record propagate
        print()

        print("=" * 60)
        print("STEP 5: Charlie fetches md://demo (should find Alice AND Bob)")
        print("=" * 60)
        ok = await charlie.fetch_site("demo")
        print(f"[DEMO] Charlie fetch: {'SUCCESS' if ok else 'FAILED'}")
        print()

        print("=" * 60)
        print("STEP 6: DHT swarm state")
        print("=" * 60)
        # Both bob and charlie should see multiple providers.
        providers = await charlie.find_providers(
            "demo", load_bundle(charlie.sites["demo"])[0]["public_key"]
        )
        print(f"[DEMO] Charlie sees {len(providers)} peer(s) advertising md://demo:")
        for addr in providers:
            print(f"       → {addr}")
        print()

        print("=" * 60)
        print("STEP 7: Site rendering (from Bob's cache)")
        print("=" * 60)
        print(bob.render_site("demo"))

        print("=" * 60)
        print("STEP 8: Cross-verification")
        print("=" * 60)
        _, sig_a = load_bundle(alice.sites["demo"])
        _, sig_b = load_bundle(bob.sites["demo"])
        _, sig_c = load_bundle(charlie.sites["demo"])
        all_match = sig_a == sig_b == sig_c
        print(f"[DEMO] Identical signatures across all 3 peers: "
              f"{'YES' if all_match else 'NO'}")
        print()

        for ctx in reversed(peer_ctxs):
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:
                pass

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
        trio.run(run_demo)
    except KeyboardInterrupt:
        print("\n[DEMO] Stopped.")
