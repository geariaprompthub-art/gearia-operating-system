# GearIA Operating System

## Workspace tenancy (P2A)

P2A introduces a personal `Workspace` as the boundary for private product data.
Canonical sources and contents remain shared; workspace visibility is a rebuildable projection.
See [P2A workspace tenancy](docs/productization/P2A-WORKSPACE-TENANCY.md) for the scoped API and execution-context contract.

Plataforma de conteúdo com FastAPI, PostgreSQL/pgvector, Redis e Next.js. O repositório preserva ingestão RSS, enriquecimento determinístico, busca lexical, vetorial, híbrida e reranking. A P0 endurece a operação sem introduzir login, billing ou novos providers.

## Serviços e arquitetura

- `services/api`: FastAPI, SQLAlchemy, Alembic, PostgreSQL e Redis.
- `apps/web`: console operacional Next.js 15.
- `postgres`: dados persistentes e extensões `vector`, `unaccent`, `pg_trgm`.
- `redis`: dependência interna de readiness.

O Compose é destinado a desenvolvimento/local ou single-host. PostgreSQL e Redis não publicam portas por padrão.

## Início rápido

1. Copie `.env.example` para `.env` e substitua os placeholders locais.
2. Execute `docker compose up --build -d`.
3. Abra `http://localhost:3000`, `http://localhost:8000/health/live` e, em desenvolvimento, `http://localhost:8000/docs`.

Comandos operacionais: `make up`, `make test`, `make lint`, `make typecheck`, `make migrate`, `make logs` e `make smoke`. Use `docker compose down` para parar; não remova volumes para operação normal.

## Configuração e segurança

Configuração é centralizada em `services/api/app/core/config.py`. Produção rejeita debug, hosts wildcard, CORS inseguro com credenciais e credenciais locais padrão. Configure `DOCS_ENABLED=false` para ocultar OpenAPI em produção. Não versione `.env` ou chaves de provider.

O backend oferece `GET /health` (compatibilidade), `/health/live` (processo) e `/health/ready` (PostgreSQL/Redis). Endpoints de busca e embeddings recebem `Cache-Control: no-store`; request IDs são retornados em toda resposta.

O Scout aceita apenas URLs HTTP(S) públicas, limita redirects, timeout, tamanho e itens por feed. Não configure fontes internas ou privadas.

## Qualidade e migrations

Execute testes pelo contêiner: `docker compose --profile tools run --rm --no-deps api-test pytest -q`. Para migration: `docker compose exec api alembic upgrade head`. O CI valida compilação Python, migrations, testes, lockfile, typecheck e build web sem chaves externas.

Consulte [P0](docs/productization/SPRINT-P0.md), [P1A](docs/identity/P1A.md), as [Fases 2](docs/identity/P1B-phase-2.md), [3](docs/identity/P1B-phase-3-login.md), [4](docs/identity/P1B-phase-4-refresh.md), [5](docs/identity/P1B-phase-5-principal.md) e [6](docs/identity/P1B-phase-6-logout.md) da P1B, além das entregas P2B de [cadastro](docs/identity/P2B-phase-4-registration.md), [verificação](docs/identity/P2B-phase-5-email-verification.md), [password reset](docs/identity/P2B-phase-6-password-reset.md) e [anonimização](docs/identity/P2B-phase-7-account-anonymization.md), e da [Feature Matrix](docs/productization/FEATURE-MATRIX.md), para escopo, evidências e pendências.
