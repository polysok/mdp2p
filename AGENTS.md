# AGENTS.md — MDP2P (Markdown Peer-to-Peer)

## Project Overview

MDP2P is a decentralized web protocol based on Markdown. Each visitor becomes a seeder.
Project: `/Users/polysok/Documents/Projects/md-server`
Language: Python 3.10+
Dependencies: `cryptography>=42.0.0`

## Commands

```bash
# Install dependencies
pip install cryptography>=42.0.0

# Run the demo
python demo.py

# Run individual modules (for testing)
python -c "from tracker import Tracker; print('OK')"
python -c "from bundle import generate_keypair; print('OK')"
python -c "from peer import Peer; print('OK')"

# No test framework is currently configured. When adding tests:
pytest                          # Run all tests
pytest tests/ -v                # Run with verbose output
pytest tests/test_bundle.py     # Run single test file
pytest tests/ -k test_signing  # Run tests matching pattern
```

## Code Style

### Formatting
- Indentation: 4 spaces (no tabs)
- Line length: 88 characters (Black default)
- Use Black for formatting: `black .`
- Trailing commas in multi-line structures

### Imports
- Standard library imports first, then third-party, then local
- Use absolute imports: `from bundle import X` not `from .bundle import X`
- Group imports with blank lines between groups
- Sort within groups: `hashlib`, `json`, `os`, `base64` (alphabetical)

```python
# Correct order
import hashlib
import json
import os
import base64
from pathlib import Path
from typing import Tuple

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization
```

### Types
- Use type hints for all function parameters and return values
- Use `Optional[X]` instead of `X | None`
- Use `Tuple[X, Y]` for fixed-length tuples
- Import types from `typing` module

```python
def load_private_key(path: str) -> Ed25519PrivateKey:
    ...

def verify_manifest(manifest: dict, signature_b64: str) -> bool:
    ...
```

### Naming Conventions
- Classes: `PascalCase` (`Tracker`, `Peer`, `SiteRecord`)
- Functions/methods: `snake_case` (`generate_keypair`, `load_bundle`)
- Constants: `SCREAMING_SNAKE_CASE` (`MAX_MSG_SIZE`, `HEADER_SIZE`)
- Private methods: prefix with `_` (`_handle_register`, `_tracker_request`)
- Variables: `snake_case` (`site_dir`, `public_key_b64`)
- Type variables: `PascalCase` (e.g., `T = TypeVar('T')`)

### Docstrings
- Use triple double quotes `"""`
- One-line docstrings for simple functions
- Multi-line docstrings with proper spacing for complex functions
- First line should be a imperative summary (not "This function...")

```python
def generate_keypair(key_dir: str, name: str) -> Tuple[str, str]:
    """Generate an ed25519 key pair. Returns (private key path, public key path)."""

def _canonical_json(obj: dict) -> bytes:
    """
    Canonical JSON for deterministic signing.
    
    Used to ensure the manifest serializes identically each time,
    which is required for signature verification.
    """
```

### Error Handling
- Use specific exception types when possible
- Return `None` or empty collections for "not found" cases
- Use `try/except` sparingly and catch specific exceptions
- Let exceptions propagate for truly exceptional cases

```python
# Good: Return None for "not found"
async def recv_msg(reader: asyncio.StreamReader) -> Optional[dict]:
    try:
        header = await reader.readexactly(HEADER_SIZE)
    except (asyncio.IncompleteReadError, ConnectionResetError):
        return None

# Good: Return errors in result dict
if not uri or not public_key:
    return {"type": "error", "msg": "uri and public_key required"}
```

### Async/Await
- Use `async def` for all asynchronous functions
- Always `await` coroutines, never call them directly
- Close streams properly in `finally` blocks
- Use context managers where possible

```python
async def _tracker_request(self, msg: dict) -> dict:
    reader, writer = await asyncio.open_connection(
        self.tracker_host, self.tracker_port
    )
    try:
        await send_msg(writer, msg)
        response = await recv_msg(reader)
        return response or {"type": "error", "msg": "No response"}
    finally:
        writer.close()
        await writer.wait_closed()
```

### Logging
- Use `logging.getLogger(__name__)` for module loggers
- Log levels: DEBUG for detailed info, INFO for normal operations, WARNING/ERROR for problems
- Include relevant context in log messages

```python
logger = logging.getLogger("mdp2p.peer")
logger.info(f"Bundle created: {manifest['file_count']} files")
logger.error(f"ALERT: Invalid signature!")
```

### Security
- Never log private keys or sensitive data
- Use constant-time comparison where security-critical
- Validate all external input
- Always close resources in finally blocks

### Module Structure
```
protocol.py  - TCP message framing (length-prefixed JSON)
bundle.py    - Cryptographic signing and verification
tracker.py   - URI → peers resolution server
peer.py      - P2P client and seeder functionality
demo.py      - End-to-end demonstration
```

## Adding New Features

1. Maintain the module separation (protocol, bundle, tracker, peer)
2. Add type hints to all new functions
3. Update docstrings for public APIs
4. Add unit tests in `tests/` directory
5. Run `python demo.py` to verify end-to-end functionality

## Architecture Notes

- **Protocol**: JSON messages prefixed with 4-byte big-endian length
- **Identity**: Ed25519 keypairs; public key = site identity
- **Integrity**: SHA-256 hashes for files, ed25519 signatures for manifests
- **Networking**: Async TCP using `asyncio.StreamReader/Writer`
