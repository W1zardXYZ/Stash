"""Command line: python -m stash [serve|scan PATH|worker|reindex]"""

import argparse
import sys


def main() -> int:
    p = argparse.ArgumentParser(prog="stash")
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("serve", help="run the web server + workers")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8420)
    sc = sub.add_parser("scan", help="add a folder/project/.blend as a source and process it now")
    sc.add_argument("path")
    sc.add_argument("--name")
    sc.add_argument("--mode", default="linked", choices=["linked", "import"])
    sub.add_parser("reindex", help="rebuild the search index")
    sub.add_parser("run-jobs", help="process all queued jobs and exit")
    args = p.parse_args()

    if args.cmd in (None, "serve"):
        import uvicorn

        uvicorn.run("stash.main:app", host=getattr(args, "host", "0.0.0.0"), port=getattr(args, "port", 8420), proxy_headers=True)
        return 0

    from . import processing  # noqa: F401  (job handlers)
    from .db import init_engine, session_scope
    from .jobs import enqueue, run_pending_sync

    init_engine()
    if args.cmd == "scan":
        from pathlib import Path

        from .api.library import _detect_kind
        from .models import Source

        path = Path(args.path).resolve()
        with session_scope() as db:
            src = db.query(Source).filter(Source.path == str(path)).one_or_none()
            if src is None:
                src = Source(name=args.name or path.stem, path=str(path), kind=_detect_kind(path), mode=args.mode)
                db.add(src)
                db.flush()
            sid = src.id
        enqueue("scan_source", {}, source_id=sid, priority=50)
        n = run_pending_sync()
        print(f"processed {n} jobs")
    elif args.cmd == "reindex":
        from .search import reindex_all

        with session_scope() as db:
            print(f"indexed {reindex_all(db)} assets")
    elif args.cmd == "run-jobs":
        print(f"processed {run_pending_sync()} jobs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
