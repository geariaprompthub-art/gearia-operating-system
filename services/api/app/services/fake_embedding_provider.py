"""Deterministic, network-free embedding provider for isolated tests."""
from hashlib import sha256
from app.services.embedding_provider import EMBEDDING_DIMENSIONS
class FakeEmbeddingProvider:
    def __init__(self, fail: bool = False, dimensions: int = EMBEDDING_DIMENSIONS, fail_indices: set[int] | None = None, vector_values: list[float] | None = None) -> None:
        self.calls = 0; self.fail = fail; self.dimensions = dimensions; self.fail_indices=fail_indices or set(); self.vector_values=vector_values; self.inputs: list[str]=[]; self.failed_indices: list[int]=[]
    def embed_text(self, text: str) -> list[float]: return self.embed_batch([text])[0]
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        vectors=[]
        for text in texts:
            index=self.calls; self.calls += 1; self.inputs.append(text)
            if self.fail or index in self.fail_indices: self.failed_indices.append(index); raise RuntimeError("controlled embedding provider failure")
            vectors.append(list(self.vector_values) if self.vector_values is not None else [((sha256(f"{text}:{value}".encode()).digest()[0] / 255) * 2 - 1) for value in range(self.dimensions)])
        return vectors
