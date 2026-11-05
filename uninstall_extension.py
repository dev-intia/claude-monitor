"""Remove the VS Code companion extension. The panel then goes back to the slower vscode:// link."""

import shutil
import subprocess
import sys

from install_extension import EXTENSION_ID


def main() -> int:
    code = shutil.which("code")
    if not code:
        print("No encuentro la orden 'code' en el PATH.")
        return 1
    return subprocess.run([code, "--uninstall-extension", EXTENSION_ID]).returncode


if __name__ == "__main__":
    sys.exit(main())
