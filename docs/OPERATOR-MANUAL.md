# Clear Skies — Operator Manual

A guide for weather station operators who want to install, configure, and maintain a Clear Skies weather site. No programming experience is required.

**Version:** 1.0 (beta)
**Last updated:** 2026-07-17

---

## Table of Contents

1. [Quick Start](#1-quick-start)
2. [System Requirements](#2-system-requirements)
3. [Installation — Native (Linux)](#3-installation--native-linux)
4. [Installation — Docker Compose](#4-installation--docker-compose)
5. [Installation — weewx Extensions](#5-installation--weewx-extensions)
6. [First-Run Wizard](#6-first-run-wizard)
7. [Admin Guide](#7-admin-guide)
8. [Under the Hood](#8-under-the-hood)
9. [Charts Configuration](#9-charts-configuration)
10. [Troubleshooting](#10-troubleshooting)
11. [Getting Help](#11-getting-help)
12. [Support Scope](#12-support-scope)
13. [Legal](#13-legal)

---

## 1. Quick Start

This section gets a Clear Skies weather site running in about 15 minutes on a machine that already has weewx 5.x and Docker installed. For detailed explanations, read the full installation sections below.

### Before you begin

You need:

- A running **weewx 5.x** installation producing archive records
- **Docker Engine 24+** and the **Docker Compose plugin** (v2.20+)
- A domain name pointing at your server (for automatic TLS), or a willingness to use self-signed certificates
- About 15 minutes

### Steps

1. **Install the Loop Relay extension** into weewx. This creates the Unix socket that Clear Skies reads weather data from:

   ```bash
   weectl extension install weewx-clearskies-extension.tar.gz
   sudo systemctl restart weewx
   ```

2. **Clone the stack repo** and run the setup script:

   ```bash
   git clone https://github.com/inguy24/weewx-clearskies-stack.git
   cd weewx-clearskies-stack
   sudo ./scripts/setup.sh
   ```

   The setup script asks about your deployment topology (single machine or two machines), network settings, and domain name. It generates the environment files and configuration you need.

3. **Start the stack:**

   ```bash
   cd single-host   # or frontend-host / weewx-host for two-host
   docker compose up -d
   ```

4. **Open the setup wizard** at `https://your-domain/wizard`. The wizard walks you through connecting to the API, choosing your database, configuring weather data providers, and customizing the appearance.

5. **Verify** by visiting your domain. You should see the Clear Skies weather site displaying your weather data.

If something goes wrong, see [Troubleshooting](#10-troubleshooting).

---

## 2. System Requirements

Measured on a running Clear Skies installation (2026-07-03) with all providers configured, archive database at approximately 2 years of 5-minute records, and Redis caching enabled.

### Per-component resource usage

| Component | RAM (idle) | RAM (peak) | Disk (installed) | Notes |
|-----------|-----------|-----------|-----------------|-------|
| **API** | ~600 MB | ~850 MB | ~620 MB | Python 3.12+ venv with all dependencies. Peak during cache warming at startup (simultaneous provider API calls). |
| **Config UI** | ~61 MB | ~120 MB | ~290 MB | Python 3.12+ venv. Peak during wizard apply (writes config, restarts services). |
| **Dashboard** | — | — | ~62 MB | Static files served by Caddy. No runtime process. Build requires ~800 MB for Node.js dependencies (build-time only). |
| **Caddy** | ~43 MB | ~80 MB | ~40 MB | Reverse proxy and TLS terminator. Peak during TLS certificate negotiation. |
| **Redis** | ~2 MB | ~250 MB | Negligible | Optional. Peak depends on number of configured providers and cache fill. Memory drops back to ~2 MB after cache TTLs expire during idle periods. |
| **Skyfield ephemeris** | — | — | ~17 MB | Downloaded once on first API start. Used for astronomical calculations (sun/moon positions, planets, eclipses). |
| **weewx** | ~80 MB | ~150 MB | Varies | Operator's existing installation. Not a Clear Skies component, but shares the host. |

### Heaviest dependencies (installed size)

These are the largest Python packages in the API's virtual environment:

| Package | Size | Why it's needed |
|---------|------|-----------------|
| SciPy | ~109 MB | Scientific computing — used by the forecast correction engine and sky classification |
| pandas | ~73 MB | Data manipulation — forecast correction pair collection, archive analysis |
| scikit-learn | ~49 MB | Machine learning — Random Forest model for forecast temperature correction |
| NumPy | ~34 MB | Numerical computing — used by SciPy, pandas, pvlib, scikit-learn |
| Babel | ~33 MB | Internationalization — locale-aware number formatting in 13 languages |
| pvlib | ~32 MB | Solar position and irradiance modeling — sky classification |
| SQLAlchemy | ~23 MB | Database access — reads the weewx archive |
| timezonefinder | ~64 MB | Timezone lookup from coordinates (Config UI only) |
| cryptography | ~15 MB | TLS certificate generation |

### Minimum specifications

#### Single-host deployment (everything on one machine)

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| CPU | 2 cores | 4 cores |
| RAM | 2 GB | 4 GB |
| Disk | 2 GB free (plus weewx data) | 5 GB free |
| Python | 3.12 or later | 3.12 or later |
| OS | Debian 12+ / Ubuntu 22.04+ | Ubuntu 24.04 LTS |

#### Two-host deployment (split across weewx host and front-end host)

| Resource | weewx host | Front-end host |
|----------|-----------|---------------|
| CPU | 2 cores | 1 core |
| RAM (min) | 1 GB | 512 MB |
| RAM (rec) | 2 GB | 1 GB |
| Disk | 1.5 GB free | 500 MB free |

### Raspberry Pi feasibility

**Pi 4 (4 GB or 8 GB): Yes — feasible.** The API's ~600 MB idle RAM fits within a 4 GB Pi 4 alongside weewx. Build the weather site on a more powerful machine and copy the built files to the Pi (building on the Pi itself is slow and requires swap). ARM64 Pi OS is recommended over 32-bit for better Python 3.12 support.

**Pi 4 (2 GB): Marginal.** The API alone consumes ~600 MB. With weewx, the OS, and Caddy, you'll be near the limit. Redis should be disabled (use in-memory caching). Consider the two-host split: run the API on the Pi alongside weewx, and put the weather site on a separate machine.

**Pi 3 and earlier: Not recommended.** Insufficient RAM for the API's scientific computing dependencies.

### Measurement conditions

These numbers were measured on 2026-07-03 on LXD containers hosted on an AMD Ryzen Threadripper 2950X. The weewx station has been running continuously for approximately 2 years with a 5-minute archive interval. Configured providers: Vaisala Xweather (forecast + AQI), NWS (alerts), USGS (earthquakes), RainViewer (radar), LibreWxR (radar + satellite), 7Timer (seeing forecast). Redis caching enabled. Your actual resource usage will vary based on your provider configuration, archive size, and system load.

---

## 3. Installation — Native (Linux)

This section covers installing Clear Skies directly on Linux using pip and systemd, without Docker. This is the recommended path for operators who prefer to manage services directly or who run on constrained hardware like a Raspberry Pi.

### Prerequisites

| Requirement | Version | How to check |
|-------------|---------|-------------|
| weewx | 5.x | `weewx --version` |
| Python | 3.12 or later | `python3 --version` |
| pip | 23+ | `pip --version` |
| Node.js | 22 LTS | `node --version` (for building the weather site only) |
| Caddy | 2.x | `caddy version` |

Install Python 3.12 on older systems via the [deadsnakes PPA](https://launchpad.net/~deadsnakes/+archive/ubuntu/ppa) (Ubuntu) or from source.

### Step 1: Install the Loop Relay extension

The Loop Relay is a weewx extension that creates a Unix socket for the API to read real-time weather data from. It is required.

```bash
weectl extension install weewx-clearskies-extension.tar.gz
sudo systemctl restart weewx
```

Verify the socket exists after weewx restarts:

```bash
ls -la /var/run/weewx-clearskies/loop.sock
```

### Step 2: Run the prerequisites script

The setup script creates the system user, groups, and directories that Clear Skies needs:

```bash
git clone https://github.com/inguy24/weewx-clearskies-stack.git
cd weewx-clearskies-stack
sudo bash scripts/install-prerequisites.sh
```

This script:

- Creates a `clearskies` system user (no login shell, no home directory)
- Creates a `weewx-ro` group for read-only database access
- Creates `/etc/weewx-clearskies/` for configuration files
- Creates `/var/run/weewx-clearskies/` for the Unix domain socket
- Creates `/var/www/clearskies/` for the weather site static files
- Sets appropriate file permissions

### Step 3: Install the API

Create a Python virtual environment and install the API:

```bash
sudo -u clearskies python3.12 -m venv /opt/clearskies-api
sudo -u clearskies /opt/clearskies-api/bin/pip install --pre weewx-clearskies-api
```

Copy and enable the systemd unit:

```bash
sudo cp examples/systemd/weewx-clearskies-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable weewx-clearskies-api
sudo systemctl start weewx-clearskies-api
```

The API starts in "life-support" mode because there is no configuration yet — it only serves the setup endpoints that the wizard needs. Full configuration happens in Step 6 when you run the wizard.

The API takes approximately 2 minutes to start because it warms its provider caches on startup. After the first start (in life-support mode), this is fast because no providers are configured yet.

### Step 4: Install the Config UI

```bash
sudo -u clearskies python3.12 -m venv /opt/clearskies-config
sudo -u clearskies /opt/clearskies-config/bin/pip install --pre weewx-clearskies-config
```

Copy and enable the systemd unit:

```bash
sudo cp examples/systemd/weewx-clearskies-config.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable weewx-clearskies-config
sudo systemctl start weewx-clearskies-config
```

The Config UI runs on port 9876 by default.

### Step 5: Build and install the weather site

```bash
git clone https://github.com/inguy24/weewx-clearskies-dashboard.git
cd weewx-clearskies-dashboard
npm ci
npm run build
sudo rsync -av --delete --exclude webcam/ --exclude cards/ dist/ /var/www/clearskies/
```

The `--exclude webcam/` and `--exclude cards/` flags protect the webcam image directory (which is a read-only mount from weewx) and the future third-party card assets directory from being deleted during deployments.

### Step 6: Configure Caddy

Copy the example Caddyfile and edit it for your domain:

```bash
sudo cp weewx-clearskies-stack/examples/reverse-proxy/Caddyfile /etc/caddy/Caddyfile
sudo $EDITOR /etc/caddy/Caddyfile
```

At minimum, replace `your-domain.example.com` with your actual domain. Caddy handles TLS automatically via Let's Encrypt if your domain's DNS points to this machine.

Reload Caddy:

```bash
sudo systemctl reload caddy
```

### Step 7: Run the setup wizard

Open `https://your-domain/wizard` in a browser. The wizard guides you through:

1. Choosing your language
2. Connecting to the API (trust handshake)
3. Importing settings from an existing Belchertown skin (optional)
4. Accepting the license agreement
5. Configuring the database connection
6. Mapping database columns
7. Setting station identity (name, location, timezone)
8. Choosing display units
9. Configuring weather data providers and API keys
10. Setting up a webcam (optional)
11. Customizing appearance (colors, logos, theme)
12. Privacy, legal, and analytics settings
13. Feature settings
14. SWAN+TruShore nearshore wave model (conditional — surf locations only, when SWAN is installed)
15. TLS configuration
16. Reviewing and applying

After the wizard completes, the API restarts with the full configuration. Wait approximately 2 minutes for the cache warmer to finish, then visit your domain to see the weather site.

### Step 8: Verify

```bash
curl -k https://localhost:8765/api/v1/status
# Should return: {"data":{"configured":true},...}

curl https://your-domain/api/v1/station
# Should return station metadata
```

### Updating

```bash
# Update the API
sudo -u clearskies /opt/clearskies-api/bin/pip install -U --pre weewx-clearskies-api
sudo systemctl restart weewx-clearskies-api

# Update the Config UI
sudo -u clearskies /opt/clearskies-config/bin/pip install -U --pre weewx-clearskies-config
sudo systemctl restart weewx-clearskies-config

# Update the Dashboard
cd weewx-clearskies-dashboard
git pull
npm ci
npm run build
sudo rsync -av --delete --exclude webcam/ --exclude cards/ dist/ /var/www/clearskies/
```

Read the release notes before upgrading — some updates may require configuration changes.

---

## 4. Installation — Docker Compose

Docker Compose is the simplest installation path. It bundles the API, dashboard, Caddy, and Redis into containers managed by a single `docker compose up` command.

### Prerequisites

| Requirement | Version | How to check |
|-------------|---------|-------------|
| weewx | 5.x | `weewx --version` |
| Docker Engine | 24+ | `docker --version` |
| Docker Compose plugin | v2.20+ | `docker compose version` |

The Loop Relay extension (see [§5](#5-installation--weewx-extensions)) must be installed in weewx before starting the stack.

### Choosing a topology

Clear Skies supports two deployment topologies:

**Single-host** — everything on one machine alongside weewx. Simplest to set up. Use the `single-host/` compose file.

**Two-host** — the API runs on the weewx host (alongside the weather database for fast local reads), and the dashboard, Caddy, and Config UI run on a separate front-end host. This isolates the database from internet-facing services. Use `weewx-host/` on the weewx machine and `frontend-host/` on the front-end machine.

### Installation

1. **Clone the stack repo:**

   ```bash
   git clone https://github.com/inguy24/weewx-clearskies-stack.git
   cd weewx-clearskies-stack
   ```

2. **Run the setup script:**

   ```bash
   sudo ./scripts/setup.sh
   ```

   The script asks about your topology, network stack (IPv4, IPv6, or dual-stack), domain name, and paths to your weewx installation. It generates `.env`, `secrets.env`, and an initial `api.conf`.

3. **Start the stack:**

   ```bash
   # Single-host:
   cd single-host && docker compose up -d

   # Two-host (run on each machine):
   cd weewx-host && docker compose up -d    # on the weewx machine
   cd frontend-host && docker compose up -d  # on the front-end machine
   ```

4. **Run the setup wizard** at `https://your-domain/wizard`.

5. **Verify:**

   ```bash
   docker compose ps         # all containers should be "Up"
   curl https://your-domain/api/v1/status
   ```

### What the compose file provides

| Service | Image | Purpose |
|---------|-------|---------|
| `api` | `weewx-clearskies-api` | REST API, SSE stream, weather data processing |
| `dashboard` | `weewx-clearskies-dashboard` | Init container — builds the SPA, copies static files to a shared volume, then exits |
| `config` | `weewx-clearskies-config` | Setup wizard and admin interface |
| `caddy` | `caddy:2-alpine` | Reverse proxy, TLS termination, static file serving |
| `redis` | `redis:7-alpine` | Provider API response cache (optional but recommended) |

### Configuration files

All configuration lives in `/etc/weewx-clearskies/` (bind-mounted into the containers). The setup wizard creates most of these files automatically:

| File | Created by | Purpose |
|------|-----------|---------|
| `api.conf` | Wizard | API configuration: database, providers, units, TLS |
| `secrets.env` | Setup script / Wizard | API keys, database credentials, proxy secret |
| `charts.conf` | Wizard (or migration tool) | Chart groups, charts, and series definitions |
| `branding.json` | Wizard | Site branding: accent color, logos, theme |
| `webcam.json` | Wizard | Webcam settings: enabled, URLs, refresh interval |
| `pages.json` | Admin UI | Page visibility settings |
| `now-layout.json` | Admin UI | Now page card layout |

### Updating

```bash
docker compose pull
docker compose up -d
```

Configuration files on the host are preserved across image updates. Read the release notes before upgrading.

---

## 5. Installation — weewx Extensions

This section covers two weewx extensions and the optional SWAN+TruShore nearshore wave model. The weewx extensions run inside the weewx process itself, installed via weewx's built-in extension manager — neither requires Docker. SWAN+TruShore is a separate optional pip extra for the API.

### ClearSkiesLoopRelay (required)

The Loop Relay listens to every weather observation (called a "loop packet") that your station produces and forwards it through a Unix socket so the API can read it in real time. Without this extension, the weather site cannot show live-updating data — it can only show data from the archive database, which updates every 5 minutes (or whatever your archive interval is set to).

**Install:**

```bash
weectl extension install weewx-clearskies-extension.tar.gz
sudo systemctl restart weewx
```

**Verify:**

After weewx restarts, check that the socket file exists:

```bash
ls -la /var/run/weewx-clearskies/loop.sock
```

You should see a socket file owned by the weewx user. If the file does not appear, check the weewx log:

```bash
sudo journalctl -u weewx -n 50 | grep -i clearskies
```

Common issues:
- The `/var/run/weewx-clearskies/` directory does not exist — create it and set ownership to `weewx:weewx` with permissions `770`.
- The weewx user does not have permission to write to the directory.

### ClearSkiesTruesun (optional)

The TrueSun extension replaces weewx's built-in solar radiation model (Ryan-Stolzenbach) with a more accurate one based on the pvlib library's Simplified Solis model. It uses real atmospheric data from the Copernicus Atmosphere Monitoring Service (CAMS) — specifically, aerosol optical depth (AOD) from satellite observations — combined with your station's humidity readings to estimate precipitable water in the atmosphere.

The practical effect: more accurate sky classification, especially during sunrise and sunset transitions when the atmosphere's thickness matters most. Without TrueSun, weewx falls back to the Ryan-Stolzenbach model, which is less accurate but perfectly functional — there is no regression.

**Prerequisites:**

- A free CAMS API key from [https://ads.atmosphere.copernicus.eu/](https://ads.atmosphere.copernicus.eu/) (registration required)
- Python packages installed in the weewx Python environment: `pvlib`, `cdsapi`, `h5netcdf`

**Install:**

```bash
# Install Python dependencies into the weewx environment
pip install pvlib cdsapi h5netcdf

# Install the extension
weectl extension install weewx-clearskies-truesun.tar.gz
```

**Configure** by adding the following section to your `weewx.conf`:

```ini
[ClearSkiesTruesun]
    # CAMS API key (register at https://ads.atmosphere.copernicus.eu/)
    cams_api_key = your-api-key-here

    # Fallback AOD at 700nm when CAMS is unavailable
    # 0.06 is typical for clean coastal areas
    # 0.10-0.15 for inland areas with more aerosols
    fallback_aod700 = 0.06

    # How often to refresh CAMS AOD data (hours)
    aod_fetch_interval_hours = 12
```

The extension reads your station's latitude, longitude, and altitude automatically from the `[Station]` section of `weewx.conf`.

**Restart weewx:**

```bash
sudo systemctl restart weewx
```

**Verify:**

Check the weewx log for successful CAMS data fetching:

```bash
sudo journalctl -u weewx -n 100 | grep -i truesun
```

You should see messages like `clearskies_truesun: CAMS AOD fetch successful`. To confirm the model is working, compare `maxSolarRad` values at sunrise — the TrueSun model should show values above 10 W/m² at 6:00 AM, while the built-in Ryan-Stolzenbach model typically shows about 1.4 W/m² at the same time.

### SWAN+TruShore Nearshore Model (optional)

SWAN+TruShore is the Clear Skies nearshore wave model. SWAN (Simulating WAves Nearshore, a Fortran spectral wave model from Delft University of Technology) runs inside the marine service — a separate companion process that communicates with the API over a configured network connection. The marine service need not run on the same host as the API. SWAN uses a two-level nested grid architecture driven by blended wind forcing (HRRR for hours 0–48 + GFS for hours 48–72), running 4× daily on extended HRRR cycles (00/06/12/18Z), to produce 72-hour surf forecasts with nearshore physics. Total memory budget: ≤300 MB. When the marine service is connected and SWAN is available, SWAN+TruShore becomes the sole source for surf forecasts at configured surf spot locations. It replaces the former dependence on NOAA's Nearshore Wave Prediction System (NWPS).

#### Prerequisites

All requirements in this section apply to the **marine service host**, not the API host.

| Requirement | Notes |
|-------------|-------|
| SWAN binary | Fortran executable — not a Python package. Install via package manager or compile from source. |
| gfortran | Required to build SWAN from source. Not needed if installing via `apt`. |
| OpenMP | Parallel execution. Bundled with gfortran on most distributions. |
| eccodes system library | Required by the `[nearshore]` extra. Install before the pip package. |
| `weewx-clearskies-marine[nearshore]` | Pip extra that adds the HRRR wind provider, GFS wind provider, and SWAN runner to the marine service. |

**Install on Debian / Ubuntu (on the marine service host):**

```bash
sudo apt-get install -y libeccodes-dev swan gfortran
pip install "weewx-clearskies-marine[nearshore]"
```

**Install on RHEL / Fedora (on the marine service host):**

```bash
sudo dnf install eccodes-devel
pip install "weewx-clearskies-marine[nearshore]"
```

**Install on Alpine (on the marine service host):**

```bash
apk add eccodes-dev
pip install "weewx-clearskies-marine[nearshore]"
```

**Build SWAN from source (any Linux):**

```bash
sudo bash scripts/install_swan.sh   # download and compile SWAN
```

Verify the SWAN binary is on PATH on the marine service host:

```bash
swan --version
```

Restart the marine service to load the `[nearshore]` extra.

**Docker:** The SWAN-enabled marine service image includes the `[nearshore]` extra and the SWAN binary. Pull the latest image:

```bash
docker compose pull && docker compose up -d
```

#### Wizard Setup

The SWAN+TruShore wizard step appears when marine is enabled, at least one configured location has surf activity, and the connected marine service reports SWAN is installed and available. If the marine service is not connected or SWAN is not available, the step shows a notice and a **Skip** button — surf forecasting will be unavailable until a marine service with SWAN is connected.

When SWAN is available, the step collects:

**SWAN nested grid:**

SWAN uses a two-level nested grid — a coarse outer grid for continental shelf wave propagation, and a fine inner nest focused on your surf spots. This is the standard approach used by NOAA's NWPS and all operational nearshore wave forecast systems.

| Field | Default | Range | Description |
|-------|---------|-------|-------------|
| Outer grid resolution | 3 km | 1–10 km | Coarse grid covering the shelf approach. Larger domain, fewer grid points. |
| Inner nest resolution | 200 m | 50–1000 m | Fine grid focused on surf spots. Resolves coastal features (jetties, headlands, reefs). |
| Inner nest bounding box (N/S/E/W) | Auto-computed from surf spot coordinates ± 0.2° | ±90° lat, ±180° lon | The tight domain around your configured surf spots. |

The outer grid domain is derived from the HRRR bounding box (the full marine location extent). The inner nest domain is derived from the surf spot coordinates. Lower inner nest resolution values are more accurate but increase memory and compute time. 200 m is recommended for most setups. Total memory for both grids: ≤300 MB.

**OpenMP thread count:** Number of CPU cores SWAN may use. **Recommended: 2–4 cores.** SWAN's parallel efficiency drops sharply beyond 6 cores and can actually slow down on small grids (Rautenbach et al. 2021, *Geosci. Model Dev.* 14, 4241–4247). More cores does not mean faster — the peak time-saving ratio is at ~6 cores, and for the small grids TruShore uses (~5,000–15,000 points), 2–4 is optimal. `0` uses all available cores (not recommended — wastes CPU on thread synchronization). On shared hosts running weewx, MariaDB, and the API alongside SWAN, set to 2 to leave capacity for other processes.

**Per-spot surf settings** (one set per configured surf location):

| Setting | Options | Default | Notes |
|---------|---------|---------|-------|
| Breaker formula | Komar-Gaughan / Caldwell | Komar-Gaughan | Komar-Gaughan is general-purpose for most coastlines. Caldwell is tuned for steep volcanic island coasts (Hawaii, Indonesia, Tahiti) and auto-falls to Komar-Gaughan when wave period is under 10 s. |
| Surf height display | Face height / Hawaiian | Face height | Face height measures trough-to-crest of the breaking wave face (US mainland / Europe standard). Hawaiian / back-of-wave is roughly half the face height (Hawaii / Australia convention). The API always returns both values; this setting controls which the surf card shows as the primary height. |

#### Admin Maintenance

Open `https://your-domain/admin` and navigate to **SWAN+TruShore**.

**SWAN status panel** shows:

| Field | Description |
|-------|-------------|
| Binary | Available or Not found, with version and path when found |
| CPU cores | OpenMP-available cores on this host |
| Last run | Timestamp of the most recent completed SWAN run, or "Never run" |
| Outer grid resolution | Current outer grid resolution (km) |
| Inner nest resolution | Current inner nest resolution (metres) |
| Memory budget | Peak memory during SWAN run (target ≤300 MB) |

**Run SWAN Now** triggers an immediate run outside the regular 6-hourly schedule (00/06/12/18Z). Use this after changing grid settings to verify the configuration before the next automatic cycle.

**Configuration form** lets you update:

- **OpenMP thread count** — adjust CPU parallelism for SWAN. 2–4 cores recommended; beyond 6 cores, returns diminish sharply and can slow down on small grids.
- **Outer grid resolution** — coarser values (4–5 km) reduce run time; finer values (1–2 km) improve shelf wave propagation accuracy. Changes take effect on the next SWAN run.
- **Inner nest resolution** — coarser values (400–500 m) reduce run time; finer values (50–100 m) increase nearshore accuracy at higher compute cost. Changes take effect on the next SWAN run.
- **Per-spot surf settings** — update breaker formula and surf height display per surf location. Changes take effect on the next SWAN run.

#### Troubleshooting

**SWAN binary not found**

If the `[nearshore]` extra is installed on the marine service host but `swan` is not on PATH, the marine service cannot run SWAN and surf forecast fields in API responses return null. The Marine Service admin section shows SWAN availability.

```bash
# Run these on the marine service host:
which swan          # check whether swan is on PATH
swan --version      # confirm it runs

# Install on Debian / Ubuntu:
sudo apt-get install -y swan gfortran

# Build from source:
sudo bash scripts/install_swan.sh
```

After installing, restart the marine service.

**SWAN runs taking more than 15 minutes**

Increase the inner nest resolution in the admin interface (**SWAN+TruShore** → **Inner nest resolution**). A 200 m inner nest typically completes both grid levels in 2–10 minutes. Only reduce below 200 m if your domain is small and your hardware supports the finer resolution. If runs are still slow, increase the outer grid resolution (e.g. from 2 km to 3 km).

**SWAN uses too much memory (OOM killed)**

The nested grid architecture is designed to stay under 300 MB total. If SWAN is OOM-killed, check: (1) inner nest domain is tight around surf spots, not covering the whole coast — the outer grid handles the shelf; (2) inner nest resolution is not too fine for the domain size. A 50 m resolution over a 30 km domain produces ~360,000 grid points (~6 GB). Use 200–500 m for typical setups.

**HRRR or GFS wind data unavailable**

If NOMADS returns an error or the current cycle has not yet posted, the SWAN runner skips that cycle and retries on the next (6 hours later for extended cycles). Surf forecasts continue to serve from the most recent successful run. If only GFS is unavailable, the runner produces a shortened forecast (HRRR hours 0–48 only). Check for errors:

```bash
journalctl -u weewx-clearskies-api | grep -i "hrrr\|trushore"
```

**Marine service unreachable**

When the marine service becomes unreachable, the API logs an ERROR and serves the last successful SWAN+TruShore cache. No manual intervention is required — the API resumes fetching fresh data when the service recovers. Check the **Marine Service** admin section to verify the connection status.

```bash
journalctl -u weewx-clearskies-api | grep -i "marine\|trushore"
```

---

## 6. First-Run Wizard

The setup wizard is a step-by-step process that configures your Clear Skies installation. It runs in your web browser at `https://your-domain/wizard`. Every step has a help button (the `?` icon in the upper right) that opens a side panel with detailed guidance.

This section provides an overview of each step. For detailed field-by-field help, use the in-app help panels — they are more detailed than what is practical to include in a printed manual.

### Language selection

Choose the language for the wizard and admin interfaces. This setting controls the wizard's own display language — it is separate from the language your visitors see on the weather site (that is set later in the Station Identity step).

The wizard supports 13 languages: English, German, Spanish, Filipino, French, Italian, Japanese, Dutch, Portuguese (Portugal), Portuguese (Brazil), Russian, Chinese (Simplified), and Chinese (Traditional). Each language is shown in its native script so you can find yours without needing to read English.

### API connection

The wizard needs to connect to the Clear Skies API to read your weather data and write configuration. On this step, you enter the API's address and verify its identity using a trust token and TLS fingerprint — similar to how SSH verifies a server's identity the first time you connect.

The API prints the trust token and fingerprint to its log when it starts for the first time. Check the API's log output to find these values.

### Skin import (optional)

If you are migrating from the Belchertown weewx skin, this step imports your existing settings — chart configuration, station metadata, and display preferences — so you do not have to re-enter them. Clear Skies reads your Belchertown `skin.conf` and translates the settings to its own format.

Skip this step if you are starting fresh or migrating from a different skin.

### License agreement

Read and accept the Clear Skies End User License Agreement (EULA). The EULA supplements the PolyForm Noncommercial License 1.0.0 that governs the software. Key points:

- Clear Skies is free for noncommercial, personal, educational, nonprofit, government, and community use
- Commercial use (advertising, paid access, managed hosting) requires a separate paid license
- You are responsible for complying with the terms of service of any external data providers you configure

The EULA is available in all 13 supported languages, but the English version is the legally binding document.

### Database

Clear Skies reads weather data from your weewx archive database. The wizard auto-detects your database type (SQLite or MariaDB) and connection settings from `weewx.conf`.

- **SQLite** — the default. No additional setup required. The database file is read in read-only mode.
- **MariaDB** — requires a database user with SELECT-only permissions. The wizard will test the connection before proceeding.

You generally do not need to change anything on this step unless the auto-detected settings are incorrect.

### Column mapping

This step maps the columns in your weewx database to the measurement names that Clear Skies uses. The wizard reads your database schema and makes its best guess at each mapping, showing a confidence level (high, medium, or low) for each one.

**If a column is not mapped, Clear Skies cannot read it** — that measurement will not appear anywhere on your weather site (no current reading, no charts, no records). The data is still safe in your weewx database; Clear Skies just does not know it exists.

Most operators can accept the defaults. Adjust mappings only if the wizard guessed incorrectly. You can always map skipped columns later from the admin interface — no data is lost.

### Station identity

Set your station's name, location, timezone, altitude, default visitor language, photo, and description.

**Pre-filled values:** If the API can read your `weewx.conf`, the location fields (latitude, longitude, altitude, timezone) are pre-filled automatically. Review them and adjust only if needed.

**Important:** Clear Skies does not write changes back to `weewx.conf`. If you correct your latitude, altitude, or other values here, also update `weewx.conf` to keep them in sync.

- **Latitude and longitude** — enter in decimal degrees (e.g. `40.7128` / `-74.0060`). Used for sun/moon calculations, provider geo-routing, timezone detection, and sky classification.
- **Altitude** — elevation above sea level, used for barometric pressure calculations.
- **Default language** — the language visitors see by default on your weather site (not the wizard language). Visitors can switch languages in their browser.
- **Station photo** — appears on the About page. Provide **alt text** — a short description of the image (e.g. "Davis Vantage Pro2 mounted on a rooftop pole") for visually impaired visitors using screen readers.
- **Station description** — a few sentences about your equipment, location, or history. Appears on the About page below the photo. Keep it to 2–3 sentences.

### Display units

Choose how weather data is displayed to visitors. Clear Skies supports the three standard weewx unit systems:

- **US Customary** — Fahrenheit, mph, inHg, inches
- **Metric** — Celsius, km/h, mbar, mm
- **MetricWX** — Celsius, m/s, hPa, mm

You can also override individual unit groups — for example, using Celsius for temperature but mph for wind speed. The **Distance** group also controls how earthquake depth and distance-from-station are displayed on the Seismic page. The unit system controls display only; the database stores data in whatever unit system weewx uses.

### Data providers

Configure where Clear Skies gets forecast, air quality, alert, earthquake, and radar data from. Each category has multiple provider options. Some require API keys (usually free for personal use); others work with no signup. The wizard guides you through key acquisition and tests each one before proceeding.

- **Forecast:** NWS (US only, no key needed), Open-Meteo (global, no key needed), OpenWeatherMap (global, key required), Vaisala Xweather (global, key required — free through PWSWeather Contributor Plan)
- **Air quality (AQI):** Open-Meteo (global, no key needed, model-based), IQAir (global, key required, observed), Vaisala Xweather (global, key required, observed)
- **Alerts:** NWS (US only, no key needed), Vaisala Xweather (global, key required), OpenWeatherMap (global, paid subscription required)
- **Earthquakes:** USGS (global, no key needed)
- **Radar:** RainViewer (global, no key needed), LibreWxR (global, no key needed — higher quality, self-hosting recommended)

AQI providers fall into two categories: **observed data** providers (Vaisala Xweather, IQAir) report particulate matter readings from nearby government monitoring stations, while **model-based** providers (Open-Meteo) estimate air quality from satellite and atmospheric models. Observed data enables haze detection in the sky classification engine; model-based data does not.

### Webcam (optional)

If you have a weather camera, enter the URLs for the live image and timelapse video, and set the refresh interval. Supported formats: JPEG, PNG, GIF, or WebP for still images; MP4 (H.264) for video. URLs can be relative paths on the same server (e.g. `/webcam/weather_cam.jpg`) or full URLs to external hosts (e.g. `https://cam.example.com/latest.jpg`). The webcam card appears on the Now page. If you do not have a camera, skip this step — the webcam card will not appear.

### Appearance and branding

Customize the visual appearance of your weather site:

- **Accent color** — the primary color used for interactive elements, charts, and highlights
- **Logos** — upload a file or enter a path/URL. SVG is preferred (scales to any size); PNG with a transparent background also works. Recommended dimensions: wide/horizontal layout, roughly 200–400 px wide by 40–80 px tall. Maximum 500 KB. Provide separate logos for light and dark themes, or just one for both. Include alt text for each logo.
- **Favicon** — browser tab icon. ICO or PNG, 32×32 or 64×64 px, max 100 KB.
- **Theme mode** — light, dark, Auto (OS preference), or Auto (sunrise/sunset)
- **Custom background** — upload a JPEG, PNG, or WebP image (max 5 MB, landscape orientation recommended) to replace the built-in day/night backgrounds.

### Privacy, legal, and analytics

Configure visitor tracking and privacy compliance:

- **Google Analytics** — enter your Measurement ID (starts with `G-`) to enable visitor tracking. Leave blank to disable — no tracking code will load.
- **Privacy regions** — controls the consent banner:
  - *None / Disabled* — no consent banner. **You are responsible for complying with any applicable privacy laws.**
  - *Global* — consent banner for all visitors (safest when in doubt)
  - *EU (GDPR)* — banner for European visitors
  - *US (CCPA)* — opt-out notice for California visitors
  - *Both* — EU and US rules together
- **Legal templates** — your weather site includes built-in Terms of Use and Privacy Policy templates in all 13 languages. **These are not legal advice — we are not lawyers.** You are responsible for verifying they are adequate for your jurisdiction. You can upload your own replacements (Markdown or plain text); uploaded documents are shown as-is and are not translated by Clear Skies.

### Feature settings

Configure optional features:

- **Earthquake radius** — how far from your station (in kilometers) to search for earthquake data. The Seismic page shows results within this radius.
- **Minimum magnitude** — filter threshold on the **Moment Magnitude scale (Mw)**, the standard used by the USGS and other agencies worldwide. The scale is logarithmic — each whole number represents roughly 32 times more energy released.
- **Default time range** — how many days of earthquake history to show when a visitor first opens the Seismic page.

### SWAN+TruShore (conditional)

This step appears when marine is enabled with at least one surf location configured and the connected marine service reports SWAN is installed and available. It collects the nested grid parameters (outer grid resolution, inner nest resolution, inner nest bounding box), the OpenMP thread count, and per-spot surf settings (breaker formula and surf height display convention). If the marine service is not connected or SWAN is not available, the wizard shows a notice and a **Skip** button — you can proceed without surf forecasting and configure the marine service later. See [§5 — Installation — weewx Extensions](#5-installation--weewx-extensions) for full SWAN installation details.

### TLS configuration

TLS (Transport Layer Security) is what makes the padlock icon appear in your browser's address bar and the URL start with `https://`. It encrypts traffic between visitors and your server.

- **Self-signed** — you provide your own self-signed certificate. Clear Skies does not generate one. Browsers show a security warning to visitors. Use only for testing or private networks. If your self-signed cert is on a separate proxy, select Behind Proxy instead.
- **ACME HTTP-01 (Let's Encrypt)** — automatic free certificate. Requires port 80 access from the internet and a domain name. Only use this if this server is directly internet-facing. If you run ACME on a separate tool (Nginx Proxy Manager, Traefik), select Behind Proxy.
- **ACME DNS-01 (Let's Encrypt)** — same free certs, validated via DNS records. Use when port 80 is blocked or for wildcard certificates.
- **Manual** — provide your own certificate and key files in PEM format. The certificate file should contain the server cert plus any intermediates (full chain). You can upload files directly through the wizard, or enter filesystem paths to files already on the server.
- **Behind Proxy** — choose this when something else handles TLS for you (Nginx Proxy Manager, Traefik, Cloudflare, a corporate load balancer). Clear Skies accepts plain HTTP from the upstream proxy.

### Review and apply

Review all your settings before applying. The wizard shows a summary of every configuration choice you made. When you click Apply:

1. The wizard writes configuration files to `/etc/weewx-clearskies/`
2. The API restarts to load the new configuration
3. The API warms its provider caches (approximately 2 minutes)
4. The weather site begins displaying your weather data

After the wizard completes, you are redirected to the weather site. Bookmark the admin page at `https://your-domain/admin` for ongoing configuration changes.

---

## 7. Admin Guide

The admin interface at `https://your-domain/admin` lets you change configuration settings after the initial wizard setup. You reach it by logging in with the admin credentials you created during the bootstrap step (the first time you visited the wizard URL).

Every section in the admin interface has a help button (the `?` icon) that opens detailed guidance. This section covers the most common tasks.

### Changing station identity

Update your station's name, location, timezone, altitude, photo, or description from the **Station Identity** section. Fields are pre-filled from the values you entered during setup. Remember that Clear Skies does not write changes back to `weewx.conf` — if you update location or altitude here, also update `weewx.conf` to keep them in sync. Changes take effect immediately — no restart required.

### Changing the database connection

Update database connection settings from the **Database** section. The form shows fields for the database type detected during setup (SQLite path or MySQL/MariaDB host, port, user, password). Test the connection before saving. Database changes require an API restart to take effect.

### Changing data providers

Each provider category (Forecast, Alerts, AQI, Earthquakes, Radar) has its own section in the admin. To switch providers:

- Select the new provider from the dropdown
- Enter the new provider's API key (if required)
- Test the connection using the **Test** button
- Save changes

The change takes effect after saving. If you switch forecast providers while the forecast correction engine is enabled, the training data resets because bias patterns differ between providers. (There was previously an Imagery provider selector here and in the setup wizard's provider step, choosing the aerial-photo source behind the marine heatmap — removed 2026-08-27 (Q10-6) now that the marine heatmap uses the Clear Skies product basemap instead.)

### Changing the appearance

Update colors, logos, theme, and custom background image from the **Appearance & Branding** section. Changes are written to `branding.json` and take effect on the next page load — visitors see the change when they refresh. A custom background image (JPG/PNG/WebP, max 5 MB) replaces the weather site's built-in day/night scene backgrounds; remove it to return to the built-in backgrounds.

### Managing page visibility

Hide or show weather site pages from the **Page Visibility** section. Hidden pages are removed from the navigation bar and their routes return a 404 to visitors. The Now page (home page) cannot be hidden.

### Editing the Now page layout

The **Now Page Layout** section provides a drag-and-drop editor for arranging the cards on the home page. Each card has a "footprint" (how many columns it spans) and an optional row span. Drag cards to reorder them, change their size, and save the layout. The weather site reads the layout on page load, so changes take effect on the visitor's next refresh.

### Adjusting column mapping

If your database schema changes (new columns, renamed columns), update the mappings in the **Column Mapping** section. Changed mappings affect what data appears in charts and current observations.

### Managing TLS certificates

Renew or change TLS certificates from the **TLS** section. If you are using ACME (Let's Encrypt), renewal is automatic — Caddy handles it. For Manual mode, the admin form takes filesystem **paths** to your certificate and key files (unlike the wizard, which also supports direct file upload). Copy updated certificate files onto the server first (e.g. via SCP/SFTP), then enter their paths. Both files must be in PEM format.

### Sky classification calibration

The **Sky Classification** section shows the thresholds the sky condition engine uses to distinguish clear, partly cloudy, mostly cloudy, and overcast skies. These thresholds are dynamically computed based on solar elevation and do not normally require adjustment. If your station consistently misclassifies sky conditions, review your pyranometer's calibration first.

### Haze calibration

The **Haze Calibration** section provides a monthly calibration grid for the haze detection system. The system uses a two-channel approach: solar radiation deficit plus particulate matter confirmation. Monthly calibration accounts for seasonal variations in typical aerosol levels at your location. The "Reset" button clears calibration data and triggers a re-bootstrap on the next API restart.

### Forecast correction

The **Forecast Correction** section controls the machine-learning system that learns to correct systematic temperature forecast errors. When enabled, the API collects forecast-observation pairs over time and trains a Random Forest model to predict and correct temperature bias. The section shows training data status, model accuracy metrics, and lets you trigger a manual retrain.

### Basemap

The **Basemap** section extracts the map ground — water, land, coastlines, administrative boundaries, place labels, and major roads — used by every Clear Skies map on your weather site: the marine map, the seismic map, the radar/satellite map, and the surf height map. The data comes from OpenStreetMap (via Protomaps) and is stored as three PMTiles files on the API host: a world tier (low detail, global fallback), a local tier (detailed, covering your station's area), and a radar tier (covering the radar provider's coverage area). Clear Skies extracts and serves these itself — no external map provider is required. The local tier's coverage is derived automatically from your station location, the earthquake search radius, and your configured marine locations — never typed in directly. The radar tier covers the radar provider's declared coverage box, or falls back to the station area when the provider does not declare one. Click **Update Basemap** to re-extract all three tiers; only one update runs at a time. Run once after initial setup, and re-run after changing the station location, the earthquake radius, the marine locations, or the radar coverage bounds. (There was previously a separate single-file OpenStreetMap overlay section here for the satellite/radar views — removed 2026-08-27 (ADR-078 Amendment 2, M5) now that this Basemap section's three-tier extraction covers the same OpenStreetMap data for every map.)

### Marine locations

The **Marine Locations** section manages marine, surf, fishing, and beach safety locations after initial setup — the ongoing counterpart to the setup wizard's Marine Locations step. Each location has a name, coordinates, one or more activities (Marine/Boating, Surf, Fishing, Beach Safety), and activity-specific fields (surf beach-facing direction/bottom type/topographic feature, fishing target category, beach safety links).

- **Add / Edit** opens a form with the same fields as the wizard step. Saving re-applies the full marine configuration and restarts the API so the change takes effect within a few seconds. Adding a *new* location requires a marine service connection to already be configured — see [Marine Service](#marine-service) below. Without one there is nothing to provide the location's data, so the save is refused rather than accepted silently. Editing a location that already exists is never blocked.
- **Delete** removes a location after confirmation.
- **Test** checks whether NDBC buoys and CO-OPS tide stations are reachable near the location's coordinates, and whether an NWS marine zone id is stored. This is a best-effort connectivity check, not a live verification that a specific station is currently transmitting data.
- **Update Bathymetry** (surf locations only) re-downloads the seafloor depth profile used for wave forecasting — useful after correcting a beach's facing direction.

### Marine Service

The **Marine Service** section manages the connection between the API and the external marine service that runs SWAN wave computations. This section appears when at least one location has surf activity enabled.

**Status panel** shows:

| Field | Description |
|-------|-------------|
| Connection | Whether the API can currently reach the marine service |
| Shared secret | Whether a shared secret is stored (shown as "Set" or "Not set") |
| TLS certificate verification | Whether TLS certificate verification is enabled for the marine service connection |
| Configured at | The URL the marine service is configured at, or "Not configured — no marine service is connected." if no URL has been saved |

**Configuration form** lets you update:

- **Marine service URL** — the address of the marine service, including scheme and port (e.g. `https://marine.example.com:8780`, `https://localhost:8780` on the same host, or `https://[2001:db8::1]:8780` for a bare IPv6 address). Leaving it blank does **not** disconnect a marine service that is already saved — the saved URL is left as it is. To disconnect one, delete its marine locations and then edit `api.conf [providers]` on the API host. The form will refuse to save a blank URL while any marine location is configured, because those locations would have nothing to provide their data.
- **Marine service secret** — the authentication secret for the marine service. Leave blank to keep the existing secret.
- **Verify the marine service's TLS certificate** — enable to verify the marine service's TLS certificate against system-trusted CAs. Disable only for self-signed certificates on trusted private networks.

Use **Test Connection** to verify the API can reach the marine service at the configured URL before saving. If the most recent wizard apply pushed marine configuration to the service and the push failed, a warning is shown here.

### Managing SWAN+TruShore

The **SWAN+TruShore** section shows current SWAN model status (binary availability, version, last run time, memory usage, and nested grid resolutions), lets you trigger a manual SWAN run, adjust OpenMP thread count and grid resolutions (outer and inner nest), and update per-spot surf settings (breaker formula and surf height display preference). Changes to per-spot settings take effect on the next SWAN run. For installation details see [§5 — Installation — weewx Extensions](#5-installation--weewx-extensions).

### Surf Score Weights

The **Surf Score Weights** section adjusts how much each of the five surf quality score components — Size, Shape, Conditions, Power, and Consistency — counts toward the score. This section appears when at least one location has surf activity enabled (same visibility condition as Marine Service).

The surf quality score is a weighted geometric mean of the five components (ADR-101): a very poor component drags the whole score down even if the others are strong, unlike a plain average. Each component's exponent in that geometric mean is one of the five weights below.

The form shows one number field per component:

| Field | Default |
|-------|---------|
| Size | 0.25 |
| Shape | 0.25 |
| Conditions | 0.20 |
| Power | 0.20 |
| Consistency | 0.10 |

**Entering weights.** Any positive number is accepted for each field — zero, negative, and non-numeric entries are rejected on save with a "must be positive" error next to the field, and nothing is saved until every field is valid. There is no upper bound and no requirement that the five values sum to any particular total: the weights are normalized by their sum when the score is computed, so only the proportions between them matter. An "Effective share" column next to each field shows the resulting percentage live as you type, and a **Reset to defaults** button fills in the shipped defaults above (not saved until you click **Save**).

**Scope.** One weight set applies system-wide — it is not configurable per surf spot.

**Saving and effect.** Saving writes the five weights as a top-level `[surf_scoring]` configuration section (a sibling of `[marine]`, not nested under it) via the same `/setup/apply` path the admin uses for other config changes; the API then pushes the section on to the marine service. The marine service re-reads its configuration on every surf forecast computation, so a saved change takes effect on the next `GET /surf` request — no service restart is required. Until you save for the first time, the section shows the built-in shipped defaults above; those defaults live in code on both the admin UI and the marine service's scorer, not in any config file, so an unconfigured system already scores using them.

The admin landing page's Surf Score Weights summary shows the current effective percentage for each component in the shipped order (Size / Shape / Conditions / Power / Consistency), or "Defaults" if nothing has been saved yet, or "API unreachable" if the API cannot be reached to read the current configuration.

---

## 8. Under the Hood

This section explains how Clear Skies processes, enriches, and displays your weather data. Understanding these systems is not required to operate Clear Skies, but it helps you interpret what you see on the weather site and troubleshoot unexpected behavior.

### Data flow: from station to weather site

```
Your weather station
  → weewx engine (reads hardware, stores archive records)
  → ClearSkiesLoopRelay extension (sends each observation to a Unix socket)
  → Clear Skies API (reads the socket, enriches the data, serves it via REST + SSE)
  → Caddy reverse proxy (handles TLS, routes requests)
  → Dashboard (React app in the visitor's browser, fetches data from the API)
```

**Real-time data** flows through the Loop Relay socket. Every time your station produces a reading (typically every 2–5 seconds), the Loop Relay forwards it to the API. The API enriches the reading with derived values and pushes it to connected browsers via Server-Sent Events (SSE) — a lightweight streaming protocol that keeps the weather site's "current conditions" area updated without refreshing the page.

**Historical data** (charts, records, reports) comes from the weewx archive database. The API queries the database and applies the same enrichment pipeline (unit conversion, derived values) before sending it to the weather site.

### Unit conversion pipeline

Clear Skies converts every number from its stored format to the display format you chose in the wizard:

1. **Source unit** — the unit system your weewx archive stores data in (US, Metric, or MetricWX)
2. **Unit group** — the category of measurement (temperature, wind speed, pressure, etc.)
3. **Display unit** — what your visitors see, per your configuration
4. **Label** — the unit symbol (°F, mph, inHg, etc.), resolved from the locale file

The API is the single authority for unit conversion. The weather site never does math on weather values — it receives already-converted numbers with labels attached and displays them as-is.

### Sky conditions engine

The sky conditions system determines labels like "Clear," "Partly Cloudy," or "Overcast" from your station's solar radiation sensor readings. It does not use cloud cameras or satellite imagery — it infers cloud cover from how much sunlight reaches your pyranometer compared to how much should reach it on a perfectly clear day.

**How it works:**

The engine maintains a rolling 30-minute window of 1-minute solar radiation averages. From this window, it computes four indices derived from atmospheric science research (Duchon & O'Malley 1999, Ruiz-Arias & Gueymard 2023):

- **Km** (clearness) — the ratio of measured solar radiation to the theoretical clear-sky maximum. A value of 1.0 means perfectly clear; lower values mean something is blocking sunlight.
- **Kv** (variability) — how much the radiation changes from minute to minute. High variability means clouds are moving across the sky (partly cloudy conditions). Low variability means the sky is uniform (either clear or fully overcast).
- **Kcs** (clear-sky index) — the ratio of measured to modeled clear-sky radiation, used to detect cloud enhancement (bright flashes when sunlight reflects off cloud edges).
- **Kvf** (filtered variability) — variability with the natural solar geometry change subtracted, isolating cloud-induced changes from the sun's normal arc across the sky.

The engine first checks variability: is the sky uniform or changing? Then it checks clearness: is it uniformly clear or uniformly cloudy? This two-step process produces seven labels: **Clear, Mostly Clear, Partly Cloudy, Mostly Cloudy, Cloudy, Overcast, Heavy Overcast**.

The thresholds are not fixed constants — they vary with solar elevation because the atmosphere's effect on sunlight changes as the sun moves from the horizon to overhead. The engine computes dynamic thresholds for each solar elevation angle.

**At night and twilight** (when the sun is below 15° elevation), the solar radiation sensor cannot distinguish sky conditions. The engine falls back to cloud cover data from your configured forecast provider.

**Startup behavior:** When the API starts, it reads the last 30 minutes of archive records to seed the engine's rolling window. Full-quality classification begins after about 30 minutes of live data.

### Enrichment pipeline

Before any weather data reaches the weather site, the API's enrichment pipeline adds derived values that your station hardware cannot measure directly:

- **Beaufort wind scale** — converts wind speed to the 0–12 Beaufort scale with descriptive labels ("Light Breeze," "Strong Gale," etc.)
- **Comfort index** — classifies how the temperature feels based on apparent temperature and dewpoint (e.g., "Warm and Humid," "Cold and Dry")
- **Barometer trend** — computes whether pressure is rising, falling, or steady, and at what rate
- **Wind averages** — rolling averages of wind speed and gust over configurable windows
- **Weather text** — composes a natural-language summary of current conditions (e.g., "Warm and Humid, Partly Cloudy, with Light Rain")
- **Sky classification** — the label from the sky conditions engine described above
- **Haze detection** — two-channel detection using solar radiation deficit plus particulate matter confirmation from your AQI provider

All derived values are computed by the API and sent to the weather site as ready-to-display data. The weather site does not carry any weather computation logic.

### Forecast correction engine

Clear Skies includes an optional machine-learning system that corrects systematic temperature forecast errors specific to your location. Forecast providers optimize for broad regional accuracy, but local microclimates (elevation, proximity to water, urban heat islands) create predictable biases that a local model can learn to correct.

**How it works:**

1. **Pair collection** — a background process continuously collects pairs of (forecast temperature, actual observed temperature) and stores them in a local database.
2. **Training** — periodically, a Random Forest regression model trains on the collected pairs, learning the relationship between forecast conditions (temperature, wind, humidity, time of day, season) and the typical forecast error at your station.
3. **Correction** — when the API serves forecast data, it passes the raw forecast temperatures through the trained model, which adjusts them to better match what your station typically observes.

The correction is applied only to temperature and only when enough training data has been collected (typically 2–4 weeks of forecast-observation pairs). Enable or disable it from the admin interface.

### Haze detection

The haze detection system identifies hazy conditions that a solar radiation sensor alone cannot distinguish from thin cloud cover. It uses two independent signals:

1. **Solar radiation deficit** — a drop in the clear-sky index (Kcs) below the dynamic clear threshold, indicating something is reducing sunlight
2. **Particulate matter confirmation** — elevated PM2.5 or PM10 readings from your AQI provider, confirming that the sunlight reduction is caused by airborne particles rather than clouds

Both signals must agree before "Hazy" appears in the conditions text. The PM thresholds are graduated by relative humidity (drier air requires higher PM to cause visible haze) and calibrated monthly to account for seasonal aerosol variations.

Haze detection only operates when the sun is above 15° elevation. At night, the system defers to the forecast provider's current conditions report.

Haze detection requires an observed-data AQI provider (Vaisala Xweather or IQAir). Model-based AQI providers (Open-Meteo) do not provide the particulate matter readings needed for confirmation.

---

## 9. Charts Configuration

Clear Skies uses a configuration file called `charts.conf` to define what charts appear on the Charts page and how they look. The format is INI-style (the same format weewx uses for `skin.conf`), with three levels of nesting: **groups** contain **charts**, and charts contain **series**.

If you migrated from the Belchertown skin, your existing `graphs.conf` was automatically converted to `charts.conf` by the migration tool during the wizard's import step.

### File location

`/etc/weewx-clearskies/charts.conf`

### Structure

```ini
# A group — appears as a tab on the Charts page
[[day]]
    title = "Today"
    time_length = 86400  # 24 hours in seconds

    # A chart within the group
    [[[temperature]]]
        title = "Temperature"

        # A series within the chart
        [[[[outTemp]]]]
            observation_type = outTemp
            color = "#FF0000"

        [[[[dewpoint]]]]
            observation_type = dewpoint
            color = "#00CC00"

    [[[wind]]]
        title = "Wind"

        [[[[windSpeed]]]]
            observation_type = windSpeed
            color = "#0066FF"

        [[[[windGust]]]]
            observation_type = windGust
            color = "#CC0000"
```

### Groups

A group is a time range. Common groups:

| Group name | `time_length` | What it shows |
|-----------|--------------|--------------|
| `day` | `86400` | Last 24 hours |
| `week` | `604800` | Last 7 days |
| `month` | `2592000` | Last 30 days |
| `year` | `31536000` | Last 365 days |

You can also use `xAxis_groupby` for calendar-aligned groups like monthly averages. See "Grouped charts" below.

### Charts

Each chart shows one or more data series on shared axes. Key settings:

| Setting | Example | Effect |
|---------|---------|--------|
| `title` | `"Temperature"` | Chart title displayed above the chart |
| `yAxis_label` | `"°F"` | Y-axis label (usually set automatically from the unit system) |
| `yAxis_min` | `0` | Minimum Y-axis value (useful for rain charts that should start at zero) |
| `yAxis_softMax` | `100` | Soft maximum — the axis extends beyond this value if data exceeds it |
| `type` | `line` | Default chart type for series in this chart (`line`, `spline`, `area`, `column`, `scatter`) |

### Series

Each series maps to a column in your weewx database:

| Setting | Example | Effect |
|---------|---------|--------|
| `observation_type` | `outTemp` | The database column to plot |
| `color` | `"#FF0000"` | Series color (hex code) |
| `type` | `spline` | Override the chart's default type for this series |
| `aggregate_type` | `max` | Aggregation function when data is binned (`avg`, `min`, `max`, `sum`, `sumcumulative`) |
| `yAxis` | `1` | Use the right Y-axis (for dual-axis charts) |

### Special series types

Three series names trigger automatic rendering behavior — you do not need to configure anything beyond placing the series in a chart:

**`windRose`** — renders a polar wind rose chart showing wind direction frequency and speed distribution. Uses 16 compass directions and 7 Beaufort speed bands with automatic color coding.

```ini
[[[windrose]]]
    title = "Wind Rose"
    [[[[windRose]]]]
```

**`weatherRange`** — renders a temperature range chart showing daily highs and lows with 15-band color zones (blue for cold, orange-red for hot). Default rendering is a column range chart.

```ini
[[[temprange]]]
    title = "Temperature Range"
    [[[[weatherRange]]]]
        observation_type = outTemp
```

**`haysChart`** — renders a circular 24-hour wind chart (inspired by Mount Washington Observatory) showing wind speed and gust by hour of day.

```ini
[[[haysuv]]]
    title = "Circular Wind"
    [[[[haysChart]]]]
```

### Cumulative rain

To show cumulative rainfall (a running total that resets each day/week/month), use `aggregate_type = sumcumulative`:

```ini
[[[rain]]]
    title = "Rain"

    [[[[rain]]]]
        observation_type = rain
        aggregate_type = sumcumulative
        color = "#0066FF"

    [[[[rainRate]]]]
        observation_type = rainRate
        aggregate_type = max
        color = "#CC0000"
        yAxis = 1
```

### Grouped charts (monthly/yearly averages)

For charts that aggregate by calendar period rather than a rolling time window, use `xAxis_groupby`:

```ini
[[climate]]
    title = "Average Climate"
    xAxis_groupby = month

    [[[avgtemp]]]
        title = "Average Temperature"

        [[[[outTemp]]]]
            observation_type = outTemp
            aggregate_type = avg
            average_type = max  # average of daily maximums
```

Supported `xAxis_groupby` values: `month`, `day`, `hour`, `year`.

### Custom SQL queries

Advanced operators can define custom SQL queries as chart series. The SQL is read from the configuration file (never from user input over the network) and is validated at API startup:

```ini
[[[[custom_dewpoint_spread]]]]
    custom_sql = "SELECT dateTime, outTemp - dewpoint AS value FROM archive WHERE dateTime >= ? AND dateTime <= ?"
```

The `?` placeholders are filled with the chart's time range boundaries by the API. Queries run in read-only transactions with a 10-second timeout.

### Migration from Belchertown

If you have an existing Belchertown `graphs.conf`, the migration tool converts it automatically:

```bash
clearskies-migrate-charts /path/to/your/graphs.conf /etc/weewx-clearskies/charts.conf
```

Most settings translate directly because `charts.conf` was designed to match Belchertown's format. The tool adds `# NOTE:` comments for any settings it could not translate.

---

## 10. Troubleshooting

### The weather site loads but shows no data

**Likely cause:** The API is not reachable from the weather site.

1. Check that the API is running: `systemctl status weewx-clearskies-api` (native) or `docker compose ps` (Docker)
2. Test the API directly: `curl -k https://localhost:8765/api/v1/status`
3. Test through Caddy: `curl https://your-domain/api/v1/status`
4. If the API is running but Caddy cannot reach it, check the Caddyfile's `reverse_proxy` address

### The API will not start

**Check the log:** `journalctl -u weewx-clearskies-api -n 100` (native) or `docker compose logs api` (Docker)

Common causes:

| Log message | Cause | Fix |
|------------|-------|-----|
| `FileNotFoundError: api.conf` | No configuration file | Run the setup wizard to generate `api.conf` |
| `FATAL: write-probe succeeded` | Database user has write access | Re-create the database user with SELECT-only permissions |
| `ConnectionRefusedError` (database) | Database is not running or wrong host/port | Check `[database]` section in `api.conf` |
| `ValueError: secret in .conf file` | API key or password found in `api.conf` instead of `secrets.env` | Move secrets to `secrets.env` and reference them as environment variables |

### No real-time updates (SSE not working)

The weather site's "current conditions" area should update every few seconds when you have a working station producing loop packets.

1. Check that the Loop Relay extension is running: look for `/var/run/weewx-clearskies/loop.sock`
2. Check the SSE endpoint directly: `curl -N https://your-domain/sse` — you should see events streaming every few seconds
3. If the socket exists but the API is not reading it, check the API log for socket connection errors

### Forecast or AQI shows "No data"

1. Check that the provider is configured: `https://your-domain/admin` → Data Providers
2. Test the provider key: use the "Test" button in the admin interface
3. Check the API log for provider errors: `journalctl -u weewx-clearskies-api | grep -i "provider\|error"`
4. Check that the provider supports your location — for example, NWS only covers the United States and its territories

### The wizard cannot connect to the API

1. Verify the API is running and listening on port 8765
2. Check that you entered the correct address in the wizard's API connection step
3. Verify the trust token matches — the API prints it once to the log on first start. If you missed it, restart the API and check the log immediately
4. If using a two-host setup, ensure the front-end host can reach the weewx host on port 8765 (check firewalls)

### Weather site shows stale data

1. Check the API's health endpoint: `curl -k https://localhost:8081/health/ready`
2. If the API reports "degraded," one or more providers may be unreachable. Check the API log for provider timeout errors.
3. Verify the archive database has recent records: `sqlite3 /var/lib/weewx/weewx.sdb "SELECT datetime(MAX(dateTime), 'unixepoch') FROM archive;"`

### TLS certificate errors

| Scenario | Fix |
|----------|-----|
| Browser shows "not secure" warning | If using self-signed certs, this is expected — add an exception. For public sites, configure Caddy with your domain for automatic Let's Encrypt certificates. |
| `curl: (60) SSL certificate problem` | Use `curl -k` to skip verification for testing. For production, ensure the Caddy TLS certificate is valid and the domain DNS is correct. |
| API-to-Caddy TLS errors | In the Caddyfile, use `tls_insecure_skip_verify` for self-signed API certs, or import the API's CA certificate. |

### High memory usage

The API uses approximately 600 MB of RAM at idle. This is normal — the scientific computing libraries (NumPy, SciPy, pandas, scikit-learn) are loaded at startup. If memory is a concern:

- Disable Redis and use in-memory caching (saves 2–250 MB depending on cache state)
- Disable forecast correction (saves the scikit-learn model memory)
- Consider a two-host split to distribute memory across machines

### Collecting information for a bug report

When reporting an issue, include:

1. **API log:** `journalctl -u weewx-clearskies-api -n 200 --no-pager` or `docker compose logs --tail 200 api`
2. **API version:** `curl -k https://localhost:8765/api/v1/status`
3. **Browser console:** Open browser developer tools (F12), switch to the Console tab, and copy any error messages
4. **Configuration (redacted):** Share your `api.conf` with API keys and passwords replaced by `[REDACTED]`
5. **System info:** OS version, Python version, Docker version (if applicable)

---

## 11. Getting Help

### Where to ask

**GitHub Issues** — the primary support channel for bug reports, feature requests, and questions:
[https://github.com/inguy24/weewx-clearskies-stack/issues](https://github.com/inguy24/weewx-clearskies-stack/issues)

When opening an issue:

1. **Search first** — your question may already be answered
2. **Use the issue template** — it asks for the information needed to diagnose your problem
3. **Include logs and configuration** — see "Collecting information for a bug report" in the Troubleshooting section
4. **One issue per report** — separate issues are easier to track and resolve

### What to include in a bug report

- A clear description of what you expected to happen and what actually happened
- Steps to reproduce the problem
- API log output (see Troubleshooting section for how to collect this)
- Your `api.conf` with secrets replaced by `[REDACTED]`
- Browser console errors (for weather site issues)
- Your operating system, Python version, and Docker version (if applicable)

### What not to include

- API keys, passwords, or tokens — always redact these before sharing
- Your full `secrets.env` file — never share this
- Personal information beyond what is needed to reproduce the issue

---

## 12. Support Scope

Clear Skies is a personal project maintained by a single developer. This section sets realistic expectations about what is supported, what is acknowledged but not supported, and what is outside the project's scope.

### Supported

The following are fully supported — bugs will be investigated and fixed:

- **Installation** via the documented Docker Compose and native (pip + systemd) paths on supported platforms
- **The setup wizard** — all steps, on all 13 supported languages
- **The admin interface** — all configuration sections
- **The weather site** — all 9 built-in pages, responsive layout, dark/light themes, WCAG 2.1 AA accessibility
- **Data providers** — all documented providers with correct API keys
- **Charts** — the `charts.conf` configuration system, including special series types (wind rose, weather range, Hays chart) and custom SQL
- **weewx extensions** — ClearSkiesLoopRelay and ClearSkiesTruesun
- **Internationalization** — all 13 supported locales in the weather site, wizard, and admin
- **Unit conversion** — all 14 weewx unit groups with per-group override

### Acknowledged but not actively supported

The following are known use cases that may work but are not tested or guaranteed:

- **Raspberry Pi 3** and earlier — insufficient RAM for comfortable operation
- **macOS native installs** — works for development but has no launchd template for production
- **Windows native installs** — not supported; use Docker Desktop with WSL2
- **weewx 4.x** — Clear Skies is built for weewx 5.x. weewx 4.x may work with the Loop Relay extension but is not tested
- **Custom reverse proxies** (Apache, nginx) — example configurations are provided but only Caddy is tested
- **LXD containers with nesting disabled** — Docker-in-LXD requires `security.nesting=true`

### Not documented and not supported

The following are outside the project's scope:

- **weewx installation and configuration** — refer to the [weewx documentation](https://weewx.com/docs/)
- **Domain name registration and DNS configuration**
- **Firewall and network configuration** beyond what is needed for Clear Skies
- **Home Assistant integration** — example configurations are provided as a starting point but are not maintained
- **Third-party data provider issues** — if a provider's API is down, returns incorrect data, or changes its terms of service, that is between you and the provider
- **Custom weather site development** — modifying the React source code, adding custom pages, or building plugins
- **Commercial deployment consulting** — contact the developer for commercial license terms

---

## 13. Legal

### License

Clear Skies is licensed under the **PolyForm Noncommercial License 1.0.0** for the three core repositories (API, dashboard, and stack). The full license text is in the `LICENSE` file in each repository.

The two weewx extensions (ClearSkiesLoopRelay and ClearSkiesTruesun) are licensed under the **GNU General Public License v3** because they are derivative works of weewx, which is GPL v3 licensed. The PolyForm Noncommercial license cannot apply to weewx extensions.

### What you can do without a commercial license

The PolyForm Noncommercial license plus the Additional Permitted Uses document (included as `ADDITIONAL-USES.md` in each core repository) permit the following uses at no cost:

- **Personal use** — running a weather site for yourself, your family, or your friends
- **Educational use** — schools, universities, research institutions
- **Government use** — federal, state, and local government agencies, regardless of size
- **Nonprofit use** — organizations recognized as tax-exempt under IRC 501(c)(3), (c)(4), (c)(6), (c)(7), or international equivalents
- **Community weather sharing** — HOAs, neighborhood groups, and voluntary associations sharing weather data without generating revenue
- **Family farms** — family-owned agricultural operations with fewer than 50 employees
- **Amateur radio and citizen science** — weather stations operated by licensed amateur radio operators, CWOP participants, Weather Underground personal stations
- **Agricultural cooperatives and CSA programs** — sharing weather data relevant to members and operations

### When you need a commercial license

A separate paid commercial license is required for:

- Displaying advertising on a Clear Skies–powered website
- Charging visitors for access (subscriptions, memberships, premium tiers)
- Operating Clear Skies as a managed service or hosted platform for third parties
- Using a weather page as a revenue-generating marketing tool (resort weather pages, real-estate sites, tourism websites)
- Use by publicly traded companies or organizations with more than 50 employees (except government and nonprofits)
- Reselling, white-labeling, or bundling Clear Skies in a commercial product

Contact the developer via [GitHub Issues](https://github.com/inguy24/weewx-clearskies-stack/issues) to discuss commercial licensing.

### Provider compliance

Clear Skies connects to external weather data providers (Vaisala Xweather, NWS, OpenWeatherMap, IQAir, Open-Meteo, USGS, RainViewer, and others). Each provider has its own terms of service, usage limits, and licensing requirements.

**You are responsible for complying with the terms of service of every provider you configure.** A Clear Skies license (whether free or commercial) does not grant you any rights to third-party data or services. In particular:

- Some providers offer free tiers for personal use but require paid plans for commercial use
- Some providers restrict how their data may be displayed, cached, or redistributed
- Provider terms of service may change at any time

Review each provider's terms before configuring your installation. The wizard links to each provider's registration page and terms of service during the provider configuration step.

### Translations and legal authority

- `LICENSE` and `ADDITIONAL-USES.md` — English only, never translated. These are the legally binding documents.
- `EULA` — available in all 13 supported languages. The English version is the sole legally binding document. All translated versions include a prominent disclaimer in both English and the target language stating this.
- Weather site Legal page — translated for visitor convenience. Non-English versions display a disclaimer banner noting that the English version is authoritative.
- Wizard, admin, and help content — fully translated for usability. These are interface elements, not legal instruments.

The English-language documents under California governing law are always authoritative. This follows the standard practice of major software projects: provide translations for understanding, but designate a single authoritative language for legal clarity.

### EULA summary

The End User License Agreement (EULA), which you accept during the setup wizard, supplements the PolyForm Noncommercial license with additional terms specific to operating a Clear Skies installation:

- **Third-party services:** You agree to comply with the terms of service of all external providers you configure
- **Data accuracy:** Clear Skies provides weather data on an "as-is" basis and makes no guarantees about accuracy, completeness, or timeliness
- **Operator responsibilities:** You are responsible for keeping your installation updated, securing your server, and complying with applicable privacy regulations in your jurisdiction
- **Warranty disclaimer and limitation of liability:** The software is provided without warranties, and liability is limited as described in the EULA
- **Termination:** Violating the license terms or EULA terminates your right to use the software

The full EULA text is in the `static/EULA.txt` file in the stack repository and is displayed during the wizard's EULA step.
