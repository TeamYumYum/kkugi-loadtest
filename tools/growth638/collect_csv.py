"""List or copy an existing Locust history CSV from the observation host."""
import argparse
from pathlib import Path
import re
import subprocess

SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "kkugi-obs",
       "docker", "exec", "kkugi-loadgen-alerts-1"]
PATTERN = re.compile(r"kkugi-\d{8}-\d{6}_stats_history\.csv")


def list_files():
    result = subprocess.check_output([*SSH, "ls", "-1", "/app/stats"], text=True, timeout=30)
    return sorted(n for n in result.splitlines() if PATTERN.fullmatch(n))


def copy(name, directory):
    if not PATTERN.fullmatch(name):
        raise ValueError(f"Invalid Locust history filename: {name}")
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    temp = target.with_suffix(".part")
    try:
        with temp.open("wb") as out:
            subprocess.run([*SSH, "cat", f"/app/stats/{name}"], stdout=out, check=True, timeout=240)
        temp.replace(target)
    finally:
        temp.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("names", nargs="*")
    parser.add_argument("--dir", type=Path, default=Path("state/growth638"))
    args = parser.parse_args()
    if not args.names:
        print("\n".join(list_files()))
    else:
        for name in args.names:
            copy(name, args.dir)
