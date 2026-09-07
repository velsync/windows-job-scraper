"""Package entry module: ``python -m jobscraper`` / packaged ``JobScraper.exe``.

Modes:

* (default)  launcher — the double-click product entry;
* ``--service``        internal service process (spawned by the launcher);
* ``--browser-worker`` internal browser worker process (S0.9);
* ``--doctor``         run Doctor diagnostics (S0.10);
* ``--version``        print application identity.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if "--version" in argv:
        from jobscraper.version import APP_NAME, APP_VERSION, SCHEMA_VERSION

        print(f"{APP_NAME} {APP_VERSION} (schema {SCHEMA_VERSION})")
        return 0

    from jobscraper.config import config_from_env

    def _config_with_root():
        config = config_from_env()
        if "--data-root" in argv:
            index = argv.index("--data-root")
            try:
                root = argv[index + 1]
            except IndexError:
                print("--data-root requires a path", file=sys.stderr)
                raise SystemExit(2)
            config = config.with_data_root(root)
        return config

    if "--service" in argv:
        from jobscraper.service.runner import run_service

        return run_service(_config_with_root())

    if "--browser-worker" in argv:
        from jobscraper.browser_worker.main import main as browser_worker_main

        return browser_worker_main()

    if "--doctor" in argv:
        from jobscraper.launcher.doctor import main as doctor_main

        return doctor_main([a for a in argv if a != "--doctor"])

    from jobscraper.launcher.main import main as launcher_main

    return launcher_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
