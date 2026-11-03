#!/usr/bin/env python3
"""Claude Code PermissionRequest hook: lets the board answer a permission prompt.

Claude shows its normal prompt in the terminal while this runs, so either place can
answer. This prints a decision only when someone answered on the board. On anything
else (no daemon, no board, daemon restart, any error) it exits without a decision,
and the terminal prompt is simply still there.

Install it with: ttycatd.py hook install

It also runs on other machines, on whatever Python they have (macOS's own is 3.9): see remote_hook.py.
"""
from __future__ import annotations

import json
import os
import socket
import sys

SOCK = os.environ.get("TTYCAT_HOOK_SOCK") or os.path.expanduser("~/.ttycat/hook.sock")


def wait_for_answer(pane: str, request: dict) -> dict | None:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1)
    s.connect(SOCK)
    s.sendall(json.dumps({"pane": pane, "request": request}).encode() + b"\n")
    s.settimeout(5)
    reply = b""
    while b"\n" not in reply:
        try:
            part = s.recv(4096)
        except socket.timeout:  # not the same as TimeoutError before Python 3.10
            if os.getppid() == 1:  # Claude is gone
                return None
            continue
        if not part:
            return None  # the prompt was answered elsewhere, or the daemon stopped
        reply += part
    return json.loads(reply)


def decision(answer: dict | None, request: dict) -> dict | None:
    if not answer:
        return None
    answers = answer.get("answers")
    if answers is not None:
        # The only input the board can ever fill in: a question's own answers.
        if request.get("tool_name") != "AskUserQuestion" or not isinstance(answers, dict):
            return None
        return {"behavior": "allow", "updatedInput": {**request["tool_input"], "answers": answers}}
    if answer.get("behavior") == "allow":
        return {"behavior": "allow"}  # a command is never changed, and no permission is ever added
    if answer.get("behavior") == "deny":
        return {"behavior": "deny", "message": str(answer.get("message", "")), "interrupt": bool(answer.get("interrupt"))}
    return None


def main():
    request = json.load(sys.stdin)
    # In herdr, its pane. In any other terminal, the session: see claude_sessions.py. herdr clears TMUX
    # from its panes, so having both means tmux inside a pane, which herdr can't see into.
    pane = "" if os.environ.get("TMUX") else os.environ.get("HERDR_PANE_ID", "")
    pane = pane or (f"s:{request['session_id'][:8]}" if request.get("session_id") else "")
    if not pane:
        return
    made = decision(wait_for_answer(pane, request), request)
    if made:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": made}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
