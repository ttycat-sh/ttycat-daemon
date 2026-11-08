#!/usr/bin/env python3
"""Packs herdr's agent detection rules for the daemon: import_agent_rules.py HERDR_REPO

herdr (https://github.com/herdrdev/herdr, Apache-2.0) writes the rules that say what each agent's
screen means. The daemon runs on whatever Python a Mac has, so they're stored as JSON: tomllib
is only needed here. Run this again to pick up newer rules.
"""
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

repo = Path(sys.argv[1])
daemon = Path(__file__).resolve().parent.parent
commit = subprocess.run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
folder = repo / "distribution" / "agent-detection"
agents = {}
for path in sorted(folder.glob("*.toml")):
    if path.name == "index.toml":
        continue
    manifest = tomllib.loads(path.read_text())
    agents[manifest["id"]] = {"version": manifest.get("version", ""), "rules": manifest["rules"]}
out = {"from": f"herdr {commit}, distribution/agent-detection, Apache-2.0", "agents": agents}
(daemon / "agent_rules.json").write_text(json.dumps(out, ensure_ascii=False, indent=0) + "\n")
shutil.copyfile(repo / "LICENSE", daemon / "agent_rules.LICENSE")
print(f"{sum(len(a['rules']) for a in agents.values())} rules for {len(agents)} agents, from herdr {commit}")
