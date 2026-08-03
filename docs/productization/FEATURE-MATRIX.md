# Feature Matrix

| Domínio | Funcionalidade | Backend | Migration | Teste | UI | Auth | Observabilidade | Produção | Situação | Evidência | Pendência |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Operação | Health/live/ready | Sim | Não | Sim | Status | Não | Request ID/logs | Configurável | Entregue | 367 testes e stack Docker | Monitor externo futuro |
| Scout | RSS com proteção SSRF | Sim | Não | Sim | Não | Não | logs sanitizados | limites configuráveis | Entregue | testes P0 e stack Docker | proteção de egress por rede |
| Conteúdo | Sources/contents/enriquecimento | Sim | Existente | Sim | Não | Não | logs | existente | Preservado | suíte regressiva | console autenticado |
| Retrieval | lexical/vector/hybrid/reranking | Sim | Existente | Sim | Não | Não | telemetria | existente | Preservado | suíte regressiva | políticas de acesso |
| Identidade | Identity Core interno (User, e-mail, Argon2id) | Sim, interno | `20260727_0007` | Sim | Não | Não | Não | Não | Entregue | P1A: testes unitários e PostgreSQL | endpoints, sessões e autorização |
| Autenticação | Infraestrutura interna de sessão, refresh token, JWT, CSRF, cookies e rate limit | Sim, interna | `20260728_0008` | Sim | Não | Não | Não | Não | Fase 2 entregue | P1B: serviços, repositories e PostgreSQL | login, refresh, logout e usuário atual |
| Autenticação | Login público com sessão e cookies centralizados | Sim | `20260728_0008` | Sim | Não | Não | Não | Não | Fase 3 entregue | P1B: POST /auth/login | refresh, logout e usuário atual |
| Autenticação | Refresh HTTP com rotação, CSRF e detecção de reutilização | Sim | `20260728_0008` | Sim | Não | Não | Não | Não | Fase 4 entregue | P1B: POST /auth/refresh | logout e usuário atual |
| Autenticação | Principal autenticado e usuário atual | Sim | `20260728_0008` | Sim | Não | Não | Não | Não | Fase 5 entregue | P1B: GET /auth/me | logout |
| Autenticação | Logout da sessão atual com CSRF e revogação transacional | Sim | `20260728_0008` | Sim | Não | Não | Não | Não | Fase 6 em validação | P1B: POST /auth/logout | auditoria final e encerramento |
| Identidade | Cadastro, verificação de e-mail e password reset P2B | Sim | `20260730_0010` | Sim | Não | Não | logs sanitizados | Não | Entregue | P2B fases 4-6 | entrega de e-mail real futura |
| Identidade | Anonimização irreversível da conta e bloqueio do workspace | Sim | `20260730_0010` | Sim, PostgreSQL | Não | CSRF + rate limit | logs sanitizados | Não | Entregue | P2B fase 7: DELETE /auth/me | remoção física e PATCH /auth/me fora de escopo |
| Frontend | Console operacional | Não | Não | lint/typecheck/build | Sim | Não | status público | Sim | Entregue | build e smoke Docker | UX autenticada |
