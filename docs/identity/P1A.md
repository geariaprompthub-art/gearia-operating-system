# P1A — Identity Core interno

## Objetivo

Estabelecer a persistência interna de identidade para as próximas etapas de
autenticação, sem publicar endpoints nem introduzir sessão, JWT ou tenancy.

## Escopo implementado

- modelo SQLAlchemy `User` e migration `20260727_0007`;
- normalização canônica de e-mail (NFC, `strip` e `casefold`);
- política de senha de 12 a 128 caracteres, preservando espaços e Unicode;
- hashing e verificação Argon2id por `argon2-cffi==23.1.0`;
- DTOs internos que não serializam `password_hash`;
- repositório de leitura/criação e `IdentityService` de verificação interna.

## Modelo e invariantes

`users` possui identificador UUID, `email`, `email_normalized` único,
`password_hash` opcional, status, dados de verificação, controle de versão de
token, contagem de falhas, bloqueio, auditoria de login e timestamps.

Os estados são: `pending_verification`, `active`, `suspended`, `locked` e
`anonymized`. O banco protege `token_version >= 1`,
`failed_login_count >= 0`, o vocabulário de status e a regra normativa:

```text
status = 'locked' OR locked_until IS NULL
```

Assim, somente usuários `locked` podem ter `locked_until`; bloqueios sem prazo,
futuros e expirados continuam representáveis. Um bloqueio expirado retorna
`lock_expired` na verificação interna e não altera estado nem timestamps.

## Senhas

O hasher usa Argon2id com parâmetros explícitos de produção: `time_cost=3`,
`memory_cost=65536 KiB`, `parallelism=2`, `hash_len=32` e `salt_len=16`.
Testes usam parâmetros menores isoladamente. A mesma senha gera hashes
diferentes; verificação de usuário inexistente usa um hash dummy. Hashes
malformados são tratados como falha de credencial, sem expor material ou exceção
interna. `needs_rehash` permanece disponível para uma etapa futura de login.

## Repositório e serviço

`UserRepository` encapsula criação e busca por UUID/e-mail normalizado. A
unicidade física de `email_normalized` é a autoridade final; após uma colisão
confirmada o repositório faz rollback e retorna um erro interno sanitizado.

`IdentityService` somente cria usuários locais e verifica credenciais/status.
Ele não cria sessões, tokens, JWTs, cookies, endpoints, nem atualiza
`last_login_at` ou `failed_login_count`.

## Migration e validação

A revision `20260727_0007` cria apenas `users` e seu índice de e-mail
normalizado. O ciclo validado em PostgreSQL descartável foi:

```text
20260721_0006 → 20260727_0007 → 20260721_0006 → 20260727_0007
```

O downgrade remove somente `users`; `contents` e `content_embeddings` foram
preservadas. No banco persistente, o upgrade não criou usuários nem alterou as
contagens legadas.

## Testes

Há cobertura unitária para normalização, limites de senha, Argon2id, DTOs e
estados internos. A integração PostgreSQL cobre defaults, consultas, unicidade,
checks, rollback e duas sessões concorrentes para o mesmo e-mail canônico.
Nenhum teste chama providers externos.

## Fora de escopo

- registro, login e endpoints públicos;
- sessão, JWT, refresh token, cookies e CSRF;
- recuperação/verificação de e-mail, MFA e login social;
- organizações, papéis, tenantização e billing;
- envio de e-mail ou qualquer provider externo.

## Riscos residuais e preparação para P1B

O núcleo ainda não aplica rate limit, lockout automático nem fluxo de
verificação de e-mail porque não há boundary HTTP nesta etapa. P1B deve criar
os contratos públicos sobre este serviço sem serializar hash, token ou detalhes
de erro de credencial.
