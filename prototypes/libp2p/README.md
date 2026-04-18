# mdp2p — py-libp2p hello-world

Throwaway prototype to validate that py-libp2p is a viable foundation for
mdp2p. Two goals only:

1. Prove that we can open a custom protocol stream between two libp2p hosts.
2. Prove that an ed25519-signed payload (mock manifest) can be carried and
   verified on top, keeping the **author identity** (ed25519) separate from
   the **peer identity** (libp2p PeerID).

If this works reliably, the full migration path described in the main
discussion is viable.

## Setup

```bash
cd prototypes/libp2p
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## One-shot automated demo

Runs a listener and a dialer in the same process, exchanges a signed
manifest, verifies the signature, and tries a tampering attack that must
fail.

```bash
python run_demo.py
```

Expected: ends with `[demo] tampered manifest correctly rejected` and
exits 0.

## Manual two-process demo

Terminal A:

```bash
python hello.py
# Copy the printed multiaddr ending in /p2p/<PeerID>
```

Terminal B:

```bash
python hello.py --dial /ip4/127.0.0.1/tcp/<PORT>/p2p/<PeerID>
```

Terminal B prints the received manifest and `Signature OK`.

## Prototype 2: tracker-less swarm via DHT

```bash
python dht_demo.py
```

Three peers (Alice, Bob, Charlie) discover each other through a Kademlia
DHT — no tracker involved. Alice advertises a content key, Bob queries
the DHT to find her and becomes a seeder himself, Charlie later queries
and finds both.

Validates that `tracker.py` can be replaced by libp2p's built-in DHT
provider records.

Expected tail:

```
[charlie] DHT lookup for /mdp2p/blog.alice: 2 provider(s)
[charlie] swarm size = 2 ✓
```

## Prototype 3: NAT traversal via Circuit Relay v2 + DCUtR

```bash
python nat_demo.py
```

Three peers in-process:

- `relay`    — public HOP relay (simulates a public-IP server)
- `listener` — NAT'd seeder, reserves a slot on the relay via auto-discovery
- `dialer`   — NAT'd visitor, dials the listener through a `/p2p-circuit`
  multiaddr, then attempts DCUtR hole punching

The signed manifest from prototype 1 is then exchanged over the resulting
connection. On localhost DCUtR trivially "succeeds" because there's no
real NAT to punch, but the whole reservation + circuit-dial + hole-punch
pipeline is exercised.

Expected tail:

```
[dialer]   DCUtR upgrade: OK
[dialer]   manifest received & verified: hello from mdp2p over libp2p
[demo] SUCCESS — circuit relay transported signed manifest
```

## What this is NOT (yet)

- **Not real NAT**: the demo is same-host, so DCUtR success is not proof
  it works across real routers. A true validation needs two machines behind
  separate consumer routers. Still, the libp2p plumbing (reservations,
  circuit dial, HOP/STOP, DCUtR handshake) is end-to-end exercised.
- **No real bundle transfer** — payload is a small JSON. Real bundles
  would be chunked or streamed.
- **No naming layer** — the content key is a plain string. In full mdp2p
  it would be derived from the author pubkey (IPNS-like) or resolved by
  a minimal `name → pubkey` tracker.

Those come in follow-up prototypes once these baselines are confirmed.
