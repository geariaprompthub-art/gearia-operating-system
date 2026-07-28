# P1B — Fase 2: infraestrutura transacional de autenticação

## Componentes concluídos

- `AuthSession` e `AuthRefreshToken`, com migration `20260728_0008`;
- repositórios transacionalmente neutros: usam `flush()` e a unidade de trabalho
  chamadora é a única responsável por `commit()` ou `rollback()`;
- JWT de acesso Ed25519/EdDSA, com `issuer`, `audience`, `kid`, expiração e
  claims obrigatórias validadas estritamente;
- refresh token opaco no formato canônico `token_id.secret`; somente SHA-256 é
  persistível, e token/hash não aparecem em `repr`;
- primitives de CSRF, política central de cookies e rate limiting Redis atômico;
- factories cacheadas em `auth_dependencies.py`; `auth_enabled=False` mantém a
  aplicação atual sem exigir chaves de assinatura.

## Fronteira transacional

Os repositórios recebem uma `Session` SQLAlchemy externa, fazem `add` e
`flush`, mas nunca confirmam ou revertem transações. Os fluxos públicos futuros
devem adotar a única fronteira: iniciar unidade de trabalho, operar repositórios,
`flush`, `commit` ou `rollback`.

## Migração e validação

A migration depende de `20260727_0007`, cria `auth_sessions` e
`auth_refresh_tokens`, e foi validada em banco PostgreSQL descartável no ciclo
`0007 → 0008 → 0007 → 0008`. Os testes de integração cobrem FKs, unicidade do
hash, constraints, rollback externo e `SELECT FOR UPDATE` real.

## Deliberadamente fora de escopo

Não existem router, endpoint, schema HTTP, login, refresh, logout, `/auth/me`,
JWT emitido por HTTP, cookie emitido por endpoint, principal dependency,
registro, organizações, RBAC ou tenantização. `AuthService.login`, `refresh` e
`logout` permanecem explicitamente não orquestrados até a fase seguinte.

## Preparação para a próxima fase

A orquestração pública poderá compor somente os serviços e repositórios desta
fase. Ela deverá manter a fronteira transacional, aplicar política de cookies no
boundary HTTP e integrar CSRF/rate limiting sem expor tokens, hashes ou chaves.
