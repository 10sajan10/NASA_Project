"""Run logging helpers.

The run logger tees stdout/stderr to both the terminal and a stable log file,
then provides stage markers with elapsed time. Existing `print(...)` calls in
drivers/models are captured without forcing every module to switch to Python's
logging package.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import sys
import time
from typing import Iterator, TextIO


class TeeStream:
    def __init__(self, *streams: TextIO):
        self.streams = streams

    def write(self, data: str) -> int:
        for stream in self.streams:
            stream.write(data)
            stream.flush()
        return len(data)

    def flush(self) -> None:
        for stream in self.streams:
            stream.flush()

    def isatty(self) -> bool:
        return any(getattr(stream, "isatty", lambda: False)()
                   for stream in self.streams)

    @property
    def encoding(self):
        return getattr(self.streams[0], "encoding", "utf-8")

    @property
    def errors(self):
        return getattr(self.streams[0], "errors", "replace")


@dataclass
class RunLogger:
    path: Path
    run_id: str
    _file: TextIO
    _stdout: TextIO
    _stderr: TextIO
    _closed: bool = False

    def install(self) -> None:
        sys.stdout = TeeStream(self._stdout, self._file)  # type: ignore[assignment]
        sys.stderr = TeeStream(self._stderr, self._file)  # type: ignore[assignment]
        self.log(f"RUN START id={self.run_id}")
        self.log(f"log_file={self.path}")

    def close(self) -> None:
        if self._closed:
            return
        self.log(f"RUN END id={self.run_id}")
        sys.stdout = self._stdout
        sys.stderr = self._stderr
        self._file.close()
        self._closed = True

    def log(self, message: str) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"[run_epoch] {now}  {message}", flush=True)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        start = time.monotonic()
        self.log(f"STAGE START: {name}")
        try:
            yield
        except Exception as exc:
            elapsed = time.monotonic() - start
            self.log(f"STAGE FAIL: {name} after {elapsed:.2f}s ({exc})")
            raise
        else:
            elapsed = time.monotonic() - start
            self.log(f"STAGE END: {name} after {elapsed:.2f}s")


def start_run_epoch_log(log_file: str | Path = "logs/run_epoch.log"
                        ) -> RunLogger:
    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fh = path.open("a", buffering=1, encoding="utf-8")
    fh.write("\n" + "=" * 80 + "\n")
    logger = RunLogger(path=path, run_id=run_id, _file=fh,
                       _stdout=sys.stdout, _stderr=sys.stderr)
    logger.install()
    return logger
