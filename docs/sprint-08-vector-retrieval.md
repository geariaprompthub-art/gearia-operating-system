# Sprint 08 — Vector Retrieval Foundation

## Objetivo e escopo

Esta sprint implementa busca vetorial pura e exata no PostgreSQL com pgvector.
Ela acrescenta `POST /search/vector` sem alterar os contratos públicos da Sprint
07. A query é transformada em um vetor efêmero: ela nunca é gravada em banco.

Não fazem parte desta sprint: Hybrid Search, combinação com FTS, RRF, expansão de
grafo, reranking, RAG ou geração de respostas.

## Arquitetura

O router apenas valida o request e delega para `VectorSearchService`. O serviço
normaliza a query, obtém o vetor por dependency injection, valida suas 1536
dimensões e determina os IDs elegíveis. `VectorSearchRepository` concentra as
consultas PostgreSQL e pgvector. Service e repository não importam nem instanciam
o cliente OpenAI; o provider de produção é resolvido de forma lazy pela
dependência.

## Contrato HTTP

`POST /search/vector` recebe:

```json
{
  "query": "automação com IA",
  "top_k": 20,
  "threshold": 0.0
}
```

- `query` é obrigatória, recebe `strip` e aceita no máximo 8.000 caracteres.
- `top_k` aceita de 1 a 100 e tem padrão 20.
- `threshold` aceita de -1 a 1, tem padrão 0.0 e é inclusivo:
  `similarity >= threshold`.
- Campos extras do request são ignorados intencionalmente (`extra="ignore"`).
- Payload inválido recebe 422. Falha, vetor vazio, dimensão incorreta ou valores
  não finitos do provider recebem 503 com mensagem sanitizada.

A resposta contém `query`, `top_k`, `threshold`, `total` e `items`. Cada item
inclui metadados do conteúdo, `similarity` finita e `rank`. O rank começa em 1,
é contínuo e `total` é sempre igual a `len(items)`.

Nenhuma resposta, inclusive de erro, expõe `embedding`, `vector`,
`embedding_vector`, `values`, stack trace ou detalhes internos do provider.

## Elegibilidade, stale e ranking

Participam apenas embeddings com `status=completed`, vetor não nulo, provider
`openai`, modelo `text-embedding-3-small`, `dimensions=1536` e estratégia
`content-text-v1`. O hash atual do conteúdo é recalculado em memória: hash
divergente torna o embedding stale e o exclui.

Antes da busca vetorial, o serviço identifica IDs elegíveis sem carregar vetores.
Quando a lista está vazia, ele retorna imediatamente `total=0` e `items=[]`, sem
consulta vetorial ampla. A repository reaplica todos os filtros persistidos na
consulta final como defesa adicional.

A busca usa cosine distance do pgvector e calcula:

```text
similarity = 1 - cosine_distance
```

O resultado é ordenado por distância ascendente e, em empate, por `content_id`
ASC. Assim a ordem é determinística. Similaridades não finitas não chegam ao
contrato público.

## Estratégia de testes

Os testes usam `FakeEmbeddingProvider` determinístico e overrides de dependência.
Eles cobrem validação HTTP, normalização, ranking, threshold, empate, exclusões de
elegibilidade, provider chamado uma vez, falhas sanitizadas, ausência recursiva
de vetores e ausência de efeitos colaterais.

Uma integração PostgreSQL real cria vetores de 1536 dimensões em transação
isolada, valida similaridade 1.0 para vetor idêntico, ordem por cosine distance,
threshold no banco e rollback sem resíduos.

## Limitações atuais

A busca é exata e não cria índice HNSW ou IVFFlat. Essa decisão privilegia
previsibilidade e validação do contrato no volume atual. Um índice aproximado só
deve ser introduzido quando houver métricas de escala e uma migration justificada.
