"""Snapshot d'etat partage entre agents.

Les agents utilisent GameState (dataclass) comme vue typée de l'etat renvoye
par fl_tools.get_state. Le coordinateur peut diffuser un snapshot unique a
plusieurs agents pour eviter des appels RCON redondants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class CharacterState:
    x: float
    y: float
    health: float
    max_health: float
    walking: bool


@dataclass
class GameState:
    tick: int
    ready: bool
    test_mode: bool
    character: CharacterState | None
    home_position: dict | None
    inventory: dict[str, int]
    surface: str | None
    task: dict[str, Any]

    @classmethod
    def from_dict(cls, d: dict) -> "GameState":
        ch = d.get("character")
        character = None
        if ch:
            pos = ch.get("position", {})
            character = CharacterState(
                x=pos.get("x", 0.0),
                y=pos.get("y", 0.0),
                health=ch.get("health", 0.0),
                max_health=ch.get("max_health", 100.0),
                walking=ch.get("walking", False),
            )
        return cls(
            tick=d.get("tick", -1),
            ready=d.get("ready", False),
            test_mode=d.get("test_mode", False),
            character=character,
            home_position=d.get("home_position"),
            inventory=d.get("inventory", {}) or {},
            surface=d.get("surface"),
            task=d.get("task", {}) or {},
        )

    def pos_tuple(self) -> tuple[float, float] | None:
        if self.character is None:
            return None
        return (self.character.x, self.character.y)