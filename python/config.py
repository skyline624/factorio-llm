"""Configuration du pilote factorio-llm.

Charge les variables depuis python/.env (via python-dotenv) puis depuis l'environnement.
Les valeurs RCON doivent correspondre aux flags de lancement de Factorio.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


@dataclass
class Config:
    # Acces RCON commun a tous les agents.
    rcon_host: str = "127.0.0.1"
    rcon_port: int = 27015
    rcon_password: str = "factoriollm"

    # LLM OpenAI-compatible (Ollama / LM Studio / vLLM / OpenAI / custom).
    # Utilise par FactoryBuilder P1b (decision LLM plan structure).
    openai_api_key: str = ""
    openai_base_url: str = "http://localhost:11434/v1"
    openai_model: str = "glm-5.2:cloud"
    llm_timeout: float = 30.0
    llm_max_tokens: int = 2048
    llm_enabled: bool = True


def load_config() -> Config:
    return Config(
        rcon_host=os.getenv("RCON_HOST", "127.0.0.1"),
        rcon_port=int(os.getenv("RCON_PORT", "27015")),
        rcon_password=os.getenv("RCON_PASSWORD", "factoriollm"),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_base_url=os.getenv("OPENAI_BASE_URL", "http://localhost:11434/v1"),
        openai_model=os.getenv("OPENAI_MODEL", "glm-5.2:cloud"),
        llm_timeout=float(os.getenv("LLM_TIMEOUT", "30.0")),
        llm_max_tokens=int(os.getenv("LLM_MAX_TOKENS", "2048")),
        llm_enabled=os.getenv("LLM_ENABLED", "true").lower() in ("1", "true", "yes"),
    )