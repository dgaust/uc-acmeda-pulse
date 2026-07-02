# uc-acmeda-pulse

An [Unfolded Circle Remote](https://www.unfoldedcircle.com/) integration
driver for the **Rollease Acmeda Pulse Hub v2** (the hub behind the "Pulse 2"
app). It talks to the hub directly over the local network - no Home
Assistant, no cloud - using a small purpose-built async client (`pulsehub.py`).

## What it does

- Discovers all rollers (blinds) configured on the hub.
- Exposes each roller as a UC **Cover** entity: open / close / stop / set
  position.
- Exposes a battery-percentage **Sensor** entity for battery-powered rollers,
  and a signal-strength **Sensor** entity for all rollers.

Not yet supported: tilt, rooms/scenes, multiple hubs.

## Requirements

- Python 3.11+
- A Rollease Acmeda Pulse Hub v2 reachable on your local network.

## Running

```bash
cd intg-acmeda
pip install -r requirements.txt
python3 driver.py
```

The driver listens on port `10091` (see `driver.json`) and advertises itself
over mDNS so the Remote can discover it. Add it from the Remote's
integrations screen and enter the hub's IP address/hostname when prompted.

Configuration (the hub host + a cached roller list) is persisted to
`config.json` in the directory pointed to by `UC_CONFIG_HOME` (defaults to the
current directory if unset).

## Running as an external driver in Docker

To run the driver on a home server / NAS (rather than on the Remote itself),
use the provided `Dockerfile` / `docker-compose.yml`:

```bash
docker compose up -d --build
```

**Host networking is required** (`network_mode: host`, already set in the
compose file). The driver advertises itself over mDNS (`_uc-integration._tcp`)
for the Remote to discover it, and multicast DNS does not cross a Docker bridge
network - so the container must share the host's network. This also lets it
reach the hub on your LAN.

The compose file mounts `./config` into the container's `UC_CONFIG_HOME`, so
the hub host and cached roller list persist across restarts and image updates.
After the container is running, add the integration from the Remote's
integrations screen (it should be auto-discovered) and enter the hub's IP.

To run without compose:

```bash
docker build -t uc-acmeda-pulse .
docker run -d --name uc-acmeda-pulse --network host --restart unless-stopped \
  -v "$PWD/config:/config" uc-acmeda-pulse
```

### Architectures / multi-arch images

The image is pure Python on a multi-arch base, so it runs on both **amd64
(x86-64)** and **arm64** hosts (Intel/AMD servers & NASes, Raspberry Pi, ARM
NASes, etc.). `docker build` / `docker compose` automatically produce an image
for whatever machine you build on.

To build both architectures at once, use `build-docker.sh` (wraps `docker
buildx`):

```bash
# validate both arches build
./build-docker.sh

# build one arch and load it locally for testing
PLATFORMS=linux/amd64 LOAD=1 ./build-docker.sh

# build and push a multi-arch image to a registry
IMAGE=ghcr.io/dgaust/uc-acmeda-pulse TAG=0.2.2 PUSH=1 ./build-docker.sh
```

The included GitHub Actions workflow (`.github/workflows/docker.yml`) builds
and publishes multi-arch (`linux/amd64,linux/arm64`) images to GHCR on pushes
to `main` and on version tags, so you can also just pull a prebuilt image:

```bash
docker pull ghcr.io/dgaust/uc-acmeda-pulse:latest
```

## Installing / upgrading on the Remote

The driver ships as a custom-integration archive
(`dist/uc-intg-acmeda-<version>-aarch64.tar.gz`) built for the Remote's ARM64
runtime. In the web-configurator: _Integrations → Add new → Install custom_.

To ship a new version **without losing your configuration**, tick
**"Update existing driver"** on that upload screen (requires Remote firmware
v2.9.3+), or use the REST API with `?update=true`:

```shell
curl --location 'http://<remote-ip>/api/intg/install?update=true' \
  --user 'web-configurator:<PIN>' \
  --form 'file=@"uc-intg-acmeda-<version>-aarch64.tar.gz"'
```

An update preserves the `UC_CONFIG_HOME` directory, so the saved hub host and
cached roller list survive; on restart the driver re-registers its entities
from that cache and reconnects with no re-setup. A plain (non-update) install
starts from a clean config.

The upgrade is matched by `driver_id` (`acmeda_pulse`), which is kept stable
across versions. It deliberately does **not** use the reserved `uc_` prefix
(reserved for pre-installed integrations, and liable to be removed by a
firmware update).

## Project layout

```
intg-acmeda/
  driver.json      driver metadata + first setup screen (asks for hub host)
  driver.py         entrypoint, lifecycle events (connect/disconnect/standby)
  setup_flow.py      validates the hub is reachable, snapshots its rollers
  hub_manager.py    owns the persistent PulseHub connection, maps it to entities
  pulsehub.py        purpose-built async client for the Pulse Hub v2 protocol
  entities.py        maps a pulsehub Roller <-> UC Cover/Sensor entities
  config.py         persists the hub host + cached roller list to config.json
  shared.py         shared IntegrationAPI/event-loop singletons
Dockerfile          external-driver container image (host networking)
docker-compose.yml  run the external driver with a persisted config volume
build-docker.sh     build multi-arch (amd64 + arm64) images with buildx
.github/workflows/  CI: build & publish multi-arch images to GHCR
```

## The hub client (`pulsehub.py`)

Rather than depend on a third-party library, the hub protocol is implemented
directly so the driver controls the behaviours that matter for reliability
(protocol reference: the [aiopulse2 wiki](https://github.com/sillyfrog/aiopulse2/wiki)):

- **The websocket is the single source of truth.** It connects to
  `wss://<host>:443/rpc` and polls `shadow` every few seconds; each response
  carries the full roller state (position, online, battery voltage, signal).
  Commands (`movePercent` / `stopShade`) are sent over the same socket. The
  `connected` flag and all update callbacks track *this* socket only, and it
  reconnects automatically.
- **Roller names are fetched out-of-band and never gate anything.** The hub
  does not send names over the websocket - the only local source is the
  single-connection port-1487 "serial" channel, which can fail. Name
  resolution runs as a best-effort background task; if it fails, state and
  control are completely unaffected and rollers keep their id as a name until
  the next attempt. (Coupling names to state/connectivity was the root cause of
  the early reliability problems.)

## Protocol / behaviour notes

- `Roller.closed_percent` is 0=open / 100=closed; the UC Cover `position`
  attribute is the opposite (0=closed / 100=open) - the conversion lives
  entirely in `entities.py`.
- Entities are registered from the cached roller list at **startup**, before
  the live hub connection completes, because the Remote re-subscribes to its
  remembered entities the instant it (re)connects to the driver. If the
  entities weren't already present that subscribe would fail.
- The driver emits a `device_state` in response to the Remote's `connect`
  request (and `exit_standby`), even when the hub is already connected - the
  Remote waits for that event to mark the integration connected.
