"""agentctl — Oracle-side management CLI.

Usage (run from repo root, so `server` package is importable):

    .venv/bin/python -m server.ctl list
    .venv/bin/python -m server.ctl task push <agent_id> <type> '<json params>'
    .venv/bin/python -m server.ctl task status <task_id>
    .venv/bin/python -m server.ctl task list [--agent <agent_id>] [--status <status>]
"""
import argparse
import json
import sys

from . import db


def _print_table(headers, rows):
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    print(line)
    print("-" * len(line))
    for row in rows:
        print("  ".join(str(c).ljust(widths[i]) for i, c in enumerate(row)))


def cmd_list(_args):
    db.init_db()
    agents = db.list_agents()
    print(f"AGENTS ({len(agents)})")
    rows = [
        (
            a["agent_id"],
            "online" if a["online"] else "offline",
            a["status"],
            ",".join(a["capabilities"]),
            (a["last_seen"] or "")[:19],
        )
        for a in agents
    ]
    _print_table(["agent_id", "state", "status", "capabilities", "last_seen"], rows)
    print()
    tasks = db.list_tasks()
    print(f"TASKS ({len(tasks)})")
    rows = [
        (
            t["task_id"][:12],
            t["agent_id"],
            t["type"],
            t["status"],
            (t["finished_at"] or "")[:19],
        )
        for t in tasks
    ]
    _print_table(["task_id", "agent_id", "type", "status", "finished_at"], rows)


def cmd_task_push(args):
    db.init_db()
    params = {}
    if args.params:
        try:
            params = json.loads(args.params)
        except json.JSONDecodeError:
            print(f"error: params must be valid JSON: {args.params}", file=sys.stderr)
            sys.exit(1)
    task = db.create_task(args.agent_id, args.type, params, timeout_sec=args.timeout)
    print(f"task created: {task['task_id']} -> {task['agent_id']} ({task['type']})")


def cmd_task_status(args):
    db.init_db()
    task = db.get_task(args.task_id)
    if not task:
        print(f"task not found: {args.task_id}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(task, indent=2, ensure_ascii=False))


def cmd_run_list(args):
    db.init_db()
    runs = db.list_runs(limit=args.limit)
    if not runs:
        print("no run records yet")
        return
    rows = [
        (
            r["run_id"],
            r["agent_id"][:22],
            r["public_ip"],
            r["private_ip"],
            r["vm_size"],
            r["location"],
            (r["registered_at"] or "")[:19],
        )
        for r in runs
    ]
    _print_table(["run_id", "agent_id", "public_ip", "private_ip", "vm_size", "location", "registered_at"], rows)


def cmd_run_show(args):
    db.init_db()
    run = db.get_run(args.run_id)
    if not run:
        print(f"run not found: {args.run_id}", file=sys.stderr)
        sys.exit(1)
    keep = dict(run)
    meta = keep.pop("meta", None)
    print(json.dumps(keep, indent=2, ensure_ascii=False))
    if meta:
        print("meta_json (raw):")
        print(json.dumps(meta, indent=2, ensure_ascii=False))


def cmd_run_by_ip(args):
    db.init_db()
    runs = db.find_runs_by_ip(args.ip)
    if not runs:
        print(f"no runs for ip: {args.ip}")
        return
    rows = [(r["run_id"], r["agent_id"], r["public_ip"], r["private_ip"], r["vm_size"], r["location"], (r["registered_at"] or "")[:19]) for r in runs]
    _print_table(["run_id", "agent_id", "public_ip", "private_ip", "vm_size", "location", "registered_at"], rows)


def cmd_task_list(args):
    db.init_db()
    tasks = db.list_tasks(agent_id=args.agent, status=args.status)
    rows = [
        (
            t["task_id"][:12],
            t["agent_id"],
            t["type"],
            t["status"],
            json.dumps(t.get("result", {}), ensure_ascii=False)[:60],
        )
        for t in tasks
    ]
    _print_table(["task_id", "agent_id", "type", "status", "result"], rows)


def main():
    parser = argparse.ArgumentParser(prog="agentctl", description="Oracle Agent management")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="list agents and tasks")

    p_run = sub.add_parser("run", help="run records (GitHub Actions runs)")
    rsub = p_run.add_subparsers(dest="run_cmd", required=True)
    p_run_list = rsub.add_parser("list", help="list run records")
    p_run_list.add_argument("--limit", type=int, default=100)
    p_run_list.set_defaults(func=cmd_run_list)
    p_run_show = rsub.add_parser("show", help="show full run record by run_id")
    p_run_show.add_argument("run_id", type=int)
    p_run_show.set_defaults(func=cmd_run_show)
    p_run_ip = rsub.add_parser("by-ip", help="find runs by public/private IP")
    p_run_ip.add_argument("ip")
    p_run_ip.set_defaults(func=cmd_run_by_ip)

    p_task = sub.add_parser("task", help="task operations")
    tsub = p_task.add_subparsers(dest="task_cmd", required=True)

    p_push = tsub.add_parser("push", help="create a task for an agent")
    p_push.add_argument("agent_id")
    p_push.add_argument("type")
    p_push.add_argument("params", nargs="?", default="{}")
    p_push.add_argument("--timeout", type=int, default=60)
    p_push.set_defaults(func=cmd_task_push)

    p_status = tsub.add_parser("status", help="show task status")
    p_status.add_argument("task_id")
    p_status.set_defaults(func=cmd_task_status)

    p_list = tsub.add_parser("list", help="list tasks")
    p_list.add_argument("--agent")
    p_list.add_argument("--status")
    p_list.set_defaults(func=cmd_task_list)

    p_tpl = sub.add_parser("template", help="task template management")
    plsub = p_tpl.add_subparsers(dest="tpl_cmd", required=True)

    p_ti = plsub.add_parser("import", help="import/update a template from YAML")
    p_ti.add_argument("name")
    p_ti.add_argument("file")
    p_ti.set_defaults(func=cmd_template_import)

    p_tl = plsub.add_parser("list", help="list templates")
    p_tl.set_defaults(func=cmd_template_list)

    p_ts = plsub.add_parser("show", help="show a template (steps, no secret values)")
    p_ts.add_argument("name")
    p_ts.set_defaults(func=cmd_template_show)

    p_se = plsub.add_parser("set-secret", help="set a template secret value")
    p_se.add_argument("name")
    p_se.add_argument("key")
    p_se.add_argument("value")
    p_se.set_defaults(func=cmd_template_set_secret)

    p_ap = plsub.add_parser("apply", help="apply template to an online agent (serial)")
    p_ap.add_argument("name")
    p_ap.add_argument("--agent", required=True)
    p_ap.set_defaults(func=cmd_template_apply)

    p_tr = plsub.add_parser("runs", help="list template run records (audit)")
    p_tr.add_argument("--template")
    p_tr.add_argument("--limit", type=int, default=50)
    p_tr.set_defaults(func=cmd_template_runs)

    args = parser.parse_args()
    if args.cmd == "list":
        cmd_list(args)
    else:
        args.func(args)

def cmd_template_import(args):
    db.init_db()
    from . import template as tplmod
    with open(args.file, encoding="utf-8") as f:
        text = f.read()
    parsed = tplmod.parse_template(text)
    refs = set()
    for s in parsed["steps"]:
        refs.update(s["env"].values())
    db.upsert_template(args.name, parsed["description"], parsed["steps"])
    print(f"template '{args.name}' saved: {len(parsed['steps'])} steps")
    print(f"secret refs: {sorted(refs) if refs else '(none)'}")


def cmd_template_list(args):
    db.init_db()
    rows = [
        (t["name"], t.get("description", "") or "", "enabled" if t["enabled"] else "disabled",
         (t.get("updated_at") or "")[:19], ",".join(db.list_template_secret_keys(t["name"])))
        for t in db.list_templates()
    ]
    _print_table(["name", "description", "enabled", "updated_at", "secret_keys"], rows)


def cmd_template_show(args):
    db.init_db()
    t = db.get_template(args.name)
    if not t:
        print(f"template not found: {args.name}", file=sys.stderr)
        sys.exit(1)
    out = {
        "name": t["name"],
        "description": t["description"],
        "enabled": t["enabled"],
        "secrets": db.list_template_secret_keys(args.name),
        "steps": t["steps"],
    }
    print(json.dumps(out, indent=2, ensure_ascii=False))


def cmd_template_set_secret(args):
    db.init_db()
    db.set_template_secret(args.name, args.key, args.value)
    print(f"secret '{args.key}' set for template '{args.name}'")


def cmd_template_apply(args):
    db.init_db()
    from . import template as tplmod

    def on_step(i, total, name, task_id, status):
        print(f"[{status:8}] step {i+1}/{total} {name} (task {task_id[:12]})", flush=True)

    res = tplmod.apply_template(args.agent, args.name, on_step=on_step)
    print(json.dumps({k: v for k, v in res.items() if k != "steps"}, indent=2, ensure_ascii=False))
    for s in res["steps"]:
        print(f"  - {s['step']}: {s['status']}")


def cmd_template_runs(args):
    db.init_db()
    rows = [
        (r["id"], r["run_id"] if r["run_id"] else "", r["template"], (r["agent_id"] or "")[:20],
         r["status"], r["failed_step"] or "", (r["finished_at"] or "")[:19])
        for r in db.list_template_runs(limit=args.limit, template=args.template)
    ]
    _print_table(["id", "run_id", "template", "agent_id", "status", "failed_step", "finished_at"], rows)


if __name__ == "__main__":
    main()
