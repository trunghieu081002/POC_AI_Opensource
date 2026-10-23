"""Forward one TCP port, for watching a demo host from another machine (docs/demo.md, "Live show").

  port_forward.py LISTEN_HOST:PORT TARGET_HOST:PORT

Superset in the demo listens on 127.0.0.1:8088 inside its host (by design: the superset pack's bind_host). On a
Docker demo host, run this inside the container to expose it on the container's bridge address only, e.g.
`0.0.0.0:18088 -> 127.0.0.1:8088`; the bridge is reachable from the Docker machine, and from a laptop through an SSH
tunnel to that machine. It changes no configuration and stops with the process.
"""
import asyncio
import sys


async def pipe(reader, writer):
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def main(listen: str, target: str) -> None:
    lh, lp = listen.rsplit(":", 1)
    th, tp = target.rsplit(":", 1)

    async def handle(client_r, client_w):
        try:
            up_r, up_w = await asyncio.open_connection(th, int(tp))
        except OSError:
            client_w.close()
            return
        await asyncio.gather(pipe(client_r, up_w), pipe(up_r, client_w), return_exceptions=True)

    server = await asyncio.start_server(handle, lh, int(lp))
    print(f"forwarding {listen} -> {target}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__, file=sys.stderr)
        raise SystemExit(2)
    asyncio.run(main(sys.argv[1], sys.argv[2]))
