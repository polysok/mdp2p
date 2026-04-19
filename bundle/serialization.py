"""Bundle persistence and wire-format helpers.

Handles the on-disk layout (manifest.json + manifest.sig next to the files)
and the dict-based representation used to ship a bundle across the network.
"""

import json
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
        files[entry["path"]] = fpath.read_text(encoding="utf-8")
    return {
        "manifest": manifest,
        "signature": signature,
        "files": files,
    }


def dict_to_bundle(data: dict, output_dir: str) -> str:
    """Reconstruct a bundle from a dict received over the network.

    Validates path safety, file count, and total size before writing anything.
    """
    manifest = data["manifest"]
    files_data = data["files"]

    file_count = manifest.get("file_count", len(files_data))
    if file_count > MAX_BUNDLE_FILES:
        raise ValueError(f"Too many files: {file_count} (max {MAX_BUNDLE_FILES})")

    out = Path(output_dir).resolve()

    validated_files: List[Tuple[Path, str]] = []
    actual_total_size = 0
    for path, content in files_data.items():
        fpath = validate_path(out, path)
        actual_total_size += len(content.encode("utf-8"))
        if actual_total_size > MAX_BUNDLE_TOTAL_SIZE:
            raise ValueError(
                f"Actual content exceeds size limit ({MAX_BUNDLE_TOTAL_SIZE} bytes)"
            )
        validated_files.append((fpath, content))

    out.mkdir(parents=True, exist_ok=True)
    for fpath, content in validated_files:
        fpath.parent.mkdir(parents=True, exist_ok=True)
        fpath.write_text(content, encoding="utf-8")

    save_bundle(output_dir, data["manifest"], data["signature"])
    return str(out)
