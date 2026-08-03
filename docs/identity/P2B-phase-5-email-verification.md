# P2B — Fase 5: verificação pública de e-mail

`POST /auth/email-verification/confirm` aceita exclusivamente um token opaco e
retorna `200 {"status":"email_verified"}`. A confirmação de token inválido,
expirado, invalidado ou já consumido preserva a mesma resposta e não altera dados.

O fluxo limita IP e token nos namespaces `auth:verify:ip` e `auth:verify:token`.
Depois do rate limit, o token é transformado em HMAC pelo `LifecycleTokenService`,
selecionado com bloqueio de linha, marcado como usado, e seu usuário pendente é
ativado com `email_verified_at` na mesma transação. Um segundo confirmador espera
o lock e observa o token usado; não há dupla ativação.

A rota não cria sessão, JWT, cookie ou refresh token. Respostas usam
`Cache-Control: no-store`; token, hash, e-mail e payload não entram em logs ou
respostas. CSRF não se aplica porque a confirmação é anônima e não depende de
cookies autenticados.
