# ChatGPT Conversation Cleanup

An unofficial terminal utility for two deliberately separate jobs:

1. reconcile and remove stale **local ChatGPT Desktop list entries** without deleting server-side ChatGPT conversations; and
2. permanently delete selected **archived Codex conversations** through the official Codex app-server, allowing Codex itself to remove spawned subagents and associated history.

It is not a bulk ChatGPT history deleter. Ordinary server-side ChatGPT deletion remains a ChatGPT UI action.

The common case stays simple: if one ChatGPT/Codex profile is found, no profile selector is shown. Verified multi-profile providers are detected only when present.

When several profiles are present, the tool can ask the official local Codex app-server for read-only account metadata so profiles can be distinguished by available email, plan and workspace/account identifier. Authentication tokens are not parsed for display.

> Independent open-source project. Not affiliated with or endorsed by OpenAI.

## Platform status

| Platform | Read-only diagnostics | Archived Codex deletion | Direct local catalog cleanup |
|---|---:|---:|---:|
| macOS | Yes | Yes, through the bundled/official Codex app-server | Build + schema gated |
| Windows | Foundation implemented | Requires an available official Codex runtime and validation | Read-only until a Windows build is verified |
| Linux | Core release not validated | Not advertised yet | Not supported |

Platform-specific discovery, process checks and write gates stay outside the common cleanup logic. Unsupported or changed external state fails closed rather than being guessed.

## Portable first: no installation

The release model is portable. There is no second installed program copy and no background service.

This fresh public repository does not yet publish signed consumer binaries. Running from source is supported below, and GitHub Actions produces unsigned portable test artifacts for validation. Signed/notarized consumer downloads can be added later without changing the portable model.

### macOS

For a portable release or CI test artifact, expand the archive and double-click the single Finder item:

~~~text
ChatGPT Conversation Cleanup.app
~~~

The app is portable: it opens a dedicated Terminal window but does not copy the program elsewhere. When the tool exits, the native launcher closes only that dedicated window. Running from source or from an existing terminal never closes the caller's terminal. Internally the bundle contains a self-contained CLI and a tiny native launcher so the bundle can use normal macOS code-signing and notarization workflows.

### Windows

Run the portable executable directly:

~~~text
ChatGPT Conversation Cleanup.exe
~~~

The executable does not install another copy under `%LOCALAPPDATA%`.

### Why portable?

Portable-first distribution avoids version drift between a source checkout and a second copied installation: the app or executable you open is the program you are running.

A one-file/one-icon utility is not inherently obsolete or suspicious. Trust comes from provenance and platform signing. Public consumer releases should therefore be code-signed, and macOS releases should be notarized. CI artifacts in this repository are **unsigned test builds**, not a substitute for signed consumer releases.

## Run from source

Python 3.11+ is required only when running from source.
No Zstandard decompression library is needed at runtime: Cleanup leaves
compressed archive files and all physical deletion to the official Codex
engine, and its local doctor checks file presence only. Some optional
synthetic tests use Python 3.14's built-in decoder or `zstandard`.
The macOS source launcher selects an available supported interpreter instead
of using an outdated system `python3`.

macOS: double-click `ChatGPT Cleanup.command`, or run:

~~~sh
python3 run.py
~~~

Windows: double-click `ChatGPT Cleanup.cmd`, or run:

~~~powershell
py -3 .\run.py
~~~

Useful read-only commands:

~~~sh
python3 run.py doctor
python3 run.py doctor --all-profiles
python3 run.py doctor --all-profiles --json
python3 run.py list-archived
python3 run.py --version
~~~

The JSON doctor output intentionally excludes conversation titles, bodies, email addresses, local paths and authentication data so it can be attached to bug reports more safely.

Doctor also counts archived Codex rollout files found on disk, including
`*.jsonl.zst` compressed archives. These local storage counts do **not**
certify that the official Desktop archived-chats screen can load those chats.
An inaccessible archived list is not evidence that the local archive was deleted.

## Everyday flow

Interactive mode keeps the main menu narrow:

1. reconcile the local ChatGPT Desktop list, preselecting only strongly confirmed leftovers while still allowing explicit review of unverified local entries;
2. permanently delete selected archived Codex conversations through the official Codex app-server.

**More…** contains diagnostics, recovery, one-time removal of an older installed copy, and explicit purge of this project's recovery/local state.

Strongly confirmed leftovers are selected by default. Press Enter or `A` to continue with those confirmed entries, `R` to review every local ChatGPT Desktop entry, or `C` to cancel. Manual review accepts explicit numbers and ranges such as `1-8,11,14-18`; it never silently selects all unverified entries. Arrow-key input is ignored with a prompt instead of terminating the program.

If no leftovers are confirmed, possible leftovers (when any exist) are displayed by title as **preview only**; none is auto-selected. Press `R` for the full numbered local catalog and explicit manual selection. Protected entries remain unselectable.

For local catalog writes, **every runtime sharing the selected profile's Codex/Desktop data must be closed normally first**. This includes a separately managed Codex app-server (for example, one running inside a development runtime) even if the ChatGPT window is already closed. The blocker screen now identifies the process type and PID and records bounded, private, content-free diagnostics. Do not kill a shared app-server from a terminal: close its owning session through that product's supported controls.

After the **final local-cleanup confirmation**, a verified standalone official macOS ChatGPT app can receive **one normal quit request** through the standard macOS application controls. It may ask to save or finish open work; Cleanup never kills it. If it is still running, the blocker prompt offers `Q` for a normal-quit request (if not already attempted), or cancellation. A separately managed runtime is never quit this way. On completion or cancellation, a previously open, provably standalone official ChatGPT app is reopened only if it remains closed and its data is available. A session launched through an external profile manager must instead be reopened through that manager: a normal app launch cannot recreate its special routing or connection. The **restore step** never opens a profile that was closed at the start; an explicitly requested fresh reconciliation may separately open Desktop to run its scan.

Option 1 works from ChatGPT Desktop's local conversation catalog, not only from rows currently visible in the sidebar. A server-deleted branch or other stale local record can therefore be offered for cleanup even after the sidebar has already stopped showing it. Cleanup removes only that exact local catalog record; it does not delete a server conversation.

## How local ChatGPT cleanup is decided

Option 1 is a **reconciliation tool**, not a perfect ghost detector. The local ChatGPT catalog is the inventory. Evidence changes how each row is labeled and whether it is preselected; uncertainty no longer makes a row disappear from review.

Each local ChatGPT row is classified into one of these states:

- **Confirmed leftover** — Desktop recorded a structured deleted/not-found result, an optional live verifier conclusively failed to load the conversation twice, or a newly completed build-verified Desktop full reconciliation freshly marked the row missing. These are preselected for **local** cleanup.
- **Possible leftover** — Desktop's own `missing_candidate` flag is set, but there is not enough evidence to call the row deleted. These are visible in review but not preselected.
- **Not verified** — no reliable automatic signal is available. These rows are still visible in manual review instead of being silently hidden.
- **Protected — access issue** — Desktop reported `conversation_inaccessible`. Project/workspace routing can produce this for a live conversation, so the row cannot be selected by this tool.
- **Still available in ChatGPT** — an optional current-session verifier returned the conversation. The row is protected from cleanup.

This matters for the normal single-profile ChatGPT Desktop app: mainstream builds do not necessarily expose a localhost debugging endpoint. Live verification is therefore **optional supplemental evidence**, never a prerequisite for listing or reviewing local rows. Verified multi-profile runtimes that explicitly expose a profile-scoped localhost debugging endpoint can provide stronger evidence, but the core cleanup flow does not depend on that capability.

When the optional live provider is available, it routes each local row through the same current-session surface that matches that row instead of treating every conversation alike. Ordinary global chats use Desktop's `/conversations/batch` capability in sequential batches of at most 10. **Work** chats (`conversation_origin=tpp`) are verified against the paginated active Work inventory, and **Project** chats are verified against that project's paginated conversation inventory. An omitted row is checked a second time before it becomes confirmed local-cleanup evidence. A failure in one category stays `unverified` instead of poisoning unrelated categories, while HTTP 429 still abandons the entire live result immediately. The tool never launches ChatGPT with debugging flags just to gain this capability.

On a specifically refresh-verified Desktop build, stale or uncertain rows can instead be handed back to **ChatGPT Desktop's own catalog synchronizer**. The utility recommends this refresh by default (press `S` to skip), asks you to close every runtime that owns the selected profile's Desktop/Codex data root, clears only that profile host's full-refresh scheduling marker plus stale scan checkpoint/seen scratch state, and then automatically reopens the verified profile when possible. A genuinely isolated second profile may remain open. A developer runtime that reuses the selected profile's `--user-data-dir` or `CODEX_HOME` must close too, because it shares the same catalog. Desktop performs the authenticated history requests and owns every resulting catalog change. Cleanup then **waits automatically** and prints progress only when the sync stage changes (waiting, incremental sync, full reconciliation, completion), including current local/seen counts. There is no repeated "press Enter to check" loop. If more than one verified profile is selected, all eligible reconciliations are queued and launched before waiting, so their independent scans can overlap. A full reconciliation completed within the last 10 minutes is reused instead of immediately repeating another expensive full scan. The utility trusts a new result only after it observes a newer completed full reconciliation and a newer catalog observation sequence. A historical `missing_candidate=1` is never upgraded merely because some later full scan completed; the row itself must have been freshly advanced by that reconciliation.

Structured Desktop logs are another optional evidence source. Only definite deleted/not-found codes can preselect a row. `missing_candidate` is treated as a suspicion signal, not truth. `conversation_inaccessible` is a protection signal, not deletion evidence.

The tool does **not** match the visible `Could not load...` message, scrape conversation HTML, extract browser credentials, or parse authentication tokens. Manual cleanup is intentionally local-only: removing an unverified row changes this computer's Desktop catalog and creates an undo point; it does not issue a server-side ChatGPT delete request.

## Cleanup sequence

Local-list cleanup follows this order:

1. discover verified profiles;
2. scan each selected profile independently;
3. collect structured evidence and optional live evidence when available;
4. classify every local ChatGPT row instead of hiding unverified rows;
5. when supported and useful, first queue a fresh Desktop-owned full reconciliation for stale or uncertain rows, automatically launch the verified profile, monitor progress, and re-read the catalog only after that full pass is proven complete; recently completed full scans are reused and multiple independent profiles are queued before waiting so their scans can overlap;
6. preselect confirmed local leftovers or explicitly review/select local entries;
7. require a final explicit confirmation of the exact local-cleanup selection **before** changing app state;
8. when applicable, request normal quit once for a verified standalone macOS ChatGPT app; otherwise wait for its owner to close the affected profile normally;
9. re-read and compare the exact selected catalog rows inside the write path;
10. create a private recovery record;
11. apply the same authoritative-removal bookkeeping used by the current Desktop catalog: advance the host observation sequence, preserve a scan tombstone when a checkpoint exists, remove only the exact row, and advance the catalog revision when a visible row was removed.

The server is not queried a second time after selection. This avoids bursty duplicate reads and leaves the destructive safety check to the exact local-row comparison immediately before the write.

The tool never force-terminates ChatGPT or arbitrary background processes. The optional macOS normal-quit and restore path is confined to a verified standalone official app and the user's approved local-cleanup workflow. An unrelated application using the same data root must be closed through its own interface. Data-write guards still verify that the selected profile is stopped immediately before mutation.

Archived Codex deletion is a separate engine:

**You can leave ChatGPT Desktop open.** This operation starts a short-lived
official Codex app-server scoped to the selected profile and issues the same
`thread/delete` requests as the official app. Native per-thread writer and
lifecycle locks coordinate with other running Codex processes: a busy thread
or live internal worker is left intact rather than forcing its owner to quit.
An archived original still referenced by an active fork is likewise protected
by Codex's own history-reference checks.

1. select the exact profile and archived logical thread IDs from the local Codex inventory, including children omitted by older upstream list filters;
2. read the official app-server's **database-only** `thread/list` inventory and the small native `thread_spawn_edges` index; protect selected roots with active, unlisted or otherwise unresolved **spawned** descendants, since official deletion cascades to those descendants;
3. collapse selected spawned children under their eligible parent so **one official `thread/delete` request** owns the whole spawned subtree. Cleanup does not decode fork history or implement physical file deletion to decide the result;
4. keep independent archived threads eligible when another thread is protected. A confirmed native fork-history-reference refusal protects just that thread; an explicit native live-writer refusal also leaves its owner running. One bounded follow-up is allowed **only for history-reference refusals** and only if other deletions succeeded and may have removed the reference. Unknown errors and uncertain transport outcomes stop immediately and are **never** automatically retried;
5. after each successful native response, verify that the expected logical threads, their native rollout versions (including compressed siblings), history projection entries, and spawn links are gone. This is **read-only auditing, not local repair**. If native deletion leaves residue, stop and report rather than manipulating OpenAI data directly.

Deletion confirmation shows the maximum affected-thread count, the fewer official requests needed when subagents will cascade, and any preflight-protected roots. An active **user fork** may still reference the bytes of a protected archived original; only Codex's own reference scanner decides whether that original can be deleted. Forks are never silently deleted just because they share a parent with a selected thread.

**The two menu actions deliberately differ:** the local ChatGPT catalog
reconciliation (option 1) still requires the relevant Desktop/profile writers
to close before its verified local database changes. The official archived
Codex deletion (option 2) **does not close, quit, relaunch, or require closing**
ChatGPT Desktop or separate profile runtimes. Upstream lock/conflict refusals
are reported for those particular threads. An already-open Desktop UI may
need a normal in-app refresh to update its cached archived list.

## Multi-profile behavior

One profile: no profile selector is shown.

Several verified profiles: one operation can target multiple profiles. Each profile is still previewed, revalidated and finally applied sequentially, but eligible Desktop-owned reconciliations are queued/launched before the wait phase so independent profile scans can run concurrently. Arbitrary `.codex-*` directories are not trusted as profiles merely because they exist. Fresh Desktop-owned reconciliation is enabled only for provider/build combinations whose launch, data-root ownership and full-sync behavior have been verified. The normal official profile and the current Plura isolated-profile runtime are verified independently; unknown/legacy providers keep the existing evidence/manual-review flow.

## Safety boundary

- A ChatGPT local catalog entry, a standalone/historical `missing_candidate` flag, `conversation_inaccessible` result, or Native-state absence is not proof that the server conversation was deleted. Uncertain rows require explicit manual review. Fresh Desktop reconciliation evidence is accepted only after a newer full pass completes, and only a row whose own observation sequence advanced while being marked missing can gain automatic local-cleanup confidence; `conversation_inaccessible` remains protected.
- Native Codex history/lineage, authentication and provider tables are never rewritten directly. Archived Codex deletion delegates **all deletion and file/index cleanup** to official app-server `thread/delete`; the utility only selects, guards active spawned descendants, isolates confirmed refusals and audits native results. Log traces, cloud-side retention and files generated in project worktrees are outside the local thread-delete guarantee.
- Direct catalog mutation requires an exact known schema and a platform/build write gate.
- Preview changes, profile/account mismatch, unknown schemas/builds, symlinks, recovery conflicts and ambiguous request/command results stop the operation.
- **Only direct Desktop catalog writes and refresh scheduling require the affected profile to be closed.** Native archived deletion uses official per-thread writer locks and permits the affected Desktop to remain open. An approved local catalog cleanup can request a normal quit of a verified standalone official macOS app, but never forces external runtimes to stop. A supported fresh reconciliation similarly requires a brief stop while only its scheduling/scratch state is reset, then launches the verified Desktop where supported so Desktop itself performs the authenticated network reconciliation. For archived deletion, the utility starts its own short-lived official Codex app-server over stdio, scoped by `CODEX_HOME`, and releases it by closing stdio when the operation ends. If Codex refuses a busy or referenced thread, Cleanup does not bypass the protection.
- Recovery files may contain conversation metadata and are stored privately outside the repository.
- Automated tests use synthetic homes only. Separately authorized, explicitly bounded manual live acceptance experiments may exercise real profiles; they are never run by the automated suite, and no further existing personal conversations are eligible without fresh user approval.

## Remove the program

Because the program is portable, deleting `ChatGPT Conversation Cleanup.app` or `ChatGPT Conversation Cleanup.exe` removes the program itself.

Recovery data is intentionally separate so deleting the executable does not silently destroy undo records.

### Remove an older copied installation

If this tool previously created a separate copied program/launcher on this computer, choose:

~~~text
More… → Remove an old installed copy
~~~

or, from a source checkout / installed console entry point, run:

~~~sh
python3 run.py retire-install
# or: chatgpt-cleanup retire-install
~~~

This removes only the old program copy/launcher. Recovery data is preserved.

### Full project-data purge

To also delete ChatGPT Conversation Cleanup recovery/local state, choose:

~~~text
More… → Remove this app's local data
~~~

or, from a source checkout / installed console entry point, run:

~~~sh
python3 run.py purge-data
# or: chatgpt-cleanup purge-data
~~~

The command requires the exact confirmation phrase `PURGE CLEANUP DATA`. It removes only this project's data namespace; it does not remove ChatGPT or Codex conversation data.

## Build portable releases

PyInstaller is required only at build time, not at runtime:

~~~sh
python -m pip install pyinstaller
python scripts/build-portable.py
~~~

On macOS the build creates a portable `ChatGPT Conversation Cleanup.app.zip` plus SHA-256 checksum. The `.app` contains a self-contained CLI and a native Mach-O launcher. The launcher creates a dedicated Terminal window, waits for the CLI to finish, and closes only that window. On Windows the build creates one `ChatGPT Conversation Cleanup.exe` plus checksum.

`CCC_CODESIGN_IDENTITY` can be supplied to the macOS build for Developer ID signing. A consumer release should add the corresponding notarization step. The GitHub Actions portable-build workflow deliberately labels its outputs as unsigned test artifacts.

## Development

Design rationale and the recent-project comparison behind the current split architecture are documented in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and [`docs/RECENT_RESEARCH.md`](docs/RECENT_RESEARCH.md).

~~~sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m py_compile run.py scripts/build-portable.py src/chatgpt_cleanup/*.py
zsh -n 'ChatGPT Cleanup.command'
~~~

The current macOS direct-write gate remains intentionally narrow. New ChatGPT builds and Windows write support require evidence and fixture validation before the gate is extended.

## License

MIT
