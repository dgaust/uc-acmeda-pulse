"""Smoke test: start the driver's IntegrationAPI, connect a raw websocket, hit a couple of requests."""
import asyncio
import json
import os
import sys
import tempfile

os.environ["UC_DISABLE_MDNS_PUBLISH"] = "true"
os.environ["UC_INTEGRATION_HTTP_PORT"] = "9099"
os.environ["UC_CONFIG_HOME"] = tempfile.mkdtemp()

import _path  # noqa: F401,E402 - puts intg-acmeda on sys.path
os.chdir(_path.INTG_DIR)

import driver  # noqa: E402
import websockets  # noqa: E402
from shared import loop as uc_loop  # noqa: E402


async def main():
    await driver.main()
    await asyncio.sleep(0.5)

    ws = None
    for attempt in range(10):
        try:
            ws = await websockets.connect("ws://127.0.0.1:9099")
            break
        except ConnectionRefusedError:
            print(f"connect attempt {attempt} refused, retrying...")
            await asyncio.sleep(0.5)
    if ws is None:
        raise RuntimeError("could not connect after retries")

    async def recv_msg(expected_msg):
        while True:
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            if resp.get("msg") == expected_msg:
                return resp
            print(f"(skipping unsolicited {resp.get('msg')})")

    async with ws:
        await ws.send(json.dumps({"kind": "req", "id": 1, "msg": "get_driver_version"}))
        resp = await recv_msg("driver_version")
        print("get_driver_version ->", resp)

        await ws.send(json.dumps({"kind": "req", "id": 2, "msg": "get_available_entities"}))
        resp = await recv_msg("available_entities")
        print("get_available_entities ->", resp)
        assert resp["msg_data"]["available_entities"] == []

    print("DRIVER_STARTUP_OK")


uc_loop.run_until_complete(asyncio.wait_for(main(), timeout=25))
