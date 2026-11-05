#!/usr/bin/env python3
"""Start the claude-monitor panel on 127.0.0.1."""

import argparse
import sys

from claude_monitor.config import load_config
from claude_monitor.server import HOST, Monitor, create_server


def main() -> int:
    parser = argparse.ArgumentParser(description="Panel local de sesiones de Claude Code.")
    parser.add_argument("--port", type=int, help="Puerto (por defecto, el de config.toml)")
    parser.add_argument("--config", help="Fichero de configuración")
    args = parser.parse_args()
    if sys.version_info < (3, 13):
        print("Error: claude-monitor necesita Python 3.13 o superior.", file=sys.stderr)
        return 2
    cfg = load_config(args.config)
    port = args.port or cfg.port
    try:
        server = create_server(port)  # Before anything else: a busy port leaves no trace.
    except OSError as error:
        print(
            f"Error: no se puede usar el puerto {port} en {HOST}: está ocupado o no disponible ({error}).\n"
            f"Cierra lo que lo use o elige otro con --port. El panel no cambia de puerto por su cuenta.",
            file=sys.stderr,
        )
        return 1
    monitor = Monitor(cfg)
    server.monitor = monitor
    monitor.start()
    print(f"claude-monitor en http://{HOST}:{port}/  (Ctrl+C para salir)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Parando…", flush=True)
    finally:
        server.server_close()
        monitor.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
