# ADR-001 — Integração do HybridRerankingPipeline

## Status

Proposto

## Contexto

O fluxo atual de `POST /search/hybrid` executa busca lexical e vetorial,
aplica Reciprocal Rank Fusion (RRF), expande candidatos por Graph, deduplica
por primeira ocorrência, filtra elegibilidade, aplica `top_k` e faz a
hidratação pública. Atualmente, o RRF recebe o próprio `top_k` público como
limite interno. Isso corta candidatos antes de uma etapa de reranking poder
compará-los.

O merge atual preserva os seeds RRF antes dos candidatos Graph. A
deduplicação por primeira ocorrência garante que um seed RRF vence uma
duplicata vinda do Graph. Se o RRF for ampliado para 100 candidatos e o Graph
for apenas anexado depois, os 100 lugares do pool do
`HybridRerankingPipeline` serão ocupados pelos seeds e os candidatos Graph
inéditos não chegarão ao reranking. Esse é um caso de starvation total de
Graph.

O `HybridRerankingPipeline` já existe de forma isolada. Ele encapsula
elegibilidade, pool máximo de 100 candidatos, hidratação parcial, formatação,
reranking, aplicação final de `top_k` e hidratação pública. O pipeline ainda
não está integrado ao `HybridSearchService`.

O protocolo `RerankingProvider` existe, mas não há adapter de produção,
factory de injeção de dependência, setting ou variável de ambiente para
reranking. O `EmbeddingProvider` não é compatível: ele gera vetores a partir
de texto, enquanto `RerankingProvider` recebe query e candidatos e devolve
scores por `content_id`.

O contrato HTTP atual de `POST /search/hybrid` não deve ser alterado.

## Decisão 1 — Provider de produção

O provider de produção será um adapter para um serviço externo dedicado de
reranking que implemente o protocolo `RerankingProvider`.

- O `EmbeddingProvider` não será reutilizado.
- O `RerankingService` não será acoplado diretamente ao SDK de um fornecedor.
- O adapter será construído por injeção de dependência.
- O provider poderá ser substituído sem alterar o pipeline.
- Os testes usarão um fake provider injetado, sem chamadas externas.
- Falhas operacionais permanecerão fail-closed.
- Não haverá fallback silencioso para a ordem anterior ao reranking.

Fornecedor, modelo, endpoint, credencial, nome de variável de ambiente,
timeout, retry e custo máximo permanecem indefinidos.

## Decisão 2 — Pool pré-reranking

O `top_k` público não limitará o horizonte interno do RRF. O fluxo alvo será:

```text
Lexical + Vector
→ RRF
→ Graph Expansion
→ merge
→ deduplicação
→ formação de pool consolidado
→ limite máximo de 100
→ HybridRerankingPipeline
→ top_k público
→ resposta pública
```

As regras do pool são:

- RRF e Graph disputarão o mesmo reranking.
- Graph não poderá sofrer starvation sistemático.
- Nenhuma fonte poderá ocupar antecipadamente todas as 100 posições.
- O pool consolidado terá no máximo 100 candidatos.
- A deduplicação será feita por `content_id` e a primeira ocorrência vencerá.
- Seeds RRF precederão duplicatas vindas do Graph.
- Somente candidatos Graph inéditos ocuparão vagas destinadas a Graph.
- Se uma fonte não preencher sua parcela, a outra poderá preencher vagas
  ociosas.
- `top_k` será aplicado somente após o reranking.
- Quando o total disponível for menor que `top_k`, não haverá backfill
  artificial.
- Elegibilidade, hidratação parcial, formatação, reranking, `top_k` e
  hidratação pública continuarão como responsabilidades do
  `HybridRerankingPipeline`.

A divisão exata entre RRF e Graph permanece indefinida: não foi escolhida uma
quantidade, percentual, fórmula, setting ou constante.

## Alternativas consideradas

### 1. Manter a política atual

Rejeitada para a integração de reranking. O RRF é limitado por `top_k`, o que
impede a promoção de candidatos RRF abaixo do corte público inicial. Graph só
ocupa vagas restantes e pode não participar quando `top_k` for alto.

### 2. RRF com horizonte 100 e Graph anexado depois

Rejeitada. Os 100 seeds RRF ocupam o pool máximo antes de Graph, causando
starvation total de candidatos Graph inéditos.

### 3. Reserva fixa sem preenchimento

Adiada. Evita starvation, mas pode deixar vagas vazias quando uma fonte não
possui candidatos suficientes. A reserva numérica não foi decidida.

### 4. Reserva com preenchimento balanceado

Direção arquitetural aprovada, com orçamento exato pendente. Preserva a
participação de ambas as fontes e permite preencher vagas ociosas sem exceder
100 candidatos. A divisão fixa ou proporcional ainda exige decisão de produto.

### 5. Modelo local

Adiado. Não há infraestrutura de modelo local, dependências ou configuração
desse tipo no repositório.

### 6. Reutilização do EmbeddingProvider

Rejeitada. Os contratos são incompatíveis e reutilizá-lo acoplaria reranking à
infraestrutura de embeddings sem compatibilidade funcional.

### 7. Adiar a integração

Alternativa válida enquanto fornecedor, configuração e orçamento do pool não
forem definidos. Evita introduzir provider fictício ou constantes arbitrárias.

## Consequências positivas

- RRF e Graph podem disputar o mesmo reranking.
- Evita starvation sistemático de Graph.
- Permite promover candidatos abaixo do `top_k` inicial.
- Desacopla o fornecedor do pipeline e do `RerankingService`.
- Permite testes determinísticos com fake provider e sem chamadas externas.
- Preserva o contrato HTTP público existente.

## Consequências negativas

- Introduz uma integração externa adicional.
- Acrescenta custo e latência à busca híbrida.
- Exige configuração ainda não definida.
- Cria novos caminhos de falha operacional.
- Exige testes de integração do adapter e da composição híbrida.
- Exige decisão posterior sobre o orçamento entre RRF e Graph.

## Decisões pendentes

1. Fornecedor externo.
2. Modelo.
3. Endpoint.
4. Credencial e variável de ambiente.
5. Timeout e retry.
6. Divisão exata entre RRF e Graph.
7. Divisão fixa ou proporcional.
8. Política HTTP para provider ausente ou indisponível.
9. Confirmação de que 100 permanece como cap definitivo.

## Fluxo-alvo

```text
POST /search/hybrid
→ lexical search
→ vector search
→ RRF com horizonte interno
→ Graph Expansion
→ merge e deduplicação
→ pool consolidado de até 100
→ HybridRerankingPipeline
→ top_k público
→ resposta HTTP
```

## Critérios para liberar implementação

A implementação só poderá começar após definição explícita de:

1. fornecedor e modelo do provider;
2. configuração necessária;
3. orçamento exato de RRF;
4. orçamento exato de Graph;
5. regra de preenchimento;
6. erro operacional para provider indisponível.
