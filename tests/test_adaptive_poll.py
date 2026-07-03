"""
Verify adaptive polling: the client polls fast while a roller is moving and
slowly when everything is stationary, and a command wakes the poller at once.
Driven against a real in-process TLS websocket 'hub' that counts poll requests.
"""
import asyncio
import json
import os
import ssl
import sys
import tempfile
import time

import _path  # noqa: F401,E402 - puts intg-acmeda on sys.path

import websockets
import pulsehub


def make_cert():
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import datetime

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    d = tempfile.mkdtemp()
    cp, kp = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
    open(cp, "wb").write(cert.public_bytes(serialization.Encoding.PEM))
    open(kp, "wb").write(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()))
    return cp, kp


# Server state: whether the roller reports as moving, and poll timestamps.
state = {"moving": False, "polls": []}


async def ws_handler(conn):
    async for raw in conn:
        msg = json.loads(raw)
        if msg.get("method") == "shadow" and "args" not in msg:
            state["polls"].append(time.monotonic())
            await conn.send(json.dumps({"result": {"reported": {
                "shades": {"S1S": {"is": not state["moving"], "ol": True, "mp": 50, "rs": -70}},
            }}}))


async def main():
    cp, kp = make_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cp, kp)
    server = await websockets.serve(ws_handler, "127.0.0.1", 8444, ssl=ctx)

    pulsehub.WS_PORT = 8444
    pulsehub.POLL_INTERVAL_S = 2.0
    pulsehub.POLL_INTERVAL_MOVING_S = 0.25

    hub = pulsehub.PulseHub("127.0.0.1")
    run_task = asyncio.create_task(hub.run())
    await asyncio.wait_for(hub.rollers_known.wait(), timeout=5)

    # --- Idle window: should poll slowly (interval 2.0s) ---
    state["polls"].clear()
    await asyncio.sleep(1.5)
    idle_polls = len(state["polls"])
    print("idle polls in 1.5s:", idle_polls)
    assert idle_polls <= 2, f"expected slow idle polling, got {idle_polls}"

    # --- Issue a move command: server now reports the roller as moving. ---
    # The command wakes the poller immediately and fast polling continues while
    # the roller is moving. This is the real-world path (user drives the blind).
    state["moving"] = True
    state["polls"].clear()
    t0 = time.monotonic()
    await hub.rollers["S1S"].move_to(80)
    await asyncio.sleep(0.15)
    assert state["polls"], "command should trigger an immediate poll"
    latency = state["polls"][0] - t0
    print(f"poll latency after command: {latency * 1000:.0f} ms")
    assert latency < 0.3, f"command didn't wake poller promptly: {latency:.2f}s"

    await asyncio.sleep(1.5)  # keep moving; count fast polls
    moving_polls = len(state["polls"])
    print("polls in ~1.65s while moving:", moving_polls)
    assert moving_polls >= 4, f"expected fast polling while moving, got {moving_polls}"

    # --- Roller stops: polling backs off to the idle interval again. ---
    state["moving"] = False
    await asyncio.sleep(0.6)  # let one fast poll observe 'is: True' -> stationary
    state["polls"].clear()
    await asyncio.sleep(1.5)
    settled_polls = len(state["polls"])
    print("polls in 1.5s after stopping:", settled_polls)
    assert settled_polls <= 2, f"expected slow polling after stop, got {settled_polls}"

    await hub.stop()
    run_task.cancel()
    server.close()
    print("ADAPTIVE_POLL_TEST_PASSED")


asyncio.run(asyncio.wait_for(main(), timeout=30))
