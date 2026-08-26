"""Allow ``python -m progress_notify``."""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
