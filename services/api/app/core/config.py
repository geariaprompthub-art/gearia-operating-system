from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "GearIA Operating System API"
    environment: str = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://gearia:gearia@localhost:5432/gearia"
    redis_url: str = "redis://localhost:6379/0"
    openai_api_key: str | None = None
    embedding_provider: str = "openai"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding_text_strategy_version: str = "content-text-v1"
    embedding_distance_metric: str = "cosine"
    embedding_batch_size: int = Field(default=50, ge=1, le=500)
    embedding_max_input_characters: int = Field(default=20000, ge=1, le=20000)
    hybrid_lexical_candidate_k: int = Field(default=50, ge=1, le=500)
    hybrid_vector_candidate_k: int = Field(default=50, ge=1, le=500)
    hybrid_rrf_k: int = Field(default=60, ge=1, le=1000)

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


@lru_cache
def get_settings() -> Settings:
    return Settings()
