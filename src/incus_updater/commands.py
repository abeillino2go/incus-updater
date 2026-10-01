import asyncio
from incus_updater.output import output

COMMAND_TIMEOUT = 3600

class CommandError(RuntimeError):
    pass

async def run_command(*args: str, timeout: int = COMMAND_TIMEOUT, container_name: str | None = None) -> int:
    process = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    async def consume_output() -> None:
        assert process.stdout is not None
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace")
            output.write(container_name, text)

    try:
        await asyncio.wait_for(consume_output(), timeout=timeout)
        returncode = await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        raise CommandError(f"Command timed out after {timeout}s: {' '.join(args)}")

    if returncode != 0:
        raise CommandError(f"Command failed with exit code {returncode}: {' '.join(args)}")
    return returncode

async def incus_exec(container, *command: str) -> None:
    await run_command("incus", "exec", container.name, "--", *command, container_name=container.name)

async def incus_command(*args: str) -> None:
    await run_command("incus", *args)
