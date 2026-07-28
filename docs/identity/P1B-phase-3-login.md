# P1B — Fase 3: login público

## Endpoint

`POST /auth/login` aceita somente o payload abaixo; propriedades adicionais são
rejeitadas.

```json
{"email": "user@example.com", "password": "user password"}
```

Os dois campos são strings estritas. E-mail tem no máximo 320 caracteres e a
senha tem no máximo 128; a validação de credenciais continua usando a política
interna Argon2id.

Uma resposta bem-sucedida é `200` e contém apenas a identidade pública mínima:

```json
{"user": {"id": "uuid", "email": "user@example.com"}}
```

O endpoint não retorna access token, refresh token, hash ou token CSRF no corpo.

## Cookies e headers

O boundary HTTP aplica exclusivamente a `CookiePolicy` central para emitir:

- `gearia_access`: HttpOnly, path `/`;
- `gearia_refresh`: HttpOnly, path `/auth`;
- `gearia_csrf`: legível pelo browser, path `/auth`.

Os atributos `Secure`, `SameSite`, `Domain`, `Max-Age` e `Expires` derivam da
configuração central. A resposta também envia `Cache-Control: no-store` e
`Pragma: no-cache`.

## Códigos e segurança

- `401`: credenciais inválidas; usuário inexistente e senha incorreta têm a
  mesma resposta `{"detail":"Invalid credentials"}`;
- `403`: credenciais válidas de conta sem permissão para iniciar sessão;
- `429`: limite de tentativas excedido;
- `422`: payload inválido;
- `500`: falha interna sanitizada, sem detalhes de chaves, tokens ou hashes.

O fluxo realiza rate limit, verificação de credenciais, validação de status,
criação de sessão/refresh, emissão EdDSA, CSRF e um único commit. Exceções após
iniciar a unidade de trabalho produzem rollback completo.

## Fora de escopo

Refresh, logout, `/auth/me`, registro, organizações e autorização continuam
fora desta fase.
