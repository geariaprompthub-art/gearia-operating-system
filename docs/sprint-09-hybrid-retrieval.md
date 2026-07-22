# Sprint 09 — Hybrid Retrieval

`POST /search/hybrid` combina FTS PostgreSQL e candidatos vetoriais já elegíveis
com Reciprocal Rank Fusion. O request aceita estritamente somente `query` e `top_k`;
campos desconhecidos são rejeitados com 422 e os tipos não sofrem coerção implícita. Os limites
internos são 50 candidatos lexicais, 50 vetoriais e `rrf_k=60`, sempre elevados ao
menos a `top_k`.

O fluxo é sequencial e fail-closed: falha operacional em FTS, pgvector ou provider
retorna 503 sanitizado, sem fallback silencioso. A resposta publica apenas rank,
metadados mínimos e `matched_by`; não expõe scores, vetores ou detalhes de provider.

Itens desaparecidos antes da hidratação são omitidos e os ranks são recalculados.
Não há migration, persistência de query, Graph, reranking ou RAG.
