import hashlib, secrets, sqlite3, threading
from datetime import date, datetime
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
DB_PATH = DATA / "app.db"
FRONTEND = ROOT / "frontend"
SESSIONS: dict[str, str] = {}

PRODUCT = "日切放行"
SKELETON = "业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账"

# ---------------------------------------------------------------------------
# 关日门闩说明（口径唯一，选定为甲，当天不许更换）
#
#   甲门闩：关日后该业务日一切放行与水位推进硬拒绝，仅允许只读查询。
#   乙门闩：关日后仅允许进入迟到冻结区登记，正式放行与水位仍硬拒绝。
#
# 本仓选甲，落库 day_closes.mode='甲'，服务端逐条写入路径硬裁定。
# 乙口径的“迟到冻结区登记”写入口仍然存在，但在甲口径下必须拒绝并记
# “关日口径偏了”，以此保证两边不混卯关。
# 禁止关日标记只写页面本地；禁止关日后改已放行成功。
# ---------------------------------------------------------------------------
GATE_MODE = "甲"
GATE_TITLE = "关日门闩说明"
GATE_SOURCE = "docs/关日门闩说明.md"
GATE_BODY = (
    "选定甲门闩（唯一，当天不许更换）：关日登记成功即生效。"
    "关日后该业务日一切放行与水位推进硬拒绝，仅允许只读查询。"
    "甲口径不设迟到冻结区，迟到冻结登记属乙口径，混用一律拒绝并记“关日口径偏了”。"
    "关日标记只认服务端库内记录，不接受页面本地标记。"
    "禁止关日后修改已放行成功记录。"
    "任何偏离本说明的写入一律拒绝并记入关日审计。"
)
GATE_STATE = {"mode": GATE_MODE, "locked": True, "title": GATE_TITLE,
              "body": GATE_BODY, "source": GATE_SOURCE}

_DAY_LOCKS: dict[str, threading.Lock] = {}
_DAY_LOCKS_GUARD = threading.Lock()


def connect(busy_ms: int = 10000):
    DATA.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=busy_ms / 1000)
    conn.row_factory = sqlite3.Row
    # 自己控制事务边界，保证关日门闩裁决与写入在同一事务里完成。
    conn.isolation_level = None
    conn.execute(f"PRAGMA busy_timeout={int(busy_ms)}")
    return conn


def sha(p: str) -> str:
    return hashlib.sha256(p.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def norm_date(value: str) -> str:
    value = (value or "").strip()
    if len(value) != 10:
        raise HTTPException(422, "业务日格式须为 YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, "业务日不是有效日期")
    return value


def day_lock(biz_date: str) -> threading.Lock:
    with _DAY_LOCKS_GUARD:
        return _DAY_LOCKS.setdefault(biz_date, threading.Lock())


def ensure_users(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS users(
        username TEXT PRIMARY KEY, password_sha TEXT NOT NULL, role TEXT NOT NULL)""")
    if conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] == 0:
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("admin", sha("123456"), "管理员"))
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("viewer", sha("view123"), "只读"))


class LoginIn(BaseModel):
    username: str
    password: str


def require_user(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "未登录")
    u = SESSIONS.get(authorization.removeprefix("Bearer ").strip())
    if not u:
        raise HTTPException(401, "登录已失效")
    return u


# ------------------------------ 审计 ------------------------------

def _insert_audit(conn, biz_date, action, actor, result, note):
    conn.execute(
        "INSERT INTO gate_audit(biz_date,action,actor,result,note,created_at) VALUES (?,?,?,?,?,?)",
        (biz_date, action, actor, result, note, now_iso()),
    )


def audit_later(biz_date, action, actor, result, note):
    """独立事务落审计：拒绝与争用也必须留痕，写不进去也不许默认成功。"""
    try:
        conn = connect(busy_ms=1500)
        try:
            conn.execute("BEGIN IMMEDIATE")
            _insert_audit(conn, biz_date, action, actor, result, note)
            conn.execute("COMMIT")
        finally:
            conn.close()
    except sqlite3.Error:
        pass


# ------------------------------ 门闩核心 ------------------------------

def _closed(conn, biz_date: str):
    return conn.execute("SELECT * FROM day_closes WHERE biz_date=?", (biz_date,)).fetchone()


def acquire_day(biz_date: str, action: str, actor: str) -> threading.Lock:
    """同业务日关日与放行排队互斥；后手记关日争用，禁止默认成功。"""
    lock = day_lock(biz_date)
    if not lock.acquire(blocking=False):
        audit_later(biz_date, action, actor, "拒绝", "关日争用")
        raise HTTPException(409, "关日争用：同一业务日已有另一值班在操作，请排队后重试")
    return lock


def begin_write(conn, biz_date, action, actor):
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError:
        audit_later(biz_date, action, actor, "拒绝", "关日争用")
        raise HTTPException(409, "关日争用：库写锁被另一值班占用，请排队后重试")


class RejectAndCommit(Exception):
    """业务拒绝但拒绝流水须留库（如完整度未达标的放行拒收）。"""

    def __init__(self, detail: str):
        self.detail = detail


def run_guarded(biz_date, action, actor, fn, gate_message=None, drift=False):
    """统一写入口：排队互斥 → 写锁 → 关日裁定 → 业务写入 → 提交/回滚。

    gate_message 为 None 表示该动作不做关日裁定（仅关日登记自身使用）。
    drift=True 表示该动作本身即偏离关日门闩说明，须记“关日口径偏了”。
    """
    lock = acquire_day(biz_date, action, actor)
    conn = connect(busy_ms=3000)
    try:
        begin_write(conn, biz_date, action, actor)
        if gate_message is not None:
            state = _closed(conn, biz_date)
            if state:
                if drift:
                    note = f"关日口径偏了：{gate_message}（业务日 {biz_date} 已关日）"
                else:
                    note = (f"关日门闩：{biz_date} 已关日（口径{state['mode']}），"
                            f"{gate_message}")
                _insert_audit(conn, biz_date, action, actor, "拒绝", note)
                conn.execute("COMMIT")
                if drift:
                    raise HTTPException(409, note + "，已拒绝，仅允许只读查询")
                raise HTTPException(
                    409,
                    f"关日门闩：业务日 {biz_date} 已关日（口径{state['mode']}），"
                    f"{gate_message}，仅允许只读查询")
        result = fn(conn)
        conn.execute("COMMIT")
        return result
    except RejectAndCommit as exc:
        conn.execute("COMMIT")
        raise HTTPException(409, exc.detail)
    except HTTPException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        audit_later(biz_date, action, actor, "拒绝", f"库写入失败：{exc}")
        raise HTTPException(409, f"关日争用：库写入未完成，请排队后重试（{exc}）")
    finally:
        conn.close()
        lock.release()


# ------------------------------ 应用 ------------------------------

app = FastAPI(title=PRODUCT)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])


def init_db():
    conn = connect()
    try:
        ensure_users(conn)
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS slices(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_no TEXT UNIQUE NOT NULL,
          biz_date TEXT NOT NULL,
          partition_key TEXT NOT NULL,
          row_count INTEGER NOT NULL,
          expected_count INTEGER NOT NULL,
          completeness TEXT NOT NULL,
          release_status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS releases(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          slice_id INTEGER NOT NULL,
          consumer TEXT NOT NULL,
          status TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS watermarks(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          consumer TEXT NOT NULL,
          biz_date TEXT NOT NULL,
          watermark TEXT NOT NULL,
          source_slice TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS day_closes(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          biz_date TEXT UNIQUE NOT NULL,
          mode TEXT NOT NULL,
          gate_title TEXT NOT NULL,
          gate_body TEXT NOT NULL,
          note TEXT NOT NULL,
          closed_by TEXT NOT NULL,
          closed_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS gate_audit(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          biz_date TEXT NOT NULL,
          action TEXT NOT NULL,
          actor TEXT NOT NULL,
          result TEXT NOT NULL,
          note TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_slices_date ON slices(biz_date);
        CREATE INDEX IF NOT EXISTS idx_releases_slice ON releases(slice_id);
        CREATE INDEX IF NOT EXISTS idx_audit_date ON gate_audit(biz_date);
        """)
        if conn.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"] == 0:
            rows = [
                ("SL-2026-11-01-east", "2026-11-01", "east", 980, 1000, "部分完整", "未放行"),
                ("SL-2026-11-01-west", "2026-11-01", "west", 1000, 1000, "完整", "已放行"),
                ("SL-2026-11-02-east", "2026-11-02", "east", 0, 1000, "待检", "未放行"),
            ]
            for r in rows:
                conn.execute(
                    "INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,"
                    "completeness,release_status) VALUES (?,?,?,?,?,?,?)", r)
            conn.execute(
                "INSERT INTO releases(slice_id,consumer,status,note,created_at) "
                "VALUES (2,'ads_order','成功','完整切片放行','2026-11-01T18:00:00')")
            conn.execute(
                "INSERT INTO watermarks(consumer,biz_date,watermark,source_slice) "
                "VALUES ('ads_order','2026-11-01','2026-11-01T18:00:00','SL-2026-11-01-west')")
    finally:
        conn.close()


@app.on_event("startup")
def _start():
    init_db()


@app.get("/api/health")
def health():
    conn = connect()
    try:
        n = conn.execute("SELECT COUNT(*) c FROM slices").fetchone()["c"]
        closed = conn.execute("SELECT COUNT(*) c FROM day_closes").fetchone()["c"]
    finally:
        conn.close()
    return {"ok": True, "product": PRODUCT, "skeleton": SKELETON, "slices": n,
            "closed_days": closed, "gate_mode": GATE_MODE, "db": str(DB_PATH)}


@app.post("/api/login")
def login(body: LoginIn):
    conn = connect()
    try:
        row = conn.execute(
            "SELECT username, role FROM users WHERE username=? AND password_sha=?",
            (body.username, sha(body.password))).fetchone()
    finally:
        conn.close()
    if not row:
        raise HTTPException(401, "账号或密码错误")
    tok = secrets.token_hex(16)
    SESSIONS[tok] = row["username"]
    return {"token": tok, "username": row["username"], "role": row["role"]}


@app.get("/api/policy")
def policy(authorization: str | None = Header(default=None)):
    require_user(authorization)
    return dict(GATE_STATE)


@app.get("/api/slices")
def list_slices(biz_date: str | None = None, authorization: str | None = Header(default=None)):
    require_user(authorization)
    conn = connect()
    try:
        if biz_date:
            rows = conn.execute("SELECT * FROM slices WHERE biz_date=? ORDER BY id",
                                (norm_date(biz_date),)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM slices ORDER BY id").fetchall()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows]}


class SliceIn(BaseModel):
    slice_no: str
    biz_date: str
    partition_key: str
    row_count: int
    expected_count: int


@app.post("/api/slices")
def add_slice(body: SliceIn, authorization: str | None = Header(default=None)):
    actor = require_user(authorization)
    biz_date = norm_date(body.biz_date)
    comp = "完整" if body.row_count >= body.expected_count and body.expected_count > 0 else (
        "部分完整" if body.row_count > 0 else "待检")

    def write(conn):
        try:
            cur = conn.execute(
                "INSERT INTO slices(slice_no,biz_date,partition_key,row_count,expected_count,"
                "completeness,release_status) VALUES (?,?,?,?,?,?,?)",
                (body.slice_no, biz_date, body.partition_key, body.row_count,
                 body.expected_count, comp, "未放行"))
        except sqlite3.IntegrityError:
            _insert_audit(conn, biz_date, "切片登记", actor, "拒绝", "切片号已存在")
            raise RejectAndCommit("切片号已存在")
        _insert_audit(conn, biz_date, "切片登记", actor, "成功", f"{body.slice_no} / {comp}")
        return {"id": cur.lastrowid, "completeness": comp, "biz_date": biz_date}

    return run_guarded(biz_date, "切片登记", actor, write,
                       gate_message="切片登记与重裁均被拒绝")


@app.post("/api/slices/{sid}/evaluate")
def evaluate(sid: int, authorization: str | None = Header(default=None)):
    actor = require_user(authorization)
    probe = read_one("SELECT biz_date FROM slices WHERE id=?", (sid,))
    if not probe:
        raise HTTPException(404, "切片不存在")
    biz_date = probe["biz_date"]

    def write(conn):
        row = conn.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        comp = "完整" if row["row_count"] >= row["expected_count"] and row["expected_count"] > 0 else (
            "部分完整" if row["row_count"] > 0 else "待检")
        conn.execute("UPDATE slices SET completeness=? WHERE id=?", (comp, sid))
        _insert_audit(conn, biz_date, "完整度裁决", actor, "成功", f"{row['slice_no']} / {comp}")
        return {"id": sid, "completeness": comp}

    return run_guarded(biz_date, "完整度裁决", actor, write,
                       gate_message="重裁被拒绝")


class ReleaseIn(BaseModel):
    consumer: str


@app.post("/api/slices/{sid}/release")
def release(sid: int, body: ReleaseIn, authorization: str | None = Header(default=None)):
    actor = require_user(authorization)
    probe = read_one("SELECT biz_date FROM slices WHERE id=?", (sid,))
    if not probe:
        raise HTTPException(404, "切片不存在")
    biz_date = probe["biz_date"]

    def write(conn):
        row = conn.execute("SELECT * FROM slices WHERE id=?", (sid,)).fetchone()
        if not row:
            raise HTTPException(404, "切片不存在")
        if row["release_status"] == "已放行" or conn.execute(
                "SELECT 1 FROM releases WHERE slice_id=? AND status='成功' LIMIT 1", (sid,)).fetchone():
            _insert_audit(conn, biz_date, "放行", actor, "拒绝", "该切片已放行，禁止重复放行")
            raise RejectAndCommit("该切片已放行，禁止重复放行")
        if row["completeness"] != "完整":
            conn.execute(
                "INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
                (sid, body.consumer, "拒绝", "完整度未达标", now_iso()))
            _insert_audit(conn, biz_date, "放行", actor, "拒绝", "完整度未达标")
            raise RejectAndCommit("未完整禁止放行")
        ts = now_iso()
        conn.execute("UPDATE slices SET release_status='已放行' WHERE id=?", (sid,))
        conn.execute(
            "INSERT INTO releases(slice_id,consumer,status,note,created_at) VALUES (?,?,?,?,?)",
            (sid, body.consumer, "成功", "完整切片放行", ts))
        conn.execute(
            "INSERT INTO watermarks(consumer,biz_date,watermark,source_slice) VALUES (?,?,?,?)",
            (body.consumer, row["biz_date"], ts, row["slice_no"]))
        _insert_audit(conn, biz_date, "放行", actor, "成功",
                      f"{row['slice_no']} -> {body.consumer}，水位推进")
        return {"ok": True, "slice": row["slice_no"], "biz_date": row["biz_date"],
                "watermark_advanced": True}

    return run_guarded(biz_date, "放行", actor, write,
                       gate_message="放行与水位推进硬拒绝")


class CloseIn(BaseModel):
    biz_date: str
    mode: str | None = None
    note: str = ""


@app.get("/api/day-closes")
def list_day_closes(authorization: str | None = Header(default=None)):
    require_user(authorization)
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM day_closes ORDER BY biz_date DESC").fetchall()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/day-closes")
def close_day(body: CloseIn, authorization: str | None = Header(default=None)):
    actor = require_user(authorization)
    biz_date = norm_date(body.biz_date)
    if body.mode and body.mode != GATE_MODE:
        # 关日口径只认落库的甲门闩，改口径请求直接拒绝并留痕。
        audit_later(biz_date, "关日登记", actor, "拒绝",
                    f"关日口径偏了：只允许甲门闩，收到口径{body.mode}")
        raise HTTPException(409, "关日口径偏了：关日门闩只允许甲口径，不得改口径或混用乙口径")

    def write(conn):
        if _closed(conn, biz_date):
            _insert_audit(conn, biz_date, "关日登记", actor, "拒绝",
                          "该业务日已关日，当天不许更换口径")
            raise RejectAndCommit(f"业务日 {biz_date} 已关日，当天不许更换口径")
        ts = now_iso()
        conn.execute(
            "INSERT INTO day_closes(biz_date,mode,gate_title,gate_body,note,closed_by,closed_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (biz_date, GATE_MODE, GATE_TITLE, GATE_BODY, body.note.strip(), actor, ts))
        _insert_audit(conn, biz_date, "关日登记", actor, "成功",
                      f"甲门闩生效：放行与水位推进硬拒绝（{body.note.strip() or '无备注'}）")
        row = conn.execute("SELECT * FROM day_closes WHERE biz_date=?", (biz_date,)).fetchone()
        return {"ok": True, "item": dict(row)}

    return run_guarded(biz_date, "关日登记", actor, write, gate_message=None)


class LateFreezeIn(BaseModel):
    biz_date: str
    slice_no: str = ""
    reason: str = ""


@app.post("/api/late-freezes")
def late_freeze(body: LateFreezeIn, authorization: str | None = Header(default=None)):
    actor = require_user(authorization)
    biz_date = norm_date(body.biz_date)
    # 甲口径下不存在迟到冻结区。保留该入口只为显式拒绝并存证“关日口径偏了”。
    audit_later(biz_date, "迟到冻结登记", actor, "拒绝",
                "关日口径偏了：甲门闩不设迟到冻结区，关日后仅允许只读查询")
    raise HTTPException(409, "关日口径偏了：甲门闩不设迟到冻结区，关日后仅允许只读查询")


class ReleaseNoteIn(BaseModel):
    note: str


@app.patch("/api/releases/{rid}")
def patch_release(rid: int, body: ReleaseNoteIn, authorization: str | None = Header(default=None)):
    actor = require_user(authorization)
    probe = read_one(
        "SELECT s.biz_date FROM releases r JOIN slices s ON s.id=r.slice_id WHERE r.id=?", (rid,))
    if not probe:
        raise HTTPException(404, "放行流水不存在")
    biz_date = probe["biz_date"]

    def write(conn):
        row = conn.execute(
            "SELECT r.*, s.biz_date, s.slice_no FROM releases r "
            "JOIN slices s ON s.id=r.slice_id WHERE r.id=?", (rid,)).fetchone()
        if not row:
            raise HTTPException(404, "放行流水不存在")
        conn.execute("UPDATE releases SET note=? WHERE id=?", (body.note, rid))
        _insert_audit(conn, biz_date, "放行流水修订", actor, "成功",
                      f"{row['slice_no']} / {body.note}")
        return {"ok": True}

    return run_guarded(biz_date, "放行流水修订", actor, write,
                       gate_message="禁止关日后修改已放行成功记录", drift=True)


@app.get("/api/releases")
def list_releases(biz_date: str | None = None, authorization: str | None = Header(default=None)):
    require_user(authorization)
    conn = connect()
    try:
        sql = ("SELECT r.*, s.slice_no, s.biz_date FROM releases r "
               "JOIN slices s ON s.id=r.slice_id")
        args: tuple = ()
        if biz_date:
            sql += " WHERE s.biz_date=?"
            args = (norm_date(biz_date),)
        rows = conn.execute(sql + " ORDER BY r.id DESC", args).fetchall()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows]}


@app.get("/api/watermarks")
def list_wm(authorization: str | None = Header(default=None)):
    require_user(authorization)
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM watermarks ORDER BY id DESC").fetchall()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows]}


class WatermarkIn(BaseModel):
    consumer: str
    biz_date: str
    source_slice: str


@app.post("/api/watermarks")
def advance_watermark(body: WatermarkIn, authorization: str | None = Header(default=None)):
    """水位推进同样受关日门闩约束；关日后硬拒绝。"""
    actor = require_user(authorization)
    biz_date = norm_date(body.biz_date)

    def write(conn):
        ts = now_iso()
        conn.execute(
            "INSERT INTO watermarks(consumer,biz_date,watermark,source_slice) VALUES (?,?,?,?)",
            (body.consumer, biz_date, ts, body.source_slice))
        _insert_audit(conn, biz_date, "水位推进", actor, "成功", f"{body.consumer} / {body.source_slice}")
        return {"ok": True, "biz_date": biz_date, "watermark": ts}

    return run_guarded(biz_date, "水位推进", actor, write,
                       gate_message="水位推进硬拒绝")


@app.get("/api/audit")
def list_audit(authorization: str | None = Header(default=None)):
    require_user(authorization)
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM gate_audit ORDER BY id DESC LIMIT 200").fetchall()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows]}


def read_one(sql: str, args: tuple = ()):
    conn = connect()
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


@app.get("/api/biz-dates")
def biz_dates(authorization: str | None = Header(default=None)):
    require_user(authorization)
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT biz_date, COUNT(*) total, "
            "SUM(CASE WHEN completeness='完整' THEN 1 ELSE 0 END) complete, "
            "SUM(CASE WHEN release_status='已放行' THEN 1 ELSE 0 END) released "
            "FROM slices GROUP BY biz_date ORDER BY biz_date").fetchall()
    finally:
        conn.close()
    return {"items": [dict(r) for r in rows]}


# ------------------------------ 同库快照 ------------------------------

def _snapshot(conn) -> dict:
    closes = [dict(r) for r in conn.execute(
        "SELECT * FROM day_closes ORDER BY biz_date DESC").fetchall()]
    slices = [dict(r) for r in conn.execute("SELECT * FROM slices ORDER BY id").fetchall()]
    releases = [dict(r) for r in conn.execute(
        "SELECT r.*, s.slice_no, s.biz_date FROM releases r "
        "JOIN slices s ON s.id=r.slice_id ORDER BY r.id DESC").fetchall()]
    watermarks = [dict(r) for r in conn.execute(
        "SELECT * FROM watermarks ORDER BY id DESC").fetchall()]
    audit = [dict(r) for r in conn.execute(
        "SELECT * FROM gate_audit ORDER BY id DESC LIMIT 200").fetchall()]

    close_by_date = {c["biz_date"]: c for c in closes}
    cat: dict[str, dict] = {}

    def bucket(d):
        return cat.setdefault(d, {"biz_date": d, "total": 0, "complete": 0,
                                  "released": 0, "closed": False,
                                  "closed_at": None, "mode": None})

    for s in slices:
        e = bucket(s["biz_date"])
        e["total"] += 1
        e["complete"] += 1 if s["completeness"] == "完整" else 0
        e["released"] += 1 if s["release_status"] == "已放行" else 0
    for d, c in close_by_date.items():
        e = bucket(d)
        e["closed"] = True
        e["closed_at"] = c["closed_at"]
        e["mode"] = c["mode"]
    catalog = [cat[k] for k in sorted(cat)]

    slice_ids = {s["id"] for s in slices}
    checks = [
        {"key": "close_in_catalog", "label": "关日列表均落入业务目录",
         "ok": all(c["biz_date"] in cat for c in closes),
         "detail": f"{len(closes)} 条关日记录"},
        {"key": "release_in_catalog", "label": "放行流水切片均属业务目录",
         "ok": all(r["slice_id"] in slice_ids and r["biz_date"] in cat for r in releases),
         "detail": f"{len(releases)} 条放行流水"},
        {"key": "watermark_in_catalog", "label": "水位业务日均属业务目录",
         "ok": all(w["biz_date"] in cat for w in watermarks),
         "detail": f"{len(watermarks)} 条水位"},
    ]
    post_release = [r for r in releases
                    if r["status"] == "成功" and r["biz_date"] in close_by_date
                    and r["created_at"] > close_by_date[r["biz_date"]]["closed_at"]]
    checks.append({"key": "no_release_after_close", "label": "关日后无新增放行成功",
                   "ok": not post_release, "detail": f"{len(post_release)} 条越闸"})
    post_wm = [w for w in watermarks
               if w["biz_date"] in close_by_date
               and w["watermark"] > close_by_date[w["biz_date"]]["closed_at"]]
    checks.append({"key": "no_watermark_after_close", "label": "关日后无水位推进",
                   "ok": not post_wm, "detail": f"{len(post_wm)} 条越闸"})
    drift = [a for a in audit if "口径偏了" in a["note"]]
    drift_passed = [a for a in drift if a["result"] != "拒绝"]
    checks.append({"key": "gate_policy_single", "label": "偏离关日门闩说明的请求均已拒绝",
                   "ok": not drift_passed,
                   "detail": f"{len(drift)} 条口径偏了请求，{len(drift_passed)} 条未被拒绝"})

    released_for_date = {}
    for r in releases:
        if r["status"] == "成功":
            released_for_date[r["biz_date"]] = released_for_date.get(r["biz_date"], 0) + 1
    counts_match = all(e["released"] <= released_for_date.get(e["biz_date"], 0) for e in catalog)
    checks.append({"key": "counts_match", "label": "关日画面与放行流水读数一致",
                   "ok": counts_match, "detail": "按同一快照逐日对账"})

    return {
        "db": str(DB_PATH),
        "read_at": now_iso(),
        "policy": dict(GATE_STATE),
        "day_closes": closes,
        "catalog": catalog,
        "slices": slices,
        "releases": releases,
        "watermarks": watermarks,
        "audit": audit,
        "consistency": {"ok": all(x["ok"] for x in checks), "checks": checks},
        "counts": {
            "day_closes": len(closes),
            "catalog": len(catalog),
            "slices": len(slices),
            "releases": len(releases),
            "watermarks": len(watermarks),
            "closed_days": len(close_by_date),
        },
    }


@app.get("/api/snapshot")
def snapshot(authorization: str | None = Header(default=None)):
    require_user(authorization)
    conn = connect()
    try:
        conn.execute("BEGIN")
        data = _snapshot(conn)
        conn.execute("COMMIT")
    finally:
        conn.close()
    return data


@app.get("/")
def index():
    return FileResponse(FRONTEND / "index.html")


app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")
