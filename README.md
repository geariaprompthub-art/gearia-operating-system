# GearIA Operating System

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

Consulte [P0](docs/productization/SPRINT-P0.md) e a [Feature Matrix](docs/productization/FEATURE-MATRIX.md) para escopo, evidências e pendências.
