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

    # LLM OpenAI-compatible (utilise plus tard par les agents).
    openai_api_key: str = ""
    openai_base_url: str = "http://localhost:11434/v1"
    openai_model: str = "qwen2.5"


def load_config() -> Config:
    return Config(
        rcon_host=os.getenv("RCON_HOST", "127.0.0.1"),
        rcon_port=int(os.getenv("RCON_PORT", "27015")),
        rcon_password=os.getenv("RCON_PASSWORD", "factoriollm"),
        openai_api_key=os.getenv("OPENAI_API_KEY", ""),
        openai_base_url=os.getenv("OPENAI_BASE_URL", "http://localhost:11434/v1"),
        openai_model=os.getenv("OPENAI_MODEL", "qwen2.5"),
    )