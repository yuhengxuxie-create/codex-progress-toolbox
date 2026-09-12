"""Migrate an installed backend database after the updater has stopped and backed it up."""
from pathlib import Path
import os
import sys

def validate_preserved_paths(root: Path, database: Path) -> Path:
    root = root.resolve()
    preserved = root / ".state"
    # load_config resolves database paths. Inspect the original copy tree as well,
    # so an alias to another location inside .state cannot hide a reparse point.
    pending = [preserved]
    while pending:
        path = pending.pop()
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if path.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise RuntimeError("Preserved .state uses a reparse link; automatic migration is unsupported.")
        if path.is_dir():
            with os.scandir(path) as entries:
                pending.extend(Path(entry.path) for entry in entries)
    resolved = database.resolve()
    # Upgrade reconstructs only .state/.secrets/logs, not arbitrary root children.
    if not resolved.is_relative_to(preserved):
        raise RuntimeError("Database is outside the preserved .state directory; automatic migration is unsupported.")
    guardian = database.parent / "guardian"
    if not guardian.resolve().is_relative_to(preserved):
        raise RuntimeError("Guardian state is outside the preserved .state directory; automatic migration is unsupported.")
    checked = {database, guardian, preserved}
    for path in (database, guardian):
        checked.update(parent for parent in path.parents if parent.is_relative_to(root))
    if guardian.is_dir():
        checked.update(guardian.rglob("*"))
    for path in checked:
        if path.is_symlink() or (path.exists() and getattr(path.lstat(), "st_file_attributes", 0) & 0x400):
            raise RuntimeError("Database or guardian state uses a reparse link; automatic migration is unsupported.")
    return resolved


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(root / "src"))
    from progress_wx.config import load_config
    config = load_config(root / "config.yaml")
    database = validate_preserved_paths(root, config.service.database)
    if "--check-only" in sys.argv[2:]:
        print("Database and guardian paths are covered by the preserved .state directory.")
        return
    from progress_wx.state import StateStore
    store = StateStore(database)
    store.close()
    print("Installed database migration completed.")


if __name__ == "__main__":
    main()
