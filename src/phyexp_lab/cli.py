"""命令行入口：`python run.py <子命令>`。

子命令
------
- `version`  查看版本
- `status`   查看数据目录、会话状态与最近的采集文件
- `login`    打开浏览器登录并保存会话
- `recon`    侦察：登录 + 记录请求（HAR + 脱敏 JSONL）
- `logout`   删除本地会话文件
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
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
    engine = grabber.Grabber(client, cfg)
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
    print("[演练汇总]（dry-run 的结果**不是**成功，只说明定时链路走通了）")
    for attempt in engine.attempts:
        deviation = "-" if attempt.deviation_ms is None else f"{attempt.deviation_ms:+.1f}ms"
        print(f"  场次 {attempt.slot_id}  结果 {attempt.outcome.value}  计划偏差 {deviation}")
    if args.real:
        print("[提示] 真实提交尚未实现：写接口需在选课窗口开放时实测后再接入。")
    return 0


def _cmd_gui(_args: argparse.Namespace) -> int:
    """启动 PySide6 只读工作台。"""
    try:
        from . import gui
    except ImportError as exc:
        print(f"[错误] 未安装 PySide6，无法启动界面：{exc}", file=sys.stderr)
        print("       安装：pip install PySide6", file=sys.stderr)
        return 2
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
        print("[说明] 场次为 0 是**如实结果**：可能该学期尚未放课/已结束，或该项目的排课未发布。")
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
        if writer:
            print(f"样本文件 → {writer.path}")
        mon = monitor.SlotMonitor(client, cfg, writer=writer)

        def on_change(slot, prev) -> None:  # type: ignore[no-untyped-def]
            old = "（首次见到）" if prev is None else str(prev.remaining)
            print(f"  [变化] 场次 {slot.slot_id} {slot.time_text} "
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
    sub.add_parser("gui", help="启动图形界面（PySide6 只读工作台）")

    p_clock = sub.add_parser("clock", help="时钟对时：测出「服务端 − 本地」偏移")
    p_clock.add_argument("--samples", type=int, default=7, help="对时采样次数（默认 7）")
    p_clock.add_argument("--timeout", type=float, default=10.0, help="单次请求超时秒数（默认 10）")

    p_analyze = sub.add_parser("analyze", help="分析采样样本：退课/被抢事件与空位存活时间")
    p_analyze.add_argument("--samples", nargs="*", default=None,
                           help="指定样本 JSONL 文件（默认取样本目录下全部）")

    p_grab = sub.add_parser("grab", help="抢课引擎：对时 + 预热 + 精确定时 + 预发射（当前默认演练）")
    p_grab.add_argument("--slot", required=True, help="目标场次 id（可逗号分隔多个）")
    p_grab.add_argument("--at", default=None, help="服务端墙上时刻 HH:MM:SS（今天）")
    p_grab.add_argument("--in", dest="in_seconds", type=float, default=None,
                        help="从现在起多少秒后发射（演练方便；与 --at 二选一）")
    p_grab.add_argument("--real", action="store_true",
                        help="真实提交（**目前会明确报错**：写接口未实测，禁止猜测参数）")
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
                            help="取该项目**全部已发布**场次（研究余量用）；不加则只取「我已选/已排」的场次（与前端首页一致）")

    p_watch = sub.add_parser("watch", help="余量监控：低频轮询关注场次的剩余名额（只读）")
    p_watch.add_argument("--course", default=None, help="课程 id（默认取我该学期第一门课）")
    p_watch.add_argument("--projects", default=None,
                         help="逗号分隔的实验项目 id（默认监控该课程全部项目）")
    p_watch.add_argument("--interval", type=float, default=60.0,
                         help="轮询间隔秒数（默认 60；研究期建议 60–300，追放闸时可临时 3–10）")
    p_watch.add_argument("--rounds", type=int, default=0, help="轮数（默认 0 = 一直跑；研究/测试可设小值）")
    p_watch.add_argument("--timeout", type=float, default=20.0, help="单次请求超时秒数（默认 20）")
    p_watch.add_argument("--no-save", action="store_true", help="不落盘样本（仅打印）")
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
        "gui": _cmd_gui,
        "clock": _cmd_clock,
        "grab": _cmd_grab,
        "analyze": _cmd_analyze,
        "stop": _cmd_stop,
        "logout": _cmd_logout,
    }
    return handlers[args.command](args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
