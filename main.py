import telebot
import subprocess
import os
import shutil
import threading
import time
import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
import re
import html
import json
import urllib.parse
import requests

import telebot.apihelper as apihelper
apihelper.READ_TIMEOUT = 120
apihelper.CONNECT_TIMEOUT = 30

#import logging
#telebot.logger.setLevel(logging.DEBUG)

# ---- NEW: v3 and v4 are both installed as a top-level package literally
# named `lncrawl`, so a given server only ever has ONE of them on disk
# (whichever `lightnovel-crawler==X` was pip-installed in that image).
# Importing either one at module load time would crash on servers running
# the other version. Both are imported lazily instead, only from inside
# the v3-specific / v4-specific functions below. ----

# ---------------------- CONFIG ----------------------
TOKEN = os.environ.get("BOT_TOKEN", "")
if not TOKEN:
    raise SystemExit("BOT_TOKEN env var is missing.")
MY_GROUP_ID = "@novelsFinder"
BOREAL_PRIVATE_GROUP_ID = -1003389806855
BOREAL_OTHER_GROUP = "@selesste"
OTHER_GROUP_ID = "@patiorjunrick1"
BLOCKED_GROUP_USERNAME = "@novelsFinder"
ADMIN_USER_ID = [7994985826,  8402824940, 7378945311]
UPLOAD_RETRY_DELAY_MINUTES = 10
UPLOAD_RETRY_KEEP_HOURS = 12
UPLOAD_RETRY_SECOND_DELAY_MINUTES = 20
PENDING_UPLOAD_DIR = "./pending_epub_uploads"
PROGRESS_UPDATE_INTERVAL = 7  # seconds
PREVIEW_RETRY_SERVER = "s4"
USER_RETRY_WINDOW_HOURS = 20
USER_RETRY_SLOT_LIMIT = 3


# Seeded only once; admins update this catalog with /addsource and /delsource.
DEFAULT_SOURCE_DIRECTORY = [
    ("novelarrow.com", ""), ("royalroad.com", ""), ("wattpad.com", "(stable)"),
    ("allnovel.org", ""), ("sonicmtl.com", ""), ("fanmtl.com", ""),
    ("novgo.net", ""), ("novlove.com", ""), ("novelfire.net", ""),
    ("wuxiabox.com", ""), ("novelight.net", ""), ("asianovel.net", ""),
    ("novel543.com", ""), ("katreadingcafe.com", ""), ("libread.com", ""),
    ("novelcool.com", ""), ("novelfull.net", ""),
    ("chickengege.org", "(books that require passwords will probably fail)"),
    ("webnovel.com", "--(for paid chapters use /webtoepub after paying)"),
    ("chereads.com", "(only free chapters will be fully downloaded, for paid chapters use /webtoepub after paying)"),
    ("freewebnovel.com", "(unstable, final epub might have missing chapters)"),
    ("scribblehub.com", "--download 10 to 50 chapters at a time or your download might fail(unstable, final epub might have missing chapters)"),
    ("novelhall.com", "(unstable, might stop working)"),
    ("novelhi.com", "(unstable, might stop working)"),
]
#SERVER_ID = "s4"
SERVER_ID = os.getenv("SERVER_ID")
if not SERVER_ID:
    SERVER_ID = f"worker_{os.getpid()}"  # unique per process

import uuid
RUN_ID = str(uuid.uuid4())  # unique per process instance, changes on every restart
class _NeverReject(dict):
    """Drop-in replacement for lncrawl's ctx.sources.rejected. Silently
    discards anything written to it, so a host that fails once is never
    permanently cached as rejected — every future attempt (whether a
    later chapter in the same task, or a retry from s4/process_preview_loop)
    gets a genuine fresh network attempt instead of an instant cached failure."""
    def __setitem__(self, key, value):
        pass
    def __contains__(self, key):
        return False
    
if SERVER_ID in ("s1", "s2", "s10"):
    ALLOWED_CRAWLER = "v3"
else:
    ALLOWED_CRAWLER = "v4"

# ---------------------- DATABASE ----------------------
DB_HOST = "primary.final-lightnovel-bot-task-queue--87srfx6hrv5l.addon.code.run"
DB_PORT = 29301
DB_NAME = "_4f9e4d342580"
DB_USER = "_c676b0c0215f5654"
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")

def get_db_connection():
    try:
        conn = psycopg2.connect(
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            cursor_factory=RealDictCursor,
            sslmode="require",
            connect_timeout=10,
            options="-c statement_timeout=15000 -c lock_timeout=10000 -c idle_in_transaction_session_timeout=30000"
        )
        return conn
    except Exception as e:
        print(f"[DB ERROR] Could not connect: {e}")
        return None
    
def setup_tasks_table():
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id SERIAL PRIMARY KEY,
                    chat_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    url TEXT NOT NULL,
                    chapter_range TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    crawler_version TEXT DEFAULT 'v3',
                    server_id TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    completed_at TIMESTAMP                                      
                );
            """)

            cur.execute("""
                ALTER TABLE tasks
                ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0
            """)
            cur.execute("""
                UPDATE tasks
                SET retry_count = 0
                WHERE retry_count IS NULL
            """)
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS username TEXT;")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS first_name TEXT;")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS last_name TEXT;")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS novel_title TEXT;")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS total_chapters INT;")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS run_id TEXT;")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS last_progress_at TIMESTAMP;")
            cur.execute(
                "ALTER TABLE tasks "
                "ADD COLUMN IF NOT EXISTS retry_mode TEXT NOT NULL DEFAULT 'fast';"
            )
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS is_user_retry BOOLEAN NOT NULL DEFAULT FALSE;")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS retry_server_id TEXT;")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS retry_expires_at TIMESTAMP;")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS missing_chapters INT NOT NULL DEFAULT 0;")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS retry_attempts INT NOT NULL DEFAULT 0;")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS cache_novel_id TEXT;")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_retry_slots (
                    user_id BIGINT NOT NULL,
                    task_id INT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    first_retry_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (user_id, task_id)
                );
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS user_retry_slots_window_idx
                ON user_retry_slots (user_id, first_retry_at);
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS tasks_pending_picker_idx
                ON tasks (crawler_version, id)
                WHERE status = 'pending';
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS tasks_user_fairness_idx
                ON tasks (user_id, created_at)
                WHERE status IN ('done', 'running');
            """)

            conn.commit()
            print("[DB] tasks table ready")
    except Exception as e:
        print(f"[DB SETUP ERROR] {e}")
    finally:
        conn.close()

def setup_preview_table():
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS preview_requests (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    chat_id BIGINT,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    url TEXT NOT NULL,
                    version TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    title TEXT,
                    volume_count INT,
                    chapter_count INT,
                    error TEXT,
                    server_id TEXT,
                    retry_count INT DEFAULT 0,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)
            cur.execute("ALTER TABLE preview_requests ADD COLUMN IF NOT EXISTS username TEXT;")
            cur.execute("ALTER TABLE preview_requests ADD COLUMN IF NOT EXISTS first_name TEXT;")
            cur.execute("ALTER TABLE preview_requests ADD COLUMN IF NOT EXISTS last_name TEXT;")
            cur.execute("ALTER TABLE preview_requests ADD COLUMN IF NOT EXISTS retry_count INT DEFAULT 0;")
            cur.execute("ALTER TABLE preview_requests ADD COLUMN IF NOT EXISTS run_id TEXT;")
            cur.execute("ALTER TABLE preview_requests ADD COLUMN IF NOT EXISTS retry_server_id TEXT;")

            conn.commit()
            print("[DB] preview_requests table ready")
    except Exception as e:
        print(f"[DB SETUP ERROR] preview_requests: {e}")
    finally:
        conn.close()

def setup_server_table():
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS servers (
                    server_id TEXT PRIMARY KEY,
                    last_heartbeat TIMESTAMP,
                    status TEXT DEFAULT 'online'
                );
            """)
            cur.execute("ALTER TABLE servers ADD COLUMN IF NOT EXISTS run_id TEXT;")
            conn.commit()
            print("[DB] tasks table ready")
    except Exception as e:
        print(f"[DB SETUP ERROR] {e}")
    finally:
        conn.close()

def setup_user_controls_table():
    conn = get_db_connection()
    if not conn:
        return

    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS user_controls (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    is_banned BOOLEAN NOT NULL DEFAULT FALSE,
                    daily_task_limit INTEGER,
                    updated_by BIGINT,
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    CHECK (daily_task_limit IS NULL OR daily_task_limit >= 1)
                );
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS user_controls_username_idx
                ON user_controls (LOWER(username));
            """)
            conn.commit()
            print("[DB] user_controls table ready")
    except Exception as error:
        print(f"[DB SETUP ERROR] user_controls: {error}")
    finally:
        conn.close()


def setup_pending_uploads_table():
    conn = get_db_connection()
    if not conn:
        return

    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pending_uploads (
                    id SERIAL PRIMARY KEY,
                    task_id INTEGER UNIQUE NOT NULL,
                    chat_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    server_id TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    caption TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_retry_at TIMESTAMP NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    expires_at TIMESTAMP NOT NULL,
                    sent_at TIMESTAMP,
                    error TEXT
                );
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS pending_uploads_retry_idx
                ON pending_uploads (server_id, next_retry_at)
                WHERE status = 'pending';
            """)
            cur.execute("""
                ALTER TABLE pending_uploads
                ADD COLUMN IF NOT EXISTS gofile_url TEXT;
            """)
            cur.execute("""
                ALTER TABLE pending_uploads
                ADD COLUMN IF NOT EXISTS buzzheavier_url TEXT;
            """)
            cur.execute("""
                ALTER TABLE pending_uploads
                ADD COLUMN IF NOT EXISTS external_status TEXT NOT NULL DEFAULT 'pending';
            """)
            cur.execute("""
                ALTER TABLE pending_uploads
                ADD COLUMN IF NOT EXISTS external_error TEXT;
            """)

            conn.commit()
            print("[DB] pending_uploads table ready")
    except Exception as error:
        print(f"[DB SETUP ERROR] pending_uploads: {error}")
    finally:
        conn.close()

def setup_site_management_tables():
    """Create the shared admin-managed source, ban, and image-rule tables."""
    conn = get_db_connection()
    if not conn:
        return

    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL,
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS site_rules (
                    domain TEXT PRIMARY KEY,
                    ban_reason TEXT,
                    ignore_chapter_images BOOLEAN NOT NULL DEFAULT FALSE,
                    created_by BIGINT,
                    updated_by BIGINT,
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS source_directory (
                    domain TEXT PRIMARY KEY,
                    note TEXT NOT NULL DEFAULT '',
                    sort_order INTEGER NOT NULL DEFAULT 0,
                    created_by BIGINT,
                    updated_by BIGINT,
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
            """)

            # Import the original hard-coded catalog only once. Later admin edits
            # must remain intact, including deliberate removals.
            cur.execute(
                "SELECT 1 FROM bot_settings WHERE setting_key = 'source_directory_seeded'"
            )
            if not cur.fetchone():
                for order, (domain, note) in enumerate(DEFAULT_SOURCE_DIRECTORY, start=1):
                    cur.execute("""
                        INSERT INTO source_directory (domain, note, sort_order)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (domain) DO NOTHING;
                    """, (domain, note, order))
                cur.execute("""
                    INSERT INTO bot_settings (setting_key, setting_value)
                    VALUES ('source_directory_seeded', '1');
                """)

            conn.commit()
            print("[DB] site management tables ready")
    except Exception as error:
        conn.rollback()
        print(f"[DB SETUP ERROR] site management: {error}")
    finally:
        conn.close()
conn = get_db_connection()
if conn:
    print("[DB] Connected successfully")
    conn.close()
    setup_tasks_table()
    setup_preview_table()
    setup_server_table()
    setup_user_controls_table()
    setup_pending_uploads_table()
    setup_site_management_tables()
else:
    print("[DB] Connection failed")
# ---------------------- BOT SETUP ----------------------
class BotExceptionHandler(telebot.ExceptionHandler):
    def handle(self, exception):
        print(f"[HANDLER EXCEPTION] {exception}")
        return True  # tells telebot the exception was handled — worker thread survives

bot = telebot.TeleBot(TOKEN, num_threads=20, exception_handler=BotExceptionHandler())

running_processes = {}  # task_id -> threading.Event (v3 & v4 cancel signal)
running_lock = threading.Lock()  # <--- add this right here
pending_prompts = {}  # user_id -> pending bot message
last_bot_messages = {}  # user_id -> list of final messages
pending_urls = {}
pending_retry_urls = {}  # user_id -> chat_id; the next message must be the completed task URL
pending_cancel_tasks = {}  # user_id -> ordered list of cancelable tasks, expires after 10 minutes

pending_range_state = {}  # user_id -> {token, chat_id, url, version, title, total_chapters, user_id, username, first_name, last_name}


# ---------------------- HELPER FUNCTIONS ----------------------

def is_v4_banned_source(url: str) -> bool:
    url = url.lower()
    banned = [
        "gamesareking",
        "iloveyou"

    ]
    return any(b in url for b in banned)

def is_v3_broken_source(url: str) -> bool:
    url = url.lower()
    broken = [
        "m.webnovel.com"
    ]
    return any(b in url for b in broken)

notified_dead_servers = set()


def normalize_site_domain(value: str):
    """Return a normalized hostname suitable for site-rule commands."""
    value = (value or "").strip().lower()
    if not value:
        return None

    parsed = urllib.parse.urlsplit(value if "://" in value else f"//{value}")
    host = (parsed.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return host if "." in host else None


def get_url_host(url: str):
    return normalize_site_domain(url)


def url_matches_domain(url: str, domain: str) -> bool:
    host = get_url_host(url)
    domain = normalize_site_domain(domain)
    return bool(host and domain and (host == domain or host.endswith("." + domain)))


def find_banned_site(url: str):
    conn = get_db_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT domain, ban_reason
                FROM site_rules
                WHERE ban_reason IS NOT NULL AND BTRIM(ban_reason) <> '';
            """)
            matches = [row for row in cur.fetchall() if url_matches_domain(url, row["domain"])]
            return max(matches, key=lambda row: len(row["domain"])) if matches else None
    except Exception as error:
        print(f"[SITE RULE ERROR] ban lookup: {error}")
        return None
    finally:
        conn.close()


def should_ignore_chapter_images(url: str) -> bool:
    conn = get_db_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT domain
                FROM site_rules
                WHERE ignore_chapter_images = TRUE;
            """)
            return any(url_matches_domain(url, row["domain"]) for row in cur.fetchall())
    except Exception as error:
        print(f"[SITE RULE ERROR] image-rule lookup: {error}")
        return False
    finally:
        conn.close()


def get_source_directory():
    conn = get_db_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT domain, note
                FROM source_directory
                ORDER BY sort_order ASC, domain ASC;
            """)
            return cur.fetchall()
    except Exception as error:
        print(f"[SOURCE DIRECTORY ERROR] {error}")
        return []
    finally:
        conn.close()


def is_blocked_group(message):
    return (
        message.chat.type in ["group", "supergroup"]
        and message.chat.username == BLOCKED_GROUP_USERNAME.replace("@", "")
    )
def safe_send_message(chat_id, text, parse_mode=None):
    try:
        return bot.send_message(
            chat_id,
            text,
            parse_mode=parse_mode,
            link_preview_options=telebot.types.LinkPreviewOptions(
                is_disabled=True
            ),
        )
    except telebot.apihelper.ApiTelegramException as e:
        if e.error_code == 429:
            retry_after = e.result_json.get("parameters", {}).get("retry_after", 5)
            print(f"[RATE LIMIT] chat_id={chat_id}: sleeping {retry_after}s")
            time.sleep(min(retry_after, 30))
        else:
            print(f"[SEND MESSAGE ERROR] chat_id={chat_id}: {e}")
        return None
    except Exception as e:
        print(f"[SEND MESSAGE ERROR] {e}")
        return None
    
def send_prompt(chat_id, text, user_id):
    msg = safe_send_message(chat_id, text)
    if msg:
        pending_prompts[user_id] = msg
    return msg

def send_temp_message(chat_id, text, user_id, is_final=False, parse_mode=None):
    msg = safe_send_message(chat_id, text, parse_mode=parse_mode)
    if not msg:
        return None
    if user_id not in last_bot_messages:
        last_bot_messages[user_id] = []
    last_bot_messages[user_id].append((chat_id, msg.message_id, is_final))
    if not is_final:
        delete_last_bot_message_later(user_id, msg.message_id)
    return msg

def send_task_message(chat_id, text, user_id, is_final=False, parse_mode=None):
    msg = safe_send_message(chat_id, text, parse_mode=parse_mode)
    if not msg:
        return None
    if user_id not in last_bot_messages:
        last_bot_messages[user_id] = []
    last_bot_messages[user_id].append((chat_id, msg.message_id, is_final))
    if not is_final:
        delete_task_message(user_id, msg.message_id)
    return msg

def send_long_temp_message(chat_id, text, user_id, is_final=False, parse_mode=None):
    msg = safe_send_message(chat_id, text, parse_mode=parse_mode)
    if not msg:
        return None
    if user_id not in last_bot_messages:
        last_bot_messages[user_id] = []
    last_bot_messages[user_id].append((chat_id, msg.message_id, is_final))
    if not is_final:
        delete_last_bot_message_a_little_later(user_id, msg.message_id)
    return msg

TELEGRAM_MAX_LEN = 4000  # keep some margin

def send_temp_long_message(chat_id, text, user_id, parse_mode=None):
    chunks = [
        text[i:i + TELEGRAM_MAX_LEN]
        for i in range(0, len(text), TELEGRAM_MAX_LEN)
    ]
    for chunk in chunks:
        send_task_message(chat_id, chunk, user_id, parse_mode=parse_mode)

def user_responded(user_id):
    msg = pending_prompts.pop(user_id, None)
    if msg:
        threading.Thread(
            target=delete_message_later,
            args=(msg.chat.id, msg.message_id, 60),
            daemon=True
        ).start()

def edit_and_autodelete(chat_id, message_id, text, delay=300, parse_mode=None):
    try:
        bot.edit_message_text(text, chat_id, message_id, parse_mode=parse_mode)
    except Exception:
        pass
    threading.Thread(target=delete_message_later, args=(chat_id, message_id, delay), daemon=True).start()

def delete_message_later(chat_id, message_id, delay):
    time.sleep(delay)
    try:
        bot.delete_message(chat_id, message_id)
    except:
        pass

def delete_last_bot_message_later(user_id, msg_id, delay=300):
    def _delete():
        time.sleep(delay)
        if user_id in last_bot_messages:
            for cid, mid, is_final in last_bot_messages[user_id]:
                if mid == msg_id:
                    try:
                        bot.delete_message(cid, mid)
                    except:
                        pass
                    last_bot_messages[user_id] = [
                        x for x in last_bot_messages[user_id] if x[1] != msg_id
                    ]
                    break
    threading.Thread(target=_delete, daemon=True).start()

def delete_task_message(user_id, msg_id, delay=700):
    def _delete():
        time.sleep(delay)
        if user_id in last_bot_messages:
            for cid, mid, is_final in last_bot_messages[user_id]:
                if mid == msg_id:
                    try:
                        bot.delete_message(cid, mid)
                    except:
                        pass
                    last_bot_messages[user_id] = [
                        x for x in last_bot_messages[user_id] if x[1] != msg_id
                    ]
                    break
    threading.Thread(target=_delete, daemon=True).start()

def cleanup_bot_message(user_id, chat_id, msg_id):
    try:
        bot.delete_message(chat_id, msg_id)
    except:
        pass
    if user_id in last_bot_messages:
        last_bot_messages[user_id] = [
            x for x in last_bot_messages[user_id] if x[1] != msg_id
        ]



def delete_task_message_24h(user_id, msg_id, delay=86400):
    def _delete():
        time.sleep(delay)
        if user_id in last_bot_messages:
            for cid, mid, is_final in last_bot_messages[user_id]:
                if mid == msg_id:
                    try:
                        bot.delete_message(cid, mid)
                    except:
                        pass
                    last_bot_messages[user_id] = [
                        x for x in last_bot_messages[user_id] if x[1] != msg_id
                    ]
                    break
    threading.Thread(target=_delete, daemon=True).start()

def send_task_message_24h(chat_id, text, user_id, is_final=False, parse_mode=None):
    msg = safe_send_message(chat_id, text, parse_mode=parse_mode)
    if not msg:
        return None
    if user_id not in last_bot_messages:
        last_bot_messages[user_id] = []
    last_bot_messages[user_id].append((chat_id, msg.message_id, is_final))
    if not is_final:
        delete_task_message_24h(user_id, msg.message_id)
    return msg

def delete_last_bot_message_a_little_later(user_id, msg_id, delay=3600):
    def _delete():
        time.sleep(delay)
        if user_id in last_bot_messages:
            for cid, mid, is_final in last_bot_messages[user_id]:
                if mid == msg_id:
                    try:
                        bot.delete_message(cid, mid)
                    except:
                        pass
                    last_bot_messages[user_id] = [
                        x for x in last_bot_messages[user_id] if x[1] != msg_id
                    ]
                    break
    threading.Thread(target=_delete, daemon=True).start()

def forward_to_groups_sequentially(from_chat_id, message_id, groups, delay=5):
    def _forward():
        for group_id in groups:
            try:
                bot.forward_message(
                    chat_id=group_id,
                    from_chat_id=from_chat_id,
                    message_id=message_id
                )
            except:
                pass
            time.sleep(delay)
    threading.Thread(target=_forward, daemon=True).start()

def ask_v4_range(chat_id, user_id):
    markup = telebot.types.InlineKeyboardMarkup()
    markup.add(
        telebot.types.InlineKeyboardButton(
            "📚 All chapters",
            callback_data="range_all",
        ),
        telebot.types.InlineKeyboardButton(
            "↔️ Custom range",
            callback_data="range_custom",
        ),
    )

    sent = bot.send_message(
        chat_id,
        "Select chapter range:\n\n"
        "📚 All chapters → download the full novel\n"
        "↔️ Custom range → choose a start and end chapter\n\n"
        "Example custom range: 40-60 or 40 60",
        reply_markup=markup,
    )
    pending_prompts[user_id] = sent
# ---------------------- NEW: V4 LIBRARY INTEGRATION ----------------------
# Direct in-process calls into the v4 library instead of a CLI subprocess,
# so we get exact chapter counts up front and per-chapter progress. `ctx`
# and `OutputFormat` are imported lazily here (see note at top of file).

_v4_mod_cache = {}

def _v4():
    if not _v4_mod_cache:
        from lncrawl.context import ctx
        from lncrawl.enums import OutputFormat
        try:
            ctx.sources.rejected = _NeverReject()
        except Exception as e:
            print(f"[V4 INIT WARNING] Could not patch ctx.sources.rejected: {e}")
        _v4_mod_cache["ctx"] = ctx
        _v4_mod_cache["OutputFormat"] = OutputFormat
    return _v4_mod_cache["ctx"], _v4_mod_cache["OutputFormat"]


def get_admin_user_v4():
    ctx, _ = _v4()
    ctx.setup()
    return ctx.users.get_admin()


def preview_novel_v4(url: str):
    """Fetch novel title/volume/chapter counts WITHOUT downloading anything.
    Raises on invalid url / unsupported source / network failure."""
    ctx, _ = _v4()
    user = get_admin_user_v4()
    crawler = ctx.sources.init_crawler(url)
    novel = ctx.crawler.fetch_novel(user.id, url, custom=crawler)
    return novel


def _resolve_chapter_ids_v4(novel, chapter_range: str):
    ctx, _ = _v4()
    all_ids = ctx.chapters.list_ids(novel_id=novel.id)

    if chapter_range.strip().lower() == "all":
        return all_ids

    try:
        start, end = map(int, chapter_range.split())
    except ValueError:
        raise Exception("Invalid chapter range. Use: 40-60 or 40 60")

    if start < 1 or end < start:
        raise Exception("Invalid chapter range. Start must be at least 1 and end must not be smaller than start.")

    # A request such as 1-5000 for a 100-chapter novel becomes 1-100.
    return all_ids[start - 1:end]

def _crawl_v4_with_progress_impl(
    url,
    chapter_range,
    on_progress,
    cancel_event,
    on_image_progress=None,
    on_total_known=None,
    resume_missing=False,
):
    """Download the selected v4 chapters once, then fetch images and create the EPUB."""
    


    ctx, OutputFormat = _v4()
    user = get_admin_user_v4()
    crawler = ctx.sources.init_crawler(url)


    novel = ctx.crawler.fetch_novel(user.id, url, custom=crawler)

    # The cover has already been discovered. Ignore only chapter illustrations.
    if should_ignore_chapter_images(url):
        ctx.config.crawler.ignore_images = True
        print(f"[IMAGES] Skipping chapter images for {get_url_host(url)}")

    if on_total_known:
        on_total_known(novel.chapter_count)

    if novel.chapter_count == 0:
        raise Exception("No chapters found on this page")

    chapters = list(dict.fromkeys(_resolve_chapter_ids_v4(novel, chapter_range)))
    if not chapters:
        raise Exception("No chapters to download")

    if resume_missing:
        reopened = ctx.chapters.reopen_empty(chapters)
        print(f"[RETRY] Reopened {reopened} stored-empty chapter(s) for {novel.title}")

    total = len(chapters)
    successful_chapter_ids = set()
    chapter_image_ids = []

    def download_chapter_batch(chapter_ids, serial=False):
        """
        Returns IDs of chapters that failed during this one download pass.
        """
        retryable_failures = []


        batches = ([chapter_id] for chapter_id in chapter_ids) if serial else [chapter_ids]

        for batch in batches:
            if cancel_event.is_set():
                raise Exception("Cancelled")

            futures = {
                crawler.taskman.submit_task(
                    ctx.crawler.fetch_chapter,
                    user.id,
                    chapter_id,
                    custom=crawler,
                ): chapter_id
                for chapter_id in batch
            }

            # Report successful chapters immediately as they complete.
            # Do not wrap resolve() in list(), because that would suppress
            # live progress until the entire batch finishes.
            for chapter in crawler.taskman.resolve(
                futures,
                desc="Chapters",
                unit=" c",
                signal=cancel_event,
            ):
                if cancel_event.is_set():
                    raise Exception("Cancelled")

                if chapter and chapter.is_available:
                    chapter_id = chapter.id

                    if chapter_id not in successful_chapter_ids:
                        successful_chapter_ids.add(chapter_id)

                        chapter_image_ids.extend(
                            ctx.images.list_ids(chapter_id=chapter.id)
                        )

                        if on_progress:
                            on_progress(
                                len(successful_chapter_ids),
                                total,
                            )

            if cancel_event.is_set():
                raise Exception("Cancelled")

            # The resolve() loop above reports successes immediately.
            # This second pass identifies which futures failed.
            for future, chapter_id in futures.items():
                if future.cancelled():
                    retryable_failures.append(chapter_id)
                    continue

                try:
                    chapter = future.result()
                except Exception as error:
                    print(
                        f"[CHAPTER] Chapter ID {chapter_id} "
                        f"failed: {type(error).__name__}: {error}"
                    )
                    retryable_failures.append(chapter_id)


        return retryable_failures

    failed_chapter_ids = download_chapter_batch(chapters, serial=False)
    unavailable_chapter_ids = [
        chapter_id for chapter_id in chapters
        if not ctx.chapters.get(chapter_id).is_available
    ]
    failed_chapter_ids = list(dict.fromkeys(failed_chapter_ids + unavailable_chapter_ids))

    if failed_chapter_ids:
        print(
            f"[CHAPTER] {len(failed_chapter_ids)} chapter(s) failed and will be skipped: "
            f"{failed_chapter_ids}"
        )

    if cancel_event.is_set():
        raise Exception("Cancelled")

    # Images are fetched only for chapters that actually downloaded.
    image_futures = {
        crawler.taskman.submit_task(
            ctx.crawler.fetch_image,
            user.id,
            image_id,
            custom=crawler,
        ): image_id
        for image_id in sorted(set(chapter_image_ids))
    }

    total_images = len(image_futures)
    img_done = 0

    for _ in crawler.taskman.resolve(
        image_futures,
        desc="Images",
        unit=" img",
        signal=cancel_event,
    ):
        img_done += 1
        if on_image_progress:
            on_image_progress(img_done, total_images)

    if cancel_event.is_set():
        raise Exception("Cancelled")

    artifact = ctx.binder.make_artifact(
        novel.id,
        novel.title,
        format=OutputFormat.epub,
        user_id=user.id,
    )

    if not artifact.is_available:
        raise Exception("Failed to generate EPUB")

    file = ctx.files.resolve(artifact.output_file)

    cover_source = ""
    if novel.cover_available:
        cover_source = str(ctx.files.resolve(novel.cover_file))

    return (
        str(file),
        novel.chapter_count,
        novel.title,
        novel.synopsis or "",
        cover_source,
        len(set(failed_chapter_ids)),
        str(novel.id),
    )

# ---------------------- NEW: V3 LIBRARY INTEGRATION ----------------------
# Same idea as v4: call the v3 library (`lncrawl.core.app.App`) directly
# instead of shelling out. `App.start_download(signal=...)` is a generator
# that yields once per completed chapter and updates
# `app.fetch_chapter_progress` (0-100) as it goes -- same shape as v4's
# taskman.resolve(), just a different attribute to read instead of a
# per-item yield value.
_v3_sources_loaded = False
_v3_sources_lock = threading.Lock()

def crawl_v4_with_progress(
    url,
    chapter_range,
    on_progress,
    cancel_event,
    on_image_progress=None,
    on_total_known=None,
    resume_missing=False,
):
    """Run v4 while restoring its global image setting after this task."""
    ctx, _ = _v4()
    previous_ignore_images = getattr(ctx.config.crawler, "ignore_images", False)
    try:
        return _crawl_v4_with_progress_impl(
            url,
            chapter_range,
            on_progress,
            cancel_event,
            on_image_progress=on_image_progress,
            on_total_known=on_total_known,
            resume_missing=resume_missing,
        )
    finally:
        ctx.config.crawler.ignore_images = previous_ignore_images
def _ensure_v3_sources_loaded():
    global _v3_sources_loaded
    if _v3_sources_loaded:
        return
    with _v3_sources_lock:
        if _v3_sources_loaded:
            return
        from lncrawl.core.sources import load_sources
        load_sources()
        _v3_sources_loaded = True

def preview_novel_v3(url: str):
    """Fetch novel title/volume/chapter counts for v3 WITHOUT downloading
    any chapter content. Raises on invalid url / unsupported source /
    network failure. Mirrors preview_novel_v4's shape."""
    from lncrawl.core.app import App

    _ensure_v3_sources_loaded()

    app = App()
    app.user_input = url
    app.prepare_search()

    if not app.crawler:
        raise Exception("No crawler found for this source")

    app.get_novel_info()

    class _Preview:
        pass
    preview = _Preview()
    preview.title = app.crawler.novel_title
    preview.chapter_count = len(app.crawler.chapters)
    preview.volume_count = len(app.crawler.volumes)
    return preview
    
def crawl_v3_with_progress(url, chapter_range, on_progress, cancel_event, on_image_progress=None, on_total_known=None):
    """Runs a full v3 download in-process and reports (done, total) chapter
    progress via on_progress. Returns the absolute path to the generated
    epub. chapter_range is either "all" or "start stop" (
    same semantics as the old --range flag)."""
    from lncrawl.core.app import App
    from lncrawl.core.download_chapters import fetch_chapter_body
    from lncrawl.core.download_images import fetch_chapter_images
    from lncrawl.core.metadata import save_metadata
    from lncrawl.models import OutputFormat as V3OutputFormat

    _ensure_v3_sources_loaded()

    app = App()
    app.user_input = url
    app.prepare_search()

    if not app.crawler:
        raise Exception("No crawler found for this source")

    app.get_novel_info()

    all_chapters = app.crawler.chapters
    actual_total = len(all_chapters)
    if on_total_known:
        on_total_known(actual_total)


    if chapter_range.lower() == "all":
        app.chapters = all_chapters[:]
    else:
        try:
            start, stop = map(int, chapter_range.split())
        except Exception:
            raise Exception("Invalid chapter range. Use: 1 10")
        app.chapters = all_chapters[start - 1: stop]

    if not app.chapters:
        raise Exception("No chapters to download for that range")

    app.output_formats = {V3OutputFormat.epub: True}

    save_metadata(app)
    if cancel_event.is_set():
        raise Exception("Cancelled")

    total = len(app.chapters)
    done = 0
    for _ in fetch_chapter_body(app, signal=cancel_event):
        if cancel_event.is_set():
            raise Exception("Cancelled")
        done += 1
        if on_progress:
            on_progress(done, total)
    save_metadata(app)
    if cancel_event.is_set():
        raise Exception("Cancelled")

    total_images = (1 if app.crawler.novel_cover else 0)
    for chapter in app.chapters:
        total_images += len(chapter.get("images") or {})

    img_done = 0
    for _ in fetch_chapter_images(app, signal=cancel_event):
        img_done += 1
        if on_image_progress:
            on_image_progress(img_done, total_images)
    save_metadata(app, True)
    if cancel_event.is_set():
        raise Exception("Cancelled")

    if app.crawler and app.can_do("logout"):
        app.crawler.logout()

    archive_path = None
    for _fmt, path in app.bind_books(signal=cancel_event):
        archive_path = path

    if not archive_path:
        raise Exception("Failed to generate EPUB")

    return (
        archive_path,
        actual_total,
        app.crawler.novel_title,
        getattr(app.crawler, "novel_synopsis", "") or "",
        getattr(app.crawler, "novel_cover", "") or "",
    )


skip_scan_events = {}  # preview_id -> threading.Event

def prompt_manual_range(message, chat_id, user_id, url, version):
    pending_urls[user_id] = {
    "url": url,
    "title": None,
    "total_chapters": None,
    "version": version,
}
    start_range_timeout(message.from_user, chat_id, url, version, None, None)
    ask_v4_range(chat_id, user_id)

def request_preview(url, version, user_id, chat_id, username=None, first_name=None, last_name=None):
    conn = get_db_connection()
    if not conn:
        return None
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                INSERT INTO preview_requests (user_id, chat_id, username, first_name, last_name, url, version)
                VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id
            """, (user_id, chat_id, username, first_name, last_name, url, version))
            pid = cur.fetchone()['id']
            conn.commit()
        return pid
    finally:
        conn.close()

def start_range_timeout(user, chat_id, url, version, title, total_chapters, delay=600):
    token = str(uuid.uuid4())
    pending_range_state[user.id] = {
        "token": token,
        "chat_id": chat_id,
        "url": url,
        "version": version,
        "title": title,
        "total_chapters": total_chapters,
        "user_id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "last_name": user.last_name,
    }
    threading.Thread(target=_range_timeout_watcher, args=(user.id, token, delay), daemon=True).start()

def _range_timeout_watcher(user_id, token, delay):
    time.sleep(delay)
    state = pending_range_state.get(user_id)
    if not state or state["token"] != token:
        return  # user already responded, or a newer request superseded this one
    pending_range_state.pop(user_id, None)

    try:
        bot.clear_step_handler_by_chat_id(state["chat_id"])
    except Exception:
        pass
    pending_urls.pop(user_id, None)

    send_task_message(
        state["chat_id"],
        f"⏰ No response for 10 minutes — automatically queuing the full novel (All chapters).",
        user_id
    )
    insert_task_direct(
        state["chat_id"], state["url"], "all", state["version"],
        state["user_id"], state["username"], state["first_name"], state["last_name"],
        novel_title=state["title"], total_chapters=state["total_chapters"]
    )

def wait_for_preview(pid, timeout=7200, cancel_event=None):
    deadline = time.time() + timeout
    consecutive_db_failures = 0
    TERMINAL_STATUSES = ('done', 'failed', 'abandoned')

    while time.time() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            _abandon_preview(pid)
            return "skipped"

        conn = get_db_connection()
        if conn:
            consecutive_db_failures = 0
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("SELECT * FROM preview_requests WHERE id=%s", (pid,))
                    row = cur.fetchone()
            finally:
                conn.close()
            if row and row['status'] in TERMINAL_STATUSES:
                return row
            sleep_time = 0.5
        else:
            consecutive_db_failures += 1
            sleep_time = min(10.0, 0.5 * (2 ** consecutive_db_failures))
            print(f"[PREVIEW WAIT] DB connection failed (attempt {consecutive_db_failures}), backoff {sleep_time}s")

        time.sleep(sleep_time)

    _abandon_preview(pid)
    return None

def _abandon_preview(pid):
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE preview_requests SET status='abandoned' WHERE id=%s AND status IN ('pending','processing')",
                    (pid,)
                )
                conn.commit()
        except Exception as e:
            print(f"[ABANDON PREVIEW ERROR] pid={pid}: {e}")
        finally:
            conn.close()

def format_range_desc(chapter_range, total_chapters):
    cr = chapter_range.strip().lower()
    if not total_chapters:
        return chapter_range  # unknown total (skipped/failed scan) — show raw input as-is
    if cr == "all":
        return f"1 to {total_chapters}"
    if cr.startswith("first"):
        n = int(cr.split()[1])
        return f"1 to {min(n, total_chapters)}"
    if cr.startswith("last"):
        n = int(cr.split()[1])
        return f"{max(1, total_chapters - n + 1)} to {total_chapters}"
    parts = cr.split()
    if len(parts) == 2:
        return f"{parts[0]} to {parts[1]}"
    return chapter_range

def range_suffix_for_filename(chapter_range, total_chapters):
    desc = format_range_desc(chapter_range, total_chapters)
    match = re.match(r"^\s*(\d+)\s+to\s+(\d+)\s*$", desc)
    if match:
        start, end = match.groups()
        return f"c{start}-{end}"
    safe = re.sub(r"[^\w\-]+", "_", chapter_range.strip())
    return safe or "full"

def process_preview_loop():
    MAX_RETRIES = 2
    while True:
        conn = get_db_connection()
        if not conn:
            time.sleep(2)
            continue
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT * FROM preview_requests
                    WHERE status = 'pending'
                      AND version = %s
                      AND (retry_server_id IS NULL OR retry_server_id = %s)
                    ORDER BY id ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED;
                """, (ALLOWED_CRAWLER, SERVER_ID))
                req = cur.fetchone()
                if not req:
                    conn.commit()
                    time.sleep(1.0)
                    continue

                cur.execute("""
                    UPDATE preview_requests
                    SET server_id = %s, run_id = %s, status = 'processing'
                    WHERE id = %s
                      AND status = 'pending'
                      AND server_id IS NULL
                      AND (retry_server_id IS NULL OR retry_server_id = %s)
                    RETURNING *;
                """, (SERVER_ID, RUN_ID, req['id'], SERVER_ID))
                req = cur.fetchone()
                if not req:
                    conn.commit()
                    time.sleep(1.0)
                    continue

            conn.commit()

            try:
                novel = preview_novel_v4(req['url']) if req['version'] == 'v4' else preview_novel_v3(req['url'])
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE preview_requests
                        SET status = 'done', title = %s, volume_count = %s,
                            chapter_count = %s, retry_server_id = NULL
                        WHERE id = %s;
                    """, (novel.title, novel.volume_count, novel.chapter_count, req['id']))
                    conn.commit()
            except Exception as error:
                error_text = str(error)

                # v4 scans tried outside s4 get one silent fallback to s4. Only
                # an s4 failure is shown to the user as a failed scan.
                if req['version'] == 'v4' and SERVER_ID != PREVIEW_RETRY_SERVER:
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE preview_requests
                            SET status = 'pending', server_id = NULL, run_id = NULL,
                                retry_server_id = %s, retry_count = retry_count + 1,
                                error = %s
                            WHERE id = %s AND status = 'processing';
                        """, (
                            PREVIEW_RETRY_SERVER,
                            f"{error_text} (retrying scan on {PREVIEW_RETRY_SERVER})",
                            req['id'],
                        ))
                        conn.commit()
                    print(f"[PREVIEW] ID {req['id']} failed on {SERVER_ID}; retrying on {PREVIEW_RETRY_SERVER}: {error_text}")
                    continue

                # v3 retains its original two-attempt behavior. v4 on s4 is
                # final because it is the designated fallback worker.
                if req['version'] == 'v4':
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE preview_requests
                            SET status = 'failed', error = %s, retry_count = retry_count + 1,
                                retry_server_id = NULL
                            WHERE id = %s AND status = 'processing';
                        """, (error_text, req['id']))
                        conn.commit()
                    print(f"[PREVIEW] ID {req['id']} failed on {SERVER_ID}: {error_text}")
                    continue

                retry_count = (req.get('retry_count') or 0) + 1
                if retry_count >= MAX_RETRIES:
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE preview_requests
                            SET status = 'failed', error = %s, retry_count = %s
                            WHERE id = %s AND status = 'processing';
                        """, (error_text, retry_count, req['id']))
                        conn.commit()
                    print(f"[PREVIEW] ID {req['id']} failed permanently after {MAX_RETRIES} retries: {error_text}")
                else:
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE preview_requests
                            SET status = 'pending', server_id = NULL, run_id = NULL,
                                retry_count = %s, error = %s
                            WHERE id = %s AND status = 'processing';
                        """, (retry_count, f"{error_text} (retry {retry_count}/{MAX_RETRIES})", req['id']))
                        conn.commit()
                    print(f"[PREVIEW] ID {req['id']} retrying ({retry_count}/{MAX_RETRIES}): {error_text}")
        except Exception as error:
            print(f"[PREVIEW WORKER ERROR] {error}")
        finally:
            conn.close()
def cleanup_old_preview():
    while True:
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        DELETE FROM preview_requests
                        WHERE created_at < NOW() - INTERVAL '1 day';
                    """)
                    conn.commit()
            except Exception as e:
                print(f"[CLEANUP PREVIEW ERROR] {e}")
            finally:
                conn.close()

        time.sleep(60 * 60)

threading.Thread(target=process_preview_loop, daemon=True).start()
threading.Thread(target=cleanup_old_preview, daemon=True).start()

def reap_orphaned_previews():
    while True:
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("""
                        SELECT p.id
                        FROM preview_requests p
                        LEFT JOIN servers s ON s.server_id = p.server_id
                        WHERE p.status = 'processing'
                          AND (
                              s.server_id IS NULL
                              OR s.status = 'offline'
                              OR s.run_id IS DISTINCT FROM p.run_id
                              OR s.last_heartbeat < NOW() - INTERVAL '60 seconds'
                          )
                    """)
                    orphaned = cur.fetchall()
                    if orphaned:
                        cur.execute("""
                            UPDATE preview_requests
                            SET status='pending', server_id=NULL, run_id=NULL
                            WHERE id = ANY(%s)
                        """, ([o['id'] for o in orphaned],))
                    conn.commit()
            except Exception as e:
                print(f"[PREVIEW REAPER ERROR] {e}")
            finally:
                conn.close()
        time.sleep(15)

threading.Thread(target=reap_orphaned_previews, daemon=True).start()

# ---------------------- BLOCK GROUP COMMANDS ----------------------
 
@bot.message_handler(
    func=lambda message: (
        is_blocked_group(message)
        and message.text
        and message.text.startswith("/")
        and "@lightnovel_crawer_bot" in message.text
    )
)
def block_group_commands(message):
    try:
        warn = bot.send_message(
            message.from_user.id,
            "⚠️ This bot cannot be used in that group.\nPlease send the command here to use it."
        )
        threading.Thread(
            target=delete_message_later,
            args=(warn.chat.id, warn.message_id, 60),
            daemon=True
        ).start()
    except:
        pass

def build_synopsis_messages(synopsis):
    """Return synopsis-only Telegram messages, split safely if needed."""
    synopsis = html.unescape(synopsis or "")
    synopsis = re.sub(r"<[^>]+>", " ", synopsis)
    synopsis = re.sub(r"\s+", " ", synopsis).strip()

    if not synopsis:
        return []

    prefix = "<b>Synopsis:</b> "
    messages = []
    remaining = synopsis

    while remaining:
        max_plain_len = 3500
        chunk = remaining[:max_plain_len]

        if len(remaining) > len(chunk):
            split_at = chunk.rfind(" ")
            if split_at > 0:
                chunk = chunk[:split_at]

        while chunk and len(prefix + html.escape(chunk)) > 3900:
            chunk = chunk[:-1]

        if not chunk:
            break

        if not messages:
            messages.append(prefix + html.escape(chunk))
        else:
            messages.append(html.escape(chunk))

        remaining = remaining[len(chunk):].lstrip()

    return messages

def send_message_with_retry(chat_id, text, parse_mode=None, max_retries=2, base_delay=3):
    for attempt in range(1, max_retries + 1):
        try:
            return bot.send_message(
                chat_id,
                text,
                parse_mode=parse_mode,
                link_preview_options=telebot.types.LinkPreviewOptions(
                    is_disabled=True
                ),
                timeout=60 + (attempt * 15),
            )

        except telebot.apihelper.ApiTelegramException as error:
            if error.error_code == 429:
                retry_after = error.result_json.get(
                    "parameters", {}
                ).get("retry_after", 0)

                print(
                    f"[MESSAGE SKIPPED] chat_id={chat_id}: "
                    f"Telegram rate limited this chat for {retry_after}s"
                )
                return None

            print(f"[MESSAGE RETRY {attempt}/{max_retries}] {error}")

        except Exception as error:
            print(f"[MESSAGE RETRY {attempt}/{max_retries}] {error}")

        if attempt < max_retries:
            time.sleep(base_delay * attempt)

    print(f"[MESSAGE FAILED] chat_id={chat_id}")
    return None

def send_cover_post(chat_id, cover_source, display_title, source_url, crawler_version):
    safe_title = html.escape(display_title)
    safe_url = html.escape(source_url, quote=True)
    safe_version = html.escape(str(crawler_version))

    caption = (
        f"📖 <b>Novel:</b> {safe_title}\n"
        f'🌐 <b>Source:</b> <a href="{safe_url}">{safe_url}</a>\n'
        f"🤖 <b>Crawler:</b> {safe_version}"
    )

    if cover_source:
        for attempt in range(1, 3):
            try:
                if os.path.isfile(cover_source):
                    with open(cover_source, "rb") as cover_file:
                        return bot.send_photo(
                            chat_id,
                            cover_file,
                            caption=caption,
                            parse_mode="HTML",
                            timeout=120,
                        )

                if cover_source.startswith(("https://", "http://")):
                    return bot.send_photo(
                        chat_id,
                        cover_source,
                        caption=caption,
                        parse_mode="HTML",
                        timeout=120,
                    )

            except telebot.apihelper.ApiTelegramException as error:
                if error.error_code == 429:
                    retry_after = error.result_json.get(
                        "parameters", {}
                    ).get("retry_after", 0)

                    print(
                        f"[COVER SKIPPED] chat_id={chat_id}: "
                        f"Telegram rate limited this chat for {retry_after}s"
                    )
                    return None

                print(f"[COVER RETRY {attempt}/2] {error}")

            except Exception as error:
                print(f"[COVER RETRY {attempt}/2] {error}")

            if attempt < 2:
                time.sleep(3)

    # Only use this fallback when there was no usable cover,
    # not after a Telegram 429.
    return send_message_with_retry(chat_id, caption, parse_mode="HTML")

def forward_messages_to_groups_sequentially(from_chat_id, message_ids, groups, delay=5):
    def _forward():
        for group_id in groups:
            for message_id in message_ids:
                try:
                    bot.copy_message(
                        chat_id=group_id,
                        from_chat_id=from_chat_id,
                        message_id=message_id,
                    )
                except Exception as error:
                    print(f"[COPY ERROR] {group_id}: {error}")
            time.sleep(delay)

    threading.Thread(target=_forward, daemon=True).start()

def send_document_with_retry(chat_id, file_path, caption=None, max_retries=2, base_delay=5):
    for attempt in range(1, max_retries + 1):
        try:
            with open(file_path, "rb") as file_handle:
                return bot.send_document(
                    chat_id,
                    file_handle,
                    caption=caption,
                    timeout=150,
                )

        except telebot.apihelper.ApiTelegramException as error:
            if error.error_code == 429:
                retry_after = error.result_json.get(
                    "parameters", {}
                ).get("retry_after", 0)

                print(
                    f"[EPUB SKIPPED] chat_id={chat_id}: "
                    f"Telegram rate limited this chat for {retry_after}s"
                )
                return None

            print(f"[SEND RETRY {attempt}/{max_retries}] {error}")

        except Exception as error:
            # This is where brief SSL/network failures get one retry.
            print(f"[SEND RETRY {attempt}/{max_retries}] {error}")

        if attempt < max_retries:
            time.sleep(base_delay * attempt)

    print(f"[EPUB SEND FAILED] chat_id={chat_id}")
    return None

def queue_epub_upload_retry(task, file_path, caption):
    if not os.path.isfile(file_path):
        print(f"[UPLOAD RETRY NOT QUEUED] Missing EPUB: {file_path}")
        return False

    try:
        os.makedirs(PENDING_UPLOAD_DIR, exist_ok=True)

        safe_name = os.path.basename(file_path)
        saved_name = f"task_{task['id']}_{uuid.uuid4().hex}_{safe_name}"
        saved_path = os.path.abspath(os.path.join(PENDING_UPLOAD_DIR, saved_name))

        shutil.copy2(file_path, saved_path)

        conn = get_db_connection()
        if not conn:
            os.remove(saved_path)
            return False

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO pending_uploads (
                        task_id, chat_id, user_id, server_id,
                        file_path, caption, next_retry_at, expires_at
                    )
                    VALUES (
                        %s, %s, %s, %s, %s, %s,
                        NOW() + (%s || ' minutes')::INTERVAL,
                        NOW() + (%s || ' hours')::INTERVAL
                    )
                    ON CONFLICT (task_id) DO NOTHING;
                """, (
                    task["id"],
                    task["chat_id"],
                    task["user_id"],
                    SERVER_ID,
                    saved_path,
                    caption,
                    UPLOAD_RETRY_DELAY_MINUTES,
                    UPLOAD_RETRY_KEEP_HOURS,
                ))
                conn.commit()
        finally:
            conn.close()

        conn = get_db_connection()
        if not conn:
            return False

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id
                    FROM pending_uploads
                    WHERE task_id=%s;
                """, (task["id"],))
                upload_row = cur.fetchone()
        finally:
            conn.close()

        if upload_row:
            threading.Thread(
                target=upload_external_fallback,
                args=(
                    upload_row["id"],
                    task["id"],
                    task["chat_id"],
                    os.path.splitext(os.path.basename(file_path))[0],
                    saved_path,
                ),
                daemon=True,
            ).start()

        print(f"[UPLOAD RETRY QUEUED] Task {task['id']} at {saved_path}")
        return True

    except Exception as error:
        print(f"[UPLOAD RETRY QUEUE ERROR] Task {task['id']}: {error}")
        return False


FALLBACK_LINK_GROUPS = [
    MY_GROUP_ID,
    OTHER_GROUP_ID,
    BOREAL_PRIVATE_GROUP_ID,
    BOREAL_OTHER_GROUP,
]

def upload_to_gofile(file_path):


    try:
        with open(file_path, "rb") as file_handle:
            response = requests.post(
                "https://upload.gofile.io/uploadfile",
                files={"file": (os.path.basename(file_path), file_handle)},
                headers={},
                timeout=(30, 1800),
            )

        response.raise_for_status()
        data = response.json().get("data", {})

        url = (
            data.get("downloadPage")
            or data.get("pageLink")
            or data.get("page_link")
        )

        if not url:
            raise RuntimeError(f"Gofile returned no download URL: {data}")

        return url, None

    except Exception as error:
        return None, str(error)


def upload_to_buzzheavier(file_path):


    filename = urllib.parse.quote(os.path.basename(file_path), safe="")
    upload_url = f"https://w.buzzheavier.com/{filename}"

    try:
        with open(file_path, "rb") as file_handle:
            response = requests.put(
                upload_url,
                data=file_handle,
                headers={},
                timeout=(30, 1800),
            )

        response.raise_for_status()
        data = response.json().get("data", {})
        file_id = data.get("id")

        if not file_id:
            raise RuntimeError(f"Buzzheavier returned no file ID: {data}")

        return f"https://buzzheavier.com/{file_id}", None

    except Exception as error:
        return None, str(error)


def send_fallback_links(task_id, chat_id, title, gofile_url, buzzheavier_url):
    lines = [
        "📥 <b>Telegram EPUB delivery is delayed.</b>",
        f"📖 <b>{html.escape(title)}</b>",
        "",
        "You can download the EPUB now from:",
    ]

    if gofile_url:
        lines.append(f'• <a href="{html.escape(gofile_url, quote=True)}">Gofile</a>')

    if buzzheavier_url:
        lines.append(
            f'• <a href="{html.escape(buzzheavier_url, quote=True)}">Buzzheavier</a>'
        )

    link_message = "\n".join(lines)

    # Try the requesting user first. A user-specific 429 does not stop the rest.
    send_message_with_retry(chat_id, link_message, parse_mode="HTML", max_retries=1)

    # Admins receive the fallback link directly.
    for admin_id in ADMIN_USER_ID:
        send_message_with_retry(
            admin_id,
            f"Task #{task_id}\n\n{link_message}",
            parse_mode="HTML",
            max_retries=1,
        )

    # Groups receive brand-new direct messages, not forwarded messages.
    for group_id in FALLBACK_LINK_GROUPS:
        send_message_with_retry(
            group_id,
            link_message,
            parse_mode="HTML",
            max_retries=1,
        )
        time.sleep(5)


def upload_external_fallback(upload_id, task_id, chat_id, title, file_path):
    results = {
        "gofile_url": None,
        "buzzheavier_url": None,
        "gofile_error": None,
        "buzzheavier_error": None,
    }

    def gofile_worker():
        results["gofile_url"], results["gofile_error"] = upload_to_gofile(file_path)

    def buzzheavier_worker():
        results["buzzheavier_url"], results["buzzheavier_error"] = upload_to_buzzheavier(file_path)

    gofile_thread = threading.Thread(target=gofile_worker)
    buzzheavier_thread = threading.Thread(target=buzzheavier_worker)

    gofile_thread.start()
    buzzheavier_thread.start()

    gofile_thread.join()
    buzzheavier_thread.join()

    gofile_url = results["gofile_url"]
    buzzheavier_url = results["buzzheavier_url"]

    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                if gofile_url or buzzheavier_url:
                    cur.execute("""
                        UPDATE pending_uploads
                        SET gofile_url=%s,
                            buzzheavier_url=%s,
                            external_status='uploaded',
                            external_error=NULL
                        WHERE id=%s;
                    """, (gofile_url, buzzheavier_url, upload_id))
                else:
                    cur.execute("""
                        UPDATE pending_uploads
                        SET external_status='failed',
                            external_error=%s
                        WHERE id=%s;
                    """, (
                        f"Gofile: {results['gofile_error']} | "
                        f"Buzzheavier: {results['buzzheavier_error']}",
                        upload_id,
                    ))

                conn.commit()
        finally:
            conn.close()

    if gofile_url or buzzheavier_url:
        send_fallback_links(
            task_id,
            chat_id,
            title,
            gofile_url,
            buzzheavier_url,
        )
        print(f"[EXTERNAL UPLOAD SUCCESS] Task {task_id}")
    else:
        print(f"[EXTERNAL UPLOAD FAILED] Task {task_id}")
# ---------------------- BOT COMMANDS ----------------------
if SERVER_ID == "s1":
    @bot.message_handler(commands=['start'])
    def start(message):
        send_long_temp_message(
            message.chat.id,
            "👋 Welcome! I can help you download novels.\n"
            "Use /download to start a download.\n"
            "Use /cancel to stop all your ongoing or queued downloads.\n"
            "Use /retry after an EPUB reports missing chapters (available for 20 hours).\n"
            "Use /queue to view pending downloads and see if the bot is busy.\n"
            "Use /tasks to check bot downloads for the past 24 hours.\n"
            "Use /sources to see supported websites.\n"
            "Use /webtoepub for tutorials to download from unsupported websites or websites that are failing.\n\n",
            message.from_user.id
        )

    @bot.message_handler(commands=['webtoepub'])
    def webtoepub(message):
        send_long_temp_message(
            message.chat.id,
            "📚 WebToEpub lets you download novels as EPUB.\n\n"
            "Installation tutorial: https://t.me/LightnovelCrawlerTutorial/88\n"
            "How to use WebToEpub: https://t.me/LightnovelCrawlerTutorial/69\n"
            "How to update WebToEpub: https://t.me/LightnovelCrawlerTutorial/91\n"
            "Latest version of WebToEpub: https://github.com/dteviot/WebToEpub/releases",
            message.from_user.id
        )
    def resolve_control_target(identifier):
        identifier = identifier.strip()

        if identifier.lstrip("-").isdigit():
            return int(identifier), None

        if identifier.startswith("@"):
            username = identifier[1:].lower()
            conn = get_db_connection()
            if not conn:
                return None, "Database connection failed."

            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT user_id, username
                        FROM tasks
                        WHERE LOWER(username) = %s
                        ORDER BY created_at DESC
                        LIMIT 1;
                    """, (username,))
                    row = cur.fetchone()

                if not row:
                    return None, (
                        "That username has not used the bot yet. "
                        "Use their numeric user ID instead."
                    )

                return row["user_id"], row["username"]
            finally:
                conn.close()

        return None, "Use a numeric user ID or @username."


    @bot.message_handler(commands=["banuser", "unbanuser", "userlimit"])
    def manage_user_control(message):
        if message.from_user.id not in ADMIN_USER_ID:
            safe_send_message(message.chat.id, "❌ Access Denied.")
            return

        parts = message.text.split()

        if len(parts) < 2:
            safe_send_message(
                message.chat.id,
                "Usage:\n"
                "/banuser <user_id or @username>\n"
                "/unbanuser <user_id or @username>\n"
                "/userlimit <user_id or @username> <number|off>"
            )
            return

        command = parts[0].split("@")[0].lower()
        target_id, target_result = resolve_control_target(parts[1])

        if target_id is None:
            safe_send_message(message.chat.id, f"❌ {target_result}")
            return

        if command == "/userlimit":
            if len(parts) != 3:
                safe_send_message(
                    message.chat.id,
                    "Usage: /userlimit <user_id or @username> <number|off>"
                )
                return

            limit_text = parts[2].lower()

            if limit_text == "off":
                daily_limit = None
            elif limit_text.isdigit() and int(limit_text) >= 1:
                daily_limit = int(limit_text)
            else:
                safe_send_message(
                    message.chat.id,
                    "❌ The limit must be a whole number of 1 or more, or `off`.",
                )
                return

            conn = get_db_connection()
            if not conn:
                safe_send_message(message.chat.id, "❌ Database error.")
                return

            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO user_controls (
                            user_id, username, daily_task_limit, updated_by, updated_at
                        )
                        VALUES (%s, %s, %s, %s, NOW())
                        ON CONFLICT (user_id) DO UPDATE SET
                            username = EXCLUDED.username,
                            daily_task_limit = EXCLUDED.daily_task_limit,
                            updated_by = EXCLUDED.updated_by,
                            updated_at = NOW();
                    """, (target_id, target_result, daily_limit, message.from_user.id))
                    conn.commit()
            finally:
                conn.close()

            status = "removed" if daily_limit is None else f"set to {daily_limit}"
            safe_send_message(
                message.chat.id,
                f"✅ Daily limit for `{target_id}` {status}.",
                parse_mode="Markdown",
            )
            return

        is_banned = command == "/banuser"

        conn = get_db_connection()
        if not conn:
            safe_send_message(message.chat.id, "❌ Database error.")
            return

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO user_controls (
                        user_id, username, is_banned, updated_by, updated_at
                    )
                    VALUES (%s, %s, %s, %s, NOW())
                    ON CONFLICT (user_id) DO UPDATE SET
                        username = EXCLUDED.username,
                        is_banned = EXCLUDED.is_banned,
                        updated_by = EXCLUDED.updated_by,
                        updated_at = NOW();
                """, (
                    target_id,
                    target_result,
                    is_banned,
                    message.from_user.id,
                ))
                conn.commit()
        finally:
            conn.close()

        action = "banned" if is_banned else "unbanned"
        safe_send_message(message.chat.id, f"✅ User `{target_id}` was {action}.", parse_mode="Markdown")
    def _require_site_admin(message):
        if message.from_user.id in ADMIN_USER_ID:
            return True
        send_temp_message(message.chat.id, "Access denied.", message.from_user.id)
        return False

    def _site_command_parts(message, usage, require_value=False):
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) < 2:
            send_temp_message(message.chat.id, f"Usage: {usage}", message.from_user.id)
            return None, None
        domain = normalize_site_domain(parts[1])
        if not domain:
            send_temp_message(message.chat.id, "Use a valid domain, for example: dreamy-translations.com", message.from_user.id)
            return None, None
        value = parts[2].strip() if len(parts) > 2 else ""
        if require_value and not value:
            send_temp_message(message.chat.id, f"Usage: {usage}", message.from_user.id)
            return None, None
        return domain, value

    @bot.message_handler(commands=['ignoreimages'])
    def ignoreimages(message):
        if not _require_site_admin(message):
            return
        domain, _ = _site_command_parts(message, "/ignoreimages domain")
        if not domain:
            return
        conn = get_db_connection()
        if not conn:
            send_temp_message(message.chat.id, "Database error.", message.from_user.id)
            return
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO site_rules (domain, ignore_chapter_images, updated_by)
                    VALUES (%s, TRUE, %s)
                    ON CONFLICT (domain) DO UPDATE SET
                        ignore_chapter_images = TRUE,
                        updated_by = EXCLUDED.updated_by,
                        updated_at = NOW();
                """, (domain, message.from_user.id))
                conn.commit()
            send_temp_message(message.chat.id, f"Chapter images will be skipped for {domain}. Covers are kept.", message.from_user.id)
        finally:
            conn.close()

    @bot.message_handler(commands=['unignoreimages'])
    def unignoreimages(message):
        if not _require_site_admin(message):
            return
        domain, _ = _site_command_parts(message, "/unignoreimages domain")
        if not domain:
            return
        conn = get_db_connection()
        if not conn:
            send_temp_message(message.chat.id, "Database error.", message.from_user.id)
            return
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE site_rules
                    SET ignore_chapter_images = FALSE, updated_by = %s, updated_at = NOW()
                    WHERE domain = %s;
                """, (message.from_user.id, domain))
                conn.commit()
            send_temp_message(message.chat.id, f"Chapter images are enabled again for {domain}.", message.from_user.id)
        finally:
            conn.close()

    @bot.message_handler(commands=['bansite'])
    def bansite(message):
        if not _require_site_admin(message):
            return
        domain, reason = _site_command_parts(message, "/bansite domain reason", require_value=True)
        if not domain:
            return
        conn = get_db_connection()
        if not conn:
            send_temp_message(message.chat.id, "Database error.", message.from_user.id)
            return
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO site_rules (domain, ban_reason, updated_by)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (domain) DO UPDATE SET
                        ban_reason = EXCLUDED.ban_reason,
                        updated_by = EXCLUDED.updated_by,
                        updated_at = NOW();
                """, (domain, reason, message.from_user.id))
                conn.commit()
            send_temp_message(message.chat.id, f"Banned {domain}.", message.from_user.id)
        finally:
            conn.close()

    @bot.message_handler(commands=['unbansite'])
    def unbansite(message):
        if not _require_site_admin(message):
            return
        domain, _ = _site_command_parts(message, "/unbansite domain")
        if not domain:
            return
        conn = get_db_connection()
        if not conn:
            send_temp_message(message.chat.id, "Database error.", message.from_user.id)
            return
        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE site_rules
                    SET ban_reason = NULL, updated_by = %s, updated_at = NOW()
                    WHERE domain = %s;
                """, (message.from_user.id, domain))
                conn.commit()
            send_temp_message(message.chat.id, f"Unbanned {domain}.", message.from_user.id)
        finally:
            conn.close()

    @bot.message_handler(commands=['addsource', 'addsite'])
    def addsource(message):
        if not _require_site_admin(message):
            return
        domain, note = _site_command_parts(message, "/addsource domain [note]")
        if not domain:
            return
        conn = get_db_connection()
        if not conn:
            send_temp_message(message.chat.id, "Database error.", message.from_user.id)
            return
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 AS next_order FROM source_directory;")
                next_order = cur.fetchone()["next_order"]
                cur.execute("""
                    INSERT INTO source_directory (domain, note, sort_order, created_by, updated_by)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (domain) DO UPDATE SET
                        note = EXCLUDED.note,
                        updated_by = EXCLUDED.updated_by,
                        updated_at = NOW();
                """, (domain, note, next_order, message.from_user.id, message.from_user.id))
                conn.commit()
            send_temp_message(message.chat.id, f"Added {domain} to /sources.", message.from_user.id)
        finally:
            conn.close()

    @bot.message_handler(commands=['removesource', 'deletesource', 'delsource', 'removesite', 'deletesite', 'delsite'])
    def delsource(message):
        if not _require_site_admin(message):
            return
        domain, _ = _site_command_parts(message, "/delsource domain")
        if not domain:
            return
        conn = get_db_connection()
        if not conn:
            send_temp_message(message.chat.id, "Database error.", message.from_user.id)
            return
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM source_directory WHERE domain = %s RETURNING domain;", (domain,))
                removed = cur.fetchone()
                conn.commit()
            result = f"Removed {domain} from /sources." if removed else f"{domain} was not in /sources."
            send_temp_message(message.chat.id, result, message.from_user.id)
        finally:
            conn.close()

    @bot.message_handler(commands=['sources'])
    def sources(message):
        rows = get_source_directory()
        text = "\U0001F4DA Supported sources:\n"
        for row in rows:
            note = (row["note"] or "").strip()
            text += f"- {row['domain']}"
            if note:
                text += f" {note}"
            text += "\n"
        text += (
            "\nMore sources here: https://github.com/dipu-bd/lightnovel-crawler but not all may work.\n"
            "To ask for more sources to be added, or to report broken sources, create an issue here: https://github.com/dipu-bd/lightnovel-crawler/issues/new/choose"
        )
        send_long_temp_message(message.chat.id, text, message.from_user.id)
    def cancel_selected_tasks(user_id, chat_id, task_ids):
        """Cancel only task IDs that still belong to this user and are pending/running."""
        if not task_ids:
            send_temp_message(chat_id, "ℹ️ No tasks were selected.", user_id)
            return

        conn = get_db_connection()
        if not conn:
            send_temp_message(chat_id, "⚠️ Database error. Try again.", user_id)
            return

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE tasks
                    SET status = 'cancelled'
                    WHERE user_id = %s
                      AND id = ANY(%s)
                      AND status IN ('pending', 'running')
                    RETURNING id, status;
                """, (user_id, task_ids))

                cancelled_tasks = cur.fetchall()
                conn.commit()

            if not cancelled_tasks:
                send_temp_message(
                    chat_id,
                    "ℹ️ None of those tasks were still pending or running.",
                    user_id,
                )
                return

            # Stop only selected locally-running tasks.
            with running_lock:
                for task in cancelled_tasks:
                    cancel_event = running_processes.pop(task["id"], None)
                    if cancel_event:
                        try:
                            cancel_event.set()
                        except Exception:
                            pass

            cancelled_ids = ", ".join(str(task["id"]) for task in cancelled_tasks)
            send_temp_message(
                chat_id,
                f"❌ Cancelled task(s): {cancelled_ids}",
                user_id,
            )

        except Exception as e:
            send_temp_message(chat_id, f"❌ Cancel failed: {e}", user_id)
        finally:
            conn.close()


    def cancel_selection_timeout(user_id, delay=600):
        """Forget the numbered list after 10 minutes."""
        def expire():
            time.sleep(delay)
            pending_cancel_tasks.pop(user_id, None)

        threading.Thread(target=expire, daemon=True).start()


    def get_cancel_task_selection(message):
        user_id = message.from_user.id
        chat_id = message.chat.id

        tasks = pending_cancel_tasks.pop(user_id, None)
        if not tasks:
            send_temp_message(
                chat_id,
                "⌛ Your cancellation list expired. Send /cancel again.",
                user_id,
            )
            return

        # Accept spaces and commas:
        # 1 3 5
        # 1,3,5
        # 2 78642 5 6734789
        raw_tokens = re.split(r"[\s,]+", (message.text or "").strip())
        raw_tokens = [token for token in raw_tokens if token]

        if not raw_tokens:
            send_temp_message(
                chat_id,
                "❌ Enter one or more list numbers or task IDs.",
                user_id,
            )
            return

        task_ids_in_list = {task["id"] for task in tasks}
        selected_ids = set()
        ignored_inputs = []

        for token in raw_tokens:
            # Letters/symbols such as g are ignored, not treated as errors.
            if not token.isdigit():
                ignored_inputs.append(token)
                continue

            value = int(token)

            # Exact task ID has priority over the list position.
            if value in task_ids_in_list:
                selected_ids.add(value)
            elif 1 <= value <= len(tasks):
                selected_ids.add(tasks[value - 1]["id"])
            else:
                ignored_inputs.append(token)

        if not selected_ids:
            ignored_text = ", ".join(ignored_inputs) or "your input"
            send_temp_message(
                chat_id,
                f"❌ No matching tasks found. Ignored: {ignored_text}\n\n"
                "Send /cancel and select tasks again.",
                user_id,
            )
            return

        # Valid selections are cancelled even if other entries were invalid.
        cancel_selected_tasks(user_id, chat_id, list(selected_ids))

        if ignored_inputs:
            send_temp_message(
                chat_id,
                f"ℹ️ Ignored invalid/non-matching entries: {', '.join(ignored_inputs)}",
                user_id,
            )


    @bot.callback_query_handler(func=lambda call: call.data.startswith("cancel_"))
    def handle_cancel_choice(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        choice = call.data.replace("cancel_", "")

        # Same behavior as version/range buttons: remove buttons immediately.
        try:
            bot.edit_message_reply_markup(
                chat_id,
                call.message.message_id,
                reply_markup=None,
            )
        except Exception:
            pass

        if choice == "all":
            conn = get_db_connection()
            if not conn:
                bot.answer_callback_query(call.id, "Database error.")
                send_temp_message(chat_id, "⚠️ Database error. Try again.", user_id)
                return

            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT id
                        FROM tasks
                        WHERE user_id = %s
                          AND status IN ('pending', 'running')
                        ORDER BY id ASC;
                    """, (user_id,))
                    task_ids = [row["id"] for row in cur.fetchall()]
            finally:
                conn.close()

            cancel_selected_tasks(user_id, chat_id, task_ids)
            bot.answer_callback_query(call.id)
            return

        if choice != "select":
            bot.answer_callback_query(call.id, "Invalid choice.")
            return

        conn = get_db_connection()
        if not conn:
            bot.answer_callback_query(call.id, "Database error.")
            send_temp_message(chat_id, "⚠️ Database error. Try again.", user_id)
            return

        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT
                        id,
                        server_id,
                        crawler_version,
                        status,
                        url,
                        chapter_range,
                        created_at,
                        completed_at
                    FROM tasks
                    WHERE user_id = %s
                      AND status IN ('pending', 'running')
                    ORDER BY id ASC;
                """, (user_id,))
                tasks = cur.fetchall()
        finally:
            conn.close()

        if not tasks:
            bot.answer_callback_query(call.id, "No tasks to cancel.")
            send_temp_message(chat_id, "ℹ️ No pending or running downloads to cancel.", user_id)
            return

        # Save only this user's task list; later input cannot cancel anyone else's task.
        pending_cancel_tasks[user_id] = tasks
        cancel_selection_timeout(user_id)

        text = "📋 Your pending/running downloads:\n\n"
        for number, task in enumerate(tasks, start=1):
            server = task["server_id"] or "Not assigned"
            completed = task["completed_at"] or "Not completed"

            text += (
                f"{number}. Task ID: {task['id']}\n"
                f"   Server: {server}\n"
                f"   Crawler: {task['crawler_version']}\n"
                f"   Status: {task['status']}\n"
                f"   URL: {task['url']}\n"
                f"   Range: {task['chapter_range']}\n"
                f"   Created: {task['created_at']}\n"
                f"   Completed: {completed}\n"
                f"-----------------------------\n"
            )

        # Same long temporary-message behavior as your /tasks-style lists.
        send_temp_long_message(chat_id, text, user_id)

        send_temp_message(
            chat_id,
            "Reply with task numbers or task IDs to cancel.\n\n"
            "Examples:\n"
            "Task Number like\n"
            "1 3 5\n"
            "or Task ID like\n"
            "85385 564890\n\n"
            "This selection expires after 10 minutes.",
            user_id,
        )

        bot.register_next_step_handler_by_chat_id(
            chat_id,
            get_cancel_task_selection,
        )
        bot.answer_callback_query(call.id)


    @bot.message_handler(commands=['cancel'])
    def cancel(message):
        user_id = message.from_user.id
        chat_id = message.chat.id

        conn = get_db_connection()
        if not conn:
            send_temp_message(chat_id, "⚠️ Database error. Try again.", user_id)
            return

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) AS task_count
                    FROM tasks
                    WHERE user_id = %s
                      AND status IN ('pending', 'running');
                """, (user_id,))
                task_count = cur.fetchone()["task_count"]
        finally:
            conn.close()

        if task_count == 0:
            send_temp_message(
                chat_id,
                "ℹ️ You have no pending or running downloads to cancel.",
                user_id,
            )
            return

        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(
            telebot.types.InlineKeyboardButton(
                "❌ Cancel all",
                callback_data="cancel_all",
            ),
            telebot.types.InlineKeyboardButton(
                "📋 Select tasks",
                callback_data="cancel_select",
            ),
        )

        # Like the version prompt: it is tracked, and disappears after response.
        sent = bot.send_message(
            chat_id,
            f"You have {task_count} pending/running download(s).\n\n"
            "Choose what you want to cancel:",
            reply_markup=markup,
        )
        pending_prompts[user_id] = sent


    @bot.message_handler(commands=['scan'])
    def scan_requests(message):
        if message.from_user.id not in ADMIN_USER_ID:
            bot.send_message(message.chat.id, "❌ Access Denied.")
            return

        conn = get_db_connection()
        if not conn:
            send_temp_message(message.chat.id, "⚠️ Cannot connect to Database.", message.from_user.id)
            return

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, user_id, username, first_name, last_name, chat_id, url, version, status, title,
                        volume_count, chapter_count, error, server_id, created_at
                    FROM preview_requests
                    ORDER BY created_at
                """)
                rows = cur.fetchall()

                if not rows:
                    send_temp_message(message.chat.id, "ℹ️ No scan requests found.", message.from_user.id)
                    return

                text = "🔍 Scan Requests:\n\n"
                for r in rows:
                    username = f"@{r['username']}" if r['username'] else "N/A"
                    first = r['first_name'] or "N/A"
                    last = r['last_name'] or "N/A"

                    text += (
                        f"🆔 ID: {r['id']}\n"
                        f"👤 User ID: {r['user_id']}\n"
                        f"🔗 Username: {username}\n"
                        f"📛 Name: {first} {last}\n"
                        f"🖥 Server: {r['server_id']}\n"
                        f"⚙️ Version: {r['version']}\n"
                        f"📌 Status: {r['status']}\n"
                        f"🌐 URL: {r['url']}\n"
                        f"📖 Title: {r['title'] or 'N/A'}\n"
                        f"📚 Volumes: {r['volume_count'] if r['volume_count'] is not None else 'N/A'} | "
                        f"Chapters: {r['chapter_count'] if r['chapter_count'] is not None else 'N/A'}\n"
                        f"❗ Error: {r['error'] or 'N/A'}\n"
                        f"🕒 Created: {r['created_at']}\n"
                        f"-----------------------------\n"
                    )
                send_temp_long_message(message.chat.id, text, message.from_user.id)
        finally:
            conn.close()


    @bot.message_handler(commands=['queue'])
    def queue(message):
        conn = get_db_connection()
        if not conn:
            send_temp_message(
                message.chat.id,
                "⚠️ Cannot connect to Database. If error persists, contact https://t.me/patiorjunrick004 with this message.",
                message.from_user.id
            )
            return

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, user_id, url, chapter_range, status, server_id, crawler_version, created_at
                    FROM tasks
                    WHERE status='pending'
                    ORDER BY created_at
                """)
                tasks = cur.fetchall()
                if not tasks:
                    send_temp_message(
                        message.chat.id,
                        "ℹ️ Queue is empty.",
                        message.from_user.id
                    )
                    return

                text = "📋 Pending tasks Queue:\n\n"

                for t in tasks:
                    text += (
                        f"🆔 Task ID: {t['id']}\n"
                        f"👤 User ID: {t['user_id']}\n"
                        f"🖥 Server: {t['server_id']}\n"
                        f"⚙️ Crawler: {t['crawler_version']}\n"
                        f"🌐 URL: {t['url']}\n"
                        f"📖 Range: {t['chapter_range']}\n"
                        f"📌 Status: {t['status']}\n"
                        f"🕒 Created: {t['created_at']}\n"
                        f"-----------------------------\n"
                    )

                send_temp_long_message(
                    message.chat.id,
                    text,
                    message.from_user.id
                )

        finally:
            conn.close()



    @bot.message_handler(commands=['tasks'])
    def tasks(message):
        conn = get_db_connection()
        if not conn:
            send_temp_message(
                message.chat.id,
                "⚠️ Cannot connect to Database, if error persists, contact https://t.me/patiorjunrick004 with this message.",
                message.from_user.id
            )
            return

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, user_id, url, chapter_range, status, server_id, crawler_version, created_at, completed_at
                    FROM tasks
                    ORDER BY created_at
                """)
                tasks = cur.fetchall()
                if not tasks:
                    send_temp_message(
                        message.chat.id,
                        "ℹ️ Queue is empty.",
                        message.from_user.id
                    )
                    return

                text = "📋 All tasks in Table:\n\n"

                for t in tasks:
                    text += (
                        f"🆔 Task ID: {t['id']}\n"
                        f"👤 User ID: {t['user_id']}\n"
                        f"🖥 Server: {t['server_id']}\n"
                        f"⚙️ Crawler: {t['crawler_version']}\n"
                        f"📌 Status: {t['status']}\n"
                        f"🌐 URL: {t['url']}\n"
                        f"📖 Range: {t['chapter_range']}\n"
                        f"🕒 Created: {t['created_at']}\n"
                        f"✅ Completed: {t['completed_at']}\n"
                        f"------------------------------\n"
                    )

                send_temp_long_message(
                    message.chat.id,
                    text,
                    message.from_user.id
                )

        finally:
            conn.close()

    @bot.message_handler(commands=['retry'])
    def retry_download(message):
        pending_retry_urls[message.from_user.id] = message.chat.id
        send_prompt(
            message.chat.id,
            "Send the same novel link within 20 hours to retry its missing chapters.\n"
            "Retries work only for completed v4 EPUBs that had missing chapters.",
            message.from_user.id,
        )
        bot.register_next_step_handler_by_chat_id(message.chat.id, receive_retry_url)

    def receive_retry_url(message):
        user_id = message.from_user.id
        chat_id = message.chat.id
        if pending_retry_urls.pop(user_id, None) != chat_id:
            return

        retry_url = message.text.strip()
        conn = get_db_connection()
        if not conn:
            send_temp_message(chat_id, "⚠️ Database error. Try again.", user_id)
            return

        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s);", (user_id,))
                cur.execute("DELETE FROM user_retry_slots WHERE user_id=%s AND first_retry_at < NOW() - INTERVAL '24 hours';", (user_id,))
                cur.execute("""
                    SELECT *
                    FROM tasks
                    WHERE user_id=%s
                      AND crawler_version='v4'
                      AND status='done'
                      AND missing_chapters > 0
                      AND retry_expires_at > NOW()
                      AND rtrim(url, '/') = rtrim(%s, '/')
                    ORDER BY completed_at DESC
                    LIMIT 1
                    FOR UPDATE;
                """, (user_id, retry_url))
                task = cur.fetchone()
                if not task:
                    conn.rollback()
                    send_temp_message(
                        chat_id,
                        "❌ No completed v4 EPUB with missing chapters was found for that link, or its 20-hour retry window expired.",
                        user_id,
                    )
                    return
                if not task['server_id']:
                    conn.rollback()
                    send_temp_message(chat_id, "❌ The original worker is unknown, so this EPUB cannot be retried safely.", user_id)
                    return

                cur.execute("SELECT 1 FROM user_retry_slots WHERE user_id=%s AND task_id=%s;", (user_id, task['id']))
                uses_existing_slot = cur.fetchone() is not None
                if not uses_existing_slot:
                    cur.execute("SELECT COUNT(*) AS count FROM user_retry_slots WHERE user_id=%s;", (user_id,))
                    if cur.fetchone()['count'] >= USER_RETRY_SLOT_LIMIT:
                        conn.rollback()
                        send_temp_message(
                            chat_id,
                            "❌ You can retry up to 3 different novels in each rolling 24-hour period. You can still retry a novel you already retried.",
                            user_id,
                        )
                        return
                    cur.execute("INSERT INTO user_retry_slots (user_id, task_id) VALUES (%s, %s);", (user_id, task['id']))

                cur.execute("""
                    UPDATE tasks
                    SET status='pending', completed_at=NULL, is_user_retry=TRUE,
                        retry_server_id=%s, retry_attempts=retry_attempts + 1
                    WHERE id=%s;
                """, (task['server_id'], task['id']))
                conn.commit()
        except Exception as error:
            conn.rollback()
            print(f"[USER RETRY ERROR] {error}")
            send_temp_message(chat_id, "❌ Could not queue the retry. Try again shortly.", user_id)
            return
        finally:
            conn.close()

        send_temp_message(
            chat_id,
            f"🔁 Retry queued for Task {task['id']}. It will run next on {task['server_id']} after that server finishes its current download.",
            user_id,
        )

    # ---------------------- DOWNLOAD COMMAND ----------------------
    @bot.message_handler(commands=['download'])
    def download(message):
        user_id = message.from_user.id

        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(
            telebot.types.InlineKeyboardButton(
                "⚙️ v3.10.1 (UnStable)", callback_data="version_v3"
            ),
            telebot.types.InlineKeyboardButton(
                "🚀 v4.13.1 (Stable)", callback_data="version_v4"
            )
        )
        markup.add(
            telebot.types.InlineKeyboardButton(
                "❓ Description", callback_data="version_description"
            )
        )

        sent = bot.send_message(
            message.chat.id,
            "Select crawler version:",
            reply_markup=markup
        )

        # ✅ Track this prompt so it can be deleted later
        pending_prompts[user_id] = sent

        
    def get_version(message):
        version = message.text.strip()
        if version not in ("v3", "v4"):
            send_temp_message(message.chat.id, "❌ Invalid version.", message.from_user.id)
            return

        send_prompt(message.chat.id, "📌 Enter novel URL:", message.from_user.id)
        bot.register_next_step_handler(message, get_url, version)


    def insert_task_direct(
        chat_id,
        url,
        chapter_range,
        version,
        user_id,
        username,
        first_name,
        last_name,
        novel_title=None,
        total_chapters=None,
    ):
        conn = get_db_connection()
        if not conn:
            send_temp_message(chat_id, "❌ Database error.", user_id)
            return False
        remaining_tasks = None
        daily_task_limit = None
        try:
            with conn.cursor() as cur:
                # Prevent two simultaneous requests by the same user bypassing the limit.
                cur.execute("SELECT pg_advisory_xact_lock(%s);", (user_id,))

                cur.execute("""
                    SELECT is_banned, daily_task_limit
                    FROM user_controls
                    WHERE user_id = %s;
                """, (user_id,))
                control = cur.fetchone()

                if control and control["is_banned"]:
                    conn.rollback()
                    send_temp_message(
                        chat_id,
                        "🚫 You cannot create download tasks with this bot.",
                        user_id,
                    )
                    return False

                if control and control["daily_task_limit"] is not None:
                    daily_task_limit = control["daily_task_limit"]
                    cur.execute("""
                        SELECT COUNT(*) AS task_count
                        FROM tasks
                        WHERE user_id = %s
                            AND status IN ('pending', 'running', 'done')
                            AND created_at >= NOW() - INTERVAL '24 hours';
                    """, (user_id,))
                    task_count = cur.fetchone()["task_count"]

                    if task_count >= control["daily_task_limit"]:
                        conn.rollback()
                        send_temp_message(
                            chat_id,
                            f"🚫 You have reached your limit of "
                            f"{control['daily_task_limit']} download task(s) "
                            f"within the last 24 hours.",
                            user_id,
                        )
                        return False
                    remaining_tasks = max(0, daily_task_limit - task_count - 1)
                # Send WTR-Lab tasks only to the separate local PC worker.
                host = urllib.parse.urlparse(url).hostname or ""
                if host.lower() in ("wtr-lab.com", "www.wtr-lab.com"):

                    chapter_range = "1 10"
                    total_chapters = 15

                cur.execute("""
                    INSERT INTO tasks (
                        user_id, chat_id, username, first_name, last_name,
                        url, chapter_range, crawler_version, novel_title,
                        total_chapters

                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """, (
                    user_id,
                    chat_id,
                    username,
                    first_name,
                    last_name,
                    url,
                    chapter_range,
                    version,
                    novel_title,
                    total_chapters,
                ))

                conn.commit()

        except Exception as error:
            conn.rollback()
            print(f"[TASK INSERT ERROR] {error}")
            send_temp_message(chat_id, "❌ Database error.", user_id)
            return False
        finally:
            conn.close()

        limit_note = ""
        if remaining_tasks is not None:
            limit_note = (
                f"\n\n📊 Daily task limit: "
                f"{remaining_tasks}/{daily_task_limit} task(s) remaining "
                f"within the next 24 hours."
            )

        send_temp_message(
            chat_id,
            "✅ Download task has been queued and will be processed shortly."
            f"{limit_note}\n\n"
            "Send /download to create a new task.",
            user_id,
        )
        return True



    def insert_task(
        message,
        url,
        chapter_range,
        version,
        user=None,
        novel_title=None,
        total_chapters=None,
    ):
        if user is None:
            user = message.from_user

        pending_range_state.pop(user.id, None)

        insert_task_direct(
            message.chat.id,
            url,
            chapter_range,
            version,
            user.id,
            user.username,
            user.first_name,
            user.last_name,
            novel_title,
            total_chapters,
        )




    def get_url(message, version):
        user_responded(message.from_user.id)
        url = message.text.strip()
        user_id = message.from_user.id
        chat_id = message.chat.id

        banned_site = find_banned_site(url)
        if banned_site:
            send_temp_message(
                chat_id,
                "This source is not supported by this bot.\n\n"
                f"Reason: {banned_site['ban_reason']}",
                user_id
            )
            return

         # 🔁 AUTO SWITCH v3 ➜ v4 for unsupported v3 sources
        if version == "v3" and is_v3_broken_source(url):
            send_temp_message(
                chat_id,
                "⚠️ This source does not work on v3.10.1\n\n"
                "🔁 Automatically switching to v4.13.1 ",
                user_id
            )
            version = "v4"

        # 🚫 BLOCK fanmtl & wuxiabox ON v4 ONLY
        if version == "v4" and is_v4_banned_source(url):
            send_temp_message(
                chat_id,
                "⚠️ This source is unstable on v4.13.1\n\n"
                "🔁 Automatically switching to v3.10.1",
                user_id
            )
            version = "v3"  # 👈 FORCE SWITCH


        pid = request_preview(
            url, version, user_id, chat_id,
            username=message.from_user.username,
            first_name=message.from_user.first_name,
            last_name=message.from_user.last_name
        )
        if pid is None:
            send_temp_message(chat_id, "❌ Database error, please try again.", user_id)
            return

        scan_cancel_event = threading.Event()
        skip_scan_events[pid] = scan_cancel_event

        markup = telebot.types.InlineKeyboardMarkup()
        markup.add(telebot.types.InlineKeyboardButton("⏭ Skip scan — enter range now", callback_data=f"skipscan_{pid}"))
        scanning_msg = bot.send_message(
            chat_id,
            "🔍 Scanning novel page...\n\nLarge novels can take a while — tap below to skip straight to entering the chapter range.",
            reply_markup=markup
        )

        threading.Thread(
            target=_await_preview_and_prompt_range,
            args=(message, url, version, user_id, chat_id, pid, scan_cancel_event, scanning_msg),
            daemon=True
        ).start()

    def _await_preview_and_prompt_range(message, url, version, user_id, chat_id, pid, scan_cancel_event, scanning_msg):
        row = wait_for_preview(pid, cancel_event=scan_cancel_event, timeout=7200)
        skip_scan_events.pop(pid, None)

        if row == "skipped" or row is None or (isinstance(row, dict) and row.get('status') == 'abandoned'):
            try:
                edit_and_autodelete(chat_id, scanning_msg.message_id, "⏭ Skipping scan — enter the chapter range manually.")
            except Exception:
                pass
            prompt_manual_range(message, chat_id, user_id, url, version)
            return

        if row['status'] == 'failed':
            edit_and_autodelete(
                chat_id, scanning_msg.message_id, f"❌ Couldn't read that page ({row['error']}).\nCheck the link and try again with /download."
            )
            return
        if not row['chapter_count']:
            edit_and_autodelete(chat_id, scanning_msg.message_id, "❌ No chapters found on that page. Check if the link leads to the novel's info page."
            )
            return

        edit_and_autodelete(
            chat_id, scanning_msg.message_id,
            f"📖 {row['title']}\n{row['volume_count']} volumes, {row['chapter_count']} chapters found.",
            delay=300  # auto-delete after 5 minutes
        )

        pending_urls[user_id] = {
    "url": url,
    "title": row["title"],
    "total_chapters": row["chapter_count"],
    "version": version,
}
        start_range_timeout(message.from_user, chat_id, url, version, row['title'], row['chapter_count'])

        ask_v4_range(chat_id, user_id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("skipscan_"))
    def handle_skip_scan(call):
        pid = int(call.data.replace("skipscan_", ""))
        ev = skip_scan_events.pop(pid, None)
        if ev:
            ev.set()
        # Also write the skip directly to the DB — this works even if the
        # background wait thread ends up running on a different process
        # than the one that receives this button click.
        _abandon_preview(pid)
        try:
            bot.answer_callback_query(call.id, "Skipping scan...")
        except Exception:
            pass

    def get_range(message, url, version, novel_title=None, total_chapters=None):
        user_responded(message.from_user.id)
        chapter_range = message.text.strip()
        user_id = message.from_user.id
        chat_id = message.chat.id
        pending_range_state.pop(user_id, None)

        insert_task(
            message,
            url,
            chapter_range,
            version,
            user=message.from_user,
            novel_title=novel_title,
            total_chapters=total_chapters,
        )

    def get_custom_range(message, url, version, novel_title, total_chapters):
        user_responded(message.from_user.id)

        user_id = message.from_user.id
        chat_id = message.chat.id
        pending_range_state.pop(user_id, None)

        text = message.text.strip().replace("–", "-")

        try:
            parts = re.findall(r"\d+", text)

            if len(parts) != 2:
                raise ValueError

            start, end = map(int, parts)

            if start < 1 or end < start:
                raise ValueError

            # When scan was not skipped, clamp to the real final chapter.
            if total_chapters is not None:
                start = min(start, total_chapters)
                end = min(end, total_chapters)

                if start > end:
                    raise ValueError

        except (ValueError, TypeError):
            send_temp_message(
                chat_id,
                "❌ Invalid range. Send it like: 40-60 or 40 60\n"
                "The first number must be at least 1 and cannot be above the last number.",
                user_id,
            )
            return

        chapter_range = f"{start} {end}"

        insert_task(
            message,
            url,
            chapter_range,
            version,
            user=message.from_user,
            novel_title=novel_title,
            total_chapters=total_chapters,
        )
        

    @bot.callback_query_handler(func=lambda call: call.data.startswith("version_"))
    def version_callback(call):
        user_responded(call.from_user.id)
        chat_id = call.message.chat.id
        user_id = call.from_user.id

        # Remove the inline keyboard (we'll re-show it if necessary)
        try:
            bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        except Exception:
            pass

        if call.data == "version_description":
            # Send the temporary description message
            send_temp_long_message(
                chat_id,
                "<b>📌 Crawler Versions Description</b>\n\n"
                " <b> • v3.10.1 </b>\n"
                "– Old version released on Jun 16, 2025\n"
                " ✅ Pros: \n"
                "--> It is faster.\n"
                " ⚠️ Cons: \n"
                "--> Some newer sites may not be supported.\n"
                "--> Most of the novels it downloads will have missing chapters.\n"
                
                " <b> • v4.13.1 ––Recommended</b>\n"
                "– Newest version cloned from github on July 27, 2026\n"

                " ✅ Pros: \n"
                "--> Supports newest sources/sites\n"
                "--> Some issues were resolved\n"
                " ⚠️ Cons: \n"
                "--> May be slower\n"
                "– The sources that v4.13.1 supports are here: https://github.com/dipu-bd/lightnovel-crawler but not all may work.\n"
                "– To ask for more sources to be added, or to report broken sources, create an issue here: https://github.com/dipu-bd/lightnovel-crawler/issues/new/choose\n"
                "– And if a new source is added or an issue is solved and you would like the bot updated, contact me at https://t.me/patiorjunrick004 \n\n",
                user_id,
                parse_mode="HTML"
            )


            # Re-show the version selection buttons after description
            markup = telebot.types.InlineKeyboardMarkup()
            markup.add(
                telebot.types.InlineKeyboardButton("⚙️ v3.10.1 (UnStable)", callback_data="version_v3"),
                telebot.types.InlineKeyboardButton("🚀 v4.13.1 (Stable)", callback_data="version_v4")
            )


            sent = bot.send_message(chat_id, "Select crawler version:", reply_markup=markup)
            pending_prompts[user_id] = sent
            
            bot.answer_callback_query(call.id)

            return

        # Existing version selection flow
        version = call.data.replace("version_", "")
        send_prompt(chat_id, "📌 Enter novel URL:", user_id)
        bot.register_next_step_handler_by_chat_id(chat_id, get_url, version)
        bot.answer_callback_query(call.id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("range_"))
    def handle_v4_range(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id

        user_responded(user_id)

        pending = pending_urls.pop(user_id, None)
        if not pending:
            bot.answer_callback_query(call.id, "❌ Session expired")
            return

        url = pending["url"]
        version = pending["version"]
        choice = call.data.replace("range_", "")
        try:
            bot.edit_message_reply_markup(
                chat_id,
                call.message.message_id,
                reply_markup=None,
            )
        except Exception:
            pass

        if choice == "all":
            insert_task(
                call.message,
                url,
                "all",
                version,
                user=call.from_user,
                novel_title=pending["title"],
                total_chapters=pending["total_chapters"],
            )
            bot.answer_callback_query(call.id)
            return

        if choice != "custom":
            bot.answer_callback_query(call.id, "Invalid range option.")
            return

        send_temp_message(
            chat_id,
            "↔️ Send the chapter range you want.\n\n"
            "Example: 40-60 or 40 60\n"
            "This downloads chapter 40 through chapter 60.",
            user_id,
        )

        bot.register_next_step_handler_by_chat_id(
            chat_id,
            get_custom_range,
            url,
            version,
            pending["title"],
            pending["total_chapters"],
        )
        bot.answer_callback_query(call.id)

    @bot.message_handler(commands=['logs'])
    def logs(message):
        # Check if the user is an admin
        if message.from_user.id not in ADMIN_USER_ID:
            bot.send_message(message.chat.id, "❌ Access Denied.")
            return
        
        conn = get_db_connection()
        if not conn:
            send_temp_message(
                message.chat.id,
                "⚠️ Cannot connect to Database. If the error persists, contact support. https://t.me/patiorjunrick004",
                message.from_user.id
            )
            return

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        id,
                        user_id,
                        username,
                        first_name,
                        last_name,
                        url,
                        chapter_range,
                        status,
                        server_id,
                        crawler_version,
                        created_at,
                        completed_at
                    FROM tasks
                    ORDER BY created_at
                """)
                tasks = cur.fetchall()

                if not tasks:
                    send_temp_message(
                        message.chat.id,
                        "ℹ️ No task logs found.",
                        message.from_user.id
                    )
                    return

                text = "📜 Task Logs (with user details):\n\n"

                for t in tasks:
                    username = f"@{t['username']}" if t['username'] else "N/A"
                    first = t['first_name'] or "N/A"
                    last = t['last_name'] or "N/A"

                    text += (
                        f"🆔 Task ID: {t['id']}\n"
                        f"👤 User ID: {t['user_id']}\n"
                        f"🔗 Username: {username}\n"
                        f"📛 Name: {first} {last}\n"
                        f"🖥 Server: {t['server_id']}\n"
                        f"⚙️ Crawler: {t['crawler_version']}\n"
                        f"📌 Status: {t['status']}\n"
                        f"🌐 URL: {t['url']}\n"
                        f"📖 Range: {t['chapter_range']}\n"
                        f"🕒 Created: {t['created_at']}\n"
                        f"✅ Completed: {t['completed_at']}\n"
                        f"-----------------------------\n"
                    )

                send_temp_long_message(
                    message.chat.id,
                    text,
                    message.from_user.id
                )

        finally:
            conn.close()


    @bot.message_handler(commands=['servers'])
    def list_servers(message):
        global notified_dead_servers
        conn = get_db_connection()
        if not conn:
            send_temp_message(message.chat.id, "⚠️ Cannot connect to DB", message.from_user.id)
            return

        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("SELECT server_id, last_heartbeat, status FROM servers ORDER BY server_id")
                servers = cur.fetchall()
                if not servers:
                    send_temp_message(message.chat.id, "ℹ️ No servers found.", message.from_user.id)
                    return
                
                text = "🖥️ Servers status:\n"
                for s in servers:
                    last_seen = s['last_heartbeat']
                    if last_seen:
                        last_seen += datetime.timedelta(hours=3)
                        last_seen_str = last_seen.strftime("%d-%m-%Y %H:%M:%S")
                    else:
                        last_seen_str = "Never"
                    text += f"- {s['server_id']} | Status: {s['status']} | Last heartbeat: {last_seen_str}\n"

                send_temp_long_message(message.chat.id, text, message.from_user.id)


                # Reset notified list so dead notifications can happen again
                notified_dead_servers.clear()
        finally:
            conn.close()
   

# ---------------------- CRAWLING WORKER ----------------------
def mark_failed(task_id, keep_retryable=False):
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                if keep_retryable:
                    cur.execute(
                        """
                        UPDATE tasks
                        SET status='done', completed_at=NOW(), is_user_retry=FALSE
                        WHERE id=%s AND status!='cancelled'
                        """,
                        (task_id,),
                    )
                else:
                    cur.execute(
                        """
                        UPDATE tasks
                        SET status='failed', completed_at=NOW(), retry_count = retry_count + 1
                        WHERE id=%s AND status!='cancelled'
                        """,
                        (task_id,),
                    )
            conn.commit()
        finally:
            conn.close()

def _touch_progress(task_id):
    conn = get_db_connection()
    if conn:
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE tasks SET last_progress_at=NOW() WHERE id=%s", (task_id,))
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()


def process_tasks_loop():
    while True:
        conn = None
        try:
            conn = get_db_connection()
            if not conn:
                time.sleep(10)
                continue

            with running_lock:
                busy = bool(running_processes)
            if busy:
                time.sleep(5)
                continue

            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                task = None

                if SERVER_ID == "s4":
                    # 1️⃣ s4 tries to pick a failed task from other servers first
                    cur.execute("""
                        SELECT *
                        FROM tasks
                        WHERE status='failed'
                          AND retry_count < 2
                          AND crawler_version= %s
                          AND NOT is_user_retry
                          AND (server_id IS NULL OR server_id != %s)
                        ORDER BY id ASC
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    """, (ALLOWED_CRAWLER, SERVER_ID))

                    task = cur.fetchone()

                    if task:
                        # Notify user that s2 is retrying their task
                        send_task_message(
                            task['chat_id'],
                            f"🔁 Retrying failed download...\n\n"
                            f"🆔 Task ID: {task['id']}\n"
                            f"🔗 URL: {task['url']}",
                            task['user_id']
                        )
                # 2️⃣ If no failed tasks (or not s2), pick a normal pending task
                if not task:
                    cur.execute("""
                        SELECT t.*
                        FROM tasks AS t
                        WHERE t.status = 'pending'
                          AND t.crawler_version = %s
                          AND (NOT t.is_user_retry OR t.retry_server_id = %s)
                          AND (t.is_user_retry OR t.server_id IS NULL OR t.server_id != %s)
                        ORDER BY
                            CASE WHEN t.is_user_retry THEN 0 ELSE 1 END,
                            CASE
                                WHEN (
                                    SELECT COUNT(*)
                                    FROM tasks AS user_tasks
                                    WHERE user_tasks.user_id = t.user_id
                                      AND user_tasks.status IN ('done', 'running')
                                      AND user_tasks.created_at >= NOW() - INTERVAL '1 day'
                                ) < 3
                                THEN 0
                                ELSE 1
                            END,
                            t.id ASC
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    """, (ALLOWED_CRAWLER, SERVER_ID, SERVER_ID))

                    task = cur.fetchone()

                if not task:
                    conn.commit()
                    time.sleep(5)
                    continue

                # Mark task as running and assign this server
                cur.execute(
                    "UPDATE tasks SET status='running', server_id=%s, run_id=%s WHERE id=%s",
                    (SERVER_ID, RUN_ID, task['id'])
                )
                conn.commit()

            # Perform crawling
            crawl_task(task)

        except Exception as e:
            print(f"[WORKER ERROR] {e}")
            time.sleep(5)
        finally:
            if conn:
                conn.close()


def crawl_task(task):
    user_id = task['user_id']
    chat_id = task['chat_id']

    # ---- HARD CRAWLER GUARD ----
    if task["crawler_version"] != ALLOWED_CRAWLER:
        print(
            f"[SKIP] Task {task['id']} "
            f"{task['crawler_version']} not allowed on {SERVER_ID}"
        )
        return

    url = task['url']
    chapter_range = task['chapter_range']
    task_id = task['id']
    
    temp_folder = os.path.join("./novels", f"{user_id}_{task_id}")

    os.makedirs(temp_folder, exist_ok=True)


    # ⚠️ wtr-lab warning (non-blocking)
    if "wtr" in url.lower():
        send_task_message(
            chat_id,
            "⚠️ Notice:\n"
            "This bot currently does NOT fully support wtr-lab.\n\n"
            "You'll probably receive an EPUB with the first ten chapters only.\n"
            "send /webtoepub to see how to download from unsupported sites like wtr-lab",
            user_id
        )
    elif any(site in url.lower() for site in ["novelhall", "fanmtl"]):
        send_task_message(
            chat_id,
            "⚠️ Notice:\nThis download might fail then retry once, please be patient.\n\n",
            user_id
        )

    send_long_temp_message(
        chat_id,
        "📚 Downloading novel...\nUse /cancel to stop.\nUse /download to create a new task to be queued.",
        user_id
    )

    # ---------------- direct library call, live progress (both v3 and v4) ----------------
    range_desc_holder = {"desc": format_range_desc(chapter_range, task.get('total_chapters') or 0)}
    title_line = f"📖 {task['novel_title']}\n\n" if task.get('novel_title') else ""

    def on_total_known(total):
        range_desc_holder["desc"] = format_range_desc(chapter_range, total)

    progress_msg = send_task_message_24h(chat_id, f"{title_line}📥 Downloading Task {task_id}...\n\n📊 Progress: 0.0%", user_id)
    cancel_event = threading.Event()
    crawl_started_at = time.time()
    with running_lock:
        running_processes[task_id] = cancel_event

    stop_watcher = threading.Event()
    def _watch_cancel():
        while not stop_watcher.is_set() and not cancel_event.is_set():
            conn = get_db_connection()
            if conn:
                try:
                    with conn.cursor() as cur:
                        cur.execute("SELECT status FROM tasks WHERE id=%s", (task_id,))
                        row = cur.fetchone()
                        if row and row['status'] == 'cancelled':
                            cancel_event.set()
                            return
                finally:
                    conn.close()
            time.sleep(10)
    threading.Thread(target=_watch_cancel, daemon=True).start()

    last_edit = {"t": 0.0}

    def format_progress_duration(seconds):
        seconds = max(0, int(round(seconds)))
        minutes, secs = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    def on_progress(done, total):
        pct = (done / total * 100) if total else 0
        now = time.time()
        if now - last_edit["t"] >= PROGRESS_UPDATE_INTERVAL or done == total:
            last_edit["t"] = now
            _touch_progress(task_id)
            elapsed = max(0.0, now - crawl_started_at)
            speed = elapsed / done if done else 0.0
            remaining = speed * max(0, total - done) if speed else 0.0
            text = (
                f"{title_line}"
                f"📥 Downloading Task {task_id}...\n\n"
                f"📊 Progress: {pct:.1f}%\n"
                f"📖 Currently At: Chapter {done}\n"
                f"🔍 Range: from {range_desc_holder['desc']}\n\n"
                f"📌 Status: Downloading chapters ({done}/{total})\n\n"
                f"⏱ Elapsed: {format_progress_duration(elapsed)}\n"
                f"⌛ Remaining: {format_progress_duration(remaining)}\n"
                f"⚡ Speed: {speed:.2f} s/chapter"
            )
            try:
                bot.edit_message_text(text, chat_id, progress_msg.message_id)
            except Exception:
                pass

    def on_image_progress(done, total):
        pct = (done / total * 100) if total else 0
        now = time.time()
        if now - last_edit["t"] >= PROGRESS_UPDATE_INTERVAL or done == total:
            last_edit["t"] = now
            _touch_progress(task_id)
            text = (
                f"{title_line}"
                "🖼 Downloading images...\n\n"
                f"📊 Progress: {pct:.1f}%\n"
                f"📌 Status: Fetching embedded images ({done}/{total})"
            )
            try:
                bot.edit_message_text(text, chat_id, progress_msg.message_id)
            except Exception:
                pass

    try:
        if task['crawler_version'] == "v4":
            epub_path, actual_total_chapters, novel_title, novel_synopsis, cover_source, missing_chapter_count, cache_novel_id = crawl_v4_with_progress(
                url,
                chapter_range,
                on_progress,
                cancel_event,
                on_image_progress=on_image_progress,
                on_total_known=on_total_known,
                resume_missing=bool(task.get("is_user_retry")),
            )
        else:
            epub_path, actual_total_chapters, novel_title, novel_synopsis, cover_source = crawl_v3_with_progress(
                url, chapter_range, on_progress, cancel_event, on_image_progress=on_image_progress, on_total_known=on_total_known
            )
            missing_chapter_count = 0
            cache_novel_id = None
    except Exception as e:
        import traceback
        stop_watcher.set()
        with running_lock:
            running_processes.pop(task_id, None)

        cleanup_bot_message(user_id, chat_id, progress_msg.message_id)

        if str(e) == "Cancelled":
            shutil.rmtree(temp_folder, ignore_errors=True)
            return  # /cancel already sent its own message + set status

        print(f"[CRAWL ERROR] Task {task_id} ({url}): {type(e).__name__}: {e}")
        traceback.print_exc()

        try:
            send_task_message(
                chat_id,
                f"⚠️ Download failed: {type(e).__name__}: {e}\n\n"
                f"🔗 {url}\n\n"
                f"Please check the link and make sure it leads to the novel info page. "
                f"Or send /sources to check if the site is supported.",
                user_id
            )
        except Exception as notify_err:
            print(f"[NOTIFY ERROR] Task {task_id}: {notify_err}")
        mark_failed(task_id, keep_retryable=bool(task.get("is_user_retry")))
        shutil.rmtree(temp_folder, ignore_errors=True)
        return
    
    stop_watcher.set()
    cleanup_bot_message(user_id, chat_id, progress_msg.message_id)

    epub_folder = os.path.dirname(epub_path)
    original_name = os.path.basename(epub_path)

    if task['crawler_version'] == "v4":
        name_root, ext = os.path.splitext(original_name)
        suffix = range_suffix_for_filename(chapter_range, actual_total_chapters)
        new_name = f"{name_root} {suffix}{ext}" if suffix else f"{name_root}{ext}"
        new_path = os.path.join(epub_folder, new_name)

        try:
            os.rename(epub_path, new_path)
            epub_files = [new_name]
        except Exception as e:
            print(f"[RENAME WARNING] Could not rename epub: {e}")
            epub_files = [original_name]
    else:
        # v3 already bakes the chapter range into its own generated filename
        epub_files = [original_name]

    # ---------------- shared: send epub(s), same for v3 & v4 ----------------
    try:
        final_msg = send_temp_message(
            chat_id,
            "✅ Download completed!\nYou will receive your file soon...\nBackup file will be sent to @novelsFinder\n",
            user_id
        )

        groups = [MY_GROUP_ID, OTHER_GROUP_ID, BOREAL_PRIVATE_GROUP_ID, BOREAL_OTHER_GROUP]
        delay = 5

        message_ids_to_forward = []

        # Message 1: cover image and compact information caption.
        # The filename without ".epub" includes the requested range, e.g. c1-500.
        first_file_name = epub_files[0]
        display_title = os.path.splitext(first_file_name)[0]

        cover_msg = send_cover_post(
            chat_id,
            cover_source,
            display_title,
            url,
            task["crawler_version"],
        )
        if cover_msg:
            message_ids_to_forward.append(cover_msg.message_id)

        # Message 2: synopsis only, split if Telegram's limit requires it.
        for synopsis_message in build_synopsis_messages(novel_synopsis):
            synopsis_msg = send_message_with_retry(
                chat_id,
                synopsis_message,
                parse_mode="HTML",
            )
            if synopsis_msg:
                message_ids_to_forward.append(synopsis_msg.message_id)

        # Message 3: EPUB with the novel filename as its caption.
        for file_name in epub_files:
            file_path = os.path.join(epub_folder, file_name)

            # EPUB caption: filename without the .epub extension.
            epub_caption = os.path.splitext(file_name)[0].strip()

            # Telegram document captions have a 1,024-character limit.
            if len(epub_caption) > 1024:
                epub_caption = epub_caption[:1021] + "..."

            print(f"[EPUB SEND] caption={epub_caption!r}")

            user_msg = send_document_with_retry(
                chat_id=chat_id,
                file_path=file_path,
                caption=epub_caption,
                max_retries=1,
            )

            if user_msg:
                print(
                    f"[EPUB SENT] message_id={user_msg.message_id}, "
                    f"telegram_caption={getattr(user_msg, 'caption', None)!r}"
                )
                message_ids_to_forward.append(user_msg.message_id)

            else:
                queued = queue_epub_upload_retry(task, file_path, epub_caption)

                conn = get_db_connection()
                if conn:
                    try:
                        with conn.cursor() as cur:
                            cur.execute("""
                                UPDATE tasks
                                SET status='upload_failed', completed_at=NOW()
                                WHERE id=%s AND status!='cancelled';
                            """, (task["id"],))
                            conn.commit()
                    finally:
                        conn.close()

                if queued:
                    print(
                        f"[EPUB SEND DEFERRED] Task {task['id']} will receive "
                        f"one automatic retry in {UPLOAD_RETRY_DELAY_MINUTES} minutes."
                    )
                else:
                    print(f"[EPUB SEND FAILED] Task {task['id']} could not be queued.")

                continue

        forward_messages_to_groups_sequentially(
            chat_id,
            message_ids_to_forward,
            groups,
            delay,
        )

        if final_msg:
            threading.Thread(
                target=delete_message_later,
                args=(final_msg.chat.id, final_msg.message_id, 60),
                daemon=True,
            ).start()

        # Mark task complete
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE tasks
                        SET status = CASE
                            WHEN status = 'upload_failed' THEN 'upload_failed'
                            ELSE 'done'
                        END,
                        completed_at=NOW(),
                        missing_chapters=%s,
                        retry_expires_at=COALESCE(
                            retry_expires_at,
                            NOW() + (%s * INTERVAL '1 hour')
                        ),
                        is_user_retry=FALSE,
                        cache_novel_id=COALESCE(%s, cache_novel_id)
                        WHERE id=%s AND status!='cancelled'
                        """,
                        (missing_chapter_count, USER_RETRY_WINDOW_HOURS, cache_novel_id, task['id']),
                    )

                conn.commit()
            finally:
                conn.close()

        if missing_chapter_count:
            send_task_message(
                chat_id,
                f"⚠️ This EPUB was delivered with {missing_chapter_count} missing chapter(s).\n\n"
                "Send /retry within 20 hours, then submit this same novel link to retry only the missing chapters.",
                user_id,
            )

    except Exception as e:
        import traceback
        print(f"[UPLOAD ERROR] Task {task['id']} ({url}): {type(e).__name__}: {e}")
        traceback.print_exc()
        send_temp_message(
            chat_id,
            f"⚠️ Your novel finished downloading, but sending the file to Telegram failed "
            f"after several attempts (likely a slow connection, try splitting the chapters into 2, download the first half then the last half): {type(e).__name__}: {e}\n\n"
            f"🔗 {url}\n\n",
            user_id
        )

        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE tasks SET status='upload_failed', completed_at=NOW() WHERE id=%s AND status!='cancelled'",
                        (task['id'],)
                    )
                conn.commit()
            finally:
                conn.close()

    finally:
        with running_lock:
            running_processes.pop(task_id, None)
        shutil.rmtree(temp_folder, ignore_errors=True)


def cleanup_expired_retry_cache():
    """Remove local v4 chapter caches only after their retry window has ended."""
    if ALLOWED_CRAWLER != "v4":
        return

    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, url, cache_novel_id
                FROM tasks
                WHERE crawler_version='v4'
                  AND server_id=%s
                  AND status='done'
                  AND retry_expires_at < NOW() - INTERVAL '4 hours'
                  AND cache_novel_id IS NOT NULL
                ORDER BY retry_expires_at ASC
                LIMIT 20;
            """, (SERVER_ID,))
            expired_tasks = cur.fetchall()

        for task in expired_tasks:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT 1
                    FROM tasks
                    WHERE server_id=%s
                      AND rtrim(url, '/')=rtrim(%s, '/')
                      AND id != %s
                      AND (
                          status IN ('pending', 'running')
                          OR (status='done' AND retry_expires_at > NOW())
                      )
                    LIMIT 1;
                """, (SERVER_ID, task['url'], task['id']))
                if cur.fetchone():
                    continue

            try:
                ctx, _ = _v4()
                from lncrawl.dao.novel import Novel

                with ctx.db.session() as session:
                    novel = session.get(Novel, task['cache_novel_id'])
                    if novel:
                        session.delete(novel)
                        session.commit()
                shutil.rmtree(ctx.files.resolve(f"novels/{task['cache_novel_id']}"), ignore_errors=True)

                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE tasks SET cache_novel_id=NULL WHERE id=%s AND cache_novel_id=%s",
                        (task['id'], task['cache_novel_id']),
                    )
                conn.commit()
                print(f"[RETRY CACHE CLEANUP] Removed Task {task['id']} cache")
            except Exception as error:
                conn.rollback()
                print(f"[RETRY CACHE CLEANUP ERROR] Task {task['id']}: {error}")
    finally:
        conn.close()


def retry_cache_cleanup_loop():
    while True:
        try:
            cleanup_expired_retry_cache()
        except Exception as error:
            print(f"[RETRY CACHE CLEANUP LOOP ERROR] {error}")
        time.sleep(60 * 30)


# ---------------------- CLEANUP ----------------------
def cleanup_lightnovels_folder():
    """
    Cleans leftover crawler artifacts (json, images, temp files).
    NOTE:
    - v4 EPUBs are resolved via ctx.files, v3 EPUBs land under ./Lightnovels
    - ./novels is cleaned per-task
    - Only ./Lightnovels accumulates trash (both v3 output and old v4 cache)
    """

    base_path = "./Lightnovels"
    if not os.path.exists(base_path):
        return
    try:
        for item in os.listdir(base_path):
            full_path = os.path.join(base_path, item)
            if item == "novels":
                continue
            if os.path.isdir(full_path):
                shutil.rmtree(full_path, ignore_errors=True)
            else:
                try:
                    os.remove(full_path)
                except:
                    pass
        print("[CLEANUP] Lightnovels folder cleaned")
    except Exception as e:
        print(f"[CLEANUP ERROR] {e}")

def cleanup_loop():
    while True:
        time.sleep(20 * 60)
        if not running_processes:
            cleanup_lightnovels_folder()

threading.Thread(target=cleanup_loop, daemon=True).start()
threading.Thread(target=process_tasks_loop, daemon=True).start()
def reap_orphaned_tasks():
    while True:
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("""
                        SELECT t.id, t.chat_id, t.user_id
                        FROM tasks t
                        LEFT JOIN servers s ON s.server_id = t.server_id
                        WHERE t.status = 'running'
                          AND (t.last_progress_at IS NULL OR t.last_progress_at < NOW() - INTERVAL '45 seconds')
                          AND (
                              s.server_id IS NULL
                              OR s.status = 'offline'
                              OR s.run_id IS DISTINCT FROM t.run_id
                              OR s.last_heartbeat < NOW() - INTERVAL '60 seconds'
                          )
                    """)
                    orphaned = cur.fetchall()
                    for t in orphaned:
                        cur.execute(
                            "UPDATE tasks SET status='failed', server_id=NULL, completed_at=NOW(), retry_count = retry_count + 1 WHERE id=%s",
                            (t['id'],)
                        )
                        try:
                            send_task_message(
                                t['chat_id'],
                                "⚠️ Your download was interrupted by a server restart. Retrying automatically, or send /download again.",
                                t['user_id']
                            )
                        except Exception:
                            pass
                    conn.commit()
            except Exception as e:
                print(f"[REAPER ERROR] {e}")
            finally:
                conn.close()
        time.sleep(30)

threading.Thread(target=reap_orphaned_tasks, daemon=True).start()




def send_heartbeat():
    while True:
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO servers(server_id, last_heartbeat, status, run_id)
                        VALUES (%s, NOW(), 'online', %s)
                        ON CONFLICT (server_id)
                        DO UPDATE SET last_heartbeat = NOW(), status='online', run_id=%s;
                    """, (SERVER_ID, RUN_ID, RUN_ID))
                    conn.commit()
            except Exception as e:
                print(f"[HEARTBEAT ERROR] {e}")
            finally:
                conn.close()
        time.sleep(5)  # This affects how frequently s1 updates its own status in the servers table.

threading.Thread(target=send_heartbeat, daemon=True).start()


# Keep track of which dead servers have already been notified
notified_dead_servers = set()

def check_dead_servers(timeout_seconds=15):
    global notified_dead_servers
    while True:
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute("""
                        SELECT server_id, last_heartbeat, status
                        FROM servers
                        WHERE last_heartbeat < NOW() - (%s * INTERVAL '1 second')
                        AND status = 'online'
                        AND server_id != %s;
                    """, (timeout_seconds, SERVER_ID))
                    dead_servers = cur.fetchall()
                    
                    for server in dead_servers:
                        server_id = server['server_id']

                        # Skip if already notified
                        if server_id in notified_dead_servers:
                            continue

                        last_seen = server['last_heartbeat'] + datetime.timedelta(hours=3)
                        last_seen_str = last_seen.strftime("%d-%m-%Y %H:%M:%S")
                        msg = f"⚠️ Server {server_id} is dead! Last heartbeat: {last_seen_str}"
                        print(msg)
                        try:
                            bot.send_message(OTHER_GROUP_ID, msg)
                        except Exception as e:
                            print(f"[Telegram notify failed] {e}")

                        notified_dead_servers.add(server_id)

                        # Mark server offline
                        cur.execute("UPDATE servers SET status='offline' WHERE server_id=%s", (server_id,))
                    conn.commit()
            except Exception as e:
                print(f"[CHECK DEAD SERVERS ERROR] {e}")
            finally:
                conn.close()
        time.sleep(1800) #determines how often Telegram notifications are sent for dead servers.
threading.Thread(target=check_dead_servers, daemon=True).start()

def cleanup_old_tasks():
    while True:
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cur:
                    # Keep retry-eligible tasks until their 20-hour retry window and
                    # 24-hour local-cache retention period have both elapsed.
                    cur.execute("""
                        DELETE FROM tasks
                        WHERE created_at IS NOT NULL
                          AND created_at < NOW() - INTERVAL '1 day'
                          AND (
                              retry_expires_at IS NULL
                              OR retry_expires_at < NOW() - INTERVAL '4 hours'
                          );
                    """)
                    conn.commit()
            except Exception as e:
                print(f"[CLEANUP tasks ERROR] {e}")
            finally:
                conn.close()
        time.sleep(60 * 60)  # run every hour


def process_pending_upload_retries():
    while True:
        upload = None
        conn = get_db_connection()

        if not conn:
            time.sleep(60)
            continue

        try:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT *
                    FROM pending_uploads
                    WHERE server_id = %s
                      AND status = 'pending'
                      AND attempts < 2
                      AND next_retry_at <= NOW()
                      AND expires_at > NOW()
                    ORDER BY next_retry_at ASC
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED;
                """, (SERVER_ID,))
                upload = cur.fetchone()

                if upload:
                    cur.execute("""
                        UPDATE pending_uploads
                        SET status='sending',
                            attempts=attempts + 1
                        WHERE id=%s;
                    """, (upload["id"],))

                conn.commit()

        except Exception as error:
            conn.rollback()
            print(f"[UPLOAD RETRY PICK ERROR] {error}")
        finally:
            conn.close()

        if not upload:
            time.sleep(30)
            continue

        retry_number = upload["attempts"] + 1

        sent_message = send_document_with_retry(
            chat_id=upload["chat_id"],
            file_path=upload["file_path"],
            caption=upload["caption"],
            max_retries=1,
        )

        conn = get_db_connection()
        if not conn:
            time.sleep(30)
            continue

        try:
            with conn.cursor() as cur:
                if sent_message:
                    cur.execute("""
                        UPDATE pending_uploads
                        SET status='sent', sent_at=NOW(), error=NULL
                        WHERE id=%s;
                    """, (upload["id"],))

                    cur.execute("""
                        UPDATE tasks
                        SET status='done', completed_at=NOW()
                        WHERE id=%s AND status='upload_failed';
                    """, (upload["task_id"],))

                    try:
                        os.remove(upload["file_path"])
                    except OSError:
                        pass

                    print(f"[UPLOAD RETRY SENT] Task {upload['task_id']}")

                elif retry_number < 2:
                    cur.execute("""
                        UPDATE pending_uploads
                        SET status='pending',
                            next_retry_at=NOW() + (%s * INTERVAL '1 minute'),
                            error='First Telegram EPUB retry failed'
                        WHERE id=%s;
                    """, (
                        UPLOAD_RETRY_SECOND_DELAY_MINUTES,
                        upload["id"],
                    ))

                    print(f"[UPLOAD RETRY 1 FAILED] Task {upload['task_id']}")

                else:
                    cur.execute("""
                        UPDATE pending_uploads
                        SET status='held',
                            error='Both delayed Telegram EPUB retries failed'
                        WHERE id=%s;
                    """, (upload["id"],))

                    print(
                        f"[UPLOAD RETRIES EXHAUSTED] Task {upload['task_id']} "
                        f"will be deleted at expiry."
                    )

                conn.commit()

        except Exception as error:
            conn.rollback()
            print(f"[UPLOAD RETRY UPDATE ERROR] {error}")
        finally:
            conn.close()

        time.sleep(3)

def cleanup_expired_pending_uploads():
    while True:
        conn = get_db_connection()

        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT id, file_path
                        FROM pending_uploads
                        WHERE expires_at <= NOW()
                          AND status IN ('held', 'failed', 'pending', 'sending');
                    """)
                    expired_uploads = cur.fetchall()

                    for upload in expired_uploads:
                        try:
                            if os.path.isfile(upload["file_path"]):
                                os.remove(upload["file_path"])
                        except OSError as error:
                            print(f"[PENDING EPUB DELETE ERROR] {error}")

                        cur.execute("""
                            UPDATE pending_uploads
                            SET status='expired'
                            WHERE id=%s;
                        """, (upload["id"],))

                    conn.commit()

            except Exception as error:
                conn.rollback()
                print(f"[PENDING EPUB CLEANUP ERROR] {error}")
            finally:
                conn.close()

        time.sleep(60 * 60)

threading.Thread(target=cleanup_old_tasks, daemon=True).start()
threading.Thread(target=process_pending_upload_retries, daemon=True).start()
threading.Thread(target=cleanup_expired_pending_uploads, daemon=True).start()
threading.Thread(target=retry_cache_cleanup_loop, daemon=True).start()


# ---------------------- RUN BOT ----------------------
if SERVER_ID == "s1":
    print("[MODE] s1 – polling enabled")
    bot.infinity_polling()

else:
    print(f"[MODE] {SERVER_ID} – worker only (no polling)")
    while True:
        time.sleep(10)

