# Container image for running uc-acmeda-pulse as an *external* Unfolded Circle
# integration driver (on a home server / NAS), as opposed to the on-Remote
# custom-driver tar.gz. The ucapi library advertises the driver over mDNS
# (_uc-integration._tcp) so the Remote can discover it - which is why the
# container MUST be run with host networking (see docker-compose.yml / README);
# multicast DNS does not traverse a Docker bridge network.

FROM python:3.11-slim

WORKDIR /app

# Install dependencies first for better layer caching.
COPY intg-acmeda/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Application (driver.py, driver.json, pulsehub.py, ...).
COPY intg-acmeda/ ./

# Config (hub host + cached roller list) is written to UC_CONFIG_HOME - mount a
# volume there to persist it across container restarts/upgrades.
ENV UC_CONFIG_HOME=/config
VOLUME ["/config"]

# Integration-API WebSocket server port (matches driver.json "port").
ENV UC_INTEGRATION_HTTP_PORT=10091
EXPOSE 10091

CMD ["python", "driver.py"]
