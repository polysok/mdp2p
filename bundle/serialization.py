"""Bundle persistence and wire-format helpers.

Handles the on-disk layout (manifest.json + manifest.sig next to the files)
and the dict-based representation used to ship a bundle across the network.
"""

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import List, Tuple

from .manifest import MAX_BUNDLE_FILES, MAX_BUNDLE_TOTAL_SIZE
from .paths import validate_path


def save_bundle(site_dir: str, manifest: dict, signature: str) -> None:
    """Save the manifest and signature to the site directory."""
    site_path = Path(site_dir)
    (site_path / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    (site_path / "manifest.sig").write_text(signature, encoding="utf-8")


def load_bundle(site_dir: str) -> Tuple[dict, str]:
    """Load the manifest and signature from a directory."""
    site_path = Path(site_dir)
    manifest = json.loads(
        (site_path / "manifest.json").read_text(encoding="utf-8")
    )
    signature = (site_path / "manifest.sig").read_text(encoding="utf-8").strip()
    return manifest, signature


def bundle_to_dict(site_dir: str) -> dict:
    """Serialize a complete bundle (manifest + signature + files) to a dict.
    Used for peer-to-peer network transfer."""
    site_path = Path(site_dir).resolve()
    manifest, signature = load_bundle(site_dir)
    files = {}
    for entry in manifest["files"]:
        fpath = validate_path(site_path, entry["path"])
        # Decode the raw bytes rather than read_text(): universal-newline
        # translation would turn CRLF into LF and break the manifest hash.
        files[entry["path"]] = fpath.read_bytes().decode("utf-8")
    return {
        "manifest": manifest,
        "signature": signature,
        "files": files,
    }


def validate_bundle_payload(data: dict, output_dir: str) -> List[Tuple[str, bytes]]:
    """Check a received bundle dict in memory. Returns [(path, raw bytes)].

    Validates path safety (relative to ``output_dir``), file count, total
    size, and that the set of files is exactly the one listed in the
    manifest with matching SHA-256 hashes. Raises ValueError otherwise.
    Does not verify the manifest signature.
    """
    manifest = data["manifest"]
    files_data = data["files"]

    file_count = manifest.get("file_count", len(files_data))
    if file_count > MAX_BUNDLE_FILES or len(files_data) > MAX_BUNDLE_FILES:
        raise ValueError(f"Too many files: {file_count} (max {MAX_BUNDLE_FILES})")

    out = Path(output_dir).resolve()

    validated_files: List[Tuple[str, bytes]] = []
    actual_total_size = 0
    for path, content in files_data.items():
        if not isinstance(path, str) or not isinstance(content, str):
            raise ValueError("bundle files must map str paths to str contents")
        validate_path(out, path)
        raw = content.encode("utf-8")
        actual_total_size += len(raw)
        if actual_total_size > MAX_BUNDLE_TOTAL_SIZE:
            raise ValueError(
                f"Actual content exceeds size limit ({MAX_BUNDLE_TOTAL_SIZE} bytes)"
            )
        validated_files.append((path, raw))

    expected = {entry["path"]: entry["hash"] for entry in manifest["files"]}
    if set(files_data) != set(expected):
        extra = sorted(set(files_data) - set(expected))
        missing = sorted(set(expected) - set(files_data))
        raise ValueError(
            f"files do not match manifest (extra={extra}, missing={missing})"
        )
    for path, raw in validated_files:
        if not path.endswith(".md"):
            raise ValueError(f"only .md files are allowed in a bundle: {path}")
        if hashlib.sha256(raw).hexdigest() != expected[path]:
            raise ValueError(f"CORRUPTED: {path}")
    return validated_files


def dict_to_bundle(data: dict, output_dir: str) -> str:
    """Reconstruct a bundle from a dict received over the network.

    Everything is validated in memory first (see validate_bundle_payload),
    then written to a temporary sibling directory and swapped into place,
    so a hostile or truncated transfer can never clobber a previously-good
    local copy or leave unlisted files behind.

    The caller is still responsible for verifying the manifest signature.
    """
    validated_files = validate_bundle_payload(data, output_dir)
    out = Path(output_dir).resolve()

    out.parent.mkdir(parents=True, exist_ok=True)
    staging = out.parent / f".{out.name}.staging-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir()
    try:
        for path, raw in validated_files:
            fpath = validate_path(staging, path)
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_bytes(raw)
        save_bundle(str(staging), data["manifest"], data["signature"])
        _swap_into_place(staging, out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return str(out)


def _swap_into_place(staging: Path, out: Path) -> None:
    """Replace ``out`` with ``staging`` using renames (no partial state)."""
    if not out.exists():
        staging.rename(out)
        return
    backup = out.parent / f".{out.name}.old-{os.getpid()}"
    if backup.exists():
        shutil.rmtree(backup)
    out.rename(backup)
    try:
        staging.rename(out)
    except BaseException:
        backup.rename(out)
        raise
    shutil.rmtree(backup, ignore_errors=True)
