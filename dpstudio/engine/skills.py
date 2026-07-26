"""
dpstudio/engine/skills.py

Loads every SKILL.md the pipeline needs and exposes their parsed YAML blocks.
No LLM calls here. This module is pure file I/O + parsing, so it should be the
first thing tested and the last thing that ever breaks.
"""
from __future__ import annotations

import re
import functools
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_YAML_BLOCK = re.compile(r"```yaml\n(.*?)\n```", re.S)


@dataclass(frozen=True)
class Skill:
    name: str
    path: Path
    text: str          # full file text, used verbatim as LLM context
    data: dict[str, Any]  # parsed machine-readable YAML block


def _parse(path: Path, name: str) -> Skill:
    text = path.read_text(encoding="utf-8")
    m = _YAML_BLOCK.search(text)
    if not m:
        raise ValueError(f"No ```yaml block found in {path}")
    data = yaml.safe_load(m.group(1))
    return Skill(name=name, path=path, text=text, data=data)


class SkillSet:
    """Loads grammar + client-dna + a chosen set of feature skills from a root dir.

    Root layout expected (matches the repo structure):
        <root>/grammar/SKILL.md
        <root>/client-dna/SKILL.md
        <root>/features/<feature>/SKILL.md
    """

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self._cache: dict[str, Skill] = {}

    def _load(self, rel_path: str, name: str) -> Skill:
        if name not in self._cache:
            self._cache[name] = _parse(self.root / rel_path, name)
        return self._cache[name]

    @property
    def grammar(self) -> Skill:
        return self._load("grammar/SKILL.md", "grammar")

    @property
    def client_dna(self) -> Skill:
        return self._load("client-dna/SKILL.md", "client-dna")

    def feature(self, feature_id: str) -> Skill:
        # grammar's own registry maps feature ids -> folder names; fall back to
        # the id itself if the registry doesn't say otherwise (e.g. "serverless").
        folder = {"liquid_clustering": "liquid-clustering"}.get(feature_id, feature_id)
        return self._load(f"features/{folder}/SKILL.md", feature_id)

    def features(self, feature_ids: list[str]) -> list[Skill]:
        return [self.feature(f) for f in feature_ids]

    def registered_features(self) -> list[str]:
        return [f["feature"] for f in self.grammar.data["feature_skills"]["registered"]]


@functools.lru_cache(maxsize=1)
def default_skillset(root: str) -> SkillSet:
    """Process-wide singleton so skill files are parsed once per cluster, not per call."""
    return SkillSet(root)
