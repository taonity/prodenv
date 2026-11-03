import argparse
import json
import time
from pathlib import Path

from worker import verify, run


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("exports", nargs="+", type=Path)
    parser.add_argument("--database", required=True)
    parser.add_argument("--config", default="/retained-worker/config.json")
    options = parser.parse_args()
    config = json.loads(Path(options.config).read_text())
    exports = []
    for directory in options.exports:
        directory = directory.resolve()
        verify(directory)
        metadata = json.loads((directory / "metadata.json").read_text())
        if metadata["format"] != "postgres-csv-v1":
            raise ValueError("Unsupported retained export format")
        if time.time() >= metadata["expiresAt"] - config["expirySafetySeconds"]:
            raise ValueError(f"Refusing expired records from {directory}")
        exports.append((metadata["recordDate"], directory, metadata))
    for _, directory, metadata in sorted(exports):
        run(["psql", "-X", "-v", "ON_ERROR_STOP=1", "-d", options.database, "-f", "restore.sql"], config, cwd=directory)


if __name__ == "__main__":
    main()
