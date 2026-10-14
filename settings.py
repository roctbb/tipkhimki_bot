"""Runtime configuration. Environment variables take precedence over config.py."""

import importlib
import math
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace


def parse_ids(value):
    if isinstance(value, str):
        value = value.replace(",", " ").split()
    try:
        result = tuple(dict.fromkeys(int(item) for item in value))
    except (TypeError, ValueError):
        raise ValueError("User IDs must be positive integers separated by commas") from None
    if any(not 0 < item < 2**63 for item in result):
        raise ValueError("User IDs must be positive 64-bit integers")
    return result


@dataclass(frozen=True)
class Settings:
    token: str
    admin_ids: tuple[int, ...]
    proxy: str = ""
    database_path: Path = Path("data/bot.sqlite3")
    batch_delay: float = 120
    # Import the old hard-coded block only when creating a new database.
    initial_blocked_ids: tuple[int, ...] = (7901962333,)

    @classmethod
    def load(cls):
        legacy = SimpleNamespace()
        if "BOT_TOKEN" not in os.environ or "ADMIN_IDS" not in os.environ:
            try:
                legacy = importlib.import_module("config")
            except ModuleNotFoundError as exc:
                if exc.name != "config":
                    raise
        token = os.environ.get("BOT_TOKEN", getattr(legacy, "token", "")).strip()
        admins = parse_ids(os.environ.get("ADMIN_IDS", getattr(legacy, "admin_ids", ())))
        if not token:
            raise ValueError("Set BOT_TOKEN in the environment or token in config.py")
        if not admins:
            raise ValueError("Set at least one administrator in ADMIN_IDS or config.py")
        delay = float(os.environ.get("BATCH_DELAY_SECONDS", "120"))
        if not math.isfinite(delay) or delay < 0:
            raise ValueError("BATCH_DELAY_SECONDS must be a finite non-negative number")
        return cls(
            token=token,
            admin_ids=admins,
            proxy=os.environ.get("PROXY_URL", getattr(legacy, "proxy", "")) or "",
            database_path=Path(os.environ.get("DATABASE_PATH", "data/bot.sqlite3")),
            batch_delay=delay,
            initial_blocked_ids=parse_ids(os.environ.get("INITIAL_BLOCKED_IDS", "7901962333")),
        )
