"""Minimal MemPalace config surface used by the evaluation adapter."""

import json
import os
from pathlib import Path

DEFAULT_PALACE_PATH = os.path.expanduser("~/.mempalace/palace")

DEFAULT_HALL_KEYWORDS = {
    "emotions": ["scared", "afraid", "worried", "happy", "sad", "love", "hate", "feel"],
    "consciousness": ["consciousness", "conscious", "aware", "real", "genuine", "alive"],
    "memory": ["memory", "remember", "forget", "recall", "archive", "palace", "store"],
    "technical": ["code", "python", "script", "bug", "error", "function", "api", "database", "server"],
    "identity": ["identity", "name", "who am i", "persona", "self"],
    "family": ["family", "kids", "children", "daughter", "son", "parent", "mother", "father"],
    "creative": ["game", "gameplay", "player", "app", "design", "art", "music", "story"],
}


class MempalaceConfig:
    """Minimal configuration manager used by `convo_miner`."""

    def __init__(self, config_dir=None):
        self._config_dir = (
            Path(config_dir) if config_dir else Path(os.path.expanduser("~/.mempalace"))
        )
        self._config_file = self._config_dir / "config.json"
        self._file_config = {}

        if self._config_file.exists():
            try:
                with open(self._config_file, "r", encoding="utf-8") as f:
                    self._file_config = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._file_config = {}

    @property
    def palace_path(self):
        env_val = os.environ.get("MEMPALACE_PALACE_PATH") or os.environ.get("MEMPAL_PALACE_PATH")
        if env_val:
            return env_val
        return self._file_config.get("palace_path", DEFAULT_PALACE_PATH)

    @property
    def hall_keywords(self):
        return self._file_config.get("hall_keywords", DEFAULT_HALL_KEYWORDS)
