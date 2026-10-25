#!/usr/bin/env python3
"""Offline headless sidecar smoke: isolated WebSocket host, ready, shutdown.

Requires Python websockets and an exported Godot .NET sidecar (or --project plus the editor).
Use a test build, never a production process. Logs and results go to --output under tests/output.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path


async def run(args):
    from websockets.asyncio.server import serve

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    completed = asyncio.get_running_loop().create_future()

    async def host(socket):
        try:
            while True:
                message = json.loads(await asyncio.wait_for(socket.recv(), 20))
                if message.get("type") == "stage.ready":
                    await socket.send(json.dumps({"type": "host.shutdown"}))
                    completed.set_result(True)
                    await socket.wait_closed()
                    return
        except Exception as error:
            if not completed.done():
                completed.set_exception(error)

    async with serve(host, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        command = [str(args.executable.resolve()), "--headless"]
        if args.project:
            command += ["--path", str(args.project.resolve())]
        command += ["--", f"--airi-ws-url=ws://127.0.0.1:{port}/ws", f"--airi-storage-root={output / 'storage'}"]
        environment = {**os.environ, "AIRI_GODOT_STAGE_DEV_MODE": "0"}
        with (output / "godot.log").open("wb") as log:
            process = await asyncio.create_subprocess_exec(*command, cwd=output, env=environment, stdout=log, stderr=log)
            try:
                await asyncio.wait_for(completed, 30)
                code = await asyncio.wait_for(process.wait(), 10)
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
    text = (output / "godot.log").read_text(encoding="utf-8", errors="replace")
    checks = {
        "ready": completed.result() is True,
        "shutdown_exit_zero": code == 0,
        "no_runtime_errors": "ERROR:" not in text,
        "hdr_available": "HDR skybox could not" not in text,
    }
    (output / "result.json").write_text(json.dumps(checks, indent=2) + "\n", encoding="utf-8")
    for name, passed in checks.items():
        print(f"{'PASS' if passed else 'FAIL'} {name}")
    print(f"{sum(checks.values())}/{len(checks)}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
