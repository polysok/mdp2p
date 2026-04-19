"""
MDP2P Bundle — Creation, signing and verification of Markdown sites.

A bundle is a directory containing:
  - manifest.json  : list of files, SHA-256 hashes, version, public key
  - manifest.sig   : ed25519 signature of the manifest (base64)
  - *.md           : the site files

The public key serves as the site's identity. The human-readable URI is just an alias.

This package exposes a flat API: every public symbol is re-exported here so
existing callers (`from bundle import X`) continue to work unchanged.
"""

from .crypto import (
    b64_to_public_key,
    generate_keypair,
    load_private_key,
    load_public_key,
    public_key_to_b64,
)
from .manifest import (
    DEFAULT_TTL_SECONDS,
    MAX_BUNDLE_FILES,
    MAX_BUNDLE_TOTAL_SIZE,
    compute_content_key,
    compute_manifest_ref,
    create_manifest,
    is_manifest_expired,
    sign_manifest,
    verify_files,
    verify_manifest,
)
from .name_records import (
    MAX_TIMESTAMP_DRIFT_SECONDS,
    build_name_record,
    create_register_proof,
    sign_name_record,
    verify_name_record,
    verify_register_proof,
)
from .paths import (
    MAX_PATH_DEPTH,
    MAX_URI_LENGTH,
    _VALID_URI_RE,
    make_key_name,
    validate_path,
    validate_uri,
)
from .serialization import (
    bundle_to_dict,
    dict_to_bundle,
    load_bundle,
    save_bundle,
)

__all__ = [
    # crypto
    "b64_to_public_key",
    "generate_keypair",
    "load_private_key",
    "load_public_key",
    "public_key_to_b64",
    # paths
    "MAX_PATH_DEPTH",
    "MAX_URI_LENGTH",
    "_VALID_URI_RE",
    "make_key_name",
    "validate_path",
    "validate_uri",
    # manifest
    "DEFAULT_TTL_SECONDS",
    "MAX_BUNDLE_FILES",
    "MAX_BUNDLE_TOTAL_SIZE",
    "compute_content_key",
    "compute_manifest_ref",
    "create_manifest",
    "is_manifest_expired",
    "sign_manifest",
    "verify_files",
    "verify_manifest",
    # name_records
    "MAX_TIMESTAMP_DRIFT_SECONDS",
    "build_name_record",
    "create_register_proof",
    "sign_name_record",
    "verify_name_record",
    "verify_register_proof",
    # serialization
    "bundle_to_dict",
    "dict_to_bundle",
    "load_bundle",
    "save_bundle",
]
