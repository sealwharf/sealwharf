"""Set up a USB token without outside help.

`token-driver` unpacks the vendor's Linux driver package into the data
folder, and `token-check` says, without using the PIN, which library reads
the connected token and whose certificates it holds.
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from zipfile import ZipFile

from .paths import config_path, data_dir

OPENSC = "/usr/lib/x86_64-linux-gnu/opensc-pkcs11.so"


def packages_dir() -> Path:
    return config_path().parent / "pkcs11"


def drivers_dir() -> Path:
    return data_dir() / "pkcs11"


def library_name(filename: str) -> str | None:
    """Reduce `libexample.so.3.9.33.1` to the name programs load, `libexample.so.3`."""
    match = re.fullmatch(r"(lib[\w.+-]+?\.so)((?:\.\d+)*)", filename)
    if match is None:
        return None
    version = match.group(2).split(".")
    return match.group(1) + (f".{version[1]}" if len(version) > 1 else "")


def install_driver(*, output=print, unpack=None) -> list[str]:
    """Copy the libraries of every driver package in config/pkcs11 into the data folder."""
    packages = sorted(path for path in packages_dir().glob("*") if path.suffix.lower() in {".zip", ".deb"})
    if not packages:
        output("No driver package found. Put the .deb file from your token's vendor (or the .zip that contains it) "
               "in config/pkcs11 and run this command again.")
        return []
    unpack = unpack or (lambda package, target: subprocess.run(["dpkg-deb", "-x", str(package), str(target)], check=True))
    installed = []
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary)
        for package in packages:
            debs = [package]
            if package.suffix.lower() == ".zip":
                with ZipFile(package) as archive:
                    members = [name for name in archive.namelist() if name.lower().endswith(".deb")]
                    debs = [Path(archive.extract(name, work / "zip")) for name in members]
            for deb in debs:
                unpack(deb, work / "root")
        drivers_dir().mkdir(parents=True, exist_ok=True)
        for library in sorted((work / "root").rglob("lib*.so*")):
            name = library_name(library.name)
            if name is not None and library.is_file() and not library.is_symlink():
                shutil.copyfile(library, drivers_dir() / name)
                installed.append(name)
    if not installed:
        output("The package holds no libraries. Download the Linux 64-bit package (Ubuntu 22.04 or Debian 12) from the vendor.")
        return []
    output("Installed: " + ", ".join(sorted(set(installed))))
    output("Now run: docker compose run --rm sealwharf token-check")
    return sorted(set(installed))


def _probe(module: str) -> dict:
    """Describe what one library sees. Runs in its own process, because only one library can be loaded at a time."""
    import pkcs11
    from asn1crypto import x509
    from pkcs11 import Attribute, ObjectClass
    try:
        slots = list(pkcs11.lib(module).get_slots(token_present=True))
    except Exception:
        return {"state": "unusable"}
    if not slots:
        return {"state": "no_token"}
    holders = []
    for slot in slots:
        try:
            with slot.get_token().open() as session:
                for entry in session.get_objects({Attribute.CLASS: ObjectClass.CERTIFICATE}):
                    certificate = x509.Certificate.load(bytes(entry[Attribute.VALUE]))
                    name = certificate.subject.native.get("common_name")
                    if isinstance(name, str) and not certificate.ca:
                        # Brazilian certificates append the holder's tax number; it is not part of the name.
                        holders.append({"name": re.sub(r"[^\w ]|\d", "", name.split(":")[0]).strip().title(),
                                        "days": (certificate.not_valid_after - datetime.now(UTC)).days})
        except Exception:
            continue
    return {"state": "read", "holders": holders} if holders else {"state": "not_recognized"}


def probe_in_subprocess(module: str) -> dict:
    result = subprocess.run([sys.executable, "-m", "sealwharf.token_tools", module], capture_output=True, text=True, timeout=60, check=False)
    try:
        return json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {"state": "unusable"}


def check_token(*, output=print, probe=probe_in_subprocess) -> str | None:
    """Report which library reads the connected token; return its path when one does."""
    installed = sorted(str(path) for path in drivers_dir().glob("lib*.so*")) if drivers_dir().is_dir() else []
    # A vendor package brings several libraries; only some of them speak PKCS#11, so each is simply tried.
    results = {module: probe(module) for module in [OPENSC, *installed]}
    working = next((module for module, result in results.items() if result["state"] == "read"), None)
    if working is not None:
        output("The token is readable. Use these lines in config/sealwharf.yaml:")
        output("    token:")
        output(f"      module: {working}")
        for holder in results[working]["holders"]:
            output(f"Certificate holder: {holder['name']} (valid for {holder['days']} more day(s)). Use this as the certificate name.")
        return working
    if all(result["state"] in ("no_token", "unusable") for result in results.values()):
        output("No token was found. Check that it is plugged in, that the reader service is running "
               "(docker compose --profile token up -d reader) and, on Windows, that it is attached with usbipd.")
    else:
        output("A token is connected, but no installed library can read it. Download the Linux driver from the token's "
               "vendor, put the package in config/pkcs11 and run: docker compose run --rm sealwharf token-driver")
    return None


def driver_command() -> None:
    sys.exit(0 if install_driver() else 1)


def check_command() -> None:
    sys.exit(0 if check_token() else 1)


if __name__ == "__main__":
    print(json.dumps(_probe(sys.argv[1])))
