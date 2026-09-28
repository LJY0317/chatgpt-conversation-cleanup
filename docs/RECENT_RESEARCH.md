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

The normal single-profile Desktop app does not necessarily expose a safe localhost debugging endpoint, so the live provider is optional. Fully automatic server-vs-local reconciliation for that case would require another authenticated current-session provider (for example, an explicit browser/extension bridge); the tool does not extract credentials or weaken Desktop security just to manufacture one.

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

Option 2 plans dependencies before any irreversible call. Local state DB rows, rollout lineage and official app-server parent/fork metadata are combined; archived dependents are included child-first, while active or unresolved dependencies stop the operation. The irreversible operation itself is delegated only to official app-server `thread/delete`, and each result is verified before proceeding.
