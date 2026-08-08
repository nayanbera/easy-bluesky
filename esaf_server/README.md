# ESAF Server

FastAPI service that stores Experiment Safety Assessment Form (ESAF) records for synchrotron beamlines. Runs on the Linux beamline machine alongside the bluesky RE Manager.

## Installation

```bash
cd /path/to/easy-bluesky
pip install -r esaf_server/requirements.txt
```

Or into a conda environment:

```bash
conda activate easy-bluesky
pip install -r esaf_server/requirements.txt
```

## Configuration

The server reads `~/.easy_bluesky/esaf_server/config.json`. If absent, it creates defaults (SQLite backend, port 8765).

Example config with MongoDB backend:

```json
{
  "backend": "mongodb",
  "mongodb": {
    "uri": "mongodb://localhost:27017",
    "database": "esaf_db"
  },
  "server": {
    "host": "0.0.0.0",
    "port": 8765,
    "api_key": "change-me-to-something-secret"
  }
}
```

Example config with SQLite backend (default):

```json
{
  "backend": "sqlite",
  "sqlite": {
    "db_path": "~/.easy_bluesky/esaf_server/esaf.db",
    "pdf_dir": "~/.easy_bluesky/esaf_server/pdfs/"
  },
  "server": {
    "host": "0.0.0.0",
    "port": 8765,
    "api_key": ""
  }
}
```

If `api_key` is empty, all operations are open. If set, POST/PUT/DELETE requests must include the header `X-API-Key: <key>`. GET requests are always open.

## Running

### Direct

```bash
uvicorn esaf_server.main:app --host 0.0.0.0 --port 8765
```

### With procServ (recommended for beamline deployment)

```bash
procServ -L /tmp/esaf-server.log -p /tmp/esaf-server.pid 8766 \
  uvicorn esaf_server.main:app --host 0.0.0.0 --port 8765
```

### As a systemd service

Create `/etc/systemd/system/esaf-server.service`:

```ini
[Unit]
Description=ESAF Server
After=network.target mongod.service

[Service]
User=chem_epics
WorkingDirectory=/home/chem_epics
ExecStart=/home/chem_epics/anaconda3/envs/easy-bluesky/bin/uvicorn \
  esaf_server.main:app --host 0.0.0.0 --port 8765
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable esaf-server
sudo systemctl start esaf-server
```

## Admin UI

Once running, visit `http://<beamline-host>:8765/admin` in a browser.

Navigation:
- **ESAFs** — list, search, and filter all ESAF records
- **PI Groups** — manage PI group definitions and member lists
- **Upload PDF** — parse an APS ESAF PDF and review extracted fields before saving
- **OpenAPI Docs** — interactive REST API docs at `/docs`

## REST API

Base URL: `http://<host>:8765`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Service health check |
| GET | `/api/esafs` | List ESAFs (params: `pi_group`, `beamline`, `search`) |
| GET | `/api/esafs/{id}` | Get one ESAF |
| POST | `/api/esafs` | Create ESAF (auth) |
| PUT | `/api/esafs/{id}` | Update ESAF (auth) |
| DELETE | `/api/esafs/{id}` | Delete ESAF (auth) |
| POST | `/api/esafs/parse-pdf` | Parse a PDF, return extracted fields (auth) |
| POST | `/api/esafs/{id}/pdf` | Upload PDF for an ESAF (auth) |
| GET | `/api/esafs/{id}/pdf` | Download stored PDF |
| GET | `/api/pi_groups` | List PI groups |
| GET | `/api/pi_groups/{slug}` | Get one PI group |
| POST | `/api/pi_groups` | Create PI group (auth) |
| PUT | `/api/pi_groups/{slug}` | Update PI group (auth) |
| DELETE | `/api/pi_groups/{slug}` | Delete PI group (auth) |
| GET | `/api/pi_groups/match/{name}` | Find PI groups by member name |

Full interactive documentation: `http://<host>:8765/docs`
