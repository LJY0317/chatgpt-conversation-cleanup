# Architecture

ChatGPT Conversation Cleanup contains two intentionally separate engines. They share profile discovery, locking, process guards and UI primitives, but they do **not** share a destructive decision model.

## 1. ChatGPT Desktop reconciliation

This engine repairs the local ChatGPT Desktop conversation catalog. It does not delete server-side ChatGPT conversations.

### Inventory first

`local_thread_catalog` is treated as the local inventory. Every ChatGPT-hosted row remains visible to the reconciliation model even when no automatic verifier is available.

Each row receives an evidence status:

- `confirmed`: strong structured deleted/not-found evidence, or an optional live provider that conclusively cannot load the conversation twice;
- `suspected`: Desktop's own `missing_candidate` signal without stronger proof;
- `unknown`: no reliable automatic signal;
- `protected`: a structured access/routing failure such as `conversation_inaccessible`;
- `present`: an optional live provider returned the conversation.

Only `confirmed` rows are preselected. `suspected` and `unknown` rows can be selected only from explicit manual review. `protected` and `present` rows cannot be selected.

This makes the normal one-profile Desktop case independent of a debugging endpoint. A verified localhost CDP endpoint is an optional evidence provider, not a core requirement.

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

This engine performs an irreversible server/official-runtime operation and therefore uses a stricter plan/execute protocol.

### Inventory and dependency plan

The preview combines:

- all local rows in `state_5.sqlite`;
- official app-server `thread/list` results for active and archived threads, including derived source kinds;
- rollout `session_meta` lineage (`history_base.thread_id` / fork metadata);
- app-server `parentThreadId` and `forkedFromId` metadata.

The planner computes the descendant closure of the requested archived threads. Dependent archived children/forks are included automatically and ordered before their parents. An active dependent child, a dependency cycle, conflicting app-server state, or an app-server-only dependent missing from the local snapshot stops the operation before deletion.

### Official deletion only

Execution uses official app-server `thread/delete`; the utility does not rewrite Native Codex history/lineage tables directly. After the user's final confirmation and before the first irreversible call, the entire local/app-server dependency plan is rebuilt and must match the preview exactly.

Before every delete, the selected local row must still exactly match the preview snapshot. After every delete, the local state database must no longer contain the thread. The first app-server error, timeout, ambiguous result or local verification failure stops remaining work. Destructive calls are never retried automatically.

## Shared safety rules

- Single-profile is the default UX; extra profiles require verified providers.
- Unknown schemas, builds, identities, symlinks or concurrent changes fail closed.
- ChatGPT/Codex Desktop is never terminated or restarted by the utility.
- Recovery data is private local state and is excluded from Git.
- Tests use synthetic homes; destructive development tests never target real conversations.
