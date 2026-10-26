"""Export Catalyst Center inventory hostname, serial, and running software version."""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import json
from swim import ROOT, Center, load_env

FIELDS = ("hostname", "serial_number", "current_image")


def inventory_rows(devices: list[dict[str, Any]]) -> list[dict[str, str]]:
    rows = []
    for device in devices:
        values = {}
        for field in ("hostname", "serialNumber", "softwareVersion"):
            value = device.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"Unexpected non-text inventory field: {field}")
            values[field] = (value or "").strip()
        serials = list(dict.fromkeys(
            serial.strip() for serial in values["serialNumber"].split(",") if serial.strip()
        )) or [""]
        for serial in serials:
            rows.append({
                "hostname": values["hostname"], "serial_number": serial,
                "current_image": values["softwareVersion"],
            })
    return sorted(rows, key=lambda row: (row["hostname"].casefold(), row["serial_number"]))


def write_csv(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=ROOT / "policy.json")
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "runs" / f"inventory-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.csv",
        help="New CSV output path; existing files are never overwritten",
    )
    args = parser.parse_args(argv)
    try:
        if args.output.exists():
            raise ValueError(f"Output already exists; choose another filename: {args.output}")
        load_env()
        policy = json.loads(args.policy.read_text(encoding="utf-8"))
        controller = policy.get("controller")
        if not isinstance(controller, str) or not controller.strip():
            raise ValueError("Policy must contain a controller hostname or IP")
        center = Center(policy)
        devices = center.rows("/dna/intent/api/v1/network-device")
        rows = inventory_rows(devices)
        write_csv(args.output, rows)
        print(f"Exported {len(devices)} inventory devices as {len(rows)} rows to {args.output}")
        print("current_image contains the inventory softwareVersion, not an image filename.")
        return 0
    except KeyboardInterrupt:
        print("Export stopped; no device changes were submitted.", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"Export failed: {type(exc).__name__}. {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
