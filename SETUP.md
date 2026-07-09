# QCGate — Setup Guide

---

## macOS Setup (Development / Test)

These instructions are for running QCGate locally on a Mac workstation,
connecting to real job storage on the network.

### 1. Prerequisites

**Python 3.11 or later.** Check your version:

```bash
python3 --version
```

If you need to install or upgrade:

```bash
brew install python
```

**ffmpeg** (includes ffprobe):

```bash
brew install ffmpeg
```

Confirm ffprobe is available and note its path — you'll need this later:

```bash
which ffprobe
which ffmpeg
```

On a Mac with Apple Silicon and Homebrew these will likely be:
```
/opt/homebrew/bin/ffprobe
/opt/homebrew/bin/ffmpeg
```

---

### 2. Project folder

All QCGate files live at:

```
/Users/ian.fallon/Documents/qcgate/
```

Open this folder in PyCharm: **File → Open → select the qcgate folder**.

---

### 3. Create a virtual environment

In the PyCharm terminal:

```bash
cd /Users/ian.fallon/Documents/qcgate
python3 -m venv venv
source venv/bin/activate
```

You should see `(venv)` at the start of your terminal prompt.

---

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

---

### 5. Create your .env file

```bash
cp .env.example .env
```

Open `.env` in PyCharm and set it to:

```
SECRET_KEY=change-me-to-a-long-random-string
DATABASE_PATH=data/qcgate.db
```

Replace `change-me-to-a-long-random-string` with any long random string of
your choice. This is used to sign login sessions — keep it private.

---

### 6. Initialise the database

```bash
python scripts/init_db.py
```

You will be prompted to create an admin username and password.
This creates `data/qcgate.db` and all tables.

---

### 7. Verify the installation

Run this to confirm the database and config are correct:

```bash
python3 - <<'EOF'
import sqlite3
conn = sqlite3.connect("data/qcgate.db")
tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print("Tables:", [t[0] for t in tables])
config = conn.execute("SELECT key, value FROM config").fetchall()
print("\nConfig:")
for row in config:
    print(f"  {row[0]}: {row[1]}")
conn.close()
EOF
```

Expected output:

```
Tables: ['jobs', 'masters', 'iterations', 'users', 'conflicts', 'config']

Config:
  watch_path: /jobs/*/masters/for_qc
  failed_path: /jobs/*/masters/failed
  passed_path: /jobs/*/masters/passed
  ffmpeg_path: /usr/bin/ffmpeg
  ffprobe_path: /usr/bin/ffprobe
```

The config values shown are defaults — you will update them in the next step.

---

### 8. Update config for the test environment

Run this script to set the correct paths for the test environment:

```bash
python3 - <<'EOF'
import sys
sys.path.insert(0, ".")
from qcgate import config

config.set("watch_path", "/Volumes/jobs/blackKiteStudios_01234/library/mastersExport/forQC")
config.set("failed_path", "/Volumes/jobs/blackKiteStudios_01234/library/mastersExport/failed")
config.set("passed_path", "/Volumes/jobs/techOpsTestProject_03991/library/mastersExport/{job}")
config.set("ffprobe_path", "/opt/homebrew/bin/ffprobe")
config.set("ffmpeg_path", "/opt/homebrew/bin/ffmpeg")

print("Config updated:")
for key, data in config.get_all().items():
    print(f"  {key}: {data['value']}")
EOF
```

If your ffmpeg/ffprobe are not at `/opt/homebrew/bin/`, replace those paths
with the output of `which ffprobe` and `which ffmpeg` from Step 1.

---

### 9. Confirm the watch folder exists

QCGate will not create folders on the network — they must already exist.
Confirm the following paths are accessible from your Mac:

```bash
ls /Volumes/jobs/blackKiteStudios_01234/library/mastersExport/forQC
ls /Volumes/jobs/blackKiteStudios_01234/library/mastersExport/failed
ls /Volumes/jobs/techOpsTestProject_03991/library/mastersExport/
```

If any of these return `No such file or directory`, create them:

```bash
mkdir -p /Volumes/jobs/blackKiteStudios_01234/library/mastersExport/forQC
mkdir -p /Volumes/jobs/blackKiteStudios_01234/library/mastersExport/failed
mkdir -p /Volumes/jobs/techOpsTestProject_03991/library/mastersExport/
```

---

## Next Steps

Once setup is complete and verified, the next phase covers:

- The file watcher (`qcgate/watcher.py`)
- ffprobe metadata extraction (`qcgate/ffprobe.py`)
- File ingest logic (`qcgate/ingest.py`)
- File movement logic (`qcgate/filemover.py`)
