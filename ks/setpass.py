"""Generate/rotate ks-secret.json (Fork C2).

Usage:
    uv run python -m ks.setpass                Set (or replace) the passphrase.
                                               Creates the JWT signing key if absent.
    uv run python -m ks.setpass --rotate-key   Regenerate the JWT signing key only.
                                               Invalidates all live access tokens;
                                               connectors recover via refresh.

The file is written with mode 600 and must never be committed or pasted.
Contents:
    passphrase: scrypt parameters + salt + derived hash (base64)
    jwt_key:    32 random bytes, base64 (HS256 signing key)
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import secrets
import sys
import time

from . import config

SCRYPT_N = 2**14
SCRYPT_R = 8
SCRYPT_P = 1


def _read_existing() -> dict:
    try:
        with open(config.SECRET_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _write(data: dict) -> None:
    data["updated"] = int(time.time())
    tmp = str(config.SECRET_PATH) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, config.SECRET_PATH)
    print(f"wrote {config.SECRET_PATH} (mode 600)")


def _new_key() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def _hash_passphrase(passphrase: str) -> dict:
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        passphrase.encode("utf-8"), salt=salt,
        n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32,
    )
    return {
        "salt": base64.b64encode(salt).decode("ascii"),
        "hash": base64.b64encode(derived).decode("ascii"),
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage ks-secret.json")
    parser.add_argument("--rotate-key", action="store_true",
                        help="regenerate the JWT signing key only")
    args = parser.parse_args()

    data = _read_existing()

    if args.rotate_key:
        if "passphrase" not in data:
            print("no existing secret file; run without --rotate-key first",
                  file=sys.stderr)
            return 1
        data["jwt_key"] = _new_key()
        _write(data)
        print("JWT signing key rotated; all live access tokens invalidated")
        return 0

    p1 = getpass.getpass("New passphrase: ")
    p2 = getpass.getpass("Repeat: ")
    if p1 != p2:
        print("passphrases do not match", file=sys.stderr)
        return 1
    if len(p1) < 12:
        print("refusing: passphrase under 12 characters", file=sys.stderr)
        return 1

    data["passphrase"] = _hash_passphrase(p1)
    if "jwt_key" not in data:
        data["jwt_key"] = _new_key()
    _write(data)
    print("passphrase set")
    return 0


if __name__ == "__main__":
    sys.exit(main())
