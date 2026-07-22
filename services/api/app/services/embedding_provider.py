"""Provider contract and deterministic text/hash helpers."""
from hashlib import sha256
from typing import Protocol
from app.models.content import Content

EMBEDDING_DIMENSIONS = 1536
TEXT_STRATEGY_VERSION = "content-text-v1"
class EmbeddingProvider(Protocol):
    def embed_text(self, text: str) -> list[float]: ...
    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...

def build_content_embedding_text(content: Content, maximum: int = 20000) -> str:
    def clean(value: object) -> str: return " ".join(str(value or "").split())
    def tokens(values: list[str] | None) -> str: return ", ".join(sorted({clean(v) for v in (values or []) if clean(v)}))
    text = "\n".join((f"Title: {clean(content.title)}", f"Category: {clean(content.category)}", f"Topics: {tokens(content.topics)}", f"Keywords: {tokens(content.keywords)}", f"Summary: {clean(content.summary)}", f"Language: {clean(content.language)}"))
    return text[:maximum]
def content_hash(text: str) -> str: return sha256(text.encode("utf-8")).hexdigest()
