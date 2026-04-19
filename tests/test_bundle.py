import os
import time
from pathlib import Path

import pytest

from bundle import (
    generate_keypair,
    load_private_key,
    load_public_key,
    make_key_name,
    public_key_to_b64,
    b64_to_public_key,
    validate_path,
    create_manifest,
    sign_manifest,
    verify_manifest,
    verify_files,
    create_register_proof,
    verify_register_proof,
    save_bundle,
    load_bundle,
    dict_to_bundle,
    bundle_to_dict,
    is_manifest_expired,
    MAX_BUNDLE_FILES,
    MAX_BUNDLE_TOTAL_SIZE,
    MAX_PATH_DEPTH,
    MAX_TIMESTAMP_DRIFT_SECONDS,
)


@pytest.fixture
def tmp_site(tmp_path):
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.md").write_text("# Hello", encoding="utf-8")
    (site / "about.md").write_text("# About", encoding="utf-8")
    return site


@pytest.fixture
def tmp_key(tmp_path):
    keys = tmp_path / "keys"
    keys.mkdir()
    priv, pub = generate_keypair(str(keys), "test")
    return priv, pub


class TestKeypair:
    def test_generate_and_load(self, tmp_path):
        keys = tmp_path / "keys"
        priv, pub = generate_keypair(str(keys), "test")
        assert Path(priv).exists()
        assert Path(pub).exists()
        private_key = load_private_key(priv)
        pub_key = load_public_key(pub)
        assert public_key_to_b64(pub_key) == public_key_to_b64(
            private_key.public_key()
        )

    def test_generate_with_passphrase(self, tmp_path):
        keys = tmp_path / "keys"
        priv, pub = generate_keypair(str(keys), "enc", passphrase="secret")
        with pytest.raises(TypeError):
            load_private_key(priv)
        loaded = load_private_key(priv, passphrase="secret")
        assert loaded is not None

    @pytest.mark.skipif(
        os.name == "nt",
        reason="Windows uses ACLs instead of POSIX mode bits; os.chmod is a no-op there.",
    )
    def test_key_permissions(self, tmp_path):
        keys = tmp_path / "keys"
        priv, _ = generate_keypair(str(keys), "perm")

        mode = Path(priv).stat().st_mode & 0o777
        assert mode == 0o600


class TestPublicKeyB64:
    def test_roundtrip(self, tmp_key):
        _, pub = tmp_key
        pub_key = load_public_key(pub)
        b64 = public_key_to_b64(pub_key)
        restored = b64_to_public_key(b64)
        assert public_key_to_b64(restored) == b64

    def test_invalid_b64_raises(self):
        with pytest.raises(Exception):
            b64_to_public_key("not-valid-base64!!!")


class TestValidatePath:
    def test_valid_path(self, tmp_path):
        result = validate_path(tmp_path, "sub/file.md")
        assert result.is_relative_to(tmp_path)

    def test_traversal_blocked(self, tmp_path):
        with pytest.raises(ValueError, match="Path traversal"):
            validate_path(tmp_path, "../../../etc/passwd")

    def test_base_dir_blocked(self, tmp_path):
        with pytest.raises(ValueError, match="base directory"):
            validate_path(tmp_path, ".")

    def test_too_deep(self, tmp_path):
        deep = "/".join(["d"] * (MAX_PATH_DEPTH + 1)) + "/file.md"
        with pytest.raises(ValueError, match="Path too deep"):
            validate_path(tmp_path, deep)

    def test_symlink_traversal(self, tmp_path):
        site = tmp_path / "site"
        site.mkdir()
        link = site / "link"
        link.symlink_to(tmp_path / ".." / "..")
        with pytest.raises(ValueError):
            validate_path(site, "link/target")


class TestCreateManifest:
    def test_basic_manifest(self, tmp_site):
        manifest = create_manifest(str(tmp_site), uri="test", author="alice")
        assert manifest["uri"] == "test"
        assert manifest["author"] == "alice"
        assert manifest["file_count"] == 2
        assert manifest["total_size"] > 0
        assert "expires_at" in manifest
        assert len(manifest["files"]) == 2

    def test_manifest_includes_uri_author(self, tmp_site):
        manifest = create_manifest(str(tmp_site), uri="blog", author="bob")
        assert manifest["uri"] == "blog"
        assert manifest["author"] == "bob"

    def test_empty_dir(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        manifest = create_manifest(str(empty))
        assert manifest["file_count"] == 0
        assert manifest["total_size"] == 0


class TestSignVerifyManifest:
    def test_sign_and_verify(self, tmp_site, tmp_key):
        priv, pub = tmp_key
        private_key = load_private_key(priv)
        pub_key = load_public_key(pub)

        manifest = create_manifest(str(tmp_site), uri="test", author="alice")
        signed, signature = sign_manifest(manifest, private_key)

        assert "public_key" in signed
        assert signed["public_key"] == public_key_to_b64(pub_key)
        assert verify_manifest(signed, signature, pub_key)

    def test_sign_does_not_mutate_input(self, tmp_site, tmp_key):
        priv, _ = tmp_key
        private_key = load_private_key(priv)

        manifest = create_manifest(str(tmp_site))
        original_keys = set(manifest.keys())
        _, _ = sign_manifest(manifest, private_key)
        assert set(manifest.keys()) == original_keys
        assert "public_key" not in manifest

    def test_wrong_key_fails(self, tmp_site):
        priv1, _ = generate_keypair(str(tmp_site.parent / "keys1"), "key1")
        priv2, _ = generate_keypair(str(tmp_site.parent / "keys2"), "key2")

        private1 = load_private_key(priv1)
        private2 = load_private_key(priv2)

        manifest = create_manifest(str(tmp_site))
        signed, sig = sign_manifest(manifest, private1)

        assert not verify_manifest(signed, sig, private2.public_key())

    def test_tampered_manifest_fails(self, tmp_site, tmp_key):
        priv, pub = tmp_key
        private_key = load_private_key(priv)
        pub_key = load_public_key(pub)

        manifest = create_manifest(str(tmp_site))
        signed, sig = sign_manifest(manifest, private_key)

        signed["file_count"] = 9999
        assert not verify_manifest(signed, sig, pub_key)


class TestVerifyFiles:
    def test_valid_files(self, tmp_site, tmp_key):
        priv, _ = tmp_key
        private_key = load_private_key(priv)
        manifest = create_manifest(str(tmp_site))
        signed, _ = sign_manifest(manifest, private_key)
        save_bundle(str(tmp_site), signed, "sig")
        errors = verify_files(signed, str(tmp_site))
        assert errors == []

    def test_missing_file(self, tmp_site, tmp_key):
        priv, _ = tmp_key
        private_key = load_private_key(priv)
        manifest = create_manifest(str(tmp_site))
        signed, _ = sign_manifest(manifest, private_key)
        (tmp_site / "index.md").unlink()
        errors = verify_files(signed, str(tmp_site))
        assert any("MISSING" in e for e in errors)

    def test_corrupted_file(self, tmp_site, tmp_key):
        priv, _ = tmp_key
        private_key = load_private_key(priv)
        manifest = create_manifest(str(tmp_site))
        signed, _ = sign_manifest(manifest, private_key)
        (tmp_site / "index.md").write_text("TAMPERED", encoding="utf-8")
        errors = verify_files(signed, str(tmp_site))
        assert any("CORRUPTED" in e for e in errors)

    def test_unauthorized_file(self, tmp_site, tmp_key):
        priv, _ = tmp_key
        private_key = load_private_key(priv)
        manifest = create_manifest(str(tmp_site))
        signed, _ = sign_manifest(manifest, private_key)
        (tmp_site / "sneaky.md").write_text("# Evil", encoding="utf-8")
        errors = verify_files(signed, str(tmp_site))
        assert any("UNAUTHORIZED" in e for e in errors)


class TestRegisterProof:
    def test_valid_proof(self, tmp_key):
        priv, pub = tmp_key
        private_key = load_private_key(priv)
        pub_b64 = public_key_to_b64(private_key.public_key())

        proof, ts = create_register_proof("blog", "alice", private_key)
        ok, msg = verify_register_proof("blog", "alice", pub_b64, proof, ts)
        assert ok
        assert msg == ""

    def test_wrong_uri(self, tmp_key):
        priv, _ = tmp_key
        private_key = load_private_key(priv)
        pub_b64 = public_key_to_b64(private_key.public_key())

        proof, ts = create_register_proof("blog", "alice", private_key)
        ok, msg = verify_register_proof("other", "alice", pub_b64, proof, ts)
        assert not ok

    def test_expired_timestamp(self, tmp_key):
        priv, _ = tmp_key
        private_key = load_private_key(priv)
        pub_b64 = public_key_to_b64(private_key.public_key())

        old_ts = int(time.time()) - MAX_TIMESTAMP_DRIFT_SECONDS - 100
        proof, _ = create_register_proof(
            "blog", "alice", private_key, timestamp=old_ts
        )
        ok, msg = verify_register_proof("blog", "alice", pub_b64, proof, old_ts)
        assert not ok
        assert "Timestamp" in msg


class TestBundleIO:
    def test_save_and_load(self, tmp_site):
        manifest = {"version": 1, "files": [], "file_count": 0, "total_size": 0}
        save_bundle(str(tmp_site), manifest, "testsig")
        loaded_manifest, loaded_sig = load_bundle(str(tmp_site))
        assert loaded_manifest == manifest
        assert loaded_sig == "testsig"

    def test_bundle_to_dict_roundtrip(self, tmp_site, tmp_key):
        priv, _ = tmp_key
        private_key = load_private_key(priv)

        manifest = create_manifest(str(tmp_site), uri="test", author="alice")
        signed, sig = sign_manifest(manifest, private_key)
        save_bundle(str(tmp_site), signed, sig)

        data = bundle_to_dict(str(tmp_site))
        assert data["manifest"]["uri"] == "test"
        assert "index.md" in data["files"]

        out_dir = tmp_site.parent / "restored"
        dict_to_bundle(data, str(out_dir))

        restored_manifest, restored_sig = load_bundle(str(out_dir))
        assert restored_manifest["uri"] == "test"
        assert restored_sig == sig


class TestManifestExpiry:
    def test_not_expired(self):
        manifest = {"expires_at": int(time.time()) + 3600}
        assert not is_manifest_expired(manifest)

    def test_expired(self):
        manifest = {"expires_at": int(time.time()) - 3600}
        assert is_manifest_expired(manifest)

    def test_no_expiry(self):
        assert not is_manifest_expired({})


class TestMakeKeyName:
    def test_simple(self):
        assert make_key_name("alice", "blog") == "alice_blog"

    def test_with_scheme(self):
        assert make_key_name("alice", "md://blog") == "alice_md_blog"

    def test_with_slash(self):
        assert make_key_name("bob", "site/path") == "bob_site_path"


class TestDictToBundleLimits:
    def test_too_many_files(self, tmp_path):
        files = {f"file{i}.md": "content" for i in range(MAX_BUNDLE_FILES + 1)}
        data = {
            "manifest": {
                "file_count": MAX_BUNDLE_FILES + 1,
                "total_size": 100,
            },
            "signature": "sig",
            "files": files,
        }
        with pytest.raises(ValueError, match="Too many files"):
            dict_to_bundle(data, str(tmp_path / "out"))

    def test_size_exceeded(self, tmp_path):
        big_content = "x" * (MAX_BUNDLE_TOTAL_SIZE + 1)
        data = {
            "manifest": {"file_count": 1, "total_size": MAX_BUNDLE_TOTAL_SIZE + 1},
            "signature": "sig",
            "files": {"big.md": big_content},
        }
        with pytest.raises(ValueError, match="exceeds size limit"):
            dict_to_bundle(data, str(tmp_path / "out"))


# ─── URI validation (migrated from test_protocol.py in Phase 6) ─────────

from bundle import validate_uri


class TestValidateUri:
    def test_accepts_simple(self):
        assert validate_uri("demo") == "demo"

    def test_accepts_dotted(self):
        assert validate_uri("blog.alice") == "blog.alice"

    def test_accepts_hyphens_underscores(self):
        assert validate_uri("my-site_v2") == "my-site_v2"

    def test_accepts_max_length(self):
        uri = "a" * 255
        assert validate_uri(uri) == uri

    def test_rejects_empty(self):
        with pytest.raises(ValueError, match="non-empty"):
            validate_uri("")

    def test_rejects_none(self):
        with pytest.raises(ValueError, match="non-empty"):
            validate_uri(None)

    def test_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="forbidden"):
            validate_uri("../../etc")

    def test_rejects_dot(self):
        with pytest.raises(ValueError, match="traversal"):
            validate_uri(".")

    def test_rejects_dotdot(self):
        with pytest.raises(ValueError, match="traversal"):
            validate_uri("..")

    def test_rejects_embedded_dotdot(self):
        with pytest.raises(ValueError, match="traversal"):
            validate_uri("foo..bar")

    def test_rejects_slash(self):
        with pytest.raises(ValueError, match="forbidden"):
            validate_uri("foo/bar")

    def test_rejects_backslash(self):
        with pytest.raises(ValueError, match="forbidden"):
            validate_uri("foo\\bar")

    def test_rejects_null_byte(self):
        with pytest.raises(ValueError, match="forbidden"):
            validate_uri("foo\x00bar")

    def test_rejects_too_long(self):
        with pytest.raises(ValueError, match="too long"):
            validate_uri("a" * 256)

    def test_rejects_spaces(self):
        with pytest.raises(ValueError, match="invalid characters"):
            validate_uri("my site")

    def test_rejects_starting_with_dot(self):
        with pytest.raises(ValueError, match="invalid characters"):
            validate_uri(".hidden")

    def test_rejects_starting_with_hyphen(self):
        with pytest.raises(ValueError, match="invalid characters"):
            validate_uri("-flag")
