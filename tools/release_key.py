#!/usr/bin/env python3
"""A new key for signing releases: release_key.py [--add NOTE]

Prints the secret, to keep only where releases are signed (TTYCAT_SIGNING_KEY in ttycat-firmware's
release environments, for firmware and core releases), and the public half. The app's update keys
are made with it too, without --add: their public halves are in ttycat-app's tools/app_build.py.
--add also puts the public half in release_keys.json, so apps built
from here on trust it. Apps already out there only trust the keys they shipped with: keep the
secret safe, and back it up, or releases can't reach them.
"""
import argparse
import base64
import json
import os
import sys
from pathlib import Path

KEYS = Path(__file__).resolve().parents[1] / "release_keys.json"
sys.path.insert(0, str(KEYS.parent))
import ed25519  # noqa: E402
import release  # noqa: E402

p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
p.add_argument("--add", metavar="NOTE", help="trust it from now on, with a note saying what it's for")
a = p.parse_args()

secret = os.urandom(32)
public = ed25519.public_key(secret)
entry = {"id": release.key_id(public), "key": base64.b64encode(public).decode()}
print("secret (TTYCAT_SIGNING_KEY):", base64.b64encode(secret).decode())
print("public:", json.dumps(entry))
if a.add:
    data = json.loads(KEYS.read_text())
    data["keys"].append({**entry, "note": a.add})
    KEYS.write_text(json.dumps(data, indent=1) + "\n")
    print("added to", KEYS)
