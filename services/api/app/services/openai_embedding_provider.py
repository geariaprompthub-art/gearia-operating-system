"""Lazy OpenAI embedding provider; no client is created at import/startup."""
from openai import OpenAI
from app.core.config import get_settings
from app.services.embedding_provider import EMBEDDING_DIMENSIONS
class OpenAIEmbeddingProvider:
    def _client(self) -> OpenAI:
        key = get_settings().openai_api_key
        if not key: raise RuntimeError("OpenAI embedding provider is not configured")
        return OpenAI(api_key=key)
    def embed_text(self, text: str) -> list[float]: return self.embed_batch([text])[0]
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        response = self._client().embeddings.create(model="text-embedding-3-small", input=texts, dimensions=EMBEDDING_DIMENSIONS)
        vectors = [[float(item) for item in row.embedding] for row in response.data]
        if len(vectors) != len(texts) or any(len(item) != EMBEDDING_DIMENSIONS for item in vectors): raise RuntimeError("Invalid embedding response")
        return vectors
