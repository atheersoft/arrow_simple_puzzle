#!/usr/bin/env python3
"""
خادم ألغاز الأسهم
- تسجيل اسم المستخدم
- حفظ التقدم لكل درجة صعوبة بشكل مستقل
- احتساب الدرجة الرقمية على الخادم (لا يُوثق برقم يرسله الجوال)
- لوحة ترتيب عالمية مستقلة لكل درجة صعوبة

قاعدة البيانات:
  - بدون إعداد: SQLite في ملف arrow.db
  - للإنتاج:  DATABASE_URL=postgresql://user:pass@host:5432/dbname  (يتطلب psycopg)
"""
import hashlib
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

BASE = Path(__file__).parent
DIFFS = ("easy", "medium", "hard")
LEVELS = json.loads((BASE / "levels.json").read_text())
DIRS = {"U": (-1, 0), "D": (1, 0), "L": (0, -1), "R": (0, 1)}
# للتجربة على الويب فقط: يسمح بربط الحساب بمعرّف المتصفح
ALLOW_VISITOR_LOGIN = os.environ.get("ALLOW_VISITOR_LOGIN") == "1"


# ======================= طبقة قاعدة البيانات =======================
class DB:
    def __init__(self):
        url = os.environ.get("DATABASE_URL", "")
        self.pg = url.startswith("postgres")
        self.lock = threading.Lock()
        if self.pg:
            import psycopg  # pip install "psycopg[binary]"

            self.conn = psycopg.connect(url, autocommit=True)
        else:
            path = os.environ.get("SQLITE_PATH", str(BASE / "arrow.db"))
            self.conn = sqlite3.connect(path, check_same_thread=False)
            self.conn.execute("PRAGMA journal_mode=WAL")
        self.migrate()

    def _q(self, sql):
        return sql.replace("?", "%s") if self.pg else sql

    def exec(self, sql, args=()):
        with self.lock:
            cur = self.conn.cursor()
            cur.execute(self._q(sql), args)
            if not self.pg:
                self.conn.commit()
            return cur

    def one(self, sql, args=()):
        return self.exec(sql, args).fetchone()

    def all(self, sql, args=()):
        return self.exec(sql, args).fetchall()

    def migrate(self):
        idtype = "BIGSERIAL PRIMARY KEY" if self.pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
        stmts = [
            f"""CREATE TABLE IF NOT EXISTS users (
                id {idtype},
                username TEXT NOT NULL,
                username_lc TEXT NOT NULL UNIQUE,
                token_hash TEXT NOT NULL UNIQUE,
                visitor_id TEXT,
                created_at BIGINT NOT NULL,
                last_seen BIGINT NOT NULL)""",
            # أفضل نتيجة لكل مستوى، لكل درجة صعوبة
            """CREATE TABLE IF NOT EXISTS level_results (
                user_id BIGINT NOT NULL,
                difficulty TEXT NOT NULL,
                level INTEGER NOT NULL,
                best_score INTEGER NOT NULL,
                best_stars INTEGER NOT NULL,
                best_time INTEGER NOT NULL,
                best_mistakes INTEGER NOT NULL,
                plays INTEGER NOT NULL DEFAULT 1,
                updated_at BIGINT NOT NULL,
                PRIMARY KEY (user_id, difficulty, level))""",
            # ملخص اللاعب لكل درجة صعوبة (للترتيب)
            """CREATE TABLE IF NOT EXISTS difficulty_stats (
                user_id BIGINT NOT NULL,
                difficulty TEXT NOT NULL,
                total_score BIGINT NOT NULL DEFAULT 0,
                levels_done INTEGER NOT NULL DEFAULT 0,
                total_stars INTEGER NOT NULL DEFAULT 0,
                total_time BIGINT NOT NULL DEFAULT 0,
                total_mistakes INTEGER NOT NULL DEFAULT 0,
                current_level INTEGER NOT NULL DEFAULT 0,
                updated_at BIGINT NOT NULL,
                PRIMARY KEY (user_id, difficulty))""",
            # كل محاولة لعب: مفتوحة / مكتملة / متروكة
            f"""CREATE TABLE IF NOT EXISTS attempts (
                id {idtype},
                user_id BIGINT NOT NULL,
                difficulty TEXT NOT NULL,
                level INTEGER NOT NULL,
                status TEXT NOT NULL,
                started_at BIGINT NOT NULL,
                ended_at BIGINT,
                seconds INTEGER NOT NULL DEFAULT 0,
                mistakes INTEGER NOT NULL DEFAULT 0,
                penalty INTEGER NOT NULL DEFAULT 0)""",
            "CREATE INDEX IF NOT EXISTS idx_attempts ON attempts (user_id, difficulty, status)",
            "CREATE INDEX IF NOT EXISTS idx_rank ON difficulty_stats (difficulty, total_score DESC, total_mistakes, total_time)",
            "CREATE INDEX IF NOT EXISTS idx_visitor ON users (visitor_id)",
        ]
        for s in stmts:
            self.exec(s)
        # ترقية الجداول القديمة بدون حذف بيانات
        for col in ("penalty BIGINT NOT NULL DEFAULT 0", "best_sum BIGINT NOT NULL DEFAULT 0",
                    "abandoned INTEGER NOT NULL DEFAULT 0"):
            try:
                ine = "IF NOT EXISTS " if self.pg else ""
                self.exec(f"ALTER TABLE difficulty_stats ADD COLUMN {ine}{col}")
            except sqlite3.OperationalError:
                pass  # العمود موجود مسبقًا


db = DB()


# ======================= منطق اللعبة والدرجة =======================
def replay(level: dict, taps: List[int]):
    """يعيد تشغيل ضغطات اللاعب على الخادم: يتحقق من الحل ويحسب الأخطاء."""
    n, cells = level["n"], list(level["g"])
    mistakes = 0
    for p in taps:
        if not (0 <= p < n * n) or cells[p] == ".":
            continue  # ضغطة على خانة فارغة لا تُحتسب
        dr, dc = DIRS[cells[p]]
        r, c = p // n + dr, p % n + dc
        ok = True
        while 0 <= r < n and 0 <= c < n:
            if cells[r * n + c] != ".":
                ok = False
                break
            r += dr
            c += dc
        if ok:
            cells[p] = "."
        else:
            mistakes += 1
    return all(x == "." for x in cells), mistakes


def par_time(arrows: int) -> int:
    return round(arrows * 1.5 + 5)


def calc_score(arrows: int, seconds: int, mistakes: int) -> int:
    """الدرجة الرقمية: أخطاء أقل + وقت أسرع = درجة أعلى."""
    base = arrows * 100
    time_factor = min(2.0, max(0.1, par_time(arrows) / max(seconds, 1)))
    mistake_factor = 1 / (1 + 0.3 * mistakes)
    return round(base * time_factor * mistake_factor)


def calc_stars(arrows: int, seconds: int, mistakes: int) -> int:
    par = par_time(arrows)
    if mistakes == 0 and seconds <= par:
        return 3
    if mistakes <= 2 and seconds <= par * 2:
        return 2
    return 1


# خصم المحاولات غير المكتملة
PENALTY_PER_MISTAKE = 100
PENALTY_PER_SECOND = 5
MAX_ABANDON_SECONDS = 600   # حد أقصى للوقت المحتسب في محاولة متروكة
STALE_SECONDS_CAP = 300     # محاولة لم يُبلَّغ عنها (إغلاق التطبيق فجأة)
MIN_SECONDS_TO_COUNT = 5    # فتح المستوى والخروج فورًا بدون ضغطات لا يُحتسب


def calc_penalty(seconds: int, mistakes: int) -> int:
    return mistakes * PENALTY_PER_MISTAKE + seconds * PENALTY_PER_SECOND


def count_mistakes(level: dict, taps: List[int]) -> int:
    return replay(level, taps)[1]


def min_seconds(arrows: int) -> int:
    # حد أدنى منطقي لمنع الأرقام المزيفة (~7 ضغطات في الثانية)
    return max(1, int(arrows * 0.15))


# ======================= التطبيق =======================
app = FastAPI(title="Arrow Puzzle Server")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

USERNAME_RE = re.compile(r"^[\w\u0600-\u06FF]{3,20}$")


def now():
    return int(time.time())


def hash_token(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def current_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "مطلوب تسجيل الدخول")
    row = db.one("SELECT id, username FROM users WHERE token_hash=?", (hash_token(authorization[7:]),))
    if not row:
        raise HTTPException(401, "رمز الدخول غير صالح")
    db.exec("UPDATE users SET last_seen=? WHERE id=?", (now(), row[0]))
    return {"id": row[0], "username": row[1]}


class RegisterIn(BaseModel):
    username: str = Field(..., min_length=3, max_length=20)


class ResultIn(BaseModel):
    difficulty: str
    level: int  # يبدأ من 0
    taps: List[int] = Field(..., max_length=5000)
    seconds: int = Field(..., ge=0, le=86400)
    attempt_id: Optional[int] = None


class StartIn(BaseModel):
    difficulty: str
    level: int


class AbandonIn(BaseModel):
    difficulty: str
    level: int
    taps: List[int] = Field(default_factory=list, max_length=5000)
    seconds: int = Field(0, ge=0, le=86400)
    attempt_id: Optional[int] = None


class SyncIn(BaseModel):
    results: List[ResultIn] = Field(default_factory=list, max_length=500)
    abandoned: List[AbandonIn] = Field(default_factory=list, max_length=500)


@app.get("/api/health")
def health():
    return {"ok": True, "db": "postgres" if db.pg else "sqlite"}


@app.post("/api/register")
def register(body: RegisterIn, request: Request):
    name = body.username.strip()
    if not USERNAME_RE.match(name):
        raise HTTPException(400, "الاسم من 3 إلى 20 حرفًا: حروف عربية أو إنجليزية أو أرقام أو _")
    if db.one("SELECT 1 FROM users WHERE username_lc=?", (name.lower(),)):
        raise HTTPException(409, "اسم المستخدم مستخدم من قبل، اختر اسمًا آخر")
    token = secrets.token_urlsafe(32)
    visitor = request.headers.get("x-visitor-id") if ALLOW_VISITOR_LOGIN else None
    t = now()
    db.exec(
        "INSERT INTO users (username, username_lc, token_hash, visitor_id, created_at, last_seen) VALUES (?,?,?,?,?,?)",
        (name, name.lower(), hash_token(token), visitor, t, t),
    )
    uid = db.one("SELECT id FROM users WHERE username_lc=?", (name.lower(),))[0]
    for d in DIFFS:
        db.exec("INSERT INTO difficulty_stats (user_id, difficulty, updated_at) VALUES (?,?,?)", (uid, d, t))
    return {"user_id": uid, "username": name, "token": token}


@app.get("/api/session")
def visitor_session(request: Request):
    """للتجربة على الويب فقط (ALLOW_VISITOR_LOGIN=1): يسترجع حساب المتصفح."""
    if not ALLOW_VISITOR_LOGIN:
        raise HTTPException(404, "غير مفعّل")
    vid = request.headers.get("x-visitor-id")
    if not vid:
        return {"user": None}
    row = db.one("SELECT id, username FROM users WHERE visitor_id=? ORDER BY id DESC", (vid,))
    if not row:
        return {"user": None}
    token = secrets.token_urlsafe(32)
    db.exec("UPDATE users SET token_hash=? WHERE id=?", (hash_token(token), row[0]))
    return {"user": {"user_id": row[0], "username": row[1], "token": token}}


def stats_row(uid, d):
    r = db.one(
        "SELECT total_score, levels_done, total_stars, total_time, total_mistakes, current_level, penalty, abandoned, best_sum FROM difficulty_stats WHERE user_id=? AND difficulty=?",
        (uid, d),
    )
    keys = ("total_score", "levels_done", "total_stars", "total_time", "total_mistakes", "current_level", "penalty", "abandoned", "best_sum")
    return dict(zip(keys, r)) if r else dict.fromkeys(keys, 0)


def rank_of(uid, d):
    s = stats_row(uid, d)
    if s["levels_done"] == 0 and s["abandoned"] == 0:
        return None
    r = db.one(
        """SELECT COUNT(*) FROM difficulty_stats WHERE difficulty=? AND (levels_done>0 OR abandoned>0) AND (
             total_score > ? OR (total_score = ? AND total_mistakes < ?) OR
             (total_score = ? AND total_mistakes = ? AND total_time < ?))""",
        (d, s["total_score"], s["total_score"], s["total_mistakes"], s["total_score"], s["total_mistakes"], s["total_time"]),
    )
    return r[0] + 1


@app.get("/api/me")
def me(user=Depends(current_user)):
    return {
        "user": user,
        "difficulties": {d: {**stats_row(user["id"], d), "rank": rank_of(user["id"], d)} for d in DIFFS},
    }


@app.get("/api/progress/{difficulty}")
def progress(difficulty: str, user=Depends(current_user)):
    if difficulty not in DIFFS:
        raise HTTPException(400, "درجة صعوبة غير معروفة")
    rows = db.all(
        "SELECT level, best_score, best_stars, best_time, best_mistakes FROM level_results WHERE user_id=? AND difficulty=? ORDER BY level",
        (user["id"], difficulty),
    )
    return {
        "difficulty": difficulty,
        **stats_row(user["id"], difficulty),
        "rank": rank_of(user["id"], difficulty),
        "levels": [dict(zip(("level", "score", "stars", "time", "mistakes"), r)) for r in rows],
    }


def recompute_stats(uid: int, d: str) -> int:
    """درجة اللاعب = مجموع أفضل نتائجه − خصم المحاولات المتروكة."""
    agg = db.one(
        "SELECT COALESCE(SUM(best_score),0), COUNT(*), COALESCE(SUM(best_stars),0), COALESCE(SUM(best_time),0), COALESCE(SUM(best_mistakes),0), COALESCE(MAX(level),-1) FROM level_results WHERE user_id=? AND difficulty=?",
        (uid, d),
    )
    ab = db.one(
        "SELECT COALESCE(SUM(penalty),0), COUNT(*), COALESCE(SUM(seconds),0), COALESCE(SUM(mistakes),0) FROM attempts WHERE user_id=? AND difficulty=? AND status='abandoned'",
        (uid, d),
    )
    best_sum, penalty = int(agg[0]), int(ab[0])
    total = max(0, best_sum - penalty)
    cur_level = min(int(agg[5]) + 1, len(LEVELS[d]) - 1)
    db.exec(
        """UPDATE difficulty_stats SET total_score=?, best_sum=?, penalty=?, abandoned=?, levels_done=?, total_stars=?,
             total_time=?, total_mistakes=?, current_level=?, updated_at=? WHERE user_id=? AND difficulty=?""",
        (total, best_sum, penalty, int(ab[1]), agg[1], agg[2], int(agg[3]) + int(ab[2]), int(agg[4]) + int(ab[3]),
         max(cur_level, 0), now(), uid, d),
    )
    return total


def open_attempt(uid: int, attempt_id: Optional[int]):
    if attempt_id is None:
        return None
    return db.one("SELECT id, started_at, difficulty, level FROM attempts WHERE id=? AND user_id=? AND status='open'",
                  (attempt_id, uid))


def close_abandoned(uid: int, att_id: int, seconds: int, mistakes: int):
    """يغلق محاولة كمتروكة ويحتسب الخصم (أو يحذفها إذا لم يلعب فعليًا)."""
    seconds = min(seconds, MAX_ABANDON_SECONDS)
    if mistakes == 0 and seconds < MIN_SECONDS_TO_COUNT:
        db.exec("DELETE FROM attempts WHERE id=?", (att_id,))
        return 0
    p = calc_penalty(seconds, mistakes)
    db.exec("UPDATE attempts SET status='abandoned', ended_at=?, seconds=?, mistakes=?, penalty=? WHERE id=?",
            (now(), seconds, mistakes, p, att_id))
    return p


def close_stale(uid: int):
    """محاولات مفتوحة لم يُبلَّغ عنها (أُغلق التطبيق فجأة) تُحتسب بالوقت فقط."""
    rows = db.all("SELECT id, started_at, difficulty FROM attempts WHERE user_id=? AND status='open'", (uid,))
    diffs = set()
    for aid, started, d in rows:
        close_abandoned(uid, aid, min(now() - started, STALE_SECONDS_CAP), 0)
        diffs.add(d)
    for d in diffs:
        recompute_stats(uid, d)


def save_abandon(uid: int, a: AbandonIn):
    if a.difficulty not in DIFFS or not (0 <= a.level < len(LEVELS[a.difficulty])):
        raise HTTPException(400, "مستوى غير صالح")
    mistakes = count_mistakes(LEVELS[a.difficulty][a.level], a.taps)
    att = open_attempt(uid, a.attempt_id)
    if att:
        seconds = max(a.seconds, now() - att[1] - 5)  # لا يمكن تقليل الوقت
        aid = att[0]
    else:  # محاولة أثناء انقطاع الإنترنت
        seconds = a.seconds
        t = now()
        db.exec("INSERT INTO attempts (user_id, difficulty, level, status, started_at) VALUES (?,?,?,'open',?)",
                (uid, a.difficulty, a.level, t - seconds))
        aid = db.one("SELECT MAX(id) FROM attempts WHERE user_id=?", (uid,))[0]
    penalty = close_abandoned(uid, aid, max(0, seconds), mistakes)
    total = recompute_stats(uid, a.difficulty)
    return {"difficulty": a.difficulty, "level": a.level, "mistakes": mistakes, "seconds": min(seconds, MAX_ABANDON_SECONDS),
            "penalty": penalty, "total_score": total, "rank": rank_of(uid, a.difficulty)}


@app.post("/api/attempt/start")
def attempt_start(body: StartIn, user=Depends(current_user)):
    if body.difficulty not in DIFFS or not (0 <= body.level < len(LEVELS[body.difficulty])):
        raise HTTPException(400, "مستوى غير صالح")
    close_stale(user["id"])
    db.exec("INSERT INTO attempts (user_id, difficulty, level, status, started_at) VALUES (?,?,?,'open',?)",
            (user["id"], body.difficulty, body.level, now()))
    aid = db.one("SELECT MAX(id) FROM attempts WHERE user_id=?", (user["id"],))[0]
    return {"attempt_id": aid}


@app.post("/api/attempt/abandon")
def attempt_abandon(body: AbandonIn, user=Depends(current_user)):
    return save_abandon(user["id"], body)


def save_result(uid: int, r: ResultIn):
    if r.difficulty not in DIFFS or not (0 <= r.level < len(LEVELS[r.difficulty])):
        raise HTTPException(400, "مستوى غير صالح")
    lvl = LEVELS[r.difficulty][r.level]
    solved, mistakes = replay(lvl, r.taps)
    if not solved:
        raise HTTPException(422, "الحركات لا تحل المستوى")
    seconds = max(r.seconds, min_seconds(lvl["a"]))
    att = open_attempt(uid, r.attempt_id)
    if att:  # الوقت الحقيقي على الخادم يمنع إرسال وقت أقل من الحقيقي
        seconds = max(seconds, min(now() - att[1] - 5, 86400))
    score = calc_score(lvl["a"], seconds, mistakes)
    stars = calc_stars(lvl["a"], seconds, mistakes)
    t = now()
    if att:
        db.exec("UPDATE attempts SET status='done', ended_at=?, seconds=?, mistakes=? WHERE id=?",
                (t, seconds, mistakes, att[0]))
    old = db.one(
        "SELECT best_score, best_stars, best_time, best_mistakes, plays FROM level_results WHERE user_id=? AND difficulty=? AND level=?",
        (uid, r.difficulty, r.level),
    )
    improved = old is None or score > old[0]
    if old is None:
        db.exec(
            "INSERT INTO level_results VALUES (?,?,?,?,?,?,?,1,?)",
            (uid, r.difficulty, r.level, score, stars, seconds, mistakes, t),
        )
    else:
        db.exec(
            """UPDATE level_results SET plays=plays+1, updated_at=?,
                 best_stars=?, best_time=?,
                 best_score=?, best_mistakes=?
               WHERE user_id=? AND difficulty=? AND level=?""",
            (
                t,
                max(old[1], stars),
                min(old[2], seconds),
                max(old[0], score),
                mistakes if improved else old[3],
                uid, r.difficulty, r.level,
            ),
        )
    total = recompute_stats(uid, r.difficulty)
    return {
        "difficulty": r.difficulty,
        "level": r.level,
        "score": score,
        "stars": stars,
        "mistakes": mistakes,
        "seconds": seconds,
        "new_best": improved,
        "total_score": total,
        "rank": rank_of(uid, r.difficulty),
    }


@app.post("/api/results")
def submit(r: ResultIn, user=Depends(current_user)):
    return save_result(user["id"], r)


@app.post("/api/sync")
def sync(body: SyncIn, user=Depends(current_user)):
    """رفع النتائج المحفوظة محليًا أثناء انقطاع الإنترنت دفعة واحدة."""
    out = []
    for r in body.results:
        try:
            out.append({"ok": True, **save_result(user["id"], r)})
        except HTTPException as e:
            out.append({"ok": False, "difficulty": r.difficulty, "level": r.level, "error": e.detail})
    ab = []
    for a in body.abandoned:
        try:
            ab.append({"ok": True, **save_abandon(user["id"], a)})
        except HTTPException as e:
            ab.append({"ok": False, "error": e.detail})
    return {"results": out, "abandoned": ab}


@app.get("/api/leaderboard/{difficulty}")
def leaderboard(difficulty: str, limit: int = 100, authorization: Optional[str] = Header(None)):
    if difficulty not in DIFFS:
        raise HTTPException(400, "درجة صعوبة غير معروفة")
    limit = max(1, min(limit, 500))
    rows = db.all(
        """SELECT u.id, u.username, s.total_score, s.levels_done, s.total_stars, s.total_mistakes, s.total_time, s.penalty, s.abandoned
           FROM difficulty_stats s JOIN users u ON u.id = s.user_id
           WHERE s.difficulty=? AND (s.levels_done > 0 OR s.abandoned > 0)
           ORDER BY s.total_score DESC, s.total_mistakes ASC, s.total_time ASC, s.updated_at ASC
           LIMIT ?""",
        (difficulty, limit),
    )
    keys = ("user_id", "username", "score", "levels_done", "stars", "mistakes", "time", "penalty", "abandoned")
    top = [{"rank": i + 1, **dict(zip(keys, r))} for i, r in enumerate(rows)]
    mine = None
    if authorization:
        try:
            u = current_user(authorization)
            mine = {"rank": rank_of(u["id"], difficulty), "username": u["username"], **stats_row(u["id"], difficulty)}
        except HTTPException:
            pass
    total = db.one("SELECT COUNT(*) FROM difficulty_stats WHERE difficulty=? AND (levels_done>0 OR abandoned>0)", (difficulty,))[0]
    return {"difficulty": difficulty, "players": total, "top": top, "me": mine}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
