# P2A — Workspace tenancy, ownership and data isolation

## Boundary

`Workspace` is the P2A aggregate root and the required boundary for private product data.
`Source` and `Content` remain canonical global records; a workspace never duplicates them.

`WorkspaceSource` is the primary link between a workspace and a canonical source.
`WorkspaceContentVisibility` is a derived, rebuildable projection of content from linked sources. It is not an authorization source of truth and may be rebuilt idempotently.

## Contexts

Private repositories require an explicit `WorkspaceContext(workspace_id, user_id)`.
They must apply the workspace condition in SQL; missing context is rejected before a query can run.

Every future workspace-scoped asynchronous job must carry `WorkspaceExecutionContext` with:

- `workspace_id`
- `correlation_id`
- `actor_type`
- `actor_id`
- `trigger`
- `timestamp`

## Current APIs

Authenticated callers resolve their workspace from the principal, never from a client-supplied workspace ID:

- `GET /workspaces/current`
- `PATCH /workspaces/current`
- `GET /workspaces/current/sources`
- `POST /workspaces/current/sources`
- `DELETE /workspaces/current/sources/{source_id}`
- `GET /workspaces/current/contents`
- `POST /workspaces/current/search/hybrid`

The existing global retrieval contracts remain unchanged. The workspace hybrid route filters the derived visibility set before lexical and vector candidate retrieval; Graph candidates are restricted to that same set before reranking.

## Persistence and rollback

Migration `20260729_0009` creates only `workspaces`, `workspace_sources`, and `workspace_content_visibility`.
It does not assign historical canonical data to a synthetic owner and does not remove shared PostgreSQL extensions. Its downgrade removes only P2A objects.

## Future compatibility

The intended evolution is:

```text
Workspace → WorkspaceMembership → Organization → RBAC → Entitlements
```

P2A deliberately does not introduce those future entities or their behavior.
