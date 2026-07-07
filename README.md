# PhotonScript

**Mission: maximize the use of every hour of shutter time, and turn it into award-winning, high-quality astrophotography.**

Remote telescope orchestration platform. Four autonomous agents coordinate to plan imaging sessions, monitor equipment, transfer files, and process astrophotography — so your telescope is always running and you only need to watch the results come in.

Built for a shared Starlink-connected telescope in New Mexico, but configurable for any site.

## One-command deploy

On the **scope PC**, start PhotonScript through the wrapper (instead of `photonscript start`):

```powershell
powershell -ExecutionPolicy Bypass -File C:\astro\PhotonScript\deploy\run-photonscript.ps1
```

From the **desktop**, after making changes:

```powershell
.\deploy\deploy.ps1 "what I changed"
```

This commits, pushes, and calls `POST /api/update` on the scope PC, which exits
with code 42; the wrapper then `git pull`s and restarts in the same console.
The nav-bar version stamp shows the commit each machine is running. Updates are
refused while a sequence is RUNNING so a night is never interrupted. The System
page has the same controls (`Check for updates` / `Pull latest & restart`).

## Architecture

```
                        You (anywhere)
                            |
                    +-------+-------+
                    |   Scheduler   |  Web UI + API (port 8100)
                    |  target plan  |  Seasonal catalog, NINA sequences
                    +---+-------+---+  AstroBin research
                        |       |
          message bus   |       |   message bus
                        v       v
        +---------------+       +----------------+
        | Telescope Agent|       |    Librarian   |
        | (Windows PC)   |       | image catalog  |
        | NINA + PHD2    |       | daytime xfer   |
        | image QA       |       | Starlink-aware |
        +----------------+       +-------+--------+
                                         |
                                         v
                                 +-------+--------+
                                 | Image Processor |
                                 | Siril / PI      |
                                 | stacking        |
                                 | progress feed   |
                                 +-----------------+
```

**Scheduler** — Runs anywhere (laptop, phone, cloud). Plans nightly sessions using astronomical calculations, generates NINA sequence files, ranks targets as Good/Better/Best based on seasonal visibility. Web dashboard with live WebSocket updates.

**Telescope Agent** — Runs on the Windows PC at the telescope. Monitors NINA (camera, mount, focuser, filter wheel, sequencer) and PHD2 (guiding RMS, star lock). Validates every captured sub-frame for FWHM, HFR, eccentricity, and tracking quality. Rejects bad frames automatically.

**Librarian** — Catalogs captured images and transfers validated frames from the remote telescope to your local machine. Transfers only during daytime hours (8 AM - 6 PM) to avoid competing with imaging on shared Starlink bandwidth.

**Image Processor** — Groups transferred subs by target and filter, runs automated stacking via Siril CLI (or optionally PixInsight), and feeds completion progress back to the scheduler.

## Quick Start

### Requirements

- Python 3.11+
- [NINA](https://nighttime-imaging.eu/) with Advanced API plugin (on telescope PC)
- [PHD2](https://openphdguiding.org/) (on telescope PC)
- [Siril](https://siril.org/) (optional, for automated stacking)

### Installation

```bash
git clone https://github.com/jhitchco/PhotonScript.git
cd PhotonScript
pip install -e ".[dev]"
```

### Configuration

```bash
cp config/example.env .env
# Edit .env with your observatory location, paths, and API keys
```

Key settings:

| Variable | Default | Description |
|----------|---------|-------------|
| `PS_OBSERVATORY_LAT` | `32.9` | Observatory latitude (degrees N) |
| `PS_OBSERVATORY_LON` | `-105.5` | Observatory longitude (degrees W) |
| `PS_OBSERVATORY_ELEV` | `2200` | Elevation in meters |
| `PS_OBSERVATORY_TZ` | `America/Denver` | Local timezone |
| `PS_SCHEDULER_PORT` | `8100` | Web UI port |
| `PS_NINA_BASE_URL` | `http://localhost:1888/api` | NINA Advanced API endpoint |
| `PS_PHD2_PORT` | `4400` | PHD2 server port |
| `PS_IMAGE_WATCH_DIR` | `C:\Astrophotography\Tonight` | NINA image output directory |
| `PS_TRANSFER_START_HOUR` | `8` | Transfer window start (local time) |
| `PS_TRANSFER_END_HOUR` | `18` | Transfer window end (local time) |
| `PS_TRANSFER_BANDWIDTH_LIMIT_MBPS` | `50` | Max transfer speed |
| `PS_QUALITY_FWHM_MAX` | `4.0` | Max acceptable FWHM (arcsec) |
| `PS_QUALITY_TRACKING_RMS_MAX` | `2.0` | Max guiding RMS (arcsec) |

### Usage

#### Browse tonight's targets

```bash
# Show targets for the current month, ranked by visibility
photonscript targets

# Show targets for a specific month
photonscript targets --month 7
```

Output shows each target with its tier (BEST/BETTER/GOOD), visibility hours, transit time, and recommended total integration.

#### Plan a session

```bash
# Plan tonight's imaging session
photonscript plan

# Plan for a specific date
photonscript plan --date 2026-04-15
```

The planner orders targets by meridian transit time to minimize slewing, allocates exposures proportionally, and fills the entire dark window.

#### Generate NINA sequences

```bash
# Generate NINA Advanced Sequencer file (JSON format)
photonscript sequence

# Save to a specific path
photonscript sequence --output "C:\NINA\Sequences\tonight.xml"
```

The sequence includes slew-and-center, autofocus, dithering every 3 frames, meridian flip triggers, filter changes, and camera cooling/parking.

#### Start the agents

```bash
# Start the scheduler web UI (run anywhere — laptop, server, etc.)
photonscript start --mode scheduler

# Start the telescope agent (run on the Windows telescope PC)
photonscript start --mode telescope

# Start librarian + image processor (run on your local machine)
photonscript start --mode librarian

# Start everything together (development/single-machine)
photonscript start --mode full
```

#### Check status

```bash
# Query the running scheduler for telescope status
photonscript status
```

### Web Dashboard

Once the scheduler is running, open `http://localhost:8100` in any browser. The dashboard shows:

- **Telescope status** — current state, target, filter, guiding RMS, camera temp, exposure progress
- **Tonight's plan** — ranked targets with visibility hours and tier badges
- **Project progress** — per-filter acquisition counts and completion percentages
- **Seasonal catalog** — browse targets by month
- **Activity log** — real-time feed of telescope events

The dark-themed UI is designed for night-time use and updates live via WebSocket.

### API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /api/status` | System status summary |
| `GET /api/tonight` | Tonight's planned targets and schedule |
| `GET /api/tonight/sequence.json` | Download NINA Advanced Sequencer JSON |
| `GET /api/tonight/sequence.xml` | Download NINA sequence XML |
| `GET /api/seasonal/{month}` | Seasonal targets for a given month (1-12) |
| `GET /api/projects` | List all imaging projects |
| `POST /api/projects` | Create a new imaging project |
| `GET /api/telescope/state` | Current telescope state |
| `POST /api/telescope/command` | Send command to telescope agent |
| `WS /ws` | WebSocket for live dashboard updates |

## Deployment

### Typical Setup

You need the agents running on two machines:

**On the remote telescope PC (Windows):**
```bash
photonscript start --mode telescope
```
This connects to NINA and PHD2 locally, watches for new images, and validates quality.

**On your local machine (or a server):**
```bash
photonscript start --mode scheduler
photonscript start --mode librarian
```
The scheduler serves the web UI. The librarian pulls images from the telescope PC via SSH during daytime hours and feeds them to the image processor for stacking.

### Transfer Setup

For SSH-based transfers from the telescope PC, configure:

```env
PS_TRANSFER_HOST=telescope-pc.local    # or IP address
PS_TRANSFER_PORT=22
PS_TRANSFER_USER=astro
PS_TRANSFER_KEY_PATH=~/.ssh/telescope_key
```

If the telescope PC is on a network share instead, leave `PS_TRANSFER_HOST` empty and set `PS_IMAGE_WATCH_DIR` to the UNC or mounted path.

### Bandwidth Management

The librarian enforces a strict daytime transfer window to protect imaging bandwidth on shared Starlink:

- **Night (6 PM - 8 AM):** No transfers. All bandwidth reserved for guiding and NINA API.
- **Day (8 AM - 6 PM):** Transfers run with a configurable rate limit (default 50 Mbps).
- Files are queued overnight and transferred in priority order the next morning.

## Seasonal Target Catalog

PhotonScript includes a curated catalog of 30+ deep-sky objects organized by season, pre-configured for optimal imaging from southern New Mexico:

| Season | Targets |
|--------|---------|
| **Winter** (Dec-Feb) | Orion Nebula (M42), Horsehead (B33), Rosette (NGC 2237), Crab (M1), Monkey Head (NGC 2174) |
| **Spring** (Mar-May) | Leo Triplet, Markarian's Chain, Whirlpool (M51), Sombrero (M104), Pinwheel (M101), Antennae Galaxies, Owl Nebula (M97) |
| **Summer** (Jun-Aug) | Eagle Nebula (M16), Lagoon (M8), Trifid (M20), Swan (M17), North America (NGC 7000), Veil Nebula, Crescent (NGC 6888), Ring (M57) |
| **Autumn** (Sep-Nov) | Andromeda (M31), Triangulum (M33), Heart (IC 1805), Soul (IC 1848), Pacman (NGC 281), Elephant Trunk (IC 1396), Bubble (NGC 7635), Cave (Sh2-155) |

The target ranker automatically selects appropriate targets for tonight and assigns tiers:

- **BEST** (top 20%) — Highest visibility, near meridian transit during peak darkness
- **BETTER** (next 30%) — Good visibility, solid imaging window
- **GOOD** (remainder) — Visible but shorter window or lower altitude

## Image Quality Thresholds

Every captured sub-frame is validated against configurable thresholds:

| Metric | Default Threshold | Description |
|--------|-------------------|-------------|
| FWHM | < 4.0 arcsec | Star sharpness (seeing + focus) |
| Eccentricity | < 0.6 | Star roundness (tracking/wind) |
| Tracking RMS | < 2.0 arcsec | PHD2 guiding error |
| Star count | >= 5 | Minimum detected stars |

Frames that fail QA are marked as rejected and not transferred, saving bandwidth.

## Exposure Planning

The planner auto-selects exposure strategies based on target type:

- **Emission nebulae / supernova remnants:** Narrowband (Ha 300s x40, OIII 300s x30, SII 300s x30) at gain 200 / offset 50
- **Galaxies / clusters:** Broadband LRGB (L 180s x60, RGB 180s x20 each) at gain 200 / offset 50

**Guiding:** unguided is the default — the iOptron CEM70G's absolute encoders track
at sub-arcsecond precision without PHD2. Pass `--guided` for very long subs; the
first guided target forces a fresh PHD2 calibration and dithers every 5 frames.

These defaults can be overridden per project via the API or when creating projects.

## AARO Deployment (single telescope PC)

Everything can run on the scope PC (Tailscale 100.94.189.77):

```
git clone <this repo> C:\astro\PhotonScript
cd C:\astro\PhotonScript
pip install -e ".[dev]"
copy config\example.env .env     # pre-filled with AARO values; add Pushover keys
photonscript start --mode full   # scheduler + telescope agent + librarian
```

Prerequisites on the scope PC: Python 3.11+, NINA 3.x with the **Advanced API
plugin** (enable API, note the port, keep localhost-bound), GroundStation plugin
(Pushover), and PHD2 with **Tools -> Enable Server** if you ever run guided.
Reach the dashboard from home via Tailscale: `http://100.94.189.77:8100`.

## The Nanny (telescope agent escalation)

The telescope agent watches every sub and escalates when something systemic
goes wrong — Pushover warning first, and (only if `PS_AUTO_ABORT_ON_SEVERE=true`)
a sequence stop. NINA's own Safety Monitor remains the hard weather backstop.

| Watch | Trigger | Severity |
| --- | --- | --- |
| Consecutive rejects | 3 rejected subs in a row (clouds, dew, focus, tracking) | severe |
| Cooling | cooler on but sensor >1C off the -10C setpoint | warn |
| Guide RMS | above threshold (guided runs), re-alerts hourly max | warn |
| Collimation/tilt | corner FWHM spread >0.35 vs frame median (RC16 watch) | warn (daily max) |
| Heartbeat | "still alive" ping every 30 min at low priority | info |

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Lint
ruff check photonscript/

# Type check
mypy photonscript/
```

## Project Structure

```
photonscript/
  shared/
    models.py            # Pydantic data models (targets, images, guiding, transfers)
    config.py            # Environment-based configuration
    astronomy.py         # Visibility, altitude, twilight, seasonal calculations
    database.py          # SQLAlchemy async database (SQLite)
    messagebus.py        # Async pub/sub for inter-agent communication
  scheduler/
    app.py               # FastAPI web application + WebSocket
    target_planner.py    # Night planning engine
    nina_sequence.py     # NINA XML sequence generator
    nina_sequence_json.py # NINA Advanced Sequencer JSON generator
    astrobin_client.py   # AstroBin API client
    templates/           # Dashboard HTML
    static/              # CSS + JavaScript
  telescope_agent/
    agent.py             # Main agent loop (NINA poll + PHD2 + file watch)
    nina_client.py       # NINA Advanced API client
    phd2_client.py       # PHD2 JSON-RPC client
    image_validator.py   # Star detection, FWHM/HFR/eccentricity analysis
  librarian/
    agent.py             # Transfer queue + daytime window enforcement
  image_processor/
    agent.py             # Siril/PixInsight stacking pipeline
  orchestrator.py        # Multi-mode agent launcher
  cli.py                 # Typer CLI (targets, plan, sequence, status, start)
```

## License

MIT
