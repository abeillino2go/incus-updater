import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
LOG_FILE = BASE_DIR / "incus-updater.log"

class RunLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write("")
        self._write("=" * 90)
        self._write(f"Incus updater started: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        self._write("=" * 90)

    def _write(self, text: str) -> None:
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.path.open("a", encoding="utf-8") as f:
            if text:
                f.write(f"{timestamp} {text}\n")
            else:
                f.write("\n")

    def event(self, container: str | None, message: str) -> None:
        if container:
            self._write(f"[{container}] {message}")
        else:
            self._write(message)

    def summary(self, message: str) -> None:
        self._write(message)

run_log = RunLog(LOG_FILE)
