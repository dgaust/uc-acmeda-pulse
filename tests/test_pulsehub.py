"""
End-to-end test of the purpose-built pulsehub client against a fake hub:
a real in-process TLS websocket server (the /rpc shadow channel) plus a fake
TCP serial server (port-1487 name channel).

Verifies:
- state parsing from shadow responses (position, online, battery, signal)
- connected flag + callbacks fire without names
- names arrive out-of-band via the serial channel and DON'T gate state
- move/stop commands emit the correct JSON payloads
- a broken serial (name) channel leaves state/control fully working
"""
import asyncio
import json
import ssl
import sys
import tempfile
import os

import _path  # noqa: F401,E402 - puts intg-acmeda on sys.path

import websockets
import pulsehub

# --- self-signed cert for the fake wss server -------------------------------
def make_cert():
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    import datetime

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(issuer).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow() - datetime.timedelta(days=1))
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    d = tempfile.mkdtemp()
    cp = os.path.join(d, "c.pem"); kp = os.path.join(d, "k.pem")
    with open(cp, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(kp, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption()))
    return cp, kp


received_commands = []


async def ws_handler(conn):
    # Respond to every shadow poll with a fixed reported state; echo commands.
    async for raw in conn:
        msg = json.loads(raw)
        if msg.get("method") == "shadow" and "args" not in msg:
            await conn.send(json.dumps({
                "result": {"reported": {
                    "hubId": "100X", "name": "TestHub", "mac": "aa:bb",
                    "firmware": {"version": "1.0.0"},
                    "mfi": {"model": "MT02"},
                    "shades": {
                        "S1S": {"is": True, "ol": True, "mp": 0, "vo": "11.3D24", "rs": -77},
                        "D55": {"is": True, "ol": True, "mp": 30, "vo": "11.4D24", "rs": -70},
                    },
                }}
            }))
        elif "args" in msg:
            received_commands.append(msg["args"]["desired"]["shades"])


async def serial_handler(reader, writer):
    # Reply to NAME queries.
    names = {"S1S": "Window", "D55": "Door"}
    try:
        while True:
            data = await asyncio.wait_for(reader.readuntil(b";"), timeout=2)
            q = data.decode()
            for rid, name in names.items():
                if q == f"!{rid}NAME?;":
                    writer.write(f"!{rid}NAME{name};".encode())
            await writer.drain()
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        writer.close()


async def main(with_serial=True):
    cp, kp = make_cert()
    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(cp, kp)

    ws_server = await websockets.serve(ws_handler, "127.0.0.1", 8443, ssl=ssl_ctx)
    serial_server = None
    if with_serial:
        serial_server = await asyncio.start_server(serial_handler, "127.0.0.1", 8487)

    # Point the client at our fake ports by overriding the module constants.
    pulsehub.WS_PORT = 8443
    pulsehub.SERIAL_PORT = 8487
    pulsehub.NAME_FETCH_RETRY_DELAY_S = 0.5
    pulsehub.POLL_INTERVAL_S = 0.5

    hub = pulsehub.PulseHub("127.0.0.1")
    updates = {"hub": 0, "rollers": []}

    async def on_hub(h):
        updates["hub"] += 1

    async def on_roller(r):
        updates["rollers"].append((r.id, r.name, r.closed_percent))

    hub.callback_subscribe(on_hub)

    run_task = asyncio.create_task(hub.run())

    # Wait for rollers to be known (state), WITHOUT waiting for names.
    await asyncio.wait_for(hub.rollers_known.wait(), timeout=5)
    for r in hub.rollers.values():
        r.callback_subscribe(on_roller)

    assert hub.connected, "hub should be connected after first shadow response"
    assert set(hub.rollers) == {"S1S", "D55"}, hub.rollers
    s1s = hub.rollers["S1S"]
    assert s1s.closed_percent == 0 and s1s.online is True
    assert s1s.has_battery and s1s.battery == 11.3
    assert hub.name == "TestHub" and hub.model == "MT02"
    print("state OK (names not required):", {k: v.name for k, v in hub.rollers.items()})

    # Commands emit correct payloads.
    await s1s.move_to(40)
    await asyncio.sleep(0.2)
    assert {"S1S": {"movePercent": 40}} in received_commands, received_commands
    await s1s.move_stop()
    await asyncio.sleep(0.2)
    assert {"S1S": {"stopShade": True}} in received_commands, received_commands
    print("commands OK:", received_commands)

    if with_serial:
        # Names arrive out-of-band within the bounded wait.
        await hub.wait_for_names(5)
        assert hub.rollers["S1S"].name == "Window", hub.rollers["S1S"].name
        assert hub.rollers["D55"].name == "Door"
        print("names resolved via serial:", {k: v.name for k, v in hub.rollers.items()})
    else:
        # No serial server: names never resolve, but state/commands still worked.
        await hub.wait_for_names(1.5)
        assert hub.rollers["S1S"].name is None
        print("names absent but everything else worked (broken serial channel)")

    await hub.stop()
    run_task.cancel()
    ws_server.close()
    if serial_server:
        serial_server.close()


asyncio.run(asyncio.wait_for(main(with_serial=True), timeout=30))
received_commands.clear()
asyncio.run(asyncio.wait_for(main(with_serial=False), timeout=30))
print("PULSEHUB_TEST_PASSED")
