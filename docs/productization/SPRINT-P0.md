# Sprint P0 — Productization

## Objetivo

Endurecer a base operacional do GearIA sem alterar migrations, os contratos de retrieval ou introduzir autenticação, billing e novas capacidades de IA.

## Escopo implementado

- Configuração centralizada por ambiente, CORS e hosts confiáveis.
- Headers de segurança, correlação de requisição e health/readiness.
- Transporte RSS com bloqueio SSRF, validação de redirect, timeout e limite de payload.
- Imagens Docker não-root, runtime web de produção, rede interna e banco/cache sem portas públicas.
- Lockfile web, comandos oficiais e CI inicial.
- Console operacional mínimo e documentação atualizada.

## Validação de encerramento

- 367 testes da API aprovados, sem chamadas a providers externos.
- Lint, typecheck e build do frontend aprovados com o lockfile versionado.
- API, frontend, PostgreSQL e Redis validados em uma stack Docker real.
- Health, liveness, readiness e as rotas operacionais do frontend responderam com sucesso.
- Alembic permaneceu em `20260721_0006 (head)` e os dados preexistentes foram preservados.

## Situação

Sprint P0 concluída. A próxima etapa recomendada é a definição da Sprint P1 para identidade, organizações, RBAC e fundações de acesso.

## Fora de escopo

Identidade, organizações, RBAC, billing, RAG, novos providers e novas migrations.

## Riscos e pendências

O Scout valida DNS antes de cada hop; proteção contra rebinding posterior depende da resolução/transporte da infraestrutura. A exposição de banco/cache para debugging deve usar override local, nunca o Compose padrão. A segurança de dependências Node precisa de manutenção periódica via atualização compatível e revisão de `npm audit`.
