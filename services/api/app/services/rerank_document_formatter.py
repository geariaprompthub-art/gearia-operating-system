"""Pure deterministic text formatting for reranking documents."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RerankDocument:
    """Structured, provider-neutral text fields accepted by the reranking formatter."""

    title: str | None
    summary: str | None
    category: str | None
    topics: tuple[str, ...]
    keywords: tuple[str, ...]


class RerankDocumentFormatter:
    """Render the single frozen ``rerank-document-v1`` textual representation."""

    strategy_version = "rerank-document-v1"

    def format(self, document: RerankDocument) -> str:
        """Validate and render one structured document without persistence or normalization of meaning."""

        self._validate_document(document)
        lines = [
            line
            for label, value in (
                ("Title", self._normalize_optional_text(document.title)),
                ("Summary", self._normalize_optional_text(document.summary)),
                ("Category", self._normalize_optional_text(document.category)),
                ("Topics", self._format_terms(document.topics)),
                ("Keywords", self._format_terms(document.keywords)),
            )
            if value
            for line in (f"{label}: {value}",)
        ]
        if not lines:
            raise ValueError("document must contain at least one useful field")
        return "\n".join(lines)

    @classmethod
    def _validate_document(cls, document: object) -> None:
        """Reject malformed structure before any text is emitted."""

        if type(document) is not RerankDocument:
            raise ValueError("document must be a RerankDocument")
        for name in ("title", "summary", "category"):
            value = getattr(document, name)
            if value is not None and type(value) is not str:
                raise ValueError(f"document {name} must be a string or None")
        cls._validate_terms(document.topics, "topics")
        cls._validate_terms(document.keywords, "keywords")

    @classmethod
    def _validate_terms(cls, terms: object, name: str) -> None:
        """Validate ordered tuple terms without silently coercing or deduplicating them."""

        if type(terms) is not tuple:
            raise ValueError(f"document {name} must be a tuple")
        normalized_terms: set[str] = set()
        for term in terms:
            if type(term) is not str:
                raise ValueError(f"document {name} items must be strings")
            normalized = cls._normalize_text(term)
            if not normalized:
                raise ValueError(f"document {name} items must not be blank")
            if normalized in normalized_terms:
                raise ValueError(f"document {name} items must not contain normalized duplicates")
            normalized_terms.add(normalized)

    @staticmethod
    def _normalize_text(value: str) -> str:
        """Collapse Unicode whitespace deterministically while preserving content semantics."""

        return " ".join(value.split())

    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str:
        return "" if value is None else cls._normalize_text(value)

    @classmethod
    def _format_terms(cls, terms: tuple[str, ...]) -> str:
        return ", ".join(cls._normalize_text(term) for term in terms)
