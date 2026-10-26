"""Simplified SWIM demo: devices.csv + policy.json -> distribute -> activate."""

from __future__ import annotations

import argparse
import csv
import json
import os
import ssl
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"


def load_env() -> None:
    """Load `export KEY='value'` lines from .env (local, else ../Demo_SWIM) if not already set."""
    import shlex

    for path in (ROOT / ".env", ROOT.parent / "Demo_SWIM" / ".env"):
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = shlex.split(line, comments=True)
            if parts and parts[0] == "export":
                parts = parts[1:]
            if parts and "=" in parts[0]:
                key, value = parts[0].split("=", 1)
                os.environ.setdefault(key, value)
        return


class Center:
    def __init__(self, policy: dict[str, Any]) -> None:
        import httpx

        user, password = os.getenv("CC_USERNAME"), os.getenv("CC_PASSWORD")
        if not user or not password:
            raise SystemExit("Set CC_USERNAME and CC_PASSWORD (source .env)")
        verify = policy.get("verify_tls", True)
        self.client = httpx.Client(
            base_url=f"https://{policy['controller']}",
            verify=ssl.create_default_context() if verify else False,
            timeout=httpx.Timeout(60, connect=10),
        )
        response = self.client.post("/dna/system/api/v1/auth/token", auth=(user, password))
        if response.status_code != 200:
            raise SystemExit(f"Authentication failed: HTTP {response.status_code}")
        self.client.headers["X-Auth-Token"] = response.json()["Token"]

    def get(self, path: str, **params: Any) -> Any:
        response = self.client.get(path, params=params)
        response.raise_for_status()
        return response.json().get("response")

    def rows(self, path: str) -> list[dict[str, Any]]:
        result, offset = [], 1
        while True:
            page = self.get(path, offset=offset, limit=500)
            result.extend(page)
            if len(page) < 500:
                return result
            offset += len(page)


def load_policy(path: Path) -> dict[str, Any]:
    policy = json.loads(path.read_text(encoding="utf-8"))
    for key in ("controller", "images", "operation_timeout_seconds"):
        if key not in policy:
            raise SystemExit(f"policy.json is missing '{key}'")
    return policy


def read_serials(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8-sig") as stream:
        rows = list(csv.DictReader(stream))
    serials = [(row.get("serial_number") or "").strip().upper() for row in rows]
    serials = [s for s in serials if s]
    if not serials:
        raise SystemExit("devices.csv has no serial_number values")
    return serials


def resolve_targets(center: Center, serials: list[str], policy: dict[str, Any]) -> list[dict]:
    targets = []
    for serial in serials:
        found = center.get("/dna/intent/api/v1/network-device", serialNumber=serial)
        if not isinstance(found, list) or len(found) != 1:
            print(f"SKIP {serial}: not found in inventory")
            continue
        device = found[0]
        spec = policy["images"].get(device.get("series"))
        if spec is None:
            print(f"SKIP {serial}: no policy image for series {device.get('series')!r}")
            continue
        if device.get("softwareVersion") == spec["target_version"]:
            print(f"SKIP {serial}: already on {spec['target_version']}")
            continue
        allowed = spec.get("allowed_current_versions")
        if allowed and device.get("softwareVersion") not in allowed:
            print(f"SKIP {serial}: version {device.get('softwareVersion')} not in {allowed}")
            continue
        targets.append({"serial": serial, "device": device, "image": spec})
    return targets


def module_config(target: dict, phase: str) -> list[dict]:
    device, spec = target["device"], target["image"]
    details = {
        "image_name": spec["image_name"],
        "device_ip_address": device["managementIpAddress"],
        "device_hostname": device["hostname"],
        "site_name": "Global",
    }
    if phase == "activate":
        details.update({
            "activate_lower_image_version": False,
            "distribute_if_needed": False,
            "device_upgrade_mode": spec.get("upgrade_mode", "install"),
            "schedule_validate": False,
        })
    key = "image_distribution_details" if phase == "distribute" else "image_activation_details"
    return [{key: details}]


def run_module(policy: dict, target: dict, phase: str) -> None:
    executable = Path(sys.executable).parent / "ansible-playbook"
    RUNS.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    result = RUNS / f"{stamp}-{target['serial']}-{phase}.json"
    with tempfile.TemporaryDirectory(prefix="swim-") as directory:
        variables = Path(directory) / "vars.json"
        variables.write_text(json.dumps({
            "controller": policy["controller"],
            "swim_config": module_config(target, phase),
            "verify_tls": policy.get("verify_tls", True),
            "task_timeout": policy["operation_timeout_seconds"],
            "result_path": str(result),
        }))
        env = dict(os.environ)
        env.update({
            "ANSIBLE_COLLECTIONS_PATH": str(ROOT / ".collections"),
            "ANSIBLE_CONFIG": str(ROOT / "ansible.cfg"),
            "ANSIBLE_LOCAL_TEMP": directory,
            "ANSIBLE_NOCOLOR": "1",
        })
        with result.with_suffix(".log").open("w") as log:
            code = subprocess.call(
                [str(executable), "-i", "localhost,", "-c", "local",
                 str(ROOT / "workflow.yml"), "--extra-vars", f"@{variables}"],
                stdout=log, stderr=subprocess.STDOUT, env=env, cwd=ROOT,
                timeout=policy["operation_timeout_seconds"],
            )
    if result.is_file():
        data = json.loads(result.read_text())
        if data.get("failed"):
            raise RuntimeError(f"{data.get('msg')} {json.dumps(data.get('response'))[:800]}")
    if code != 0 or not result.is_file():
        raise RuntimeError(f"Ansible failed; see {result.with_suffix('.log')}")


def wait_for_version(center: Center, target: dict, policy: dict) -> bool:
    deadline = time.monotonic() + policy.get("verification_timeout_seconds", 1800)
    while time.monotonic() < deadline:
        try:
            device = center.get(f"/dna/intent/api/v1/network-device/{target['device']['id']}")
            if device.get("softwareVersion") == target["image"]["target_version"]:
                return True
        except Exception:
            pass
        time.sleep(30)
    return False


def show_targets(title: str, targets: list[dict]) -> None:
    print(f"\n{title} targets:")
    for t in targets:
        d = t["device"]
        print(f"  {t['serial']}  {d['hostname']}  {d['softwareVersion']} -> "
              f"{t['image']['target_version']}  ({t['image']['image_name']})")


def confirm(yes: bool, warning: str = "") -> bool:
    if warning:
        print(f"WARNING: {warning}")
    if yes or input("Proceed? [y/N] ").strip().lower() == "y":
        return True
    print("Aborted.")
    return False


def print_legacy_readiness(center: Center, target: dict) -> None:
    serial = target["serial"]
    try:
        details = center.get("/api/v2/device-image/device", id=target["device"]["id"])
        detail = details[0] if details else {}
        print(f"[{serial}] readinessCheckStatus : {detail.get('readinessCheckStatus')}")
        print(f"[{serial}] upgradeStatus        : {detail.get('upgradeStatus')}")
        print(f"[{serial}] imageUpgradeStatus   : {detail.get('deviceImageUpgradeStatus')}")
        for key in ("error", "errorDetails"):
            if detail.get(key):
                print(f"[{serial}] {key}: {detail[key]}")
    except Exception as exc:
        print(f"[{serial}] could not read device image status: {exc}")
    print(f"[{serial}] Run/refresh readiness in Catalyst Center (Provision > Image Repository"
          f" > Updates) if status is stale.")


def run_readiness(center: Center, policy: dict, targets: list[dict]) -> int:
    failed = 0
    for t in targets:
        serial = t["serial"]
        try:
            response = center.client.post(
                f"/dna/intent/api/v1/networkDeviceImages/{t['device']['id']}/readinessChecks"
            )
            if response.status_code == 404:
                print(f"[{serial}] Readiness trigger API not available on this controller "
                      f"(HTTP 404: {response.text[:300]}). Reading current status instead.")
                print_legacy_readiness(center, t)
                continue
            if response.status_code not in (200, 202):
                raise RuntimeError(f"HTTP {response.status_code}: {response.text[:500]}")
            task_id = response.json()["response"]["taskId"]
            deadline = time.monotonic() + policy.get("verification_timeout_seconds", 1800)
            while time.monotonic() < deadline:
                task = center.get(f"/dna/intent/api/v1/tasks/{task_id}")
                if task.get("endTime") or task.get("isError"):
                    break
                time.sleep(10)
            if task.get("isError"):
                raise RuntimeError(task.get("failureReason") or task.get("progress"))
            print(f"[{serial}] readiness OK")
        except Exception as exc:
            failed += 1
            print(f"[{serial}] readiness FAILED: {exc}")
    return failed


def run_phase(center: Center, policy: dict, targets: list[dict], phase: str) -> int:
    failed = 0
    for t in targets:
        print(f"[{t['serial']}] {phase} ...", flush=True)
        try:
            run_module(policy, t, phase)
            if phase == "activate" and not wait_for_version(center, t, policy):
                raise RuntimeError("version not confirmed before timeout")
            print(f"[{t['serial']}] {phase} OK")
        except Exception as exc:
            failed += 1
            print(f"[{t['serial']}] {phase} FAILED: {exc}")
    return failed


def perform(action: str, center: Center, policy: dict, serials: list[str], yes: bool) -> int:
    targets = resolve_targets(center, serials, policy)
    if not targets:
        print("Nothing to do.")
        return 1
    if action == "readiness":
        show_targets("READINESS", targets)
        return 1 if not confirm(yes) else int(bool(run_readiness(center, policy, targets)))
    if action == "full":
        show_targets("READINESS + DISTRIBUTE + ACTIVATE", targets)
        if not confirm(yes, "activation reboots the devices."):
            return 1
        steps = (("readiness", None), ("distribute", "distribute"), ("activate", "activate"))
        for name, phase in steps:
            failed = run_readiness(center, policy, targets) if phase is None else run_phase(
                center, policy, targets, phase)
            if failed:
                print(f"Stopping: {name} failed.")
                return 1
        return 0
    show_targets(action.upper(), targets)
    warning = "activation reboots the devices." if action == "activate" else ""
    if not confirm(yes, warning):
        return 1
    return int(bool(run_phase(center, policy, targets, action)))


def menu(center: Center, policy: dict, serials: list[str], yes: bool) -> int:
    options = {"1": "readiness", "2": "distribute", "3": "activate", "4": "full"}
    while True:
        print("\nCatalyst Center SWIM workflow")
        print("1. Check readiness")
        print("2. Distribute an image")
        print("3. Activate an image")
        print("4. Run readiness, distribute, and activate")
        print("q. Quit")
        choice = input("> ").strip().lower()
        if choice in ("q", "quit"):
            return 0
        if choice in options:
            try:
                perform(options[choice], center, policy, serials, yes)
            except Exception as exc:
                print(f"ERROR: {exc}")
        else:
            print("Choose 1-4 or q.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("menu", "readiness", "distribute", "activate", "full"))
    parser.add_argument("--csv", type=Path, default=ROOT / "devices.csv")
    parser.add_argument("--policy", type=Path, default=ROOT / "policy.json")
    parser.add_argument("--yes", action="store_true", help="skip confirmation prompt")
    args = parser.parse_args()

    load_env()
    policy = load_policy(args.policy)
    serials = read_serials(args.csv)
    center = Center(policy)
    if args.command == "menu":
        return menu(center, policy, serials, args.yes)
    return perform(args.command, center, policy, serials, args.yes)


if __name__ == "__main__":
    sys.exit(main())
