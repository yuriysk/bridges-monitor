import sys


def main() -> None:
    from bridges_monitor.cli import main as cli_main

    sys.exit(cli_main())
