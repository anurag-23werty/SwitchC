"""Runtime settings, read from the environment (and .env)."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    switch_api_url: str = "http://localhost:8000"
    switch_registration_token: str
    switch_gateway_url: str = "http://localhost:8000"
    switch_gateway_email: str = "admin@switch.local"
    switch_gateway_password: str = ""

    khata_database_url: str = "postgresql://khata:khata@localhost:5433/khata"

    groq_api_key: str = ""
    groq_model: str = ""
    openrouter_api_key: str = ""
    openrouter_model: str = ""

    agent_keys_path: Path = ROOT / ".khata-agents.json"


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # required fields come from env
