"""Create the database on first start, or bring an existing one up to the current schema version.

    python scripts/init_db.py            create if missing, then apply pending migrations
    python scripts/init_db.py --status   report current and latest schema versions; change nothing

The app runs the same steps itself on startup (services.db.init_db). Use this script to
prepare or upgrade a production database before starting the app, and to check its version.
Every change to services/models.py needs a new entry in services/migrations.py; see docs/migrations.md.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import dotenv  # noqa: E402

dotenv.load_dotenv(ROOT / ".env")

from sqlalchemy import inspect  # noqa: E402

from services import db, migrations  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--status", action="store_true", help="Report versions without changing anything.")
    args = parser.parse_args()

    url = db.database_url()
    tables = inspect(db.engine()).get_table_names()
    print(f"database: {url}")
    if args.status:
        if not tables:
            print("state: no tables (fresh); latest schema version is", migrations.LATEST)
            return 0
        report = migrations.status(db.engine())
        print(f"state: version {report['current']} of {report['latest']}")
        for line in report["pending"]:
            print("pending:", line)
        return 0

    before = migrations.current_version(db.engine()) if tables else None
    db.init_db()
    after = migrations.current_version(db.engine())
    if before is None:
        print(f"created fresh database and stamped schema version {after}")
    elif after > before:
        print(f"migrated schema version {before} -> {after}")
    else:
        print(f"schema version {after}; nothing to do")
    return 0


if __name__ == "__main__":
    sys.exit(main())
