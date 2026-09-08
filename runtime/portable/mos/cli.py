from __future__ import annotations
import argparse, json
from .runtime import MOSRuntime
from .migration import migrate_r2

def main():
    p = argparse.ArgumentParser(prog="mos")
    p.add_argument("--db", default="mos.sqlite3")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    a = sub.add_parser("ask"); a.add_argument("text")
    g = sub.add_parser("goal"); g.add_argument("goal")
    o = sub.add_parser("outcome"); o.add_argument("episode_id"); o.add_argument("outcome"); o.add_argument("--revision"); o.add_argument("--success", choices=["true","false"])
    e = sub.add_parser("export"); e.add_argument("path"); e.add_argument("--parent")
    i = sub.add_parser("import"); i.add_argument("path"); i.add_argument("--expected-revision", type=int); i.add_argument("--no-backup", action="store_true")
    m = sub.add_parser("mpl"); m.add_argument("script")
    q = sub.add_parser("research"); q.add_argument("query")
    v = sub.add_parser("validate-weakness"); v.add_argument("domain"); v.add_argument("evidence_ref"); v.add_argument("--failed", action="store_true")
    act = sub.add_parser("activate-strategy"); act.add_argument("name"); act.add_argument("evaluation_json")
    b = sub.add_parser("backup"); b.add_argument("path")
    mig = sub.add_parser("migrate-r2"); mig.add_argument("memory_json"); mig.add_argument("stats_json")
    args = p.parse_args()

    if args.cmd == "migrate-r2":
        print(json.dumps(migrate_r2(args.memory_json, args.stats_json, args.db), ensure_ascii=False, indent=2))
        return

    r = MOSRuntime(args.db)
    try:
        if args.cmd == "status":
            print(json.dumps({"state": r.state(), "verify": r.verify()}, ensure_ascii=False, indent=2))
        elif args.cmd == "ask":
            print(json.dumps(r.process(args.text).__dict__, ensure_ascii=False, indent=2))
        elif args.cmd == "goal":
            r.set_goal(args.goal); print(json.dumps({"goal": args.goal}, ensure_ascii=False))
        elif args.cmd == "outcome":
            success = None if args.success is None else args.success == "true"
            print(json.dumps(r.record_outcome(args.episode_id, args.outcome, args.revision, success), ensure_ascii=False, indent=2))
        elif args.cmd == "export":
            print(json.dumps(r.relay.export(args.path, args.parent), ensure_ascii=False, indent=2))
        elif args.cmd == "import":
            print(json.dumps(r.relay.import_snapshot(args.path, args.expected_revision, create_backup=not args.no_backup), ensure_ascii=False, indent=2))
        elif args.cmd == "mpl":
            print(json.dumps(r.mpl.execute(args.script), ensure_ascii=False, indent=2))
        elif args.cmd == "research":
            print(json.dumps(r.research.run(args.query), ensure_ascii=False, indent=2))
        elif args.cmd == "validate-weakness":
            print(json.dumps(r.validate_weakness(args.domain, passed=not args.failed, evidence_ref=args.evidence_ref), ensure_ascii=False, indent=2))
        elif args.cmd == "activate-strategy":
            print(json.dumps(r.activate_strategy_candidate(args.name,json.loads(args.evaluation_json)),ensure_ascii=False,indent=2))
        elif args.cmd == "backup":
            r.store.backup(args.path); print(json.dumps({"backup": args.path}, ensure_ascii=False))
    finally:
        r.close()

if __name__ == "__main__":
    main()
