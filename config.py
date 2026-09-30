import tomllib
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.toml"

@dataclass
class Config:
    max_started_containers: int = 2
    max_downloads: int = 1
    startup_delay_seconds: int = 10
    incus_snapshots: bool = True  # NEU: Standardwert ist True
    include: set[str] = field(default_factory=set)
    exclude: set[str] = field(default_factory=set)

def load_config() -> Config:
    if not CONFIG_FILE.exists():
        raise RuntimeError(f"Configuration file not found: {CONFIG_FILE}")

    with CONFIG_FILE.open("rb") as f:
        data = tomllib.load(f)

    general = data.get("general", {})
    containers = data.get("containers", {})

    max_started = int(general.get("max_started_containers", 2))
    max_downloads = int(general.get("max_downloads", 1))
    startup_delay = int(general.get("startup_delay_seconds", 10))
	# NEU: Liest den Boolean aus, Fallback ist True, falls das Feld fehlt
    incus_snapshots = bool(general.get("incus_snapshots", True))

    if max_started < 1: raise ValueError("max_started_containers must be >= 1")
    if max_downloads < 1: raise ValueError("max_downloads must be >= 1")
    if startup_delay < 0: raise ValueError("startup_delay_seconds must be >= 0")

    return Config(
        max_started_containers=max_started,
        max_downloads=max_downloads,
        startup_delay_seconds=startup_delay,
        incus_snapshots=incus_snapshots,  # NEU
        include={str(x) for x in containers.get("include", [])},
        exclude={str(x) for x in containers.get("exclude", [])},
    )
