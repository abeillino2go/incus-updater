import sys
import tomllib
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path


@dataclass
class Config:
    max_started_containers: int = 2
    max_downloads: int = 1
    startup_delay_seconds: int = 10
    include: set[str] = field(default_factory=set)
    exclude: set[str] = field(default_factory=set)
    incus_snapshots: bool = True


def load_config(custom_path: str | None = None) -> Config:
    """Lädt die Konfiguration. Priorisiert custom_path, nutzt sonst den Default."""

    if custom_path is not None:
        config_path = Path(custom_path).resolve()

        if not config_path.exists():
            print(
                f"ERROR: Configuration file not found: {config_path}",
                file=sys.stderr,
            )
            sys.exit(1)

        with config_path.open("rb") as f:
            data = tomllib.load(f)

    else:
        config_file = files("incus_updater").joinpath("config.toml")

        with config_file.open("rb") as f:
            data = tomllib.load(f)

    general = data.get("general", {})
    containers = data.get("containers", {})

    max_started = int(general.get("max_started_containers", 2))
    max_downloads = int(general.get("max_downloads", 1))
    startup_delay = int(general.get("startup_delay_seconds", 10))
    incus_snapshots = bool(general.get("incus_snapshots", True))

    if max_started < 1:
        raise ValueError("max_started_containers must be >= 1")
    if max_downloads < 1:
        raise ValueError("max_downloads must be >= 1")
    if startup_delay < 0:
        raise ValueError("startup_delay_seconds must be >= 0")

    return Config(
        max_started_containers=max_started,
        max_downloads=max_downloads,
        startup_delay_seconds=startup_delay,
        incus_snapshots=incus_snapshots, 
        include={str(x) for x in containers.get("include", [])},
        exclude={str(x) for x in containers.get("exclude", [])},
    )
