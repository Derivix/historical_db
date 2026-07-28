"""
Configuration loading via pydantic-settings + YAML profile loader.

Settings are resolved in priority order:
  1. Environment variables (prefix: MARKET_INGEST_)
  2. config/settings.yaml
  3. Pydantic field defaults

Profiles (source-format descriptions) are separate YAML files loaded by name.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic_settings.sources.providers.dotenv import read_env_file


# ---------------------------------------------------------------------------
# Sub-models
# ---------------------------------------------------------------------------

class DatabaseSettings(BaseModel):
    dsn: str = "postgresql://postgres:password@localhost:5432/market_data"
    pool_min: int = 2
    pool_max: int = 10


class IngestSettings(BaseModel):
    batch_size: int = 10_000
    default_profile: str = "default"
    profiles_dir: str = "config/profiles"


class SessionSettings(BaseModel):
    start: str = "09:15"
    end: str = "15:30"
    timezone: str = "Asia/Kolkata"
    granularity_minutes: int = 1


class AuditSettings(BaseModel):
    max_gap_pct: float = 5.0
    missing_oi_critical: bool = False
    output_path: str = "audit_report.json"


# ---------------------------------------------------------------------------
# Root settings
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    """
    Top-level application settings.
    Environment variable override: MARKET_INGEST__DATABASE__DSN=... etc.
    """
    model_config = SettingsConfigDict(
        env_prefix="MARKET_INGEST__",
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="ignore",
    )

    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    ingest: IngestSettings = Field(default_factory=IngestSettings)
    session: SessionSettings = Field(default_factory=SessionSettings)
    audit: AuditSettings = Field(default_factory=AuditSettings)

    # Path to the settings.yaml file itself (resolved at load time)
    _config_path: Path | None = None

    @classmethod
    def from_yaml(cls, path: str | Path | None = None) -> "Settings":
        """Load settings from a YAML file, then layer environment variables on top."""
        if path is None:
            # Search for settings.yaml relative to cwd or this file's parents
            candidates = [
                Path.cwd() / "config" / "settings.yaml",
                Path(__file__).parent.parent / "config" / "settings.yaml",
            ]
            path = next((p for p in candidates if p.exists()), None)

        raw: dict[str, Any] = {}
        if path is not None:
            path = Path(path)
            if path.exists():
                with path.open("r", encoding="utf-8") as fh:
                    raw = yaml.safe_load(fh) or {}

        def merge_env_override(target: dict[str, Any], parts: list[str], value: str) -> None:
            for part in parts[:-1]:
                part = part.lower()
                if part not in target or not isinstance(target[part], dict):
                    target[part] = {}
                target = target[part]
            target[parts[-1].lower()] = value

        env_sources: list[dict[str, str | None]] = []
        dot_env_path = Path.cwd() / ".env"
        if dot_env_path.exists():
            env_sources.append(dict(read_env_file(dot_env_path, encoding="utf-8", case_sensitive=False, ignore_empty=True)))

        env_sources.append(dict(os.environ))

        for env_raw in env_sources:
            for key, value in env_raw.items():
                if value is None:
                    continue
                normalized = key.upper()
                if not normalized.startswith("MARKET_INGEST__"):
                    continue
                parts = [part for part in normalized.split("__")[1:] if part]
                if not parts:
                    continue
                merge_env_override(raw, parts, value)

        instance = cls(**raw)
        instance._config_path = path
        return instance


# ---------------------------------------------------------------------------
# Profile models
# ---------------------------------------------------------------------------

class ColumnMapProfile(BaseModel):
    """Describes how source column headers map to canonical field names."""
    granularity: str = "intraday"   # "intraday" | "daily"
    timezone: str = "Asia/Kolkata"
    datetime_format: str = "%m/%d/%Y %H:%M:%S"
    column_map: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("granularity")
    @classmethod
    def validate_granularity(cls, v: str) -> str:
        if v not in ("intraday", "daily"):
            raise ValueError(f"granularity must be 'intraday' or 'daily', got: {v!r}")
        return v


# ---------------------------------------------------------------------------
# Profile loader
# ---------------------------------------------------------------------------

def load_profile(name: str, profiles_dir: str | Path | None = None) -> ColumnMapProfile:
    """
    Load a source profile by name from the profiles directory.

    Resolution order:
      1. profiles_dir argument
      2. cwd/config/profiles/
      3. package root/config/profiles/
    """
    if profiles_dir is None:
        candidates = [
            Path.cwd() / "config" / "profiles",
            Path(__file__).parent.parent / "config" / "profiles",
        ]
        profiles_dir = next((p for p in candidates if p.exists()), Path.cwd() / "config" / "profiles")

    profiles_dir = Path(profiles_dir)
    profile_path = profiles_dir / f"{name}.yaml"

    if not profile_path.exists():
        raise FileNotFoundError(
            f"Profile {name!r} not found at {profile_path}. "
            f"Available profiles: {[p.stem for p in profiles_dir.glob('*.yaml')]}"
        )

    with profile_path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    return ColumnMapProfile(**raw)


# ---------------------------------------------------------------------------
# Module-level singleton (lazy)
# ---------------------------------------------------------------------------

_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_yaml()
    return _settings
