"""Watch the Jenkins container for new builds and ingest them into Postgres.

Near-real-time: every POLL_INTERVAL seconds it checks whether any build number
is newer than the latest one already in the DB, and ingests anything new.

Usage:
    python watch_builds.py                 # run forever (Ctrl+C to stop)
    python watch_builds.py --once          # do one poll cycle and exit
    python watch_builds.py --interval 60   # poll every 60s

For auto-start you can also register it as a logon scheduled task:
    schtasks /Create /TN "ci-build-ingest" /TR "python C:\\...\\watch_builds.py" \
            /SC ONLOGON /RL LIMITED
"""

import argparse
import time

from db import ensure_schema, get_db_conn
from feed_jenkins_builds import ingest_build, list_builds, max_build_in_db

DEFAULT_INTERVAL = 30


def poll_once(reconcile=5):
    ensure_schema()
    conn = get_db_conn()
    cur = conn.cursor()
    db_max = max_build_in_db(cur)
    builds = list_builds()
    if db_max == 0:
        target = [n for n in builds]
    else:
        lo = max(db_max - reconcile + 1, 1)
        target = [n for n in builds if n >= lo]
    ingested = 0
    for n in target:
        if ingest_build(cur, n):
            ingested += 1
    conn.commit()
    conn.close()
    return db_max, sorted(builds), ingested


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    ap.add_argument(
        "--reconcile",
        type=int,
        default=5,
        help="re-ingest the last N builds every poll",
    )
    args = ap.parse_args()

    print(f"Watching Jenkins for new builds (poll every {args.interval}s)...")
    while True:
        try:
            db_max, builds, ingested = poll_once(args.reconcile)
            if db_max == 0:
                print(f"DB empty, caught up after ingesting {ingested} build(s).")
            elif ingested:
                print(
                    f"caught up: {ingested} build(s) verified (now tracking {len(builds)})."
                )
            if args.once:
                print(f"latest DB build: {db_max} | jenkins builds: {len(builds)}")
                return
        except KeyboardInterrupt:
            print("\nstopped.")
            raise SystemExit(0)
        except Exception as e:  # keep watching through transient wsl/docker/db errors
            print(f"[{time.strftime('%H:%M:%S')}] poll failed: {type(e).__name__}: {e}")
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
