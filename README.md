# GearIA Operating System

## Sprints 06 e 07

Esta sprint acrescenta a tabela `content_relationships` e o algoritmo versionado
`deterministic-v1`. Cada par é armazenado uma única vez, em ordem canônica de UUID,
sem autorrelações. Relações incidentes que deixam de qualificar em um rebuild são
removidas; um rebuild futuro do outro conteúdo pode recriá-las.

O algoritmo compara conteúdo já enriquecido (`processing_status=processed`) usando
normalização sem diferença de caixa ou acentos. A pontuação máxima é 100: tópicos
(35), keywords (25), categoria (15), similaridade textual (15), proximidade de
publicação (5) e mesma fonte (5). Somente scores a partir de 20 são persistidos.
Há no máximo 500 candidatos avaliados e 50 relações mantidas por conteúdo.

Em PostgreSQL, a similaridade de `title + summary` usa `pg_trgm`/`similarity()`;
SQLite usa apenas um fallback determinístico da biblioteca padrão para os testes.
As relações permanecem determinísticas. A Sprint 07 acrescenta a fundação de
embeddings com pgvector e geração controlada por provider; ela não implementa
retrieval, RAG, IA externa em testes, Neo4j ou recomendações personalizadas.
O contrato completo da Sprint 07 está em `docs/sprint-07-embedding-foundation.md`.

Principais endpoints:

- `POST /relationships/contents/{content_id}/rebuild?dry_run=false`
- `POST /relationships/rebuild` (body com filtros e `limit`, padrão 100, máximo 500)
- `GET /contents/{content_id}/related`
- `GET /contents/{content_id}/recommendations`
- `GET /relationships/between/{content_id}/{related_content_id}`

`related` expõe todas as relações persistidas com filtros e paginação. Já
`recommendations` reutiliza essas mesmas relações com score mínimo 20 e página menor;
não representa personalização por usuário. `dry_run=true` calcula e relata criações,
atualizações e remoções sem alterar linhas ou timestamps.

Para aplicar ou conferir migrations no container:

```bash
docker compose up --build
docker compose exec api alembic current
docker compose exec api alembic upgrade head
```

Para executar a suíte de testes:

```bash
docker compose run --rm --no-deps api pytest -q
```

Fundação do monorepo da plataforma de inteligência artificial GearIA.

## Estrutura

```text
apps/web/          Frontend Next.js 15 + TypeScript
services/api/      API FastAPI + SQLAlchemy 2.x + Alembic
packages/          Pacotes compartilhados (reservado)
infrastructure/    Configurações de infraestrutura (reservado)
docs/              Documentação (reservado)
tests/             Testes de integração e ponta a ponta (reservado)
```

## Pré-requisitos

- Docker Desktop com Docker Compose v2

## Como iniciar

1. Opcionalmente, copie `.env.example` para `.env` e ajuste as credenciais locais.
2. Na raiz do projeto, execute:

   ```bash
   docker compose up
   ```

Serviços disponíveis:

- Frontend: http://localhost:3000
- API: http://localhost:8000/health
- PostgreSQL: `localhost:5432`
- Redis: `localhost:6379`

Para encerrar, execute `docker compose down`. Para também remover os dados locais, use `docker compose down --volumes`.

## Backend

O backend recebe `DATABASE_URL`, `REDIS_URL` e `LOG_LEVEL` por variáveis de ambiente, possui logs básicos e expõe `GET /health`, que responde `{"status":"ok"}`. As migrações Alembic ficam em `services/api/migrations`.

Esta etapa contém apenas a fundação técnica; não inclui regras de negócio, autenticação ou agentes.

## Busca textual

`GET /search` consulta conteúdos indexados pelo PostgreSQL Full-Text Search. O índice `GIN` é mantido por trigger usando `unaccent` e a configuração `simple`, por isso a busca não diferencia maiúsculas/minúsculas ou acentos. O vetor pondera título (A), keywords e topics (B), resumo (C) e categoria (D).

Exemplos:

```text
GET /search?q=chatgpt
GET /search?q=automacao&category=inteligencia_artificial
GET /search?processing_status=processed&min_relevance_score=70
GET /search?page=2&page_size=20
GET /search?q=prompt&sort_by=rank&sort_order=desc
```

Além de `q`, a busca aceita filtros de fonte, categoria, tópico, idioma, status, relevância e intervalo de publicação. A resposta inclui paginação (`page`, `page_size`, `total`, `total_pages`) e `search_rank`; sem `q`, o rank é `null` e a ordenação padrão é por criação decrescente.

Após atualizar o projeto, aplique a migration e execute os testes:

```bash
docker compose exec api alembic upgrade head
docker compose exec api pytest
```
