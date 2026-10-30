"""FastAPI application configuration using Pydantic settings."""
from pathlib import Path

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    # API Keys
    esp_api_key: str = ""  # EskomSePush API key

    # Application settings
    log_level: str = "INFO"
    reload: bool = False

    # Database
    db_path: str = str(Path(__file__).parent.parent / "data" / "myciti.duckdb")

    # Cache settings
    cache_ttl: int = 300  # 5 minutes default TTL
    connection_pool_size: int = 5

    # Timezone
    timezone: str = "Africa/Johannesburg"

    class Config:  # pylint: disable=too-few-public-methods
        """pydantic-settings options: read .env, case-insensitive names."""

        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False


settings = Settings()
