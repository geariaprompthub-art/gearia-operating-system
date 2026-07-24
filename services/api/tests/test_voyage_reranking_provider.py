"""Offline contract tests for the isolated Voyage reranking adapter."""

from dataclasses import dataclass
from math import inf, nan
from uuid import UUID

import pytest
from voyageai.error import (
    APIConnectionError,
    AuthenticationError,
    InvalidRequestError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)

from app.services.reranking_contracts import ProviderRerankResult, RerankCandidate
from app.services.reranking_provider_errors import (
    RerankingProviderConfigurationError,
    RerankingProviderResponseError,
    RerankingProviderUnavailableError,
)
from app.services.voyage_reranking_provider import VoyageRerankingProvider


@dataclass
class Result:
    index: object
    relevance_score: object


@dataclass
class Response:
    results: object


class FakeClient:
    def __init__(self, response: object | None = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, object]] = []

    def rerank(
        self,
        *,
        query: str,
        documents: list[str],
        model: str,
        top_k: None,
        truncation: bool,
    ) -> object:
        self.calls.append(
            {
                "query": query,
                "documents": documents,
                "model": model,
                "top_k": top_k,
                "truncation": truncation,
            }
        )
        if self.error is not None:
            raise self.error
        return self.response


class FalsyFakeClient(FakeClient):
    def __bool__(self) -> bool:
        return False


def candidate(number: int, text: str | None = None) -> RerankCandidate:
    return RerankCandidate(UUID(int=number), text or f"document-{number}", number, ("lexical",))


def provider(client: FakeClient) -> VoyageRerankingProvider:
    return VoyageRerankingProvider(api_key=None, model="rerank-2.5-lite", timeout_seconds=5, client=client)


def test_constructs_the_official_client_with_timeout_and_no_logical_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, object]] = []
    client = FakeClient(Response([Result(0, 0.5)]))

    def factory(**kwargs: object) -> FakeClient:
        created.append(kwargs)
        return client

    monkeypatch.setattr("app.services.voyage_reranking_provider.voyageai.Client", factory)
    adapter = VoyageRerankingProvider(
        api_key="secret",
        model="rerank-2.5-lite",
        timeout_seconds=5,
    )

    assert created == [{"api_key": "secret", "max_retries": 0, "timeout": 5.0}]
    assert adapter.rerank("query", [candidate(1)]) == [
        ProviderRerankResult(UUID(int=1), 0.5)
    ]
    assert len(client.calls) == 1


def test_maps_provider_order_by_original_index_and_requests_sdk_truncation() -> None:
    client = FakeClient(Response([Result(2, 0.1), Result(0, 0.9), Result(1, 0.5)]))
    values = [candidate(1, "same"), candidate(2, "same"), candidate(3, "third")]

    result = provider(client).rerank("query", values)

    assert result == [
        ProviderRerankResult(UUID(int=3), 0.1),
        ProviderRerankResult(UUID(int=1), 0.9),
        ProviderRerankResult(UUID(int=2), 0.5),
    ]
    assert client.calls == [
        {
            "query": "query",
            "documents": ["same", "same", "third"],
            "model": "rerank-2.5-lite",
            "top_k": None,
            "truncation": True,
        }
    ]


def test_empty_candidates_skip_the_provider_call() -> None:
    client = FakeClient(Response([]))

    assert provider(client).rerank("query", []) == []
    assert client.calls == []


@pytest.mark.parametrize(
    "results",
    [
        [Result(-1, 0.2), Result(1, 0.3)],
        [Result(2, 0.2), Result(1, 0.3)],
        [Result(0, 0.2), Result(0, 0.3)],
        [Result(0, "bad"), Result(1, 0.3)],
        [Result(0, True), Result(1, 0.3)],
        [Result(0, nan), Result(1, 0.3)],
        [Result(0, inf), Result(1, 0.3)],
        [Result(0, -inf), Result(1, 0.3)],
        [Result(0, 0.2)],
        [Result(0, 0.2), Result(1, 0.3), Result(1, 0.4)],
    ],
)
def test_rejects_invalid_or_incomplete_provider_results(results: list[Result]) -> None:
    with pytest.raises(RerankingProviderResponseError):
        provider(FakeClient(Response(results))).rerank("query", [candidate(1), candidate(2)])


@pytest.mark.parametrize(
    "response",
    [object(), Response(None), Response("not-a-list"), Response([object()])],
)
def test_rejects_malformed_provider_responses(response: object) -> None:
    with pytest.raises(RerankingProviderResponseError):
        provider(FakeClient(response)).rerank("query", [candidate(1)])


@pytest.mark.parametrize(
    "error",
    [
        Timeout("secret query"),
        APIConnectionError("document"),
        RateLimitError("rate limited"),
        ServiceUnavailableError("service unavailable"),
    ],
)
def test_translates_operational_sdk_errors_once_without_sensitive_details(error: Exception) -> None:
    client = FakeClient(error=error)

    with pytest.raises(RerankingProviderUnavailableError) as raised:
        provider(client).rerank("secret query", [candidate(1, "private document")])

    assert raised.value.__cause__ is error
    assert "secret" not in str(raised.value).lower()
    assert "document" not in str(raised.value).lower()
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "error",
    [AuthenticationError("secret"), InvalidRequestError("bad model")],
)
def test_translates_configuration_sdk_errors_once(error: Exception) -> None:
    client = FakeClient(error=error)

    with pytest.raises(RerankingProviderConfigurationError) as raised:
        provider(client).rerank("secret query", [candidate(1, "private document")])

    assert raised.value.__cause__ is error
    assert "secret" not in str(raised.value).lower()
    assert "document" not in str(raised.value).lower()
    assert len(client.calls) == 1


def test_does_not_mask_unexpected_programming_errors() -> None:
    error = RuntimeError("unexpected adapter bug")
    client = FakeClient(error=error)

    with pytest.raises(RuntimeError) as raised:
        provider(client).rerank("query", [candidate(1)])

    assert raised.value is error
    assert len(client.calls) == 1


def test_uses_an_injected_falsy_client_without_constructing_the_sdk_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_factory(**_: object) -> object:
        raise AssertionError("the SDK client must not be constructed")

    monkeypatch.setattr("app.services.voyage_reranking_provider.voyageai.Client", forbidden_factory)
    client = FalsyFakeClient(Response([Result(0, 0.5)]))

    result = VoyageRerankingProvider(
        api_key=None,
        model="rerank-2.5-lite",
        timeout_seconds=5,
        client=client,
    ).rerank("query", [candidate(1)])

    assert result == [ProviderRerankResult(UUID(int=1), 0.5)]
    assert len(client.calls) == 1


def test_fake_client_does_not_require_an_api_key_and_validates_configuration() -> None:
    assert provider(FakeClient(Response([Result(0, -1.0)]))).rerank(
        "query",
        [candidate(1)],
    ) == [ProviderRerankResult(UUID(int=1), -1.0)]
    with pytest.raises(RerankingProviderConfigurationError, match="configured"):
        VoyageRerankingProvider(
            api_key=None,
            model="rerank-2.5-lite",
            timeout_seconds=5,
        )
    for timeout in (0, -1, nan, inf, -inf, True, False):
        with pytest.raises(RerankingProviderConfigurationError, match="timeout"):
            VoyageRerankingProvider(
                api_key="key",
                model="rerank-2.5-lite",
                timeout_seconds=timeout,
            )
    for model in ("", "   "):
        with pytest.raises(RerankingProviderConfigurationError, match="model"):
            VoyageRerankingProvider(
                api_key="key",
                model=model,
                timeout_seconds=5,
            )
    for api_key in (None, "", "   "):
        with pytest.raises(RerankingProviderConfigurationError, match="configured"):
            VoyageRerankingProvider(
                api_key=api_key,
                model="rerank-2.5-lite",
                timeout_seconds=5,
            )
