#!/usr/bin/env python3
"""Publishes signed releases to ttycat's update server, as the release workflow does:

  publish_release.py --api https://api.ttycat.sh RELEASE...

Each RELEASE is a core release's ttycat-core-VERSION.json, or an official board's image folder with its
release.json. The token is $API_PUBLISH_TOKEN. Publish only once the files are in the bucket: from then
on Macs are offered them. The server checks each signature too, and never replaces a release, so one
that's already there counts as done: a workflow run can be repeated.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
p.add_argument("releases", nargs="+")
p.add_argument("--api", required=True, help="such as https://api.ttycat.sh")
p.add_argument("--rollout", type=int, default=100, help="the share of Macs offered it, 0 to 100")
a = p.parse_args()
token = os.environ.get("API_PUBLISH_TOKEN", "")
if not token:
    sys.exit("API_PUBLISH_TOKEN isn't set")

for item in a.releases:
    path = Path(item)
    envelope = json.loads((path / "release.json" if path.is_dir() else path).read_text())
    request = urllib.request.Request(
        a.api.rstrip("/") + "/v1/releases", method="POST",
        data=json.dumps({"release": envelope, "rollout": a.rollout}).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            print(f"{item}: published, {response.read().decode().strip()}")
    except urllib.error.HTTPError as e:
        said = e.read().decode().strip()
        if e.code == 409:
            print(f"{item}: already published")
            continue
        sys.exit(f"{item}: {e.code} {said}")
