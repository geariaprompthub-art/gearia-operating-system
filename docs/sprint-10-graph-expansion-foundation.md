# Sprint 10 — Graph Expansion Foundation

## Objetivo e estado inicial

A Sprint 10 acrescenta expansão determinística de um salto ao resultado híbrido
existente. O ponto de partida continha `content_relationships`, criado na Sprint
06, e a busca híbrida da Sprint 09. Não houve migration nesta sprint: o schema,
as constraints e os índices existentes já suportam a leitura de pares canônicos.

O baseline operacional permanece `sources=3`, `contents=70`,
`content_relationships=0` e `content_embeddings=0`. O Graph depende de
relações previamente calculadas; ele não cria relações.

## Relações persistidas

`content_relationships` representa similaridade determinística como um par
canônico não direcionado: `content_id < related_content_id`. Há constraints
contra auto-relação e duplicação, FKs com `ON DELETE CASCADE` e índices para
ambos os lados. A leitura projeta esse armazenamento para adjacência lógica
`seed -> neighbor` sem expor a estrutura física aos serviços superiores.

## Arquitetura

- `ContentRelationshipRepository` lê ambos os lados do par em uma consulta e
  retorna `RelationshipNeighbor` mínimo.
- `GraphCandidateAggregator` é puro e usa `Decimal` para deduplicar pares,
  agregar targets e ordenar candidatos.
- `GraphExpansionService` recebe repository e agregador por injeção, cria seeds
  a partir de IDs ordenados e expande no máximo um salto.
- `ContentEligibilityRepository` filtra somente existência e
  `processing_status="processed"`, preservando a ordem.
- `HybridSearchService` orquestra a composição; a factory usa a mesma sessão
  para todos os repositories da requisição.

## Score e expansão

Para cada par lógico válido, a contribuição é:

```text
(edge_score / Decimal("100")) / Decimal(seed_rank)
```

O `graph_score` é a soma das contribuições de seeds distintos para o mesmo
target. Ele é interno: nunca é comparado ao `rrf_score`, serializado ou exposto.
O agregador preserva apenas a primeira ocorrência de cada par lógico e exclui
seeds como targets.

## Pipeline integrado

```text
query
  → lexical retrieval
  → vector retrieval
  → reciprocal rank fusion
  → fused seeds
  → graph expansion
  → hybrid-first merge
  → content eligibility
  → final top_k
  → single hydration
  → public response
```

Graph entra depois do RRF e antes da hidratação. A mesclagem mantém todos os
seeds híbridos primeiro e candidatos Graph depois; não há intercalação por
score. IDs são deduplicados por primeira ocorrência, portanto um seed sempre
vence sobre um candidato Graph igual.

## Backfill, top_k e elegibilidade

`top_k` continua sendo o limite público final. O corte ocorre somente depois
de expansão, merge, deduplicação e elegibilidade. Não há reserva artificial de
vagas para Graph: resultados híbridos elegíveis têm prioridade absoluta. Graph
preenche principalmente vagas abertas quando um seed híbrido não é elegível.

A elegibilidade v1 é deliberadamente restrita a conteúdo existente e
`processing_status="processed"`. Ela não aplica stale global, estado de
embedding, provider, model, source enabled, categoria ou data. A hydration
ocorre uma única vez após o corte final e preserva a ordem. Se um conteúdo for
removido entre elegibilidade e hydration, ele é omitido; não há segundo
backfill nessa sprint.

## Provenance e contrato HTTP

Seeds preservam `matched_by` do RRF, em ordem canônica `lexical`, `vector`.
Candidatos expandidos recebem somente `graph`; seeds não recebem `graph` por
terem sido alcançados pela expansão. O endpoint preservado é
`POST /search/hybrid` e continua expondo somente os campos já congelados:
`content_id`, `rank`, `title`, `url`, `summary`, `matched_by` e `total`.

Não são expostos `graph_score`, `rrf_score`, `edge_score`, relationship ID,
algorithm version, contributing seeds, embeddings ou metadados do provider.

## Falhas

O fluxo é fail-closed. Falhas operacionais de lexical, vector, Graph,
elegibilidade ou hydration produzem 503 sanitizado na fronteira HTTP. Falhas
de contrato interno inesperadas produzem 500 sanitizado. Graph vazio, ausência
de relações e resultado vazio são condições normais: retornam 200.

## Testes e validação

Os testes cobrem expansão de pares canônicos nos dois lados, agregação Decimal,
limites, elegibilidade, merge híbrido-primeiro, deduplicação, matched_by,
falhas fail-closed, uma única hydration e omissão concorrente. A integração usa
PostgreSQL real com FTS, pgvector e relações temporárias em transação com
rollback; não deixa resíduos. A validação também confirma uma consulta de
relações para múltiplos seeds, uma consulta de elegibilidade e ausência de N+1.

## Limitações conhecidas

- O baseline não possui relações persistidas; Graph exige relações já calculadas.
- A profundidade é fixa em um salto.
- Relações são não direcionadas e canônicas.
- RRF continua limitado a `top_k`; Graph é sobretudo backfill de vagas abertas.
- Não há reranking conjunto e `graph_score` não é comparável a `rrf_score`.
- Não há endpoint nem parâmetro público para ligar/desligar Graph.
- `content_id` permanece público por compatibilidade com a Sprint 09.

## Decisões adiadas para sprints futuras

- overfetch híbrido e reserva de vagas Graph;
- reranking cross-encoder e comparação conjunta de candidatos;
- profundidade maior que um salto;
- relationship types persistidos e direção real;
- source enabled, stale global e política de visibilidade;
- remoção futura de `content_id`;
- RAG e geração por LLM.

## Preparação para a Sprint 11

O conjunto combinado já preserva proveniência interna, prioridade de seeds e
ordem determinística. Uma Sprint futura pode reranquear esse conjunto sem
alterar o armazenamento de relações ou expor scores internos pela API.
