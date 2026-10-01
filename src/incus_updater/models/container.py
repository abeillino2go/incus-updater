import time
from dataclasses import dataclass
from incus_updater.output import output
from incus_updater.logger import run_log

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
    update_count: int = 0

    def log_event(self, message: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        output.write(self.name, f"{timestamp} {message}")
        run_log.event(self.name, message)

    def set_status(self, status: str) -> None:
        self.status = status
        output.status(self.name, status)
 
        # KORREKTUR: call_next nutzen statt call_from_thread, da wir im async Event-Loop laufen
        if output.ui is not None:
            output.ui.call_next(output.ui.update_container_table_row, self)
