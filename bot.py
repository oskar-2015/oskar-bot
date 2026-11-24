# -*- coding: utf-8 -*-
"""
بوت Oskarix التفاعلي - مستودع مستقل.
لا يمس أي جدول أو تطبيق موجود.
"""

import os
import re
import logging
import secrets
import string
from datetime import datetime, timedelta

import requests
from flask import Flask, request

# ═══════════════════════════════════════════════════════════════
# الإعدادات
# ═══════════════════════════════════════════════════════════════
BOT_TOKEN        = os.environ["TELEGRAM_BOT_TOKEN"]
ADMIN_CHAT_ID    = os.environ.get("TELEGRAM_ADMIN_CHAT_ID", "").strip()
WEBHOOK_URL      = (os.environ.get("WEBHOOK_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "").rstrip("/")
REFERRAL_LINK    = os.environ.get("REFERRAL_LINK", "https://broker-qx.pro/sign-up/?lid=2385298")
VIP_LINK         = os.environ.get("VIP_CHANNEL_LINK", "https://t.me/+LwPYuBaS6OgzM2I0")
PUBLIC_LINK      = os.environ.get("PUBLIC_CHANNEL_LINK", "https://t.me/oskar_1123")
TURSO_URL_RAW    = os.environ["TURSO_DATABASE_URL"].strip()
TURSO_TOKEN      = os.environ["TURSO_AUTH_TOKEN"].strip()
POSTBACK_SECRET  = os.environ.get("POSTBACK_SECRET", "").strip()

TRIAL_HOURS     = 24
PERMANENT_HOURS = 720

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════
# Turso HTTP
# ═══════════════════════════════════════════════════════════════
def _norm_url(u):
    u = u.strip()
    for p in ("libsql://", "wss://", "ws://"):
        if u.startswith(p):
            u = "https://" + u[len(p):]
            break
    if not u.startswith("http"):
        u = "https://" + u
    return u.rstrip("/")

TURSO_BASE = _norm_url(TURSO_URL_RAW)
_http = requests.Session()
_http.headers.update({"Authorization": f"Bearer {TURSO_TOKEN}"})


def turso_execute(sql, params=None):
    args = []
    for p in (params or []):
        if p is None:
            args.append({"type": "null"})
        elif isinstance(p, bool):
            args.append({"type": "integer", "value": str(int(p))})
        elif isinstance(p, int):
            args.append({"type": "integer", "value": str(p)})
        elif isinstance(p, float):
            args.append({"type": "float", "value": p})
        else:
            args.append({"type": "text", "value": str(p)})
    payload = {"requests": [
        {"type": "execute", "stmt": {"sql": sql, "args": args}},
        {"type": "close"},
    ]}
    r = _http.post(f"{TURSO_BASE}/v2/pipeline", json=payload, timeout=20)
    r.raise_for_status()
    result = r.json()["results"][0]
    if result.get("type") == "error":
        raise RuntimeError(result.get("error", {}).get("message", "Turso error"))
    res = result["response"]["result"]
    cols = [c["name"] for c in res.get("cols", [])]
    rows = []
    for row in res.get("rows", []):
        d = {}
        for i, col in enumerate(cols):
            v = row[i]
            t = v.get("type")
            if t == "null":
                d[col] = None
            elif t == "integer":
                d[col] = int(v["value"])
            elif t == "float":
                d[col] = float(v["value"])
            else:
                d[col] = v.get("value")
        rows.append(d)
    return rows


def ensure_bot_users_table():
    """ينشئ جدول bot_users فقط. لا يمس أي جدول آخر."""
    turso_execute("""
        CREATE TABLE IF NOT EXISTS bot_users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            quotex_trader_id TEXT,
            trial_key TEXT,
            permanent_key TEXT,
            has_received_trial INTEGER DEFAULT 0,
            deposit_amount REAL DEFAULT 0,
            status TEXT DEFAULT 'new',
            state TEXT DEFAULT 'idle',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)


def get_bot_user(tid):
    rows = turso_execute("SELECT * FROM bot_users WHERE telegram_id = ?", [int(tid)])
    return rows[0] if rows else None


def get_bot_user_by_trader_id(trader_id):
    rows = turso_execute("SELECT * FROM bot_users WHERE quotex_trader_id = ?", [str(trader_id)])
    return rows[0] if rows else None


def upsert_bot_user(tid, **fields):
    tid = int(tid)
    existing = get_bot_user(tid)
    if not existing:
        turso_execute(
            "INSERT INTO bot_users (telegram_id, status, state) VALUES (?, 'new', 'idle')",
            [tid],
        )
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields.keys())
        vals = list(fields.values()) + [tid]
        turso_execute(f"UPDATE bot_users SET {sets} WHERE telegram_id = ?", vals)


def generate_license_key(hours):
    alphabet = string.ascii_uppercase + string.digits
    parts = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
    key = "OSCAR-" + "-".join(parts)
    expires_at = None
    if hours and hours > 0:
        expires_at = (datetime.utcnow() + timedelta(hours=hours)).isoformat()
    turso_execute(
        "INSERT INTO licenses (key, expires_at, is_active) VALUES (?, ?, 1)",
        [key, expires_at],
    )
    return key, expires_at


def get_license_status(key):
    if not key:
        return "none"
    rows = turso_execute("SELECT expires_at, is_active FROM licenses WHERE key = ?", [key])
    if not rows:
        return "not_found"
    row = rows[0]
    if not row.get("is_active"):
        return "deactivated"
    exp = row.get("expires_at")
    if exp and datetime.utcnow().isoformat() > exp:
        return "expired"
    return "valid"


# ═══════════════════════════════════════════════════════════════
# Telegram API
# ═══════════════════════════════════════════════════════════════
def tg_call(method, **payload):
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
                          json=payload, timeout=15)
        return r.json()
    except Exception as e:
        logger.error(f"tg_call {method} failed: {e}")
        return {"ok": False}


def tg_send(chat_id, text, parse_mode=None, reply_markup=None):
    p = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if parse_mode:
        p["parse_mode"] = parse_mode
    if reply_markup:
        p["reply_markup"] = reply_markup
    return tg_call("sendMessage", **p)


def tg_edit(chat_id, message_id, text, parse_mode=None, reply_markup=None):
    p = {"chat_id": chat_id, "message_id": message_id, "text": text,
         "disable_web_page_preview": True}
    if parse_mode:
        p["parse_mode"] = parse_mode
    if reply_markup:
        p["reply_markup"] = reply_markup
    return tg_call("editMessageText", **p)


def notify_admin(text):
    if ADMIN_CHAT_ID:
        tg_send(ADMIN_CHAT_ID, text, parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════
# كشف النية
# ═══════════════════════════════════════════════════════════════
INTENT_KEYWORDS = {
    "greeting": ["مرحبا", "مرحباً", "اهلا", "أهلا", "السلام", "سلام", "هاي", "هلا",
                 "hello", "hi", "hey", "صباح", "مساء", "صباح الخير"],
    "price": ["بكم", "كم", "سعر", "price", "cost", "ثمن", "تكلفة", "فلوس", "كم سعر", "مجاني"],
    "app_info": ["تطبيق", "app", "oskarix", "اسكاري", "أوسكار", "اوسكار", "برنامج",
                 "الاستراتيجية", "استراتيجية", "مميزات", "features"],
    "key": ["مفتاح", "key", "تفعيل", "activation", "كود", "code", "license", "صلاحية"],
    "register": ["تسجيل", "register", "سجل", "رابط", "link", "انضم", "انضمام", "اشترك", "حساب"],
    "deposit": ["ايداع", "إيداع", "deposit", "أودع", "اودع", "دفع", "تحويل", "فلوس"],
    "profit": ["ربح", "profit", "أرباح", "ارباح", "كسب", "نتائج", "نتيجة", "دخل"],
    "loss": ["خسارة", "loss", "خسرت", "خسر", "خسائر", "خساره", "خطر"],
    "vip": ["vip", "في اي بي", "قروب", "group", "المجموعة", "القناة الخاصة", "خاصة"],
    "help": ["مساعده", "مساعدة", "help", "كيف", "how", "شرح", "اشرح", "وضح", "طريقة"],
    "not_interested": ["لا اريد", "لا أريد", "ما اريد", "not interested", "مو مهتم",
                       "لست مهتم", "لا شكرا", "مايهمني", "ما يهمني"],
    "thanks": ["شكرا", "شكراً", "thanks", "thank", "thx"],
    "trial": ["تجريبي", "تجربة", "trial", "free", "مجانا", "مجاناً"],
    "platform": ["منصة", "quotex", "كووتكس", "كوتكس", "quotes", "broker"],
}


def detect_intent(text):
    text_l = text.lower()
    for intent, keywords in INTENT_KEYWORDS.items():
        for kw in keywords:
            if kw in text_l:
                return intent
    return None


# ═══════════════════════════════════════════════════════════════
# بناء الردود
# ═══════════════════════════════════════════════════════════════
def btn(text, cb=None, url=None):
    b = {"text": text}
    if cb:
        b["callback_data"] = cb
    if url:
        b["url"] = url
    return b


def base_kb(extra=None):
    rows = []
    if extra:
        rows.extend(extra)
    rows.append([btn("🎁 مفتاح تجريبي مجاني", cb="trial_yes")])
    rows.append([btn("🔗 سجل للنسخة الدائمة", url=REFERRAL_LINK)])
    rows.append([btn("ℹ️ ما هو Oskarix؟", cb="about")])
    rows.append([btn("📢 قناتنا العامة", url=PUBLIC_LINK)])
    return {"inline_keyboard": rows}


def build_response(intent, u):
    status = u.get("status", "new")
    has_trial = bool(u.get("has_received_trial"))
    has_perm = bool(u.get("permanent_key"))
    trader_id = u.get("quotex_trader_id")
    name = u.get("first_name") or "صديقي"

    if has_perm:
        return (
            f"أهلاً {name} 👑\n\n"
            f"أنت عضو دائم في Oskarix!\n\n"
            f"🔑 مفتاحك: `{u['permanent_key']}`\n\n"
            f"📢 قناة VIP: {VIP_LINK}",
            {"inline_keyboard": [
                [btn("📢 افتح قناة VIP", url=VIP_LINK)],
                [btn("📱 شرح التطبيق", url="https://t.me/oskar_1123/3635")],
            ]}
        )

    if trader_id and not has_perm:
        return (
            f"أهلاً {name}!\n\n"
            f"✅ Trader ID مسجل: `{trader_id}`\n\n"
            "⏳ في انتظار إيداعك...\n\n"
            "بمجرد اكتشاف الإيداع سيصلك:\n"
            "• المفتاح الدائم لتطبيق Oskarix\n"
            "• رابط قناة VIP",
            {"inline_keyboard": [
                [btn("💳 كيف تودع", cb="how_deposit")],
                [btn("🔗 رابط التسجيل", url=REFERRAL_LINK)],
                [btn("📢 قناتنا العامة", url=PUBLIC_LINK)],
            ]}
        )

    if intent == "greeting":
        if has_trial:
            return (
                f"أهلاً مجدداً {name} 👋\n\n"
                "لقد استلمت مفتاحاً تجريبياً سابقاً.\n\n"
                "🎯 للنسخة الدائمة + قناة VIP:\n"
                f"1️⃣ سجل: {REFERRAL_LINK}\n"
                "2️⃣ أرسل Trader ID هنا\n"
                "3️⃣ أودع، ويصلك كل شيء تلقائياً.",
                base_kb()
            )
        return (
            f"أهلاً وسهلاً {name} في Oskarix 🤖\n\n"
            "أنا هنا لمساعدتك في تجربة أحد أقوى تطبيقات التداول الآلي.\n\n"
            "🎁 احصل على مفتاح تجريبي مجاني 24 ساعة!\n"
            "وبعد أن ترى النتائج، انضم إلينا بشكل دائم مع قناة VIP.",
            base_kb()
        )

    if intent == "price":
        return (
            "💰 التطبيق *مجاني تماماً* للتجربة 24 ساعة!\n\n"
            "للاستفادة الكاملة (مفتاح دائم + قناة VIP):\n"
            "فقط سجّل من رابطنا وأودع *أي مبلغ* في حسابك على Quotex.\n\n"
            "الحساب باسمك، أموالك عندك، أنت من يتحكم 100%.\n"
            "هذا ليس اشتراكاً – هو فتح حساب تداول طبيعي معنا، والتطبيق مجاناً.",
            base_kb()
        )

    if intent == "app_info":
        return (
            "📱 *تطبيق Oskarix – جيل جديد من التداول الآلي*\n\n"
            "✅ 5 استراتيجيات ذكية مدمجة\n"
            "✅ نسخ تلقائي لصفقات الأوسكار\n"
            "✅ إدارة مخاطر متقدمة\n"
            "✅ يعمل 24/7 على السيرفر\n"
            "✅ يعمل على أندرويد\n\n"
            "🎁 جربه مجاناً 24 ساعة الآن!\n"
            "💎 للنسخة الدائمة: سجل + أودع + أرسل Trader ID.",
            base_kb()
        )

    if intent == "key":
        if has_trial:
            return (
                "🔑 لديك مفتاح تجريبي بالفعل.\n\n"
                "للمفتاح *الدائم* + قناة VIP:\n"
                f"1️⃣ سجل: {REFERRAL_LINK}\n"
                "2️⃣ أرسل Trader ID هنا\n"
                "3️⃣ أودع، ويصلك المفتاح تلقائياً.",
                base_kb()
            )
        return (
            "🔑 يمكنك الحصول على مفتاح *تجريبي مجاني* فوراً!\n\n"
            "اضغط الزر أدناه، واحصل على مفتاحك خلال ثوانٍ.\n\n"
            "💎 للمفتاح الدائم: سجّل + أودع + أرسل Trader ID.",
            base_kb()
        )

    if intent == "register":
        return (
            f"🔗 للتسجيل في منصتنا:\n{REFERRAL_LINK}\n\n"
            "خطوات بسيطة:\n"
            "1️⃣ سجّل من الرابط أعلاه\n"
            "2️⃣ وثّق حسابك\n"
            "3️⃣ أرسل Trader ID هنا\n"
            "4️⃣ أودع أي مبلغ\n\n"
            "وسيصلك المفتاح الدائم + رابط VIP تلقائياً! 🎁",
            {"inline_keyboard": [
                [btn("🔗 سجل الآن", url=REFERRAL_LINK)],
                [btn("📹 شرح التسجيل", url="https://t.me/oskar_1123/3343")],
                [btn("🎁 مفتاح تجريبي أولاً", cb="trial_yes")],
            ]}
        )

    if intent == "deposit":
        return (
            "💳 الإيداع في حسابك على Quotex:\n\n"
            "• الحساب باسمك، لا نلمس أموالك.\n"
            "• المبلغ يبقى لك، التطبيق يداول به.\n"
            "• بعد الإيداع سنكتشفه تلقائياً ونرسل المفتاح + VIP.\n\n"
            f"سجّل أولاً إن لم تكن مسجلاً:\n{REFERRAL_LINK}",
            {"inline_keyboard": [
                [btn("🔗 سجل الآن", url=REFERRAL_LINK)],
                [btn("📹 كيف تودع", url="https://t.me/oskar_1123/3343")],
                [btn("🎁 مفتاح تجريبي", cb="trial_yes")],
            ]}
        )

    if intent == "profit":
        return (
            "📈 أرباحك تعتمد على الأداء، لكن تطبيقنا مصمم لتقليل المخاطر.\n\n"
            "5 استراتيجيات مدروسة، وإدارة مخاطر صارمة.\n\n"
            "شاهد نتائج حقيقية في قناتنا العامة 👇\n"
            "ثم جرب مجاناً 24 ساعة وقرر بنفسك.",
            {"inline_keyboard": [
                [btn("📢 شاهد النتائج", url=PUBLIC_LINK)],
                [btn("🎁 جرب 24 ساعة مجاناً", cb="trial_yes")],
                [btn("🔗 سجل للنسخة الدائمة", url=REFERRAL_LINK)],
            ]}
        )

    if intent == "loss":
        return (
            "💡 لا يوجد تداول بدون مخاطر، لكن الفرق هو *إدارة المخاطر*.\n\n"
            "تطبيق Oskarix يحتوي على:\n"
            "• حد أقصى للخسارة\n"
            "• حد أقصى للربح\n"
            "• استراتيجيات استرداد ذكية\n\n"
            "جربه 24 ساعة مجاناً قبل أي التزام.",
            base_kb()
        )

    if intent == "vip":
        return (
            "👑 *قناة VIP – حصرية لأعضاء Oskarix*\n\n"
            "شروط الدخول:\n"
            "• التسجيل من رابطنا\n"
            "• إيداع في حسابك\n"
            "• إرسال Trader ID للتحقق\n\n"
            "بعد التحقق يصلك رابط VIP تلقائياً + مفتاح دائم.",
            {"inline_keyboard": [
                [btn("🔗 سجل الآن", url=REFERRAL_LINK)],
                [btn("🎁 جرب أولاً", cb="trial_yes")],
            ]}
        )

    if intent == "help":
        return (
            "🤝 كيف أساعدك؟\n\n"
            "أخبرني ماذا تريد:\n"
            "• مفتاح تجريبي ➡️ اضغط الزر\n"
            "• معلومات عن التطبيق\n"
            "• كيفية التسجيل\n"
            "• قناة VIP\n\n"
            "أو أرسل أي سؤال، سأجيبك فوراً!",
            base_kb()
        )

    if intent == "not_interested":
        return (
            "أتفهم موقفك 🤝\n\n"
            "لكن اسمح لي باقتراح: جرّب التطبيق *مجاناً* 24 ساعة،\n"
            "بدون أي التزام أو دفع.\n\n"
            "إذا لم يعجبك، لا شيء يخسره أحد.\n"
            "وإذا أعجبك، نكون في انتظارك.",
            {"inline_keyboard": [
                [btn("🎁 حسناً جرب مجاناً", cb="trial_yes")],
                [btn("📢 قناتنا", url=PUBLIC_LINK)],
            ]}
        )

    if intent == "thanks":
        return (
            "العفو 🌟\n\n"
            "هل تريد تجربة التطبيق مجاناً الآن؟\n"
            "أو تحتاج مساعدة في خطوة معينة؟",
            base_kb()
        )

    if intent == "trial":
        if has_trial:
            return (
                "لقد حصلت على مفتاح تجريبي سابقاً 🙂\n\n"
                "للمفتاح الدائم + VIP:\n"
                f"1️⃣ سجل: {REFERRAL_LINK}\n"
                "2️⃣ أرسل Trader ID\n"
                "3️⃣ أودع، ويصلك كل شيء تلقائياً.",
                base_kb()
            )
        return (
            "🎁 رائع! اضغط الزر أدناه لاستلام مفتاحك التجريبي فوراً.\n"
            "صالح 24 ساعة كاملة لتجربة كل ميزات التطبيق.",
            {"inline_keyboard": [
                [btn("🎁 استلم مفتاحي الآن", cb="trial_yes")],
                [btn("📢 قناتنا", url=PUBLIC_LINK)],
            ]}
        )

    if intent == "platform":
        return (
            "📊 نستخدم منصة *Quotex* – منصة تداول عالمية موثوقة.\n\n"
            "• حسابك باسمك\n"
            "• أموالك تحت سيطرتك\n"
            "• سحب في أي وقت\n"
            "• تداول من الهاتف أو الكمبيوتر\n\n"
            f"سجل الآن:\n{REFERRAL_LINK}\n\n"
            "واحصل على مفتاح تجريبي فوراً!",
            base_kb()
        )

    if status == "new":
        return (
            f"أهلاً {name}!\n\n"
            "لم أفهم سؤالك تماماً، لكن اسمح لي بعرض سريع:\n\n"
            "🚀 *Oskarix* – تطبيق تداول آلي احترافي\n"
            "• 5 استراتيجيات\n"
            "• نسخ صفقات الأوسكار\n"
            "• إدارة مخاطر\n\n"
            "🎁 جربه مجاناً 24 ساعة الآن!",
            base_kb()
        )

    return (
        "لم أفهم قصدك تماماً 🤔\n\n"
        "لكن دعني أساعدك:\n\n"
        "• تريد مفتاح تجريبي؟ اضغط الزر\n"
        "• تريد النسخة الدائمة؟ سجل من الرابط وأودع\n"
        "• تريد قناة VIP؟ نفس الخطوات\n\n"
        "أو أرسل سؤالك بشكل أوضح وسأجيبك.",
        base_kb()
    )


# ═══════════════════════════════════════════════════════════════
# المعالجات
# ═══════════════════════════════════════════════════════════════
def handle_start(u, chat_id):
    first_name = u.get("first_name") or "صديقي"

    if u.get("permanent_key"):
        lic = get_license_status(u["permanent_key"])
        if lic == "valid":
            tg_send(chat_id,
                f"أهلاً مجدداً {first_name} 👑\n\n"
                f"أنت عضو دائم في Oskarix!\n\n"
                f"🔑 مفتاحك: `{u['permanent_key']}`\n\n"
                f"📢 قناة VIP: {VIP_LINK}",
                parse_mode="Markdown",
                reply_markup={"inline_keyboard": [
                    [btn("📢 افتح قناة VIP", url=VIP_LINK)],
                    [btn("📊 حالتي", cb="my_status")],
                ]})
            return
        tg_send(chat_id,
            f"أهلاً {first_name}\n\n"
            "⚠️ انتهت صلاحية مفتاحك أو تم إيقافه.\n"
            "تواصل مع الإدارة للتجديد.")
        return

    if u.get("quotex_trader_id"):
        tg_send(chat_id,
            f"أهلاً {first_name} 👋\n\n"
            f"✅ Trader ID مسجل: `{u['quotex_trader_id']}`\n\n"
            "⏳ في انتظار إيداعك...\n\n"
            "بعد اكتشاف الإيداع سيتم إرسال المفتاح الدائم وقناة VIP تلقائياً.",
            parse_mode="Markdown",
            reply_markup={"inline_keyboard": [
                [btn("💳 كيف تودع", cb="how_deposit")],
                [btn("🔗 رابط التسجيل", url=REFERRAL_LINK)],
            ]})
        return

    if u.get("has_received_trial"):
        tg_send(chat_id,
            f"أهلاً مجدداً {first_name} 👋\n\n"
            "لقد استلمت مفتاحك التجريبي سابقاً.\n\n"
            "🎯 *الخطوة التالية:*\n"
            f"1️⃣ سجل من: {REFERRAL_LINK}\n"
            "2️⃣ أرسل Trader ID هنا\n"
            "3️⃣ أودع، ويصلك المفتاح الدائم + VIP",
            parse_mode="Markdown",
            reply_markup={"inline_keyboard": [
                [btn("🔗 سجل الآن", url=REFERRAL_LINK)],
                [btn("📹 شرح التسجيل", url="https://t.me/oskar_1123/3343")],
                [btn("💳 معلومات الإيداع", cb="how_deposit")],
            ]})
        return

    tg_send(chat_id,
        f"أهلاً وسهلاً {first_name} في بوت *Oskarix* 🤖\n\n"
        "🚀 *تطبيق التداول الآلي الاحترافي*\n"
        "• 5 استراتيجيات ذكية\n"
        "• نسخ صفقات الأوسكار\n"
        "• إدارة مخاطر متقدمة\n\n"
        "🎁 *احصل على مفتاح تجريبي مجاني 24 ساعة الآن!*\n\n"
        "أو أرسل لي أي سؤال وسأجيبك فوراً 😊",
        parse_mode="Markdown",
        reply_markup={"inline_keyboard": [
            [btn("🎁 مفتاح تجريبي مجاني", cb="trial_yes")],
            [btn("ℹ️ ما هو Oskarix؟", cb="about")],
            [btn("🔗 سجل للنسخة الدائمة", url=REFERRAL_LINK)],
            [btn("📢 قناتنا العامة", url=PUBLIC_LINK)],
        ]})


def handle_trader_id(u, chat_id, text):
    tid = u["telegram_id"]
    existing = u.get("quotex_trader_id")

    if existing:
        if existing == text:
            tg_send(chat_id,
                f"✅ Trader ID `{text}` مسجل بالفعل.\n\n"
                "بانتظار إيداعك...",
                parse_mode="Markdown")
        else:
            tg_send(chat_id,
                f"لديك Trader ID مسجل: `{existing}`\n\n"
                "للتغيير أرسل /cancel ثم الرقم الجديد.",
                parse_mode="Markdown")
        return

    other = get_bot_user_by_trader_id(text)
    if other and other["telegram_id"] != tid:
        tg_send(chat_id,
            "⚠️ هذا Trader ID مسجل لحساب آخر.\n"
            "تأكد من الرقم أو تواصل مع الإدارة.")
        notify_admin(
            f"⚠️ تكرار Trader ID: <code>{text}</code>\n"
            f"User1: {other['telegram_id']}\nUser2: {tid}")
        return

    upsert_bot_user(tid, quotex_trader_id=text, status="registered")
    tg_send(chat_id,
        "✅ *تم استلام Trader ID بنجاح!*\n\n"
        f"`{text}`\n\n"
        "🎯 *الخطوة الأخيرة:*\n"
        "أودع الآن في حسابك على Quotex.\n\n"
        "🔔 بمجرد اكتشاف الإيداع (خلال دقائق):\n"
        "• يصلك المفتاح الدائم تلقائياً\n"
        "• رابط قناة VIP\n\n"
        f"إذا لم تكن سجلت بعد: {REFERRAL_LINK}",
        parse_mode="Markdown",
        reply_markup={"inline_keyboard": [
            [btn("💳 كيف تودع", cb="how_deposit")],
            [btn("🔗 رابط التسجيل", url=REFERRAL_LINK)],
        ]})
    notify_admin(
        f"📝 <b>Trader ID جديد</b>\n"
        f"User: <code>{tid}</code>\n"
        f"Name: {u.get('first_name')}\n"
        f"Username: @{u.get('username') or 'N/A'}\n"
        f"Trader: <code>{text}</code>\n"
        f"⏳ بانتظار الإيداع...")


def handle_message(msg):
    chat_id = msg["chat"]["id"]
    user = msg.get("from", {})
    tid = user.get("id")
    if not tid:
        return
    text = (msg.get("text") or "").strip()
    first_name = user.get("first_name", "")
    username = user.get("username", "")

    u = get_bot_user(tid)
    if not u:
        upsert_bot_user(tid, username=username, first_name=first_name)
        u = get_bot_user(tid)
    else:
        if username != u.get("username") or first_name != u.get("first_name"):
            upsert_bot_user(tid, username=username, first_name=first_name)
            u = get_bot_user(tid)

    if text.startswith("/start"):
        return handle_start(u, chat_id)

    if text.startswith("/cancel"):
        if u.get("status") == "registered" and not u.get("permanent_key"):
            upsert_bot_user(tid, quotex_trader_id=None,
                            status="trial" if u.get("has_received_trial") else "new")
            tg_send(chat_id, "✅ تم إلغاء Trader ID. أرسل الرقم الصحيح عندما تكون جاهزاً.")
        else:
            tg_send(chat_id, "لا يمكن الإلغاء في حالتك الحالية.")
        return

    if text.startswith("/help"):
        r = build_response("help", u)
        return tg_send(chat_id, r[0], parse_mode="Markdown", reply_markup=r[1])

    if text.startswith("/status"):
        lines = [f"📊 *حالتك:*\n"]
        lines.append(f"• الحالة: `{u.get('status', 'new')}`")
        if u.get("trial_key"):
            lines.append(f"• مفتاح تجريبي: `{u['trial_key']}`")
        if u.get("permanent_key"):
            lines.append(f"• مفتاح دائم: `{u['permanent_key']}`")
        if u.get("quotex_trader_id"):
            lines.append(f"• Trader ID: `{u['quotex_trader_id']}`")
        if u.get("deposit_amount"):
            lines.append(f"• إجمالي الإيداع: ${u['deposit_amount']}")
        return tg_send(chat_id, "\n".join(lines), parse_mode="Markdown",
                       reply_markup=base_kb())

    if text.isdigit() and 4 <= len(text) <= 12:
        return handle_trader_id(u, chat_id, text)

    intent = detect_intent(text)
    response_text, buttons = build_response(intent, u)
    tg_send(chat_id, response_text, parse_mode="Markdown", reply_markup=buttons)


def handle_callback(cq):
    tg_call("answerCallbackQuery", callback_query_id=cq["id"])
    tid = cq["from"]["id"]
    chat_id = cq["message"]["chat"]["id"]
    message_id = cq["message"]["message_id"]
    data = cq.get("data", "")

    u = get_bot_user(tid)
    if not u:
        upsert_bot_user(tid, first_name=cq["from"].get("first_name", ""),
                        username=cq["from"].get("username", ""))
        u = get_bot_user(tid)

    if data == "trial_yes":
        if u.get("has_received_trial"):
            tg_edit(chat_id, message_id,
                "لقد استلمت مفتاحاً تجريبياً سابقاً 🙂\n\n"
                "للمفتاح الدائم:\n"
                f"1️⃣ سجل: {REFERRAL_LINK}\n"
                "2️⃣ أرسل Trader ID\n"
                "3️⃣ أودع، ويصلك المفتاح + VIP",
                reply_markup={"inline_keyboard": [
                    [btn("🔗 سجل الآن", url=REFERRAL_LINK)],
                ]})
            return
        try:
            key, _ = generate_license_key(TRIAL_HOURS)
        except Exception as e:
            logger.error(f"trial gen failed: {e}")
            tg_edit(chat_id, message_id, "حدث خطأ مؤقت. حاول بعد لحظات.")
            return
        upsert_bot_user(tid, has_received_trial=1, trial_key=key, status="trial")
        tg_edit(chat_id, message_id,
            "🎉 *تم إنشاء مفتاحك التجريبي!*\n\n"
            f"`{key}`\n\n"
            "📋 *طريقة الاستخدام:*\n"
            "1. حمل تطبيق Oskarix\n"
            "2. افتح التطبيق\n"
            "3. الصق المفتاح عند الطلب\n\n"
            "✅ صالح لمدة 24 ساعة كاملة\n\n"
            "💎 *للمفتاح الدائم + قناة VIP:*\n"
            f"1️⃣ سجل: {REFERRAL_LINK}\n"
            "2️⃣ أرسل Trader ID هنا\n"
            "3️⃣ أودع، ويصلك كل شيء تلقائياً",
            parse_mode="Markdown",
            reply_markup={"inline_keyboard": [
                [btn("🔗 سجل للنسخة الدائمة", url=REFERRAL_LINK)],
                [btn("📹 شرح التطبيق", url="https://t.me/oskar_1123/3635")],
            ]})
        notify_admin(
            f"🎁 <b>مفتاح تجريبي جديد</b>\n"
            f"User: <code>{tid}</code>\n"
            f"Name: {u.get('first_name')}\n"
            f"Key: <code>{key}</code>")

    elif data == "about":
        tg_edit(chat_id, message_id,
            "📱 *Oskarix – التداول الآلي باحترافية*\n\n"
            "✨ *المميزات:*\n"
            "• 5 استراتيجيات ذكية\n"
            "• نسخ تلقائي لصفقات الأوسكار\n"
            "• إدارة مخاطر (حد ربح/خسارة)\n"
            "• يعمل 24/7 بدون توقف\n"
            "• يعمل على أندرويد\n\n"
            "🎯 *كيف يعمل؟*\n"
            "تربط حسابك على Quotex بالتطبيق، والتطبيق يتداول بدلاً عنك.\n\n"
            "🎁 *جربه مجاناً 24 ساعة!*\n"
            "💎 *للنسخة الدائمة: سجل + أودع + أرسل Trader ID*",
            parse_mode="Markdown",
            reply_markup={"inline_keyboard": [
                [btn("🎁 مفتاح تجريبي", cb="trial_yes")],
                [btn("🔗 سجل للنسخة الدائمة", url=REFERRAL_LINK)],
                [btn("📹 شاهد الشرح", url="https://t.me/oskar_1123/3635")],
            ]})

    elif data == "how_deposit":
        tg_edit(chat_id, message_id,
            "💳 *كيف تودع في Quotex:*\n\n"
            "1. سجل من رابطنا\n"
            "2. افتح حسابك\n"
            "3. اختر (إيداع)\n"
            "4. اختر طريقة الدفع\n"
            "5. أدخل المبلغ وأكمل\n\n"
            "⏱️ الإيداع عادة فوري\n"
            "🔔 بعد الإيداع سيصلك المفتاح تلقائياً\n\n"
            f"🔗 رابط التسجيل:\n{REFERRAL_LINK}",
            parse_mode="Markdown",
            reply_markup={"inline_keyboard": [
                [btn("🔗 سجل الآن", url=REFERRAL_LINK)],
                [btn("📹 شرح كامل", url="https://t.me/oskar_1123/3343")],
            ]})

    elif data == "my_status":
        text = build_response(None, u)[0]
        tg_edit(chat_id, message_id, text, parse_mode="Markdown",
                reply_markup=base_kb())


# ═══════════════════════════════════════════════════════════════
# Flask
# ═══════════════════════════════════════════════════════════════
flask_app = Flask(__name__)


@flask_app.route("/")
@flask_app.route("/health")
def health():
    return "OK", 200


@flask_app.route(f"/webhook/{BOT_TOKEN}", methods=["POST"])
def webhook():
    try:
        update = request.get_json(force=True, silent=True) or {}
        if "message" in update:
            handle_message(update["message"])
        elif "callback_query" in update:
            handle_callback(update["callback_query"])
    except Exception as e:
        logger.error(f"webhook error: {e}", exc_info=True)
    return "OK", 200


@flask_app.route("/postback/quotex", methods=["GET", "POST"])
def quotex_postback():
    if POSTBACK_SECRET:
        provided = request.args.get("secret")
        if not provided and request.is_json:
            provided = (request.get_json(silent=True) or {}).get("secret")
        if provided != POSTBACK_SECRET:
            return {"error": "unauthorized"}, 401

    data = request.get_json(silent=True) if request.is_json else request.values.to_dict()
    data = data or {}
    logger.info(f"postback: {data}")

    status = (data.get("status") or "").lower()
    trader_id = str(data.get("trader_id") or data.get("uid") or "").strip()
    try:
        deposit_amount = float(data.get("sumdep") or data.get("payout") or 0)
    except (TypeError, ValueError):
        deposit_amount = 0.0

    if not trader_id:
        return {"ok": True, "note": "no trader_id"}, 200

    u = get_bot_user_by_trader_id(trader_id)
    if not u:
        notify_admin(
            f"⚠️ Postback trader غير معروف: <code>{trader_id}</code>\n"
            f"Status: {status}\nAmount: ${deposit_amount}")
        return {"ok": True}, 200

    tid = u["telegram_id"]

    if deposit_amount > 0 or status in ("ftd", "dep", "deposit"):
        if u.get("permanent_key"):
            new_total = float(u.get("deposit_amount") or 0) + deposit_amount
            upsert_bot_user(tid, deposit_amount=new_total)
            tg_send(tid,
                f"💰 *إيداع إضافي مستلم*\n\n"
                f"المبلغ: *${deposit_amount}*\n"
                f"إجمالي إيداعاتك: *${new_total}*\n\n"
                "شكراً لثقتك! 🚀",
                parse_mode="Markdown")
            notify_admin(
                f"💰 إيداع إضافي\n"
                f"User: {tid}\n"
                f"Amount: ${deposit_amount}\nTotal: ${new_total}")
        else:
            try:
                key, _ = generate_license_key(PERMANENT_HOURS)
            except Exception as e:
                logger.error(f"key gen failed: {e}")
                notify_admin(f"❌ فشل إنشاء مفتاح لـ {tid}: {e}")
                return {"ok": False}, 500
            upsert_bot_user(tid, permanent_key=key,
                            deposit_amount=deposit_amount, status="active")
            tg_send(tid,
                f"🎉 *مبروك يا {u.get('first_name', 'صديقي')}!*\n\n"
                f"✅ تم اكتشاف إيداعك بمبلغ: *${deposit_amount}*\n\n"
                f"🔑 *مفتاحك الدائم:*\n`{key}`\n\n"
                f"📢 *قناة VIP:*\n{VIP_LINK}\n\n"
                "مرحباً بك في العائلة! 🚀\n"
                "التطبيق الآن جاهز للعمل من أجلك.",
                parse_mode="Markdown",
                reply_markup={"inline_keyboard": [
                    [btn("📢 افتح قناة VIP", url=VIP_LINK)],
                    [btn("📱 حمل التطبيق", url="https://t.me/oskar_1123/3635")],
                ]})
            notify_admin(
                f"✅ <b>إيداع جديد - مفتاح دائم</b>\n"
                f"User: <code>{tid}</code>\n"
                f"Name: {u.get('first_name')}\n"
                f"Username: @{u.get('username') or 'N/A'}\n"
                f"Trader: <code>{trader_id}</code>\n"
                f"Amount: <b>${deposit_amount}</b>\n"
                f"Key: <code>{key}</code>")
    else:
        notify_admin(
            f"📋 Postback\n"
            f"Trader: {trader_id}\n"
            f"Status: {status}\n"
            f"Amount: ${deposit_amount}")

    return {"ok": True}, 200


# ═══════════════════════════════════════════════════════════════
# Startup
# ═══════════════════════════════════════════════════════════════
def _startup():
    try:
        me = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getMe", timeout=10).json()
        if me.get("ok"):
            logger.info(f"✅ Bot: @{me['result']['username']}")
        else:
            logger.error(f"❌ Invalid bot token: {me}")
    except Exception as e:
        logger.error(f"verify bot failed: {e}")

    try:
        ensure_bot_users_table()
        logger.info("✅ bot_users table ready")
    except Exception as e:
        logger.error(f"❌ ensure_bot_users_table failed: {e}")

    if WEBHOOK_URL:
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
                json={"url": f"{WEBHOOK_URL}/webhook/{BOT_TOKEN}",
                      "drop_pending_updates": True},
                timeout=15,
            )
            logger.info(f"setWebhook: {r.json()}")
        except Exception as e:
            logger.error(f"setWebhook failed: {e}")
    else:
        logger.warning("WEBHOOK_URL/RENDER_EXTERNAL_URL غير مضبوط")


_startup()


if __name__ == "__main__":
    flask_app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
