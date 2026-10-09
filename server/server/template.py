"""Task Template 引擎（Oracle-native YAML → exec 步骤串行执行）。

格式（Plan B8，2026-09 定稿）：
  name / description / steps[]
  每步：name / run(argv 数组) / timeout_sec / env({KEY:{secret:REF}})
  retry 预留不实现；needs/if/jobs/… 不引入。

职责：
  parse_template   —— 解析 + 校验，输出 canonical steps（env 保留 secret ref）
  step_to_task     —— 解 secret + 组装 exec 任务
  apply_template   —— 串行执行 + 落 template_runs 记录（供未来保活循环查询）
"""
from datetime import datetime, timedelta, timezone

import shlex
import time

import yaml

from . import db


class TemplateError(Exception):
    """模板解析/校验/执行错误。"""


DEFAULT_TIMEOUT_SEC = 300
FINAL_STATUSES = ("success", "failed", "timeout", "rejected")
ONLINE_AFTER_SEC = 180


def parse_template(text: str) -> dict:
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise TemplateError(f"YAML 解析失败: {e}")

    if not isinstance(doc, dict):
        raise TemplateError("Template 顶层必须是 mapping")

    name = doc.get("name")
    if not isinstance(name, str) or not name.strip():
        raise TemplateError("缺少 name")
    description = doc.get("description") or ""

    steps = doc.get("steps")
    if not isinstance(steps, list) or not steps:
        raise TemplateError("steps 必须是非空 list")

    out_steps = []
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            raise TemplateError(f"steps[{i}] 必须是 mapping")
        sname = s.get("name")
        if not isinstance(sname, str) or not sname.strip():
            raise TemplateError(f"steps[{i}] 缺少 name")
        run = s.get("run")
        if not isinstance(run, list) or not run or not all(isinstance(x, str) for x in run):
            raise TemplateError(f"steps[{i}]({sname}) run 必须是非空字符串 argv 数组")

        timeout_sec = s.get("timeout_sec", DEFAULT_TIMEOUT_SEC)
        if not isinstance(timeout_sec, int) or timeout_sec <= 0:
            raise TemplateError(f"steps[{i}]({sname}) timeout_sec 必须是正整数")

        env = s.get("env") or {}
        if not isinstance(env, dict):
            raise TemplateError(f"steps[{i}]({sname}) env 必须是 mapping")
        norm_env = {}
        for k, v in env.items():
            if not (isinstance(v, dict) and isinstance(v.get("secret"), str)):
                raise TemplateError(
                    f"steps[{i}]({sname}) env.{k} 必须是 {{secret: KEY}} 引用"
                )
            norm_env[str(k)] = v["secret"]

        out_steps.append({
            "name": sname.strip(),
            "run": [str(x) for x in run],
            "timeout_sec": timeout_sec,
            "env": norm_env,  # ENV_KEY -> secret ref
        })

    return {"name": name.strip(), "description": description, "steps": out_steps}


def resolve_env(env_refs: dict, secrets: dict) -> dict:
    """env_refs({ENV_KEY: secret_ref}) → {ENV_KEY: 实际值}；缺 ref 报错。"""
    out = {}
    missing = [ref for ref in env_refs.values() if ref not in secrets]
    if missing:
        raise TemplateError(f"secret 缺失: {', '.join(sorted(set(missing)))}")
    for k, ref in env_refs.items():
        out[k] = secrets[ref]
    return out


def step_to_task(agent_id: str, step: dict, secrets: dict) -> dict:
    env = resolve_env(step["env"], secrets)
    run = list(step["run"])
    # 兼容：agent 端 exec handler 未消费 params.env；对 bash -c 脚本
    # 用 export 前缀把 secret 注入 shell（模板仍只存 secret ref，不落明文）。
    if env and len(run) == 3 and run[0] == "bash" and run[1] == "-c":
        prefix = "".join(f"export {k}={shlex.quote(str(v))};\n" for k, v in env.items())
        run[2] = prefix + run[2]
    params = {"command": run, "timeout_sec": step["timeout_sec"]}
    if env:
        params["env"] = env
    return db.create_task(agent_id, "exec", params, timeout_sec=step["timeout_sec"])


def apply_template(agent_id: str, template_name: str, *,
                   run_id=None,
                   poll_sec: float = 2.0,
                   overall_timeout_sec: float = 1800.0,
                   on_step=None) -> dict:
    """串行执行：step N success → step N+1；任一失败即 template failed。

    - v1 同步轮询，适合 agentctl / supervisor 后台线程调用。
    - 落 template_runs 记录（running → success|failed），保活循环据此判断健康。
    - on_step(index, total, step_name, task_id, status) 可选进度回调。
    """
    tpl = db.get_template(template_name)
    if not tpl:
        raise TemplateError(f"template 不存在: {template_name}")

    agent = db.get_agent(agent_id)
    if not agent:
        raise TemplateError(f"agent 不存在: {agent_id}")
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ONLINE_AFTER_SEC)).isoformat()
    if not agent.get("last_seen") or agent["last_seen"] < cutoff:
        raise TemplateError(
            f"agent 不在线: {agent_id}（last_seen={agent.get('last_seen')}）"
        )

    # 全局：同一模板最多一个 running
    running = db.get_running_template_run(template_name)
    if running:
        raise TemplateError(
            f"template '{template_name}' already has running template_run "
            f"(id={running['id']}, agent_id={running['agent_id']}, created_at={running['created_at']})"
        )

    secrets = db.get_template_secrets(template_name)
    steps = tpl["steps"]

    # 一次性校验 secret 引用，避免步骤跑到一半才缺 key
    refs = set()
    for s_step in steps:
        refs.update(s_step["env"].values())
    missing = [r for r in sorted(refs) if r not in secrets]
    if missing:
        raise TemplateError(f"secret 缺失: {', '.join(missing)}")

    try:
        trun_id = db.create_template_run(run_id, template_name, agent_id)
    except sqlite3.IntegrityError:
        running = db.get_running_template_run(template_name)
        if running:
            raise TemplateError(
                f"template '{template_name}' already has running template_run "
                f"(id={running['id']}, agent_id={running['agent_id']}, created_at={running['created_at']})"
            )
        raise TemplateError(f"template '{template_name}' create template_run failed (concurrent insert)")
    summary = []
    started = time.time()
    failed_step = ""

    for idx, step in enumerate(steps):
        task = step_to_task(agent_id, step, secrets)
        if on_step:
            on_step(idx, len(steps), step["name"], task["task_id"], "pending")

        final = None
        while True:
            t = db.get_task(task["task_id"])
            if t and t["status"] in FINAL_STATUSES:
                final = t
                break
            if time.time() - started > overall_timeout_sec:
                break
            time.sleep(poll_sec)

        if final is None:  # 整体超时
            failed_step = step["name"]
            summary.append({"step": step["name"], "status": "timeout"})
            break

        summary.append({"step": step["name"], "status": final["status"]})
        if on_step:
            on_step(idx, len(steps), step["name"], task["task_id"], final["status"])
        if final["status"] != "success":
            failed_step = step["name"]
            break

    final_status = "failed" if failed_step else "success"
    db.finish_template_run(trun_id, final_status, failed_step=failed_step, steps=summary)
    return {
        "template": template_name,
        "agent_id": agent_id,
        "status": final_status,
        "template_run_id": trun_id,
        "failed_step": failed_step or None,
        "steps": summary,
    }