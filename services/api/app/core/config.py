from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "GearIA Operating System API"
    environment: str = "development"
    debug: bool = False
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://gearia:gearia@localhost:5432/gearia"
    redis_url: str = "redis://localhost:6379/0"
    redis_required: bool = True
    trusted_hosts: list[str] = Field(default_factory=lambda: ["localhost", "127.0.0.1", "test", "testserver"])
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    cors_allow_credentials: bool = False
    docs_enabled: bool = True
    health_timeout_seconds: float = Field(default=2.0, gt=0, le=10)
    scout_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    scout_read_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    scout_max_response_bytes: int = Field(default=2_000_000, ge=1_024, le=20_000_000)
    scout_max_redirects: int = Field(default=3, ge=0, le=10)
    scout_max_entries_per_feed: int = Field(default=100, ge=1, le=1_000)
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
    voyage_api_key: str | None = None
    voyage_rerank_model: str = "rerank-2.5"
    voyage_rerank_timeout_seconds: float = Field(default=10.0, gt=0)
    hybrid_search_telemetry_enabled: bool = True
    structured_logging_enabled: bool = True

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        """Reject insecure production defaults without constraining local development."""

        if self.environment.lower() != "production":
            return self
        if self.debug:
            raise ValueError("debug must be disabled in production")
        if "*" in self.trusted_hosts:
            raise ValueError("trusted_hosts must not contain wildcard in production")
        if not self.trusted_hosts:
            raise ValueError("trusted_hosts must be configured in production")
        if not self.cors_origins:
            raise ValueError("cors_origins must be configured in production")
        if "*" in self.cors_origins and self.cors_allow_credentials:
            raise ValueError("CORS wildcard cannot be combined with credentials")
        if "gearia:gearia@" in self.database_url:
            raise ValueError("database_url must not use local default credentials in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
