"""Install the VS Code companion extension, packed from vscode-extension/. No dependencies."""

import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parent / "vscode-extension"
EXTENSION_ID = "local.claude-monitor-companion"
CONTENT_TYPES = """<?xml version="1.0" encoding="utf-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="json" ContentType="application/json"/>
<Default Extension="js" ContentType="application/javascript"/>
<Default Extension="vsixmanifest" ContentType="text/xml"/>
</Types>
"""
MANIFEST = """<?xml version="1.0" encoding="utf-8"?>
<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011">
<Metadata>
<Identity Language="en-US" Id="{name}" Version="{version}" Publisher="{publisher}"/>
<DisplayName>{display}</DisplayName>
<Description xml:space="preserve">{description}</Description>
</Metadata>
<Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation>
<Dependencies/>
<Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/></Assets>
</PackageManifest>
"""


def build_vsix(target: Path) -> None:
    info = json.loads((SOURCE / "package.json").read_text(encoding="utf-8"))
    manifest = MANIFEST.format(name=info["name"], version=info["version"], publisher=info["publisher"],
                               display=info["displayName"], description=info["description"])
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("extension.vsixmanifest", manifest)
        for name in ("package.json", "extension.js"):
            archive.write(SOURCE / name, f"extension/{name}")


def main() -> int:
    code = shutil.which("code")
    if not code:
        print("No encuentro la orden 'code' en el PATH.")
        return 1
    with tempfile.TemporaryDirectory(prefix="claude-monitor-") as folder:
        vsix = Path(folder) / "claude-monitor-companion.vsix"
        build_vsix(vsix)
        return subprocess.run([code, "--install-extension", str(vsix), "--force"]).returncode


if __name__ == "__main__":
    sys.exit(main())
