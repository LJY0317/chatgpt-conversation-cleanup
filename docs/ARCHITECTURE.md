# Architecture and remaining work

Two distinct user problems, two **native-owned** operations. Source-only
`run.py` CLI, no installer or separate deletion service.

## 1. Normal Chat: deleted remotely, still visible locally

`repair-chat-list` delegates actual list reconciliation to the
original ChatGPT Desktop. The utility uses Desktop's
version-verified native sync scheduling contract, for one
authenticated ChatGPT host:

1. Read official Codex `account/read` routing identity and identify
   local Desktop hosts without using browser credentials.
2. Show a read-only preview. If multiple hosts match one account,
   require an explicitly selected **opaque host token**.
3. With owner confirmation, normally close only the relevant verified
   Desktop owner. Reset the native `last_full_reconciled_at` scheduling
   marker and stale scan-checkpoint/seen scratch state **for one host**.
4. Reopen the verified original Desktop, or ask the user to reopen
   externally managed runtimes through their own launcher.
5. **Desktop performs all authenticated network requests and all
   local catalog removals.** Cleanup only reads
   `observation_sequence` and completion metadata afterward.

The tool **never directly deletes a local chat catalog row** or a normal
Chat conversation on the server. It never persists auth tokens or
reimplements a remote conversation-presence scanner. Ambiguous hosts,
unverified builds and unknown process owners fail closed.

This marker reset is narrowly modeled on observed upstream Desktop
migrations; **it is not a stable public refresh API**. Support for future
Desktop versions must be verified rather than assumed. Scheduling
a refresh does not prove that the app performed it.

## 2. Work/Codex: archived conversation cannot be deleted

`delete-archived-codex` uses the official profile-bound Codex app-server
`thread/delete`. Cleanup holds an operation lock and checks exact
archived IDs, then delegates **all actual deletion** to the official API.
After native success it reads logical rows, rollout/version paths and
history/spawn indexes to detect incomplete cleanup.

The ChatGPT app may **stay open**: per-thread writer/lifecycle locks
and fork-history references are owned by Codex. An active fork can
protect an archived original. A narrowly bounded second attempt is
allowed only if another native deletion demonstrably removed a reference.
Ambiguous RPC errors stop. Cleanup never rewrites fork ancestry or
deletes original SQLite/rollout data directly.

## Agent routing rules

“Server-deleted normal Chat still visible in Desktop” means **list
repair**; “Work/Codex archived thread Delete fails” means **native archive
deletion**. Generic “채팅 삭제해 줘” is ambiguous and requires clarification.
“안 지워지는 채팅” without explicit archive/Work/Codex context defaults
to a **read-only investigation of normal Chat's stale-list issue**,
not a native archive deletion. Confirm remote deletion and exact
profile/target before any Refresh. **Never silently fall back** between
the two operations. If an unarchived normal Chat
still exists on the server, refreshing cannot delete it.

## Incomplete work — intentionally retained

- Two Profile 1 archived originals remain protected by active forks
  with `history_base` references. Their removal without losing current
  active histories needs an upstream-supported detach/flatten feature.
  Do not force-delete them or rewrite their fork history.
- Native deletion may leave diagnostic logs, attachments or caches
  under separate retention policies. These are **not the same as orphan
  conversation rollout files**. Their ownership and retention remain to
  be established; do not bulk-remove them.
- Desktop full refresh depends on the current host/build and lifecycle.
  The installed signed app's marker, migration and scheduling contract
  were inspected statically; end-to-end verification remains specific
  to the targeted host and app version.

Original ChatGPT/Plura/Tela apps and user profiles must not be modified
by a source-only tool refactor. Old local recovery records are retained.
