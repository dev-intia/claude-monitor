#!/usr/bin/env python3
"""Install the monitor's hooks. Shows the exact change and asks before writing."""

import argparse
import sys

from claude_monitor.config import load_config
from claude_monitor.installer import SettingsError, install


def main() -> int:
    parser = argparse.ArgumentParser(description="Instala los hooks de claude-monitor.")
    parser.add_argument("--settings", help="Fichero de ajustes (por defecto, ~/.claude/settings.json)")
    parser.add_argument("--config", help="Fichero de configuración del panel")
    parser.add_argument("--dry-run", action="store_true", help="Enseña el cambio sin escribir nada")
    parser.add_argument("--yes", action="store_true", help="No preguntar antes de escribir")
    args = parser.parse_args()
    cfg = load_config(args.config)
    try:
        return install(args.settings or cfg.settings_path, str(cfg.state_dir), cfg.max_event_file_bytes,
                       yes=args.yes, dry_run=args.dry_run)
    except SettingsError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
