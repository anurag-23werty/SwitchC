"""Runtime settings, read from the environment (and .env)."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # Strip whitespace: `docker run --env-file` keeps trailing spaces python-dotenv drops,
    # and "openai/gpt-oss-120b " is a model that does not exist.
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", extra="ignore", str_strip_whitespace=True
    )

    switch_api_url: str = "http://localhost:8000"
    switch_registration_token: str
    switch_gateway_url: str = "http://localhost:8000"
    switch_gateway_email: str = "admin@switch.local"
    switch_gateway_password: str = ""

    khata_database_url: str = "postgresql://khata:khata@localhost:5433/khata"

    groq_api_key: str = ""
    groq_model: str = ""
    # A different model family on Groq: the second reader's last resort.
    groq_secondary_model: str = "qwen/qwen3.8-27b"
    groq_guard_model: str = "meta-llama/llama-prompt-guard-2-86m"
    openrouter_api_key: str = ""
    openrouter_model: str = ""
    # Tried in order when the free model above is rate-limited upstream.
    openrouter_fallback_models: str = (
        "nvidia/nemotron-3.5-super-vl:free,xingchen-agi/xing4.0-29b-a4b:free"
    )
    llm_cache_dir: Path = ROOT / ".cache" / "llm"
    payments_dir: Path = ROOT / "data" / "out" / "payments"

    agent_keys_path: Path = ROOT / ".khata-agents.json"


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # required fields come from env
