# Feature Matrix

| Domínio | Funcionalidade | Backend | Migration | Teste | UI | Auth | Observabilidade | Produção | Situação | Evidência | Pendência |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Operação | Health/live/ready | Sim | Não | Sim | Status | Não | Request ID/logs | Configurável | Entregue | 367 testes e stack Docker | Monitor externo futuro |
| Scout | RSS com proteção SSRF | Sim | Não | Sim | Não | Não | logs sanitizados | limites configuráveis | Entregue | testes P0 e stack Docker | proteção de egress por rede |
| Conteúdo | Sources/contents/enriquecimento | Sim | Existente | Sim | Não | Não | logs | existente | Preservado | suíte regressiva | console autenticado |
| Retrieval | lexical/vector/hybrid/reranking | Sim | Existente | Sim | Não | Não | telemetria | existente | Preservado | suíte regressiva | políticas de acesso |
| Identidade | Identity Core interno (User, e-mail, Argon2id) | Sim, interno | `20260727_0007` | Sim | Não | Não | Não | Não | Entregue | P1A: testes unitários e PostgreSQL | endpoints, sessões e autorização |
| Frontend | Console operacional | Não | Não | lint/typecheck/build | Sim | Não | status público | Sim | Entregue | build e smoke Docker | UX autenticada |
