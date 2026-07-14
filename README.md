# Acmeda Pulse Hub for Unfolded Circle Remote

Control your Rollease Acmeda motorised blinds from an
[Unfolded Circle Remote](https://www.unfoldedcircle.com/).

The integration talks directly to your **Pulse Hub v2** (the hub used with the
"Automate Pulse 2" phone app) over your home network. Nothing goes through the
cloud, and you don't need Home Assistant or any other software.

## What you get

- Every blind on your hub appears on the Remote. You can open it, close it,
  stop it, or move it to any position.
- A battery level sensor for battery-powered blinds.
- A signal strength sensor for every blind.
- Smooth position updates while a blind is moving (twice a second).

Not supported yet: tilt, rooms/scenes, more than one hub.

## What you need

- An Unfolded Circle Remote.
- A Rollease Acmeda Pulse Hub v2 on the same network as the Remote.
- Your hub's IP address. You can find it in your router's device list.

## Install it on the Remote (easiest)

The driver runs on the Remote itself. No other hardware needed.

1. Download the latest `uc-intg-acmeda-...-aarch64.tar.gz` file from the
   [Releases page](https://github.com/dgaust/uc-acmeda-pulse/releases).
2. Open the Remote's web configurator in a browser. Go to
   **Integrations → Add new → Install custom** and upload the file.
3. Run the setup and type in your hub's IP address.

Your blinds will appear, named the same as in the Pulse app.

### Updating

Do the same steps with the newer release file, but tick
**"Update existing driver"** when uploading. Your settings are kept, so you
won't need to set it up again. (This needs Remote firmware 2.9.3 or newer.)

## Or run it in Docker

If you'd rather run the driver on an always-on computer, server or NAS, there
is a ready-made Docker image. It works on both Intel/AMD (x86) and ARM
machines, including a Raspberry Pi.

Save this as `docker-compose.yml` in a folder of its own, and **change the IP
address on the `UC_INTEGRATION_INTERFACE` line to your server's own LAN IP**:

```yaml
services:
  uc-acmeda-pulse:
    image: ghcr.io/dgaust/uc-acmeda-pulse:latest
    container_name: uc-acmeda-pulse
    restart: unless-stopped
    # Required - see note below.
    network_mode: host
    environment:
      UC_INTEGRATION_HTTP_PORT: "10091"
      # >>> CHANGE to the LAN IP of the machine running Docker <<<
      UC_INTEGRATION_INTERFACE: "192.168.1.50"
    volumes:
      # Keeps your settings when the container restarts or updates.
      - ./config:/config
```

Then start it:

```bash
docker compose up -d
```

On the Remote, go to **Integrations → Add new**. The driver is found
automatically - select it and type in your hub's IP address.

**Why the server's IP is needed:** the driver has to tell the Remote where to
find it. Inside a container it can't work its own address out, so unless you
tell it, it announces a placeholder that leads nowhere. The Remote then either
can't find the driver at all ("resource not found"), or finds it once but never
reconnects after the container restarts.

**Why `network_mode: host` is required:** the Remote finds the driver by
listening for announcements on the local network, and those announcements
can't get out of Docker's normal isolated networking. Host mode also lets the
driver reach your hub.

### Updating

```bash
docker compose pull && docker compose up -d
```

Your settings live in the `config` folder next to the compose file, so they
survive updates.

### Using Cosmos?

If your server runs [Cosmos](https://cosmos-cloud.io/), you can install the
driver as a ServApp instead of using compose directly:

1. In Cosmos, go to **ServApps → Start ServApp → Import Compose File**.
2. Paste the contents of
   [`cosmos-compose.json`](https://raw.githubusercontent.com/dgaust/uc-acmeda-pulse/main/cosmos-compose.json)
   from this repo and follow the installer.

The installer asks for your server's LAN IP address (the Remote needs it to
find the driver) and where to store the settings. The app has no web page of
its own, so Cosmos won't create a URL for it - once it's running, just add the
integration on the Remote as described above. Updates are handled by Cosmos
automatically.

### If the Remote can't find the driver

Nearly always this is the server IP being wrong or unset. Check what the driver
is announcing:

```bash
docker logs uc-acmeda-pulse | grep "Publishing driver"
```

Then make sure `UC_INTEGRATION_INTERFACE` matches the IP other machines use to
reach that server, and recreate the container (`docker compose up -d`). If you
had previously added the integration on the Remote by typing an IP address by
hand, delete it there and re-add it by picking it from the discovered list.

## For developers

Everything below is only relevant if you want to change or build the
integration yourself.

### Run from source

```bash
cd intg-acmeda
pip install -r requirements.txt
python3 driver.py
```

The driver listens on port 10091 and announces itself on the network so the
Remote can find it. Settings are saved to `config.json` in the folder set by
the `UC_CONFIG_HOME` environment variable (or the current folder if unset).

### Build the Docker image yourself

`docker build -t uc-acmeda-pulse .` builds an image for your own machine.
`build-docker.sh` builds for both x86 and ARM at once (uses docker buildx):

```bash
./build-docker.sh                                            # check both build
IMAGE=ghcr.io/dgaust/uc-acmeda-pulse TAG=x.y.z PUSH=1 ./build-docker.sh   # publish
```

The GitHub Actions workflow in `.github/workflows/docker.yml` publishes
multi-arch images to GitHub Container Registry automatically on every push to
`main` and on version tags.

### Tests

The tests in `tests/` are standalone scripts - run each one on its own:

```bash
pip install -r intg-acmeda/requirements.txt -r test-requirements.txt
for t in tests/test_*.py; do python "$t"; done
```

They cover the hub protocol client (against a fake hub server), the driver's
startup and restart behaviour, and the mapping of blind state to what the
Remote shows. CI runs them on every push.

### Releases (automated)

Releases are built and published by CI. To put out a new version:

1. Change `"version"` in `intg-acmeda/driver.json` (say `0.3.0`).
2. Commit and push, then tag it:

   ```bash
   git tag v0.3.0 && git push origin v0.3.0
   ```

CI then runs the tests, builds the install file for the Remote, and publishes
a **pre-release (beta)** on GitHub with the file and its checksum attached.
Matching Docker image tags are published at the same time. The build fails if
the tag doesn't match the version in `driver.json`.

Once a beta has been tested on a real Remote, promote it: edit the release on
GitHub and untick "Set as a pre-release".

The install file can also be built locally with `bash build-intg.sh`
(needs Docker).

### What's in the repo

```
intg-acmeda/
  driver.json        driver details + the setup screen (asks for the hub IP)
  driver.py           starting point; handles connect/disconnect/standby
  setup_flow.py        first-time setup: checks the hub and finds the blinds
  hub_manager.py      keeps the hub connection alive and updates the Remote
  pulsehub.py          talks the Pulse Hub's own network protocol
  entities.py          turns each blind into the entities the Remote shows
  config.py           saves the hub IP and blind list
  shared.py           shared setup used by the other files
Dockerfile            Docker image for running on a server/NAS
docker-compose.yml    ready-to-use Docker setup
cosmos-compose.json   one-click install for Cosmos servers
build-docker.sh       builds Docker images for x86 and ARM
```

### How it works (the short version)

- The driver keeps one connection open to the hub and asks for the state of
  all blinds every 3 seconds - or every half second while a blind is moving,
  with an extra check the moment you send a command.
- Blind **names** come from a second, less reliable connection to the hub
  (TCP port 1487). If that fails, everything still works - the blinds just
  show their short ID until the name comes through.
- The driver remembers your hub address and blind list, so after a restart
  your blinds are back straight away with no re-setup.
- The Pulse Hub counts position as "percent closed" while the Remote counts
  "percent open"; the driver converts between the two.
- The hub's protocol isn't officially documented; this project uses the
  protocol notes from the [aiopulse2 wiki](https://github.com/sillyfrog/aiopulse2/wiki).
- The driver id is `acmeda_pulse` and stays the same across versions - that's
  what lets the Remote treat a new upload as an update instead of a new
  install. (It avoids the `uc_` prefix, which is reserved for Unfolded
  Circle's own integrations.)

## License

[MPL-2.0](LICENSE)
