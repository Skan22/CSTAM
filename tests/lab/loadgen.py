"""A steady request stream through the VIP that records every failed or slow request.

Each request opens a new TCP connection, like a browser's first request, so a dead VIP shows up
as a failure within the timeout instead of hiding behind a kept-alive connection.
"""

import asyncio
import re
import time
from dataclasses import dataclass, field


@dataclass
class Sample:
    at: float          # seconds since the run started, when the request was sent
    latency: float     # seconds until the response completed (or the timeout)
    ok: bool
    via: str = ""      # the address the sandbox saw, which identifies the forwarding gateway
    error: str = ""


@dataclass
class Outage:
    start: float
    end: float

    @property
    def seconds(self) -> float:
        return self.end - self.start


@dataclass
class Report:
    samples: list[Sample]
    slow_after: float
    outages: list[Outage] = field(default_factory=list)

    @property
    def failed(self) -> list[Sample]:
        return [s for s in self.samples if not s.ok]

    @property
    def slow(self) -> list[Sample]:
        return [s for s in self.samples if s.ok and s.latency > self.slow_after]

    @property
    def worst_gap(self) -> float:
        """The longest stretch with no successful response: the outage a user would feel."""
        return max((o.seconds for o in self.outages), default=0.0)

    def summary(self) -> dict[str, object]:
        return {
            "requests": len(self.samples), "failed": len(self.failed), "slow": len(self.slow),
            "worst_gap_s": round(self.worst_gap, 3),
            "outages": [{"start": round(o.start, 3), "end": round(o.end, 3),
                         "seconds": round(o.seconds, 3)} for o in self.outages],
            "failures": [{"at": round(s.at, 3), "latency": round(s.latency, 3), "error": s.error}
                         for s in self.failed[:50]],
            "vias": sorted({s.via for s in self.samples if s.via}),
        }


def outages(samples: list[Sample]) -> list[Outage]:
    """Windows in which requests were sent but none succeeded.

    Measured in send time: from the last request that succeeded to the first request after it
    that succeeded again, so a request that hung for a second before failing still counts from
    the moment it was sent.
    """
    ordered = sorted(samples, key=lambda s: s.at)
    result: list[Outage] = []
    last_ok: float | None = None
    failing = False
    for s in ordered:
        if s.ok:
            if failing and last_ok is not None:
                result.append(Outage(last_ok, s.at))
            failing = False
            last_ok = s.at
        else:
            failing = True
    if failing and last_ok is not None:
        result.append(Outage(last_ok, ordered[-1].at))
    return result


class LoadGen:
    def __init__(self, host: str, target: str, *, port: int = 80, rate: float = 50,
                 timeout: float = 1.0, slow_after: float = 0.25, source: str | None = None) -> None:
        self.host, self.target, self.port = host, target, port
        self.rate, self.timeout, self.slow_after, self.source = rate, timeout, slow_after, source
        self.samples: list[Sample] = []
        self.t0 = 0.0
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def now(self) -> float:
        return time.monotonic() - self.t0

    async def _one(self, at: float) -> None:
        start = time.monotonic()
        via, err, ok = "", "", False
        try:
            async with asyncio.timeout(self.timeout):
                reader, writer = await asyncio.open_connection(
                    self.target, self.port, local_addr=(self.source, 0) if self.source else None)
                writer.write(f"GET / HTTP/1.1\r\nHost: {self.host}\r\nConnection: close\r\n\r\n".encode())
                await writer.drain()
                raw = await reader.read(4096)
                writer.close()
            head, _, body = raw.decode("latin-1").partition("\r\n\r\n")
            ok = head.startswith("HTTP/1.1 200")
            m = re.search(r"via=(\S+)", body)
            via = m.group(1) if m else ""
            if not ok:
                err = head.split("\r\n", 1)[0]
        except TimeoutError:
            err = "timeout"
        except OSError as e:
            err = type(e).__name__
        self.samples.append(Sample(at, time.monotonic() - start, ok, via, err))

    async def _run(self) -> None:
        interval = 1 / self.rate
        pending: set[asyncio.Task[None]] = set()
        n = 0
        while not self._stop.is_set():
            t = asyncio.create_task(self._one(self.now()))
            pending.add(t)
            t.add_done_callback(pending.discard)
            n += 1
            delay = self.t0 + n * interval - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        await asyncio.gather(*pending)

    def start(self) -> None:
        self.t0 = time.monotonic()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> Report:
        self._stop.set()
        assert self._task
        await self._task
        return Report(self.samples, self.slow_after, outages(self.samples))
