# P1B — Fase 4: refresh, rotação e reutilização

## Contrato HTTP

`POST /auth/refresh` não aceita body. O refresh chega somente no cookie HttpOnly
`gearia_refresh`; o CSRF exige tanto cookie `gearia_csrf` quanto header
`X-CSRF-Token`. A resposta de sucesso é `200` com `{"status":"authenticated"}`.
Access, refresh e CSRF são renovados exclusivamente pela `CookiePolicy`; nenhum
token, hash, família, sessão ou claim é retornado no corpo.

Falhas de token ausente, inválido, expirado, revogado ou reutilizado retornam
`401` com `{"detail":"Refresh failed"}`. CSRF inválido retorna `403` com a
mesma mensagem. Rate limit retorna `429`, com `Retry-After`. Falhas internas
retornam `500` sanitizado. Falhas terminais limpam os três cookies.

## Rotação transacional

Após `SELECT FOR UPDATE` no registro de refresh, o serviço valida token, sessão,
usuário e CSRF. Um token válido é marcado em `used_at`; o sucessor recebe a
mesma `family_id`, referencia o anterior em `parent_token_id`, e o anterior
referencia o sucessor em `replaced_by_token_id`. A sessão recebe novo hash CSRF e
`last_seen_at`. Tudo é confirmado em um único commit; qualquer falha comum faz
rollback.

## Reutilização e concorrência

Token já usado ou substituído é reutilização. A política desta fase registra
`reuse_detected_at`, revoga a sessão inteira e todos seus refresh tokens, confirma
a revogação e retorna falha pública genérica. Não revoga outras sessões do usuário
nem incrementa `token_version`.

PostgreSQL é a autoridade: `SELECT FOR UPDATE`, estado de uso e uma transação
única impedem dois sucessores ativos. O teste concorrente real confirma uma única
rotação inicial, seguida da revogação de sessão pela segunda requisição.

## CSRF, rate limit e logging

CSRF é renovado em cada refresh e o hash anterior é invalidado na mesma transação.
O rate limit é fail-open por configuração padrão, em namespaces separados por IP
derivado e token UUID (nunca pelo token bruto). Logs registram apenas categoria e
IDs permitidos; tokens, hashes, cookies, CSRF e chaves não são registrados.

## Risco residual

Um atacante que possua simultaneamente refresh e CSRF pode provocar a revogação da
sessão por replay; esta é a resposta deliberada de contenção. Logout, principal
autenticado e rotação exposta por endpoints adicionais permanecem fora de escopo.
