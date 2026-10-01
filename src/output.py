from __future__ import annotations

class Output:
    def __init__(self) -> None:
        self.ui = None

    def attach(self, ui) -> None:
        self.ui = ui

    def detach(self) -> None:
        self.ui = None

    def write(self, container_name: str | None, text: str) -> None:
        if self.ui is not None and container_name:
            self.ui.write(container_name, text.rstrip("\n"))
            return
        print(text, end="" if text.endswith("\n") else "\n", flush=True)

    def status(self, container_name: str, status: str) -> None:
        if self.ui is not None:
            self.ui.status(container_name, status)
        else:
            print(f"{container_name:<30} {status}", flush=True)

    def global_message(self, text: str) -> None:
        if self.ui is not None:
            self.ui.global_message(text)
        else:
            print(text, flush=True)

output = Output()
