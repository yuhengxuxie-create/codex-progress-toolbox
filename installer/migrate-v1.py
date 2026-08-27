from __future__ import annotations

import json
from pathlib import Path
import sys

import yaml


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: migrate-v1.py CONFIG PLAN_JSON")
    config_path = Path(sys.argv[1]).resolve()
    plan_path = Path(sys.argv[2]).resolve()
    config = yaml.safe_load(config_path.read_text(encoding="utf-8-sig")) or {}
    plan = json.loads(plan_path.read_text(encoding="utf-8-sig"))
    ids = plan.get("thread_ids")
    if not isinstance(ids, list) or not all(isinstance(value, str) for value in ids):
        raise ValueError("invalid migration plan")
    monitor = config.setdefault("monitor", {})
    if not isinstance(monitor, dict):
        raise ValueError("monitor must be a mapping")
    existing = monitor.get("ids") or []
    if not isinstance(existing, list):
        raise ValueError("monitor.ids must be a list")
    monitor["ids"] = list(dict.fromkeys([*existing, *ids]))
    temporary = config_path.with_suffix(config_path.suffix + ".migrating")
    temporary.write_text(yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8")
    temporary.replace(config_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

