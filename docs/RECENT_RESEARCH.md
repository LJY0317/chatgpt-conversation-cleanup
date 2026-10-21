# Recent implementation research

Research date: 2026-09-28. The cutoff for this design pass was **2026-07-28**: only repositories pushed within the preceding two months and issue reports created or updated in that window were used. No source code was copied from these projects; they were used to compare behavior, failure modes and safety architecture.

## ChatGPT conversation management and Desktop ghosts

Recent repositories inspected:

- [`EvilIrving/chatgpt-bulk-delete`](https://github.com/EvilIrving/chatgpt-bulk-delete) — pushed 2026-09-14. Browser-side bulk management builds a current conversation inventory and applies mutations only to explicit user selections.
- [`Junyi-99/chatgpt-bulk-delete-export`](https://github.com/Junyi-99/chatgpt-bulk-delete-export) — pushed 2026-08-06. The extension documents an in-browser, current-session inventory/selection model and avoids a separate backend.
- [`Dworrall21/chatgpt-bridge`](https://github.com/Dworrall21/chatgpt-bridge) — pushed 2026-08-01. The bridge exposes conversation-management operations from the current ChatGPT session rather than deriving state from an unrelated local cache.
- [`jczl0813/chatgpt-conversation-manager`](https://github.com/jczl0813/chatgpt-conversation-manager) — pushed 2026-07-28. Recent conversation-management work was reviewed for selection/inventory behavior.
- [`Ricardo-Ping/chatgpt-codex-conversation-manager`](https://github.com/Ricardo-Ping/chatgpt-codex-conversation-manager) — pushed 2026-09-26. Its ChatGPT bridge and Codex adapter keep the two surfaces distinct and use current-session/official-runtime operations.

Recent OpenAI Codex issue reports were also important. [`openai/codex#41987`](https://github.com/openai/codex/issues/41987), [`#42628`](https://github.com/openai/codex/issues/42628), [`#44657`](https://github.com/openai/codex/issues/44657), [`#47517`](https://github.com/openai/codex/issues/47517), and [`#42768`](https://github.com/openai/codex/issues/42768) document Desktop sidebar ghosts where stale `local_thread_catalog` rows can outlive their source conversations. Reports show that `missing_candidate` is not a reliable ghost detector: verified ghosts can have `missing_candidate=0`, while other rows can have the flag without being the visible problem. Targeted local catalog reconciliation, with backup and the Desktop catalog's bookkeeping, successfully repaired individual ghosts in several reports.

### Resulting design

Option 1 therefore does not pretend that one automatic signal can perfectly identify every ghost. The local Desktop catalog is the inventory. Strong evidence can preselect a row, but weak/absent evidence becomes an explicit review state rather than causing the row to disappear from the utility. Local cleanup never becomes a server-side ChatGPT delete operation.

The normal single-profile Desktop app does not necessarily expose a safe localhost debugging endpoint, so the live provider remains optional. Follow-up inspection of the current verified Desktop build found a safer first-party route for uncertain rows: ask Desktop to perform another full catalog reconciliation itself rather than extracting credentials or building a parallel authenticated client.

Static inspection showed that incremental and full catalog scans have materially different cleanup semantics. A completed full scan can mark recent unseen rows missing, while an incremental scan primarily advances history state. It also showed that Desktop schedules a full scan when `last_full_reconciled_at` is absent. Most importantly, Desktop's own current migration for a ChatGPT catalog backfill clears both that marker **and the host's stale scan checkpoint**. A live experiment confirmed why the second part matters: clearing only the full-scan marker could leave an old incremental checkpoint in front of the requested full pass; clearing the host checkpoint caused Desktop to start a new scan attempt with a new observation sequence.

The production design therefore mirrors that host-scoped scheduling contract while Desktop is stopped: clear the full-refresh marker plus transient checkpoint/seen scratch state, but never edit conversation rows to manufacture evidence. After the user reopens Desktop, the utility waits for a genuinely newer completed full reconciliation. Fresh missing evidence is accepted only for rows whose own observation sequence advanced after the request; historical missing flags remain uncertain. This keeps the authenticated network operation and catalog ownership inside Desktop and avoids persisting or replaying session credentials.

A later real-session validation exposed a second source of false uncertainty: `/conversations/batch` is appropriate for ordinary global chats, but Work (`conversation_origin=tpp`) and Project (`g-p-*`) rows have separate inventory surfaces. Mixing those rows into one batch probe could fail closed for the entire profile. The current verifier now uses the active `tpp` history inventory for Work chats and each project's `/gizmos/{gizmo_id}/conversations` inventory for Project chats, while keeping ordinary chats on the batch route. Each omission is rechecked, category failures are isolated, and 429 still cancels all partial live evidence.

The same Desktop-owned full reconciliation was then exercised end-to-end on the isolated Plura Profile 2 runtime using its own `~/.codex-profile2` catalog and `Codex-Profile2` user-data directory. Desktop reduced the local ChatGPT catalog from 34 rows to 14 without direct row deletion by the cleanup utility; a subsequent category-aware current-session check reported all 14 remaining rows present. This validates the Plura provider/build combination for the same repair-first flow, while keeping the gate tied to the verified runtime manifest and ChatGPT build.

The UX was subsequently changed so that this slower first-party repair path no longer requires repeated manual polling. Verified profiles are reopened automatically, checkpoint state is monitored for incremental/full progress, and multiple selected isolated profiles are queued before waiting so their scans can overlap. A 10-minute freshness window prevents an immediately repeated full-history scan after one has just completed; direct evidence/manual review remains available during that window instead of paying the full cost again.

## Archived Codex deletion

Recent repositories inspected:

- [`Ricardo-Ping/chatgpt-codex-conversation-manager`](https://github.com/Ricardo-Ping/chatgpt-codex-conversation-manager) — pushed 2026-09-26. Uses official `thread/list`/`thread/delete` and previews dependent thread closure from parent metadata.
- [`kuaichu/CodexConversationManager`](https://github.com/kuaichu/CodexConversationManager) — pushed 2026-09-19. Recent Codex conversation-management implementation was reviewed for archived-thread handling.
- [`Zero-Kq/vscode-codex-chat-manager`](https://github.com/Zero-Kq/vscode-codex-chat-manager) — pushed 2026-09-05. Recent manager implementation was reviewed for archive/delete workflow separation.
- [`HikiTanis/codex-conversation-manager`](https://github.com/HikiTanis/codex-conversation-manager) — pushed 2026-09-01. Uses the official app-server deletion path and separates local backup/index handling from authoritative deletion.
- [`nghianguyen89/codex-conversation-manager`](https://github.com/nghianguyen89/codex-conversation-manager) — pushed 2026-08-25. Recent local Codex management behavior was compared.
- [`shubing-lab/codex-vault`](https://github.com/shubing-lab/codex-vault) — pushed 2026-08-18. Its deletion engine emphasizes preview, backup/journal/staging and post-delete verification.

Recent official issue [`openai/codex#43106`](https://github.com/openai/codex/issues/43106) documents an archived fork omitted from a normal archived listing while still retaining a dependency on its parent; deleting the fork first and then the parent through official `thread/delete` succeeded. [`openai/codex#40365`](https://github.com/openai/codex/issues/40365) documents the corresponding active-fork protection behavior. [`#46182`](https://github.com/openai/codex/issues/46182) and [`#46240`](https://github.com/openai/codex/issues/46240) provide additional recent archived-delete UX/failure context.

### Resulting design

Option 2 was later simplified to **delegate the entire hard-delete and spawned
subagent cascade** to official app-server `thread/delete`, rather than
reimplementing fork/rollout dependency ordering. The tool only checks for active
or unknown spawned descendants, collapses selected spawned families to one
native call, isolates confirmed fork-reference refusals so other archived
conversations can proceed, and verifies the resulting native storage state.
Timeouts or ambiguous errors do not receive automatic retries. The official
contract is documented in
[`codex-rs/app-server/README.md`](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md),
and the native reference rejection is implemented in
[`thread-store/src/local/delete_thread.rs`](https://github.com/openai/codex/blob/main/codex-rs/thread-store/src/local/delete_thread.rs).
