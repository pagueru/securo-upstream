# Pagueru fork: local changes and how they are carried

This fork (`pagueru/securo-upstream`) is what the operator's self-hosted instance runs for the
backend image. The frontend comes from the official image by digest. Everything here is a local
change that upstream does not have yet; each one has a drop criterion, so the fork trends back to
the official code.

The update procedure lives outside this repository, in the operator's runbook
`atualizar-securo` (Context Ledger project `productivity`). This file is the inventory that
procedure reads before deciding what to carry.

## Local changes

### 1. Per-workspace Pluggy credentials

- **Status:** implemented and in production (`securo-backend:0.18.0-pagueru.1`, built from `deploy/pagueru` at `2f3c732a`, since 2026-11-07). Previous image: `0.17.0-pagueru.1`, tag `v0.17.0-pagueru.1` (`aa1fd9ea`).
- **Branch:** `feature/per-workspace-pluggy-credentials`, seven commits on top of `v0.17.0`; carried onto each release by cherry-pick into `deploy/pagueru` (not rebased, because it is published and linked from issue #1351).
- **What it does:** `PLUGGY_WORKSPACE_CREDENTIALS_FILE` points to a JSON mapping workspace UUIDs to
  `client_id`/`client_secret`. A workspace without an entry uses the instance credentials. A missing
  or malformed file stops startup instead of falling back. The Pluggy API key is cached per
  `client_id`. Connect, reconnect, institutions and background sync resolve the provider from the
  connection's workspace.
- **Files:** `backend/app/providers/pluggy_credentials.py`,
  `backend/app/providers/{__init__,base,pluggy}.py`, `backend/app/core/config.py`,
  `backend/app/api/connections.py`, `backend/app/services/connection_service.py` (seven
  `get_provider(...)` call sites gain `workspace_id=`), `backend/.env.example`, both compose files.
- **Migrations:** none.
- **Tests:** `backend/tests/test_pluggy_workspace_credentials.py`,
  `backend/tests/test_pluggy_workspace_routing.py`.
- **Drop criterion:** upstream ships per-workspace provider credentials (issue
  [#1351](https://github.com/securo-finance/securo/issues/1351), which the maintainer said must be
  built on PR [#1033](https://github.com/securo-finance/securo/pull/1033)). Dropping requires moving
  the two workspaces' values into the new official storage before removing the file; it is a data
  migration, not only a code deletion.

### 2. Goals with source allocations and shared goals

- **Status:** planned, not implemented. Plan and spec live in the Context Ledger (`productivity`).
- **Branches (planned):** `feature/goal-source-allocations`, then `feature/shared-goals`.
- **What it does:** a goal whose progress is the sum of fixed and percentage shares of accounts,
  assets and asset groups (fixed first, percentages over the remainder, never above the real value),
  and a goal shared between workspaces with an accepted invite.
- **Migrations:** a new table for source allocations and one for goal members. See the migration
  policy below.
- **Drop criterion:** upstream implements equivalent goal tracking (issue
  [#745](https://github.com/securo-finance/securo/issues/745)) or the PR is merged. Watch PR
  [#1060](https://github.com/securo-finance/securo/pull/1060) — it adds a `pocket` tracking type and
  a `goal_allocations` table for explicit account reservations, which is a different feature; the
  fork's table is named `goal_source_allocations` so the two can coexist.

## Migration policy: an independent Alembic branch

Fork migrations are an Alembic branch of their own, not a tip of the official chain:

```python
revision = "pagueru001"
down_revision = None
depends_on = "<official head at creation time>"
branch_labels = ("pagueru",)
```

File name must start with the revision id (the repository's `check_migration_chain.py` asserts the
prefix), so `pagueru001_goal_source_allocations.py`.

Why not a numeric revision on the official chain: every release that adds migrations forces either a
renumber or a `down_revision` update, and the updated chain **silently skips** the official
migration the database has not applied yet, because the fork revision is already the single head.
That was measured with Alembic 1.20.1: with `alembic_version = pagueru001` and a new official `108`,
`upgrade head` did nothing and left the schema without `108`, with no error, and the compose guard
(`alembic current --check-heads`) also passed.

With the branch above:

- The deploy runs `alembic upgrade heads`, never `upgrade head`. `upgrade head` fails loudly
  ("Multiple head revisions"), which is the failure mode we want.
- `alembic_version` holds two rows, the official head and the fork's revision.
- Reverting the fork's schema is `alembic downgrade pagueru@base`, which leaves the official schema
  intact.
- `backend/scripts/check_migration_chain.py` reports two heads by design. Run it only on a branch
  where the fork migration has already been renumbered into the official chain (the PR branch), or
  accept exactly two heads, one of them `pagueru*`, in the fork gate.

## Upstream work this fork depends on

| Upstream item | What it changes | What to do when it lands |
| --- | --- | --- |
| [#1033](https://github.com/securo-finance/securo/pull/1033) provider settings (open, not merged) | rewrites `providers/__init__.py`, touches `pluggy.py` and `connection_service.py`, adds `providers/credentials.py` and a migration | the conflict with item 1 is real and semantic; rebase item 1 on it and design the per-workspace PR on top |
| [#1351](https://github.com/securo-finance/securo/issues/1351) per-workspace provider credentials | the feature item 1 already runs locally, with a different design (encrypted storage, write-only, admin-managed) | see item 1's drop criterion |
| [#1060](https://github.com/securo-finance/securo/pull/1060) account-backed pockets | new `pocket` tracking type, `goal_allocations` table, touches `connection_service.py` | keep the fork's table named `goal_source_allocations`; check for a name clash before merging |

## Divergence signals

Check these during the update pre-flight; each one means the fork needs a decision, not just a merge:

1. The diff of the fork against the new tag grew: new files, or more than two files outside the
   inventory.
2. The same conflict was resolved in two consecutive releases — then the resolution belongs in the
   code, not in the merge.
3. The official code gained a `get_provider(...)` call site without `workspace_id=` (search
   `grep -rn "get_provider(" backend/app`). That call would silently use the instance credentials in
   the wrong workspace. Today there are seven call sites, all passing `workspace_id=`.
4. The fork's tests fail on the new tag before any of our changes are applied.
5. Upstream merged one of the items in the table above.
