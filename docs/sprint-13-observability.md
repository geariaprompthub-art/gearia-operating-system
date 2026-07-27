# Sprint 13 — Observabilidade do Hybrid Search

## Fluxo e ownership

Uma requisição bem-sucedida produz, na ordem lógica: `http_request_started`,
`hybrid_search_started`, pares de eventos de estágio da pipeline,
`hybrid_search_completed` e `http_request_completed`.

Uma falha 5xx substitui o evento terminal de sucesso por `http_request_failed`.
Uma falha da pipeline também produz um único `hybrid_pipeline_stage_failed` para o
estágio que a propagou e um único `hybrid_search_failed`.

| Evento | Responsável |
| --- | --- |
| `http_request_*` | `RequestCorrelationMiddleware` |
| `hybrid_search_*` | `HybridSearchService` |
| `hybrid_pipeline_stage_*` | `HybridRerankingPipeline` |

Componentes internos — repositories, provider, RRF, Graph, pool, eligibility,
hydration e formatter — não emitem logs de domínio.

## Dados e segurança

Os eventos aceitam somente metadados de operação: `stage`, `duration_ms`, contagens
agregadas e `error_type`. Não devem registrar query, documentos, IDs individuais,
vetores, payloads de provider, prompts, headers, cookies ou mensagens de exceção.
`SafeStructuredLogger` sanitiza e limita valores como defesa adicional; a regra
principal é não encaminhar dados proibidos à chamada de logging.

## Convenções

- Todos os nomes de evento pertencem a `app.core.log_events.LogEvent`.
- Durações usam `perf_counter()` e são emitidas em milissegundos como `duration_ms`.
- Logging é fail-open exclusivamente pela fronteira `SafeStructuredLogger`.
- Telemetria também é fail-open e não substitui falhas funcionais.
- Um estágio tem exatamente um evento inicial e exatamente um evento terminal.
- `CorrelationContext` é fornecido pelo middleware; serviços não geram IDs.

## Benchmark

Medições de latência da Sprint 13 são diagnósticas de regressão local. Elas não
representam capacidade, SLO ou desempenho de produção.

## Checklist para novo evento

1. Definir ownership em uma única camada.
2. Adicionar a constante centralizada antes de qualquer uso.
3. Registrar apenas metadados agregados e não sensíveis.
4. Preservar `request_id`, exceções e contratos HTTP.
5. Cobrir sucesso, falha, fail-open, segurança e concorrência.
6. Não adicionar logs internos quando o owner já cobre o ciclo de vida.
