# ChatGPT Conversation Cleanup

A small, **source-only Python CLI** for resolving two different kinds of
ChatGPT Desktop cleanup problems. No app bundle, installer, executable build,
background daemon, paid service, or third-party Python package is required.

This is an independent tool, not an OpenAI product.

## What it does

| Operation | Actual behavior | Does ChatGPT need to quit? |
| --- | --- | --- |
| **1. Local ChatGPT list cleanup** | Repairs confirmed or manually reviewed leftover entries in this computer's Desktop conversation catalog, with private recovery records. **Does not delete server-side ChatGPT conversations.** | **Yes**, for verified local catalog writes; the tool never force-quits other managers. |
| **2. Archived Codex permanent deletion** | Invokes **official Codex app-server `thread/delete`** on exact archived logical threads. Codex itself removes spawned subagents, history versions, compression files and indexes. Cleanup only selects, checks and reports. | **No.** Native thread-owner locks may refuse an individual busy or referenced thread. |

The second operation addresses archived Codex conversations that remain after
the official Desktop Delete action fails. A live fork can require the original
archived history: Codex will protect that original, and Cleanup **never**
rewrites the fork, deletes files directly or bypasses the refusal. Other
eligible archived conversations can still be deleted.

**Scope:** This is not a bulk server-side ChatGPT history deleter, and it
does not promise to purge remote backups, diagnostic logs or project files
produced in past conversations.

## Run directly from source

Requires Python **3.11+**. On macOS, use a supported Homebrew Python such as
`python3.14` if the OS-provided `python3` is too old.

From the cloned project directory:

```sh
python3 run.py
```

The interactive menu presents the two operations, profile selection, a
review of the exact proposed changes and an explicit final confirmation
before deletion. There are no install or packaging steps.

Read-only commands useful to human operators **and agents**:

```sh
python3 run.py --version
python3 run.py doctor --all-profiles --json
python3 run.py list-archived --profile default
python3 run.py list-archived --profile profile2
```

The listing shows full logical thread IDs. For an explicitly **user-approved**
archived deletion, agents without an interactive terminal can use:

```sh
# Preview only: zero deletions.
python3 run.py delete-archived --profile default --ids "UUID_A,UUID_B"

# Use the exact phrase printed by the preview, only after user approval.
python3 run.py delete-archived --profile default --ids "UUID_A,UUID_B" --confirm "DELETE 2 CHATS"
```

Substitute the complete archived IDs from the listing. The required confirmation
count is calculated by the official-plan preview, including spawned descendants;
it may differ from the number of explicitly listed IDs. A changed selection or
unexpected count fails closed. **Never guess the confirmation phrase.**
There is no implicit `delete-all` switch. An agent may also use the reviewed
`preview_codex_delete` / `execute_codex_delete_plan` functions rather than
implementing any deletion logic.

The source-only entry point is `run.py`. The official Codex executable is
located from the verified profile's ChatGPT installation (or a supported
Codex CLI). The program never patches or re-signs ChatGPT.

### Supported environments

- **macOS:** Verified with the official signed ChatGPT Desktop and separately
  verified, isolated multi-profile runtimes. Native archive deletion can run
  with the Desktop open.
- **Windows:** Read-only and profile/runtime scaffolding exists. Destructive
  features require compatible native runtime/build verification. No signed
  Windows release is shipped.
- **Linux:** Not claimed as a verified ChatGPT Desktop target.

If an isolated profile's archived chats do not appear in the app's normal
list, `doctor` still reports the matching local Codex state and checks whether
the stored `.jsonl` or `.jsonl.zst` rollout exists. A missing UI row by itself
is **not** proof that its user data is disposable.

## Design and safety

1. **Official-first deletion.** Only Codex's own `thread/delete` performs
   irreversible conversation deletion. Parent/child cascades, compression
   formats and fork-history protections remain upstream responsibilities.
2. **Thin orchestration.** Cleanup verifies the exact archived selection,
   protects active/unknown spawned descendants, isolates confirmed reference
   or writer refusals and stops on ambiguous errors.
3. **Read-only result verification.** It checks native database rows, rollout
   versions, history projections and spawned indexes after native success.
   Any leftover is reported, **never repaired by direct deletion**.
4. **No global app shutdown for archived deletion.** Only the *separate*
   direct local Desktop-catalog operation requires an exclusive owner.
5. **No bundled runtime.** The checkout is the executable source; there is
   no PyInstaller artifact, `.app`, `.exe`, background service or duplicate
   installed copy to keep in sync.

Read [the architecture](docs/ARCHITECTURE.md) for the two separate trust
boundaries and [research notes](docs/RECENT_RESEARCH.md) for the native API
decisions. See [security guidance](SECURITY.md) before sharing diagnostics.

## Diagnostics, recovery and cleanup

`doctor --json` omits user conversation titles, bodies, email addresses,
paths and tokens. Local catalog cleanup keeps separate private undo records
outside the checkout. The optional `purge-data` command requires an explicit
confirmation and removes **only this tool's own local data**, not ChatGPT
conversations. To uninstall the source-only tool, remove its checkout; personal
undo records are not silently destroyed.

Automated tests use synthetic profiles and never delete real conversations:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```

No package install or build is required for tests. CI runs the same source
tests and produces **no downloadable program binaries**.

Licensed under [MIT](LICENSE).
