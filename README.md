# ttycat daemon

The part of [ttycat](https://github.com/ttycat-sh/ttycat-firmware) that runs on the Mac: it finds every coding agent, from herdr, Claude Code's own records, tmux and Codex, sends what they're doing to the board and the menu bar app, and carries Allow, Deny and answers back to the one prompt they were for. Python's standard library only.

| Repo | What |
|---|---|
| [ttycat-firmware](https://github.com/ttycat-sh/ttycat-firmware) | the board's firmware, and the protocol: `docs/PROTOCOL.md` |
| **ttycat-daemon** | this: the daemon, the hook, and the release formats it checks |
| [ttycat-app](https://github.com/ttycat-sh/ttycat-app) | the menu bar app, which carries this daemon and runs it |

## Run it

With the board on USB and herdr running:

```sh
python3 ttycatd.py run
```

The board leaves its "waiting for Mac" screen within a second or two. The daemon has to stay running; if it stops, the board goes back to that screen.

For Allow and Deny, add the permission hook to `~/.claude/settings.json` once. Running sessions pick it up without a restart. It adds one entry, and saves the previous file as `settings.json.ttycat-backup`:

```sh
python3 ttycatd.py hook install    # or: remove, status
```

Run this way, the hook runs from this folder, so install it again if the folder moves. The app's own hook doesn't have that problem.

To see every screen without touching real sessions, use the design's sample data. Nothing is answered in this mode:

```sh
python3 ttycatd.py demo
```

## Where agents come from

| Where | Which agents | Its state | Answer from the board or app |
|---|---|---|---|
| **In herdr** | All the agents it supports | Read from each agent's screen by herdr | Claude Code, through the hook |
| **Claude Code in any other terminal** | Claude Code | Claude's own record in `~/.claude/sessions/<pid>.json` | Through the same hook |
| **Any other agent in tmux** | Those herdr has rules for: Codex, OpenCode, Pi, Gemini and others | Read from its screen, by herdr's rules | No: its prompt is shown, and answered in its terminal |
| **The Codex app** | Its threads, active in the last 30 minutes | Codex's own records in `~/.codex` | No |
| **Codex in any other terminal** | Codex | The same records, if it's the only Codex in its folder | No |
| **Any other agent in any other terminal** | OpenCode, Gemini and others, by process name | None: it shows as running, "state unknown" | No |

herdr isn't required, on this Mac or on other machines. A session outside it just shows up, with nothing to install; the hook (Setup > Answer prompts on the board) is what lets you answer Claude from here. Anything inside herdr is left to herdr, so nothing appears twice. tmux inside a herdr pane counts as outside it, since herdr can't see in.

**What a screen means** comes from [herdr](https://github.com/herdrdev/herdr)'s detection rules (`agent_rules.json`, Apache-2.0, `agent_rules.LICENSE`) and a port of its engine (`screen_rules.py`), checked rule by rule against herdr's own `agent explain`. Update them with `tools/import_agent_rules.py HERDR_REPO`. Unlike herdr, they don't update themselves, so a new version of an agent can show the wrong state until they're refreshed. Unknown screens read as idle, as in herdr, so an unusual prompt can look idle. No keys are ever sent to these agents.

- **Which process is the agent** is worked out as herdr does: the terminal's foreground job, its leader first, by name, seeing through runtimes such as `node`, and never from an argument that only mentions one. Background services don't count.
- **An agent showing "state unknown" may be waiting on you.** Nothing can read another terminal's screen, so this can't tell. For its real state and prompts, run it in tmux or herdr.
- **Codex's log can't tell waiting for approval from a long command.** A thread that's able to ask says "may be waiting for approval" after 20 quiet seconds with a tool call open. In tmux, its screen tells them apart.
- **Claude's record isn't a documented interface.** It's read loosely (`discovery.py`): anything unfamiliar means fewer rows, never a wrong answer. If it changes in a Claude update, a Claude session outside herdr drops to "state unknown" and its prompts stay in the terminal. herdr sessions aren't affected. Nor is Codex's database or log documented (`codex_threads.py`), and they're read the same way, read-only.
- **Open in terminal** brings the session's terminal app forward, and the right pane under tmux. It can't pick the tab in other terminals. On another machine, only a herdr pane can be opened from here. A Codex app thread opens in the app, through its `codex://threads/` link.
- **Notifications can't tell which tab you're on** outside herdr, so a banner can appear for the session you're already looking at.

## Answering prompts safely

Allow, Deny and answers to questions go through Claude Code's `PermissionRequest` hook (`hook.py`, `approvals.py`). Nothing is typed into a terminal. The terminal prompt stays up as usual, and whichever answers first wins.

- **An answer can only reach the request it was shown for.** Each request is a hook process waiting on its own socket connection. The board echoes that request's id, and a decision goes only to that process.
- **Failure leaves the prompt in the terminal.** With no daemon, no board, a crash or a timeout, the hook exits without a decision, and the prompt stays open. Nothing is approved.
- **Allow is the plain Yes.** It never changes the command and never adds a permission rule. Rules in your settings that deny or ask still apply.
- **Deny is the plain No.** It ends the turn, and the agent waits for you, as with No in the terminal. No answer here lets it carry on.
- **Nothing is approved unseen.** The board shows the real command or diff, not Claude's description of it. A clipped one is marked and can be read in full by swiping up. Anything over 1000 characters is terminal-only, and so is an ask that doesn't fit the board's 6 KB of shared text.
- **Stray taps don't count.** A tap within 600 ms of an ask appearing is ignored, and so is a press that began on another screen.
- **A question can only be answered, and an answer can only go to a question.** The board sends option numbers and typed text. The daemon turns numbers back into the original labels and refuses anything incomplete, and `hook.py` fills in answers for `AskUserQuestion` and nothing else.
- **Choosing takes two taps.** A tap only selects; **Send** answers. Both ignore a tap within 600 ms of the screen appearing.
- **Plans are terminal-only.** `ExitPlanMode` also fires the hook, but Allow would dismiss it without your answer. A question the board can't show in full is terminal-only too.
- **Other prompts are terminal-only.** Folder trust and other agents don't use this hook. Their text comes from the terminal and is only displayed.
- **Nothing counts down.** Take as long as you need to read a prompt. After 2 minutes without a touch the screen goes back to the queue, so Allow isn't left live on the desk. The prompt stays open and can be reopened from the board.

Tested against real Claude Code 2.1.316. These behaviours aren't all documented, so check them again after a major update:

| Situation | What Claude Code does |
|---|---|
| Hook still waiting | The terminal prompt is up and usable |
| Hook crashes, times out, or says nothing | The prompt stays open and nothing runs |
| Allow arrives after No was pressed in the terminal | Ignored |
| Yes pressed in the terminal | The hook is left running, so the daemon drops it once herdr says the pane moved on |
| No pressed in the terminal | The hook is stopped |
| Several tool calls at once | They ask one at a time, so a new request in a pane replaces the last |
| Allow with `answers` for a question | Claude gets them as your answers, including typed text and several questions at once |

## How it maps to herdr

| herdr | Board |
|---|---|
| `blocked` | needs you |
| `working` | working |
| `done` | done, until tapped ("seen"; kept in the daemon, so herdr's own state and focus are untouched) |
| `idle`, `unknown` | idle |

Tabs are `claude` CC, `codex` CX, `opencode` OC and `pi` PI. Other kinds show under ALL only.

## Known limits

- **Claude Code is the only agent with Allow and Deny.** Codex is terminal-only, as designed. OpenCode is terminal-only too, unlike the design: no real OpenCode prompt could be captured (its provider returned errors).
- **The keyboard types ASCII.** A label with an accent shows as `?` on the board, but Claude still gets the original. Typed answers on the board are limited to 200 characters.
- **The Error state never appears with live data.** herdr has no such state, so only `demo` shows it.
- **"Recent" reads Claude Code's session log (`activity.py`), here and over SSH.** It lists your prompts, tool calls, failures and what Claude said, and it changes nothing. Claude's docs call the log format internal, so a release could break it: the list then falls back to status changes. Codex, OpenCode and Pi only have status changes for now. Prompts and file names now show on the board, so anyone at your desk can read them.
- **Only sessions in herdr panes reach the board.** The hook does nothing elsewhere, so those prompts stay in their terminal.
- **Attach is untested on a pane that's on screen on the other machine.** herdr's docs say one direct attach owns a terminal's input and size, so that machine's view may resize until the window closes.
- **The board holds 24 sessions across all machines.** Past that, the ones that need you are kept.

## Releases it checks

The daemon writes ttycat's own firmware, and builds a board of someone's own, only from releases signed by a key in `release_keys.json` (`release.py`). The tools that make them are here, beside the code that checks them:

- `tools/release_key.py` makes a signing key, and `--add` trusts it.
- `tools/firmware_bundle.py BUILD OUT [--sign]` packs a firmware build into the image the app carries.
- `tools/release_core.py FIRMWARE OUT` packs a ttycat-firmware checkout into the source release a board of someone's own is built from.
- `tools/publish_release.py` offers releases to Macs through the update server.

ttycat-firmware's release workflow runs them, from the daemon it pins.

## Tests

```sh
python3 -m unittest discover -s tests
```

The hook tests run the real `hook.py` over a real socket, with payloads shaped like those captured from Claude Code.

## License

Apache-2.0: see `LICENSE` and `NOTICE`. herdr's detection rules are Apache-2.0 too: `agent_rules.LICENSE`. The license doesn't grant use of the product's name or logo.
