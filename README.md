# ChatGPT Conversation Cleanup

A source-only Python CLI that **delegates cleanup to the original
ChatGPT/Codex applications**. No installer, app bundle, custom
deletion engine, background service, or external Python dependencies.

Requires Python 3.11+ and a compatible ChatGPT Desktop/Codex installation.
Run `python3 run.py --help` from the checkout. On macOS, `python3.14`
may be needed if the system Python is older.

## Interpret the user's request correctly

There are **two completely separate operations**. Agents must distinguish
them before taking action; neither is a generic ChatGPT server-side delete API.

| User's meaning | Example Korean request | Action |
| --- | --- | --- |
| Normal Chat already deleted on the server, but still shown in Desktop | “일반 Chat에서 지웠는데 앱 목록에 남아 있어” / “안 지워지는 채팅 삭제해 줘” (no archive context) | `repair-chat-list` **read-only preview first**; confirm the chat is already deleted remotely |
| An archived Work/Codex chat cannot be deleted | “보관된 Work/Codex 채팅이 삭제되지 않아” | `delete-archived-codex` (official Codex `thread/delete`) |
| Unclear what “delete” refers to | “채팅 삭제해 줘” (no further context) | **Ask which case first; do not execute a command yet** |
| A normal Chat still exists on the server and needs actual deletion | “일반 Chat 자체를 서버에서 삭제해 줘” | **Not implemented**; use the original ChatGPT Delete action |

As a conversational default, **“안 지워지는 채팅” without “보관/아카이브”
usually means the normal Chat stale-list problem**. Begin with read-only
list-repair diagnosis, not native Codex deletion. This does not itself
prove server deletion; verify it before scheduling a refresh.

Before execution, identify the **profile, chat mode, exact target and whether
the server already deleted the chat**. Verify through read-only inventory.
Do not infer Work/Codex solely from the word “archive” or infer Refresh solely
from “delete.” Never silently switch operations if one fails.

## Commands

```sh
python3 run.py doctor --all-profiles --json
python3 run.py list-archived --profile default --json

# Normal Chat: read-only preview of Desktop-owned reconciliation.
python3 run.py repair-chat-list --profile default

# Work/Codex archive: read-only preview of native deletion.
python3 run.py delete-archived-codex --profile default --ids "UUID_A,UUID_B"
```

Both write-capable commands **preview only by default**. After reviewing the
exact target and obtaining the user's consent, rerun with the complete
`--confirm` phrase printed by the preview:

```sh
python3 run.py delete-archived-codex --profile default --ids "UUID_A,UUID_B" --confirm "DELETE 2 CHATS"
```

This example is illustrative; native Subagent descendants may change the
confirmation count. There is intentionally no delete-all shortcut.

A normal Chat profile may contain **multiple Desktop catalog hosts for one
account**. The read-only repair preview lists opaque host tokens and row
counts; in that case, identify the intended host explicitly and supply
`--host-token TOKEN`. Its confirmation phrase also binds that token.
Do not pick a host merely because its row count looks familiar.

## Ownership and safety

**`repair-chat-list`** schedules the original Desktop's full reconciliation
using **version-verified native synchronization metadata**. It does not
delete any ChatGPT server-side conversation, directly remove local catalog
rows or invent remote deletion evidence. The relevant Desktop may need a
normal close/reopen so the official app can perform its authenticated scan.
Cleanup then reads the result. Unknown builds, catalog hosts and process
owners are protected.

**`delete-archived-codex`** calls the official profile-bound Codex app-server
`thread/delete`. Codex owns Subagent cascades, rollout removal (both
`.jsonl` and `.jsonl.zst`) and index cleanup. The ChatGPT app may remain
open. Native writer locks and active-fork history dependencies are respected.
Cleanup never deletes Codex files or SQLite conversation rows directly.

Neither action promises immediate purge of remote backups, diagnostic logs,
generated project files, or unrelated caches. No source code deletes those
artifacts or the user's existing private recovery records.

See [architecture and incomplete work](docs/ARCHITECTURE.md) and
[security guidance](SECURITY.md).

## Testing

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

Destructive automated tests use only synthetic profiles. The tool does
not bundle or install any runtime and does not patch the official apps.

Licensed under [MIT](LICENSE). Not affiliated with OpenAI.
