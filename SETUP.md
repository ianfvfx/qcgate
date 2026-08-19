# QCGate — Setup Guide

---

## Prerequisites

**Python 3.9** — this is a hard constraint. Do not use 3.10 or later.

**ffmpeg and ffprobe:**

```bash
# Ubuntu/Debian
sudo apt install ffmpeg

# macOS
brew install ffmpeg
```

Note the paths after installation — you will need them:
```bash
which ffmpeg
which ffprobe
```

**Tesseract OCR:**

```bash
# Ubuntu/Debian
sudo apt install tesseract-ocr

# macOS
brew install tesseract
```

**OpenCV system dependencies (Linux only):**

```bash
sudo apt install libgl1
```

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/ianfvfx/qcgate.git /opt/qcgate
cd /opt/qcgate
```

### 2. Create a virtual environment

```bash
python3.9 -m venv venv
source venv/bin/activate
```

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 4. Create the .env file

```bash
cp .env.example .env
```

Edit `.env`:

```
SECRET_KEY=replace-with-a-long-random-string
DATABASE_PATH=data/qcgate.db
```

`SECRET_KEY` signs login session cookies — use a long random string and keep it private.

### 5. Initialise the database

Run once only. Creates `data/qcgate.db` and all tables, and prompts you to create the first admin account.

```bash
python3 scripts/init_db.py
```

**Never run `init_db.py` again on a database that contains real data** — it will not overwrite existing tables, but any future changes to the schema must be applied as migrations. See QCGATE.md for the migration pattern.

### 6. Configure paths

Log in to the admin panel at `http://localhost:8000/admin` after first run, or set config directly:

```bash
python3 - <<'EOF'
import sys
sys.path.insert(0, ".")
from qcgate import config

config.set("watch_path",      "/media/jobs/*/library/qcgate")
config.set("passed_path",     "/media/jobs/*/library/qcgate/passed")
config.set("failed_path",     "/media/jobs/*/library/qcgate/failed")
config.set("mediavault_path", "/media/mediaVault")
config.set("ffmpeg_path",     "/usr/bin/ffmpeg")
config.set("ffprobe_path",    "/usr/bin/ffprobe")
config.set("tesseract_path",  "/usr/bin/tesseract")
config.set("qc_frames_path",  "/opt/qcgate_qc_frames")
print("Done.")
EOF
```

Adjust paths to match your environment. The `*` in watch/passed/failed paths is replaced with the job folder name at runtime.

---

## Running

### Development

```bash
source venv/bin/activate

# Web server (with auto-reload)
uvicorn qcgate.web.app:app --reload --port 8000

# File watcher (separate terminal)
python -m qcgate.watcher
```

### Production (systemd)

Two systemd service files are provided in `systemd/`. Install them:

```bash
sudo cp systemd/qcgate-web.service /etc/systemd/system/
sudo cp systemd/qcgate-watcher.service /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable qcgate-web qcgate-watcher
sudo systemctl start qcgate-web qcgate-watcher
```

Check status:

```bash
sudo systemctl status qcgate-web
sudo systemctl status qcgate-watcher
```

View logs:

```bash
journalctl -u qcgate-web -f
journalctl -u qcgate-watcher -f
```

The web server binds to `0.0.0.0:8000`. Put nginx or another reverse proxy in front for production use.

---

## Linux inotify Limits

If running on Linux with a large job tree (many subdirectories), raise the inotify limits:

```bash
echo fs.inotify.max_user_watches=524288 | sudo tee -a /etc/sysctl.conf
echo fs.inotify.max_user_instances=512  | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

---

## Importing Historical Vault Assets

If you have existing assets in the mediaVault that predate QCGate, use the import script to register them without moving or altering any files:

```bash
# List all jobs in the vault with file counts and import status
python3 scripts/import_vault.py --list

# Preview what would be imported (no writes)
python3 scripts/import_vault.py --job JOB_NAME --dry-run

# Import records and generate proxies
python3 scripts/import_vault.py --job JOB_NAME

# Import without generating proxies
python3 scripts/import_vault.py --job JOB_NAME --no-proxies

# Newest file wins on duplicate master names
python3 scripts/import_vault.py --job JOB_NAME --reverse
```

Imported masters are created with `status = Passed` and `vault_path` set to the file's location in the vault.

---

## Database Backup

Safe live backup using Python's built-in SQLite backup API:

```bash
cd /opt/qcgate && python3 - <<'EOF'
import sqlite3
from datetime import date
src = sqlite3.connect("data/qcgate.db")
dst = sqlite3.connect(f"data/qcgate.db.backup_{date.today().strftime('%Y%m%d')}")
src.backup(dst)
dst.close()
src.close()
print("Done.")
EOF
```

---

## Updating

```bash
cd /opt/qcgate
git pull
source venv/bin/activate
pip install -r requirements.txt

sudo systemctl restart qcgate-web qcgate-watcher
```

If the update includes schema changes, a migration snippet will be provided alongside it. Run the migration before restarting the services.

---

## Troubleshooting

**Web server won't start — port already in use:**
```bash
sudo lsof -i :8000
sudo kill -9 <PID>
```

**Watcher not picking up files:**
- Confirm the watch path glob resolves correctly: the `*` must match the job folder name
- Check inotify limits (see above)
- Check the watcher log: `journalctl -u qcgate-watcher -f`

**Proxies not generating:**
- Confirm `ffmpeg_path` in config points to a valid ffmpeg binary
- Check `proxy_concurrency` — if set to 0 no proxies will generate

**OCR returning no text:**
- Confirm `tesseract_path` in config points to a valid Tesseract binary
- Tesseract must have English language data installed (`tesseract-ocr-eng` on Ubuntu)

**Authentication issues after dependency changes:**
- Do not upgrade `bcrypt` or `passlib`. The pinned versions (`bcrypt==4.0.1`, `passlib==1.7.4`) are required. Upgrading bcrypt to 5.x will break password verification on Python 3.9.

---

## Full Technical Reference

See **QCGATE.md** for the complete technical reference, including: database schema, all web routes, configuration keys, architecture overview, and known issues.
