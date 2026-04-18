"""
Prototype 2 — tracker-less swarm discovery via Kademlia DHT.

Tells the mdp2p story end-to-end *without a tracker*:

  Step 0. All three peers discover each other through a common bootstrap
          link (in real life, a public DHT bootstrap node; here, Alice
          plays that role since nothing else exists yet).
  Step 1. Alice advertises herself in the DHT as provider of
          md://blog.alice. The provider record propagates to peers
          whose PeerID is close to the content key.
  Step 2. Bob queries the DHT for providers of md://blog.alice, finds
          Alice, then advertises himself too — the swarm grows to 2.
  Step 3. Charlie queries the DHT and must find BOTH Alice and Bob.

If this works, we have the functional equivalent of tracker.py built on
libp2p's DHT — without any central resolver.

Runs entirely in-process. Exits 0 on success, 1 on failure.
"""

import secrets
import sys

import multiaddr
import trio
from libp2p import new_host
from libp2p.abc import IHost
from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.kad_dht.kad_dht import DHTMode, KadDHT
from libp2p.peer.peerinfo import PeerInfo, info_from_p2p_addr
from libp2p.tools.async_service import background_trio_service
from libp2p.utils.address_validation import find_free_port, get_available_interfaces

CONTENT_KEY = "/mdp2p/blog.alice"
LOOKUP_TIMEOUT = 15.0  # seconds


def make_host() -> IHost:
    return new_host(key_pair=create_new_key_pair(secrets.token_bytes(32)))


def local_multiaddr(host: IHost, port: int) -> str:
    return f"/ip4/127.0.0.1/tcp/{port}/p2p/{host.get_id().to_string()}"


async def bootstrap(host: IHost, dht: KadDHT, peer_maddr: str) -> None:
    """Connect to a known peer and seed it into the DHT routing table."""
    info = info_from_p2p_addr(multiaddr.Multiaddr(peer_maddr))
    host.get_peerstore().add_addrs(info.peer_id, info.addrs, 3600)
    await host.connect(info)
    await dht.routing_table.add_peer(info.peer_id)


async def find_providers_with_timeout(
    dht: KadDHT, key: str, label: str
) -> list[PeerInfo]:
    with trio.move_on_after(LOOKUP_TIMEOUT) as scope:
        providers = await dht.find_providers(key)
        print(f"[{label}] DHT lookup for {key}: {len(providers)} provider(s)")
        for p in providers:
            print(f"[{label}]   → {p.peer_id.to_string()}")
        return providers
    if scope.cancelled_caught:
        print(f"[{label}] DHT lookup timed out after {LOOKUP_TIMEOUT}s")
        return []
    return []


async def main() -> int:
    alice_port = find_free_port()
    bob_port = find_free_port()
    charlie_port = find_free_port()

    alice_host = make_host()
    bob_host = make_host()
    charlie_host = make_host()

    async with (
        alice_host.run(listen_addrs=get_available_interfaces(alice_port)),
        bob_host.run(listen_addrs=get_available_interfaces(bob_port)),
        charlie_host.run(listen_addrs=get_available_interfaces(charlie_port)),
        trio.open_nursery() as nursery,
    ):
        for h in (alice_host, bob_host, charlie_host):
            nursery.start_soon(h.get_peerstore().start_cleanup_task, 60)

        alice_dht = KadDHT(alice_host, DHTMode.SERVER)
        bob_dht = KadDHT(bob_host, DHTMode.SERVER)
        charlie_dht = KadDHT(charlie_host, DHTMode.SERVER)

        alice_maddr = local_multiaddr(alice_host, alice_port)
        bob_maddr = local_multiaddr(bob_host, bob_port)

        print(f"[alice]   {alice_maddr}")
        print(f"[bob]     {bob_maddr}")
        print(f"[charlie] {local_multiaddr(charlie_host, charlie_port)}")

        async with (
            background_trio_service(alice_dht),
            background_trio_service(bob_dht),
            background_trio_service(charlie_dht),
        ):
            # ─── Step 0: all peers discover each other (bootstrap) ─
            # In production, this happens via public DHT bootstrap nodes.
            # Here we full-mesh the three peers: each dials the others so
            # their peerstores AND routing tables are populated.
            print("\n--- Step 0: peers bootstrap and mesh their routing tables ---")
            pairs = [
                (alice_host, alice_dht, bob_maddr),
                (alice_host, alice_dht, local_multiaddr(charlie_host, charlie_port)),
                (bob_host, bob_dht, alice_maddr),
                (bob_host, bob_dht, local_multiaddr(charlie_host, charlie_port)),
                (charlie_host, charlie_dht, alice_maddr),
                (charlie_host, charlie_dht, bob_maddr),
            ]
            for host, dht, maddr in pairs:
                await bootstrap(host, dht, maddr)
            await trio.sleep(1.0)

            # ─── Step 1: Alice publishes ───────────────────────────
            print("\n--- Step 1: Alice announces as provider ---")
            success = await alice_dht.provide(CONTENT_KEY)
            assert success, "alice failed to advertise"
            print(f"[alice] provide({CONTENT_KEY}) OK")

            # ─── Step 2: Bob resolves, then becomes seeder ─────────
            print("\n--- Step 2: Bob queries DHT and joins swarm ---")
            providers = await find_providers_with_timeout(bob_dht, CONTENT_KEY, "bob")
            provider_ids = {p.peer_id for p in providers}
            assert alice_host.get_id() in provider_ids, (
                f"bob did not find alice; got: {provider_ids}"
            )
            print("[bob] found Alice as provider ✓")

            success = await bob_dht.provide(CONTENT_KEY)
            assert success, "bob failed to advertise"
            print("[bob] now advertising as provider too (swarm grew to 2)")
            await trio.sleep(2.0)  # let Bob's provider record propagate

            # ─── Step 3: Charlie must find both ────────────────────
            print("\n--- Step 3: Charlie queries DHT, expects both ---")
            providers = await find_providers_with_timeout(
                charlie_dht, CONTENT_KEY, "charlie"
            )
            provider_ids = {p.peer_id for p in providers}
            assert alice_host.get_id() in provider_ids, "charlie missing alice"
            assert bob_host.get_id() in provider_ids, "charlie missing bob"
            print(f"[charlie] swarm size = {len(provider_ids)} ✓")

            nursery.cancel_scope.cancel()
            return 0


if __name__ == "__main__":
    try:
        sys.exit(trio.run(main))
    except AssertionError as e:
        print(f"\nFAILED: {e}", file=sys.stderr)
        sys.exit(1)
