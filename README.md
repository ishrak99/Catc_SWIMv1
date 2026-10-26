# Catalyst Center SWIM demo (simplified)

Reads a device CSV and a policy JSON, then runs **readiness → distribute → activate**
through Cisco Catalyst Center. There is no plan or approval step.

## Requirements

- Python **3.11+**
- Python packages (pinned in [requirements.txt](requirements.txt)):
  `ansible-core`, `dnacentersdk`, `httpx`
- Ansible collection (pinned in [requirements.yml](requirements.yml)):
  `cisco.dnac` 6.40.0 (and `ansible.utils` 5.1.2)
- Network access to Catalyst Center (HTTPS 443) and a user with SWIM permissions

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
ansible-galaxy collection install -r requirements.yml -p ./.collections
cp .env.example .env      # then fill in CC_USERNAME / CC_PASSWORD
```

`.env` is loaded automatically by `swim.py` (it is git-ignored; never commit it).

## Before you run: update these three files

The repo ships placeholders. Edit all three files for your environment first.

### 1. `.env` (credentials)

Copy `.env.example` to `.env` and set:

| Variable | Set to |
|---|---|
| `CC_USERNAME` | Catalyst Center user with SWIM (image distribute/activate) permissions |
| `CC_PASSWORD` | That user's password (keep it in single quotes) |

Optional, for an enterprise CA: `SSL_CERT_FILE` and `REQUESTS_CA_BUNDLE` (absolute path to the
CA bundle), and set `"verify_tls": true` in `policy.json`.

### 2. `devices.csv` (which devices)

Replace the placeholder `FXXXXXXXXX` with real chassis serial numbers, one per row.
Include every member serial of a stack or HA pair.

```csv
serial_number
FXXXXXXXXX
```

### 3. `policy.json` (where and what image)

| Field | Update to |
|---|---|
| `controller` | Catalyst Center IP or hostname (replace `X.X.X.X`; no `https://`, no port) |
| `verify_tls` | `true` if the controller has a trusted certificate, `false` only for labs |
| `images` key | Exact device series as shown in Catalyst Center inventory (e.g. `Cisco Catalyst 9300 Series Switches`) |
| `image_name` | Exact filename of the image already imported in Image Repository |
| `target_version` | Version that image installs (e.g. `17.15.6`); devices already on it are skipped |
| `allowed_current_versions` | Versions the devices run today and may upgrade from |
| `upgrade_mode` | `install`, `bundle` or `currentlyExists` |
| `operation_timeout_seconds` / `verification_timeout_seconds` | Increase for slow links or large images |

`description`, `controller_version`, `timezone`, `soak_seconds`, `inventory_max_age_seconds`
and `enforce_readiness_freshness` are not used by this simplified tool and can be left as is.

Prerequisite in Catalyst Center: the image must already be imported, and the devices must be
Reachable and Managed in inventory.

## Input formats

`devices.csv` – one serial per row (stack/HA members each on their own row):

```csv
serial_number
FXXXXXXXXX

```

`policy.json` – controller and approved image per device series:

```json
{
  "controller": "10.0.0.10",
  "verify_tls": false,
  "operation_timeout_seconds": 3600,
  "verification_timeout_seconds": 1800,
  "images": {
    "Cisco Catalyst 9300 Series Switches": {
      "image_name": "cat9k_iosxe.17.15.06.SPA.bin",
      "target_version": "17.15.6",
      "allowed_current_versions": ["17.15.5"],
      "upgrade_mode": "install"
    }
  }
}
```

Devices are skipped if not in inventory, no image is defined for their series, they are
already on `target_version`, or their version isn't in `allowed_current_versions`.

## Usage

```bash
python export_devices.py     # export inventory (hostname, serial, version) to runs/inventory-*.csv
python swim.py menu          # interactive menu
python swim.py readiness     # check readiness
python swim.py distribute    # copy image to devices
python swim.py activate      # activate image (devices reboot)
python swim.py full          # readiness, distribute, activate (stops on first failure)
```

Options: `--csv PATH`, `--policy PATH`, `--yes` (skip the confirmation prompt).

Each action lists the target devices and asks for confirmation first. Ansible results and
logs are written to `runs/`.

## Notes

- Controllers without the readiness-trigger API (HTTP 404) can't start readiness from
  this tool; it prints the current readiness status instead. Run readiness in
  Catalyst Center (Provision > Image Repository) if needed.
- Distribute failures such as `NCSW10079` (flash verification) are device-side: check
  free flash (`dir flash:`, `install remove inactive`) and the image checksum.
