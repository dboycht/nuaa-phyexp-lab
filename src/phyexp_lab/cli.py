"""命令行入口：`python run.py <子命令>`。

子命令
------
- `version`  查看版本
- `status`   查看数据目录、会话状态与最近的采集文件
- `login`    打开浏览器登录并保存会话
- `recon`    侦察：登录 + 记录请求（HAR + 脱敏 JSONL）
- `snapshot` 只读采集课程/实验项目/场次余量
- `watch`    余量监控（只读）
- `elect`    **选课**：列出可约场次 / 提交选课（写操作）/ 查看已选
- `cancel`   **退课**：按选课记录 id 退课（写操作）
- `grab`     抢课引擎：对时 + 精确定时 + 预发射（默认演练，`--real` 才真发）
- `logout`   删除本地会话文件
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from pathlib import Path

from . import __version__, config, probe, scrub, session
from .session import PlaywrightMissingError, SessionError

REPO_URL = "https://github.com/dboycht/nuaa-phyexp-lab"


def _human_age(seconds: float | None) -> str:
    if seconds is None:
        return "无会话"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} 秒前"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时 {(seconds % 3600) // 60} 分前"
    return f"{seconds // 86400} 天前"


def _session_cookie_count() -> str:
    """会话里的 Cookie 数量（只数个数，不展示任何值）。"""
    try:
        return str(len(session.load_state().get("cookies") or []))
    except SessionError:
        return "-"


def _token_summary() -> str:
    """token 的存在性与有效期（只显示长度与 exp，绝不打印 token 本身）。"""
    token = session.load_token()
    if not token:
        return "无 token（需登录）"
    return f"已保存（{len(token)} 字符）；{session.describe_token(token)}"


# ── 子命令 ──


def _cmd_version(_args: argparse.Namespace) -> int:
    print(f"nuaa-phyexp-lab {__version__}")
    print(f"仓库：{REPO_URL}")
    print(f"目标系统：{config.BOOKING_BASE}（物理实验预约选课）")
    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    home = config.home_dir()
    state = session.state_path()
    print(f"版本            : {__version__}")
    print(f"目标入口        : {config.BOOKING_ENTRY}")
    print(f"API 基址        : {config.API_BASE}")
    print(f"数据目录        : {home}  （存在：{home.is_dir()}）")
    print(f"会话文件        : {state}")
    if state.is_file():
        size = state.stat().st_size
        print(f"会话状态        : 已有会话（{_human_age(session.state_age_seconds())}保存，"
              f"{size} 字节，Cookie {_session_cookie_count()} 条）")
    else:
        print("会话状态        : 无（请先运行 `python run.py login`）")
    print(f"登录 token      : {_token_summary()}")

    recon_dir = config.recon_dir()
    captures = sorted(recon_dir.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True) if recon_dir.is_dir() else []
    if captures:
        print(f"采集文件（{recon_dir}）：")
        for path in captures[:5]:
            mtime = dt.datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            print(f"  - {path.name}  {path.stat().st_size} 字节  {mtime}")
    else:
        print(f"采集文件        : 无（{recon_dir}）")
    return 0


def _run_login(record_har: bool, max_wait: int, use_saved_state: bool = True) -> int:
    try:
        state = session.interactive_login(
            record_har=record_har,
            max_wait_seconds=max_wait,
            use_saved_state=use_saved_state,
        )
    except PlaywrightMissingError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    except SessionError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n[中断] 已取消。", file=sys.stderr)
        return 130
    print(f"[完成] 会话文件：{state}")
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    return _run_login(record_har=False, max_wait=args.max_wait,
                      use_saved_state=not args.no_saved_state)


def _cmd_recon(args: argparse.Namespace) -> int:
    print("[提示] 侦察模式会记录所有请求（含响应体）到本地 recon 目录；登录类请求已脱敏。")
    return _run_login(record_har=True, max_wait=args.max_wait,
                      use_saved_state=not args.no_saved_state)


def _cmd_analyze(args: argparse.Namespace) -> int:
    """分析采样样本：退课/被抢事件、空位存活时间、变动排行。"""
    from . import analyze

    if args.samples:
        sample_paths = [Path(p) for p in args.samples]
    else:
        sample_dir = config.home_dir() / "samples"
        sample_paths = sorted(sample_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
    sample_paths = [p for p in sample_paths if p.is_file()]
    if not sample_paths:
        print(f"[错误] 没找到样本文件。请先跑 `python run.py watch`；"
              f"样本目录：{config.home_dir() / 'samples'}", file=sys.stderr)
        return 2

    snap_dir = config.home_dir() / "snapshots"
    meta = analyze.load_snapshot_meta(sorted(snap_dir.glob("*.json"))) if snap_dir.is_dir() else {}

    report = analyze.build_report(sample_paths, meta)
    print("=== 采集概览 ===")
    for line in report.summary_lines():
        print(line)
    print(f"场次静态信息命中: {len(meta)} 个场次（来自 snapshot；缺则项目名显示为未采到）")

    print()
    print("=== 变化事件（最多列 20 条）===")
    if report.events:
        for event in report.events[:20]:
            print(f"  {event.describe()}")
        if len(report.events) > 20:
            print(f"  …另有 {len(report.events) - 20} 条，见报告文件")
    else:
        print("  样本期内没有任何余量变化（两者都如实为 0，不代表接口有问题）")

    markdown = analyze.render_markdown(report)
    report_dir = config.home_dir() / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    out = report_dir / f"analysis-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.md"
    out.write_text(markdown, encoding="utf-8")
    print()
    print(f"完整报告 → {out}")
    return 0


def _cmd_clock(args: argparse.Namespace) -> int:
    """时钟对时：测出「服务端 − 本地」偏移（抢课打点必须按服务端时刻）。"""
    from . import probe

    try:
        offset = probe.measure_clock_offset(samples=args.samples, timeout=args.timeout)
    except SessionError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    print(offset.summary)
    print(f"测量时刻        : {offset.measured_at}")
    now_local = dt.datetime.now().astimezone()
    est_server = now_local + dt.timedelta(seconds=offset.offset_seconds)
    print(f"本地当前        : {now_local.isoformat(timespec='milliseconds')}")
    print(f"推算服务端      : {est_server.isoformat(timespec='milliseconds')}")
    print("[用途] 抢课的目标时刻一律换算成服务端时刻再打点；本地钟差 1 秒就足以错过整场。")
    return 0


def _cmd_grab(args: argparse.Namespace) -> int:
    """抢课引擎（当前只有演练）：对时 → 预热 → 定时 → 预发射 → 限速退避。"""
    from . import api, grabber

    slot_ids = [s.strip() for s in str(args.slot).split(",") if s.strip()]
    if not slot_ids:
        print("[错误] 请用 --slot 指定目标场次 id（可逗号分隔多个）。", file=sys.stderr)
        return 2

    now_local = dt.datetime.now().astimezone()
    if args.in_seconds is not None:
        target_local_guess = now_local + dt.timedelta(seconds=args.in_seconds)
        target_wall = target_local_guess.strftime("%H:%M:%S")
        print(f"[演练] 目标：从现在起 {args.in_seconds} 秒后发射（约本地 {target_wall}）")
    elif args.at:
        try:
            hour, minute, second = (int(x) for x in args.at.split(":"))
            target_wall_dt = now_local.replace(hour=hour, minute=minute, second=second, microsecond=0)
        except Exception:
            print("[错误] --at 格式应为 HH:MM:SS（例如 21:30:00）。", file=sys.stderr)
            return 2
        target_local_guess = target_wall_dt
        print(f"[演练] 目标：服务端时钟走到 {args.at} 时发射")
    else:
        print("[错误] 需要 --at HH:MM:SS 或 --in 秒数。", file=sys.stderr)
        return 2

    try:
        client = api.PhyExpClient(timeout=args.timeout)
    except api.ApiError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    cfg = grabber.GrabConfig(
        pre_fire_offset_ms=args.pre_fire,
        min_submit_interval_ms=args.interval,
        max_attempts_per_target=args.max_attempts,
    )
    submit_func = None
    if args.real:
        course_id = args.course
        if course_id is None:
            # 未显式给课程 id 时，取"我的第一门课"，并把结论如实打印出来（不静默猜）
            try:
                semesters = client.open_semesters()
                courses = client.my_courses(semesters[0].get("id")) if semesters else []
            except api.ApiError as exc:
                print(f"[错误] 读取我的课程失败：{exc}", file=sys.stderr)
                client.close()
                return 2
            if not courses:
                print("[错误] 拿不到课程 id（没有开放学期或没有课程）；请显式传 --course。", file=sys.stderr)
                client.close()
                return 2
            course_id = courses[0].get("id")
            print(f"[准备] 未指定 --course，按「我的第一门课」提交：course_id={course_id}")
        submit_func = grabber.make_submit_func(client, course_id)

    engine = grabber.Grabber(client, cfg, submit_func=submit_func)
    try:
        engine.prepare(measure_clock=not args.no_clock)
        if args.no_clock:
            print("[准备] 已跳过对时（--no-clock）——此时按本地钟打点，仅供参考。")
        # 把"想打的墙上时刻"换算成服务端 epoch：
        # 我们认为该 HH:MM:SS 就是服务端时钟读数，故 target_server_epoch = 该墙上时刻的 epoch
        target_server_epoch = target_local_guess.timestamp()
        engine.run_until(slot_ids, target_server_epoch, dry_run=not args.real,
                         plan_only=args.plan_only)
    except NotImplementedError as exc:
        print(f"[拒绝执行] {exc}", file=sys.stderr)
        return 2
    except grabber.GrabError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    finally:
        client.close()

    print()
    print("[演练汇总]（dry-run 的结果不是成功，只说明定时链路走通了）" if not args.real
          else "[发射汇总]（真实提交）")
    for attempt in engine.attempts:
        deviation = "-" if attempt.deviation_ms is None else f"{attempt.deviation_ms:+.1f}ms"
        status = "-" if attempt.http_status is None else str(attempt.http_status)
        print(f"  场次 {attempt.slot_id}  结果 {attempt.outcome.value}  HTTP {status}  "
              f"耗时 {attempt.elapsed_ms if attempt.elapsed_ms is not None else '-'}ms  "
              f"计划偏差 {deviation}  {attempt.message[:80]}")
    if args.real:
        print("[提示] 每次写操作都已落盘到 logs\\write-*.jsonl 与 logs\\grab-*.jsonl；"
              "请用 `python run.py mine` 复核服务端是否真的选上。")
    return 0


def _sampler_pid_path() -> Path:
    return config.home_dir() / "sampler.pid"

def _cmd_watch_bg(args: argparse.Namespace) -> int:
    """把采样器作为**独立进程**启动（脱离当前会话，长跑用）。

    为什么需要它（2026-09-21 实测）：本环境里"随会话挂着的后台任务"会被中途终止
    （三次尝试分别只跑完 23、1、3 轮，且都是裸 exit 1、无 traceback）。
    规律性研究要跑几小时到几天，所以必须让采样器**脱离会话**：
    独立进程 + 断管输出 + PID 落盘（便于随时停止）。
    """
    import subprocess

    config.ensure_home()
    logs = config.logs_dir()
    logs.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_path = logs / f"sampler-{stamp}.out.log"
    err_path = logs / f"sampler-{stamp}.err.log"

    run_py = Path(__file__).resolve().parents[2] / "run.py"
    cmd = [sys.executable, str(run_py), "watch",
           "--interval", str(args.interval), "--rounds", str(args.rounds)]
    if args.projects:
        cmd += ["--projects", args.projects]
    if args.course:
        cmd += ["--course", str(args.course)]

    creationflags = 0
    if hasattr(subprocess, "DETACHED_PROCESS"):
        creationflags |= subprocess.DETACHED_PROCESS
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
    # 尽量脱离父进程所在的 Job Object（否则父进程结束时可能被连带杀掉）
    if hasattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB"):
        creationflags |= subprocess.CREATE_BREAKAWAY_FROM_JOB

    with out_path.open("ab") as out, err_path.open("ab") as err:
        try:
            proc = subprocess.Popen(cmd, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                    creationflags=creationflags, cwd=str(run_py.parent))
        except OSError as exc:
            print(f"[警告] 脱离 Job 启动失败（{exc}），改用普通独立进程重试。", file=sys.stderr)
            creationflags &= ~getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
            proc = subprocess.Popen(cmd, stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                    creationflags=creationflags, cwd=str(run_py.parent))

    _sampler_pid_path().write_text(str(proc.pid), encoding="utf-8")
    print(f"[完成] 采样器已作为独立进程启动：pid={proc.pid}")
    print(f"       间隔 {args.interval}s，轮数 {'不限' if args.rounds <= 0 else args.rounds}")
    print(f"       标准输出 → {out_path}")
    print(f"       错误输出 → {err_path}")
    print(f"       进度日志与样本：{config.logs_dir()} 与 {config.home_dir() / 'samples'}")
    print(f"       停止：python run.py watch-stop")
    print("[提醒] token 实测 2 小时过期，过期后采样会开始失败；请定期重新登录后再启动。")
    return 0


def _cmd_watch_stop(_args: argparse.Namespace) -> int:
    """停止独立采样进程（按 PID 文件精确停止，不做通配杀进程）。"""
    pid_path = _sampler_pid_path()
    if not pid_path.is_file():
        print("[跳过] 没有采样器 PID 文件（可能本来就没启动过）。")
        return 0
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        print(f"[警告] PID 文件无法解析，已删除：{pid_path}", file=sys.stderr)
        pid_path.unlink(missing_ok=True)
        return 2

    import subprocess

    result = subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                            capture_output=True, text=True)
    if result.returncode == 0:
        print(f"[完成] 已停止采样器 pid={pid}")
    else:
        message = (result.stdout or "") + (result.stderr or "")
        if "not found" in message.lower() or "找不到" in message:
            print(f"[信息] 进程 {pid} 已经不在了（可能已自行结束）。")
        else:
            print(f"[警告] 停止失败：{message.strip()[:200]}", file=sys.stderr)
            return 2
    pid_path.unlink(missing_ok=True)
    return 0


def _cmd_gui(args: argparse.Namespace) -> int:
    """启动图形界面：默认是只读工作台；`--grab` 打开抢课面板。"""
    try:
        if args.grab or args.self_check:
            from . import gui_grab
        else:
            from . import gui
    except ImportError as exc:
        print(f"[错误] 未安装 PySide6，无法启动界面：{exc}", file=sys.stderr)
        print("       安装：pip install PySide6", file=sys.stderr)
        return 2
    if args.grab or args.self_check:
        argv: list[str] = []
        if args.self_check:
            argv.append("--self-check")
        return gui_grab.main(argv)
    return gui.main()


def _cmd_snapshot(args: argparse.Namespace) -> int:
    """只读采集：课程 → 实验项目 → 场次（含容量/已选人数/余量），落盘为快照 JSON。"""
    import json

    from . import api

    config.ensure_home()
    snap_dir = config.home_dir() / "snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    try:
        client = api.PhyExpClient(timeout=args.timeout)
    except api.ApiError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    try:
        warm_ms = client.prewarm()
        server_time = client.server_time()
        local_time = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        print(f"预热耗时        : {warm_ms} ms")
        print(f"服务端时间      : {server_time}")
        print(f"本地时间        : {local_time}")

        semesters = client.open_semesters()
        if not semesters:
            print("[警告] 当前没有开放学期，无法采集。")
            return 0
        semester = semesters[0]
        print(f"开放学期        : id={semester.get('id')} {semester.get('name')} "
              f"({semester.get('since')} ~ {semester.get('to')})")

        courses = client.my_courses(semester.get("id"))
        print(f"我的课程        : {len(courses)} 门")

        snapshot: dict = {
            "captured_at": local_time,
            "server_time": server_time,
            "prewarm_ms": warm_ms,
            "semester": {k: semester.get(k) for k in ("id", "code", "name", "since", "to")},
            "courses": [],
        }

        total_projects = total_slots = total_free = 0
        for course in courses:
            course_id = course.get("id")
            entry = {
                "course_id": course_id,
                "name": course.get("name"),
                "code": course.get("code"),
                "projects": [],
            }
            projects = client.course_projects(course_id)
            free_of_course = 0
            for row in projects:
                experiment = client.to_experiment(row)
                rows = client.slots(course_id, project_id=experiment.experiment_id,
                                    with_my_status=not args.all_status)
                slots = [client.to_slot(r) for r in rows]
                free = [s for s in slots if (s.remaining or 0) > 0]
                free_of_course += len(free)
                total_projects += 1
                total_slots += len(slots)
                total_free += len(free)
                entry["projects"].append({
                    "project_id": experiment.experiment_id,
                    "name": experiment.name,
                    "slot_count": len(slots),
                    "free_slot_count": len(free),
                    "slots": [
                        {
                            "slot_id": s.slot_id,
                            "time": s.time_text,
                            "location": s.location,
                            "taken": s.taken,
                            "capacity": s.capacity,
                            "remaining": s.remaining,
                        }
                        for s in slots
                    ],
                })
                print(f"  [{course_id}] {experiment.experiment_id:>4} {experiment.name[:24]:24s} "
                      f"场次 {len(slots):3d}，有余额 {len(free):3d}")
            entry["free_slot_count"] = free_of_course
            snapshot["courses"].append(entry)
        snapshot["totals"] = {
            "projects": total_projects,
            "slots": total_slots,
            "free_slots": total_free,
        }
    finally:
        client.close()

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = snap_dir / f"snapshot-{stamp}.json"
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    print("-" * 74)
    print(f"合计：实验项目 {total_projects} 个，场次 {total_slots} 条，其中有余额 {total_free} 条")
    print(f"快照已保存 → {out}")
    if total_slots == 0:
        print("[说明] 场次为 0 是如实结果：可能该学期尚未放课/已结束，或该项目的排课未发布。")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    """余量监控：低频轮询关注场次的剩余名额，变化即时打印并逐轮落盘。"""
    from . import api, monitor

    try:
        client = api.PhyExpClient(timeout=args.timeout)
    except api.ApiError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    try:
        semesters = client.open_semesters()
        if not semesters:
            print("[警告] 当前没有开放学期。")
            return 0
        courses = client.my_courses(semesters[0].get("id"))
        if not courses:
            print("[警告] 该学期没有我的课程。")
            return 0
        course_id = args.course or courses[0].get("id")

        if args.projects:
            project_ids = [p.strip() for p in args.projects.split(",") if p.strip()]
        else:
            project_ids = [client.to_experiment(r).experiment_id
                           for r in client.course_projects(course_id)]
        print(f"课程 id={course_id}，监控 {len(project_ids)} 个实验项目；"
              f"间隔 {args.interval}s，轮数 {'不限' if args.rounds <= 0 else args.rounds}")

        cfg = monitor.MonitorConfig(min_interval_seconds=args.interval)
        writer = None if args.no_save else monitor.SampleWriter.default()
        # 长跑任务用文件日志：管道被断开时不会静默死在一次 print 上（见 monitor.FileLogger）
        log_path = config.logs_dir() / f"watch-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        logger = monitor.FileLogger(log_path)
        if writer:
            logger(f"样本文件 → {writer.path}")
        logger(f"进度日志 → {log_path}")
        mon = monitor.SlotMonitor(client, cfg, writer=writer, log=logger)

        def on_change(slot, prev) -> None:  # type: ignore[no-untyped-def]
            old = "（首次见到）" if prev is None else str(prev.remaining)
            logger(f"  [变化] 场次 {slot.slot_id} {slot.time_text} "
                   f"余量 {old} → {slot.remaining}（{slot.taken}/{slot.capacity}）"
                   f"{' @ ' + slot.location if slot.location else ''}")

        mon.run(course_id, project_ids,
                max_rounds=(None if args.rounds <= 0 else args.rounds),
                on_change=on_change)
    except api.ApiError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    finally:
        client.close()

    if not args.no_save:
        print("[提示] 样本已逐轮落盘；研究阶段建议用 60–300s 间隔，别高频打扰系统。")
    return 0


def _cmd_probe(args: argparse.Namespace) -> int:
    """只读连通性自检：脚本直连 API 到底行不行（决定抢课引擎形态）。"""
    print(f"目标：GET {config.API_BASE}/{args.path.lstrip('/')}   （只读，无写操作）")
    results = probe.probe_endpoint(path=args.path, timeout=args.timeout)
    print(f"{'用例':34s} {'状态':>6s} {'耗时':>7s}  说明")
    print("-" * 92)
    for r in results:
        status = "-" if r.status is None else str(r.status)
        elapsed = "-" if r.elapsed_ms is None else f"{r.elapsed_ms}ms"
        note = r.error or r.body_head[:44]
        print(f"{r.case.name:34s} {status:>6s} {elapsed:>7s}  {note}")
    print("-" * 92)
    print(probe.verdict(results))

    if args.repeat > 0:
        print()
        print(f"稳态延迟测量：同一条 keep-alive 会话连发 {args.repeat} 次 GET（首次含 TLS 握手，不计入结论）")
        try:
            samples = probe.measure_latency(path=args.path, repeat=args.repeat, timeout=args.timeout)
        except SessionError as exc:
            print(f"[跳过] {exc}")
            return 0
        print(f"  各次耗时(ms)：{', '.join(str(s) for s in samples)}")
        steady = samples[1:] or samples
        if steady:
            ordered = sorted(steady)
            median = ordered[len(ordered) // 2]
            print(f"  稳态（去掉首次）：最小 {min(steady)}ms / 中位 {median}ms / 最大 {max(steady)}ms  "
                  f"（样本 {len(steady)}）")
            print(f"  ⇒ 预发射提前量的量级参考：约 {median}ms（抢课参数别用首次那 {samples[0]}ms）")
    return 0


def _cmd_scrub(args: argparse.Namespace) -> int:
    source = Path(args.har)
    if not source.is_file():
        print(f"[错误] HAR 文件不存在：{source}", file=sys.stderr)
        return 2

    target = source if args.in_place else scrub.default_target(source)
    if args.in_place:
        backup = source.with_name(source.name + ".bak")
        backup.write_bytes(source.read_bytes())
        print(f"[info] 原文件已备份 → {backup}")

    stats = scrub.scrub_har(source, target)
    print(f"[完成] 脱敏输出 → {target}")
    print(f"  条目 {stats['entries']} 条：删除 postData {stats['post_data_removed']} 处、"
          f"脱敏敏感头 {stats['headers_redacted']} 处、删除敏感响应体 {stats['bodies_removed']} 条")

    problems = scrub.verify_no_credentials(target)
    if problems:
        print("[警告] 复检发现残留，请人工检查：")
        for item in problems[:20]:
            print(f"  - {item}")
        return 1
    print("[复检] 未发现明文凭据 / 敏感接口残留 postData / 残留 JWT ✅")
    return 0


def _cmd_stop(_args: argparse.Namespace) -> int:
    config.ensure_home()
    flag = config.stop_flag_path()
    flag.write_text("stop\n", encoding="utf-8")
    print(f"[完成] 已请求优雅收尾：{flag}")
    print("       正在运行的 login/recon 会在下一次轮询（≤1 秒）时保存会话与 HAR 后退出。")
    return 0


def _cmd_logout(_args: argparse.Namespace) -> int:
    removed = session.clear_state()
    if removed:
        for path in removed:
            print(f"[完成] 已删除 {path}")
    else:
        print("[跳过] 本来就没有会话文件。")
    return 0


# ── 选课 / 退课（写操作，2026-10-07 起可用）──


def _pick_semester_courses(client):
    """取「开放学期 + 我的课程」；返回 (semester, courses)。空课程时由调用方处理。"""
    semesters = client.open_semesters()
    if not semesters:
        return None, []
    semester = semesters[0]
    courses = client.my_courses(semester.get("id"))
    return semester, courses


def _cmd_elect(args: argparse.Namespace) -> int:
    """选课：`--list` 只读列出可约场次；给定 `--slot` + `--course` 则**真的提交选课**。"""
    from . import api

    config.ensure_home()
    try:
        client = api.PhyExpClient(timeout=args.timeout)
    except api.ApiError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    try:
        semester, courses = _pick_semester_courses(client)
        if semester is None:
            print("[警告] 当前没有开放学期 ⇒ 选课窗口很可能没开。")
            return 0
        if not courses:
            print(f"[警告] 学期 id={semester.get('id')} 下没有我的课程，无法选课。")
            return 0
        course_ids = [str(c.get("id")) for c in courses]
        picked = course_ids if args.course is None else [str(args.course)]
        for cid in picked:
            if cid not in course_ids:
                print(f"[警告] 课程 {cid} 不在我的课程列表里（我的：{', '.join(course_ids)}）。")

        # ── 只读列举 ──
        if args.list or not args.slot:
            print(f"开放学期        : id={semester.get('id')} {semester.get('name')} "
                  f"({semester.get('since')} ~ {semester.get('to')})")
            total_free = 0
            for course in courses:
                cid = course.get("id")
                name = course.get("name")
                mine = client.my_electives(semester.get("id"), cid)
                mine_slots = {str(m.get("schedule_id")) for m in mine}
                print()
                print(f"课程 id={cid} 「{name}」  我的选课记录 {len(mine)} 条")
                rows = []
                for row in client.course_projects(cid):
                    experiment = client.to_experiment(row)
                    for r in client.slots(cid, project_id=experiment.experiment_id,
                                          with_my_status=False):
                        slot = client.to_slot(r)
                        rows.append((experiment, slot))
                free = [(e, s) for e, s in rows if (s.remaining or 0) > 0]
                total_free += len(free)
                print(f"  该项目共 {len(rows)} 个场次，其中有余量 {len(free)} 个：")
                for experiment, slot in free:
                    flag = "✅已选" if slot.slot_id in mine_slots else "  "
                    print(f"    {flag} slot={slot.slot_id:>7}  {slot.time_text:24s} "
                          f"{slot.location or '-':12s} {slot.taken}/{slot.capacity} "
                          f"余 {slot.remaining}  [{experiment.name[:20]}]")
            print()
            print(f"合计有余量场次：{total_free} 个。")
            print("提交选课：python run.py elect --course <课程id> --slot <场次id>")
            return 0

        # ── 真提交（写操作）──
        course_id = args.course if args.course is not None else course_ids[0]
        slot_id = str(args.slot)
        print(f"[写操作] 即将提交选课：course_id={course_id} lesson_id={slot_id}")
        before = None
        try:
            before = client.schedule(slot_id)
            if before:
                taken = before.get("current_student_number")
                capacity = before.get("max_student_number")
                print(f"提交前场次状态  : {before.get('date')} 已选 {taken}/{capacity}")
        except api.ApiError as exc:
            print(f"[警告] 提交前读场次失败（继续尝试提交）：{exc}")

        if args.dry_run:
            print("[演练] --dry-run：未发送任何写请求（去掉该参数才会真的选课）。")
            return 0

        result = client.submit_booking(slot_id, course_id)
        print(f"[结果] {result.describe()}")
        if result.body_text:
            print(f"       服务端原始响应：{result.body_text[:200]}")
            print("       ⚠️ 注意：本系统成功时也返回 status:false"
                  "（实测 `HTTP 200 {\"status\":false,\"code\":200,\"message\":\"ok\"}`）"
                  "⇒ 判据只看 HTTP 状态码与 message 文案，不要用 status 字段。")

        # ── 服务端核实（不拿 HTTP 200 当成功）──
        print("[核实] 回读我的选课记录……")
        try:
            mine = client.my_electives(semester.get("id"), course_id)
            hit = [m for m in mine if str(m.get("schedule_id")) == slot_id]
            if hit:
                record = hit[0]
                print(f"  ✅ 服务端确实有这条选课记录：user2project_id={record.get('id')} "
                      f"status={record.get('schedule_status')}")
                print(f"     退课命令：python run.py cancel --id {record.get('id')}")
            else:
                print(f"  ⚠️ 回读未发现该场次（我的选课记录 {len(mine)} 条）⇒ "
                      f"请以服务端文案为准，不要当成成功。")
            after = client.schedule(slot_id)
            if after:
                print(f"  余量：{before.get('current_student_number') if before else '?'} → "
                      f"{after.get('current_student_number')}/{after.get('max_student_number')}")
        except api.ApiError as exc:
            print(f"  [警告] 核实失败（不影响提交本身）：{exc}")

        if not result.ok:
            print("[结论] 本次选课未成功；服务端文案见上，写操作日志在 "
                  f"{config.logs_dir()}\\write-*.jsonl")
            return 1
        print("[结论] 本次选课成功。")
        return 0
    except api.ApiError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    finally:
        client.close()


def _cmd_mine(args: argparse.Namespace) -> int:
    """只读：列出我的选课记录（含退课需要的 user2project_id）。"""
    from . import api

    try:
        client = api.PhyExpClient(timeout=args.timeout)
    except api.ApiError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    try:
        semester, courses = _pick_semester_courses(client)
        if semester is None:
            print("[警告] 当前没有开放学期。")
            return 0
        course_ids = [str(c.get("id")) for c in courses]
        picked = course_ids if args.course is None else [str(args.course)]
        total = 0
        for cid in picked:
            mine = client.my_electives(semester.get("id"), cid)
            name = next((c.get("name") for c in courses if str(c.get("id")) == cid), "")
            print(f"课程 id={cid} 「{name}」 共 {len(mine)} 条选课记录：")
            for record in mine:
                schedule = record.get("schedules") or {}
                periods = schedule.get("periods") or {}
                project = (schedule.get("projects") or {}).get("name") \
                    if isinstance(schedule.get("projects"), dict) else None
                when = f"{schedule.get('date', '')} " \
                       f"{periods.get('start_time', '')}-{periods.get('end_time', '')}".strip()
                print(f"  user2project_id={record.get('id'):>7}  slot={record.get('schedule_id'):>7}  "
                      f"{when:24s} {record.get('schedule_status', ''):10s} {project or ''}")
                total += 1
        print(f"合计 {total} 条。退课：python run.py cancel --id <user2project_id>")
        return 0
    except api.ApiError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    finally:
        client.close()


def _cmd_cancel(args: argparse.Namespace) -> int:
    """退课：`POST report-api/electives/<user2project_id>/cancel`（**写操作**）。"""
    from . import api

    try:
        client = api.PhyExpClient(timeout=args.timeout)
    except api.ApiError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    try:
        record_id = str(args.id)
        if args.dry_run:
            print(f"[演练] --dry-run：本应退课 user2project_id={record_id}（未发送写请求）。")
            return 0
        print(f"[写操作] 即将退课：user2project_id={record_id}")
        result = client.cancel_booking(record_id)
        print(f"[结果] {result.describe()}")
        if result.ok:
            print("[结论] 退课成功。建议用 `python run.py mine` 复核一次。")
            return 0
        print(f"[结论] 退课未成功；日志：{config.logs_dir()}\\write-*.jsonl")
        return 1
    finally:
        client.close()


# ── 自动抢课（1.0.3：按空闲时段尽量多选）──


def _parse_free_slot_args(raw: str) -> list:
    """把 `--free "2026-10-14 下午5、6节,2026-10-15 晚上9，10节"` 解析成 FreeSlot 列表。

    格式：`日期<空格>节次`，多项用英文逗号分隔；节次名支持常见等价写法（见 grabconfig）。
    """
    from . import grabconfig

    slots = []
    for chunk in str(raw or "").split(","):
        item = chunk.strip()
        if not item:
            continue
        parts = item.split(None, 1)
        if len(parts) != 2:
            print(f"[错误] --free 的每一项要写成「日期 节次」，实际是 {item!r}", file=sys.stderr)
            print(f"       合法节次：{' / '.join(grabconfig.PERIODS)}", file=sys.stderr)
            return []
        date, period = parts[0].strip(), parts[1].strip()
        try:
            slots.append(grabconfig.FreeSlot(date=date, period=period))
        except grabconfig.ConfigError as exc:
            print(f"[错误] {exc}", file=sys.stderr)
            return []
    return slots


def _cmd_autograb_config(args: argparse.Namespace) -> int:
    """写抢课配置：真实配置进运行时目录；`--example` 只打印示例。"""
    from . import grabconfig

    if args.example:
        example = grabconfig.example_path()
        if example.is_file():
            print(example.read_text(encoding="utf-8"))
        else:
            print(json.dumps(grabconfig.GrabPlan().to_dict(), ensure_ascii=False, indent=2))
        return 0

    plan = grabconfig.GrabPlan()
    if args.course is not None:
        plan.course_id = args.course
    if args.free:
        slots = _parse_free_slot_args(args.free)
        if not slots:
            return 2
        plan.free_slots = slots
    if args.priority:
        plan.priority = args.priority
    if args.max_total is not None:
        plan.max_total = args.max_total
    if args.real:
        plan.dry_run = False
    if args.no_notify:
        plan.notify = False
    if args.at:
        plan.target_at = args.at

    if not plan.free_slots:
        print("[提示] 还没给空闲时段：请加 --free，例如：")
        print('       python run.py autograb-config --course 71 '
              '--free "2026-10-14 下午5、6节,2026-10-15 晚上9，10节"')
        path = grabconfig.write_config(plan)
        print(f"[完成] 已写入默认配置（尚未含空闲时段）→ {path}")
        return 0

    try:
        plan.validate()
    except grabconfig.ConfigError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    path = grabconfig.write_config(plan)
    print(f"[完成] 配置已写入 → {path}")
    print(f"       课程 id：{plan.course_id if plan.course_id is not None else '（取我的第一门课）'}")
    print(f"       空闲时段 {len(plan.free_slots)} 个：" +
          "；".join(s.describe() for s in plan.free_slots))
    print(f"       优先级：{plan.priority}（余量多的先抢）；上限："
          f"{'不设' if not plan.max_total else plan.max_total}")
    print(f"       提交模式：{'演练（dry_run=true，不会真提交）' if plan.dry_run else '真实提交（dry_run=false）'}"
          f"；桌面通知：{'开' if plan.notify else '关'}")
    if plan.dry_run:
        print("       ⚠️ 现在仍是演练模式；确认计划无误后加 --real 才会真实提交。")
    return 0


def _cmd_autograb(args: argparse.Namespace) -> int:
    """自动抢课：计划 → （可选）等到点 → 按空闲时段尽量多选 → 回读核实 → 通知。"""
    from . import grabconfig, notify as notify_mod, planner, runner

    # 1) 配置
    try:
        plan_cfg = (grabconfig.GrabPlan.from_dict(json.loads(Path(args.config).read_text(encoding="utf-8")))
                    if args.config else grabconfig.load_config())
    except grabconfig.ConfigError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print(f"[错误] 读取配置失败（{type(exc).__name__}）：{exc}", file=sys.stderr)
        return 2

    if args.free:
        slots = _parse_free_slot_args(args.free)
        if not slots:
            return 2
        plan_cfg.free_slots = slots
    if args.course is not None:
        plan_cfg.course_id = args.course
    if args.priority:
        plan_cfg.priority = args.priority
    if args.max_total is not None:
        plan_cfg.max_total = args.max_total
    if args.real:
        plan_cfg.dry_run = False
    if args.dry_run:
        plan_cfg.dry_run = True
    if args.no_notify:
        plan_cfg.notify = False

    try:
        plan_cfg.validate()
    except grabconfig.ConfigError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 2

    mode = "演练（不会真提交）" if plan_cfg.dry_run else "真实提交（会写进你的课表）"
    print(f"[抢课] 模式：{mode}；课程 id：{plan_cfg.course_id if plan_cfg.course_id is not None else '（我的第一门课）'}")

    log_lines: list[str] = []

    def log(msg: str) -> None:
        log_lines.append(msg)
        print(msg, flush=True)

    engine = runner.Runner(plan_cfg, log=log)
    try:
        # 2) 只出计划
        if args.plan_only:
            client = engine._client_or_create()          # noqa: SLF001 - 内部装配，CLI 只读用
            plan, _semester, _course = planner.fetch_plan(client, plan_cfg)
            print()
            for line in planner.render_plan(plan, plan_cfg):
                print(line)
            print()
            print("[plan-only] 只读规划，未提交任何请求。")
            return 0

        # 3) 到点自动开抢（服务端时刻为准）
        if args.at:
            from . import probe

            target_epoch = _target_server_epoch(args.at)
            print(f"[等待] 目标（服务端时钟）{args.at}；本地对应 "
                  f"{dt.datetime.fromtimestamp(target_epoch).astimezone().isoformat(timespec='seconds')}")
            wait_seconds = target_epoch - dt.datetime.now().timestamp()
            if wait_seconds <= 0:
                print("[警告] 目标时刻已过，立即执行。")
            else:
                print(f"[等待] 还需 {wait_seconds / 60:.1f} 分钟；等待期间会定期检查登录态。")
                if not _wait_until(target_epoch, plan_cfg, log):
                    return 130

        # 4) 执行
        report = engine.run()
    except Exception as exc:  # noqa: BLE001 - 顶层兜底：任何异常都要发通知，不能静默死掉
        message = f"抢课流程异常终止（{type(exc).__name__}）：{exc}"
        print(f"[错误] {message}", file=sys.stderr)
        if plan_cfg.notify:
            notify_mod.notify("抢课异常终止", message[:180], log=log)
        return 1
    finally:
        engine.close()

    # 5) 结果
    print()
    print("=== 结果 ===")
    for line in report.summary_lines():
        print(line)
    log_path = config.logs_dir() / f"autograb-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")
        print()
        print(f"完整日志 → {log_path}")
    except OSError as exc:
        print(f"[警告] 日志落盘失败：{exc}", file=sys.stderr)

    # 6) 抢完之后的"选择退课"（用户 2026-10-07 要求：本轮抢了什么、可当场选择退）
    if not plan_cfg.dry_run and report.succeeded and not args.no_prompt:
        _post_run_review(engine, report, plan_cfg)

    if report.aborted_reason:
        return 3
    if plan_cfg.dry_run:
        return 0
    return 0 if report.succeeded else 1


def _post_run_review(engine, report, plan_cfg) -> None:
    """显示本轮抢到的结果，并让用户当场选择退掉其中若干（**不可逆，二次确认**）。"""
    from . import prompt as prompt_mod

    print()
    print("=== 本轮抢到 ===")
    for index, attempt in enumerate(report.succeeded, 1):
        print(f"  {index}. {attempt.candidate.date} {attempt.candidate.period}  "
              f"{attempt.candidate.project_name}  {attempt.candidate.location or '-'}  "
              f"slot={attempt.candidate.slot_id}  user2project_id={attempt.record_id}")

    if not prompt_mod.interactive_available():
        print()
        print("[跳过退课选择] 当前不是可交互终端（或输出被重定向）。")
        print("        如需退课：python run.py cancel --id <user2project_id>")
        print("        （上面每条都打印了 user2project_id）")
        return

    print()
    print("全部保留：直接回车即可。")
    picks = prompt_mod.ask_selection(len(report.succeeded), prompt="要退掉哪几条")
    if picks is None:
        print("[已放弃] 未做任何退课，全部保留。")
        return
    if not picks:
        print("[已选择] 全部保留。")
        return

    print()
    print("即将退掉：")
    for index in picks:
        item = report.succeeded[index]
        print(f"  - {item.candidate.date} {item.candidate.period} "
              f"{item.candidate.project_name}（user2project_id={item.record_id}）")
    if not prompt_mod.confirm("确认退课？（不可撤销，但可以重新抢）"):
        print("[已取消] 未做任何退课，全部保留。")
        return

    print()
    print("=== 退课结果 ===")
    for index in picks:
        attempt = report.succeeded[index]
        ok, detail = engine.cancel_pick(attempt)
        mark = "✓" if ok else "✗"
        print(f"  {mark} {attempt.candidate.date} {attempt.candidate.period} "
              f"{attempt.candidate.project_name}：{detail}")
    print()
    print("如需复核：python run.py mine")


def _target_server_epoch(at: str) -> float:
    """把"服务端墙上时刻 HH:MM:SS（今天）"换算成本地 epoch（**对时后再算**）。"""
    from . import probe

    try:
        offset = probe.measure_clock_offset(samples=5).offset_seconds
    except Exception as exc:  # noqa: BLE001 - 对时失败就用本地钟，但必须说清楚
        print(f"[警告] 对时失败（{type(exc).__name__}：{exc}）⇒ 按本地时钟打点（可能有秒级误差）")
        offset = 0.0
    now_local = dt.datetime.now().astimezone()
    hour, minute, second = (int(x) for x in str(at).split(":"))
    wall = now_local.replace(hour=hour, minute=minute, second=second, microsecond=0)
    if wall.timestamp() < now_local.timestamp() - 60:
        wall = wall + dt.timedelta(days=1)     # 已过则该时刻指"明天"
    return wall.timestamp() - offset


def _wait_until(target_epoch: float, plan_cfg, log) -> bool:
    """等到目标时刻；期间定期检查登录态。返回 False = 被中断/登录态失效。"""
    from . import grabconfig, runner

    checked = 0.0
    while True:
        remain = target_epoch - dt.datetime.now().timestamp()
        if remain <= 0.05:
            break
        try:
            time.sleep(min(remain, 30.0))
        except KeyboardInterrupt:
            log("[中断] 用户取消等待。")
            return False
        # 每约 10 分钟检查一次 token（JWT 实测 2 小时过期，长时间等待必须先发现失效）
        if dt.datetime.now().timestamp() - checked > 600:
            checked = dt.datetime.now().timestamp()
            try:
                from . import api as api_mod

                client = api_mod.PhyExpClient(timeout=10.0)
                try:
                    left = runner.Runner(plan_cfg).token_seconds_left(client)
                    log(f"[检查] 距离目标还有 {remain / 60:.1f} 分钟；"
                        f"登录态剩余 {'未知' if left is None else f'{int(left)} 秒'}")
                finally:
                    client.close()
            except Exception as exc:  # noqa: BLE001 - 检查失败不打断等待
                log(f"[检查] 登录态检查失败（{type(exc).__name__}）：{exc}")
    return True


# ── 解析器 ──


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phyexp",
        description="南航物理实验中心数据研究 / 物理实验预约辅助工具",
    )
    parser.add_argument("--version", action="version", version=f"nuaa-phyexp-lab {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="查看版本与目标系统")
    sub.add_parser("status", help="查看数据目录 / 会话 / 采集文件状态")
    sub.add_parser("logout", help="删除本地会话文件")
    sub.add_parser("stop", help="让正在运行的 login/recon 优雅收尾（保存 HAR 后退出）")
    p_gui = sub.add_parser("gui", help="图形界面：默认只读工作台；--grab 打开抢课面板")
    p_gui.add_argument("--grab", action="store_true",
                       help="打开抢课面板（两周网格点选空闲时段 → 到点抢 → 结果可退课）")
    p_gui.add_argument("--self-check", action="store_true",
                       help="抢课面板的脚本化自检（用假客户端，不发网络请求、不碰账号）")

    p_clock = sub.add_parser("clock", help="时钟对时：测出「服务端 − 本地」偏移")
    p_clock.add_argument("--samples", type=int, default=7, help="对时采样次数（默认 7）")
    p_clock.add_argument("--timeout", type=float, default=10.0, help="单次请求超时秒数（默认 10）")

    p_analyze = sub.add_parser("analyze", help="分析采样样本：退课/被抢事件与空位存活时间")
    p_analyze.add_argument("--samples", nargs="*", default=None,
                           help="指定样本 JSONL 文件（默认取样本目录下全部）")

    p_elect = sub.add_parser("elect", help="选课：列出可约场次 / 提交选课（--slot 时为写操作）")
    p_elect.add_argument("--list", action="store_true",
                         help="只读：列出各课程有余量的场次（不加 --slot 时默认就是列清单）")
    p_elect.add_argument("--course", default=None, help="课程 id（默认我的第一门课；提交时必填准确值）")
    p_elect.add_argument("--slot", default=None,
                         help="场次 id（= schedules.id = 前端 lesson_id）；给了它才会提交选课")
    p_elect.add_argument("--dry-run", action="store_true", help="演练：只打印要做的事，不发写请求")
    p_elect.add_argument("--timeout", type=float, default=10.0, help="单次请求超时秒数（默认 10）")

    p_mine = sub.add_parser("mine", help="只读：列出我的选课记录（含退课用的 user2project_id）")
    p_mine.add_argument("--course", default=None, help="课程 id（默认我的全部课程）")
    p_mine.add_argument("--timeout", type=float, default=10.0, help="单次请求超时秒数（默认 10）")

    p_cancel = sub.add_parser("cancel", help="退课（写操作）：按选课记录 id 退课")
    p_cancel.add_argument("--id", required=True, help="选课记录 id（rest/user2projects.id，见 `mine`）")
    p_cancel.add_argument("--dry-run", action="store_true", help="演练：只打印要做的事，不发写请求")
    p_cancel.add_argument("--timeout", type=float, default=10.0, help="单次请求超时秒数（默认 10）")

    p_autograb = sub.add_parser(
        "autograb",
        help="自动抢课：按你勾选的空闲时段（具体日期+节次）尽量多选，并回读核实 + 桌面通知")
    p_autograb.add_argument("--config", default=None, help="配置文件路径（默认取运行时目录的 config.json）")
    p_autograb.add_argument("--free", default=None,
                            help='临时指定空闲时段："2026-10-14 下午5、6节,2026-10-15 晚上9，10节"')
    p_autograb.add_argument("--course", default=None, help="课程 id（默认取我的第一门课）")
    p_autograb.add_argument("--priority", default=None,
                            choices=["remaining_desc", "date_asc"],
                            help="排序：remaining_desc=余量多的先抢（默认）；date_asc=日期早的先抢")
    p_autograb.add_argument("--max-total", type=int, default=None, help="本轮最多选几个（默认 0 = 不设上限）")
    p_autograb.add_argument("--at", default=None,
                            help="服务端墙上时刻 HH:MM:SS（今天/已过则明天）：到点自动开抢")
    p_autograb.add_argument("--plan-only", action="store_true", help="只看计划（只读，不提交）")
    p_autograb.add_argument("--real", action="store_true", help="真实提交（会写进课表；默认演练）")
    p_autograb.add_argument("--dry-run", action="store_true", help="显式演练（默认就是演练）")
    p_autograb.add_argument("--no-notify", action="store_true", help="不发桌面通知")
    p_autograb.add_argument("--no-prompt", action="store_true",
                            help="抢完后不询问是否退课（适合无人值守/重定向输出）")

    p_agc = sub.add_parser("autograb-config", help="写/查看抢课配置（真实配置进运行时目录）")
    p_agc.add_argument("--free", default=None,
                       help='空闲时段："2026-10-14 下午5、6节,2026-10-15 晚上9，10节"')
    p_agc.add_argument("--course", default=None, help="课程 id（默认取我的第一门课）")
    p_agc.add_argument("--priority", default=None, choices=["remaining_desc", "date_asc"],
                       help="排序策略（默认 remaining_desc）")
    p_agc.add_argument("--max-total", type=int, default=None, help="上限（0 = 不设）")
    p_agc.add_argument("--at", default=None, help="默认目标时刻 HH:MM:SS（可选）")
    p_agc.add_argument("--real", action="store_true", help="把 dry_run 设为 false（谨慎）")
    p_agc.add_argument("--no-notify", action="store_true", help="关闭桌面通知")
    p_agc.add_argument("--example", action="store_true", help="只打印示例配置（不写文件）")

    p_grab = sub.add_parser("grab", help="抢课引擎：对时 + 预热 + 精确定时 + 预发射（默认演练，--real 才真发）")
    p_grab.add_argument("--slot", required=True, help="目标场次 id（可逗号分隔多个）")
    p_grab.add_argument("--at", default=None, help="服务端墙上时刻 HH:MM:SS（今天）")
    p_grab.add_argument("--in", dest="in_seconds", type=float, default=None,
                        help="从现在起多少秒后发射（演练方便；与 --at 二选一）")
    p_grab.add_argument("--real", action="store_true",
                        help="真实提交（写操作，会真的写进你的课表；载荷已于 2026-10-07 实测确认）")
    p_grab.add_argument("--course", default=None,
                        help="课程 id（--real 时必需：写接口要 lesson_id + course_id 两个字段）")
    p_grab.add_argument("--pre-fire", type=int, default=50, help="预发射提前毫秒数（默认 50）")
    p_grab.add_argument("--interval", type=int, default=800, help="两次提交最小间隔毫秒（默认 800）")
    p_grab.add_argument("--max-attempts", type=int, default=5, help="最大尝试次数（默认 5）")
    p_grab.add_argument("--no-clock", action="store_true", help="跳过对时（不推荐）")
    p_grab.add_argument("--plan-only", action="store_true",
                        help="只打印发射计划就退出（窗口当天先核对计划用）")
    p_grab.add_argument("--timeout", type=float, default=10.0, help="单次请求超时秒数（默认 10）")

    p_login = sub.add_parser("login", help="打开浏览器登录并保存会话")
    p_login.add_argument("--max-wait", type=int, default=1800,
                         help="最长等待秒数（默认 1800，即 30 分钟）")
    p_login.add_argument("--no-saved-state", action="store_true",
                         help="不载入上次会话，强制重新登录")

    p_recon = sub.add_parser("recon", help="侦察：登录并记录请求（HAR + 脱敏 JSONL）")
    p_recon.add_argument("--max-wait", type=int, default=1800,
                         help="最长等待秒数（默认 1800，即 30 分钟）")
    p_recon.add_argument("--no-saved-state", action="store_true",
                         help="不载入上次会话，强制重新登录")

    p_scrub = sub.add_parser("scrub", help="HAR 脱敏：抹掉凭据与敏感响应体后再做分析")
    p_scrub.add_argument("har", help="要脱敏的 .har 文件路径")
    p_scrub.add_argument("--in-place", action="store_true",
                         help="就地覆盖（会先生成 .bak 备份）；默认输出 <名字>.scrubbed.har")

    p_probe = sub.add_parser("probe", help="只读自检：脚本直连 API 是否可行（决定抢课引擎形态）")
    p_probe.add_argument("--path", default="rest/time",
                         help="要探测的接口路径（相对 API 基址，默认 rest/time；只发 GET）")
    p_probe.add_argument("--timeout", type=float, default=10.0, help="单次请求超时秒数（默认 10）")
    p_probe.add_argument("--repeat", type=int, default=5,
                         help="额外做 N 次 keep-alive 连发以测稳态 RTT（默认 5；填 0 跳过）")

    p_snapshot = sub.add_parser("snapshot", help="只读采集课程/实验项目/场次余量并落盘")
    p_snapshot.add_argument("--timeout", type=float, default=20.0, help="单次请求超时秒数（默认 20）")
    p_snapshot.add_argument("--all-status", action="store_true",
                            help="取该项目全部已发布场次（研究余量用）；不加则只取「我已选/已排」的场次（与前端首页一致）")

    p_watch = sub.add_parser("watch", help="余量监控：低频轮询关注场次的剩余名额（只读）")
    p_watch.add_argument("--course", default=None, help="课程 id（默认取我该学期第一门课）")
    p_watch.add_argument("--projects", default=None,
                         help="逗号分隔的实验项目 id（默认监控该课程全部项目）")
    p_watch.add_argument("--interval", type=float, default=60.0,
                         help="轮询间隔秒数（默认 60；研究期建议 60–300，追放闸时可临时 3–10）")
    p_watch.add_argument("--rounds", type=int, default=0, help="轮数（默认 0 = 一直跑；研究/测试可设小值）")
    p_watch.add_argument("--timeout", type=float, default=20.0, help="单次请求超时秒数（默认 20）")
    p_watch.add_argument("--no-save", action="store_true", help="不落盘样本（仅打印）")

    p_watch_bg = sub.add_parser("watch-bg", help="把余量采样器作为独立进程长跑（脱离会话）")
    p_watch_bg.add_argument("--course", default=None, help="课程 id（默认取我该学期第一门课）")
    p_watch_bg.add_argument("--projects", default=None, help="逗号分隔的实验项目 id（默认全部）")
    p_watch_bg.add_argument("--interval", type=float, default=60.0, help="轮询间隔秒数（默认 60）")
    p_watch_bg.add_argument("--rounds", type=int, default=0, help="轮数（默认 0 = 一直跑）")

    sub.add_parser("watch-stop", help="停止独立采样进程")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "version": _cmd_version,
        "status": _cmd_status,
        "login": _cmd_login,
        "recon": _cmd_recon,
        "scrub": _cmd_scrub,
        "probe": _cmd_probe,
        "snapshot": _cmd_snapshot,
        "watch": _cmd_watch,
        "watch-bg": _cmd_watch_bg,
        "watch-stop": _cmd_watch_stop,
        "gui": _cmd_gui,
        "clock": _cmd_clock,
        "grab": _cmd_grab,
        "analyze": _cmd_analyze,
        "elect": _cmd_elect,
        "mine": _cmd_mine,
        "cancel": _cmd_cancel,
        "autograb": _cmd_autograb,
        "autograb-config": _cmd_autograb_config,
        "stop": _cmd_stop,
        "logout": _cmd_logout,
    }
    try:
        return handlers[args.command](args)
    except BrokenPipeError:
        # 输出管道断开时不要抛异常（长跑任务里这会变成"裸 exit 1、无任何线索"）
        return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
