"""Pure unit tests for the frozen rerank-document-v1 formatter."""

from dataclasses import fields

import pytest

from app.services.rerank_document_formatter import RerankDocument, RerankDocumentFormatter


def _document(
    title: str | None = None,
    summary: str | None = None,
    category: str | None = None,
    topics: tuple[str, ...] = (),
    keywords: tuple[str, ...] = (),
) -> RerankDocument:
    return RerankDocument(title, summary, category, topics, keywords)


def test_formats_complete_document_in_frozen_field_order() -> None:
    document = _document("Title", "Summary", "Category", ("Topic one", "Topic two"), ("Keyword one", "Keyword two"))
    assert RerankDocumentFormatter().format(document) == (
        "Title: Title\nSummary: Summary\nCategory: Category\n"
        "Topics: Topic one, Topic two\nKeywords: Keyword one, Keyword two"
    )


@pytest.mark.parametrize(
    ("document", "expected"),
    [
        (_document(title="Only title"), "Title: Only title"),
        (_document(summary="Only summary"), "Summary: Only summary"),
        (_document(category="Only category"), "Category: Only category"),
        (_document(topics=("Only topic",)), "Topics: Only topic"),
        (_document(keywords=("Only keyword",)), "Keywords: Only keyword"),
        (_document(None, None, None, (), ()), None),
    ],
)
def test_formats_each_supported_field_or_rejects_completely_empty_document(
    document: RerankDocument, expected: str | None
) -> None:
    formatter = RerankDocumentFormatter()
    if expected is None:
        with pytest.raises(ValueError, match="useful field"):
            formatter.format(document)
    else:
        assert formatter.format(document) == expected


def test_omits_none_empty_and_whitespace_optional_fields_without_blank_lines() -> None:
    document = _document(title="  ", summary=None, category="\t", topics=("Topic",), keywords=())
    result = RerankDocumentFormatter().format(document)
    assert result == "Topics: Topic"
    assert "\n\n" not in result and not result.endswith("\n")


def test_normalizes_whitespace_preserves_unicode_casing_punctuation_and_emoji() -> None:
    document = _document(
        "  IA\n  aplicada\tà prática!  ",
        "  MIXED Case — café ☕\n",
        "  Ciência & Dados  ",
        ("  Engenharia\tde prompt ",),
        ("  AÇÃO  ",),
    )
    assert RerankDocumentFormatter().format(document) == (
        "Title: IA aplicada à prática!\nSummary: MIXED Case — café ☕\nCategory: Ciência & Dados\n"
        "Topics: Engenharia de prompt\nKeywords: AÇÃO"
    )


def test_preserves_tuple_order_and_uses_exact_separators() -> None:
    document = _document(topics=("Second", "First"), keywords=("Beta", "Alpha"))
    result = RerankDocumentFormatter().format(document)
    assert result == "Topics: Second, First\nKeywords: Beta, Alpha"
    assert result.count("\n") == 1 and ", " in result


def test_formatter_is_repeatable_between_instances_and_does_not_mutate_document() -> None:
    document = _document("  Title  ", topics=(" First\tterm ", "Second term"))
    original = document
    first = RerankDocumentFormatter().format(document)
    second = RerankDocumentFormatter().format(document)
    third = RerankDocumentFormatter().format(original)
    assert first == second == third == "Title: Title\nTopics: First term, Second term"
    assert document == original


def test_accepts_long_text_without_truncation() -> None:
    title = "x" * 50_000
    assert RerankDocumentFormatter().format(_document(title=title)) == f"Title: {title}"


@pytest.mark.parametrize(
    "document",
    [
        None,
        object(),
        {"title": "duck typed"},
        _document(title=1),
        _document(summary=1),
        _document(category=1),
        _document(topics=["topic"]),
        _document(keywords=["keyword"]),
        _document(topics="topic"),
        _document(keywords=b"keyword"),
        _document(topics=(None,)),
        _document(keywords=(b"keyword",)),
        _document(topics=(1,)),
        _document(topics=("",)),
        _document(keywords=(" \t\n",)),
        _document(topics=("Topic", "Topic")),
        _document(keywords=("Keyword", " Keyword\t")),
    ],
)
def test_rejects_invalid_document_structures_and_terms(document: object) -> None:
    with pytest.raises(ValueError):
        RerankDocumentFormatter().format(document)  # type: ignore[arg-type]


def test_strategy_and_dto_structure_exclude_forbidden_fields() -> None:
    assert RerankDocumentFormatter.strategy_version == "rerank-document-v1"
    assert {field.name for field in fields(RerankDocument)} == {
        "title", "summary", "category", "topics", "keywords"
    }
    forbidden = {
        "content_id", "url", "raw_payload", "embedding", "processing_status", "source_id",
        "score", "rank", "matched_by", "provider", "token_usage", "timestamps",
    }
    assert not (forbidden & {field.name for field in fields(RerankDocument)})
