# ChatGPT Conversation Cleanup

An unofficial terminal utility for two deliberately separate jobs:

1. reconcile and remove stale **local ChatGPT Desktop list entries** without deleting server-side ChatGPT conversations; and
2. permanently delete selected **archived Codex conversations** through the official Codex app-server, including dependent archived forks when required.

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

## Everyday flow

Interactive mode keeps the main menu narrow:

1. reconcile the local ChatGPT Desktop list, preselecting only strongly confirmed leftovers while still allowing explicit review of unverified local entries;
2. permanently delete selected archived Codex conversations through the official Codex app-server.

**More…** contains diagnostics, recovery, one-time removal of an older installed copy, and explicit purge of this project's recovery/local state.

Strongly confirmed leftovers are selected by default. Press Enter or `A` to continue with those confirmed entries, `R` to review every local ChatGPT Desktop entry, or `C` to cancel. Manual review accepts explicit numbers and ranges such as `1-8,11,14-18`; it never silently selects all unverified entries. Arrow-key input is ignored with a prompt instead of terminating the program.

Option 1 works from ChatGPT Desktop's local conversation catalog, not only from rows currently visible in the sidebar. A server-deleted branch or other stale local record can therefore be offered for cleanup even after the sidebar has already stopped showing it. Cleanup removes only that exact local catalog record; it does not delete a server conversation.

## How local ChatGPT cleanup is decided

Option 1 is a **reconciliation tool**, not a perfect ghost detector. The local ChatGPT catalog is the inventory. Evidence changes how each row is labeled and whether it is preselected; uncertainty no longer makes a row disappear from review.

Each local ChatGPT row is classified into one of these states:

- **Confirmed leftover** — Desktop recorded a structured deleted/not-found result, or an optional live verifier conclusively failed to load the conversation twice. These are preselected.
- **Possible leftover** — Desktop's own `missing_candidate` flag is set, but there is not enough evidence to call the row deleted. These are visible in review but not preselected.
- **Not verified** — no reliable automatic signal is available. These rows are still visible in manual review instead of being silently hidden.
- **Protected — access issue** — Desktop reported `conversation_inaccessible`. Project/workspace routing can produce this for a live conversation, so the row cannot be selected by this tool.
- **Still available in ChatGPT** — an optional current-session verifier returned the conversation. The row is protected from cleanup.

This matters for the normal single-profile ChatGPT Desktop app: mainstream builds do not necessarily expose a localhost debugging endpoint. Live verification is therefore **optional supplemental evidence**, never a prerequisite for listing or reviewing local rows. Verified multi-profile runtimes that explicitly expose a profile-scoped localhost debugging endpoint can provide stronger evidence, but the core cleanup flow does not depend on that capability.

When the optional live provider is available, it uses the running Desktop renderer's own `/conversations/batch` capability. IDs are checked in the same maximum batch size used by Desktop, batches are sent sequentially with pacing, only omitted IDs are checked a second time, and HTTP 429 abandons the live result immediately. The tool never launches ChatGPT with debugging flags just to gain this capability.

Structured Desktop logs are another optional evidence source. Only definite deleted/not-found codes can preselect a row. `missing_candidate` is treated as a suspicion signal, not truth. `conversation_inaccessible` is a protection signal, not deletion evidence.

The tool does **not** match the visible `Could not load...` message, scrape conversation HTML, extract browser credentials, or parse authentication tokens. Manual cleanup is intentionally local-only: removing an unverified row changes this computer's Desktop catalog and creates an undo point; it does not issue a server-side ChatGPT delete request.

## Cleanup sequence

Local-list cleanup follows this order:

1. discover verified profiles;
2. scan each selected profile independently;
3. collect structured evidence and optional live evidence when available;
4. classify every local ChatGPT row instead of hiding unverified rows;
5. preselect confirmed leftovers or explicitly review/select local entries;
6. ask the user to quit ChatGPT normally;
7. require a final explicit confirmation;
8. re-read and compare the exact selected catalog rows inside the write path;
9. create a private recovery record;
10. apply the same authoritative-removal bookkeeping used by the current Desktop catalog: advance the host observation sequence, preserve a scan tombstone when a checkpoint exists, remove only the exact row, and advance the catalog revision when a visible row was removed.

The server is not queried a second time after selection. This avoids bursty duplicate reads and leaves the destructive safety check to the exact local-row comparison immediately before the write.

The tool never terminates or restarts ChatGPT itself.

Archived Codex deletion is a separate engine:

1. snapshot the selected archived rows from the local Codex state database;
2. ask the official Codex app-server for active and archived thread inventory, including derived thread kinds;
3. combine app-server `parentThreadId` / `forkedFromId` metadata with local rollout `session_meta` lineage;
4. expand the plan to include dependent **archived** forks/children;
5. stop before deletion if an active child/fork still depends on a selected parent;
6. delete in child-before-parent order through official `thread/delete`;
7. re-read local state after every deletion and stop on the first ambiguous result, reporting how many planned chats were already verified deleted. Failed/ambiguous deletes are never retried automatically.

## Multi-profile behavior

One profile: no profile selector is shown.

Several verified profiles: one operation can target multiple profiles. Each profile is still previewed, revalidated and applied sequentially. Arbitrary `.codex-*` directories are not trusted as profiles merely because they exist.

## Safety boundary

- A ChatGPT local catalog entry, `missing_candidate` flag, `conversation_inaccessible` result, or Native-state absence is not proof that the server conversation was deleted. Uncertain rows require explicit manual review.
- Native Codex history/lineage, authentication and provider tables are never rewritten directly. Archived Codex deletion delegates the irreversible operation to official app-server `thread/delete` and uses local state only for preview, dependency planning and verification.
- Direct catalog mutation requires an exact known schema and a platform/build write gate.
- Preview changes, profile/account mismatch, unknown schemas/builds, symlinks, recovery conflicts and ambiguous request/command results stop the operation.
- ChatGPT/Codex Desktop must be closed normally before destructive operations. The optional live read-only scan may run while ChatGPT is open; the tool then waits for the user to close it. For archived deletion, the utility starts its own short-lived official Codex app-server over stdio after Desktop is closed and releases it by closing stdio when the operation ends.
- Recovery files may contain conversation metadata and are stored privately outside the repository.
- Tests use synthetic homes only. Real user conversations are not deleted for testing.

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
