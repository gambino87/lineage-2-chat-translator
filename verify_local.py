"""Run the offline application self-test without joining a private model session.

The old standalone model probe could not authenticate after backend hardening:
session credentials belong only to the launcher process that owns the service.
Reuse the guarded headless entrypoint instead of bypassing that ownership check.
This checks local application assets and synthetic inputs, not model inference,
the user's chat log, or interactive Windows behavior.
"""
from installer_main import main as launcher_main


def main() -> int:
    return launcher_main(["--self-test"])


if __name__ == "__main__":
    raise SystemExit(main())
