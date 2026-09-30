import time
from dataclasses import dataclass
from output import output
from logger import run_log

@dataclass
class Container:
    name: str
    original_running: bool
    distro_id: str = "?"
    distro_name: str = "?"
    package_manager: str = "?"
    status: str = "DISCOVERED"
    error: str | None = None
    started_by_us: bool = False

    def log_event(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        output.write(self.name, f"{timestamp} {message}")
        run_log.event(self.name, message)

    def set_status(self, status: str) -> None:
        self.status = status
        output.status(self.name, status)
