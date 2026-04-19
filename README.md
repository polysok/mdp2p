# MDP2P — md:// Protocol (Markdown Peer-to-Peer) over libp2p

A decentralized web based on Markdown. Every visitor becomes a seeder.

## Concept

```
Client types md://blog.alice
       │
       ▼
  ┌───────────────┐
  │ Naming server │   "blog.alice" → {author_pubkey, manifest_ref}
  └───────────────┘        (record signed by the author, non-trusted)
       │
       ▼
       Kademlia DHT       ADD_PROVIDER / GET_PROVIDERS
       /mdp2p/<sha256>    (peer zero = bootstrap + relay + naming)
       │
       ▼
   Stream /mdp2p/bundle/1.0.0 (direct, or via Circuit Relay v2 behind NAT)
   DCUtR attempts a direct connection whenever possible
       │
       ▼
   ed25519 signature + manifest hash + SHA-256 file integrity checks
   TOFU pinning of the public key
       │
       ▼
   Local Markdown rendering
   The client announces itself as a new seeder → the swarm grows
```

## Architecture

| Module              | Role                                                              |
|---------------------|-------------------------------------------------------------------|
| `bundle.py`         | Creation, signing and verification of Markdown bundles + URIs     |
| `wire.py`           | Length-prefixed JSON framing over libp2p streams                  |
| `naming.py`         | Resolution `uri → (author_pubkey, manifest_ref)` over libp2p      |
| `peer.py`           | libp2p peer: publishes, downloads, seeds, DHT, Circuit Relay v2   |
| `peer_zero.py`      | Combined naming + relay HOP + DHT bootstrap for a public VPS      |
| `publish.py`        | CLI to publish a site and keep seeding                            |
| `pinstore.py`       | TOFU key pinning (similar to SSH `known_hosts`)                   |
| `demo.py`           | End-to-end in-process demonstration                               |
| `mdp2p_client/`     | Interactive terminal client (i18n: fr/en/zh/ar/hi)                |

## Installation

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

On macOS ARM the transitive `fastecdsa` dependency must be built against
GMP: `brew install gmp` then
`CFLAGS="-I$(brew --prefix gmp)/include" LDFLAGS="-L$(brew --prefix gmp)/lib" pip install ...`.

On Linux, `apt install libgmp-dev build-essential` (already in the
Dockerfile).

## Usage

### Deploy the peer-zero (public VPS)

```bash
docker compose up -d
```

The `peer-zero` service exposes on port **443** (internal container port 1707 republished on 443 externally, to traverse ISP DPI and port filters):
  - the naming protocol `/mdp2p/naming/1.0.0`
  - Circuit Relay v2 (HOP) for NAT'd peers
  - a DHT entry point for newcomers

The PeerID is persisted via a `peer_zero_data` volume.

### Publish a site

```bash
python publish.py \
    --uri blog --author alice \
    --site ./my_site \
    --naming /dns4/relay.mdp2p.net/tcp/443/p2p/<PEER_ZERO_ID>
```

### Interactive client

```bash
mdp2p setup --author alice \
    --naming /dns4/relay.mdp2p.net/tcp/443/p2p/<PEER_ZERO_ID>
mdp2p                    # interactive mode
mdp2p publish --uri blog --site ./my_site
mdp2p browse
mdp2p status
```

### Run the local demo

```bash
python demo.py
```

1. Starts a naming server + 3 trio/libp2p peers
2. Alice publishes a site on `md://demo`
3. Bob discovers Alice via the DHT, downloads, becomes a seeder
4. Charlie discovers both Alice AND Bob, becomes a seeder in turn
5. Verifies that all 3 copies have identical signatures

## Security

- **Editorial identity**: each site has an ed25519 keypair (the author), distinct from the libp2p identity (PeerID) which only serves the transport layer.
- **Authenticity**: the manifest is signed — no content can be injected.
- **Integrity**: every file is hashed with SHA-256; unauthorized files are detected.
- **Non-trusted naming**: records are signed by the author, so the naming server cannot forge them; it is merely a highly-available cache.
- **URI hijack prevention**: a second register attempt on a URI with a different pubkey is refused.
- **Monotonic versioning**: timestamps must be strictly increasing for each update.
- **Anti-MITM**: TOFU key pinning (`~/.mdp2p/known_keys.json`).
- **Anti-traversal**: strict validation of URIs and file paths.
- **Resilience**: the site survives as long as at least one peer stays online.
- **Relay limits**: `RelayLimits` caps free bandwidth (100 MiB per reservation, 10 concurrent circuits).

## Tests

```bash
pytest tests/ -v
```

84 tests cover: bundle (crypto + URI), pinstore (TOFU), naming
(register/resolve/persistence/anti-hijack), peer (publish/fetch/TOFU/
update), DHT (swarm discovery), relay (smoke).

## Dependencies

- Python 3.10+
- `cryptography>=42.0.0` (ed25519 + serialization)
- `libp2p>=0.6.0` (Kademlia DHT + Circuit Relay v2 + DCUtR)
- `trio>=0.27.0` (async framework used by py-libp2p)
- `multiaddr>=0.0.9`

## Next steps

- [ ] NAT-traversal testing across two distinct residential routers (Circuit Relay v2 + DCUtR validated on localhost via the prototype, but needs confirmation under real-world conditions).
- [ ] Tauri browser with native Markdown rendering.
- [ ] Bundle chunking/streaming for sites larger than a few MB.
- [ ] Diff protocol for incremental updates.
- [ ] IPNS-like signed resolver in the DHT (naming option A), to remove reliance on the peer-zero entirely.

## Prototype validation

The `prototypes/libp2p/` folder contains three throwaway demos that served
as feasibility proofs before the migration:

- `hello.py`: transport of a signed manifest over a custom libp2p stream.
- `dht_demo.py`: tracker-less discovery via provider records.
- `nat_demo.py`: circuit relay + DCUtR.

They run from their own `venv` in `prototypes/libp2p/.venv`.
