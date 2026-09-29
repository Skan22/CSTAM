"""A stand-in for the sandbox VMs: answers on every address of the sandbox pool.

Run it inside the sandbox namespace. It replies with the address the request came from, which
in the lab identifies the gateway that forwarded it, and the Host header it was asked for.
"""

import asyncio
import sys


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")[0]
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
    except (TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        writer.close()
        return
    host = ""
    for line in head.decode("latin-1").split("\r\n")[1:]:
        if line.lower().startswith("host:"):
            host = line.split(":", 1)[1].strip()
    body = f"sandbox ok via={peer} host={host}\n".encode()
    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n"
                 + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    try:
        await writer.drain()
    finally:
        writer.close()


async def main(port: int) -> None:
    server = await asyncio.start_server(handle, "0.0.0.0", port, backlog=512)  # noqa: S104
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 80))
