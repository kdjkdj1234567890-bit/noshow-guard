#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
노쇼 가드 — "빈 예약 시간은 버려지는 돈입니다."
동네 사장님용 노쇼 방지 자동 알림 데모 (Flask + SQLite)
"""
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta

from flask import (Flask, abort, flash, g, redirect, render_template,
                   request, url_for)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "noshow.db")

app = Flask(__name__)
app.secret_key = "noshow-guard-demo-key"
app.config["JSON_AS_ASCII"] = False

# ---------------------------------------------------------------------------
# 상수 — 전부 사장님 말로
# ---------------------------------------------------------------------------
STATUS_LABEL = {
    "pending": "응답 대기",
    "confirmed": "확정",
    "changed": "시간 변경됨",
    "cancelled": "취소됨",
    "noshow": "노쇼",
}
STATUS_BADGE = {
    "pending": "warn",
    "confirmed": "ok",
    "changed": "info",
    "cancelled": "muted",
    "noshow": "bad",
}

# 업종별 프리셋 — "어떤 가게세요?" 선택에 따라 메뉴·문자 톤이 바뀝니다
INDUSTRIES = {
    "cleaning": {
        "name": "청소업체", "icon": "🧹", "sample_shop": "깔끔클린",
        "ask_staff": False, "staff_label": "",
        "services": [
            ("입주청소", 150000), ("정기청소", 80000), ("사무실청소", 120000),
            ("이사청소", 180000), ("준공청소", 200000),
        ],
    },
    "beauty": {
        "name": "미용실·네일샵", "icon": "💇", "sample_shop": "멋쟁이미용실",
        "ask_staff": True, "staff_label": "담당 디자이너",
        "services": [
            ("커트", 25000), ("펌", 60000), ("염색", 70000),
            ("네일아트", 50000), ("속눈썹펌", 40000),
        ],
    },
    "moving": {
        "name": "이사·용달", "icon": "🚚", "sample_shop": "튼튼이사",
        "ask_staff": False, "staff_label": "",
        "services": [
            ("포장이사", 500000), ("반포장이사", 300000), ("용달이사", 150000),
            ("원룸이사", 200000), ("사무실이전", 400000),
        ],
    },
}
DEFAULT_INDUSTRY = "cleaning"


def current_industry():
    key = get_setting("industry", DEFAULT_INDUSTRY)
    return key if key in INDUSTRIES else DEFAULT_INDUSTRY


def industry_services():
    return INDUSTRIES[current_industry()]["services"]

# 예약 가능한 시간대 (30분 단위)
TIME_SLOTS = [f"{h:02d}:{m:02d}" for h in range(9, 21) for m in (0, 30)]

NO_SHOW_BEFORE = 0.30   # 도입 전 노쇼율 가정
NO_SHOW_AFTER = 0.05    # 도입 후 노쇼율 가정

# ---------------------------------------------------------------------------
# DB
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS reservations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    date TEXT NOT NULL,
    time TEXT NOT NULL,
    service TEXT NOT NULL,
    price INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    token TEXT NOT NULL UNIQUE,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reservation_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    message TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    UNIQUE(reservation_id, kind)
);
CREATE TABLE IF NOT EXISTS blacklist (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL UNIQUE,
    noshow_count INTEGER NOT NULL DEFAULT 0,
    last_date TEXT NOT NULL DEFAULT '',
    memo TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS monthly_stats (
    month TEXT PRIMARY KEY,
    total INTEGER NOT NULL DEFAULT 0,
    noshow INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(SCHEMA)
    # 구 DB 호환: 담당자(디자이너) 컬럼 추가
    try:
        db.execute("ALTER TABLE reservations ADD COLUMN staff TEXT "
                   "NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    db.commit()
    db.close()


def q1(sql, args=()):
    return get_db().execute(sql, args).fetchone()


def qall(sql, args=()):
    return get_db().execute(sql, args).fetchall()


def get_setting(key, default):
    row = q1("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key, value):
    db = get_db()
    db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
               (key, value))
    db.commit()


def today_str():
    return date.today().isoformat()


def tomorrow_str():
    return (date.today() + timedelta(days=1)).isoformat()


def won(n):
    """1234567 -> '1,234,567원'"""
    try:
        return f"{int(round(n)):,}원"
    except (TypeError, ValueError):
        return "0원"


def manwon(n):
    """만원 단위 표기: 1250000 -> '125만원'"""
    try:
        v = int(round(n))
    except (TypeError, ValueError):
        v = 0
    if v >= 10000 and v % 10000 == 0:
        return f"{v // 10000:,}만원"
    return f"{v:,}원"


def kdate(dstr):
    """2026-10-13 -> '10월 13일 (화)'"""
    try:
        d = datetime.strptime(dstr, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return dstr
    week = "월화수목금토일"[d.weekday()]
    return f"{d.month}월 {d.day}일 ({week})"


def ktime(tstr):
    """14:30 -> '오후 2시 30분'"""
    try:
        h, m = map(int, tstr.split(":"))
    except (TypeError, ValueError, AttributeError):
        return tstr
    ampm = "오전" if h < 12 else "오후"
    hh = h if h <= 12 else h - 12
    hh = 12 if hh == 0 else hh
    return f"{ampm} {hh}시" + (f" {m}분" if m else "")


def clean_phone(raw):
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) < 10 or len(digits) > 11:
        return None
    if len(digits) == 11:
        return f"{digits[:3]}-{digits[3:7]}-{digits[7:]}"
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"


def mask_name(name):
    name = (name or "").strip()
    if len(name) <= 1:
        return name + "*"
    return name[0] + "*" * (len(name) - 1)


def is_active(status):
    return status in ("pending", "confirmed", "changed")


def slot_taken(dstr, tstr, exclude_id=None):
    row = q1(
        "SELECT id FROM reservations WHERE date=? AND time=? "
        "AND status IN ('pending','confirmed','changed') "
        "AND id != COALESCE(?, -1) LIMIT 1",
        (dstr, tstr, exclude_id),
    )
    return row is not None


def reminder_message(r, kind):
    industry = current_industry()
    shop = get_setting("shop_name", "우리 가게")
    link = url_for("customer_page", token=r["token"], _external=True)
    name = r["customer_name"]
    when_date = kdate(r["date"])
    when_time = ktime(r["time"])
    svc = r["service"]
    staff = (r["staff"] or "").strip() if "staff" in r.keys() else ""

    if industry == "cleaning":
        if kind == "전날":
            return (
                f"[{shop}] {name}님, 내일({when_date}) {when_time} "
                f"{svc} 예약입니다.\n"
                f"🧹 청소 전날 준비물 안내\n"
                f"• 귀중품·현금은 미리 치워주세요\n"
                f"• 주차 공간을 비워주세요\n"
                f"• 반려동물은 안전한 곳에 부탁드려요\n"
                f"확정은 여기를 눌러주세요: {link}"
            )
        return (
            f"[{shop}] {name}님, 오늘 {when_time} {svc} 예정입니다.\n"
            f"담당자가 곧 방문할게요. "
            f"변경이 필요하면 여기를 눌러주세요: {link}"
        )

    if industry == "beauty":
        who = f" (담당: {staff} 디자이너)" if staff else ""
        if kind == "전날":
            return (
                f"[{shop}] {name}님, 내일 {when_time} {svc} "
                f"예약 확인 부탁드립니다.{who}\n"
                f"확정·변경은 여기를 눌러주세요: {link}"
            )
        return (
            f"[{shop}] {name}님, 오늘 {when_time} {svc} 예약이에요.{who}\n"
            f"늦으시면 미리 알려주세요! "
            f"변경은 여기를 눌러주세요: {link}"
        )

    # moving
    if kind == "전날":
        return (
            f"[{shop}] {name}님, 내일({when_date}) {when_time} "
            f"{svc} 예약입니다.\n"
            f"🚚 이사 전날 체크리스트\n"
            f"• 귀중품은 직접 챙겨주세요\n"
            f"• 냉장고 음식을 비워주세요\n"
            f"• 관리사무소에 이사 신고를 해주세요\n"
            f"확정은 여기를 눌러주세요: {link}"
        )
    return (
        f"[{shop}] {name}님, 오늘은 이사 당일입니다! 🚚\n"
        f"이사 당일 체크리스트\n"
        f"• 귀중품은 직접 소지해 주세요\n"
        f"• 수도·가스·전기 검침을 확인해 주세요\n"
        f"• 새집 열쇠·비밀번호를 준비해 주세요\n"
        f"기사님이 {when_time}에 도착합니다. "
        f"변경은 여기를 눌러주세요: {link}"
    )


# ---------------------------------------------------------------------------
# 에러 처리 — traceback 절대 노출 금지
# ---------------------------------------------------------------------------
@app.errorhandler(404)
def not_found(_e):
    return render_template("error.html",
                           title="페이지를 찾을 수 없어요",
                           message="주소가 바뀌었거나 없는 페이지예요. "
                                   "아래 버튼으로 처음으로 돌아가세요."), 404


@app.errorhandler(500)
def server_error(_e):
    return render_template("error.html",
                           title="잠시 문제가 생겼어요",
                           message="처음 화면으로 돌아가서 다시 시도해 주세요. "
                                   "같은 문제가 반복되면 처음부터 다시 시작해 보세요."), 500


# ---------------------------------------------------------------------------
# 1) 첫 화면 — 수익 시뮬레이터
# ---------------------------------------------------------------------------
@app.route("/", methods=["GET", "POST"])
def index():
    monthly = int(get_setting("monthly_reservations", "200"))
    avg_price = int(get_setting("avg_price", "50000"))
    shop = get_setting("shop_name", "")
    industry = current_industry()

    if request.method == "POST":
        new_industry = request.form.get("industry")
        if new_industry in INDUSTRIES and new_industry != industry:
            industry = new_industry
            set_setting("industry", industry)
            # 업종 변경 시 평균 단가를 해당 업종 메뉴 중간값으로 제안
            prices = sorted(p for _, p in INDUSTRIES[industry]["services"])
            avg_price = prices[len(prices) // 2]
            set_setting("avg_price", str(avg_price))
            flash(f"{INDUSTRIES[industry]['icon']} "
                  f"{INDUSTRIES[industry]['name']} 버전으로 바뀌었어요. "
                  "메뉴와 문자 내용이 업종에 맞게 변경됩니다.", "success")
            return redirect(url_for("index"))
        try:
            monthly = max(0, int(request.form.get("monthly", monthly)))
            avg_price = max(0, int(request.form.get("avg_price", avg_price)))
        except (TypeError, ValueError):
            flash("숫자만 입력해 주세요.", "error")
        shop = (request.form.get("shop_name") or "").strip()[:30]
        set_setting("monthly_reservations", str(monthly))
        set_setting("avg_price", str(avg_price))
        set_setting("shop_name", shop)

    loss_now = monthly * avg_price * NO_SHOW_BEFORE
    loss_after = monthly * avg_price * NO_SHOW_AFTER
    gain_month = loss_now - loss_after
    gain_year = gain_month * 12
    saved_count = monthly * (NO_SHOW_BEFORE - NO_SHOW_AFTER)
    ind = INDUSTRIES[industry]

    return render_template(
        "index.html",
        monthly=monthly, avg_price=avg_price, shop=shop,
        industry=industry, industries=INDUSTRIES, ind=ind,
        before_pct=int(NO_SHOW_BEFORE * 100),
        after_pct=int(NO_SHOW_AFTER * 100),
        loss_now=loss_now, loss_after=loss_after,
        gain_month=gain_month, gain_year=gain_year,
        saved_count=int(saved_count),
        won=won, manwon=manwon,
        annoy=annoyance_numbers(),
    )


# ---------------------------------------------------------------------------
# 2) 예약 등록
# ---------------------------------------------------------------------------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        phone = clean_phone(request.form.get("phone"))
        dstr = (request.form.get("date") or "").strip()
        tstr = (request.form.get("time") or "").strip()
        service = (request.form.get("service") or "").strip()
        custom_service = (request.form.get("custom_service") or "").strip()
        if service == "__custom__":
            service = custom_service
        staff = ""
        if INDUSTRIES[current_industry()]["ask_staff"]:
            staff = (request.form.get("staff") or "").strip()[:20]
        try:
            price = max(0, int(request.form.get("price") or 0))
        except (TypeError, ValueError):
            price = 0
        note = (request.form.get("note") or "").strip()[:200]

        errors = []
        if not name:
            errors.append("손님 성함을 입력해 주세요.")
        if len(name) > 20:
            errors.append("성함은 20자 이내로 입력해 주세요.")
        if not phone:
            errors.append("연락처를 숫자 10~11자리로 입력해 주세요. (예: 01012345678)")
        try:
            d = datetime.strptime(dstr, "%Y-%m-%d").date()
            if d < date.today():
                errors.append("지난 날짜에는 예약할 수 없어요.")
            if d > date.today() + timedelta(days=90):
                errors.append("예약은 90일 이내까지만 가능해요.")
        except (TypeError, ValueError):
            errors.append("날짜를 올바르게 선택해 주세요.")
        if tstr not in TIME_SLOTS:
            errors.append("시간을 목록에서 선택해 주세요.")
        if not service:
            errors.append("서비스를 선택하거나 직접 입력해 주세요.")
        if len(service) > 40:
            errors.append("서비스명은 40자 이내로 입력해 주세요.")
        if not errors and slot_taken(dstr, tstr):
            errors.append(f"{kdate(dstr)} {ktime(tstr)}은(는) 이미 예약이 있어요. "
                          "다른 시간을 골라주세요.")

        if errors:
            for e in errors:
                flash(e, "error")
        else:
            token = secrets.token_hex(8)
            db = get_db()
            db.execute(
                "INSERT INTO reservations(customer_name, phone, date, time, "
                "service, price, staff, status, token, note, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (name, phone, dstr, tstr, service, price, staff, "pending",
                 token, note, datetime.now().isoformat(timespec="seconds")),
            )
            db.commit()
            flash(f"{name}님 예약이 등록됐어요. "
                  "이제 자동 알림 센터에서 확인 문자를 보내보세요.", "success")
            return redirect(url_for("reservations", view=dstr, fresh=1))

    return render_template(
        "register.html",
        services=industry_services(), slots=TIME_SLOTS,
        today=today_str(), ktime=ktime,
        industry=INDUSTRIES[current_industry()],
        form=request.form if request.method == "POST" else {},
    )


# ---------------------------------------------------------------------------
# 예약 목록 (사장님 관리용)
# ---------------------------------------------------------------------------
@app.route("/reservations")
def reservations():
    view = request.args.get("view", today_str())
    try:
        datetime.strptime(view, "%Y-%m-%d")
    except (TypeError, ValueError):
        view = today_str()
    rows = qall(
        "SELECT * FROM reservations WHERE date=? ORDER BY time",
        (view,),
    )
    return render_template(
        "reservations.html", rows=rows, view=view, kdate=kdate, ktime=ktime,
        won=won, status_label=STATUS_LABEL, status_badge=STATUS_BADGE,
        today=today_str(),
        fresh=(request.args.get("fresh") == "1"),
    )


# ---------------------------------------------------------------------------
# 3) 자동 알림 센터 — 발송 시뮬레이션 + 발송 이력
# ---------------------------------------------------------------------------
def sendable(kind):
    target = tomorrow_str() if kind == "전날" else today_str()
    return qall(
        "SELECT r.* FROM reservations r "
        "LEFT JOIN reminders m ON m.reservation_id=r.id AND m.kind=? "
        "WHERE r.date=? AND r.status IN ('pending','confirmed','changed') "
        "AND m.id IS NULL ORDER BY r.time",
        (kind, target),
    )


@app.route("/reminders", methods=["GET", "POST"])
def reminders():
    if request.method == "POST":
        kind = request.form.get("kind")
        if kind not in ("전날", "당일"):
            flash("알림 종류를 올바르게 선택해 주세요.", "error")
            return redirect(url_for("reminders"))
        targets = sendable(kind)
        if not targets:
            flash(f"지금 보낼 {kind} 알림이 없어요.", "error")
            return redirect(url_for("reminders"))
        db = get_db()
        now = datetime.now().isoformat(timespec="seconds")
        for r in targets:
            msg = reminder_message(r, kind)
            try:
                db.execute(
                    "INSERT INTO reminders(reservation_id, kind, message, sent_at)"
                    " VALUES(?,?,?,?)",
                    (r["id"], kind, msg, now),
                )
            except sqlite3.IntegrityError:
                continue  # 이미 보낸 건 건너뜀
        db.commit()
        flash(f"{kind} 알림 {len(targets)}건을 보냈어요. "
              "(데모라서 실제 문자는 가지 않아요)", "success")
        return redirect(url_for("reminders"))

    pre = sendable("전날")
    today_list = sendable("당일")
    history = qall(
        "SELECT m.*, r.customer_name, r.date, r.time, r.service, r.token "
        "FROM reminders m JOIN reservations r ON r.id=m.reservation_id "
        "ORDER BY m.sent_at DESC, m.id DESC LIMIT 50"
    )
    return render_template(
        "reminders.html", pre=pre, today_list=today_list, history=history,
        kdate=kdate, ktime=ktime, won=won,
        status_label=STATUS_LABEL, status_badge=STATUS_BADGE,
        tomorrow=tomorrow_str(), today=today_str(),
    )


# ---------------------------------------------------------------------------
# 4) 고객 응답 플로우 — 문자 속 링크 화면 시뮬레이션
# ---------------------------------------------------------------------------
@app.route("/r/<token>")
def customer_page(token):
    r = q1("SELECT * FROM reservations WHERE token=?", (token,))
    if not r:
        return render_template("error.html",
                               title="예약 정보를 찾을 수 없어요",
                               message="링크가 잘못됐거나 만료됐어요. "
                                       "가게에 직접 연락해 주세요."), 404
    return render_template(
        "customer.html", r=r, kdate=kdate, ktime=ktime, won=won,
        status_label=STATUS_LABEL,
        shop=get_setting("shop_name", "우리 가게"),
    )


@app.route("/r/<token>/answer", methods=["POST"])
def customer_answer(token):
    r = q1("SELECT * FROM reservations WHERE token=?", (token,))
    if not r:
        abort(404)
    action = request.form.get("action")
    if action not in ("confirm", "cancel"):
        flash("선택이 올바르지 않아요.", "error")
        return redirect(url_for("customer_page", token=token))
    if r["status"] in ("cancelled", "noshow"):
        flash("이미 처리된 예약이에요. 변경이 필요하면 가게에 연락해 주세요.",
              "error")
        return redirect(url_for("customer_page", token=token))
    db = get_db()
    if action == "confirm":
        db.execute("UPDATE reservations SET status='confirmed' WHERE id=?",
                   (r["id"],))
        db.commit()
        return render_template("customer_done.html", r=r, action="confirm",
                               kdate=kdate, ktime=ktime, won=won,
                               shop=get_setting("shop_name", "우리 가게"))
    # cancel
    db.execute("UPDATE reservations SET status='cancelled' WHERE id=?",
               (r["id"],))
    db.commit()
    return render_template("customer_done.html", r=r, action="cancel",
                           kdate=kdate, ktime=ktime, won=won,
                           shop=get_setting("shop_name", "우리 가게"))


# ---------------------------------------------------------------------------
# 5) 시간 변경 — 중복 체크
# ---------------------------------------------------------------------------
@app.route("/r/<token>/change", methods=["GET", "POST"])
def customer_change(token):
    r = q1("SELECT * FROM reservations WHERE token=?", (token,))
    if not r:
        abort(404)
    if r["status"] in ("cancelled", "noshow"):
        return render_template("error.html",
                               title="변경할 수 없는 예약이에요",
                               message="이미 취소됐거나 처리된 예약이에요. "
                                       "가게에 직접 연락해 주세요.")

    if request.method == "POST":
        dstr = (request.form.get("date") or "").strip()
        tstr = (request.form.get("time") or "").strip()
        errors = []
        try:
            d = datetime.strptime(dstr, "%Y-%m-%d").date()
            if d < date.today():
                errors.append("지난 날짜로는 바꿀 수 없어요.")
            if d > date.today() + timedelta(days=90):
                errors.append("90일 이내 날짜만 선택할 수 있어요.")
        except (TypeError, ValueError):
            errors.append("날짜를 올바르게 선택해 주세요.")
        if tstr not in TIME_SLOTS:
            errors.append("시간을 목록에서 선택해 주세요.")
        if dstr == r["date"] and tstr == r["time"]:
            errors.append("지금 예약과 같은 시간이에요. 다른 시간을 골라주세요.")
        if not errors and slot_taken(dstr, tstr, exclude_id=r["id"]):
            errors.append(f"{kdate(dstr)} {ktime(tstr)}은(는) 이미 예약이 찼어요. "
                          "다른 시간을 골라주세요.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            db = get_db()
            db.execute(
                "UPDATE reservations SET date=?, time=?, status='changed' "
                "WHERE id=?", (dstr, tstr, r["id"]),
            )
            db.commit()
            r = q1("SELECT * FROM reservations WHERE id=?", (r["id"],))
            return render_template("customer_done.html", r=r, action="change",
                                   kdate=kdate, ktime=ktime, won=won,
                                   shop=get_setting("shop_name", "우리 가게"))

    return render_template(
        "customer_change.html", r=r, slots=TIME_SLOTS, kdate=kdate,
        ktime=ktime, today=today_str(),
    )


# ---------------------------------------------------------------------------
# 6) 사장님 대시보드
# ---------------------------------------------------------------------------
@app.route("/dashboard", methods=["GET", "POST"])
def dashboard():
    # 노쇼 등록
    if request.method == "POST":
        rid = request.form.get("reservation_id")
        r = q1("SELECT * FROM reservations WHERE id=?", (rid,))
        if not r:
            flash("예약을 찾을 수 없어요.", "error")
        elif r["status"] in ("cancelled", "noshow"):
            flash("이미 처리된 예약이에요.", "error")
        else:
            db = get_db()
            db.execute("UPDATE reservations SET status='noshow' WHERE id=?",
                       (r["id"],))
            row = q1("SELECT * FROM blacklist WHERE phone=?", (r["phone"],))
            if row:
                db.execute(
                    "UPDATE blacklist SET noshow_count=noshow_count+1, "
                    "last_date=?, name=? WHERE phone=?",
                    (r["date"], r["customer_name"], r["phone"]),
                )
            else:
                db.execute(
                    "INSERT INTO blacklist(name, phone, noshow_count, last_date)"
                    " VALUES(?,?,1,?)",
                    (r["customer_name"], r["phone"], r["date"]),
                )
            db.commit()
            flash(f"{r['customer_name']}님이 노쇼로 등록됐어요. "
                  "블랙리스트에서 확인할 수 있어요.", "success")
        return redirect(url_for("dashboard"))

    t = today_str()
    counts = {}
    for st in ("confirmed", "pending", "changed", "cancelled"):
        row = q1("SELECT COUNT(*) c FROM reservations WHERE date=? AND status=?",
                 (t, st))
        counts[st] = row["c"]
    today_rows = qall(
        "SELECT * FROM reservations WHERE date=? ORDER BY time", (t,))

    # 알림 덕분에 지킨 예약: 알림을 보낸 뒤 확정/변경된 예약
    saved = q1(
        "SELECT COUNT(*) c, COALESCE(SUM(r.price),0) s FROM reservations r "
        "WHERE r.status IN ('confirmed','changed') "
        "AND EXISTS (SELECT 1 FROM reminders m "
        "WHERE m.reservation_id=r.id)"
    )

    # 이번 달 노쇼율
    month = t[:7]
    cur = q1(
        "SELECT COUNT(*) total, "
        "SUM(CASE WHEN status='noshow' THEN 1 ELSE 0 END) ns "
        "FROM reservations WHERE substr(date,1,7)=?", (month,)
    )
    cur_total = cur["total"] or 0
    cur_ns = cur["ns"] or 0
    cur_rate = (cur_ns / cur_total * 100) if cur_total else 0

    # 최근 6개월 추이 (시드 + 실제)
    trend = qall("SELECT * FROM monthly_stats ORDER BY month DESC LIMIT 6")
    trend = list(reversed(trend))

    blacklist_top = qall(
        "SELECT * FROM blacklist ORDER BY noshow_count DESC LIMIT 5")

    return render_template(
        "dashboard.html",
        counts=counts, today_rows=today_rows, today=t,
        saved_count=saved["c"] or 0, saved_money=saved["s"] or 0,
        month=month, cur_total=cur_total, cur_ns=cur_ns, cur_rate=cur_rate,
        trend=trend, blacklist_top=blacklist_top,
        kdate=kdate, ktime=ktime, won=won, manwon=manwon,
        status_label=STATUS_LABEL, status_badge=STATUS_BADGE,
        mask_name=mask_name,
    )


# ---------------------------------------------------------------------------
# 7) 노쇼 블랙리스트
# ---------------------------------------------------------------------------
@app.route("/blacklist", methods=["GET", "POST"])
def blacklist():
    if request.method == "POST":
        phone = (request.form.get("phone") or "").strip()
        action = request.form.get("action", "remove")
        row = q1("SELECT * FROM blacklist WHERE phone=?", (phone,))
        if not row:
            flash("목록에 없는 연락처예요.", "error")
        elif action == "remove":
            get_db().execute("DELETE FROM blacklist WHERE phone=?", (phone,))
            get_db().commit()
            flash(f"{row['name']}님을 목록에서 뺐어요.", "success")
        else:
            memo = (request.form.get("memo") or "").strip()[:200]
            get_db().execute("UPDATE blacklist SET memo=? WHERE phone=?",
                             (memo, phone))
            get_db().commit()
            flash("메모를 저장했어요.", "success")
        return redirect(url_for("blacklist"))

    rows = qall("SELECT * FROM blacklist ORDER BY noshow_count DESC, last_date DESC")
    return render_template("blacklist.html", rows=rows, mask_name=mask_name,
                           kdate=kdate)


# ---------------------------------------------------------------------------
# 템플릿 공용
# ---------------------------------------------------------------------------
@app.context_processor
def inject_common():
    return dict(
        status_label=STATUS_LABEL,
        status_badge=STATUS_BADGE,
        shop_name=get_setting("shop_name", "우리 가게"),
        kdate=kdate, ktime=ktime, won=won, manwon=manwon,
        mask_name=mask_name,
    )


# ---------------------------------------------------------------------------
# 리트로핏 — 사장님 심리/바이럴/레거시 기능
# (개발자 용어 없이, 전부 사장님 말로)
# ---------------------------------------------------------------------------

INDUSTRY_EXTRA = {
    "cleaning": {
        "bench_noshow": 18, "fomo_count": 37,
        "dignity": "오늘 {n}가정이 사장님 덕분에 깨끗해졌어요 🧹",
        "meaning": "깨끗한 집에서 시작되는 하루를 만드세요",
        "unit": "건",
    },
    "beauty": {
        "bench_noshow": 12, "fomo_count": 52,
        "dignity": "오늘 {n}분이 사장님 덕분에 예뻐지셨어요 💇",
        "meaning": "손님의 가장 빛나는 순간을 만드세요",
        "unit": "건",
    },
    "moving": {
        "bench_noshow": 15, "fomo_count": 21,
        "dignity": "오늘 {n}가족이 사장님 덕분에 새 출발을 했어요 🚚",
        "meaning": "새로운 시작의 첫 페이지를 함께하세요",
        "unit": "건",
    },
}

PRINCIPLES = [
    ("사람을 사람으로", "손님을 숫자가 아닌 사람으로 대합니다. "
     "화면에 보이는 건 예약 건수가 아니라, 사장님을 믿고 찾아온 얼굴입니다."),
    ("정직", "숫자를 부풀리지 않습니다. 잘 안 된 날은 안 됐다고, "
     "그대로 보여드립니다."),
    ("다크패턴 금지", "속이지 않고, 숨기지 않고, 몰래 켜놓지 않습니다. "
     "사장님이 모르는 사이에 돌아가는 건 없습니다."),
    ("따뜻함", "모든 문구는 사장님 말로 씁니다. "
     "어려운 말은 쓰지 않습니다."),
    ("신뢰", "약속한 건 지킵니다. 문자 한 통도, 시간 하나도 어기지 않습니다."),
    ("겸손", "잘된 건 사장님 덕분입니다. 이 화면의 좋은 숫자는 "
     "사장님이 쌓아온 신뢰의 결과입니다."),
    ("인내", "사장님의 속도에 맞춥니다. 급하게 바꾸라 하지 않고, "
     "천천히 익숙해지도록 돕습니다."),
]


def daily_greeting(now=None):
    """매일 루틴 메시지 — 아침/저녁/월말"""
    now = now or datetime.now()
    if now.day >= 27:
        return "이번 달도 정말 고생 많으셨어요. 사장님 최고예요 🏆"
    if 5 <= now.hour < 12:
        return "좋은 아침이에요. 오늘도 힘내세요 😊"
    if 18 <= now.hour or now.hour < 5:
        return "오늘 하루도 수고하셨어요 🌙"
    return "바쁜 하루 보내고 계시죠? 잠깐 쉬어가세요 ☕"


def calc_value(monthly, avg_price):
    """가치 계산기 — 노쇼율 30% → 5% 개선 시 되찾는 돈"""
    loss_now = monthly * avg_price * NO_SHOW_BEFORE
    loss_after = monthly * avg_price * NO_SHOW_AFTER
    gain_month = loss_now - loss_after
    return {
        "loss_now": loss_now, "loss_after": loss_after,
        "gain_month": gain_month, "gain_year": gain_month * 12,
        "saved_count": int(monthly * (NO_SHOW_BEFORE - NO_SHOW_AFTER)),
    }


def month_stats(month):
    cur = q1(
        "SELECT COUNT(*) total, "
        "SUM(CASE WHEN status='noshow' THEN 1 ELSE 0 END) ns, "
        "COALESCE(SUM(CASE WHEN status IN ('confirmed','changed') "
        "THEN price ELSE 0 END),0) revenue "
        "FROM reservations WHERE substr(date,1,7)=?", (month,))
    total = cur["total"] or 0
    ns = cur["ns"] or 0
    return {"total": total, "noshow": ns, "revenue": cur["revenue"] or 0,
            "rate": (ns / total * 100) if total else 0}


def saved_money(month=None):
    """알림 덕분에 지킨 돈 — 알림을 보내고 확정/변경된 예약의 금액 합"""
    if month:
        row = q1(
            "SELECT COUNT(*) c, COALESCE(SUM(r.price),0) s FROM reservations r "
            "WHERE r.status IN ('confirmed','changed') "
            "AND substr(r.date,1,7)=? "
            "AND EXISTS (SELECT 1 FROM reminders m "
            "WHERE m.reservation_id=r.id)", (month,))
    else:
        row = q1(
            "SELECT COUNT(*) c, COALESCE(SUM(r.price),0) s FROM reservations r "
            "WHERE r.status IN ('confirmed','changed') "
            "AND EXISTS (SELECT 1 FROM reminders m "
            "WHERE m.reservation_id=r.id)")
    return {"count": row["c"] or 0, "money": row["s"] or 0}


def annoyance_numbers():
    """짜증 모먼트용 실제 숫자 — 어제 노쇼 건수 (없으면 이번 주)"""
    y = (date.today() - timedelta(days=1)).isoformat()
    n = q1("SELECT COUNT(*) c FROM reservations WHERE date=? AND status='noshow'",
           (y,))["c"]
    label = "어제"
    if not n:
        mon = (date.today() - timedelta(days=date.today().weekday())).isoformat()
        n = q1("SELECT COUNT(*) c FROM reservations WHERE date>=? AND status='noshow'",
               (mon,))["c"]
        label = "이번 주"
    return {"count": n, "hours": n * 2, "label": label}


# ---------------------------------------------------------------------------
# 8) 사장님 오늘 브리핑 — 도파민 대시보드
# ---------------------------------------------------------------------------
@app.route("/briefing")
def briefing():
    t = today_str()
    industry = current_industry()
    extra = INDUSTRY_EXTRA[industry]

    counts = {}
    for st in ("confirmed", "pending", "changed", "cancelled", "noshow"):
        counts[st] = q1("SELECT COUNT(*) c FROM reservations WHERE date=? AND status=?",
                        (t, st))["c"]
    today_rows = qall("SELECT * FROM reservations WHERE date=? ORDER BY time", (t,))

    month = t[:7]
    ms = month_stats(month)
    saved = saved_money(month)

    # 벤치마크: 우리 동네 평균 노쇼율 vs 사장님 이번 달
    bench = extra["bench_noshow"]

    # 존엄: 오늘 사장님을 믿고 온 사람 수 (돈이 아니라 사람 세기)
    people = q1(
        "SELECT COUNT(DISTINCT phone) c FROM reservations WHERE date=? "
        "AND status IN ('pending','confirmed','changed')", (t,))["c"] or 0

    # 쓰나미 모먼트: 오늘 노쇼 0건
    tsunami = counts["noshow"] == 0 and sum(
        counts[s] for s in ("confirmed", "pending", "changed")) > 0

    # 축하: 오늘 새로 들어온 예약이 있으면
    fresh = q1(
        "SELECT COUNT(*) c FROM reservations WHERE date=? AND status='pending' "
        "AND substr(created_at,1,10)=?", (t, t))["c"] or 0

    return render_template(
        "briefing.html",
        greeting=daily_greeting(), counts=counts, today_rows=today_rows,
        today=t, month=month, ms=ms, saved=saved, bench=bench,
        dignity=extra["dignity"].format(n=people), people=people,
        tsunami=tsunami, fresh=fresh,
        industry=INDUSTRIES[industry], extra=extra,
    )


# ---------------------------------------------------------------------------
# 9) 가치 계산기 + 경쟁사 비교 + ROI
# ---------------------------------------------------------------------------
@app.route("/value", methods=["GET", "POST"])
def value():
    monthly = int(get_setting("monthly_reservations", "200"))
    avg_price = int(get_setting("avg_price", "50000"))
    if request.method == "POST":
        try:
            monthly = max(0, int(request.form.get("monthly", monthly)))
            avg_price = max(0, int(request.form.get("avg_price", avg_price)))
        except (TypeError, ValueError):
            flash("숫자만 입력해 주세요.", "error")
        set_setting("monthly_reservations", str(monthly))
        set_setting("avg_price", str(avg_price))
    calc = calc_value(monthly, avg_price)
    # ROI: 월 3만원으로 지키는 돈
    roi_monthly_fee = 30000
    return render_template(
        "value.html", monthly=monthly, avg_price=avg_price, calc=calc,
        roi_fee=roi_monthly_fee,
        before_pct=int(NO_SHOW_BEFORE * 100),
        after_pct=int(NO_SHOW_AFTER * 100),
        industry=INDUSTRIES[current_industry()],
    )


# ---------------------------------------------------------------------------
# 10) 월간 리포트 — 단톡방 공유
# ---------------------------------------------------------------------------
@app.route("/report")
def report():
    t = today_str()
    month = t[:7]
    ms = month_stats(month)
    saved = saved_money(month)
    industry = current_industry()
    extra = INDUSTRY_EXTRA[industry]
    people_month = q1(
        "SELECT COUNT(DISTINCT phone) c FROM reservations "
        "WHERE substr(date,1,7)=? AND status IN ('confirmed','changed')",
        (month,))["c"] or 0
    share_text = (
        f"[{get_setting('shop_name', '우리 가게')} {int(month[5:])}월 리포트]\n"
        f"• 예약 {ms['total']}건 중 노쇼 {ms['noshow']}건 "
        f"(노쇼율 {ms['rate']:.1f}%)\n"
        f"• 알림 덕분에 지킨 돈: {manwon(saved['money'])} ({saved['count']}건)\n"
        f"• 이번 달 찾아온 손님 {people_month}명\n"
        f"— 노쇼 가드 월간 리포트"
    )
    return render_template(
        "report.html", month=month, ms=ms, saved=saved,
        people_month=people_month, share_text=share_text,
        industry=INDUSTRIES[industry], extra=extra,
    )


# ---------------------------------------------------------------------------
# 11) 성장 — 추천 리워드 + FOMO + 성장 시뮬레이터
# ---------------------------------------------------------------------------
@app.route("/growth")
def growth():
    industry = current_industry()
    extra = INDUSTRY_EXTRA[industry]
    monthly = int(get_setting("monthly_reservations", "200"))
    avg_price = int(get_setting("avg_price", "50000"))
    calc = calc_value(monthly, avg_price)
    referral_text = (
        f"옆 가게 사장님, 노쇼 때문에 속상하셨죠?\n"
        f"저는 '노쇼 가드' 쓰면서 한 달에 {manwon(calc['gain_month'])}를 되찾았어요.\n"
        f"제 소개로 시작하면 우리 둘 다 1개월 무료래요. 한번 써보세요 😊\n"
        f"— {get_setting('shop_name', '우리 가게')} 사장님"
    )
    return render_template(
        "growth.html", extra=extra, industry=INDUSTRIES[industry],
        gain_month=calc["gain_month"], referral_text=referral_text,
    )


# ---------------------------------------------------------------------------
# 12) 소개 — 레거시 철학 + 가치 원칙 7가지 + 사명
# ---------------------------------------------------------------------------
@app.route("/about")
def about():
    industry = current_industry()
    extra = INDUSTRY_EXTRA[industry]
    return render_template(
        "about.html", principles=PRINCIPLES,
        industry=INDUSTRIES[industry], extra=extra,
    )


# ---------------------------------------------------------------------------
# 13) 입소문 배지
# ---------------------------------------------------------------------------
@app.route("/badge")
def badge():
    shop = get_setting("shop_name", "우리 가게")
    snippet = (
        '<a href="#" style="display:inline-block;background:#e7f5ec;color:#0a5f38;'
        'font-weight:800;padding:10px 18px;border-radius:999px;'
        'text-decoration:none;font-size:14px;">'
        f"🛡️ 이 가게는 노쇼 가드 시스템을 사용합니다 — {shop}</a>"
    )
    return render_template("badge.html", snippet=snippet, shop=shop)


# ---------------------------------------------------------------------------
# 실행 — 포트 5009, 충돌 시 자동 전환
# ---------------------------------------------------------------------------
def pick_port(preferred=5009):
    import socket
    for port in range(preferred, preferred + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", port))
                return port
            except OSError:
                continue
    return preferred


if __name__ == "__main__":
    init_db()
    try:
        from seed import seed_all
        seed_all(DB_PATH)
    except Exception:
        pass
    port = pick_port(int(os.environ.get("PORT", "5009")))
    print("=" * 46)
    print("  노쇼 가드 — 빈 예약 시간은 버려지는 돈입니다.")
    print(f"  http://localhost:{port}")
    print("  (종료: Ctrl+C)")
    print("=" * 46)
    app.run(host="0.0.0.0", port=port, debug=False)
