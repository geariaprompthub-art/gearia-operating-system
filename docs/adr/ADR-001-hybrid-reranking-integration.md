# ADR-001 — Integração do HybridRerankingPipeline

## Status

Aceito

## Contexto

O fluxo atual de `POST /search/hybrid` executa busca lexical e vetorial,
aplica Reciprocal Rank Fusion (RRF), expande candidatos por Graph, deduplica
por primeira ocorrência, filtra elegibilidade, aplica `top_k` e faz a
hidratação pública. Atualmente, o RRF recebe o próprio `top_k` público como
limite interno. Isso corta candidatos antes de uma etapa de reranking poder
compará-los.

O merge atual preserva os seeds RRF antes dos candidatos Graph. A
deduplicação por primeira ocorrência garante que um seed RRF vence uma
duplicata vinda do Graph. Usar RRF com 100 candidatos e anexar Graph depois
ocuparia os 100 lugares do pool do `HybridRerankingPipeline` com seeds e
causaria starvation total de candidatos Graph inéditos.

O `HybridRerankingPipeline` já existe de forma isolada. Ele encapsula
elegibilidade, pool máximo de 100 candidatos, hidratação parcial, formatação,
reranking, aplicação final de `top_k` e hidratação pública. O pipeline ainda
não está integrado ao `HybridSearchService`.

O protocolo `RerankingProvider` existe, mas ainda não há adapter de produção,
factory de injeção de dependência, setting ou variável de ambiente para
reranking. O `EmbeddingProvider` não é compatível: ele gera vetores a partir
de texto, enquanto `RerankingProvider` recebe query e candidatos e devolve
scores por `content_id`.

O contrato HTTP atual de `POST /search/hybrid` não deve ser alterado.

## Decisão 1 — Provider de produção

O provider de produção será um adapter dedicado para Voyage AI, usando o
modelo inicial `rerank-2.5-lite` e implementando o protocolo
`RerankingProvider`.

- O `EmbeddingProvider` não será reutilizado.
- O `RerankingService` não será acoplado diretamente ao SDK da Voyage AI.
- O adapter será construído por injeção de dependência.
- O provider poderá ser substituído sem alterar o pipeline.
- Os testes injetarão um fake provider sem chamadas externas.
- Tipos do SDK Voyage não atravessarão a fronteira do domínio.
- O provider fará uma chamada por execução do pipeline.
- Não haverá retry automático na primeira versão.
- Não haverá fallback silencioso para a ordem anterior ao reranking.

### Configuração aprovada

```text
RERANKING_PROVIDER=voyage
VOYAGE_API_KEY=<secret>
VOYAGE_RERANK_MODEL=rerank-2.5-lite
RERANKING_TIMEOUT_SECONDS=5
```

### Política operacional

Provider ausente ou mal configurado, timeout, autenticação inválida, rate
limit, indisponibilidade, resposta incompleta e resposta inválida são falhas
operacionais. Futuramente, elas resultarão em HTTP 503 sanitizado.

O `RerankingService` continua responsável por validar completude e unicidade
da resposta. Não serão registrados API key, documentos completos ou resposta
bruta sensível. Preço, limites e disponibilidade são externos e podem mudar;
esses valores não pertencem aos settings nem aos contratos de domínio.

## Decisão 2 — Pool pré-reranking

O `top_k` público não limitará o horizonte interno do RRF. O horizonte máximo
desejado de candidatos consolidados antes do pipeline é:

```text
H = min(100, max(20, 5 * top_k))
```

Onde `H` é limitado pelo cap absoluto de 100 já adotado pelo
`HybridRerankingPipeline`.

O orçamento inicial é:

```text
graph_budget = ceil(H * 0.20)
rrf_budget = H - graph_budget
```

RRF recebe orçamento-base de 80% e Graph recebe orçamento-base de 20%. Uma
fonte só pode ocupar vagas da outra quando houver ociosidade.

| top_k | H | RRF | Graph |
| ---: | ---: | ---: | ---: |
| 1 | 20 | 16 | 4 |
| 5 | 25 | 20 | 5 |
| 10 | 50 | 40 | 10 |
| 20 | 100 | 80 | 20 |
| 80 | 100 | 80 | 20 |
| 100 | 100 | 80 | 20 |

### Deduplicação e preenchimento

- Deduplicar por `content_id`.
- A primeira ocorrência vence.
- RRF precede Graph quando o mesmo `content_id` aparece em ambas as fontes.
- Duplicatas Graph de candidatos RRF não consomem `graph_budget`.
- Somente candidatos Graph inéditos contam para o orçamento Graph.
- Preservar a ordem interna de cada fonte.
- Selecionar até `rrf_budget` candidatos RRF únicos.
- Selecionar até `graph_budget` candidatos Graph inéditos.
- Se Graph não preencher sua reserva, completar a sobra com RRF remanescente.
- Se RRF não preencher sua reserva, completar a sobra com Graph remanescente.
- Nunca ultrapassar `H` nem o cap absoluto de 100.
- Não produzir backfill artificial.
- Se o total disponível for menor que `top_k`, devolver somente o disponível.
- Aplicar `top_k` somente após reranking.

Elegibilidade, hidratação parcial, formatação, reranking, `top_k` e
hidratação pública continuam como responsabilidades do
`HybridRerankingPipeline`.

## Alternativas consideradas

### 1. Manter a política atual

Rejeitada para a integração de reranking. O RRF é limitado por `top_k`, o que
impede a promoção de candidatos RRF abaixo do corte público inicial. Graph só
ocupa vagas restantes e pode não participar quando `top_k` for alto.

### 2. RRF com horizonte 100 e Graph anexado depois

Rejeitada. Os 100 seeds RRF ocupam o pool máximo antes de Graph, causando
starvation total de candidatos Graph inéditos.

### 3. Reserva fixa sem preenchimento

Rejeitada. Embora evite starvation, pode deixar vagas ociosas quando uma fonte
não possui candidatos suficientes.

### 4. Reserva com preenchimento balanceado

Aceita. A divisão inicial é 80% RRF e 20% Graph, com preenchimento por
remanescente da outra fonte somente em caso de ociosidade.

### 5. Modelo local

Adiado. Não há infraestrutura de modelo local, dependências ou configuração
desse tipo no repositório.

### 6. Reutilização do EmbeddingProvider

Rejeitada. Os contratos são incompatíveis e reutilizá-lo acoplaria reranking à
infraestrutura de embeddings sem compatibilidade funcional.

### 7. Adiar a integração

Rejeitada como decisão arquitetural. A estratégia de integração, provider,
modelo, configuração e orçamento inicial foram definidos por este ADR; a
implementação ainda requer fase própria.

## Consequências positivas

- RRF e Graph disputarão o mesmo reranking.
- Graph não sofrerá starvation sistemático.
- Candidatos abaixo do `top_k` inicial poderão ser promovidos.
- O fornecedor permanecerá desacoplado do pipeline e do `RerankingService`.
- Testes poderão usar fake provider sem chamadas externas.
- O contrato HTTP público será preservado.

## Consequências negativas

- Uma integração externa adicional introduz custo e latência.
- São necessários configuração, adapter e testes de integração.
- Há novos caminhos de falha operacional.
- Preço, limites e disponibilidade do fornecedor são externos e mutáveis.
- O fator de overfetch, a proporção 80/20 e o cap de 100 precisarão de revisão
  com dados de produção.

## Decisões futuras

1. Eventual migração de modelo.
2. Política de circuit breaker.
3. Métricas e alertas de custo.
4. Revisão futura do fator de overfetch.
5. Revisão futura da proporção 80/20.
6. Confirmação futura do cap de 100 com dados de produção.

## Fluxo-alvo

```text
POST /search/hybrid
→ lexical search
→ vector search
→ RRF com horizonte interno H
→ Graph Expansion
→ merge e deduplicação
→ pool consolidado de até H candidatos, limitado a 100
→ HybridRerankingPipeline
→ top_k público
→ resposta HTTP
```

## Critérios para liberar implementação

A implementação pode iniciar uma fase própria após o design do adapter de
Voyage AI, da injeção de dependência, da tradução de falhas operacionais e dos
testes determinísticos que comprovem o orçamento, a deduplicação, o
preenchimento e a ausência de chamadas externas na suíte.
