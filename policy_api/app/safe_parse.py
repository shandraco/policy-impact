"""Parse an uploaded file in a separate process that can be killed.

A thread can't be stopped once it starts, so a crafted file could keep parsing long after the
request gave up. Here each upload is parsed in its own short-lived process with a memory and CPU
cap; on timeout the process is killed. A semaphore keeps a burst of uploads from starting dozens.

Worker side: `python -m app.safe_parse <filename> <id_pattern>`, file bytes on stdin, JSON on stdout.
"""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .documents import Clause, PolicyDoc

PARSE_TIMEOUT_S = 60
PARSE_MEMORY_BYTES = 1024 * 1024 * 1024  # 1 GiB address space per parse
MAX_PARALLEL_PARSES = 2
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent
_slots: asyncio.Semaphore | None = None


class ParseTimeout(Exception):
    pass


async def parse_isolated(filename: str, data: bytes, id_pattern: str, timeout: float = PARSE_TIMEOUT_S) -> PolicyDoc:
    """Raises ValueError for a file we can't read (message is safe to show), ParseTimeout if it took too long."""
    global _slots
    if _slots is None:
        _slots = asyncio.Semaphore(MAX_PARALLEL_PARSES)
    async with _slots:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "app.safe_parse", filename, id_pattern,
            cwd=_PACKAGE_ROOT, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(data), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise ParseTimeout() from None
    try:
        reply = json.loads(out)
    except ValueError:
        # Killed by its memory or CPU cap, or crashed.
        raise ValueError("This file couldn't be read. Try saving it again as .docx or .txt.") from None
    if "error" in reply:
        raise ValueError(reply["error"])
    doc = reply["doc"]
    doc["clauses"] = [Clause(**c) for c in doc["clauses"]]
    return PolicyDoc(**doc)


def _limit_resources() -> None:
    try:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (PARSE_MEMORY_BYTES, PARSE_MEMORY_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (PARSE_TIMEOUT_S, PARSE_TIMEOUT_S + 5))
    except (ImportError, ValueError, OSError):
        pass  # not enforceable on this OS (e.g. macOS for RLIMIT_AS); the parent's timeout still applies


def _main() -> None:
    _limit_resources()
    from .documents import parse_policy

    filename, id_pattern = sys.argv[1], sys.argv[2]
    data = sys.stdin.buffer.read()
    try:
        reply = {"doc": asdict(parse_policy(filename, data, id_pattern))}
    except ValueError as e:
        reply = {"error": str(e)}
    except Exception:
        reply = {"error": "This file couldn't be read. Try saving it again as .docx or .txt."}
    sys.stdout.write(json.dumps(reply))


if __name__ == "__main__":
    _main()
