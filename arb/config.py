"""Carica config.toml e .env dalla root del progetto."""

import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


def load_dotenv(path: Path = ROOT / ".env") -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def load_config(path: Path = ROOT / "config.toml") -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def taker_fee(cfg: dict, exchange: str) -> float:
    return float(cfg.get("fees", {}).get("taker", {}).get(exchange, 0.0005))


TRADING_MODES = ("notify", "execute")


def trading_mode() -> str:
    """`notify` (default): Claude manda istruzioni, l'utente opera su VOOI.
    `execute`: proposte con Approva/Rifiuta, Claude esegue via MCP dopo il tap."""
    load_dotenv()
    mode = (os.environ.get("TRADING_MODE") or "notify").strip().lower()
    if mode not in TRADING_MODES:
        raise ValueError(f"TRADING_MODE={mode!r} non valido: usa notify o execute")
    return mode
