"""Interoperability between the Python reference peer and the Rust node.

Runs a Python peer-zero (naming + DHT) and a Python author, then drives the
``mdp2p-node`` binary as a subprocess:

- the Rust node resolves, discovers (DHT) and downloads a Python-published
  site, verifying it with mdp2p-core;
- the Rust node announces itself in the Python DHT and serves the site to
  a Python reader.

Skipped when the binary is not built (``cargo build -p mdp2p-node`` in
``rust/``) and cargo is unavailable.
"""

import json
import os
import shutil
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import trio

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bundle import generate_keypair, load_bundle
from naming import NameStore, NamingServer
from peer import run_peer
from pinstore import PinStatus, check_pin, load_pinstore

BINARY = ROOT / "rust" / "target" / "debug" / "mdp2p-node"
URI = "blog.alice"
# ~300 KB: well past the 64 KiB Noise frame limit, and several yamux windows.
PAGES = {
    "index.md": "# Hello from Python\n\nCafé, 中文, emoji 🙂.\r\nCRLF line.\r\n",
    "posts/long.md": "# Long\n\n" + "lorem ipsum dolor sit amet\n" * 12_000,
}


@pytest.fixture(scope="module")
def binary() -> Path:
    if os.environ.get("MDP2P_SKIP_RUST_BUILD") != "1" and shutil.which("cargo"):
        subprocess.run(
            ["cargo", "build", "-q", "-p", "mdp2p-node"],
            cwd=ROOT / "rust",
            check=True,
        )
    if not BINARY.exists():
        pytest.skip("mdp2p-node binary not built and cargo unavailable")
    return BINARY


@asynccontextmanager
async def python_network(tmp_path: Path):
    """Peer-zero (naming + DHT on one host) and Alice, who publishes URI."""
    site = tmp_path / "alice_site"
    for name, content in PAGES.items():
        (site / name).parent.mkdir(parents=True, exist_ok=True)
        (site / name).write_bytes(content.encode("utf-8"))
    priv, _ = generate_keypair(str(tmp_path / "keys"), "alice")

    async with run_peer(
        data_dir=str(tmp_path / "peer0"),
        listen_host="127.0.0.1",
        pinstore_path=str(tmp_path / "pin0.json"),
    ) as peer0:
        NamingServer(peer0.host, NameStore(str(tmp_path / "names.json"))).attach()
        naming_maddr = peer0.addrs[0]
        async with run_peer(
            data_dir=str(tmp_path / "alice"),
            listen_host="127.0.0.1",
            naming_info=_info(naming_maddr),
            pinstore_path=str(tmp_path / "pin_alice.json"),
            bootstrap_multiaddrs=[naming_maddr],
        ) as alice:
            await alice.publish(URI, "alice", str(site), priv)
            yield naming_maddr, alice


def _info(maddr: str):
    import multiaddr
    from libp2p.peer.peerinfo import info_from_p2p_addr

    return info_from_p2p_addr(multiaddr.Multiaddr(maddr))


def _rust_args(binary: Path, tmp_path: Path, naming: str, *cmd: str) -> list[str]:
    return [
        str(binary),
        "--naming", naming,
        "--data-dir", str(tmp_path / "rust_data"),
        "--pinstore", str(tmp_path / "rust_pins.json"),
        "--listen", "/ip4/127.0.0.1/tcp/0",
        *cmd,
    ]


def _assert_same_site(dir_a: Path, dir_b: Path) -> None:
    for name in PAGES:
        assert (dir_a / name).read_bytes() == (dir_b / name).read_bytes(), name
    manifest_a, sig_a = load_bundle(str(dir_a))
    manifest_b, sig_b = load_bundle(str(dir_b))
    assert manifest_a == manifest_b
    assert sig_a == sig_b


def test_rust_fetches_python_published_site(tmp_path, binary):
    async def main():
        async with python_network(tmp_path) as (naming, alice):
            with trio.fail_after(60):
                result = await trio.run_process(
                    _rust_args(binary, tmp_path, naming, "fetch", URI),
                    capture_stdout=True,
                    capture_stderr=True,
                    check=False,
                )
            out = result.stdout.decode()
            assert result.returncode == 0, result.stderr.decode()[-3000:]
            fetched = [l for l in out.splitlines() if l.startswith("FETCHED ")]
            assert fetched, out
            _, uri, site_dir, count = fetched[0].split(" ")
            assert uri == URI and int(count) == len(PAGES)
            _assert_same_site(Path(alice.sites[URI]), Path(site_dir))

            # The Rust pin file is readable by the Python client.
            pins = load_pinstore(str(tmp_path / "rust_pins.json"))
            manifest, _ = load_bundle(site_dir)
            assert check_pin(pins, URI, manifest["public_key"]) == PinStatus.MATCH

    trio.run(main)


def test_python_fetches_from_rust_seeder(tmp_path, binary):
    async def main():
        async with python_network(tmp_path) as (naming, alice):
            proc = await trio.lowlevel.open_process(
                _rust_args(binary, tmp_path, naming, "fetch", "--seed", URI),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            try:
                rust_addr = None
                buf = b""
                with trio.fail_after(60):
                    while b"FETCHED " not in buf:
                        chunk = await proc.stdout.receive_some(4096)
                        assert chunk, "mdp2p-node exited early"
                        buf += chunk
                for line in buf.decode().splitlines():
                    if line.startswith("LISTEN ") and rust_addr is None:
                        rust_addr = line.split(" ", 1)[1]
                assert rust_addr
                rust_peer_id = rust_addr.rsplit("/p2p/", 1)[1]

                manifest, _ = load_bundle(alice.sites[URI])
                async with run_peer(
                    data_dir=str(tmp_path / "bob"),
                    listen_host="127.0.0.1",
                    naming_info=_info(naming),
                    pinstore_path=str(tmp_path / "pin_bob.json"),
                    bootstrap_multiaddrs=[naming],
                ) as bob:
                    # The Rust node's ADD_PROVIDER was accepted by the
                    # Python DHT: Bob discovers it next to Alice.
                    providers = []
                    with trio.fail_after(30):
                        while not any(rust_peer_id in a for a in providers):
                            providers = await bob.find_providers(
                                URI, manifest["public_key"]
                            )
                            await trio.sleep(0.5)
                    from_rust = [a for a in providers if rust_peer_id in a]

                    ok = await bob.fetch_site(
                        URI, seeder_addrs=from_rust, announce_after=False
                    )
                    assert ok is True
                    _assert_same_site(Path(alice.sites[URI]), Path(bob.sites[URI]))
            finally:
                proc.kill()
                await proc.wait()

    trio.run(main)
