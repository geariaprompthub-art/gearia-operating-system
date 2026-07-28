# P1B — Fase 5: principal autenticado e `/auth/me`

## Principal e canal de acesso

`AuthenticatedPrincipal` é um contexto imutável, separado dos modelos ORM. Ele
contém somente identificadores, timestamps e a projeção mínima necessária para
o usuário atual. Não contém JWT bruto, cookies, refresh, CSRF, hashes, senha ou
modelo SQLAlchemy.

O único canal de access token é o cookie HttpOnly `gearia_access`. Não há suporte
a Bearer header, query string, body ou precedência entre múltiplas fontes.

## Validação central

`AccessTokenAuthenticator` valida criptograficamente o JWT pelo `JWTService`,
depois consulta PostgreSQL para sessão e usuário. Exige sessão existente, ativa,
não expirada e vinculada ao `sub`; usuário `active`; e igualdade entre
`token_version` do JWT, sessão e usuário. Toda falha pública é `401` com
`{"detail":"Authentication required"}`.

Essa leitura não atualiza `last_seen_at`, status, versão de token, cookies ou
qualquer registro. PostgreSQL permanece a autoridade para revogação imediata e
invalidação por `token_version`.

## Endpoint

`GET /auth/me` recebe o principal exclusivamente pela dependency
`get_current_principal` e retorna:

```json
{"id":"uuid","email":"user@example.com","status":"active","email_verified_at":null,"created_at":"timestamp"}
```

Não retorna dados de sessão, token version, JTI, hashes, refresh, CSRF, campos
de login ou organizações. Também não emite, renova ou limpa cookies.

## Segurança e testes

Logs de sucesso usam somente `user_id` e `session_id`; falhas não registram
token, cookie, e-mail ou payload JWT. Cobertura inclui JWT malformado/ausente,
sessão inválida, usuário não permitido, token_version divergente, leitura sem
writes, HTTP e PostgreSQL real.

Logout continua fora de escopo.
