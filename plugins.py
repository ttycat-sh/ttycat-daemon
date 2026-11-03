"""What an extension can add to the daemon. The open-source daemon runs fine with none.

An extension is a folder holding ttycat_plugin.py, with a register(api) function. Everything it
can reach is on Api, so this file is the contract between the two: change it, and bump API_VERSION.
A plugin that fails to load is logged and left out: the rest of the daemon carries on.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Callable

API_VERSION = 1


class Api:
    version = API_VERSION

    def __init__(self):
        # {"t": name} on the control socket -> reply. Runs in the board's loop: return at once.
        self.ctl: dict[str, Callable[[dict], dict]] = {}
        # Called with the `run` subcommand's parser, before arguments are read.
        self.run_arguments: list[Callable] = []
        # (args, pet, focus_app) -> sources for other machines; each needs .hub and .stop().
        self.machines: Callable | None = None
        # name -> (configure(parser), run(args)): extra subcommands.
        self.commands: dict[str, tuple[Callable, Callable]] = {}


def load(folder: str | None, log=print) -> Api:
    api = Api()
    if not folder:
        return api
    try:
        sys.path.insert(0, str(Path(folder).resolve()))
        importlib.import_module("ttycat_plugin").register(api)
    except Exception as e:  # noqa: BLE001  whatever it is, the free features still work
        log(f"plugin in {folder} not loaded: {e!r}")
        return Api()
    return api
