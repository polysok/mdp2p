# MDP2P in Rust

| Crate | Role |
| --- | --- |
| `mdp2p-core` | Pure verification core (no I/O): canonical JSON, signatures, client acceptance rules, pinning, reviewer selection. Builds for `wasm32-unknown-unknown`. Pinned to the Python reference by `spec/vectors/`. |
| `mdp2p-node` | rust-libp2p peer (TCP + Noise + Yamux, Kademlia `/ipfs/kad/1.0.0`, Identify, Ping) that speaks `/mdp2p/naming/1.0.0` and `/mdp2p/bundle/1.0.0`. It fetches, verifies and seeds sites, and interoperates with the Python peers. |

```bash
cargo test                      # unit tests + conformance vectors
cargo run -p mdp2p-node -- fetch --naming /ip4/<host>/tcp/1707/p2p/<peer0> blog.alice
cargo run -p mdp2p-node -- serve --naming /ip4/<host>/tcp/1707/p2p/<peer0> \
    --listen /ip4/0.0.0.0/tcp/4001 --external /ip4/<public ip>/tcp/4001
```

`fetch` stores sites in `--data-dir` (default `./peer_data`) using the same
layout as the Python peer. Pins go to `~/.mdp2p/known_keys.json`, a file the
Python client also reads. `--key` takes the 32-byte seed format of the Python
peer, so a node can keep its PeerID. Interop with Python is covered by
`tests/test_rust_interop.py` at the repository root.

Not implemented yet: Circuit Relay v2 / DCUtR (NAT traversal),
publishing (signing), WebRTC/WebTransport for browsers, review solicitation.
