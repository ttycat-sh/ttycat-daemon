# Working on the ttycat daemon

The daemon finds coding agents, tells the board and the app what they're doing, and carries answers back to the one prompt each was for. The board's side of the conversation is `docs/PROTOCOL.md` in [ttycat-firmware](https://github.com/ttycat-sh/ttycat-firmware), whose `AGENTS.md` has the rules that always hold. Keep them here too:

- **Nothing approves by itself, and nothing rewards approving.** No auto-allow, no timers, no "allow all"; Deny always takes effect.
- **Allow only where the whole prompt can be read**, and only ttycat's release builds may approve without being asked about (`devices.py`).
- **Never answer a prompt yourself.** Not with `ttycatd.py tap`, not over serial, not on someone's real sessions. To test, use `demo`.
- **Don't touch their setup without asking:** no flashing, no changes to `~/.claude/settings.json`, no pushing, without a yes for that one action.

Standard library only. Run the tests with `python3 -m unittest discover -s tests`.
