# Architecture

ChatGPT Conversation Cleanup contains two intentionally separate engines. They share profile discovery, locking, process guards and UI primitives, but they do **not** share a destructive decision model.

## 1. ChatGPT Desktop reconciliation

This engine repairs the local ChatGPT Desktop conversation catalog. It does not delete server-side ChatGPT conversations.

### Inventory first

`local_thread_catalog` is treated as the local inventory. Every ChatGPT-hosted row remains visible to the reconciliation model even when no automatic verifier is available.

Each row receives an evidence status:

- `confirmed`: strong structured deleted/not-found evidence, an optional live provider that conclusively cannot load the conversation twice, or a freshly advanced missing row from a newly completed build-verified Desktop full reconciliation;
- `suspected`: Desktop's own `missing_candidate` signal without stronger proof;
- `unknown`: no reliable automatic signal;
- `protected`: a structured access/routing failure such as `conversation_inaccessible`;
- `present`: an optional live provider returned the conversation.

Only `confirmed` rows are preselected. `suspected` and `unknown` rows can be selected only from explicit manual review. `protected` and `present` rows cannot be selected.

This makes the normal one-profile Desktop case independent of a debugging endpoint. A verified localhost CDP endpoint is an optional evidence provider, not a core requirement.

### Category-aware current-session verification

The ChatGPT catalog contains several server-side conversation surfaces that cannot safely share one presence probe. The inspected Desktop build stores enough routing metadata on each row to keep them separate:

- ordinary global chats (`project_id IS NULL`, non-`tpp`) use `POST /conversations/batch`, in the same maximum batch size of 10 used by Desktop;
- Work chats (`conversation_origin='tpp'`) use the paginated active `/conversations` inventory filtered to `tpp`;
- Project chats use the paginated `/gizmos/{gizmo_id}/conversations` inventory for their exact `g-p-*` project.

This separation is required, not cosmetic. Live validation showed that feeding Work or Project IDs through the ordinary batch route can fail the whole mixed request even though the ordinary global IDs are valid. The verifier therefore classifies categories independently, paces pages/requests, and rechecks an omission before accepting it as local-cleanup evidence. A non-rate-limit failure makes only the affected category `unverified`; a 429 discards all partial live evidence. `conversation_inaccessible` from structured Desktop evidence remains a protection veto.

### Desktop-owned fresh reconciliation

The inspected Desktop runtime has separate incremental and full catalog scans. Incremental sync can advance history state without marking every unseen pre-existing row missing. A completed full scan can mark an unseen recent row `missing_candidate=1` and records `last_full_reconciled_at`. This distinction matters for ghosts that were created after the previous full scan and then disappeared while a delete/update notification was missed.

For a refresh-verified official build, the cleanup utility can queue one fresh full scan without taking over Desktop's authenticated network path. While that selected profile's Desktop process is stopped it:

1. verifies the exact catalog schema, app build and a single `chatgpt:<account>:<user>` host for the signed-in workspace;
2. sets only that host's `last_full_reconciled_at` to `NULL`;
3. deletes only that host's transient scan checkpoint and seen-entry scratch rows;
4. leaves every conversation row untouched.

Clearing the stale checkpoint is essential. Desktop can otherwise resume an older incremental attempt before honoring the full-refresh marker. The host-scoped reset mirrors Desktop's own migration strategy for scheduling ChatGPT catalog backfills.

The utility automatically launches a verified profile after the refresh is queued when the provider supports it, then polls the catalog instead of asking the user to repeatedly press Enter. Progress is derived from Desktop's own checkpoint/sync state and reported only when observable state changes: waiting, incremental scan, between scans, full reconciliation and completion, plus the current seen/local/missing-candidate counts. Waiting has bounded idle and total timeouts; timeout never manufactures evidence and Desktop may keep finishing the queued scan independently.

Desktop may still finish a newly started incremental pass first. The utility therefore never treats "refresh requested" as evidence. It waits until it observes both a newer observation sequence and a `last_full_reconciled_at` timestamp newer than the request. Only then does it re-read the catalog. Existing visible rows are protective `present` evidence. A missing row is promoted to automatic local-cleanup evidence only when its own `observation_sequence` advanced past the pre-refresh sequence while it was marked missing. A historical missing flag that was not touched by this reconciliation remains only `suspected`. Structured `conversation_inaccessible` remains a veto even if the fresh list omits that row.

To avoid paying the same full-history cost repeatedly, a full reconciliation completed less than 10 minutes ago is reused rather than immediately scheduling another one. When several eligible isolated profiles are selected, their refreshes are queued and launched before the wait phase; the scans can therefore run in parallel even though final review/mutation remains profile-sequential.

This path is provider/build gated. It is enabled for the verified official-profile lifecycle and for the current verified Plura isolated-profile lifecycle. Other multi-profile providers remain on the existing evidence/manual-review path until their launch, process ownership and catalog-refresh contracts are separately verified.

The stop guard used by refresh and local catalog writes is profile-scoped on macOS. A verified alternate runtime with its own `--user-data-dir`/`CODEX_HOME` and separate catalog may stay open while the official profile is queued. An external runtime pointing to the selected profile's Desktop or Codex data is treated as a co-owner and must stop normally. The direct write path rechecks ownership before and within the SQLite transaction.

### Local-only mutation

After the user closes ChatGPT Desktop and confirms the exact selection, the write path:

1. re-reads and byte-compares the selected catalog rows;
2. creates a private recovery record before mutation;
3. increments the host observation sequence;
4. preserves a `removed=1` scan tombstone when a scan checkpoint exists;
5. removes only the exact `(host_id, thread_id)` row;
6. advances catalog revision only when a visible row was removed;
7. runs SQLite `quick_check` before commit.

These bookkeeping steps mirror the current Desktop catalog's authoritative-removal semantics instead of treating the database as an arbitrary cache.

## 2. Archived Codex permanent deletion

This engine fixes the usability failure where one undeletable archived conversation
can stop the official application's entire bulk Delete. **It is an orchestration
layer around Codex's native deletion engine, not another deletion engine.**

### Source of truth and deliberately small preview

- The local `threads` table supplies the selected logical-thread IDs, including
  empty-preview archived tasks that some upstream lists omit.
- An official `thread/list` in database-only mode supplies an independent
  positive veto when Codex reports a thread active, without requesting a
  scan-and-repair of the Native profile.
- The existing `thread_spawn_edges` index and the official `parentThreadId`
  metadata are used **only** to identify the native API's spawned-child cascade
  and protect active or unknown spawned descendants. This deliberately does
  not reimplement fork lineage, paginated `history_base` interpretation,
  archive-file compression, nor a custom physical delete routine.
- Selecting a parent and its subagents produces **one** official delete call
  for that eligible parent; Codex owns cascade traversal and removal of
  associated rollouts/metadata. Independently selected children of a protected
  parent can still be submitted if they themselves have no active spawn.
- A fork is a separate user thread, not an automatically spawned subagent.
  A live fork may depend on an archived original's byte history, and only
  Codex's native reference protection can decide whether the original is
  deletable. No attempt is made to detach or rewrite its history.

### Native execution and failure isolation

After the owner's final destructive confirmation, the exact local and native
**selected threads and spawned descendants** must still match the preview.
Unrelated active chats may advance while Desktop is open without invalidating
the exact archived selection. A short-lived, official, profile-bound app-server
performs every irreversible `thread/delete` **while ChatGPT is allowed to
remain open**. Codex itself coordinates across processes through per-thread
writer/lifecycle locks; attempting to delete a loaded writer or live internal
worker yields a native ownership refusal, not a license to force-stop it.
Neither the application nor external managers are stopped by this engine.
There is no direct external IPC connection to or patching of the Desktop app.

A **definitive** native `-32600` fork-history-reference refusal is reported for
that thread while other independently selected archived roots continue. If
another selected archived fork was then successfully removed, a single bounded
second pass may attempt the previously refused root again. No other error
is automatically retried: server timeouts, transport loss, unknown refusals,
schema conflicts and partial/ambiguous outcomes stop immediately.
An explicit native live-writer/internal-worker ownership refusal is instead
reported as a protected thread, **not retried**. A newly discovered spawned
descendant or change to selected thread metadata stops the operation. Unrelated
new/updated Desktop threads do not prevent an otherwise safe archive delete.

Cleanup checks the affected logical rows and spawn index after each success,
and conducts **read-only** checks for remaining native rollout/version files
and `thread_history_1.sqlite` projections. If residue remains after native
success, it is reported as an upstream/incomplete-delete problem and the batch
stops. Cleanup never touches the native SQLite rows, `.jsonl`, `.zst`, log
traces or any app resources to fix it. The native contract does not promise
to remove generated workspace files, OS backups or all remote log retention.

The resulting user experience is **native-first, fail-closed, and version
resilient**: small safety assertions and observable results, but no mirrored
file-by-file deletion algorithm.

## Shared safety rules

- Single-profile is the default UX; extra profiles require verified providers.
- Unknown schemas, builds, identities, symlinks or concurrent changes fail closed.
- The common cleanup engine does not depend on external profile managers. On macOS only, the platform adapter may request a normal quit of a provably standalone official ChatGPT app **after explicit user approval**; it never force-quits an app or a third-party-owned process. It can restore only the originally running standalone Desktop after checking that it is still safe to open. Unverified/managed sessions are left to their owner.
- Local catalog cleanup presents the final selected-row confirmation before any optional normal-quit request. Cleanup still refuses a write while the affected profile's Desktop or app-server is active.
- Archived Codex deletion deliberately never calls that local-catalog stop guard or Desktop restore/quit mechanism. The official app-server holds the deletion rules and thread writer-lock arbitration, while Cleanup checks exact selection and aftermath only.
- Recovery data is private local state and is excluded from Git.
- The automated suite uses synthetic homes only. A separate, expressly user-authorized one-per-profile manual acceptance test confirmed native deletion while both signed Desktop apps remained open; no additional pre-existing conversations may be used for destructive experiments without new authorization.
