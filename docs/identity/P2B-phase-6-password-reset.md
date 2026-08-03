# P2B — Fase 6: password reset público

`POST /auth/password-reset/request` recebe somente um e-mail e retorna sempre
`202 {"status":"password_reset_requested"}`. Apenas usuários ativos recebem
um desafio opaco; contas inexistentes, pendentes, bloqueadas, suspensas ou
anonimizadas têm resposta idêntica e não recebem token.

`POST /auth/password-reset/confirm` recebe token opaco e nova senha. O token é
hashado por HMAC, bloqueado para consumo único e, na mesma transação, atualiza o
hash Argon2id, incrementa `token_version`, revoga sessões e refresh tokens e
marca o desafio como usado. Repetições, expiração e invalidação preservam a
resposta pública `200 {"status":"password_reset_completed"}` sem mutação.

Os limites usam `auth:password-reset:request:ip`,
`auth:password-reset:request:email`, `auth:password-reset:confirm:ip` e
`auth:password-reset:confirm:token`. A entrega permanece limitada ao adaptador
fake sem rede e ocorre somente depois do commit. Não há login automático, JWT,
cookies, sessão ou refresh novos. Todas as respostas são `no-store`, e logs não
incluem senha, token, hash, e-mail ou payload.
