"""MDP2P Peer — publishes, fetches, seeds Markdown bundles over libp2p.

Architecture:
  - Identity: libp2p PeerID (transport) distinct from the author's ed25519
    pubkey (content). Author identity travels in the signed manifest; the
    PeerID is stable per machine via a seed file.
  - Discovery: the naming service (see naming.py) resolves uri → (author_pubkey,
    manifest_ref); seeders are located through the DHT's provider records.
    Callers may also supply seeder multiaddrs directly to bypass discovery.
  - Transport: custom protocol /mdp2p/bundle/1.0.0 over libp2p streams.
    Messages are length-prefixed JSON. A bundle transfer is a single message
    whose size is capped by MAX_BUNDLE_MSG_SIZE.

This package exposes a flat API: every public symbol previously defined in
``peer.py`` is re-exported here, so ``from peer import X`` keeps working
for every existing caller.
"""

from .bundle_protocol import BUNDLE_PROTOCOL, MAX_BUNDLE_MSG_SIZE
from .host_factory import detect_local_ip
from .lifecycle import link_peers_dht, run_peer
from .peer import DEFAULT_DATA_DIR, DEFAULT_PINSTORE, Peer

__all__ = [
    "Peer",
    "run_peer",
    "link_peers_dht",
    "detect_local_ip",
    "BUNDLE_PROTOCOL",
    "MAX_BUNDLE_MSG_SIZE",
    "DEFAULT_DATA_DIR",
    "DEFAULT_PINSTORE",
]
