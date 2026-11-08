#!/usr/bin/env python3
"""Packs a built firmware into a folder the Mac app can carry: firmware_bundle.py BUILD_DIR OUT_DIR [--sign]

The version comes out of the binary itself, the string the board sends when it starts, so the
app can't be told a version that isn't what's really in the image. See make_image in flasher.py.

--sign signs it as a release, with the key in $TTYCAT_SIGNING_KEY (tools/release_key.py): only
ttycat's own firmware, with a plain version, is signed, and the app only writes that signed.
"""
import argparse
import base64
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import ed25519  # noqa: E402
import release  # noqa: E402
from flasher import make_image, read_image  # noqa: E402

p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
p.add_argument("build", help="such as build-NAME in ttycat-firmware")
p.add_argument("out")
p.add_argument("--sign", action="store_true", help="sign it as a release, with $TTYCAT_SIGNING_KEY")
a = p.parse_args()

secret = None
if a.sign:
    try:
        secret = base64.b64decode(os.environ.get("TTYCAT_SIGNING_KEY", ""), validate=True)
    except ValueError:
        secret = b""
    if len(secret) != 32:
        sys.exit("TTYCAT_SIGNING_KEY isn't set to a signing key: see tools/release_key.py")
try:
    version = make_image(Path(a.build), Path(a.out), secret)
    if secret:  # the check an app makes, with this key
        public = ed25519.public_key(secret)
        read_image(a.out, {release.key_id(public): public})
except ValueError as e:
    sys.exit(str(e))
print(f"firmware {version}: {a.out}" + (" (signed)" if secret else ""))
if secret and release.key_id(ed25519.public_key(secret)) not in release.trusted_keys():
    print("warning: that key isn't in release_keys.json, so apps won't write this image", file=sys.stderr)
