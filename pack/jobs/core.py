"""Local job protocol and storage used by the Mac launcher and the CLI.

UI/storage helpers are adapted from LumaDock (MIT; launchers/macos/README.md).
Engine operations always invoke S2Arm's own tools.
"""
from __future__ import annotations

import contextlib
import datetime
import errno
import filelock
import winproc
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from errors import Failure, explain, failure_summary, last_output_lines, recovery


def default_developer_dir():
    """请求没带 developerDir 时用哪套 Xcode：装着的 Xcode 里最新的（toolchain.detect_xcode，含 xcode-select 指的那个；与启动器缺省、tools/toolchain.sh 同一条规则）。
    11-07 Duo 定因：原先取 xcode-select（本机 26.5），命令行 / 脚本发的请求不带 developerDir 就用 26.5 SDK 出壳，iOS 27 新机型上整个 App 缩小居中。
    找不到 → None（不设 DEVELOPER_DIR，由工具链检查说清「没有 Xcode」）"""
    from toolchain import detect_xcode
    p = detect_xcode()
    return p if p and os.path.isdir(p) else None


def shared_redact():
    # App 里 build_app.py 把 tools/redact.py 拷进 Resources/Jobs；源码树里从仓库 tools/ 取
    try:
        import redact
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        import redact
    return redact


class Cancelled(Exception):
    pass


def read(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except FileNotFoundError:
        return default


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def contained(root, path):
    root, path = Path(root).resolve(), Path(path).resolve()
    if path == root or root not in path.parents:
        raise Failure("操作路径不属于托管目录。")
    return path


def output_lines(stream, chunk=65536):
    """按行切子进程输出：LF 与 CR 都算换行。

    xcodebuild 这类工具用 CR 原地刷新进度（一整段下载可能十几分钟只有 CR、没有 LF）：
    按 readline() 读会把它当成一行 60 KB 的文本，调用方用第一个百分比判进度，就一直停在 0.0%。
    """
    read = getattr(stream, "read1", None) or stream.read   # read1：有数据就返回，不等凑满（CR 进度不换行）
    pending = b""
    while True:
        data = read(chunk)
        if not data:
            break
        pending += data
        start = 0
        i = 0
        while i < len(pending):
            byte = pending[i]
            if byte in (10, 13):
                yield pending[start:i]
                if byte == 13 and i + 1 < len(pending) and pending[i + 1] == 10:
                    i += 1   # CRLF 是一处换行，不产生空行
                start = i + 1
            i += 1
        pending = pending[start:]
    if pending:
        yield pending


def _sha256():
    """SHA-256 计算器：与资源包 / 核对共用 tools/sha256_fast.py（Windows 走 CNG）。App 里 build_app.py 把它拷进 Resources/Jobs；源码树里从仓库 tools/ 取"""
    try:
        import sha256_fast
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
        import sha256_fast
    return sha256_fast.new()


def digest(path, progress=None):
    h = _sha256()
    done = 0
    total = Path(path).stat().st_size
    with Path(path).open("rb") as f:
        while chunk := f.read(4 << 20):
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    return h.hexdigest()


def clone(source, destination, progress=None):
    source, destination = Path(source), Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # macOS clonefile is an optimization, never part of the data format.
    import ctypes
    copied = False
    if sys.platform == "darwin" and hasattr(ctypes.CDLL(None), "clonefile"):   # Windows 上 CDLL(None) 直接抛 TypeError（11-07 内置引擎初始化）
        libc = ctypes.CDLL(None, use_errno=True)
        copied = libc.clonefile(os.fsencode(source), os.fsencode(destination), 0) == 0
    if not copied:
        with source.open("rb") as src, destination.open("xb") as dst:
            done, total = 0, source.stat().st_size
            while chunk := src.read(4 << 20):
                dst.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    if progress:
        progress(source.stat().st_size, source.stat().st_size)


def tree_hash(root, folders):
    h = hashlib.sha256()
    for folder in folders:
        top = Path(root) / folder
        paths = top.rglob("*") if top.is_dir() else [top]
        for p in sorted(paths):
            if set(p.relative_to(root).parts) & {"bin", "obj", "build", ".build", ".git", "__pycache__"}:
                continue
            if p.is_file():
                h.update(str(p.relative_to(root)).encode())
                h.update(bytes.fromhex(digest(p)))
    return h.hexdigest()


class Context:
    def __init__(self, request):
        # 空间预检没过、玩家点「仍然继续」重交的出包任务（library.check_pack_space）：只认布尔值、只给 Mac 打包 / 打 iPhone 包
        if "spaceOverride" in request and (not isinstance(request["spaceOverride"], bool) or request.get("operation") not in ("build", "ipa")):
            raise Failure("spaceOverride 仅支持 Mac 打包（build）与打 iPhone 包（ipa）任务的布尔值。")
        self.r = request
        self.default_developer = None if request.get("developerDir") else default_developer_dir()   # 没传 Xcode 时的缺省：建任务时取一次装着的最新 Xcode，全程共用
        self.task = request.get("jobId") or request.get("taskId") or str(uuid.uuid4())
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", self.task):
            raise Failure("无效的任务标识。")
        self.operation = request["operation"]
        self.private = Path(request["privateRoot"]).expanduser().resolve()
        self.private.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.private, 0o700)
        self.engine = Path(request.get("engineDir") or ".").expanduser().resolve()
        self.library = Path(request.get("library") or self.private / "unconfigured").expanduser().resolve()
        self.keys = self.private / "keys/prod.keys"
        self.t0 = time.monotonic()
        self.stage = ""
        self.number = self.count = self.seq = 0
        self.cancelled = False
        self.children = set()
        self.guard = threading.RLock()
        self.last_progress = 0
        self.last_log = None
        self.frontend_gone = False
        self.jobpath = self.private / "jobs" / (self.task + ".json")
        self.events = self.private / "jobs" / (self.task + ".events.jsonl")
        self.requestpath = self.private / "jobs" / (self.task + ".request.json")
        # Keep the request next to the journal so `resume` can run the same task again after a kill without the caller having to keep the
        # request file around (J10). 0600 like the request itself: it holds player paths, never keys.
        if not self.requestpath.exists():
            atomic(self.requestpath, request)
            os.chmod(self.requestpath, 0o600)
        # The journal of a previous run of this same task: an attempt that never wrote a terminal state (killed, power loss, frontend gone) is the
        # only thing that can tell a restart apart from a first run, so keep what it reached instead of overwriting it silently (J10 / #186).
        previous = read(self.jobpath) or {}
        attempt = dict(previous.get("attempts") or {}) if previous.get("id") == self.task else {}
        self.job = dict(schema=1, id=self.task, operation=self.operation, gameId=request.get("gameId"),
                        state="running", started=time.time(), diagnostics=[], pid=os.getpid(), pidStart=pid_start(os.getpid()),
                        attempts=attempt)
        if previous.get("id") == self.task:
            # Every new run of the same task is another attempt, whether the last one was killed or finished (`resume --force`); what the last
            # attempt reached is kept either way, and `ended` on it tells `describe()` whether it was interrupted.
            self.job.update(attemptNo=int(previous.get("attemptNo") or 1) + 1, previousState=previous.get("state"),
                            previousStage=previous.get("stage") or "", previousStarted=previous.get("started"),
                            previousEnded=previous.get("ended"))
        else:
            self.job["attemptNo"] = 1
        atomic(self.jobpath, self.job)

    def check(self):
        if self.cancelled:
            raise Cancelled()

    def require_source(self):
        required = ("runtime/include/s2a_native.h", "runtime/rewriter/rewriter.c", "services/nxaot.sh",
                    "pack/nx_unpack/nx_unpack.csproj", "graphics/tools/shader_pack.sh", "tools/safe_killpg.sh")
        missing = [p for p in required if not (self.engine / p).is_file()]
        if missing:
            raise Failure("请选择完整的 S2Arm 源码目录，缺少：" + "、".join(missing), code="engine_incomplete")

    def require_library(self):
        marker = read(self.library / ".s2arm-library.json", {})
        if not self.r.get("libraryId") or marker.get("id") != self.r["libraryId"]:
            parts = self.library.parts
            if len(parts) > 2 and parts[1] == "Volumes" and not Path("/", "Volumes", parts[2]).exists():
                raise Failure(f"游戏库所在的磁盘「{parts[2]}」没连上：插上这块盘后再试一次。")
            raise Failure("游戏库未连接或标识不符，请重新选择游戏库。")

    @contextlib.contextmanager
    def lock(self, name="catalog"):
        self.check()
        self.require_library()
        with (self.library / ("." + name + ".lock")).open("a") as f:
            try:
                filelock.try_lock(f)
            except BlockingIOError:
                raise Failure("另一个任务正在使用游戏库，请稍后重试。") from None
            try:
                yield
            finally:
                filelock.unlock(f)

    def catalog(self):
        self.require_library()
        return read(self.library / "catalog.json", {"schema": 1, "games": []})

    def game(self):
        game = next((g for g in self.catalog()["games"] if g["id"] == self.r.get("gameId")), None)
        if not game:
            raise Failure("游戏未登记在当前游戏库中。")
        return game

    def emit(self, kind="progress", message="", done=None, total=None, **extra):
        with self.guard:
            self.check()
            now = time.monotonic()
            if kind == "progress" and now - self.last_progress < .2 and done != total:
                return
            if kind == "progress":
                self.last_progress = now
            elif kind == "stage":
                self.last_progress = 0
                self.job.update(done=None, total=None, unit=None)
            if kind not in ("stage", "progress"):
                done, total = self.job.get("done"), self.job.get("total")
                if self.job.get("unit"):
                    extra.setdefault("unit", self.job["unit"])
            self.seq += 1
            state = extra.pop("state", self.job["state"])
            event = dict(schema=1, jobId=self.task, taskId=self.task, seq=self.seq,
                         timestamp=datetime.datetime.now(datetime.timezone.utc).isoformat(), type=kind,
                         operation=self.operation, gameId=self.r.get("gameId"), stage=self.stage,
                         stageIndex=self.number, stageCount=self.count, state=state,
                         doneUnits=done, totalUnits=total, done=done, total=total,
                         elapsed=now-self.t0, message=message, milestone=getattr(self, "milestone", None), **extra)
            if kind == "stage":
                self.job.update(done=None, total=None, unit=None)
            elif kind == "progress":
                self.job.update(done=done, total=total, unit=extra.get("unit"))
            if kind == "plan":
                self.job["milestones"] = extra.get("milestones")
            elif kind == "space":
                # 空间预算单独存一份：后面的阶段消息只覆盖 message，不冲掉它
                self.job["space"] = extra.get("space")
            self.job["milestone"] = getattr(self, "milestone", None)
            self.job.update(stage=self.stage, stageIndex=self.number, stageCount=self.count,
                            elapsed=event["elapsed"], seq=self.seq)
            if message:
                self.job["message"] = message
            line = json.dumps(event, ensure_ascii=False)
            try:
                atomic(self.jobpath, self.job)
                with self.events.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
                    f.flush()
                    os.fsync(f.fileno())   # a killed worker must not take its own last stage line with it (J10 journal)
                os.chmod(self.events, 0o600)
            except OSError as error:
                # A full or failing disk used to let the job finish "successfully" with its record missing: whoever looks for it later finds
                # nothing, or finds it stuck at an old stage. Say so and fail the job instead (J10).
                self.journal_broken = True
                raise Failure(f"任务记录写不进磁盘（{error.strerror or error}）：这次的记录没有落盘，不当作完成。", code="journal_write_failed") from error
            # 启动器（读本进程标准输出的那一端）没了：作业照跑，事件只写记录（jobs/<任务>.json + .events.jsonl），重开的启动器 / s2armctl wait 按记录接上。
            #   10-26 Spare-host：内存吃紧启动器失联，作业不该跟着死。标准输出 / 错误改指 /dev/null：之后起的子进程不会因写断掉的管道被 SIGPIPE 杀掉
            if not self.frontend_gone:
                try:
                    print(line, flush=True)
                except OSError:   # BrokenPipeError 等
                    self.frontend_gone = True
                    try:
                        null = os.open(os.devnull, os.O_WRONLY); os.dup2(null, 1); os.dup2(null, 2); os.close(null)
                    except OSError:
                        pass

    def begin(self, stage, number=1, count=1):
        # Deliberately no "completed stages" list (维护方 10-25 口径): whether a stage is done has exactly one judge, its product key in the product
        # root (.stage_<阶段>, pack/refresh.sh). A second copy in the journal would disagree the moment a kill lands between committing a stage and
        # writing the journal. What this journal does keep — attempts / previousStage — is for display and diagnosis only and never decides a skip.
        self.stage, self.number, self.count = stage, number, count
        # How many times this stage was started: the number a resume reads to say "this stage was interrupted, not finished"
        self.job.setdefault("attempts", {})
        self.job["attempts"][stage] = int(self.job["attempts"].get(stage, 0)) + 1
        self.emit("stage", stage)

    def stage_progress(self, path, pos):
        """读进度文件 pos 之后新增的整行，发一次 progress；返回新的读到位置。
        出包时几个计数器交替写（刷新各阶段 7/10、直连库 2/6、编库 n/23502、通用翻译 n/45455…），只按最后一行发会来回跳（10-26 维护者：
        「进行中的环节能看到着色器总数和编译数量的实时变化最好」）：每个计数器各记「已完成 / 总数 / 起止时刻」，
        counters = 还没做完的逐项计数（总数 ≥ 20，同 tools/progress.sh 的阶段计数口径），各带每秒速度；done / total 取最近一次更新的逐项计数，没有就取最后一行"""
        try:
            with open(path, "rb") as f:
                f.seek(pos)
                chunk = f.read()
        except OSError:
            return pos
        end = chunk.rfind(b"\n")
        if end < 0:
            return pos
        seen = getattr(self, "_counters", None)
        if seen is None:
            seen = self._counters = {}
        last, fine = None, None
        for line in chunk[:end].decode(errors="replace").split("\n"):
            f = line.split("\t")
            if len(f) != 4 or not f[2].isdigit() or not f[3].isdigit() or int(f[3]) <= 0:
                continue
            try:
                t = float(f[0])
            except ValueError:
                continue
            name, done, total = f[1], int(f[2]), int(f[3])
            c = seen.get(name)
            if c is None or total != c["total"] or done < c["done"]:   # 新计数器 / 同名计数器重新开始：速度从这一行重算
                c = seen[name] = dict(name=name, total=total, t0=t, d0=done)
            c.update(done=done, t=t)
            last = c
            if total >= 20 or name.startswith("直连库·"):
                fine = c
        if last is not None:
            main = fine or last
            counters = []
            for c in seen.values():
                if (c["total"] < 20 and not c["name"].startswith("直连库·")) or c["done"] >= c["total"]:
                    continue
                rate = (c["done"] - c["d0"]) / (c["t"] - c["t0"]) if c["t"] > c["t0"] and c["done"] > c["d0"] else None
                counters.append(dict(name=c["name"], done=c["done"], total=c["total"], rate=round(rate, 3) if rate else None))
            self.emit(message=f"{main['name']} {main['done']}/{main['total']}", done=main["done"], total=main["total"], unit="项", counters=counters)
        return pos + end + 1

    def progress(self, done, total):
        self.check()
        self.emit(done=done, total=total, unit="B")

    def redact(self, value, *, limit=3000):
        # 只留一份脱敏规则（维护者 10-23）：形状规则 + 按值匹配都在 tools/redact.py（与诊断报告共用，PR #64）。
        # 这里先把游戏库 / 私有目录 / 引擎 / 主目录换成标签（保留可读的相对位置），再交给共用规则；按值匹配用玩家导入的 prod.keys
        text = str(value)
        for p, label in ((self.library, "[LIBRARY]"), (self.private, "[PRIVATE]"), (self.engine, "[SOURCE]"), (Path.home(), "~")):
            text = text.replace(str(p), label)
        text = shared_redact().redact_text(text, key_values=self.key_values())
        return text if limit is None else text[-limit:]

    def key_values(self):
        if not hasattr(self, "_key_values"):
            self._key_values = shared_redact().load_key_values([self.keys] if getattr(self, "keys", None) else [])
        return self._key_values

    def describe_failure(self, error, severity="error", exit_code=None):
        failure = explain(error)
        code = failure.code
        result = dict(code=code, severity=severity, summary=self.redact(failure),
                      suggestion=recovery(code, packaged=bool(self.r.get("engineBundle"))), detail="", stage=self.stage,
                      component="S2Arm", logPath=str(self.last_log) if self.last_log else None)
        if exit_code is not None:
            result["exitCode"] = exit_code
        if self.last_log and self.last_log.is_file():
            with self.last_log.open("rb") as f:
                f.seek(max(0, self.last_log.stat().st_size - 16000))
                result["detail"] = self.redact(f.read().decode(errors="replace"))
        # 内部原文（阶段 / rc / 构建号 / 红行 / 归因依据）不上屏，放 detail 开头随「复制错误摘要」带走
        internal = getattr(failure, "internal", "")
        if internal:
            result["detail"] = self.redact(internal) + ("\n\n" + result["detail"] if result["detail"] else "")
        # 失败事实随错误摘要带走（玩家 #5 #8：摘要里只有「工具退出」，看不出阶段、退出码和原文）
        rc = exit_code if exit_code is not None else (self.last_rc or None if hasattr(self, "last_rc") else None)
        result["failureSummary"] = failure_summary(self.stage, rc, [self.redact(line, limit=None) for line in last_output_lines(self.last_log)])
        return result

    def diagnostic(self, message, code="job_failed", severity="error", exit_code=None, internal=""):
        result = self.describe_failure(Failure(message, code=code, internal=internal), severity, exit_code)
        self.job["diagnostics"].append(result)
        self.emit("diagnostic", result["summary"], diagnostic=result, error=result,
                  diagnostics=self.job["diagnostics"], logRef=result["logPath"])
        return result

    def report_failure(self, error):
        failure = explain(error)
        if self.last_log is None:
            self.last_log = self.private / "logs" / (self.task + "-failure.log")
            self.last_log.parent.mkdir(parents=True, exist_ok=True)
            self.last_log.write_text(self.redact(failure) + "\n" + (self.redact(failure.internal) + "\n" if getattr(failure, "internal", "") else ""))
            self.last_log.chmod(0o600)
        records = self.job["diagnostics"]
        previous = records[-1] if records else None
        if (previous and previous["severity"] == "error" and previous["code"] == failure.code
                and previous["summary"] == self.redact(failure)):
            if previous["logPath"] == (str(self.last_log) if self.last_log else None):
                return previous
            records.pop()
        # A caller may know more than the subprocess runner (e.g. inspect's
        # documented unreadable-container result). Replace only this stage's
        # generic tool error and preserve its evidence and real exit code.
        exit_code = None
        if (previous and previous["code"] == "tool_exit" and failure.code != "job_failed"
                and previous["stage"] == self.stage and previous["logPath"] == str(self.last_log)):
            exit_code = records.pop().get("exitCode")
        return self.diagnostic(str(failure), failure.code, exit_code=exit_code, internal=getattr(failure, "internal", ""))

    def fonts_dir(self):
        # 共享字体：玩家在设置里选了原版字体目录就用它，否则用应用自带的开源替代字体（Resources/Fonts，build_app.py 打进去；维护者 10-23：不再作为检测项）
        for raw in (self.r.get("fontsDir"), str(Path(self.r.get("resources") or self.private) / "Fonts")):
            if raw:
                d = Path(raw).expanduser().resolve()
                if d.is_dir() and any(d.glob("*.bfttf")):
                    return d
        return None

    @property
    def work_root(self):
        """出包缓存和临时运行目录的根：游戏库放在别处（比如外接盘）时跟着游戏库走，不占系统盘。"""
        return self.private if self.library.is_relative_to(self.private) else self.library / ".s2arm-work"

    def env(self):
        env = dict(os.environ)
        for k in list(env):
            if k.startswith(("S2A_", "NXGAL_", "DM_", "DMSH", "GAL_", "PSO_", "PYTHON")):
                env.pop(k)
        cache = self.work_root / "cache"
        cache.mkdir(parents=True, exist_ok=True)
        env.pop("DEVELOPER_DIR", None)
        developer = self.r.get("developerDir") or self.default_developer   # 启动器传的 > 装着的最新 Xcode；都没有就不设
        if developer:
            env.update(DEVELOPER_DIR=developer, S2A_DEVELOPER_DIR=developer)
        env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", PYTHONUNBUFFERED="1", LC_ALL="C",
                   DOTNET_CLI_HOME=str(cache / "dotnet-home"), NUGET_PACKAGES=str(cache / "nuget"),
                   DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1", S2A_PSO_REMOTE="off", S2A_DL_REMOTE="off",
                   DOTNET_GENERATE_ASPNET_CERTIFICATE="false", DOTNET_SKIP_FIRST_TIME_EXPERIENCE="1",
                   S2A_CS_CACHE=str(cache / "nxaot"), S2A_VPX_CACHE=str(cache / "vpx"),
                   S2A_OPUS_CACHE=str(cache / "opus"), S2A_SHX_CACHE=str(cache / "shx"),
                   S2A_SHCOLLECT=str(cache / "shcollect"), GAL_AIR_CACHE=str(cache / "air"),
                   S2A_CSCLOSURE_CACHE=str(cache / "csclosure"),
                   S2A_XCKEY_CACHE=str(cache / "xckey"), S2A_SCHED_DIR=str(cache / "local-observations"),
                   S2A_PROGRESS=str(self.private / "logs" / (self.task + ".progress.tsv")),
                   GAL_PSO_CACHE=str(cache / "pso"), S2A_ROOTS_NO_BOOT="1",
                   S2A_PACK_PLAT="macos", S2A_TARGET="macos", S2A_SEED_AOC="1", J=str(min(os.cpu_count() or 4, 8)),
                   # 出包用途 = 用户包（10-29 维护者定口径，pack/jobs/archs.py）：管线归档 / 函数体只出这个包要装的设备那几对——
                   #   Mac 包 = 出包的这台 Mac；iPhone 包由 ipa.py 按目标设备传 S2A_PSO_ARCHS / S2A_PSO_OS，没传就报红、不猜（不按我方跑局机清单出）
                   S2A_PACK_PURPOSE="user",
                   # 出包链里其余缺省落在 ~/.s2arm/run 的缓存 / 锁 / 编好的小工具（直连缓存 6.5 GB、翻译缓存 5.3 GB…，10-26 Spare-host 斯普拉遁3）一律改到私有目录：
                   #   玩家机上不出现 ~/.s2arm/run，清数据目录 / 删游戏能连它们一起删（守卫 tests/userpath_cache_test.py）
                   S2A_RUN_ROOT=str(self.work_root / "run"),
                   # 玩家作业不认我方开发机的规矩（10-26 侍魂晓在启动器里被 130 G 磁盘闸 + 独占局让路白卡 43 分钟）：不读 tools/machine.local
                   #   （记账、130 G 总量闸、签名团队、gate-host / 远端派活都是我方共享机器的设定），按用户机器出包（守卫 tests/userpath_cache_test.py ④）
                   S2A_MACHINE_NO_LOCAL="1", S2A_CPU_LEDGER="0",
                   # 玩家导入的客体着色器缓存（启动器社区资源面板拷进 <数据目录>/着色器缓存/<程序号>/，出包时自动并入；着色器线 docs/客体着色器缓存导入.md）
                   S2A_USER_SHCACHE=str(self.private / "着色器缓存"))
        # Git 只许在引擎目录里找仓库（出包各阶段用 git -C 引擎 取源码对象号）：不许往上认到外层仓库（10-23 走查：认到用户主目录的仓库）
        env["GIT_CEILING_DIRECTORIES"] = str(Path(self.engine).resolve().parent)
        env["S2A_PYTHON"] = os.sys.executable
        # 随启动器发版的补丁库（补丁包部署方案 v2，满屏补丁线 11-04；launchers/macos/build_app.py 只收真机验过的）：出包时交 pack/stage_rewrite.sh，
        #   验过的整份拷进产物根 patch.tsv、随资源包下发。起局不给（build.runtime_env 去掉）：宿主会往补丁根写 .applied，App 包内只读、写了破签名
        patches = Path(self.r.get("resources") or self.private) / "Patches"
        if (patches / "INDEX.tsv").is_file():
            env["S2A_PATCH_DIR"] = str(patches)
        runtime = Path(self.r.get("resources") or self.private) / "Runtime"
        if os.name == "nt" and (runtime / "s2arm_env.cmd").is_file():   # Windows 随包运行时（launchers/windows/bundle_runtime_win.py）
            env.update(win_runtime_env(runtime, self.engine, env.get("SystemRoot") or r"C:\Windows"))
            return env
        # 3 和 4 都是「作业 PATH 只有 Runtime + 系统目录」的便携运行环境。4 起不再带 oead / GNU timeout；
        # 旧 schema 3 仍走这条 PATH（构建侧 build_app 会因 schema / runtimeDeps 不符而重打，不在这里改 PATH）。
        if read(runtime / "versions.json", {}).get("runtimeSchema") in (3, 4):
            env.update(DOTNET_ROOT=str(runtime / "dotnet"), DOTNET_MULTILEVEL_LOOKUP="0",
                       S2A_PREBUILT_TOOLS=str(self.engine / "Binaries/Prepare"))
            paths = [str(runtime / "python/bin"), str(runtime / "bin"), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
            # App 自带的 C 改写器（pack/rewriter.py 核源码指纹，与引擎不符时不用）：玩家机器零安装
            env["S2A_REWRITER_BUNDLED"] = str(runtime / "bin/s2a-rewrite")
            # 发版 App 自带已编好的 Mac 宿主（含 C#，S2A_CS=1）：RomFS 解码器或运行时 GLSLC 时不再现编（issue #8 根因：Runtime-only dotnet 编不出）
            from embedded import artifact
            try:
                bundled_host = artifact(self, "S2ArmGame.app")
                if bundled_host and (bundled_host / "Contents/MacOS/s2arm-host").is_file() and (bundled_host / "Contents/MacOS/libs2a_vaguard.dylib").is_file():
                    env["S2A_BUNDLED_HOST"] = str(bundled_host / "Contents/MacOS/s2arm-host")
                    env["DM_GLSLC_HOST"] = str(bundled_host / "Contents/MacOS/s2arm-host")
                    env["DM_OODLE_HOST"] = str(bundled_host / "Contents/MacOS/s2arm-host")  # LZ5 / OodleLZ 解码器（uepak.oodle_host_find，缺省回退 build/s2arm-host 在发版包里不存在）
            except Exception:
                pass   # 源码树开发构建：没有内置宿主
        else:
            paths = [str(Path(os.sys.executable).parent), str(runtime / "bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin", env.get("PATH", "")]
        env["PATH"] = os.pathsep.join(dict.fromkeys(paths))
        return env

    def reap(self, process, out):
        try:
            left = reap_session(process.pid)
        except Exception as error:   # 收尾动作：自己出错不改变任务结果，记一行
            out.write(f"[s2a] 【黄】收本阶段工具会话的残留进程时出错：{self.redact(error)}\n")
            return
        if left:
            out.write(f"[s2a] 收掉本阶段工具会话里的残留进程 {len(left)} 个：{'、'.join(left[:8])}\n")
            out.flush()

    def signal_child(self, process, sig):
        if os.name == "nt":
            return winproc.signal_child(process)
        if process.poll() is not None:
            return
        # The repository's only process-group signal entry point validates both
        # the group and its live leader before sending a signal.
        helper = self.engine / "tools/safe_killpg.sh"
        if not helper.is_file():
            process.send_signal(getattr(signal, "SIG" + sig))
            return
        # Popen still owns this unreaped, live child, so its PID cannot have
        # been reused. macOS may replace argv[0] (notably framework Python),
        # and ps truncates long commands by default. Match the live leader's
        # prefix, rather than guessing a basename from the original argv.
        command = subprocess.run(["/bin/ps", "-ww", "-o", "command=", "-p", str(process.pid)],
                                 capture_output=True, text=True, timeout=5, check=False)
        marker = command.stdout.strip()[:64]
        if not marker:
            if process.poll() is not None:
                return
            raise Failure("无法确认本任务的子进程组，未发送信号。")
        result = subprocess.run(["/bin/bash", "-c", 'source "$1"; safe_killpg "$2" "$3" "$4"',
                                 "s2arm-cancel", str(helper), sig, str(process.pid), marker],
                                capture_output=True, text=True, timeout=5, check=False)
        if result.returncode and process.poll() is None:
            raise Failure("无法停止本任务进程组：" + self.redact(result.stderr))

    def stop(self):
        self.cancelled = True

    def run(self, args, log=None, env=None, cwd=None, on_line=None, timeout=None, ok_codes=(0,), until=None):
        # ok_codes：哪些退出码算这一步成功（如 refresh.sh 3 = 直连没出成、只出通用）；实际退出码留在 self.last_rc
        # until：可选，返回真表示「产物已经就绪」。这类工具（xcodebuild -downloadPlatform）把活干完后常在收尾步挂住，
        #   等它自己退出会让作业一直挂着；就绪即收（TERM 进程组），退出码不算数，判据是 until 的读数。最多每 2 秒问一次。
        self.check()
        args = list(map(str, args))
        self.last_log = Path(log or self.private / "logs" / (self.task + ".log"))
        self.last_log.parent.mkdir(parents=True, exist_ok=True)
        # Stage timeout (J10): a tool that hangs would otherwise hold the job (and the frontend's progress bar) until the user gives up. The
        # default comes from the request so the launcher and the CLI agree; 0 or absent means no timer, as before.
        limit = timeout if timeout is not None else self.r.get("stageTimeoutSeconds")
        deadline = time.monotonic() + float(limit) if limit else None
        run_env = env or self.env()
        with self.last_log.open("a", encoding="utf-8") as out:
            if run_env.get("S2A_DEVELOPER_DIR"):
                out.write("S2Arm frontend toolchain: DEVELOPER_DIR=" + run_env["DEVELOPER_DIR"] + "\n")
                out.flush()
            try:
                spawn = winproc.popen if os.name == "nt" else subprocess.Popen
                p = spawn(args, cwd=cwd or self.engine, env=run_env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as error:
                out.write(self.redact(error) + "\n")
                if not Path(cwd or self.engine).is_dir():
                    raise FileNotFoundError(errno.ENOENT, "working directory does not exist", str(cwd or self.engine)) from error
                if error.errno == errno.ENOENT and Path(cwd or self.engine).is_dir():
                    raise Failure("任务所需的工具无法启动：" + args[0], code="tool_missing") from error
                raise
            self.children.add(p)
            try:
                # Drain in a reader thread so cancellation is responsive even
                # when a compiler or the running game produces no output.
                reader_errors = []
                def drain():
                    try:
                        for raw in output_lines(p.stdout):
                            line = raw.decode(errors="replace")
                            out.write(line + "\n")
                            out.flush()
                            if on_line and line.strip():
                                on_line(line.rstrip())
                    except Exception as error:
                        reader_errors.append(error)
                reader = threading.Thread(target=drain, daemon=True)
                reader.start()
                # 阶段内进度（10-25 维护者「能看到进度么」）：阶段往 S2A_PROGRESS 追加「时刻 阶段 已完成 总数」（tools/progress.sh / progress.py），
                #   这里每秒读一次新增的行、发 progress 事件，前端阶段条按完成比例填、按本步实际速度外推剩余时间
                pf = run_env.get("S2A_PROGRESS")
                ppos = os.path.getsize(pf) if pf and os.path.exists(pf) else 0
                pnext = 0.0
                until_at = 0.0
                satisfied = False
                while p.poll() is None:
                    if pf and time.monotonic() >= pnext:
                        pnext = time.monotonic() + 1
                        ppos = self.stage_progress(pf, ppos)
                    if reader_errors:
                        raise reader_errors[0]
                    self.check()
                    if until and time.monotonic() >= until_at:
                        until_at = time.monotonic() + 2
                        try:
                            satisfied = bool(until())
                        except Exception:
                            satisfied = False   # 判据自己出错不影响工具运行，只当还没就绪
                        if satisfied:
                            out.write("[s2a] 产物已就绪，提前结束本阶段工具。\n")
                            out.flush()
                            break
                    if deadline and time.monotonic() > deadline:
                        # Say which stage ran out of time before the group is torn down, so the record and the frontend agree
                        message = f"阶段「{self.stage or '当前阶段'}」超过 {int(limit)} 秒未完成，已停止该阶段的进程组。"
                        self.diagnostic(message, "stage_timeout")
                        raise Failure(message, code="stage_timeout")
                    time.sleep(.1)
                if satisfied:
                    # 产物已就绪，工具只是收尾挂住：收掉进程组，退出码不作判据（调用方已用 until 的读数确认）
                    self.signal_child(p, "TERM")
                    try:
                        p.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.signal_child(p, "KILL")
                        p.wait(timeout=5)
                    self.reap(p, out)
                    reader.join(timeout=5)
                    self.check()
                    self.last_rc = p.returncode
                    return self.last_log
                # 工具退出了：先收它会话里的残留进程（还占着输出管道的也在内，否则读线程等不到结束、整个任务判「输出未关闭」），再等读线程
                self.reap(p, out)
                reader.join(timeout=5)
                if reader.is_alive():
                    raise Failure("子进程输出未关闭，任务未确认完成。")
                if reader_errors:
                    raise reader_errors[0]
                self.check()
                self.last_rc = p.returncode
                if p.returncode not in ok_codes:
                    if self.operation == "build":
                        from errors import build_tool_failure
                        failure = build_tool_failure(args, getattr(self, "milestone", None), self.last_log)
                        self.diagnostic(str(failure), failure.code, exit_code=p.returncode, internal=failure.internal)
                        raise failure
                    from errors import tool_reason
                    message = f"{self.stage or '这一步'}失败（退出码 {p.returncode}）：{self.redact(tool_reason(self.last_log), limit=240)}"
                    self.diagnostic(message, "tool_exit", exit_code=p.returncode)
                    raise Failure(message, code="tool_exit")
            finally:
                if p.poll() is None:
                    self.signal_child(p, "TERM")
                    try:
                        p.wait(timeout=4)
                    except subprocess.TimeoutExpired:
                        self.signal_child(p, "KILL")
                        p.wait(timeout=5)
                self.reap(p, out)   # 异常 / 取消 / 超时路径（正常路径上面已收过，这里收不到东西）
                p.stdout.close()
                self.children.discard(p)
        return self.last_log

    def finish(self, state, message, result=None):
        self.cancelled = False  # Allow the terminal event after cancellation.
        self.job.update(state=state, ended=time.time(), message=message)
        if (result or {}).get("outputPath"):
            self.job["outputPath"] = result["outputPath"]
        # 启动器重开后从任务记录恢复完成卡：IPA 路径 / 装到的设备 / 推了几个文件原先只在结果事件里，任务记录不存，
        #   重开后完成卡读不到包（画面模式「未知」、IPA / 资源包两行没了，11-01 测试开发者截图）。字段名同启动器 TaskRecord
        for src, dst in (("ipa", "ipaPath"), ("device", "device"), ("packFiles", "packFiles")):
            if (result or {}).get(src) not in (None, ""):
                self.job[dst] = result[src]
        try:
            self.emit("result", message, state="succeeded" if state == "completed" else state,
                      result=result or {}, artifactRef=(result or {}).get("outputPath"), diagnostics=self.job["diagnostics"])
            atomic(self.jobpath, self.job)
        except (Failure, OSError) as error:
            # The job is over either way; what must not happen is the caller seeing success while nothing was recorded. main.py turns
            # journal_broken into a non-zero exit, and this line is the only place a user can read why (J10).
            self.journal_broken = True
            self.job["journalBroken"] = str(error)[:300]
            print(f"[s2a] 【红】任务记录写不进磁盘，这次不当作完成：{error}", file=sys.stderr, flush=True)


# ---- Journal readers and the job control plane (J10 / #186) ---------------------------------------------------------------------------------
# Everything below only reads the journal the worker already writes, or signals a worker it recorded. The Mac launcher and the CLI reach a job the
# same way: a task id plus the private root. No daemon, no second state store, and a job that never wrote a terminal event is reported as such
# instead of being mistaken for a finished one.
TERMINAL_STATES = ("completed", "failed", "cancelled", "succeeded")


def request_path(private, task):
    """The request a task was started with, kept beside its journal so a killed attempt can be run again."""
    job, _ = job_files(private, task)
    return job.with_suffix(".request.json")


def job_files(private, task):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(task)):
        raise Failure("无效的任务标识。", code="job_unknown")
    jobs = Path(private).expanduser().resolve() / "jobs"
    return jobs / (task + ".json"), jobs / (task + ".events.jsonl")


def journal(private, task):
    path, _ = job_files(private, task)
    record = read(path)
    if not isinstance(record, dict):
        raise Failure("这条任务的记录不存在。", code="job_unknown")
    return record


def read_events(private, task, since=0, limit=None):
    _, path = job_files(private, task)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []
    out = []
    for number, line in enumerate(lines):
        if number < since or not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue   # a line cut short by a kill is not an event; the journal still has the last whole one
        if limit and len(out) >= limit:
            break
    return out


def pid_start(pid):
    """Process start time as ps prints it: a reused pid has a different one, so a journal cannot signal a stranger."""
    if os.name == "nt":
        return winproc.pid_start(pid)
    try:
        result = subprocess.run(["/bin/ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True, timeout=5, check=False)
    except OSError:
        # 沙箱/受限运行时可能连 /bin/ps 都不许 exec：退化为空串，辨陌生人检查自动跳过（构建期 bundle-engine 就在这类环境里）。
        return ""
    return result.stdout.strip()


WIN_RUNTIME_PATH = ["python", r"python\Scripts", "dotnet", r"mingit\cmd", r"git\usr\bin", "zstd"]   # 同 bundle_runtime_win.env_cmd 的 PATH（tests 核两处一致）


def win_runtime_env(runtime, engine, system_root):
    """Windows 出包作业的环境（同随包 s2arm_env.cmd）：只用随包 Python / .NET 运行时 / bash / MinGit / zstd + 系统目录，预编件在引擎 Binaries/Prepare（同 Mac）"""
    import ntpath
    rt = str(runtime)
    paths = [ntpath.join(rt, d) for d in WIN_RUNTIME_PATH] + [ntpath.join(system_root, "system32"), system_root,
                                                               ntpath.join(system_root, r"System32\WindowsPowerShell\v1.0")]
    return dict(PATH=";".join(paths), DOTNET_ROOT=ntpath.join(rt, "dotnet"), DOTNET_MULTILEVEL_LOOKUP="0",
                S2A_BASH=ntpath.join(rt, r"git\usr\bin\bash.exe"), PYTHONHOME=ntpath.join(rt, "python"), PYTHONUTF8="1",
                S2A_PREBUILT_TOOLS=ntpath.join(str(engine), r"Binaries\Prepare"),
                PYTHONPATH=ntpath.join(str(engine), r"pack\orch\site"))   # 子 Python 进程的长路径处理（pack/orch/winlong.py）


def pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        return winproc.alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # someone else's process: alive, and cancel refuses it later
    return True


def describe(private, task):
    """What a restart may believe about this task: state, last stage, attempts, and whether the worker is still there."""
    record = journal(private, task)
    pid = record.get("pid")
    running = pid_alive(pid)
    terminal = record.get("state") in TERMINAL_STATES or bool(record.get("ended"))
    events = read_events(private, task)
    return dict(job=record, state=record.get("state"), operation=record.get("operation"), stage=record.get("stage") or "",
                attempts=record.get("attempts") or {},
                attemptNo=record.get("attemptNo") or 1, pid=pid, running=running,
                terminal=terminal, interrupted=(not terminal and not running), events=len(events),
                lastEvent=events[-1] if events else None, canResume=not terminal)


def prune_jobs(private, keep_days=14, keep_count=50, dry_run=False):
    """Keep the job store bounded (J10): finished tasks older than `keep_days` go, but the newest `keep_count` finished tasks always stay, and a
    task that has not written a terminal state is never touched (its worker may still be writing). Only the three files this module owns per task
    are removed; anything else in `jobs/` is left alone."""
    jobs = Path(private).expanduser().resolve() / "jobs"
    if not jobs.is_dir():
        return dict(removed=[], kept=0, skippedRunning=0, bytes=0)
    by_task = {}
    for path in jobs.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix not in (".json", ".jsonl"):
            continue
        name = path.name
        task = name[:-len(".request.json")] if name.endswith(".request.json") else name[:-len(".events.jsonl")] if name.endswith(".events.jsonl") else name[:-len(".json")]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task):
            continue
        by_task.setdefault(task, []).append(path)
    cutoff = time.time() - float(keep_days) * 86400 if keep_days else None

    def finished(task):
        record = read(jobs / (task + ".json")) or {}
        return record.get("state") in TERMINAL_STATES or bool(record.get("ended"))

    running = [t for t in by_task if not finished(t)]   # counted before anything is deleted: a removed record must not look like a live one
    finished_tasks = sorted((t for t in by_task if finished(t)), key=lambda t: max(p.stat().st_mtime for p in by_task[t]), reverse=True)
    keep = set(finished_tasks[:max(0, int(keep_count or 0))])
    removed, freed = [], 0
    for task in finished_tasks:
        if task in keep:
            continue
        files = by_task[task]
        newest = max(p.stat().st_mtime for p in files)
        if cutoff is not None and newest >= cutoff:
            continue
        size = sum(p.stat().st_size for p in files)
        if not dry_run:
            for p in files:
                p.unlink(missing_ok=True)
        removed.append(task)
        freed += size
    return dict(removed=removed, kept=len(keep) + len(running), skippedRunning=len(running), bytes=freed)


def status(private):
    """One look at the whole job store: how many are running vs finished, how big it is, and which task last moved."""
    jobs = Path(private).expanduser().resolve() / "jobs"
    result = dict(running=0, finished=0, interrupted=0, bytes=0, newest=None, oldest=None)
    if not jobs.is_dir():
        return result
    newest = oldest = None
    for path in jobs.iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        result["bytes"] += path.stat().st_size
        stamp = path.stat().st_mtime
        if newest is None or stamp > newest[0]:
            newest = (stamp, path.name)
        if oldest is None or stamp < oldest[0]:
            oldest = (stamp, path.name)
    for record_path in jobs.glob("*.json"):
        if record_path.name.endswith(".request.json") or record_path.is_symlink():
            continue
        record = read(record_path) or {}
        if not isinstance(record, dict) or record.get("id") != record_path.stem:
            continue
        if record.get("state") in TERMINAL_STATES or record.get("ended"):
            result["finished"] += 1
        elif pid_alive(record.get("pid")):
            result["running"] += 1
        else:
            result["interrupted"] += 1
    if newest:
        result["newest"] = newest[1]
    if oldest:
        result["oldest"] = oldest[1]
    return result


def reap_session(sid, grace=2.0):
    """收掉会话 sid（本任务工具的会话：run() 用 start_new_session 起，会话号 = 工具 pid）里还活着的进程，返回收掉的进程名。
    工具退出后组长已不在，孙进程照跑：10-26 斯普拉遁3 Spare-host 出包，dl_gen 超时只杀了 metal-tt，air-tt / applegpu-nt 过继给 launchd 占 2 核 16 分钟，
    出包失败、启动器退出后还在编。有意活得比任务久的后台（宽屏自动出表）自立会话，不在这里面。会话还有成员时 sid 不会被复用，逐个按 pid 发信号、发前再核会话号"""
    if os.name == "nt":
        return winproc.reap_session(sid)
    def members():
        out = subprocess.run(["/bin/ps", "-A", "-o", "pid=,comm="], capture_output=True, text=True, timeout=10, check=False).stdout
        found = []
        for row in out.splitlines():
            pid, _, comm = row.strip().partition(" ")
            if not pid.isdigit() or int(pid) == os.getpid():
                continue
            try:
                if os.getsid(int(pid)) == sid:
                    found.append((int(pid), os.path.basename(comm.strip())))
            except OSError:
                pass
        return found
    def send(sig):
        for pid, _ in members():
            with contextlib.suppress(OSError):
                if os.getsid(pid) == sid:
                    os.kill(pid, sig)
    first = members()
    if not first:
        return []
    send(signal.SIGTERM)
    end = time.monotonic() + grace
    while members() and time.monotonic() < end:
        time.sleep(.1)
    send(signal.SIGKILL)
    return [name for _, name in first]


def cancel(private, task, sig="TERM"):
    """Signal the worker the journal recorded, after checking that the pid is still that worker (a reused pid is refused, not killed)."""
    record = journal(private, task)
    if record.get("state") in TERMINAL_STATES or record.get("ended"):
        return dict(cancelled=False, reason="任务已结束，无需取消。", state=record.get("state"))
    pid = record.get("pid")
    if os.name == "nt":
        try:
            winproc.cancel_verified(pid, record.get("pidStart"), sig)
        except ProcessLookupError:
            return dict(cancelled=False, reason="任务进程已不在，记录停在未收尾状态。", state=record.get("state"), interrupted=True)
        return dict(cancelled=True, pid=int(pid), signal=sig)
    if not pid_alive(pid):
        return dict(cancelled=False, reason="任务进程已不在，记录停在未收尾状态。", state=record.get("state"), interrupted=True)
    started = record.get("pidStart")
    if started:
        if pid_start(pid) != started:
            raise Failure("记录的进程号已不是这个任务，未发送信号。", code="job_pid_reused")
    else:
        command = subprocess.run(["/bin/ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5, check=False)
        if "jobs/main.py" not in command.stdout:
            raise Failure("记录的进程号已不是这个任务，未发送信号。", code="job_pid_reused")
    try:
        os.kill(int(pid), getattr(signal, "SIG" + sig))
    except PermissionError:
        raise Failure("这个进程不属于当前用户，未发送信号。", code="job_not_ours") from None
    except ProcessLookupError:
        return dict(cancelled=False, reason="任务进程已不在，记录停在未收尾状态。", state=record.get("state"), interrupted=True)
    return dict(cancelled=True, pid=int(pid), signal=sig)
