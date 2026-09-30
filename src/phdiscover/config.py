"""
PhDiscover Engine - Configuration Management
"""

from __future__ import annotations
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseModel):
    pool_size: int = 5
    max_overflow: int = 10
    pool_timeout: int = 30
    pool_recycle: int = 3600
    echo: bool = False


class RedisSettings(BaseModel):
    max_connections: int = 10
    socket_timeout: int = 5
    socket_connect_timeout: int = 5
    decode_responses: bool = True


class CrawlingSettings(BaseModel):
    default_timeout: int = 30
    max_retries: int = 3
    retry_backoff_base: int = 2
    max_concurrent: int = 5
    respect_robots_txt: bool = True
    user_agent: str = "PhDiscover Bot/0.1 (+https://github.com/yourusername/phdiscover-engine)"
    proxy_enabled: bool = False
    proxy_list: List[str] = Field(default_factory=list)


class SemanticMatchingSettings(BaseModel):
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_dimension: int = 384
    cheap_filter_threshold: float = 0.3
    embedding_top_k: int = 500
    rerank_top_k: int = 150
    llm_review_top_k: int = 50
    cross_encoder_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"


class SchedulerSettings(BaseModel):
    crawl_schedule: str = "0 2 * * *"
    alert_schedule: str = "0 9 * * *"
    cleanup_schedule: str = "0 3 * * 0"
    max_crawl_duration_hours: int = 6


class TelegramSettings(BaseModel):
    max_message_length: int = 4096
    parse_mode: str = "HTML"
    disable_web_page_preview: bool = True
    rate_limit_per_second: int = 20


class ExportSettings(BaseModel):
    excel_max_rows: int = 10000
    chunk_size: int = 1000
    date_format: str = "%Y-%m-%d"
    datetime_format: str = "%Y-%m-%d %H:%M:%S"


class ContentSettings(BaseModel):
    seo_base_url: str = "https://phdiscover.io"
    auto_generate: bool = True
    languages: List[str] = Field(default_factory=lambda: ["en", "fa"])
    sitemap_enabled: bool = True


class RecoverySettings(BaseModel):
    max_attempts: int = 3
    backoff_multiplier: int = 2
    max_backoff_seconds: int = 300
    sqlite_path: str = "data/recovery.db"


class LoggingSettings(BaseModel):
    level: str = "INFO"
    format: str = "json"
    file: str = "logs/phdiscover.log"
    max_bytes: int = 10485760
    backup_count: int = 5


class AppSettings(BaseModel):
    name: str = "PhDiscover Engine"
    version: str = "0.1.0"
    environment: str = "development"
    debug: bool = True
    timezone: str = "Asia/Tehran"
    language: str = "fa"


class Settings(BaseSettings):
    """Main settings class - loads from .env and config YAML files"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    app: AppSettings = Field(default_factory=AppSettings)

    # Database
    database_url: str = ""
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)

    # Redis
    redis_url: str = ""
    redis: RedisSettings = Field(default_factory=RedisSettings)

    # Telegram
    telegram_bot_token: str = ""
    telegram_channel_id: str = ""

    # Search APIs
    exa_api_key: str = ""
    openalex_email: str = ""
    semantic_scholar_api_key: str = ""

    # Sub-settings
    crawling: CrawlingSettings = Field(default_factory=CrawlingSettings)
    semantic_matching: SemanticMatchingSettings = Field(default_factory=SemanticMatchingSettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    telegram: TelegramSettings = Field(default_factory=TelegramSettings)
    export: ExportSettings = Field(default_factory=ExportSettings)
    content: ContentSettings = Field(default_factory=ContentSettings)
    recovery: RecoverySettings = Field(default_factory=RecoverySettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)

    # Loaded from YAML
    countries: List[Dict[str, Any]] = Field(default_factory=list)
    sources: List[Dict[str, Any]] = Field(default_factory=list)
    weights: Dict[str, Any] = Field(default_factory=dict)
    parser_versions: Dict[str, str] = Field(default_factory=dict)

    def load_yaml_configs(self, config_dir: Path) -> None:
        """Load configuration from YAML files"""
        try:
            with open(config_dir / "countries.yaml", "r", encoding="utf-8") as f:
                self.countries = yaml.safe_load(f).get("countries", [])
        except FileNotFoundError:
            pass

        try:
            with open(config_dir / "sources.yaml", "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
                self.sources = data.get("sources", [])
                self.parser_versions = data.get("parser_versions", {})
        except FileNotFoundError:
            pass

        try:
            with open(config_dir / "weights.yaml", "r", encoding="utf-8") as f:
                self.weights = yaml.safe_load(f) or {}
        except FileNotFoundError:
            pass

        try:
            with open(config_dir / "settings.yaml", "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                if "app" in data:
                    self.app = AppSettings(**data["app"])
                if "database" in data:
                    self.database = DatabaseSettings(**data["database"])
                if "redis" in data:
                    self.redis = RedisSettings(**data["redis"])
                if "crawling" in data:
                    self.crawling = CrawlingSettings(**data["crawling"])
                if "semantic_matching" in data:
                    self.semantic_matching = SemanticMatchingSettings(**data["semantic_matching"])
                if "scheduler" in data:
                    self.scheduler = SchedulerSettings(**data["scheduler"])
                if "telegram" in data:
                    self.telegram = TelegramSettings(**data["telegram"])
                if "export" in data:
                    self.export = ExportSettings(**data["export"])
                if "content" in data:
                    self.content = ContentSettings(**data["content"])
                if "recovery" in data:
                    self.recovery = RecoverySettings(**data["recovery"])
                if "logging" in data:
                    self.logging = LoggingSettings(**data["logging"])
        except FileNotFoundError:
            pass


# Global settings instance
settings = Settings()

# Load YAML configs on import
CONFIG_DIR = Path(__file__).parent.parent.parent / "config"
settings.load_yaml_configs(CONFIG_DIR)


def get_settings() -> Settings:
    return settings


def reload_settings() -> Settings:
    settings.load_yaml_configs(CONFIG_DIR)
    return settings