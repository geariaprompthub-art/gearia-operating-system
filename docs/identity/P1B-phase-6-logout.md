# P1B — Fase 6: Logout

`POST /auth/logout` encerra exclusivamente a sessão autenticada pelo cookie de
access e retorna `204 No Content`. A rota usa `get_current_principal`, exige o
cookie `gearia_csrf` e o header `X-CSRF-Token`, ambos vinculados à sessão.

O `AuthService` controla uma única transação: bloqueia a sessão, confirma o
principal e o CSRF, grava `revoked_at` e `revocation_reason=user_logout`, revoga
todos os refresh tokens da mesma sessão e então confirma. Falhas fazem rollback;
outras sessões e suas famílias de refresh não são afetadas. A operação de domínio
é idempotente para uma sessão já revogada, sem revelar esse estado pela API.

A resposta de sucesso limpa somente pela `CookiePolicy` os cookies de access
(`Path=/`), refresh (`Path=/auth`) e CSRF (`Path=/auth`), mantendo domínio,
Secure e SameSite consistentes. Não há tokens, hashes, CSRF, IDs de sessão ou
dados de refresh no corpo público ou nos logs.
