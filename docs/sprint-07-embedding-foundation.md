# Sprint 07 — Fundação de Embeddings

## Endpoints públicos

- `POST /embeddings/contents/{content_id}/generate`: gera um embedding, ou retorna `skipped` quando um embedding atual já existe. Aceita `force` e `dry_run`.
- `GET /embeddings/contents/{content_id}`: retorna exclusivamente metadados do embedding; responde 404 quando não há registro.
- `POST /embeddings/generate`: processa em lote. `content_ids` é convertido em UUID pela API, deduplicado de modo determinístico e IDs inexistentes são ignorados. Sem `content_ids`, a seleção é ordenada por `created_at`, `id`.
- `GET /embeddings`: lista metadados com `page` (mínimo 1), `page_size` (1 a 100), filtros `status`, `provider`, `model` e `stale`.

Todas as respostas públicas de embeddings omitem o vetor. Os metadados expostos são `content_id`, provider, model, dimensions, versão da estratégia textual, hash, status, erro sanitizado, timestamps e `is_stale`.

## Stale e leitura

Um embedding só é utilizável quando está `completed` e seu `content_hash` coincide com o hash atual do conteúdo. Portanto `failed` e `processing` são expostos como `is_stale=true`; um `completed` com hash divergente também é stale.

Os endpoints GET calculam stale em memória. Eles não persistem status, hash ou timestamps, não resolvem provider, não criam cliente OpenAI e não realizam rede. Na listagem, o filtro stale é aplicado ao conjunto completo antes do cálculo de `total` e da paginação. A ordenação é sempre `created_at ASC, id ASC`.

## Geração em lote

Com `content_ids`, `requested` é a quantidade de IDs únicos solicitados e `processed` é a quantidade de conteúdos localizados. Resultados individuais incluem somente conteúdos processados. A invariante é:

```text
completed + skipped + failed = processed
```

Uma falha individual não desfaz nem interrompe os demais itens. Em `dry_run`, o provider não é chamado e nenhuma linha, hash, status ou timestamp é persistido. Itens `would_generate` contam como `completed` previsto no resumo, mas não contêm vetor.

## Segurança e testes

O provider de produção é resolvido de forma lazy apenas em geração. Testes HTTP substituem essa dependência por `FakeEmbeddingProvider` determinístico, sem rede; nenhum teste usa OpenAI. Chaves e vetores não são serializados nem registrados em logs públicos.

Execute a validação com:

```bash
docker compose up --build -d
docker compose exec -T api pytest -q
docker compose exec -T api alembic current
```
