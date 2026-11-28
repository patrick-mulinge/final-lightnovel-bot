import telebot
import subprocess
import os
import shutil
import threading
import queue
import time
import datetime
import psycopg2
from psycopg2.extras import RealDictCursor, Json
import re
import html
import json
import urllib.parse
import requests

import telebot.apihelper as apihelper
apihelper.READ_TIMEOUT = 600
apihelper.CONNECT_TIMEOUT = 60

# ---------------------- LOCAL BOT API (2000 MB support) ----------------------
# LOCAL_BOT_API_URL example: "http://123.45.67.89:8081"
# Empty = always use the official cloud API (50 MB).
#
# s4 is the only process that calls Telegram logOut and decides the mode.
# Every other worker only copies that decision from bot_settings, so they
# stay on the same endpoint and never log the bot out themselves.
#
# Local is primary. These defaults are overridden by the same environment variables.
#   - Local down for LOCAL_API_FALLBACK_AFTER_MINUTES (20): switch to the cloud API.
#     logOut on the dead local server is attempted once; failure does not delay the switch.
#   - LOCAL_API_MAX_SILENCE_MINUTES (30) caps how long workers stay on a dead local server
#     once the cloud API is allowed again.
#   - When local is back: logOut that leftover local session (only if the earlier
#     logOut failed), logOut the cloud API, then use local immediately.
#   - After a cloud logOut, do not use the cloud API again for 15 minutes.
LOCAL_BOT_API_URL = os.environ.get("LOCAL_BOT_API_URL", "http://141.148.131.213:8081").rstrip("/")
OFFICIAL_API_URL = "https://api.telegram.org/bot{0}/{1}"
LOCAL_API_FALLBACK_AFTER_MINUTES = int(os.environ.get("LOCAL_API_FALLBACK_AFTER_MINUTES", "20"))
LOCAL_API_MAX_SILENCE_MINUTES = int(os.environ.get("LOCAL_API_MAX_SILENCE_MINUTES", "30"))
TELEGRAM_CLOUD_LOCKOUT_MINUTES = 15

API_MODE_KEY = "telegram_api_mode"
API_DOWN_SINCE_KEY = "telegram_api_local_down_since"
API_CLOUD_BLOCKED_UNTIL_KEY = "telegram_api_cloud_blocked_until"
API_PENDING_LOCAL_LOGOUT_KEY = "telegram_api_pending_local_logout"

_local_api_available = False
_local_api_lock = threading.RLock()

def _get_api_url():
    with _local_api_lock:
        if _local_api_available and LOCAL_BOT_API_URL:
            return f"{LOCAL_BOT_API_URL}/bot{{0}}/{{1}}"
        return OFFICIAL_API_URL

def _apply_api_url(use_local: bool, reason: str = ""):
    global _local_api_available
    with _local_api_lock:
        prev = _local_api_available
        _local_api_available = bool(use_local and LOCAL_BOT_API_URL)
        apihelper.API_URL = _get_api_url()
        if prev != _local_api_available:
            if _local_api_available:
                print(f"[LOCAL BOT API] Using LOCAL server {LOCAL_BOT_API_URL} {reason}")
            else:
                print(f"[LOCAL BOT API] Using official cloud API {reason}")

def check_local_bot_api_health():
    if not LOCAL_BOT_API_URL:
        return False
    try:
        # The server resets bare "/" requests, so probe a real API path with a fake token.
        # Any HTTP answer below 500 (401 expected) means the server is up.
        response = requests.get(f"{LOCAL_BOT_API_URL}/bot0:health/getMe", timeout=5)
        return response.status_code < 500
    except Exception:
        return False

def _api_setting_get(key, default=""):
    conn = get_db_connection()
    if not conn:
        return default
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT setting_value FROM bot_settings WHERE setting_key=%s",
                (key,),
            )
            row = cur.fetchone()
            return row["setting_value"] if row and row["setting_value"] is not None else default
    except Exception as error:
        print(f"[LOCAL BOT API] read {key}: {error}")
        return default
    finally:
        conn.close()

def _api_setting_set(key, value):
    conn = get_db_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO bot_settings (setting_key, setting_value, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (setting_key) DO UPDATE
                SET setting_value = EXCLUDED.setting_value,
                    updated_at = NOW();
            """, (key, "" if value is None else str(value)))
            conn.commit()
        return True
    except Exception as error:
        conn.rollback()
        print(f"[LOCAL BOT API] write {key}: {error}")
        return False
    finally:
        conn.close()

def telegram_bot_logout(use_local):
    """logOut on the endpoint being left. True if Telegram accepted it or the bot was already logged out."""
    if use_local:
        if not LOCAL_BOT_API_URL:
            return False
        url = f"{LOCAL_BOT_API_URL}/bot{TOKEN}/logOut"
        where = "local"
    else:
        url = f"https://api.telegram.org/bot{TOKEN}/logOut"
        where = "cloud"
    try:
        response = requests.post(url, timeout=30)
        try:
            data = response.json()
        except Exception:
            data = {}
        if data.get("ok") is True:
            print(f"[LOCAL BOT API] logOut succeeded on {where}")
            return True
        description = str(data.get("description") or response.text or "").lower()
        if any(part in description for part in ("logged out", "unauthorized", "not found")):
            print(f"[LOCAL BOT API] logOut on {where} already done: {description[:200]}")
            return True
        print(f"[LOCAL BOT API] logOut failed on {where}: {response.status_code} {description[:300]}")
        return False
    except Exception as error:
        print(f"[LOCAL BOT API] logOut error on {where}: {error}")
        return False

def _follow_shared_api_mode():
    mode = _api_setting_get(API_MODE_KEY, "cloud")
    _apply_api_url(mode == "local", "(shared setting)")

def local_bot_api_follower_loop():
    while True:
        try:
            _follow_shared_api_mode()
        except Exception as error:
            print(f"[LOCAL BOT API] follower: {error}")
        time.sleep(30)

def local_bot_api_controller_loop():
    """s4 only. Calls logOut, then publishes local vs cloud for every worker."""
    while True:
        try:
            local_up = check_local_bot_api_health()
            mode = _api_setting_get(API_MODE_KEY, "")
            if mode not in ("local", "cloud"):
                # Production today is the cloud API. Start there so the first
                # move onto local goes through logOut instead of skipping it.
                mode = "cloud"
                _api_setting_set(API_MODE_KEY, mode)

            now = time.time()
            try:
                cloud_blocked_until = float(_api_setting_get(API_CLOUD_BLOCKED_UNTIL_KEY, "0") or 0)
            except ValueError:
                cloud_blocked_until = 0
            down_raw = _api_setting_get(API_DOWN_SINCE_KEY, "")
            try:
                down_since = float(down_raw) if down_raw else None
            except ValueError:
                down_since = None
            pending_local_logout = _api_setting_get(API_PENDING_LOCAL_LOGOUT_KEY, "0") == "1"

            if local_up:
                if down_since is not None:
                    _api_setting_set(API_DOWN_SINCE_KEY, "")
                if mode != "local":
                    if pending_local_logout:
                        if telegram_bot_logout(True):
                            _api_setting_set(API_PENDING_LOCAL_LOGOUT_KEY, "0")
                        else:
                            print("[LOCAL BOT API] local is back; leftover local logOut failed, retrying before cloud logOut")
                            time.sleep(30)
                            continue
                    if telegram_bot_logout(False):
                        _api_setting_set(API_CLOUD_BLOCKED_UNTIL_KEY, str(now + TELEGRAM_CLOUD_LOCKOUT_MINUTES * 60))
                        _api_setting_set(API_MODE_KEY, "local")
                        _apply_api_url(True, "(logOut on cloud, switched to local)")
                    else:
                        print("[LOCAL BOT API] cloud logOut failed; staying on cloud")
                        _apply_api_url(False)
                else:
                    _apply_api_url(True)
            else:
                if down_since is None:
                    down_since = now
                    _api_setting_set(API_DOWN_SINCE_KEY, str(now))
                    print(
                        f"[LOCAL BOT API] local unreachable. "
                        f"Cloud API after {min(LOCAL_API_FALLBACK_AFTER_MINUTES, LOCAL_API_MAX_SILENCE_MINUTES)} min"
                    )
                down_for = now - down_since
                switch_after = min(LOCAL_API_FALLBACK_AFTER_MINUTES, LOCAL_API_MAX_SILENCE_MINUTES) * 60
                if mode == "local":
                    if now < cloud_blocked_until:
                        print(
                            f"[LOCAL BOT API] local down, cloud still locked for "
                            f"{int(cloud_blocked_until - now)}s after the last cloud logOut"
                        )
                    elif down_for >= switch_after:
                        logged_out = telegram_bot_logout(True)
                        _api_setting_set(API_PENDING_LOCAL_LOGOUT_KEY, "0" if logged_out else "1")
                        _api_setting_set(API_MODE_KEY, "cloud")
                        _apply_api_url(False, f"({int(down_for / 60)} min down, switched to cloud)")
                else:
                    _apply_api_url(False)
        except Exception as error:
            print(f"[LOCAL BOT API] controller: {error}")
        time.sleep(30)

if not LOCAL_BOT_API_URL:
    apihelper.API_URL = OFFICIAL_API_URL
    print("[LOCAL BOT API] No LOCAL_BOT_API_URL set – using official Telegram cloud only")

# import logging
# telebot.logger.setLevel(logging.DEBUG)  # noisy: logs every Telegram API call

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
MY_GROUP_ID = "@hi"
BOREAL_PRIVATE_GROUP_ID = -1003389806855
BOREAL_OTHER_GROUP = "@selesste"
OTHER_GROUP_ID = "@patiorjunrick1"
BLOCKED_GROUP_USERNAME = "@hi"
ADMIN_USER_ID = [7994985826,  8402824940, 7378945311]
UPLOAD_RETRY_DELAY_MINUTES = 10
UPLOAD_RETRY_KEEP_HOURS = 12
UPLOAD_RETRY_SECOND_DELAY_MINUTES = 20
PENDING_UPLOAD_DIR = "./pending_epub_uploads"
PROGRESS_UPDATE_INTERVAL = 10  # seconds (raised to reduce Telegram rate-limit risk)
PREVIEW_RETRY_SERVER = "s4"
USER_RETRY_WINDOW_HOURS = 20
USER_RETRY_SLOT_LIMIT = 3
# Max concurrent *live* progress edits a single user may receive.
# Users with more than this many running tasks get a one-shot notice instead.
MAX_LIVE_PROGRESS_PER_USER = 1
# Hard wall-clock limit for a single crawl attempt on one worker.
# After this the task is stopped, marked so the user can /retry, and cache is kept.
HARD_TASK_LIMIT_HOURS = 10
# If a held task has had no progress for this long, requeue it for another worker.
STUCK_PROGRESS_MINUTES = 20


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
    
if SERVER_ID in ("s1", "s2"):
    ALLOWED_CRAWLER = "v3"
else:
    ALLOWED_CRAWLER = "v4"

# ---- v3 RETIRED ----
# v3's sources no longer work, so every request (picker, URL flow, retries,
# stale buttons, old queued rows) is rerouted to v4. Flip V3_DISABLED to
# False to bring v3 back; nothing else needs to change.
V3_DISABLED = True

def force_v4(version=None):
    """Return the crawler version that should actually be used."""
    if V3_DISABLED:
        return "v4"
    return version if version in ("v3", "v4") else "v4"

# A server whose installed lncrawl is v3 cannot run v4, so while v3 is
# disabled it must not claim scans or tasks at all (it still polls Telegram
# if it is s4).
WORKER_ENABLED = not (V3_DISABLED and ALLOWED_CRAWLER == "v3")

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
                    crawler_version TEXT DEFAULT 'v4',
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
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS error TEXT;")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS output_format TEXT NOT NULL DEFAULT 'epub';")
            # Cross-worker /progress fields (written every progress tick)
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS progress_done INT;")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS progress_total INT;")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS progress_pct REAL;")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS progress_status TEXT;")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS progress_elapsed REAL;")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS progress_remaining REAL;")
            cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS progress_speed REAL;")
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

def setup_search_table():
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS search_requests (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    chat_id BIGINT,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    query TEXT NOT NULL,
                    mode TEXT DEFAULT 'search',
                    status TEXT NOT NULL DEFAULT 'pending',
                    queued BOOLEAN DEFAULT FALSE,
                    origin_server TEXT,
                    origin_run_id TEXT,
                    server_id TEXT,
                    run_id TEXT,
                    attempts INT DEFAULT 0,
                    source_count INT,
                    result_count INT,
                    results JSONB,
                    error TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    started_at TIMESTAMP,
                    completed_at TIMESTAMP
                );
            """)
            cur.execute("ALTER TABLE search_requests ADD COLUMN IF NOT EXISTS progress_done INT;")
            cur.execute("ALTER TABLE search_requests ADD COLUMN IF NOT EXISTS progress_total INT;")
            cur.execute("ALTER TABLE search_requests ADD COLUMN IF NOT EXISTS partial_results JSONB;")
            cur.execute("ALTER TABLE search_requests ADD COLUMN IF NOT EXISTS cached BOOLEAN DEFAULT FALSE;")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS search_source_health (
                    url TEXT PRIMARY KEY,
                    dns_failures INT DEFAULT 0,
                    dropped BOOLEAN DEFAULT FALSE,
                    dropped_at TIMESTAMP,
                    last_error TEXT,
                    updated_at TIMESTAMP DEFAULT NOW()
                );
            """)
            cur.execute("ALTER TABLE search_source_health ADD COLUMN IF NOT EXISTS priority_tier INT;")
            cur.execute("ALTER TABLE search_source_health ADD COLUMN IF NOT EXISTS reply_seconds REAL;")
            cur.execute("ALTER TABLE search_source_health ADD COLUMN IF NOT EXISTS result_count INT;")
            cur.execute("ALTER TABLE search_source_health ADD COLUMN IF NOT EXISTS last_probe_at TIMESTAMP;")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS search_test_reports (
                    id SERIAL PRIMARY KEY,
                    search_id INT,
                    query TEXT,
                    timeout_seconds INT,
                    data JSONB,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_search_requests_status ON search_requests (status, id);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_search_requests_created ON search_requests (created_at);")
            conn.commit()
            print("[DB] search_requests table ready")
    except Exception as e:
        conn.rollback()
        print(f"[DB SETUP ERROR] search_requests: {e}")
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
    setup_search_table()
    setup_server_table()
    setup_user_controls_table()
    setup_pending_uploads_table()
    setup_site_management_tables()
    if V3_DISABLED:
        _mconn = get_db_connection()
        if _mconn:
            try:
                with _mconn.cursor() as _cur:
                    _cur.execute("UPDATE tasks SET crawler_version='v4' WHERE crawler_version='v3'")
                    _t = _cur.rowcount
                    _cur.execute("UPDATE preview_requests SET version='v4' WHERE version='v3'")
                    _p = _cur.rowcount
                    _cur.execute("UPDATE preview_requests SET status='pending', server_id=NULL, run_id=NULL WHERE version='v4' AND status='processing' AND server_id IN ('s1','s2')")
                _mconn.commit()
                print(f"[V3 DISABLED] moved {_t} task(s) and {_p} scan(s) from v3 to v4")
            except Exception as _e:
                _mconn.rollback()
                print(f"[V3 DISABLED] migration error: {_e}")
            finally:
                _mconn.close()
else:
    print("[DB] Connection failed")
# ---------------------- BOT SETUP ----------------------
class BotExceptionHandler(telebot.ExceptionHandler):
    def handle(self, exception):
        print(f"[HANDLER EXCEPTION] {exception}")
        return True  # tells telebot the exception was handled — worker thread survives

bot = telebot.TeleBot(TOKEN, num_threads=20, exception_handler=BotExceptionHandler())

running_processes = {}  # task_id -> threading.Event (v3 & v4 cancel signal)
running_lock = threading.Lock()
# Separate dict — NEVER attach attributes to running_processes (plain dict → AttributeError).
progress_snapshots = {}  # task_id -> latest progress snapshot (this process only)
pending_prompts = {}  # user_id -> pending bot message
last_bot_messages = {}  # user_id -> list of final messages
pending_urls = {}
pending_retry_urls = {}  # user_id -> chat_id; the next message must be the completed task URL
pending_cancel_tasks = {}  # user_id -> ordered list of cancelable tasks, expires after 10 minutes
pending_reply_states = {}  # chat_id -> {token, user_id, kind}; timed text-reply prompts only

pending_range_state = {}  # user_id -> {token, chat_id, url, version, title, total_chapters, user_id, username, first_name, last_name}

REPLY_PROMPT_TIMEOUT_SECONDS = 600


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

def _clear_timed_reply(chat_id, user_id=None):
    """Remove one timed text prompt and its TeleBot next-step callback."""
    state = pending_reply_states.pop(chat_id, None)
    owner_id = user_id if user_id is not None else (state or {}).get("user_id")

    try:
        bot.clear_step_handler_by_chat_id(chat_id)
    except Exception:
        pass

    if owner_id is not None:
        pending_retry_urls.pop(owner_id, None)
        pending_cancel_tasks.pop(owner_id, None)


def _complete_timed_reply(chat_id, user_id):
    """Mark the active timed prompt answered without clearing a newer callback."""
    state = pending_reply_states.get(chat_id)
    if state and state["user_id"] == user_id:
        pending_reply_states.pop(chat_id, None)


def _reply_prompt_timeout(chat_id, token, delay):
    time.sleep(delay)
    state = pending_reply_states.get(chat_id)
    if not state or state["token"] != token:
        return
    user_id = state["user_id"]
    _clear_timed_reply(chat_id, user_id)
    send_temp_message(
        chat_id,
        "This request has ended. Send the command again when you are ready.",
        user_id,
    )

def register_timed_next_step(chat_id, user_id, handler, kind, *args, timeout=None):
    """Replace any older text reply prompt in this chat and expire this one safely."""
    if timeout is None:
        timeout = REPLY_PROMPT_TIMEOUT_SECONDS
    _clear_timed_reply(chat_id)
    token = uuid.uuid4().hex
    pending_reply_states[chat_id] = {
        "token": token,
        "user_id": user_id,
        "kind": kind,
    }
    bot.register_next_step_handler_by_chat_id(chat_id, handler, *args)
    threading.Thread(
        target=_reply_prompt_timeout,
        args=(chat_id, token, timeout),
        daemon=True,
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
    on_novel_id=None,
    output_format="epub",
    on_convert_start=None,
    on_epub_ready=None,
    on_part_ready=None,
    on_convert_note=None,
):
    """Download the selected v4 chapters once, then fetch images and create the EPUB
    (or the requested output format)."""
    


    ctx, OutputFormat = _v4()
    output_format = normalize_output_format(output_format)
    target_format = OutputFormat(output_format)
    # Fail fast (before downloading anything) if this format needs Calibre and it is missing.
    if target_format not in ctx.binder.available_formats:
        raise Exception(
            f"{output_format.upper()} output needs Calibre (ebook-convert), which is not "
            "installed on this worker. Choose EPUB or Text instead."
        )
    user = get_admin_user_v4()
    crawler = ctx.sources.init_crawler(url)


    novel = ctx.crawler.fetch_novel(user.id, url, custom=crawler)

    # Report the cache id immediately so a hard stop / crash can still record it.
    if on_novel_id:
        try:
            on_novel_id(str(novel.id))
        except Exception as e:
            print(f"[ON_NOVEL_ID] {e}")

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

    if target_format == OutputFormat.text:
        # Text is built natively from the chapters (a zip of .txt files); no EPUB needed.
        artifact = ctx.binder.make_artifact(
            novel.id,
            novel.title,
            format=OutputFormat.text,
            user_id=user.id,
            signal=cancel_event,
        )
        if not artifact.is_available:
            raise Exception("Failed to generate TEXT output")
    else:
        artifact = ctx.binder.make_artifact(
            novel.id,
            novel.title,
            format=OutputFormat.epub,
            user_id=user.id,
        )
        if not artifact.is_available:
            raise Exception("Failed to generate EPUB")

        if target_format != OutputFormat.epub:
            # The EPUB is finished: let the caller hand it to the user right away
            # (cover, synopsis, EPUB) so they can read it while Calibre converts.
            if on_epub_ready:
                try:
                    on_epub_ready({
                        "epub_path": str(ctx.files.resolve(artifact.output_file)),
                        "title": novel.title,
                        "synopsis": novel.synopsis or "",
                        "cover_source": (
                            str(ctx.files.resolve(novel.cover_file))
                            if novel.cover_available else ""
                        ),
                        "chapter_count": novel.chapter_count,
                    })
                except Exception as e:
                    print(f"[EPUB READY NOTIFY] {e}")
            # Calibre converts the EPUB we just built into the requested format.
            if on_convert_start:
                try:
                    on_convert_start(output_format)
                except Exception as e:
                    print(f"[CONVERT NOTIFY] {e}")
            # Big books are converted in parts (see SPLIT SIZES): each part goes to
            # on_part_ready as soon as it is finished.
            split_size = get_split_size(output_format)
            use_parts = False
            _calibre = None
            if split_size and on_part_ready:
                try:
                    from lncrawl.services.binder import calibre as _calibre
                    use_parts = _calibre.count_epub_chapters(artifact) > split_size
                except Exception as e:
                    print(f"[SPLIT] could not count chapters, converting in one piece: {e}")
            if use_parts:
                epub_artifact = artifact
                epub_stem = split_artifact_name(
                    os.path.basename(str(ctx.files.resolve(epub_artifact.output_file)))
                )[0]
                parts_dir = ctx.files.resolve(epub_artifact.output_file).parent / "parts"
                try:
                    _calibre.convert_epub_parts(
                        epub_artifact,
                        target_format,
                        split_size,
                        parts_dir,
                        epub_stem,
                        signal=cancel_event,
                        on_part_ready=on_part_ready,
                    )
                except Exception:
                    if cancel_event.is_set():
                        raise Exception("Cancelled")
                    raise
                finally:
                    shutil.rmtree(parts_dir, ignore_errors=True)
                # `artifact` stays the EPUB; the caller ignores it because the parts
                # were already delivered through on_part_ready.
            else:
                try:
                    artifact = ctx.binder.make_artifact(
                        novel.id,
                        novel.title,
                        format=target_format,
                        user_id=user.id,
                        epub=artifact,
                        signal=cancel_event,
                    )
                except Exception:
                    if cancel_event.is_set():
                        raise Exception("Cancelled")
                    raise
                if not artifact.is_available:
                    raise Exception(f"Failed to convert EPUB to {output_format.upper()}")
                # Chapters that Calibre could not render were cleaned or skipped.
                try:
                    from lncrawl.services.binder import calibre as _calibre_rep
                    report = _calibre_rep.convert_reports.pop(cancel_event, None)
                    if report and on_convert_note:
                        on_convert_note(report)
                except Exception as e:
                    print(f"[CONVERT NOTE] {e}")

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


# ---------------------- OUTPUT FORMATS ----------------------
# key -> (button label, native?)  native = built by lncrawl itself, no Calibre.
# Everything else is converted from the EPUB by Calibre's ebook-convert.
OUTPUT_FORMATS = {
    "epub": ("EPUB", True),
    "txt":  ("Text", True),
}
DEFAULT_OUTPUT_FORMAT = "epub"
FORMAT_SESSION_SECONDS = 600
pending_format_state = {}  # user_id -> {expires, chat_id, url, chapter_range, version, ...}

def normalize_output_format(value):
    value = (value or DEFAULT_OUTPUT_FORMAT).strip().lower()
    return value if value in OUTPUT_FORMATS else DEFAULT_OUTPUT_FORMAT

def output_format_label(fmt):
    return OUTPUT_FORMATS[normalize_output_format(fmt)][0]

def split_artifact_name(name):
    """('Title', '.txt.zip') / ('Title', '.pdf'). Text output is a .txt.zip."""
    lower = name.lower()
    if lower.endswith(".txt.zip"):
        return name[:-len(".txt.zip")], ".txt.zip"
    root, ext = os.path.splitext(name)
    return root, ext

def format_picker_markup(show_all=False):
    markup = telebot.types.InlineKeyboardMarkup()
    markup.row(
        telebot.types.InlineKeyboardButton("EPUB", callback_data="fmt_epub"),
        telebot.types.InlineKeyboardButton("Text", callback_data="fmt_txt"),
    )
    return markup


# ---------------------- SPLIT SIZES (chapters per converted file) ----------------------
# Calibre formats are converted in parts of N chapters: each part is a small, fast
# conversion that is sent as soon as it is done, and one bad part cannot ruin the rest.
# 0 = never split (one file with every chapter). EPUB and text are never split.
# Admins change the sizes with /split; they are stored in bot_settings so every worker agrees.
SPLIT_DEFAULTS = {"pdf": 500}  # every other format defaults to 0 = all chapters in one file
SPLITTABLE_FORMATS = [k for k, (_label, _native) in OUTPUT_FORMATS.items() if not _native]
SPLIT_MIN, SPLIT_MAX = 20, 5000

def get_split_size(fmt):
    """Chapters per file for this format; 0 means do not split."""
    fmt = normalize_output_format(fmt)
    if fmt not in SPLITTABLE_FORMATS:
        return 0
    default = SPLIT_DEFAULTS.get(fmt, 0)
    raw = str(_api_setting_get(f"split_size_{fmt}", "") or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default

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
    on_novel_id=None,
    output_format="epub",
    on_convert_start=None,
    on_epub_ready=None,
    on_part_ready=None,
    on_convert_note=None,
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
            on_novel_id=on_novel_id,
            output_format=output_format,
            on_convert_start=on_convert_start,
            on_epub_ready=on_epub_ready,
            on_part_ready=on_part_ready,
            on_convert_note=on_convert_note,
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

def prompt_manual_range(message, chat_id, user_id, url, version, requester=None):
    version = force_v4(version)
    pending_urls[user_id] = {
        "url": url,
        "title": None,
        "total_chapters": None,
        "version": version,
    }
    start_range_timeout(requester or message.from_user, chat_id, url, version, None, None)
    ask_v4_range(chat_id, user_id)

def request_preview(url, version, user_id, chat_id, username=None, first_name=None, last_name=None):
    version = force_v4(version)
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

def start_range_timeout(user, chat_id, url, version, title, total_chapters, delay=None):
    if delay is None:
        delay = REPLY_PROMPT_TIMEOUT_SECONDS
    version = force_v4(version)
    _clear_timed_reply(chat_id, user.id)
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
        return
    pending_range_state.pop(user_id, None)
    try:
        bot.clear_step_handler_by_chat_id(state["chat_id"])
    except Exception:
        pass
    pending_urls.pop(user_id, None)
    send_temp_message(
        state["chat_id"],
        "This request has ended. Send /download to start again.",
        user_id,
    )

def wait_for_preview(pid, timeout=7200, cancel_event=None):
    deadline = time.time() + timeout
    consecutive_db_failures = 0
    TERMINAL_STATUSES = ('done', 'failed', 'abandoned')

    while time.time() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            # User skipped the wait UI — do not abandon the preview so the
            # scan can still finish and fill novel_title / chapter_count.
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
    """Human-readable range, clamped to actual total when known."""
    cr = (chapter_range or "").strip().lower()
    total = int(total_chapters) if total_chapters else 0
    if not total:
        return chapter_range  # unknown total (skipped/failed scan) — show raw input as-is
    if cr == "all":
        return f"1 to {total}"
    if cr.startswith("first"):
        try:
            n = int(cr.split()[1])
        except (IndexError, ValueError):
            n = total
        return f"1 to {min(n, total)}"
    if cr.startswith("last"):
        try:
            n = int(cr.split()[1])
        except (IndexError, ValueError):
            n = total
        return f"{max(1, total - n + 1)} to {total}"
    # "40 60", "40-60", "40 to 60"
    parts = re.split(r"[\s\-to]+", cr)
    parts = [p for p in parts if p.isdigit()]
    if len(parts) >= 2:
        start = max(1, int(parts[0]))
        end = min(int(parts[1]), total)
        if end < start:
            end = start
        return f"{start} to {end}"
    if len(parts) == 1:
        n = min(int(parts[0]), total)
        return f"1 to {n}"
    return chapter_range

def range_suffix_for_filename(chapter_range, total_chapters):
    """Filename suffix like c1-500, always clamped to real chapter count when known."""
    desc = format_range_desc(chapter_range, total_chapters)
    match = re.match(r"^\s*(\d+)\s+to\s+(\d+)\s*$", desc)
    if match:
        start, end = match.groups()
        return f"c{start}-{end}"
    safe = re.sub(r"[^\w\-]+", "_", (chapter_range or "").strip())
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
                    # If the user skipped the scan UI, a task may already exist
                    # without novel_title — fill it in now so progress / logs show it.
                    cur.execute("""
                        UPDATE tasks
                        SET novel_title = COALESCE(NULLIF(novel_title, ''), %s),
                            total_chapters = COALESCE(total_chapters, %s)
                        WHERE rtrim(url, '/') = rtrim(%s, '/')
                          AND status IN ('pending', 'running')
                          AND (novel_title IS NULL OR novel_title = '' OR total_chapters IS NULL)
                    """, (novel.title, novel.chapter_count, req['url']))
                    conn.commit()
                print(
                    f"[PREVIEW DONE] pid={req['id']} title={novel.title!r} "
                    f"chapters={novel.chapter_count} url={req['url']}"
                )
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

if WORKER_ENABLED:
    threading.Thread(target=process_preview_loop, daemon=True).start()
else:
    print(f"[V3 DISABLED] {SERVER_ID} runs v3 - scan worker not started")
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

# ---------------------- SEARCH QUEUE (search_requests) ----------------------
# s4 searches by itself when it has a free slot. When every slot is busy it
# writes the search to the search_requests table instead; any v4 server
# (including s4 once a slot frees up) claims pending rows, runs the search and
# stores the results as JSON. s4 polls its own row and builds the session.
SEARCH_LIMIT = None           # None = keep every result found (set a number to cap the list)
SEARCH_CONCURRENCY = 10
SEARCH_TIMEOUT = 90           # seconds a user waits for the final list (the search is cut off here unless /testsearches is on)
# searches one server runs at once: 2 on every server (override with the SEARCH_SLOTS env variable)
SEARCH_SLOTS = max(1, int(os.getenv("SEARCH_SLOTS", "2")))
SEARCH_QUEUE_WAIT = SEARCH_TIMEOUT + 240   # seconds the requester waits for a queued search (grows with SEARCH_TIMEOUT)
SEARCH_POLL_INTERVAL = 3.0     # s4 checking its queued row
SEARCH_CLAIM_INTERVAL = 5.0    # idle servers checking the queue
SEARCH_PROGRESS_INTERVAL = 7  # seconds between progress edits sent to the user
SEARCH_PROGRESS_DB_INTERVAL = 4  # seconds between progress writes by a worker
SEARCH_CACHE_SECONDS = 86400   # identical queries inside this window (24 hours) reuse the earlier results; the list then offers a Refresh button
SEARCH_PARTIAL_TIMES = (50, 70)  # seconds into a search when an interim list is shown; the final list arrives at SEARCH_TIMEOUT
RESULTS_PER_PAGE = 10          # novels shown per page in /search results. Change this one number to resize pages.
SEARCH_ETA_TEXT = f"Search will take up to {SEARCH_TIMEOUT} seconds"   # line shown under "Searching for ..." while sources are scanned
SEARCH_STILL_TEXT = f"🔄 Still searching more sources... the final list will be provided within {SEARCH_TIMEOUT} seconds."
SEARCH_CACHED_TEXT = "ℹ️ These are saved results from the same search made {age} ago, so they may be outdated. Tap Refresh search results below to search again for fresh results."
# /testsearches: users still get their lists on the normal schedule, but the search itself keeps running this long
SEARCH_TEST_DEFAULT_SECONDS = 300
SEARCH_TEST_MAX_SECONDS = 600
SEARCH_TEST_AUTO_OFF_SECONDS = 1800   # test mode switches itself off after this long, in case it is left on
SOURCE_DROP_AFTER = 5          # searches in a row a site must fail DNS before it is dropped
SOURCE_RETRY_DAYS = 3          # a dropped site is tried again after this many days
_dropped_cache = {"at": 0.0, "urls": set()}
SEARCH_HISTORY_DAYS = 7
search_lock = threading.Semaphore(SEARCH_SLOTS)


# ---------------------- LIVE /set SETTINGS ----------------------
# Hard-coded values above are the defaults. /set stores an override in
# bot_settings (key "set:<NAME>"). Every worker re-reads those rows every
# RUNTIME_REFRESH_SECONDS and copies them onto these globals, so a change
# survives restarts and reaches servers that were not the one that ran /set.
# /reset deletes the override and the next refresh restores the default.
RUNTIME_REFRESH_SECONDS = 30
RUNTIME_PREFIX = "set:"

# Captured once, before any override is applied.
RUNTIME_DEFAULTS = {
    "SEARCH_TIMEOUT": int(SEARCH_TIMEOUT),
    "SEARCH_CACHE_SECONDS": int(SEARCH_CACHE_SECONDS),
    "SEARCH_PARTIAL_TIMES": f"{int(SEARCH_PARTIAL_TIMES[0])},{int(SEARCH_PARTIAL_TIMES[1])}",
    "SEARCH_SLOTS": int(SEARCH_SLOTS),
    "SEARCH_CONCURRENCY": int(SEARCH_CONCURRENCY),
    "HARD_TASK_LIMIT_HOURS": int(HARD_TASK_LIMIT_HOURS),
    "STUCK_PROGRESS_MINUTES": int(STUCK_PROGRESS_MINUTES),
    "UPLOAD_RETRY_DELAY_MINUTES": int(UPLOAD_RETRY_DELAY_MINUTES),
    "UPLOAD_RETRY_KEEP_HOURS": int(UPLOAD_RETRY_KEEP_HOURS),
    "UPLOAD_RETRY_SECOND_DELAY_MINUTES": int(UPLOAD_RETRY_SECOND_DELAY_MINUTES),
    "PROGRESS_UPDATE_INTERVAL": int(PROGRESS_UPDATE_INTERVAL),
    "MAX_LIVE_PROGRESS_PER_USER": int(MAX_LIVE_PROGRESS_PER_USER),
    "USER_RETRY_WINDOW_HOURS": int(USER_RETRY_WINDOW_HOURS),
    "USER_RETRY_SLOT_LIMIT": int(USER_RETRY_SLOT_LIMIT),
    "RESULTS_PER_PAGE": int(RESULTS_PER_PAGE),
    "SOURCE_DROP_AFTER": int(SOURCE_DROP_AFTER),
    "SOURCE_RETRY_DAYS": int(SOURCE_RETRY_DAYS),
    "REPLY_PROMPT_TIMEOUT_SECONDS": int(REPLY_PROMPT_TIMEOUT_SECONDS),
    "LOCAL_API_FALLBACK_AFTER_MINUTES": int(LOCAL_API_FALLBACK_AFTER_MINUTES),
    "LOCAL_API_MAX_SILENCE_MINUTES": int(LOCAL_API_MAX_SILENCE_MINUTES),
    "TELEGRAM_CLOUD_LOCKOUT_MINUTES": int(TELEGRAM_CLOUD_LOCKOUT_MINUTES),
    "CONNECT_TIMEOUT": int(apihelper.CONNECT_TIMEOUT),
    "READ_TIMEOUT": int(apihelper.READ_TIMEOUT),
    "ADMIN_USER_ID": ",".join(str(int(x)) for x in ADMIN_USER_ID),
    "SPLIT_PDF": int(SPLIT_DEFAULTS.get("pdf", 0)),
}

# type: int | admins | pair. min/max apply to each integer.
RUNTIME_SPECS = {
    "SEARCH_TIMEOUT": {"type": "int", "min": 15, "max": 600},
    "SEARCH_CACHE_SECONDS": {"type": "int", "min": 0, "max": 7 * 24 * 3600},
    "SEARCH_PARTIAL_TIMES": {"type": "pair", "min": 1, "max": 600},
    "SEARCH_SLOTS": {"type": "int", "min": 1, "max": 8},
    "SEARCH_CONCURRENCY": {"type": "int", "min": 1, "max": 40},
    "HARD_TASK_LIMIT_HOURS": {"type": "int", "min": 1, "max": 48},
    "STUCK_PROGRESS_MINUTES": {"type": "int", "min": 2, "max": 180},
    "UPLOAD_RETRY_DELAY_MINUTES": {"type": "int", "min": 1, "max": 180},
    "UPLOAD_RETRY_KEEP_HOURS": {"type": "int", "min": 1, "max": 72},
    "UPLOAD_RETRY_SECOND_DELAY_MINUTES": {"type": "int", "min": 1, "max": 180},
    "PROGRESS_UPDATE_INTERVAL": {"type": "int", "min": 3, "max": 120},
    "MAX_LIVE_PROGRESS_PER_USER": {"type": "int", "min": 1, "max": 10},
    "USER_RETRY_WINDOW_HOURS": {"type": "int", "min": 1, "max": 72},
    "USER_RETRY_SLOT_LIMIT": {"type": "int", "min": 1, "max": 20},
    "RESULTS_PER_PAGE": {"type": "int", "min": 3, "max": 30},
    "SOURCE_DROP_AFTER": {"type": "int", "min": 1, "max": 30},
    "SOURCE_RETRY_DAYS": {"type": "int", "min": 1, "max": 30},
    "REPLY_PROMPT_TIMEOUT_SECONDS": {"type": "int", "min": 60, "max": 3600},
    "LOCAL_API_FALLBACK_AFTER_MINUTES": {"type": "int", "min": 1, "max": 180},
    "LOCAL_API_MAX_SILENCE_MINUTES": {"type": "int", "min": 1, "max": 180},
    "TELEGRAM_CLOUD_LOCKOUT_MINUTES": {"type": "int", "min": 1, "max": 120},
    "CONNECT_TIMEOUT": {"type": "int", "min": 5, "max": 300},
    "READ_TIMEOUT": {"type": "int", "min": 30, "max": 3600},
    "ADMIN_USER_ID": {"type": "admins", "min": 1, "max": 30},
    "SPLIT_PDF": {"type": "int", "min": 0, "max": 5000},
}

RUNTIME_ALIASES = {
    "ADMINS": "ADMIN_USER_ID",
    "APIHELPER.CONNECT_TIMEOUT": "CONNECT_TIMEOUT",
    "APIHELPER.READ_TIMEOUT": "READ_TIMEOUT",
}

pending_runtime_confirm = {}  # user_id -> {token, action, key, stored}


def _canon_runtime_key(raw):
    key = (raw or "").strip().upper().replace(" ", "")
    return RUNTIME_ALIASES.get(key, key)


def _clamp_int(name, number):
    spec = RUNTIME_SPECS[name]
    return max(spec["min"], min(spec["max"], int(number)))


def _parse_runtime_value(name, raw):
    """Return (stored_text, error). stored_text is what goes into bot_settings."""
    spec = RUNTIME_SPECS.get(name)
    if not spec:
        known = ", ".join(sorted(RUNTIME_SPECS))
        return None, f"Unknown setting {name}. Send /set with no arguments to list them.\nKnown: {known}"
    text = (raw or "").strip()
    if spec["type"] == "int":
        try:
            number = int(float(text))
        except ValueError:
            return None, f"{name} must be a whole number between {spec['min']} and {spec['max']}."
        if number < spec["min"] or number > spec["max"]:
            return None, f"{name} must be between {spec['min']} and {spec['max']}."
        return str(number), None
    if spec["type"] == "pair":
        parts = [p for p in re.split(r"[,\s]+", text) if p]
        if len(parts) != 2:
            return None, f"{name} needs two numbers, for example 50,70."
        try:
            a, b = int(parts[0]), int(parts[1])
        except ValueError:
            return None, f"{name} needs two whole numbers."
        if not (spec["min"] <= a <= spec["max"] and spec["min"] <= b <= spec["max"]):
            return None, f"Each number in {name} must be between {spec['min']} and {spec['max']}."
        if a >= b:
            return None, "The first partial time must be smaller than the second."
        return f"{a},{b}", None
    if spec["type"] == "admins":
        ids = []
        for part in re.split(r"[,\s]+", text):
            if not part:
                continue
            if not part.isdigit():
                return None, "Admin IDs must be numbers separated by commas. Example: 111,222,333"
            number = int(part)
            if number not in ids:
                ids.append(number)
        if not (spec["min"] <= len(ids) <= spec["max"]):
            return None, f"Provide between {spec['min']} and {spec['max']} admin IDs."
        return ",".join(str(i) for i in ids), None
    return None, "Unsupported setting type."


def _load_runtime_overrides():
    """{NAME: stored_text} for rows this process should apply. None if the DB is down."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT setting_key, setting_value FROM bot_settings WHERE setting_key LIKE %s",
                (RUNTIME_PREFIX + "%",),
            )
            rows = {}
            for row in cur.fetchall():
                key = row["setting_key"][len(RUNTIME_PREFIX):]
                if key in RUNTIME_SPECS and row["setting_value"] is not None:
                    rows[key] = str(row["setting_value"])
            return rows
    except Exception as error:
        print(f"[RUNTIME SETTINGS] read: {error}")
        return None
    finally:
        conn.close()


def _format_runtime_current(name):
    if name == "ADMIN_USER_ID":
        return ",".join(str(int(x)) for x in ADMIN_USER_ID)
    if name == "SEARCH_PARTIAL_TIMES":
        return f"{int(SEARCH_PARTIAL_TIMES[0])},{int(SEARCH_PARTIAL_TIMES[1])}"
    if name == "CONNECT_TIMEOUT":
        return str(int(apihelper.CONNECT_TIMEOUT))
    if name == "READ_TIMEOUT":
        return str(int(apihelper.READ_TIMEOUT))
    if name == "SPLIT_PDF":
        raw = str(_api_setting_get("split_size_pdf", "") or "").strip()
        if raw:
            try:
                return str(max(0, int(raw)))
            except ValueError:
                pass
        return str(int(RUNTIME_DEFAULTS["SPLIT_PDF"]))
    return str(int(globals()[name]))


def apply_runtime_settings():
    """Copy bot_settings overrides onto the live globals. Missing rows restore defaults."""
    global SEARCH_TIMEOUT, SEARCH_CACHE_SECONDS, SEARCH_PARTIAL_TIMES
    global SEARCH_SLOTS, SEARCH_CONCURRENCY, search_lock
    global HARD_TASK_LIMIT_HOURS, STUCK_PROGRESS_MINUTES
    global UPLOAD_RETRY_DELAY_MINUTES, UPLOAD_RETRY_KEEP_HOURS, UPLOAD_RETRY_SECOND_DELAY_MINUTES
    global PROGRESS_UPDATE_INTERVAL, MAX_LIVE_PROGRESS_PER_USER
    global USER_RETRY_WINDOW_HOURS, USER_RETRY_SLOT_LIMIT, RESULTS_PER_PAGE
    global SOURCE_DROP_AFTER, SOURCE_RETRY_DAYS, REPLY_PROMPT_TIMEOUT_SECONDS
    global LOCAL_API_FALLBACK_AFTER_MINUTES, LOCAL_API_MAX_SILENCE_MINUTES
    global TELEGRAM_CLOUD_LOCKOUT_MINUTES, ADMIN_USER_ID
    global SEARCH_QUEUE_WAIT, SEARCH_ETA_TEXT, SEARCH_STILL_TEXT

    rows = _load_runtime_overrides()
    if rows is None:
        return False

    def _int_setting(name):
        raw = rows.get(name)
        if raw is None:
            return int(RUNTIME_DEFAULTS[name])
        try:
            return _clamp_int(name, int(float(raw)))
        except ValueError:
            return int(RUNTIME_DEFAULTS[name])

    SEARCH_TIMEOUT = _int_setting("SEARCH_TIMEOUT")
    SEARCH_CACHE_SECONDS = _int_setting("SEARCH_CACHE_SECONDS")
    SEARCH_CONCURRENCY = _int_setting("SEARCH_CONCURRENCY")
    HARD_TASK_LIMIT_HOURS = _int_setting("HARD_TASK_LIMIT_HOURS")
    STUCK_PROGRESS_MINUTES = _int_setting("STUCK_PROGRESS_MINUTES")
    UPLOAD_RETRY_DELAY_MINUTES = _int_setting("UPLOAD_RETRY_DELAY_MINUTES")
    UPLOAD_RETRY_KEEP_HOURS = _int_setting("UPLOAD_RETRY_KEEP_HOURS")
    UPLOAD_RETRY_SECOND_DELAY_MINUTES = _int_setting("UPLOAD_RETRY_SECOND_DELAY_MINUTES")
    PROGRESS_UPDATE_INTERVAL = _int_setting("PROGRESS_UPDATE_INTERVAL")
    MAX_LIVE_PROGRESS_PER_USER = _int_setting("MAX_LIVE_PROGRESS_PER_USER")
    USER_RETRY_WINDOW_HOURS = _int_setting("USER_RETRY_WINDOW_HOURS")
    USER_RETRY_SLOT_LIMIT = _int_setting("USER_RETRY_SLOT_LIMIT")
    RESULTS_PER_PAGE = _int_setting("RESULTS_PER_PAGE")
    SOURCE_DROP_AFTER = _int_setting("SOURCE_DROP_AFTER")
    SOURCE_RETRY_DAYS = _int_setting("SOURCE_RETRY_DAYS")
    REPLY_PROMPT_TIMEOUT_SECONDS = _int_setting("REPLY_PROMPT_TIMEOUT_SECONDS")
    LOCAL_API_FALLBACK_AFTER_MINUTES = _int_setting("LOCAL_API_FALLBACK_AFTER_MINUTES")
    LOCAL_API_MAX_SILENCE_MINUTES = _int_setting("LOCAL_API_MAX_SILENCE_MINUTES")
    TELEGRAM_CLOUD_LOCKOUT_MINUTES = _int_setting("TELEGRAM_CLOUD_LOCKOUT_MINUTES")

    partial_raw = rows.get("SEARCH_PARTIAL_TIMES", RUNTIME_DEFAULTS["SEARCH_PARTIAL_TIMES"])
    try:
        left, right = [int(float(x)) for x in str(partial_raw).split(",")[:2]]
        spec = RUNTIME_SPECS["SEARCH_PARTIAL_TIMES"]
        left = max(spec["min"], min(spec["max"], left))
        right = max(spec["min"], min(spec["max"], right))
        if left >= right:
            raise ValueError
        SEARCH_PARTIAL_TIMES = (left, right)
    except (ValueError, TypeError):
        a, b = RUNTIME_DEFAULTS["SEARCH_PARTIAL_TIMES"].split(",")
        SEARCH_PARTIAL_TIMES = (int(a), int(b))

    admin_raw = rows.get("ADMIN_USER_ID", RUNTIME_DEFAULTS["ADMIN_USER_ID"])
    admin_ids = []
    for part in str(admin_raw).split(","):
        part = part.strip()
        if part.isdigit():
            number = int(part)
            if number not in admin_ids:
                admin_ids.append(number)
    if admin_ids:
        ADMIN_USER_ID = admin_ids
    else:
        ADMIN_USER_ID = [int(x) for x in RUNTIME_DEFAULTS["ADMIN_USER_ID"].split(",") if x]

    apihelper.CONNECT_TIMEOUT = _int_setting("CONNECT_TIMEOUT")
    apihelper.READ_TIMEOUT = _int_setting("READ_TIMEOUT")

    slots = _int_setting("SEARCH_SLOTS")
    if slots != SEARCH_SLOTS:
        # New acquires use the new size. A search already holding the old
        # semaphore finishes on it, so the cap can be briefly soft.
        SEARCH_SLOTS = slots
        search_lock = threading.Semaphore(slots)

    SEARCH_QUEUE_WAIT = SEARCH_TIMEOUT + 240
    SEARCH_ETA_TEXT = f"Search will take up to {SEARCH_TIMEOUT} seconds"
    SEARCH_STILL_TEXT = (
        f"🔄 Still searching more sources... the final list will be provided within {SEARCH_TIMEOUT} seconds."
    )
    return True


def runtime_settings_loop():
    while True:
        try:
            apply_runtime_settings()
        except Exception as error:
            print(f"[RUNTIME SETTINGS] {error}")
        time.sleep(RUNTIME_REFRESH_SECONDS)


threading.Thread(target=runtime_settings_loop, daemon=True, name="runtime-settings").start()


def _save_runtime_override(name, stored):
    ok = _api_setting_set(RUNTIME_PREFIX + name, stored)
    if ok and name == "SPLIT_PDF":
        # /split and the converter already read split_size_pdf on every call.
        ok = _api_setting_set("split_size_pdf", stored) and ok
    return ok


def _delete_runtime_override(name):
    conn = get_db_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM bot_settings WHERE setting_key = %s",
                (RUNTIME_PREFIX + name,),
            )
            if name == "SPLIT_PDF":
                cur.execute(
                    "DELETE FROM bot_settings WHERE setting_key = %s",
                    ("split_size_pdf",),
                )
        conn.commit()
        return True
    except Exception as error:
        conn.rollback()
        print(f"[RUNTIME SETTINGS] delete {name}: {error}")
        return False
    finally:
        conn.close()


def _delete_all_runtime_overrides():
    conn = get_db_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM bot_settings WHERE setting_key LIKE %s",
                (RUNTIME_PREFIX + "%",),
            )
            cur.execute("DELETE FROM bot_settings WHERE setting_key = %s", ("split_size_pdf",))
        conn.commit()
        return True
    except Exception as error:
        conn.rollback()
        print(f"[RUNTIME SETTINGS] reset all: {error}")
        return False
    finally:
        conn.close()


def _runtime_settings_listing():
    lines = ["Live settings. /set NAME VALUE changes one. /reset NAME restores its default.", ""]
    overrides = _load_runtime_overrides() or {}
    for name in RUNTIME_SPECS:
        current = _format_runtime_current(name)
        default = str(RUNTIME_DEFAULTS[name])
        mark = " (changed)" if name in overrides else ""
        lines.append(f"{name} = {current}    default {default}{mark}")
    lines.append("")
    lines.append("Aliases: ADMINS = ADMIN_USER_ID, apihelper.CONNECT_TIMEOUT = CONNECT_TIMEOUT, apihelper.READ_TIMEOUT = READ_TIMEOUT.")
    lines.append("Every worker re-reads these from the database every 30 seconds.")
    return "\n".join(lines)


SEARCH_DNS_MARKERS = ("DNSError", "Cloudflare 1000", "NameResolutionError", "Name or service not known", "getaddrinfo")


def _classify_search_error(error):
    """'dns' = the domain no longer resolves, 'aborted' = we cancelled it, 'other' = the site answered somehow."""
    if "Abort" in type(error).__name__:
        return "aborted"
    text = f"{type(error).__name__}: {error} {error!r}"
    return "dns" if any(m in text for m in SEARCH_DNS_MARKERS) else "other"


class SearchCancelled(Exception):
    """The user pressed Cancel (or started another search) while this one was running."""


def _norm_search_text(text):
    """Lowercase, drop punctuation, collapse spaces: "Mad-God's Return!" -> "mad god s return"."""
    return re.sub(r"[\W_]+", " ", str(text or "").lower()).strip()


def _tier_of(t, q, q_words):
    """How closely a normalised title t matches a normalised query q: 0 exact, 1 starts with, 2 contains the
    phrase, 3 has every word, 4 anything else."""
    if not q:
        return 4
    if t == q:
        return 0
    if t.startswith(q + " "):
        return 1
    if f" {q} " in f" {t} ":
        return 2
    if q_words <= set(t.split()):
        return 3
    return 4


def _title_tier(title, query):
    q = _norm_search_text(query)
    return _tier_of(_norm_search_text(title), q, set(q.split()))


def _rank_search_results(results, query):
    """Order results by how well the TITLE matches the query, best first.

    Tier 0: title is exactly the query            (Mad God)
    Tier 1: title starts with the query           (Mad God Reborn)
    Tier 2: title contains the query as a phrase  (The Legend of Mad God)
    Tier 3: title has every query word, any order (God of the Mad)
    Tier 4: everything else, closest spelling first (Made by God ...)
    Inside a tier: closer spelling first, then more sources.
    Works on any objects with .title and .novels, so it is safe to run on cached or queued results too."""
    from difflib import SequenceMatcher
    q = _norm_search_text(query)
    if not q:
        return list(results)
    q_words = set(q.split())

    def _key(result):
        t = _norm_search_text(result.title)
        tier = _tier_of(t, q, q_words)
        ratio = SequenceMatcher(a=t, b=q).ratio()
        return (tier, -ratio, -len(result.novels))

    return sorted(results, key=_key)


def _combine_search_records(records, query, limit):
    from slugify import slugify
    from lncrawl.core import CombinedSearchResult

    combined = {}
    for item in records:
        if not (item and isinstance(item.title, str)):
            continue
        item.title = str(item.title).strip()
        item.info = str(item.info).strip()
        key = slugify(item.title)
        if len(key) <= 2:
            continue
        combined.setdefault(key, []).append(item)
    results = []
    for key, value in combined.items():
        value = sorted(value, key=lambda x: x.url)
        results.append(CombinedSearchResult(id=key, novels=value, title=value[0].title))
    results = _rank_search_results(results, query)
    return results[:limit]


def _dropped_search_sources():
    """URLs of sources that failed DNS SOURCE_DROP_AFTER searches in a row (cached 5 min)."""
    if time.time() - _dropped_cache["at"] < 300:
        return _dropped_cache["urls"]
    conn = get_db_connection()
    if not conn:
        return _dropped_cache["urls"]
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT url FROM search_source_health
                WHERE dropped AND dropped_at > NOW() - make_interval(days => %s)
            """, (SOURCE_RETRY_DAYS,))
            _dropped_cache["urls"] = {r["url"] for r in cur.fetchall()}
            _dropped_cache["at"] = time.time()
    except Exception as e:
        print(f"[SEARCH SOURCES] could not load dropped sources: {e}")
    finally:
        conn.close()
    return _dropped_cache["urls"]


def _record_source_health(outcomes):
    """Count DNS failures per source across searches; drop sources that keep failing."""
    alive = [u for u, o in outcomes.items() if o in ("ok", "other")]
    dead = [u for u, o in outcomes.items() if o == "dns"]
    # If nothing answered, or most sources 'failed DNS', it is our own network: record nothing.
    if not alive or len(dead) > 0.75 * max(1, len(outcomes)):
        return
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            if dead:
                cur.execute("""
                    INSERT INTO search_source_health (url, dns_failures, dropped, dropped_at, last_error, updated_at)
                    SELECT u, 1, (1 >= %s), CASE WHEN 1 >= %s THEN NOW() END, 'DNS failure', NOW()
                    FROM unnest(%s::text[]) AS u
                    ON CONFLICT (url) DO UPDATE SET
                        dns_failures = search_source_health.dns_failures + 1,
                        dropped = search_source_health.dns_failures + 1 >= %s,
                        dropped_at = CASE WHEN search_source_health.dns_failures + 1 >= %s
                                          THEN NOW() ELSE search_source_health.dropped_at END,
                        last_error = 'DNS failure',
                        updated_at = NOW()
                """, (SOURCE_DROP_AFTER, SOURCE_DROP_AFTER, dead, SOURCE_DROP_AFTER, SOURCE_DROP_AFTER))
            if alive:
                cur.execute("""
                    UPDATE search_source_health
                    SET dns_failures = 0, dropped = FALSE, dropped_at = NULL, updated_at = NOW()
                    WHERE url = ANY(%s) AND (dns_failures > 0 OR dropped)
                """, (alive,))
            conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[SEARCH SOURCES] health update failed: {e}")
    finally:
        conn.close()


# Lower number = gets a search slot first.
# 0 answered with results (fastest first), 1 never measured, 2 answered with nothing,
# 3 error, 4 started but never replied, 5 never got a slot on the last full probe.
_priority_cache = {"at": 0.0, "rows": {}}
SEARCH_PROBE_QUERY = "the"
SEARCH_PROBE_INTERVAL = 24 * 3600


def _load_source_priority():
    """url -> (tier, reply_seconds). Cached for a minute. Missing url means tier 1."""
    if time.time() - _priority_cache["at"] < 60:
        return _priority_cache["rows"]
    conn = get_db_connection()
    if not conn:
        return _priority_cache["rows"]
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT url, priority_tier, reply_seconds
                FROM search_source_health
                WHERE priority_tier IS NOT NULL
            """)
            _priority_cache["rows"] = {
                row["url"]: (int(row["priority_tier"]), float(row["reply_seconds"] or 0))
                for row in cur.fetchall()
            }
            _priority_cache["at"] = time.time()
    except Exception as error:
        print(f"[SEARCH PRIORITY] load failed: {error}")
    finally:
        conn.close()
    return _priority_cache["rows"]


def _sort_sources_by_priority(sources):
    """Proven fast hits first. lncrawl's own order is kept inside one tier."""
    stats = _load_source_priority()
    sources.sort(key=lambda source: stats.get(source.url, (1, 0.0)))
    return sources


def _update_source_priority(timings, hanging=None, never=None, mark_unreached=False):
    """Remember how each source behaved. A short search must not punish sources that never started."""
    rows = []
    for url, timing in (timings or {}).items():
        duration = max(0.0, float(timing.get("end") or 0) - float(timing.get("start") or 0))
        count = int(timing.get("count") or 0)
        if timing.get("status") == "ok" and count > 0:
            tier = 0
        elif timing.get("status") == "ok":
            tier = 2
        else:
            tier = 3
        rows.append((url, tier, duration, count))
    if mark_unreached:
        for url in hanging or []:
            rows.append((url, 4, 9999.0, 0))
        for url in never or []:
            rows.append((url, 5, 9999.0, 0))
    if not rows:
        return
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.executemany("""
                INSERT INTO search_source_health
                    (url, priority_tier, reply_seconds, result_count, last_probe_at, updated_at)
                VALUES (%s, %s, %s, %s, NOW(), NOW())
                ON CONFLICT (url) DO UPDATE SET
                    priority_tier = EXCLUDED.priority_tier,
                    reply_seconds = EXCLUDED.reply_seconds,
                    result_count = EXCLUDED.result_count,
                    last_probe_at = NOW(),
                    updated_at = NOW()
            """, rows)
        conn.commit()
        _priority_cache["at"] = 0.0
    except Exception as error:
        conn.rollback()
        print(f"[SEARCH PRIORITY] update failed: {error}")
    finally:
        conn.close()


def _perform_search_with_progress(query, sources, limit, concurrency, timeout, on_progress=None, on_partial=None,
                                  cancel_event=None, user_deadline=None, on_final=None, report_out=None, update_priority=False):
    """lncrawl's perform_search plus: on_progress(done, total) every ~2s, one early
    on_partial(results) at each SEARCH_PARTIAL_TIMES mark (50s, 70s), and per-source health tracking.
    cancel_event: set it to abort. user_deadline + on_final(results, total): test mode, where `timeout` is longer than
    what users wait. report_out: a dict that receives the per-source timing report."""
    from concurrent.futures import TimeoutError as FutureTimeout
    from threading import Event
    from lncrawl.core import TaskManager
    from lncrawl.commands.search.helper import search_job

    outcomes = {}
    timings = {}     # per source: when it started, when it answered, what it returned (used by /testsearches)
    starts = {}
    t0 = time.time()

    def _job(source, q, sig, tmo):
        began = time.time() - t0
        starts[source.url] = began
        try:
            result = search_job(source, q, sig, tmo)
        except Exception as e:
            outcomes[source.url] = _classify_search_error(e)
            timings[source.url] = {
                "start": began, "end": time.time() - t0, "status": outcomes[source.url],
                "error": f"{type(e).__name__}: {str(e)[:120]}", "count": 0, "items": [],
            }
            raise
        outcomes[source.url] = "ok"
        found = list(result or [])
        timings[source.url] = {
            "start": began, "end": time.time() - t0, "status": "ok", "count": len(found),
            "items": [(str(getattr(r, "title", "")), str(getattr(r, "url", ""))) for r in found[:8]],
        }
        return result

    signal = Event()
    taskman = TaskManager(concurrency, signal=signal)
    futures = [taskman.submit_task(_job, source, query, signal, timeout) for source in sources]
    total = len(futures)
    stop = Event()
    records = []
    partial_sent = set()
    final_sent = [False]

    def _monitor():
        last_progress = 0.0
        while not stop.wait(1.0):
            try:
                now = time.time()
                elapsed = now - t0
                if on_progress and now - last_progress >= 2.0:
                    last_progress = now
                    on_progress(sum(1 for f in futures if f.done()), total)
                if on_partial and records:
                    due = [m for m in SEARCH_PARTIAL_TIMES if elapsed >= m and m not in partial_sent]
                    if due:
                        partial_sent.update(due)
                        on_partial(_combine_search_records(list(records), query, limit))
                if on_final and user_deadline and not final_sent[0] and elapsed >= user_deadline:
                    # Test mode: the user's list is final now; the search keeps running to collect late answers.
                    final_sent[0] = True
                    on_final(_combine_search_records(list(records), query, limit), total)
            except Exception:
                pass

    def _cancel_watch():
        while not stop.is_set():
            if cancel_event.wait(0.5):
                signal.set()
                taskman.cancel_futures(futures)   # lets the result loop below return at once
                return

    if on_progress or on_partial or on_final:
        if on_progress:
            try:
                on_progress(0, total)
            except Exception:
                pass
        threading.Thread(target=_monitor, daemon=True).start()
    if cancel_event is not None:
        threading.Thread(target=_cancel_watch, daemon=True).start()

    try:
        for result in taskman.resolve(
            futures, unit="source", desc="Searching", signal=signal, timeout=timeout,
        ):
            records += result or []
    except FutureTimeout:
        print(f"[SEARCH] some sources did not answer within {timeout:.0f}s")
    except Exception as e:
        print(f"[SEARCH] perform failed: {type(e).__name__}: {e}")
    finally:
        signal.set()
        taskman.shutdown()
        stop.set()

    if cancel_event is not None and cancel_event.is_set():
        raise SearchCancelled()
    if report_out is not None:
        report_out.update(_build_search_report(
            query, [src.url for src in sources], timings, starts, time.time() - t0, timeout, user_deadline,
        ))
    threading.Thread(target=_record_source_health, args=(dict(outcomes),), daemon=True).start()
    if update_priority:
        # Only a probe changes priorities. A normal or /testsearches search is about one query,
        # so "no results" there says nothing about whether a source is healthy.
        _update_source_priority(
            timings,
            hanging=[url for url in starts if url not in timings],
            never=[src.url for src in sources if src.url not in starts],
            mark_unreached=True,
        )
    return _combine_search_records(records, query, limit)


def run_lncrawl_search(query, on_progress=None, on_partial=None, cancel_event=None, on_final=None,
                       test_timeout=None, report_out=None, update_priority=False):
    """Run the same source search used by the lncrawl CLI."""
    ctx, _ = _v4()
    ctx.setup()
    ctx.sources.ensure_load()
    # The index's can_search flag is stale for many sources (libread, novelhi,
    # royalroad, ...), so also trust the crawler class's own can_search.
    sources = [
        s for s in ctx.sources.list(None, include_rejected=False)
        if s.can_search
        or getattr(ctx.sources.crawlers.get(s.crawler_id), "can_search", False)
    ]
    dropped = _dropped_search_sources()
    if dropped:
        kept = [s for s in sources if s.url not in dropped]
        if kept:
            sources = kept
    if not sources:
        raise Exception("No searchable sources available")
    _sort_sources_by_priority(sources)
    extended = test_timeout if (test_timeout and test_timeout > SEARCH_TIMEOUT) else None
    return _perform_search_with_progress(
        query, sources, SEARCH_LIMIT, SEARCH_CONCURRENCY, extended or SEARCH_TIMEOUT, on_progress, on_partial,
        cancel_event=cancel_event,
        user_deadline=SEARCH_TIMEOUT if extended else None,
        on_final=on_final if extended else None,
        report_out=report_out,
        update_priority=update_priority,
    ), len(sources)


def _serialize_search_results(results):
    return [
        {
            "id": str(getattr(r, "id", "") or ""),
            "title": r.title,
            "novels": [
                {"title": n.title, "url": n.url, "info": n.info or ""}
                for n in r.novels
            ],
        }
        for r in results
    ]


def _deserialize_search_results(data):
    from types import SimpleNamespace
    if isinstance(data, str):
        data = json.loads(data)
    return [
        SimpleNamespace(
            id=r.get("id", ""),
            title=r["title"],
            novels=[SimpleNamespace(**n) for n in r.get("novels", [])],
        )
        for r in (data or [])
    ]


def _search_log_insert(chat_id, user_id, user, query, mode, queued):
    """Insert a search row. queued=False -> already running here; True -> pending."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO search_requests
                    (user_id, chat_id, username, first_name, last_name, query, mode,
                     status, queued, origin_server, origin_run_id,
                     server_id, run_id, started_at, attempts)
                VALUES (%s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s, %s, CASE WHEN %s THEN NULL ELSE NOW() END, %s)
                RETURNING id
            """, (
                user_id, chat_id,
                getattr(user, "username", None),
                getattr(user, "first_name", None),
                getattr(user, "last_name", None),
                query, mode,
                "pending" if queued else "processing", queued, SERVER_ID, RUN_ID,
                None if queued else SERVER_ID, None if queued else RUN_ID,
                queued, 0 if queued else 1,
            ))
            row = cur.fetchone()
            conn.commit()
            return row["id"]
    except Exception as e:
        conn.rollback()
        print(f"[SEARCH DB] insert failed: {e}")
        return None
    finally:
        conn.close()


def _search_log_finish(row_id, results=None, source_count=None, status="done", error=None):
    if row_id is None:
        return
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE search_requests
                SET status = %s, results = %s, source_count = %s, result_count = %s,
                    error = %s, completed_at = NOW(), partial_results = NULL
                WHERE id = %s AND status = 'processing'
            """, (
                status,
                Json(results) if results is not None else None,
                source_count,
                len(results) if results is not None else None,
                (error or None) and str(error)[:500],
                row_id,
            ))
            conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[SEARCH DB] finish failed for {row_id}: {e}")
    finally:
        conn.close()


def _search_get(row_id, want_partial=False):
    conn = get_db_connection()
    if not conn:
        return None
    partial_sql = "p.partial_results" if want_partial else "NULL::jsonb"
    try:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT p.status, p.results, p.source_count, p.error,
                       p.progress_done, p.progress_total,
                       {partial_sql} AS partial_results,
                       (SELECT COUNT(*) FROM search_requests q
                        WHERE q.status = 'pending' AND q.id < p.id) AS ahead
                FROM search_requests p
                WHERE p.id = %s
            """, (row_id,))
            return cur.fetchone()
    except Exception as e:
        print(f"[SEARCH DB] read failed for {row_id}: {e}")
        return None
    finally:
        conn.close()


def _unpack_partial(data):
    """partial_results is stored as {"seq": n, "results": [...], "final": bool}; returns (seq, results or None, is_final)."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:
            return 0, None, False
    if isinstance(data, dict):
        return int(data.get("seq") or 0), data.get("results"), bool(data.get("final"))
    if isinstance(data, list):
        return 1, data, False
    return 0, None, False


def _search_partial_update(row_id, partial):
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE search_requests SET partial_results = %s
                WHERE id = %s AND status = 'processing'
            """, (Json(partial), row_id))
            conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[SEARCH DB] partial update failed for {row_id}: {e}")
    finally:
        conn.close()


def _search_cache_lookup(query):
    """A finished, non-empty, real (not itself cached) search for the same query inside SEARCH_CACHE_SECONDS."""
    conn = get_db_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT results, source_count,
                       EXTRACT(EPOCH FROM (NOW() - completed_at)) AS age_seconds
                FROM search_requests
                WHERE status = 'done' AND cached IS NOT TRUE
                  AND result_count > 0 AND results IS NOT NULL
                  AND LOWER(BTRIM(query)) = LOWER(BTRIM(%s))
                  AND completed_at > NOW() - make_interval(secs => %s)
                ORDER BY id DESC
                LIMIT 1
            """, (query, SEARCH_CACHE_SECONDS))
            return cur.fetchone()
    except Exception as e:
        print(f"[SEARCH DB] cache lookup failed: {e}")
        return None
    finally:
        conn.close()


def _search_log_cached(chat_id, user_id, user, query, mode, results_json, source_count):
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO search_requests
                    (user_id, chat_id, username, first_name, last_name, query, mode,
                     status, queued, origin_server, origin_run_id, server_id, run_id,
                     started_at, completed_at, attempts, source_count, result_count,
                     results, cached)
                VALUES (%s, %s, %s, %s, %s, %s, %s,
                        'done', FALSE, %s, %s, %s, %s,
                        NOW(), NOW(), 0, %s, %s,
                        %s, TRUE)
            """, (
                user_id, chat_id,
                getattr(user, "username", None),
                getattr(user, "first_name", None),
                getattr(user, "last_name", None),
                query, mode,
                SERVER_ID, RUN_ID, SERVER_ID, RUN_ID,
                source_count, len(results_json or []),
                Json(results_json),
            ))
            conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[SEARCH DB] cache log failed: {e}")
    finally:
        conn.close()


def _search_progress_update(row_id, done, total):
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE search_requests SET progress_done = %s, progress_total = %s
                WHERE id = %s AND status = 'processing'
            """, (done, total, row_id))
            conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[SEARCH DB] progress update failed for {row_id}: {e}")
    finally:
        conn.close()


def _search_abandon(row_id):
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE search_requests
                SET status = 'abandoned', error = 'timed out waiting for a server',
                    completed_at = NOW()
                WHERE id = %s AND status IN ('pending', 'processing')
            """, (row_id,))
            conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[SEARCH DB] abandon failed for {row_id}: {e}")
    finally:
        conn.close()


def _search_cancel(row_id):
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE search_requests
                SET status = 'cancelled', error = 'cancelled by the user', completed_at = NOW()
                WHERE id = %s AND status IN ('pending', 'processing')
            """, (row_id,))
            conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[SEARCH DB] cancel failed for {row_id}: {e}")
    finally:
        conn.close()


def _search_is_cancelled(row_id):
    conn = get_db_connection()
    if not conn:
        return False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM search_requests WHERE id = %s", (row_id,))
            row = cur.fetchone()
            return bool(row and row["status"] == "cancelled")
    except Exception:
        return False
    finally:
        conn.close()


def _format_age(seconds):
    s = max(0, int(float(seconds or 0)))
    if s < 60:
        return "less than a minute"
    if s < 3600:
        n, unit = s // 60, "minute"
    elif s < 86400:
        n, unit = s // 3600, "hour"
    else:
        n, unit = s // 86400, "day"
    return f"{n} {unit}{'' if n == 1 else 's'}"


# ---- /testsearches -------------------------------------------------------------------------------------------
_TIER_LABELS = ("exact", "starts with", "contains", "all words", "loose")


def _search_test_timeout():
    """Seconds a search may run while /testsearches is on, or None (off, expired or invalid)."""
    raw = str(_api_setting_get("search_test", "") or "")
    try:
        seconds, until = raw.split("|")
        seconds, until = float(seconds), float(until)
    except ValueError:
        return None
    if until <= time.time() or seconds <= SEARCH_TIMEOUT:
        return None
    return min(seconds, SEARCH_TEST_MAX_SECONDS)


def _search_test_set(seconds):
    """seconds=None switches test mode off. Shared by every server through bot_settings."""
    if not seconds:
        return _api_setting_set("search_test", "")
    return _api_setting_set("search_test", f"{int(seconds)}|{time.time() + SEARCH_TEST_AUTO_OFF_SECONDS}")


def _build_search_report(query, source_urls, timings, starts, elapsed, timeout, user_deadline):
    rows, hanging, never = [], [], []
    for url in source_urls:
        t = timings.get(url)
        if t is None:
            if url in starts:
                hanging.append({"url": url, "start": round(starts[url], 1)})
            else:
                never.append({"url": url})
            continue
        rows.append({
            "url": url,
            "start": round(t["start"], 1),
            "end": round(t["end"], 1),
            "status": t["status"],
            "error": t.get("error", ""),
            "count": t.get("count", 0),
            "items": [{"title": title, "url": link, "tier": _title_tier(title, query)} for title, link in t["items"]],
        })
    return {
        "query": query,
        "timeout": int(timeout),
        "deadline": int(user_deadline or timeout),
        "elapsed": round(elapsed, 1),
        "total": len(source_urls),
        "sources": rows,
        "hanging": hanging,
        "never_started": never,
    }


def _save_search_test_report(search_id, query, report):
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO search_test_reports (search_id, query, timeout_seconds, data)
                VALUES (%s, %s, %s, %s)
            """, (search_id, query, report.get("timeout"), Json(report)))
            conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[SEARCH DB] test report save failed for {search_id}: {e}")
    finally:
        conn.close()


def _load_search_test_reports(limit=10, report_id=None):
    conn = get_db_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            if report_id is None:
                cur.execute("""
                    SELECT id, search_id, query, created_at, data FROM search_test_reports
                    ORDER BY id DESC LIMIT %s
                """, (limit,))
            else:
                cur.execute("""
                    SELECT id, search_id, query, created_at, data FROM search_test_reports WHERE id = %s
                """, (report_id,))
            return cur.fetchall()
    except Exception as e:
        print(f"[SEARCH DB] test report load failed: {e}")
        return []
    finally:
        conn.close()


def _report_stats(data):
    deadline = float(data.get("deadline") or SEARCH_TIMEOUT)
    ok = [r for r in data.get("sources", []) if r["status"] == "ok"]
    late = [r for r in ok if r["end"] > deadline]
    slowest = max(ok, key=lambda r: r["end"], default=None)
    return ok, late, slowest, deadline


def _format_test_report(report_id, search_id, data):
    """Full text of one test search (the caller splits it into Telegram-sized messages)."""
    ok, late, slowest, deadline = _report_stats(data)
    rows = data.get("sources", [])
    bad = [r for r in rows if r["status"] != "ok"]
    hanging = data.get("hanging", [])
    never = data.get("never_started", [])
    query = data.get("query", "")
    timeout = data.get("timeout")
    elapsed = data.get("elapsed")
    seen = {_norm_search_text(i["title"]) for r in ok if r["end"] <= deadline for i in r["items"]}
    late = sorted(late, key=lambda r: r["end"])

    lines = [
        f"🧪 Test search #{report_id} (search log id {search_id})",
        f"Query: {query}",
        f"Users saw their final list at {deadline:.0f}s. This run waited up to {timeout}s and ended at {elapsed}s.",
        "",
        f"Sources: {data.get('total')} | answered {len(ok)} | errors {len(bad)} | still waiting at the end {len(hanging)} | never started {len(never)}",
        f"Answered after {deadline:.0f}s: {len(late)}",
    ]
    if slowest:
        lines.append(f"Slowest answer: {slowest['end']}s - {slowest['url']}")

    if late:
        lines.append("")
        lines.append(f"⏱ Answers that came after {deadline:.0f}s (users never saw these):")
        for r in late:
            lines.append(f"\n{r['end']}s  {r['url']}  (started at {r['start']}s, {r['count']} result(s))")
            for it in r["items"][:5]:
                fresh = "new title" if _norm_search_text(it["title"]) not in seen else "extra source for a title users already had"
                lines.append(f"  - {it['title']} - {it['url']}  [{_TIER_LABELS[it['tier']]}; {fresh}]")
        close = [r for r in late if any(i["tier"] <= 2 for i in r["items"])]
        new_titles = {_norm_search_text(i["title"]) for r in late for i in r["items"] if i["tier"] <= 2} - seen
        lines.append("")
        lines.append(
            f"📊 Late sources with a close title match (exact, starts with or contains): {len(close)} of {len(late)}. "
            f"Close-match titles that only late sources found: {len(new_titles)}."
        )
    else:
        lines.append("")
        lines.append(f"Nothing answered after {deadline:.0f}s, so the extra wait added no results this time.")
    if slowest:
        lines.append(f"⏳ A test timeout of about {int(slowest['end']) + 15}s would have been enough for this run.")

    lines.append("")
    lines.append("🐢 Slowest 10 answers:")
    for r in sorted(ok, key=lambda r: r["end"], reverse=True)[:10]:
        best = min((i["tier"] for i in r["items"]), default=None)
        best_text = _TIER_LABELS[best] if best is not None else "no results"
        lines.append(f"{r['end']}s  {r['url']}  {r['count']} result(s), best match: {best_text}")

    if never:
        lines.append("")
        lines.append(
            f"⏸ Never started ({len(never)}): they were still queued behind slow sources. "
            "If this number is large, SEARCH_CONCURRENCY is the bottleneck, not the timeout."
        )
        lines.extend(f"  {r['url']}" for r in never[:12])
    if hanging:
        lines.append("")
        lines.append(f"⌛ Started but never answered ({len(hanging)}):")
        lines.extend(f"  {r['url']} (started at {r['start']}s)" for r in hanging[:12])
    if bad:
        lines.append("")
        lines.append(f"⚠️ Errors ({len(bad)}):")
        lines.extend(f"  {r['url']} - {r['status']}: {r['error'][:100]}" for r in bad[:12])
    return "\n".join(lines)


def run_search_request(chat_id, user_id, user, query, mode="search", progress_cb=None,
                       cancel_event=None, fresh=False, info=None):
    """Used by s4's /search and /download. Returns (results, source_count).

    Same query within SEARCH_CACHE_SECONDS -> reuse the earlier results (unless fresh=True), and info["cached"]
    / info["cached_age"] say so. Free slot -> search right here. All slots busy -> queue the search in the
    DB and wait for whichever server claims it (possibly s4 itself).
    progress_cb(info) gets {"phase": "queued"|"running", "done", "total", "ahead"} and, at the
    SEARCH_PARTIAL_TIMES marks, {"phase": "running", "partial": [early results]}.
    cancel_event aborts the search and raises SearchCancelled.
    In /testsearches mode the list handed back is the one at SEARCH_TIMEOUT; the search keeps running in the
    background, stores the late answers in the cache and writes a test report."""
    if info is None:
        info = {}
    if not fresh:
        cached = _search_cache_lookup(query)
        if cached:
            count = cached["source_count"] or 0
            _search_log_cached(chat_id, user_id, user, query, mode, cached["results"], count)
            info["cached"] = True
            info["cached_age"] = float(cached.get("age_seconds") or 0)
            print(f"[SEARCH] cache hit for user={user_id} query={query!r}")
            return _deserialize_search_results(cached["results"]), count

    test_timeout = _search_test_timeout()

    if search_lock.acquire(blocking=False):
        row_id = _search_log_insert(chat_id, user_id, user, query, mode, queued=False)
        local_progress = local_partial = None
        if progress_cb:
            local_progress = lambda d, t: progress_cb({"phase": "running", "done": d, "total": t})
            local_partial = lambda res: progress_cb({"phase": "running", "partial": res})
        outcome = {}
        ready = threading.Event()
        report = {} if test_timeout else None

        def _on_final(res, total):
            outcome.setdefault("final", (res, total))
            ready.set()

        def _runner():
            try:
                results, count = run_lncrawl_search(
                    query, on_progress=local_progress, on_partial=local_partial, cancel_event=cancel_event,
                    on_final=_on_final, test_timeout=test_timeout, report_out=report,
                )
                outcome["done"] = (results, count)
                try:
                    _search_log_finish(row_id, _serialize_search_results(results), count)
                except Exception as e:
                    print(f"[SEARCH DB] could not log results for {row_id}: {e}")
                if report:
                    _save_search_test_report(row_id, query, report)
            except SearchCancelled:
                outcome["cancelled"] = True
                _search_log_finish(row_id, status="cancelled")
            except Exception as error:
                outcome["error"] = error
                _search_log_finish(row_id, status="failed", error=f"{type(error).__name__}: {error}")
            finally:
                search_lock.release()
                ready.set()

        threading.Thread(target=_runner, daemon=True).start()
        ready.wait()
        if "cancelled" in outcome:
            raise SearchCancelled()
        if "error" in outcome:
            raise outcome["error"]
        if "done" in outcome:
            return outcome["done"]
        return outcome["final"]

    row_id = _search_log_insert(chat_id, user_id, user, query, mode, queued=True)
    if row_id is None:
        # DB unavailable: fall back to waiting for a local slot.
        with search_lock:
            return run_lncrawl_search(query, cancel_event=cancel_event)

    print(f"[SEARCH] {SERVER_ID} busy; queued search {row_id} for user={user_id} query={query!r}")
    deadline = time.time() + SEARCH_QUEUE_WAIT
    last_partial_seq = 0
    while time.time() < deadline:
        time.sleep(SEARCH_POLL_INTERVAL)
        if cancel_event is not None and cancel_event.is_set():
            _search_cancel(row_id)
            raise SearchCancelled()
        row = _search_get(row_id, want_partial=True)
        if not row:
            continue
        if row["status"] == "done":
            return _deserialize_search_results(row["results"]), row["source_count"] or 0
        if row["status"] == "cancelled":
            raise SearchCancelled()
        if row["status"] in ("failed", "abandoned"):
            raise Exception(row["error"] or "Search failed")
        if progress_cb:
            try:
                progress_cb({
                    "phase": "queued" if row["status"] == "pending" else "running",
                    "done": row["progress_done"],
                    "total": row["progress_total"],
                    "ahead": row["ahead"],
                })
            except Exception as e:
                print(f"[SEARCH] progress callback failed: {e}")
        try:
            seq, items, is_final = _unpack_partial(row.get("partial_results"))
            if items and seq > last_partial_seq:
                last_partial_seq = seq
                results = _deserialize_search_results(items)
                if is_final:
                    # Test mode on the worker: this is the user's final list, the worker keeps collecting late answers.
                    return results, row["progress_total"] or 0
                if progress_cb:
                    progress_cb({"phase": "running", "partial": results})
        except Exception as e:
            print(f"[SEARCH] partial handling failed: {e}")
    _search_abandon(row_id)
    raise Exception("Search timed out waiting for a free server. Please try again.")


def _claim_search_request():
    conn = get_db_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id FROM search_requests
                WHERE status = 'pending'
                ORDER BY id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            """)
            row = cur.fetchone()
            if not row:
                conn.commit()
                return None
            cur.execute("""
                UPDATE search_requests
                SET status = 'processing', server_id = %s, run_id = %s,
                    started_at = NOW(), attempts = attempts + 1
                WHERE id = %s AND status = 'pending'
                RETURNING *
            """, (SERVER_ID, RUN_ID, row["id"]))
            claimed = cur.fetchone()
            conn.commit()
            return claimed
    except Exception as e:
        conn.rollback()
        print(f"[SEARCH QUEUE] claim failed: {e}")
        return None
    finally:
        conn.close()


def _run_claimed_search(row):
    """Runs on a thread; the caller already holds one search_lock slot."""
    print(f"[SEARCH QUEUE] {SERVER_ID} running search {row['id']} query={row['query']!r}")
    last_write = [0.0]
    cancel_event = threading.Event()
    test_timeout = _search_test_timeout()
    report = {} if test_timeout else None

    def _progress(done, total):
        now = time.time()
        if now - last_write[0] >= SEARCH_PROGRESS_DB_INTERVAL:
            last_write[0] = now
            _search_progress_update(row["id"], done, total)
            if _search_is_cancelled(row["id"]):
                cancel_event.set()      # the requester pressed Cancel

    partial_seq = [0]

    def _partial(results):
        partial_seq[0] += 1
        _search_partial_update(row["id"], {"seq": partial_seq[0], "results": _serialize_search_results(results)})

    def _final(results, total):
        partial_seq[0] += 1
        _search_progress_update(row["id"], total, total)
        _search_partial_update(row["id"], {"seq": partial_seq[0], "results": _serialize_search_results(results), "final": True})

    try:
        results, count = run_lncrawl_search(
            row["query"], on_progress=_progress, on_partial=_partial, cancel_event=cancel_event,
            on_final=_final, test_timeout=test_timeout, report_out=report,
        )
        _search_log_finish(row["id"], _serialize_search_results(results), count)
        if report:
            _save_search_test_report(row["id"], row["query"], report)
    except SearchCancelled:
        print(f"[SEARCH QUEUE] search {row['id']} cancelled")
    except Exception as error:
        print(f"[SEARCH QUEUE] search {row['id']} failed: {type(error).__name__}: {error}")
        _search_log_finish(row["id"], status="failed", error=f"{type(error).__name__}: {error}")
    finally:
        search_lock.release()


def process_search_queue_loop():
    while True:
        if not search_lock.acquire(blocking=False):
            time.sleep(1.0)      # this server is busy with a search
            continue
        started = False
        try:
            row = _claim_search_request()
            if row:
                threading.Thread(target=_run_claimed_search, args=(row,), daemon=True).start()
                started = True
        except Exception as e:
            print(f"[SEARCH QUEUE ERROR] {e}")
        finally:
            if not started:
                search_lock.release()
        if not started:
            time.sleep(SEARCH_CLAIM_INTERVAL)


def reap_orphaned_searches():
    cycles = 0
    while True:
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cur:
                    # processing rows whose server died, restarted or hung
                    cur.execute("""
                        SELECT p.id, p.queued, p.attempts
                        FROM search_requests p
                        LEFT JOIN servers s ON s.server_id = p.server_id
                        WHERE p.status = 'processing'
                          AND (
                              s.server_id IS NULL
                              OR s.status = 'offline'
                              OR s.run_id IS DISTINCT FROM p.run_id
                              OR s.last_heartbeat < NOW() - INTERVAL '60 seconds'
                              OR p.started_at < NOW() - INTERVAL '15 minutes'
                          )
                    """)
                    for o in cur.fetchall():
                        if o["queued"] and (o["attempts"] or 0) < 2:
                            cur.execute("""
                                UPDATE search_requests
                                SET status = 'pending', server_id = NULL, run_id = NULL, started_at = NULL
                                WHERE id = %s AND status = 'processing'
                            """, (o["id"],))
                        else:
                            cur.execute("""
                                UPDATE search_requests
                                SET status = 'failed', error = 'worker lost', completed_at = NOW()
                                WHERE id = %s AND status = 'processing'
                            """, (o["id"],))
                    # nobody is waiting for these anymore
                    cur.execute("""
                        UPDATE search_requests p
                        SET status = 'abandoned', error = 'requester gone', completed_at = NOW()
                        WHERE p.status = 'pending'
                          AND (
                              p.created_at < NOW() - make_interval(secs => %s)
                              OR EXISTS (
                                  SELECT 1 FROM servers s
                                  WHERE s.server_id = p.origin_server
                                    AND s.run_id IS DISTINCT FROM p.origin_run_id
                              )
                          )
                    """, (SEARCH_QUEUE_WAIT + 30,))
                    cycles += 1
                    if cycles % 240 == 1:   # roughly hourly
                        cur.execute(
                            "DELETE FROM search_requests WHERE created_at < NOW() - make_interval(days => %s)",
                            (SEARCH_HISTORY_DAYS,),
                        )
                    conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"[SEARCH REAPER ERROR] {e}")
            finally:
                conn.close()
        time.sleep(15)


if WORKER_ENABLED and ALLOWED_CRAWLER == "v4":
    threading.Thread(target=process_search_queue_loop, daemon=True, name="search-queue").start()
threading.Thread(target=reap_orphaned_searches, daemon=True, name="search-reaper").start()


def _run_search_probe():
    """One probe: search "the" for up to 10 minutes, save a report, update every source's priority."""
    print(f"[SEARCH PROBE] {SERVER_ID} probing {SEARCH_PROBE_QUERY!r}")
    report = {}
    run_lncrawl_search(
        SEARCH_PROBE_QUERY,
        test_timeout=SEARCH_TEST_MAX_SECONDS,
        report_out=report,
        update_priority=True,
    )
    if report:
        report["probe"] = True
        _save_search_test_report(None, SEARCH_PROBE_QUERY, report)
    _api_setting_set("search_probe_at", str(time.time()))
    print(
        f"[SEARCH PROBE] recorded {len(report.get('sources', []))} answers, "
        f"{len(report.get('never_started', []))} never started"
    )


def _daily_search_probe_loop():
    """s4 runs a probe when 24 hours have passed since the last one, or when an admin asks
    with /testsearches probe (that sets search_probe_request). A probe always runs the full
    10 minutes, holds one search slot, and does not need /testsearches to be on."""
    time.sleep(90)
    while True:
        try:
            def _num(key):
                try:
                    return float(_api_setting_get(key, "0") or 0)
                except ValueError:
                    return 0.0
            last = _num("search_probe_at")
            requested = _num("search_probe_request")
            due = (not last) or time.time() - last >= SEARCH_PROBE_INTERVAL
            asked = requested > last
            if not (due or asked):
                time.sleep(10)
                continue
            if not search_lock.acquire(blocking=False):
                time.sleep(15)
                continue
            try:
                _api_setting_set("search_probe_running", str(time.time()))
                try:
                    _run_search_probe()
                finally:
                    _api_setting_set("search_probe_running", "0")
            finally:
                search_lock.release()
        except Exception as error:
            print(f"[SEARCH PROBE] {error}")
            time.sleep(60)
            continue
        time.sleep(10)


if SERVER_ID == "s4" and WORKER_ENABLED and ALLOWED_CRAWLER == "v4":
    threading.Thread(target=_daily_search_probe_loop, daemon=True, name="search-probe").start()

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

    caption = (
        f"📖 <b>Novel:</b> {safe_title}\n"
        f'🌐 <b>Source:</b> <a href="{safe_url}">{safe_url}</a>'
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
    file_size = os.path.getsize(file_path) if os.path.isfile(file_path) else 0
    with _local_api_lock:
        using_local = _local_api_available and bool(LOCAL_BOT_API_URL)
    timeout = 600 if file_size > 40 * 1024 * 1024 else 180

    for attempt in range(1, max_retries + 1):
        document = None
        try:
            # Always upload the file bytes. The local Bot API server runs on
            # the VPS, not on this worker, so passing a file path string would
            # make Telegram treat it as a file_id/URL and reject it.
            document = open(file_path, "rb")
            try:
                return bot.send_document(
                    chat_id,
                    document,
                    caption=caption,
                    timeout=timeout,
                )
            finally:
                document.close()

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

            err_text = str(error).lower()
            if "too big" in err_text or "request entity too large" in err_text:
                print(f"[EPUB TOO BIG] chat_id={chat_id} size={file_size} using_local={using_local}: {error}")
                return None

            print(f"[SEND RETRY {attempt}/{max_retries}] {error}")

        except Exception as error:
            print(f"[SEND RETRY {attempt}/{max_retries}] {error}")

        if attempt < max_retries:
            time.sleep(base_delay * attempt)

    print(f"[EPUB SEND FAILED] chat_id={chat_id} size={file_size} using_local={using_local}")
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
    gofile_url = None
    buzzheavier_url = None
    gofile_error = None
    buzzheavier_error = None

    print(f"[EXTERNAL] Task {task_id}: uploading to Buzzheavier first...")
    try:
        buzzheavier_url, buzzheavier_error = upload_to_buzzheavier(file_path)
    except Exception as error:
        buzzheavier_error = str(error)
        print(f"[EXTERNAL] Task {task_id}: Buzzheavier exception → {error}")

    print(f"[EXTERNAL] Task {task_id}: uploading to Gofile...")
    try:
        gofile_url, gofile_error = upload_to_gofile(file_path)
    except Exception as error:
        gofile_error = str(error)
        print(f"[EXTERNAL] Task {task_id}: Gofile exception → {error}")

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
                        f"Gofile: {gofile_error} | Buzzheavier: {buzzheavier_error}",
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
if SERVER_ID == "s4":
    @bot.message_handler(commands=['start'])
    def start(message):
        send_long_temp_message(
            message.chat.id,
            "👋 Welcome! I can help you download novels.\n"
            "Use /search to find a novel by name.\n"
            "Use /download to start a download.\n"
            "Use /cancel to stop all your ongoing or queued downloads.\n"
            "Use /retry after an EPUB reports missing chapters (available for 20 hours).\n"
            "  Shortcuts: /retry <task_id>  or  /retry <url>\n"
            "Use /progress for a snapshot of your running downloads.\n"
            "Use /queue to view pending and running downloads.\n"
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


    def _dispatch_command(message):
        """Run a command that was typed while the bot was waiting for a text reply."""
        try:
            bot.process_new_messages([message])
        except Exception as error:
            print(f"[COMMAND DISPATCH] {error}")

    def _admin_args_reply(message, command):
        user_responded(message.from_user.id)
        _complete_timed_reply(message.chat.id, message.from_user.id)
        text = (message.text or "").strip()
        if not text:
            send_temp_message(message.chat.id, f"Please reply with text. Send {command} again to retry.", message.from_user.id)
            return
        if text.startswith("/"):
            _dispatch_command(message)
            return
        import copy
        forwarded = copy.copy(message)
        forwarded.text = f"{command} {text}"      # same as if they had typed "/addsite site.com" in one go
        _dispatch_command(forwarded)

    def _ask_admin_args(message, prompt):
        """An admin command sent with no details: ask for them and wait (REPLY_PROMPT_TIMEOUT_SECONDS) for the reply."""
        parts = (message.text or "").split()
        if len(parts) > 1 or message.from_user.id not in ADMIN_USER_ID:
            return False
        send_prompt(message.chat.id, prompt, message.from_user.id)
        register_timed_next_step(
            message.chat.id, message.from_user.id, _admin_args_reply, "admin", parts[0].split("@")[0].lower(),
        )
        return True

    @bot.message_handler(commands=["banuser", "unbanuser", "userlimit"])
    def manage_user_control(message):
        if message.from_user.id not in ADMIN_USER_ID:
            safe_send_message(message.chat.id, "❌ Access Denied.")
            return

        parts = message.text.split()

        if _ask_admin_args(message, {
            "/banuser": "Which user do you want to ban? Send the user ID or @username.",
            "/unbanuser": "Which user do you want to unban? Send the user ID or @username.",
            "/userlimit": "Send the user ID or @username, then the daily limit (a number, or off). Example: 123456789 5",
        }.get(parts[0].split("@")[0].lower(), "Send the user ID or @username.")):
            return

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
        if _ask_admin_args(message, "Which website should skip chapter images? Send the domain, for example dreamy-translations.com"):
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
        if _ask_admin_args(message, "Which website should get its chapter images back? Send the domain."):
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

    def _split_overview():
        lines = ["✂️ Split size = chapters per converted file (EPUB and Text are never split)\n"]
        for fmt in SPLITTABLE_FORMATS:
            size = get_split_size(fmt)
            default = SPLIT_DEFAULTS.get(fmt, 0)
            raw = str(_api_setting_get(f"split_size_{fmt}", "") or "").strip()
            value = f"{size} chapters" if size else "all chapters in one file"
            note = " (default)" if not raw else (f" (default is {default or 'all'})" if size != default else " (same as default)")
            lines.append(f"• {fmt}: {value}{note}")
        lines.append(
            "\nChange it:\n"
            "/split pdf 500\n"
            "/split 300 mobi\n"
            "/split pdf all  (do not split)\n"
            "/split pdf default  (back to the default)\n"
            f"Allowed sizes: {SPLIT_MIN}-{SPLIT_MAX} chapters."
        )
        return "\n".join(lines)

    @bot.message_handler(commands=['split'])
    def split_command(message):
        if not _require_site_admin(message):
            return
        args = (message.text or "").split()[1:]
        if not args:
            safe_send_message(message.chat.id, _split_overview())
            return

        fmt, value = None, None
        for arg in args:
            low = arg.lower().lstrip(".")
            if low in OUTPUT_FORMATS:
                fmt = low
            elif low in ("all", "off", "none", "no", "0"):
                value = 0
            elif low in ("default", "reset"):
                value = ""
            elif low.isdigit():
                value = int(low)
            else:
                safe_send_message(message.chat.id, f"I do not understand '{arg}'.\n\n" + _split_overview())
                return

        if fmt is None or value is None:
            safe_send_message(message.chat.id, "Give a format and a size, for example /split pdf 500\n\n" + _split_overview())
            return
        if fmt not in SPLITTABLE_FORMATS:
            safe_send_message(
                message.chat.id,
                f"{fmt} cannot be split: it is built directly, not converted by Calibre.\n"
                f"Formats you can split: {', '.join(SPLITTABLE_FORMATS)}",
            )
            return
        if isinstance(value, int) and value != 0 and not (SPLIT_MIN <= value <= SPLIT_MAX):
            safe_send_message(message.chat.id, f"Use a size between {SPLIT_MIN} and {SPLIT_MAX} chapters, or 'all'.")
            return

        if not _api_setting_set(f"split_size_{fmt}", value):
            safe_send_message(message.chat.id, "Database error, the setting was not saved.")
            return
        size = get_split_size(fmt)
        if size:
            result = f"✅ {fmt.upper()} files will be made in parts of {size} chapters (books with fewer chapters stay in one file)."
        else:
            result = f"✅ {fmt.upper()} will be made as one file with all chapters."
        safe_send_message(message.chat.id, result)

    @bot.message_handler(commands=['bansite'])
    def bansite(message):
        if not _require_site_admin(message):
            return
        if _ask_admin_args(message, "Which website do you want to block, and why? Send the domain followed by the reason, for example: site.com copyright complaint"):
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
        if _ask_admin_args(message, "Which website do you want to unblock? Send the domain."):
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
        if _ask_admin_args(message, "Which novel website do you want to add to the /sources list? Send the domain, optionally followed by a note, for example: dreamy-translations.com fast updates"):
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
        if _ask_admin_args(message, "Which website do you want to remove from the /sources list? Send the domain."):
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
        _complete_timed_reply(chat_id, user_id)

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

        register_timed_next_step(
            chat_id,
            user_id,
            get_cancel_task_selection,
            'cancel',
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
    def queue_command(message):
        conn = get_db_connection()
        if not conn:
            send_temp_message(
                message.chat.id,
                "⚠️ Cannot connect to Database. If error persists, contact https://t.me/patiorjunrick004 with this message.",
                message.from_user.id
            )
            return

        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, user_id, url, chapter_range, status, server_id,
                           crawler_version, novel_title, created_at, last_progress_at
                    FROM tasks
                    WHERE status IN ('pending', 'running')
                    ORDER BY
                        CASE status WHEN 'running' THEN 0 ELSE 1 END,
                        id ASC
                """)
                tasks = cur.fetchall()
                if not tasks:
                    send_temp_message(
                        message.chat.id,
                        "ℹ️ Queue is empty.",
                        message.from_user.id
                    )
                    return

                text = "📋 Pending / Running tasks Queue:\n\n"

                for t in tasks:
                    title = t.get("novel_title") or "N/A"
                    text += (
                        f"🆔 Task ID: {t['id']}\n"
                        f"👤 User ID: {t['user_id']}\n"
                        f"📖 Title: {title}\n"
                        f"🖥 Server: {t['server_id'] or '—'}\n"
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

    @bot.message_handler(commands=['progress'])
    def cmd_progress(message):
        """
        Static snapshot of the user's currently running tasks.
        Prefer local in-memory snapshot; fall back to DB columns written by workers.
        Does not auto-update — send /progress again for a fresh view.
        """
        user_id = message.from_user.id
        chat_id = message.chat.id

        conn = get_db_connection()
        if not conn:
            send_temp_message(chat_id, "⚠️ Database error.", user_id)
            return

        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute("""
                    SELECT id, url, chapter_range, novel_title, total_chapters,
                           server_id, crawler_version, created_at, last_progress_at,
                           progress_done, progress_total, progress_pct,
                           progress_status, progress_elapsed, progress_remaining,
                           progress_speed
                    FROM tasks
                    WHERE user_id=%s AND status='running'
                    ORDER BY id ASC
                """, (user_id,))
                rows = cur.fetchall()
        finally:
            conn.close()

        if not rows:
            send_temp_message(chat_id, "ℹ️ You have no running downloads right now.", user_id)
            return

        def fmt_dur(seconds):
            seconds = max(0, int(round(seconds or 0)))
            m, s = divmod(seconds, 60)
            h, m = divmod(m, 60)
            if h:
                return f"{h:02d}:{m:02d}:{s:02d}"
            return f"{m:02d}:{s:02d}"

        for t in rows:
            # Local snapshot wins when this process is the worker; else DB.
            snap = progress_snapshots.get(t["id"], {})
            title = snap.get("title") or t.get("novel_title") or "N/A"
            done = snap.get("done")
            if done is None:
                done = t.get("progress_done") or 0
            total = snap.get("total") or t.get("progress_total") or t.get("total_chapters") or 0
            pct = snap.get("pct")
            if pct is None:
                pct = float(t.get("progress_pct") or 0)
            status = snap.get("status") or t.get("progress_status") or "running"
            elapsed = snap.get("elapsed")
            if elapsed is None:
                elapsed = float(t.get("progress_elapsed") or 0)
            remaining = snap.get("remaining")
            if remaining is None:
                remaining = float(t.get("progress_remaining") or 0)
            speed = snap.get("speed")
            if speed is None:
                speed = float(t.get("progress_speed") or 0)
            range_desc = snap.get("range_desc") or format_range_desc(
                t["chapter_range"], t.get("total_chapters") or total or 0
            )

            text = (
                f"📖 {title}\n\n"
                f"🆔 Task ID: {t['id']}\n"
                f"🖥 Server: {t['server_id'] or '—'}\n"
                f"⚙️ Crawler: {t['crawler_version']}\n"
                f"🌐 URL: {t['url']}\n"
                f"🔍 Range: {range_desc}\n\n"
                f"📊 Progress: {pct:.1f}%\n"
                f"📖 Currently At: Chapter {done}"
                + (f" / {total}" if total else "")
                + f"\n📌 Status: {status}\n\n"
                f"⏱ Elapsed: {fmt_dur(elapsed)}\n"
                f"⌛ Remaining: {fmt_dur(remaining)}\n"
                f"⚡ Speed: {speed:.2f} s/chapter\n\n"
                "(Static snapshot — send /progress again for a fresh view.)"
            )
            send_temp_message(chat_id, text, user_id)

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

    def _queue_user_retry(user_id, chat_id, *, task_id=None, retry_url=None):
        """
        Shared logic for /retry. Anyone may retry a completed v4 task that still
        has missing chapters (or was hard-stopped) within the retry window.
        Accepts either a task_id or a novel URL.
        Returns (ok: bool, message: str).
        """
        conn = get_db_connection()
        if not conn:
            return False, "⚠️ Database error. Try again."

        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                # Lock on the requester so concurrent /retry from same user serialize.
                cur.execute("SELECT pg_advisory_xact_lock(%s);", (user_id,))
                cur.execute(
                    "DELETE FROM user_retry_slots WHERE user_id=%s AND first_retry_at < NOW() - INTERVAL '24 hours'",
                    (user_id,),
                )

                if task_id is not None:
                    cur.execute("""
                        SELECT *
                        FROM tasks
                        WHERE id=%s
                          AND crawler_version='v4'
                          AND status IN ('done', 'failed')
                          AND (
                              missing_chapters > 0
                              OR (error IS NOT NULL AND error LIKE 'Hard limit%%')
                          )
                          AND (
                              retry_expires_at > NOW()
                              OR retry_expires_at IS NULL
                          )
                        FOR UPDATE;
                    """, (task_id,))
                else:
                    cur.execute("""
                        SELECT *
                        FROM tasks
                        WHERE crawler_version='v4'
                          AND status IN ('done', 'failed')
                          AND (
                              missing_chapters > 0
                              OR (error IS NOT NULL AND error LIKE 'Hard limit%%')
                          )
                          AND (
                              retry_expires_at > NOW()
                              OR retry_expires_at IS NULL
                          )
                          AND rtrim(url, '/') = rtrim(%s, '/')
                        ORDER BY completed_at DESC NULLS LAST, id DESC
                        LIMIT 1
                        FOR UPDATE;
                    """, (retry_url,))

                task = cur.fetchone()
                if not task:
                    conn.rollback()
                    return False, (
                        "❌ No completed/failed v4 EPUB with missing chapters "
                        "(or hard-stopped within the retry window) was found for that link/task."
                    )
                if not task['server_id']:
                    conn.rollback()
                    return False, "❌ The original worker is unknown, so this EPUB cannot be retried safely."

                cur.execute(
                    "SELECT 1 FROM user_retry_slots WHERE user_id=%s AND task_id=%s;",
                    (user_id, task['id']),
                )
                uses_existing_slot = cur.fetchone() is not None
                if not uses_existing_slot:
                    cur.execute(
                        "SELECT COUNT(*) AS count FROM user_retry_slots WHERE user_id=%s;",
                        (user_id,),
                    )
                    if cur.fetchone()['count'] >= USER_RETRY_SLOT_LIMIT:
                        conn.rollback()
                        return False, (
                            "❌ You can retry up to 3 different novels in each rolling 24-hour period. "
                            "You can still retry a novel you already retried."
                        )
                    cur.execute(
                        "INSERT INTO user_retry_slots (user_id, task_id) VALUES (%s, %s);",
                        (user_id, task['id']),
                    )

                # Prefer original server (cache lives there); clear completed_at so it runs again.
                cur.execute("""
                    UPDATE tasks
                    SET status='pending', completed_at=NULL, is_user_retry=TRUE,
                        retry_server_id=%s, retry_attempts=retry_attempts + 1,
                        retry_expires_at=COALESCE(retry_expires_at, NOW() + (%s * INTERVAL '1 hour')),
                        error=NULL
                    WHERE id=%s;
                """, (task['server_id'], USER_RETRY_WINDOW_HOURS, task['id']))
                conn.commit()
        except Exception as error:
            try:
                conn.rollback()
            except Exception:
                pass
            print(f"[USER RETRY ERROR] {error}")
            return False, "❌ Could not queue the retry. Try again shortly."
        finally:
            conn.close()

        return True, (
            f"🔁 Retry queued for Task {task['id']}. "
            f"It will run next on {task['server_id']} after that server finishes its current download."
        )

    @bot.message_handler(commands=['retry'])
    def retry_download(message):
        user_id = message.from_user.id
        chat_id = message.chat.id
        parts = (message.text or "").split(maxsplit=1)

        # /retry <task_id>  or  /retry <url>
        if len(parts) == 2:
            arg = parts[1].strip()
            if arg.isdigit():
                ok, msg = _queue_user_retry(user_id, chat_id, task_id=int(arg))
            elif arg.startswith("http"):
                ok, msg = _queue_user_retry(user_id, chat_id, retry_url=arg)
            else:
                send_temp_message(
                    chat_id,
                    "Usage:\n"
                    "/retry\n"
                    "/retry <task_id>\n"
                    "/retry <novel_url>\n\n"
                    "Any completed/failed v4 task with missing chapters "
                    "(within the 20-hour window) can be retried.",
                    user_id,
                )
                return
            send_temp_message(chat_id, msg, user_id)
            return

        # Interactive: /retry → ask for URL
        pending_retry_urls[user_id] = chat_id
        send_prompt(
            chat_id,
            "Send a novel link or task ID within 20 hours to retry missing chapters.\n"
            "Works for any completed/failed v4 EPUB with missing chapters "
            "(or a hard-stopped task). Anyone may trigger the retry.\n\n"
            "Shortcuts: /retry <task_id>  or  /retry <url>",
            user_id,
        )
        register_timed_next_step(chat_id, user_id, receive_retry_url, "retry")

    def receive_retry_url(message):
        user_id = message.from_user.id
        chat_id = message.chat.id
        _complete_timed_reply(chat_id, user_id)
        if pending_retry_urls.pop(user_id, None) != chat_id:
            return

        text = (message.text or "").strip()
        if text.isdigit():
            ok, msg = _queue_user_retry(user_id, chat_id, task_id=int(text))
        else:
            ok, msg = _queue_user_retry(user_id, chat_id, retry_url=text)
        send_temp_message(chat_id, msg, user_id)

    # ---------------------- DOWNLOAD COMMAND ----------------------

    # ---------------------- /search ----------------------
    # SEARCH_LIMIT / SEARCH_CONCURRENCY / SEARCH_TIMEOUT / search_lock and
    # run_lncrawl_search now live at module level (search queue section).
    SEARCH_SESSION_SECONDS = 600
    search_sessions = {}
    active_search_sessions = {}
    search_jobs = {}          # token -> {"cancel": Event, "message_id": id of the 'Searching...' message}
    search_session_lock = threading.Lock()


    def _search_key(chat_id, user_id):
        return chat_id, user_id

    def _close_search_session(token, delete_message=False):
        with search_session_lock:
            session = search_sessions.pop(token, None)
            if session and active_search_sessions.get(_search_key(session["chat_id"], session["user_id"])) == token:
                active_search_sessions.pop(_search_key(session["chat_id"], session["user_id"]), None)
        if session and session.get("searching"):
            job = search_jobs.get(token)
            if job:
                job["cancel"].set()      # closing the list also stops the search that is still running behind it
        if delete_message and session and session.get("message_id"):
            try:
                bot.delete_message(session["chat_id"], session["message_id"])
            except Exception:
                pass
        return session

    def _expire_search_session(token):
        time.sleep(SEARCH_SESSION_SECONDS)
        _close_search_session(token, delete_message=True)

    def _search_callback_data(token, action, value=""):
        return f"search:{token}:{action}:{value}"

    def _search_session(token, call):
        with search_session_lock:
            session = search_sessions.get(token)
        if not session or session["expires"] <= time.time():
            _close_search_session(token, delete_message=True)
            bot.answer_callback_query(call.id, "This session has ended. Search again.")
            return None
        if session["chat_id"] != call.message.chat.id or session["user_id"] != call.from_user.id:
            bot.answer_callback_query(call.id, "This search belongs to another user.")
            return None
        return session

    def _search_markup(session, view, index=None):
        token = session["token"]
        markup = telebot.types.InlineKeyboardMarkup()
        if view == "list":
            page = session["page"]
            start = page * RESULTS_PER_PAGE
            for result_index, result in enumerate(session["results"][start:start + RESULTS_PER_PAGE], start=start):
                label = f"{result_index + 1}. {result.title}"[:60]
                markup.add(telebot.types.InlineKeyboardButton(
                    label,
                    callback_data=_search_callback_data(token, "title", result_index),
                ))
            pages = max(1, (len(session["results"]) + RESULTS_PER_PAGE - 1) // RESULTS_PER_PAGE)
            counter = telebot.types.InlineKeyboardButton(f"{page + 1}/{pages}", callback_data=_search_callback_data(token, "noop"))
            if pages > 1:
                # [<<] [Back] [24/35] [Next] [>>]  - Back from page 1 wraps to the last page, Next from the last wraps to page 1
                markup.row(
                    telebot.types.InlineKeyboardButton("<<", callback_data=_search_callback_data(token, "page", 0)),
                    telebot.types.InlineKeyboardButton("Back", callback_data=_search_callback_data(token, "page", (page - 1) % pages)),
                    counter,
                    telebot.types.InlineKeyboardButton("Next", callback_data=_search_callback_data(token, "page", (page + 1) % pages)),
                    telebot.types.InlineKeyboardButton(">>", callback_data=_search_callback_data(token, "page", pages - 1)),
                )
            else:
                markup.row(counter)
            if session.get("from_cache") and not session.get("searching"):
                markup.row(telebot.types.InlineKeyboardButton(
                    "🔄 Refresh search results", callback_data=_search_callback_data(token, "refresh"),
                ))
        elif view == "sources":
            result = session["results"][index]
            for source_index, novel in enumerate(result.novels):
                label = f"{source_index + 1}. {novel.url}"[:60]
                markup.add(telebot.types.InlineKeyboardButton(
                    label,
                    callback_data=_search_callback_data(token, "source", f"{index}-{source_index}"),
                ))
            markup.row(telebot.types.InlineKeyboardButton("Back", callback_data=_search_callback_data(token, "list")))
        else:
            markup.row(
                telebot.types.InlineKeyboardButton("Yes", callback_data=_search_callback_data(token, "yes", index)),
                telebot.types.InlineKeyboardButton("Back", callback_data=_search_callback_data(token, "sources", index)),
            )
        markup.row(telebot.types.InlineKeyboardButton("Cancel", callback_data=_search_callback_data(token, "cancel")))
        return markup

    def _search_edit(session, text, markup):
        try:
            if session.get("message_id"):
                bot.edit_message_text(
                    text,
                    session["chat_id"],
                    session["message_id"],
                    parse_mode="HTML",
                    reply_markup=markup,
                    link_preview_options=telebot.types.LinkPreviewOptions(is_disabled=True),
                )
                return
        except Exception as error:
            # Pressing << on page 1 (or any button that renders the same page) makes Telegram
            # answer "message is not modified". That is not a failure, so do not post a duplicate.
            if "message is not modified" in str(error).lower():
                return
        try:
            sent = bot.send_message(
                session["chat_id"],
                text,
                parse_mode="HTML",
                reply_markup=markup,
                link_preview_options=telebot.types.LinkPreviewOptions(is_disabled=True),
            )
            session["message_id"] = sent.message_id
        except Exception as error:
            print(f"[SEARCH MESSAGE ERROR] {error}")

    def _merge_search_results(shown, new, keep):
        """Fold a newer ranked list into the one on screen. Whatever sits on page 1 (the first `keep`
        entries) keeps its number and order and only gains sources; everything after that follows the new ranking."""
        new_by_id = {r.id: r for r in new}
        frozen = [new_by_id.pop(r.id, r) for r in shown[:keep]]
        rest = [r for r in new if r.id in new_by_id]
        return frozen + rest

    def _render_search_list(session):
        pending = session.pop("pending", None)
        if pending is not None:
            session["results"] = _merge_search_results(session["results"], pending, RESULTS_PER_PAGE)
        session["view"] = "list"
        page = session["page"]
        start = page * RESULTS_PER_PAGE
        entries = []
        for result_index, result in enumerate(session["results"][start:start + RESULTS_PER_PAGE], start=start + 1):
            entries.append(f"<b>{result_index}. {html.escape(result.title)}</b> ({len(result.novels)} source(s))")
        text = "<b>Here are the novels I found for your query:</b>\n\n" + "\n\n".join(entries)
        if session.get("searching"):
            done, total = session.get("progress") or (0, 0)
            text += "\n\n" + SEARCH_STILL_TEXT
            if total:
                text += f" Searched {done}/{total} sources"
        elif session.get("from_cache"):
            text += "\n\n" + SEARCH_CACHED_TEXT.format(age=_format_age(session.get("cache_age")))
        _search_edit(session, text, _search_markup(session, "list"))

    def _render_search_sources(session, result_index):
        session["view"] = "sources"
        result = session["results"][result_index]
        lines = [f"<b>{result_index + 1}. {html.escape(result.title)}</b> ({len(result.novels)} source(s))"]
        for source_index, novel in enumerate(result.novels, start=1):
            lines.append(f"\n{source_index}. {html.escape(novel.url)}")
            info = (novel.info or "").strip()
            if info:
                lines.append(f"<i>{html.escape(info[:500])}</i>")
        _search_edit(session, "\n".join(lines), _search_markup(session, "sources", result_index))

    def _render_search_confirm(session, result_index, source_index):
        result = session["results"][result_index]
        novel = result.novels[source_index]
        session["selection"] = (result_index, source_index)
        session["view"] = "confirm"
        text = (
            "<b>Download this novel?</b>\n\n"
            f"{html.escape(result.title)}\n"
            f"{html.escape(novel.url)}"
        )
        _search_edit(session, text, _search_markup(session, "confirm", result_index))

    def _start_search(chat_id, user_id, query, mode="search", user=None, fresh=False):
        key = _search_key(chat_id, user_id)
        token = uuid.uuid4().hex[:12]
        old_session = None
        old_job = None
        with search_session_lock:
            old_token = active_search_sessions.get(key)
            if old_token:
                old_session = search_sessions.pop(old_token, None)
                old_job = search_jobs.get(old_token)
            active_search_sessions[key] = token
            search_jobs[token] = {"cancel": threading.Event(), "message_id": None}
        if old_job:
            old_job["cancel"].set()      # a new search replaces the old one, so stop the old one too
        old_message_ids = {(old_session or {}).get("message_id"), (old_job or {}).get("message_id")}
        for message_id in old_message_ids:
            if message_id:
                try:
                    bot.delete_message(chat_id, message_id)
                except Exception:
                    pass
        threading.Thread(target=_search_worker, args=(chat_id, user_id, query, mode, token, user, fresh), daemon=True).start()

    def _search_progress_text(query, info):
        head = f"Searching for {query}...\n{SEARCH_ETA_TEXT}"
        if info.get("phase") == "queued":
            return f"{head}\n\nWaiting for a free server..."
        done, total = info.get("done") or 0, info.get("total") or 0
        if not total:
            return head
        return f"{head}\n\nSearched {done}/{total} sources"

    def _search_worker(chat_id, user_id, query, mode, token, user=None, fresh=False):
        job = search_jobs.get(token) or {"cancel": threading.Event(), "message_id": None}
        try:
            _run_search_job(chat_id, user_id, query, mode, token, user, fresh, job)
        finally:
            search_jobs.pop(token, None)

    def _run_search_job(chat_id, user_id, query, mode, token, user, fresh, job):
        cancel_event = job["cancel"]
        stop_markup = telebot.types.InlineKeyboardMarkup()
        stop_markup.row(telebot.types.InlineKeyboardButton("Cancel", callback_data=f"searchstop:{token}"))
        try:
            searching_msg = bot.send_message(chat_id, f"Searching for {query}...\n{SEARCH_ETA_TEXT}", reply_markup=stop_markup)
        except Exception as error:
            print(f"[SEARCH MESSAGE ERROR] {error}")
            searching_msg = None
        if searching_msg:
            job["message_id"] = searching_msg.message_id
        state = {"last_edit": time.time(), "last_text": None, "session": None, "progress": (0, 0), "finished": False}

        def _drop_searching_msg():
            if searching_msg:
                try:
                    bot.delete_message(chat_id, searching_msg.message_id)
                except Exception:
                    pass

        def _show_partial(partial):
            """Interim list (at 50s, 70s). The first one turns the 'Searching...' message into the list;
            later ones are folded in so page 1 never reshuffles under the user."""
            if not partial or not searching_msg or state["finished"] or cancel_event.is_set():
                return
            session = state["session"]
            if session is None:
                with search_session_lock:
                    if active_search_sessions.get(_search_key(chat_id, user_id)) != token:
                        return
                    session = {
                        "token": token,
                        "chat_id": chat_id,
                        "user_id": user_id,
                        "query": query,
                        "mode": mode,
                        "results": partial,
                        "page": 0,
                        "expires": time.time() + SEARCH_SESSION_SECONDS,
                        "message_id": searching_msg.message_id,
                        "searching": True,
                        "progress": state["progress"],
                        "view": "list",
                    }
                    search_sessions[token] = session
                state["session"] = session
                _render_search_list(session)
                threading.Thread(target=_expire_search_session, args=(token,), daemon=True).start()
                return
            with search_session_lock:
                if active_search_sessions.get(_search_key(chat_id, user_id)) != token or token not in search_sessions:
                    return
            session["pending"] = partial          # applied now if they are on the list, otherwise when they go back to it
            if session.get("view", "list") == "list":
                _render_search_list(session)

        def _finish_partial(final_results):
            """Search ended after interim lists were shown: apply the final list and drop the 'still searching' note."""
            session = state["session"]
            with search_session_lock:
                if active_search_sessions.get(_search_key(chat_id, user_id)) != token or token not in search_sessions:
                    return
            session["searching"] = False
            if final_results:
                session["pending"] = final_results
            if session.get("view", "list") == "list":
                _render_search_list(session)

        def progress_cb(info):
            if state["finished"] or cancel_event.is_set():
                return
            if "partial" in info:
                _show_partial(info["partial"])
                return
            if not searching_msg:
                return
            done, total = info.get("done") or 0, info.get("total") or 0
            if total:
                state["progress"] = (done, total)
            now = time.time()
            if now - state["last_edit"] < SEARCH_PROGRESS_INTERVAL:
                return
            session = state["session"]
            if session is not None:
                # List is on screen: refresh its "Searched x/y" footer, but never over the sources/confirm screens.
                session["progress"] = state["progress"]
                if session.get("searching") and session.get("view", "list") == "list":
                    state["last_edit"] = now
                    _render_search_list(session)
                return
            text = _search_progress_text(query, info)
            if text == state["last_text"]:
                return
            state["last_edit"] = now
            state["last_text"] = text
            try:
                bot.edit_message_text(text, chat_id, searching_msg.message_id, reply_markup=stop_markup)
            except Exception:
                pass

        info = {}
        try:
            results, source_count = run_search_request(
                chat_id, user_id, user, query, mode, progress_cb=progress_cb,
                cancel_event=cancel_event, fresh=fresh, info=info,
            )
        except SearchCancelled:
            state["finished"] = True
            return                      # whoever cancelled has already cleaned up the messages
        except Exception as error:
            state["finished"] = True
            if state["session"] is not None:
                _finish_partial(None)      # keep the interim list, just stop the 'still searching' note
                return
            _drop_searching_msg()
            print(f"[SEARCH ERROR] user={user_id} query={query!r}: {type(error).__name__}: {error}")
            send_temp_message(chat_id, f"Search failed: {type(error).__name__}: {error}", user_id)
            return
        state["finished"] = True
        if cancel_event.is_set():
            return
        # Cached and queued results may come from an older ordering, so always rank here too.
        results = _rank_search_results(results, query)
        print(f"[SEARCH] user={user_id} query={query!r} -> {len(results)} result(s) from {source_count} sources")
        if state["session"] is not None:
            _finish_partial(results)
            return
        with search_session_lock:
            if active_search_sessions.get(_search_key(chat_id, user_id)) != token:
                _drop_searching_msg()
                return
            if not results:
                active_search_sessions.pop(_search_key(chat_id, user_id), None)
        if not results:
            _drop_searching_msg()
            send_temp_message(chat_id, f"No results found for {query}.\nTry a shorter or different name.", user_id)
            return
        session = {
            "token": token,
            "chat_id": chat_id,
            "user_id": user_id,
            "query": query,
            "mode": mode,
            "results": results,
            "page": 0,
            "expires": time.time() + SEARCH_SESSION_SECONDS,
            # The "Searching..." message turns into the result list in place.
            "message_id": searching_msg.message_id if searching_msg else None,
            "view": "list",
            "from_cache": bool(info.get("cached")),
            "cache_age": info.get("cached_age"),
        }
        with search_session_lock:
            search_sessions[token] = session
        _render_search_list(session)
        threading.Thread(target=_expire_search_session, args=(token,), daemon=True).start()

    def get_search_query(message):
        user_responded(message.from_user.id)
        user_id = message.from_user.id
        chat_id = message.chat.id
        _complete_timed_reply(chat_id, user_id)
        query = (message.text or "").strip()
        if message.text and query.startswith("/"):
            _dispatch_command(message)      # they typed another command: do that instead of searching for it
            return
        if len(query) < 2:
            send_temp_message(chat_id, "The name must be at least 2 characters. Send /search to try again.", user_id)
            return
        _start_search(chat_id, user_id, query, user=message.from_user)

    @bot.message_handler(commands=['search'])
    def search_novels(message):
        user_id = message.from_user.id
        parts = (message.text or "").split(maxsplit=1)
        name = parts[1].strip() if len(parts) > 1 else ""
        if name:
            # /search mad god  -> search straight away
            _clear_timed_reply(message.chat.id)
            user_responded(user_id)
            if len(name) < 2:
                send_temp_message(message.chat.id, "The name must be at least 2 characters.", user_id)
                return
            _start_search(message.chat.id, user_id, name, user=message.from_user)
            return
        send_prompt(message.chat.id, "Enter the novel name to search for:", user_id)
        register_timed_next_step(message.chat.id, user_id, get_search_query, "search")

    @bot.callback_query_handler(func=lambda call: call.data.startswith("searchstop:"))
    def handle_search_stop(call):
        token = call.data.split(":", 1)[1]
        key = _search_key(call.message.chat.id, call.from_user.id)
        with search_session_lock:
            owns = active_search_sessions.get(key) == token
            job = search_jobs.get(token)
        if not owns:
            bot.answer_callback_query(call.id, "This search has already ended.")
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass
            return
        if job:
            job["cancel"].set()
        _close_search_session(token)
        with search_session_lock:
            if active_search_sessions.get(key) == token:
                active_search_sessions.pop(key, None)
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        bot.answer_callback_query(call.id, "Search cancelled.")

    @bot.callback_query_handler(func=lambda call: call.data.startswith("search:"))
    def handle_search_selection(call):
        try:
            _prefix, token, action, value = call.data.split(":", 3)
        except ValueError:
            bot.answer_callback_query(call.id, "Invalid search selection.")
            return
        session = _search_session(token, call)
        if not session:
            return
        if action == "noop":
            bot.answer_callback_query(call.id)
            return
        if action == "cancel":
            _close_search_session(token, delete_message=True)
            bot.answer_callback_query(call.id, "Search cancelled.")
            return
        if action == "refresh":
            query, mode = session["query"], session["mode"]
            _close_search_session(token, delete_message=True)
            bot.answer_callback_query(call.id, "Starting a fresh search...")
            _start_search(call.message.chat.id, call.from_user.id, query, mode, call.from_user, fresh=True)
            return
        if action == "page":
            session["page"] = max(0, min(int(value), (len(session["results"]) - 1) // RESULTS_PER_PAGE))
            _render_search_list(session)
        elif action == "list":
            _render_search_list(session)
        elif action == "title":
            _render_search_sources(session, int(value))
        elif action == "source":
            result_index, source_index = map(int, value.split("-", 1))
            _render_search_confirm(session, result_index, source_index)
        elif action == "sources":
            _render_search_sources(session, int(value))
        elif action == "yes":
            selected = session.get("selection")
            if not selected:
                bot.answer_callback_query(call.id, "Choose a source first.")
                return
            result_index, source_index = selected
            novel = session["results"][result_index].novels[source_index]
            _close_search_session(token, delete_message=True)
            try:
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except Exception:
                pass
            bot.answer_callback_query(call.id, "Starting scan...")
            _start_url_scan(call.message, novel.url, "v4", call.from_user)
            return
        else:
            bot.answer_callback_query(call.id, "Invalid search selection.")
            return
        bot.answer_callback_query(call.id)

    @bot.message_handler(commands=['download'])
    def download(message):
        user_id = message.from_user.id
        parts = (message.text or "").split(maxsplit=1)
        value = parts[1].strip() if len(parts) > 1 else ""
        if value:
            # /download <url>  -> straight to the scan;  /download <title>  -> search
            _clear_timed_reply(message.chat.id)
            user_responded(user_id)
            _handle_download_input(message, value, "v4")
            return
        send_prompt(message.chat.id, "Enter a novel URL or title:", user_id)
        register_timed_next_step(message.chat.id, user_id, get_url, "url", "v4")

    def get_version(message):
        version = force_v4(message.text.strip())

        send_prompt(message.chat.id, "📌 Enter novel URL:", message.from_user.id)
        register_timed_next_step(
            message.chat.id,
            message.from_user.id,
            get_url,
            "url",
            version,
        )


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
        output_format="epub",
    ):
        conn = get_db_connection()
        if not conn:
            send_temp_message(chat_id, "❌ Database error.", user_id)
            return False
        version = force_v4(version)
        output_format = normalize_output_format(output_format)
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

                # Skip-scan race: scan may have finished on any worker before
                # this task row exists. Read finished preview_requests by URL
                # (shared DB) so title/chapter_count survive across processes.
                if not novel_title or not total_chapters:
                    try:
                        cur.execute(
                            """
                            SELECT title, chapter_count
                            FROM preview_requests
                            WHERE rtrim(url, '/') = rtrim(%s, '/')
                              AND user_id = %s
                              AND status = 'done'
                              AND title IS NOT NULL
                              AND created_at >= NOW() - INTERVAL '2 hours'
                            ORDER BY id DESC
                            LIMIT 1
                            """,
                            (url, user_id),
                        )
                        prev = cur.fetchone()
                        if prev:
                            novel_title = novel_title or prev.get("title")
                            if not total_chapters and prev.get("chapter_count"):
                                total_chapters = prev["chapter_count"]
                    except Exception as e:
                        print(f"[INSERT TASK PREVIEW LOOKUP] {e}")

                cur.execute("""
                    INSERT INTO tasks (
                        user_id, chat_id, username, first_name, last_name,
                        url, chapter_range, crawler_version, novel_title,
                        total_chapters, output_format

                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
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
                    output_format,
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
            "✅ Download task has been queued and will be processed shortly.\n"
            f"📦 Format: {output_format_label(output_format)}"
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

        # Range is chosen: ask which file format they want before queueing.
        chat_id = message.chat.id
        pending_format_state[user.id] = {
            "expires": time.time() + FORMAT_SESSION_SECONDS,
            "chat_id": chat_id,
            "url": url,
            "chapter_range": chapter_range,
            "version": version,
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "novel_title": novel_title,
            "total_chapters": total_chapters,
        }
        sent = None
        try:
            sent = bot.send_message(
                chat_id,
                "📦 Which file format do you want?",
                reply_markup=format_picker_markup(),
            )
        except Exception as e:
            print(f"[FORMAT PICKER] {e}")
        if sent is None:
            # Could not show the buttons: fall back to the old EPUB behaviour.
            pending_format_state.pop(user.id, None)
            insert_task_direct(
                chat_id, url, chapter_range, version, user.id, user.username,
                user.first_name, user.last_name, novel_title, total_chapters,
            )

    @bot.callback_query_handler(func=lambda call: call.data.startswith("fmt_"))
    def handle_format_choice(call):
        user_id = call.from_user.id
        chat_id = call.message.chat.id
        choice = call.data[len("fmt_"):]

        state = pending_format_state.get(user_id)
        if not state or state["chat_id"] != chat_id or state["expires"] < time.time():
            pending_format_state.pop(user_id, None)
            try:
                bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
            except Exception:
                pass
            bot.answer_callback_query(call.id, "❌ Session expired. Send /download again.")
            return

        if choice not in OUTPUT_FORMATS:
            bot.answer_callback_query(call.id, "Only EPUB and Text are available.")
            return

        pending_format_state.pop(user_id, None)
        try:
            bot.delete_message(chat_id, call.message.message_id)
        except Exception:
            try:
                bot.edit_message_text(
                    f"📦 Format: {output_format_label(choice)}",
                    chat_id,
                    call.message.message_id,
                    reply_markup=None,
                )
            except Exception:
                pass
            threading.Thread(
                target=delete_message_later,
                args=(chat_id, call.message.message_id, 30),
                daemon=True,
            ).start()
        bot.answer_callback_query(call.id)

        insert_task_direct(
            chat_id,
            state["url"],
            state["chapter_range"],
            state["version"],
            user_id,
            state["username"],
            state["first_name"],
            state["last_name"],
            state["novel_title"],
            state["total_chapters"],
            output_format=choice,
        )




    def _handle_download_input(message, value, version):
        user_id = message.from_user.id
        chat_id = message.chat.id
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            if len(value) < 2:
                send_temp_message(chat_id, "? Enter a valid URL or a novel name with at least 2 characters.", user_id)
                return
            _start_search(chat_id, user_id, value, mode="download", user=message.from_user)
            return
        _start_url_scan(message, value, version, message.from_user)

    def get_url(message, version):
        user_responded(message.from_user.id)
        value = (message.text or "").strip()
        user_id = message.from_user.id
        chat_id = message.chat.id
        _complete_timed_reply(chat_id, user_id)
        if value.startswith("/"):
            _dispatch_command(message)      # they typed another command: do that instead of searching for it
            return
        _handle_download_input(message, value, version)

    def _start_url_scan(message, url, version, requester):
        user_id = requester.id
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

        # v3 is retired: every source is handled by v4.
        version = force_v4(version)

        pid = request_preview(
            url, version, user_id, chat_id,
            username=requester.username,
            first_name=requester.first_name,
            last_name=requester.last_name
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
            args=(message, url, version, requester, user_id, chat_id, pid, scan_cancel_event, scanning_msg),
            daemon=True
        ).start()

    def _await_preview_and_prompt_range(message, url, version, requester, user_id, chat_id, pid, scan_cancel_event, scanning_msg):
        row = wait_for_preview(pid, cancel_event=scan_cancel_event, timeout=7200)
        skip_scan_events.pop(pid, None)

        if row == "skipped" or row is None or (isinstance(row, dict) and row.get('status') == 'abandoned'):
            try:
                edit_and_autodelete(chat_id, scanning_msg.message_id, "⏭ Skipping scan — enter the chapter range manually.")
            except Exception:
                pass
            prompt_manual_range(message, chat_id, user_id, url, version, requester=requester)
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
        start_range_timeout(requester, chat_id, url, version, row['title'], row['chapter_count'])

        ask_v4_range(chat_id, user_id)

    @bot.callback_query_handler(func=lambda call: call.data.startswith("skipscan_"))
    def handle_skip_scan(call):
        pid = int(call.data.replace("skipscan_", ""))
        ev = skip_scan_events.pop(pid, None)
        if ev:
            ev.set()
        # Do NOT abandon the preview — let the scan finish in the background
        # so novel_title / chapter_count can still be written onto the task
        # (and progress message) once available. User proceeds with range entry.
        try:
            bot.answer_callback_query(call.id, "Skipping scan — you can enter the range now. Title will update when scan finishes.")
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
            # v3 is retired; the description/picker no longer applies.
            send_temp_message(
                chat_id,
                "ℹ️ v3.10.1 has been retired because its sources no longer work. "
                "All downloads now run on v4.13.1.\n\nSend /download to start.",
                user_id,
            )
            bot.answer_callback_query(call.id)
            return

        # Old v3/v4 buttons may still sit in earlier messages; both go to v4.
        version = force_v4(call.data.replace("version_", ""))
        send_prompt(chat_id, "📌 Enter novel URL:", user_id)
        register_timed_next_step(chat_id, user_id, get_url, "url", version)
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


    @bot.message_handler(commands=['searches'])
    def searches(message):
        # Admin only, same as /logs. Usage: /searches [count | user_id | @username]
        if message.from_user.id not in ADMIN_USER_ID:
            bot.send_message(message.chat.id, "❌ Access Denied.")
            return

        arg = ((message.text or "").split(maxsplit=1) + [""])[1].strip()
        limit, where, params = 50, "", []
        if arg:
            if arg.isdigit() and len(arg) <= 4:
                limit = max(1, min(int(arg), 200))
            elif arg.isdigit():
                where, params = "WHERE user_id = %s", [int(arg)]
            else:
                where, params = "WHERE LOWER(username) = LOWER(%s)", [arg.lstrip("@")]

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
                cur.execute(f"SELECT COUNT(*) AS total, COUNT(DISTINCT user_id) AS users FROM search_requests {where}", params)
                summary = cur.fetchone()
                cur.execute(f"""
                    SELECT id, user_id, username, first_name, last_name, query, mode,
                           status, queued, server_id, origin_server, source_count,
                           result_count, results, error, attempts, cached,
                           created_at, started_at, completed_at
                    FROM search_requests
                    {where}
                    ORDER BY id DESC
                    LIMIT %s
                """, params + [limit])
                rows = list(reversed(cur.fetchall()))

            if not rows:
                send_temp_message(message.chat.id, "ℹ️ No search logs found.", message.from_user.id)
                return

            text = (
                f"🔎 Search Logs (showing last {len(rows)} of {summary['total']}, "
                f"{summary['users']} user(s))\n\n"
            )
            for t in rows:
                username = f"@{t['username']}" if t['username'] else "N/A"
                first = t['first_name'] or "N/A"
                last = t['last_name'] or "N/A"
                if t['cached']:
                    how = "served from cache"
                elif t['queued']:
                    how = f"queued via DB (asked on {t['origin_server']})"
                else:
                    how = "ran on the requesting server"
                titles = []
                if isinstance(t['results'], list):
                    titles = [r.get('title', '') for r in t['results'][:3]]
                duration = "N/A"
                if t['started_at'] and t['completed_at']:
                    duration = f"{(t['completed_at'] - t['started_at']).total_seconds():.1f}s"
                waited = ""
                if t['queued'] and t['started_at']:
                    waited = f" (waited {(t['started_at'] - t['created_at']).total_seconds():.1f}s)"

                text += (
                    f"🆔 Search ID: {t['id']}\n"
                    f"👤 User ID: {t['user_id']}\n"
                    f"🔗 Username: {username}\n"
                    f"📛 Name: {first} {last}\n"
                    f"🔎 Query: {t['query']}\n"
                    f"🧭 Mode: {t['mode']}\n"
                    f"🖥 Server: {t['server_id'] or 'N/A'} - {how}{waited}\n"
                    f"📌 Status: {t['status']}\n"
                    f"📊 Sources: {t['source_count'] if t['source_count'] is not None else 'N/A'}"
                    f" | Results: {t['result_count'] if t['result_count'] is not None else 'N/A'}\n"
                    + (f"📚 Top: {'; '.join(titles)}\n" if titles else "")
                    + (f"❗ Error: {t['error']}\n" if t['error'] else "")
                    + f"⏱ Duration: {duration}\n"
                    f"🕒 Created: {t['created_at']}\n"
                    f"▶️ Started: {t['started_at']}\n"
                    f"✅ Completed: {t['completed_at']}\n"
                    f"-----------------------------\n"
                )

            send_temp_long_message(message.chat.id, text, message.from_user.id)
        except Exception as e:
            conn.rollback()
            print(f"[SEARCHES CMD ERROR] {e}")
            send_temp_message(message.chat.id, f"⚠️ Could not load search logs: {e}", message.from_user.id)
        finally:
            conn.close()

    def _parse_test_duration(text):
        """'2 minutes', '90s', '5m' -> seconds. A bare number means minutes."""
        m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes)?", text.strip().lower())
        if not m:
            return None
        value = float(m.group(1))
        unit = m.group(2) or "m"
        return int(value if unit.startswith("s") else value * 60)

    def _send_in_chunks(chat_id, text):
        chunk = ""
        for line in text.split("\n"):
            if chunk and len(chunk) + len(line) + 1 > 3900:
                safe_send_message(chat_id, chunk)
                chunk = ""
            chunk += line + "\n"
        if chunk.strip():
            safe_send_message(chat_id, chunk)

    def _test_status_text():
        seconds = _search_test_timeout()
        if not seconds:
            return "🧪 Test searches are OFF."
        try:
            left = float(str(_api_setting_get("search_test", "")).split("|")[1]) - time.time()
        except Exception:
            left = 0
        return (
            f"🧪 Test searches are ON: every search keeps running for up to {seconds}s, while users still get their "
            f"lists on the normal schedule (final list at {SEARCH_TIMEOUT}s). Switches itself off in {_format_age(left)}."
        )

    def _runtime_confirm_reply(message):
        user_id = message.from_user.id
        chat_id = message.chat.id
        user_responded(user_id)
        _complete_timed_reply(chat_id, user_id)
        state = pending_runtime_confirm.pop(user_id, None)
        text = (message.text or "").strip()
        if text.startswith("/"):
            _dispatch_command(message)
            return
        if not state:
            send_temp_message(chat_id, "That confirmation has expired. Send /set again.", user_id)
            return
        answer = text.lower()
        if answer not in ("yes", "y", "no", "n"):
            pending_runtime_confirm[user_id] = state
            send_temp_message(chat_id, "Reply yes or no.", user_id)
            register_timed_next_step(chat_id, user_id, _runtime_confirm_reply, "set")
            return
        if answer in ("no", "n"):
            send_temp_message(chat_id, "Cancelled. Nothing was changed.", user_id)
            return
        if state["action"] == "reset_all":
            if not _delete_all_runtime_overrides():
                send_temp_message(chat_id, "Database error. Nothing was reset.", user_id)
                return
            apply_runtime_settings()
            send_temp_message(
                chat_id,
                "All /set overrides were cleared. Every worker returns to the built-in defaults within 30 seconds. This server already has the defaults.",
                user_id,
            )
            return
        if state["action"] == "reset":
            if not _delete_runtime_override(state["key"]):
                send_temp_message(chat_id, "Database error. Nothing was reset.", user_id)
                return
            apply_runtime_settings()
            send_temp_message(
                chat_id,
                f"{state['key']} is back to the built-in default ({_format_runtime_current(state['key'])}). Other workers follow within 30 seconds.",
                user_id,
            )
            return
        if not _save_runtime_override(state["key"], state["stored"]):
            send_temp_message(chat_id, "Database error. The setting was not changed.", user_id)
            return
        apply_runtime_settings()
        send_temp_message(
            chat_id,
            f"{state['key']} is now {_format_runtime_current(state['key'])}. Other workers follow within 30 seconds.",
            user_id,
        )

    def _ask_runtime_confirm(message, prompt, state):
        pending_runtime_confirm[message.from_user.id] = state
        send_prompt(message.chat.id, prompt, message.from_user.id)
        register_timed_next_step(
            message.chat.id, message.from_user.id, _runtime_confirm_reply, "set",
        )

    @bot.message_handler(commands=["set"])
    def set_runtime(message):
        if message.from_user.id not in ADMIN_USER_ID:
            safe_send_message(message.chat.id, "❌ Access Denied.")
            return
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) == 1:
            send_temp_long_message(message.chat.id, _runtime_settings_listing(), message.from_user.id)
            return
        name = _canon_runtime_key(parts[1])
        if name not in RUNTIME_SPECS:
            send_temp_message(
                message.chat.id,
                f"Unknown setting {parts[1]}. Send /set alone to see every name.",
                message.from_user.id,
            )
            return
        if len(parts) < 3:
            send_temp_message(
                message.chat.id,
                f"{name} is {_format_runtime_current(name)} (built-in default {RUNTIME_DEFAULTS[name]}).\n"
                f"Usage: /set {name} VALUE",
                message.from_user.id,
            )
            return
        stored, error = _parse_runtime_value(name, parts[2])
        if error:
            send_temp_message(message.chat.id, error, message.from_user.id)
            return
        current = _format_runtime_current(name)
        if stored == current:
            send_temp_message(message.chat.id, f"{name} is already {current}.", message.from_user.id)
            return
        _ask_runtime_confirm(
            message,
            f"Change {name} from {current} to {stored}?\n\nReply yes or no. This request ends in 10 minutes.",
            {"action": "set", "key": name, "stored": stored},
        )

    @bot.message_handler(commands=["reset"])
    def reset_runtime(message):
        if message.from_user.id not in ADMIN_USER_ID:
            safe_send_message(message.chat.id, "❌ Access Denied.")
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            send_temp_message(
                message.chat.id,
                "Usage:\n/reset NAME\n/reset all\n\nSend /set to see every name.",
                message.from_user.id,
            )
            return
        target = parts[1].strip()
        if target.lower() == "all":
            _ask_runtime_confirm(
                message,
                "Clear every /set change and return all of those settings to the built-in defaults?\n\nReply yes or no. This request ends in 10 minutes.",
                {"action": "reset_all"},
            )
            return
        name = _canon_runtime_key(target)
        if name not in RUNTIME_SPECS:
            send_temp_message(
                message.chat.id,
                f"Unknown setting {target}. Send /set alone to see every name.",
                message.from_user.id,
            )
            return
        current = _format_runtime_current(name)
        default = str(RUNTIME_DEFAULTS[name])
        if current == default:
            send_temp_message(message.chat.id, f"{name} is already the built-in default ({default}).", message.from_user.id)
            return
        _ask_runtime_confirm(
            message,
            f"Reset {name} from {current} back to the built-in default {default}?\n\nReply yes or no. This request ends in 10 minutes.",
            {"action": "reset", "key": name},
        )

    @bot.message_handler(commands=['testsearches'])
    def testsearches(message):
        # Admin only. /testsearches [on | off | <time> | report [number]]
        if message.from_user.id not in ADMIN_USER_ID:
            safe_send_message(message.chat.id, "❌ Access Denied.")
            return
        chat_id = message.chat.id
        parts = (message.text or "").split(maxsplit=1)
        arg = parts[1].strip().lower() if len(parts) > 1 else ""

        if arg in ("off", "stop", "disable"):
            _search_test_set(None)
            safe_send_message(chat_id, "🧪 Test searches are OFF. Searches stop at the normal timeout again.")
            return

        if arg == "probe":
            try:
                running = float(_api_setting_get("search_probe_running", "0") or 0)
            except ValueError:
                running = 0
            if running and time.time() - running < SEARCH_TEST_MAX_SECONDS + 300:
                safe_send_message(chat_id, f"🧪 A probe is already running (started {_format_age(time.time() - running)} ago). "
                                           "It updates the priorities when it finishes.")
                return
            if not _api_setting_set("search_probe_request", str(time.time())):
                safe_send_message(chat_id, "❌ Database error. Probe was not requested.")
                return
            safe_send_message(
                chat_id,
                f"🧪 Probe requested. s4 starts it within about 15 seconds, searches \"{SEARCH_PROBE_QUERY}\" for up to "
                f"{SEARCH_TEST_MAX_SECONDS // 60} minutes, then updates the source priorities and saves a report. "
                "It holds one search slot meanwhile. Read it with /testsearches report.",
            )
            return

        if arg.startswith("report"):
            rest = arg[len("report"):].strip()
            rows = _load_search_test_reports(report_id=int(rest)) if rest.isdigit() else _load_search_test_reports(limit=1)
            if not rows:
                safe_send_message(chat_id, "No test report found. Turn test mode on with /testsearches on and run a search first.")
                return
            row = rows[0]
            _send_in_chunks(chat_id, _format_test_report(row["id"], row["search_id"], row["data"]))
            return

        if arg in ("on", "enable") or (arg and _parse_test_duration(arg)):
            seconds = SEARCH_TEST_DEFAULT_SECONDS if arg in ("on", "enable") else _parse_test_duration(arg)
            if not (SEARCH_TIMEOUT < seconds <= SEARCH_TEST_MAX_SECONDS):
                safe_send_message(
                    chat_id,
                    f"Choose a time longer than {SEARCH_TIMEOUT} seconds and at most {SEARCH_TEST_MAX_SECONDS // 60} minutes, "
                    "for example /testsearches 2 minutes",
                )
                return
            if not _search_test_set(seconds):
                safe_send_message(chat_id, "❌ Database error. Test mode was not changed.")
                return
            safe_send_message(
                chat_id,
                f"{_test_status_text()}\n\nEach search holds its slot for the full time, so use this for short tests and "
                "lower the time once you know the slowest answer. Read the results with /testsearches report.",
            )
            return

        if arg:
            safe_send_message(chat_id, "Usage: /testsearches on | off | <time, e.g. 2 minutes> | report [number] | probe")
            return

        lines = [_test_status_text(), ""]
        rows = _load_search_test_reports(limit=10)
        if rows:
            lines.append("Latest test searches, newest first (open one with /testsearches report <number>):")
            for row in rows:
                ok, late, slowest, deadline = _report_stats(row["data"])
                slow_text = f"{slowest['end']}s" if slowest else "none"
                tag = "  [probe]" if (row["data"] or {}).get("probe") else ""
                lines.append(f"#{row['id']}  {row['query']}  -  {len(late)} answer(s) after {deadline:.0f}s, slowest {slow_text}{tag}")
        else:
            lines.append("No test reports yet. Turn test mode on with /testsearches on, then run a search.")
        lines += [
            "",
            f"/testsearches on - searches run up to {SEARCH_TEST_DEFAULT_SECONDS // 60} minutes",
            f"/testsearches 2 minutes - choose the length (up to {SEARCH_TEST_MAX_SECONDS // 60} minutes)",
            "/testsearches off",
            "/testsearches report [number] - full report, default is the latest",
            f"/testsearches probe - search \"{SEARCH_PROBE_QUERY}\" for {SEARCH_TEST_MAX_SECONDS // 60} minutes now and update source priorities "
            "(only a probe changes priorities; it also runs by itself every 24 hours)",
        ]
        _send_in_chunks(chat_id, "\n".join(lines))

    @bot.message_handler(commands=['deadsources'])
    def deadsources(message):
        # Admin only. /deadsources lists dropped / at-risk sources.
        # /deadsources reset [text] clears them (all, or URLs containing text).
        if message.from_user.id not in ADMIN_USER_ID:
            bot.send_message(message.chat.id, "❌ Access Denied.")
            return
        parts = (message.text or "").split(maxsplit=2)
        conn = get_db_connection()
        if not conn:
            send_temp_message(message.chat.id, "⚠️ Cannot connect to DB", message.from_user.id)
            return
        try:
            with conn.cursor() as cur:
                if len(parts) >= 2 and parts[1].lower() == "reset":
                    pattern = f"%{parts[2].strip()}%" if len(parts) > 2 else "%"
                    cur.execute("""
                        UPDATE search_source_health
                        SET dns_failures = 0, dropped = FALSE, dropped_at = NULL
                        WHERE url ILIKE %s
                    """, (pattern,))
                    changed = cur.rowcount
                    conn.commit()
                    _dropped_cache["at"] = 0.0
                    send_temp_message(message.chat.id, f"✅ Reset {changed} source(s).", message.from_user.id)
                    return
                cur.execute("""
                    SELECT url, dns_failures, dropped, dropped_at, last_error
                    FROM search_source_health
                    WHERE dns_failures > 0
                    ORDER BY dropped DESC, dns_failures DESC, url
                """)
                rows = cur.fetchall()
            if not rows:
                send_temp_message(message.chat.id, "ℹ️ No failing sources recorded.", message.from_user.id)
                return
            dropped = [r for r in rows if r['dropped']]
            risk = [r for r in rows if not r['dropped']]
            text = f"🚫 Dropped sources ({len(dropped)}) - retried after {SOURCE_RETRY_DAYS} days:\n"
            for r in dropped:
                text += f"{r['url']} - {r['dns_failures']} DNS failures, dropped {r['dropped_at']}\n"
            text += f"\n⚠️ At risk ({len(risk)}) - dropped after {SOURCE_DROP_AFTER} in a row:\n"
            for r in risk:
                text += f"{r['url']} - {r['dns_failures']}/{SOURCE_DROP_AFTER}\n"
            send_temp_long_message(message.chat.id, text, message.from_user.id)
        except Exception as e:
            conn.rollback()
            send_temp_message(message.chat.id, f"⚠️ Failed: {e}", message.from_user.id)
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

def _touch_progress(task_id, snapshot=None):
    """
    Heartbeat + optional full progress snapshot for cross-worker /progress.
    snapshot keys: done, total, pct, status, elapsed, remaining, speed
    """
    conn = get_db_connection()
    if not conn:
        return
    try:
        with conn.cursor() as cur:
            if snapshot:
                cur.execute(
                    """
                    UPDATE tasks SET
                        last_progress_at=NOW(),
                        progress_done=%s,
                        progress_total=%s,
                        progress_pct=%s,
                        progress_status=%s,
                        progress_elapsed=%s,
                        progress_remaining=%s,
                        progress_speed=%s
                    WHERE id=%s
                    """,
                    (
                        int(snapshot.get("done") or 0),
                        int(snapshot.get("total") or 0) or None,
                        float(snapshot.get("pct") or 0),
                        (snapshot.get("status") or "")[:200],
                        float(snapshot.get("elapsed") or 0),
                        float(snapshot.get("remaining") or 0),
                        float(snapshot.get("speed") or 0),
                        task_id,
                    ),
                )
            else:
                cur.execute(
                    "UPDATE tasks SET last_progress_at=NOW() WHERE id=%s",
                    (task_id,),
                )
        conn.commit()
    except Exception as e:
        print(f"[TOUCH PROGRESS ERROR] task={task_id}: {e}")
    finally:
        conn.close()


_task_loop_lock = threading.Lock()
_task_loop_generation = 0


def _start_task_loop():
    """
    Start a worker loop thread and retire any earlier one. An old loop thread
    that is stuck inside crawl_task() exits by itself if that call ever returns.
    """
    global _task_loop_generation
    with _task_loop_lock:
        _task_loop_generation += 1
        generation = _task_loop_generation
    if not WORKER_ENABLED:
        print(f"[V3 DISABLED] {SERVER_ID} runs v3 - task worker not started")
        return generation
    threading.Thread(
        target=process_tasks_loop,
        args=(generation,),
        daemon=True,
        name=f"task-loop-{generation}",
    ).start()
    return generation


def process_tasks_loop(generation):
    while True:
        if generation != _task_loop_generation:
            print(f"[WORKER LOOP] generation {generation} retired (replaced by {_task_loop_generation})")
            return
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
                                      AND user_tasks.status IN ('done', 'running', 'upload_failed')
                                      AND user_tasks.created_at >= NOW() - INTERVAL '1 day'
                                ) < 2
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

                # Mark task as running and assign this server.
                # Always stamp last_progress_at so the stuck-reaper does not
                # treat a brand-new claim (NULL / stale timestamp) as stalled.
                cur.execute(
                    """
                    UPDATE tasks
                    SET status='running',
                        server_id=%s,
                        run_id=%s,
                        last_progress_at=NOW()
                    WHERE id=%s
                    """,
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


def _count_user_running_tasks(user_id: int) -> int:
    """How many tasks this user currently has in status=running (DB)."""
    conn = get_db_connection()
    if not conn:
        return 0
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM tasks WHERE user_id=%s AND status='running'",
                (user_id,),
            )
            row = cur.fetchone()
            return int(row["c"] if isinstance(row, dict) else row[0]) if row else 0
    except Exception:
        return 0
    finally:
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
    novel_title = task.get("novel_title") or ""

    print(
        f"[CRAWL START] server={SERVER_ID} task={task_id} "
        f"user={user_id} title={novel_title!r} range={chapter_range!r} "
        f"url={url} crawler={task.get('crawler_version')}"
    )

    temp_folder = os.path.join("./novels", f"{user_id}_{task_id}_{uuid.uuid4().hex[:8]}")
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
    # Mutable so background scan / on_total_known can fill title later
    title_holder = {"title": novel_title}

    def title_line():
        t = title_holder.get("title") or ""
        return f"📖 {t}\n\n" if t else ""

    def on_total_known(total):
        range_desc_holder["desc"] = format_range_desc(chapter_range, total)
        # Also persist total_chapters + refresh title from DB if scan finished later
        try:
            conn = get_db_connection()
            if conn:
                with conn.cursor(cursor_factory=RealDictCursor) as cur:
                    cur.execute(
                        "UPDATE tasks SET total_chapters=%s, last_progress_at=NOW() WHERE id=%s RETURNING novel_title",
                        (total, task_id),
                    )
                    row = cur.fetchone()
                    conn.commit()
                    if row and row.get("novel_title") and not title_holder["title"]:
                        title_holder["title"] = row["novel_title"]
                conn.close()
        except Exception as e:
            print(f"[ON_TOTAL_KNOWN] task={task_id}: {e}")

    novel_id_holder = {"id": None}

    def on_novel_id(nid):
        """Remember the lncrawl cache id and persist it right away."""
        novel_id_holder["id"] = nid
        conn = get_db_connection()
        if not conn:
            return
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE tasks SET cache_novel_id=%s WHERE id=%s AND status!='cancelled'",
                    (nid, task_id),
                )
            conn.commit()
        except Exception as e:
            print(f"[CACHE ID SAVE ERROR] task={task_id}: {e}")
        finally:
            conn.close()

    # Decide whether this user gets live progress or a static notice
    user_running = _count_user_running_tasks(user_id)
    live_progress = user_running <= MAX_LIVE_PROGRESS_PER_USER

    if live_progress:
        progress_msg = send_task_message_24h(
            chat_id,
            f"{title_line()}📥 Downloading Task {task_id}...\n\n📊 Progress: 0.0%",
            user_id,
        )
    else:
        progress_msg = send_task_message_24h(
            chat_id,
            (
                f"{title_line()}"
                f"✅ Task {task_id} has started downloading.\n\n"
                "You have more than one running task, so live progress updates "
                "are paused for you to avoid Telegram rate limits.\n\n"
                "Use /progress to view a snapshot of all your running tasks."
            ),
            user_id,
        )

    cancel_event = threading.Event()
    crawl_started_at = time.time()
    # Store lightweight progress snapshot for /progress (local process)
    progress_snapshot = {
        "done": 0,
        "total": 0,
        "pct": 0.0,
        "status": "starting",
        "elapsed": 0.0,
        "remaining": 0.0,
        "speed": 0.0,
        "range_desc": range_desc_holder["desc"],
        "title": title_holder["title"],
        "started_at": crawl_started_at,
    }
    with running_lock:
        running_processes[task_id] = cancel_event
        progress_snapshots[task_id] = progress_snapshot

    stop_watcher = threading.Event()
    hard_limit_hit = {"flag": False}

    def _watch_cancel_and_limits():
        while not stop_watcher.is_set() and not cancel_event.is_set():
            # Hard wall-clock limit for this attempt
            if time.time() - crawl_started_at >= HARD_TASK_LIMIT_HOURS * 3600:
                hard_limit_hit["flag"] = True
                print(
                    f"[HARD LIMIT] task={task_id} exceeded "
                    f"{HARD_TASK_LIMIT_HOURS}h — cancelling (cache kept)"
                )
                cancel_event.set()
                return

            conn = get_db_connection()
            if conn:
                try:
                    with conn.cursor(cursor_factory=RealDictCursor) as cur:
                        cur.execute(
                            "SELECT status, novel_title, total_chapters FROM tasks WHERE id=%s",
                            (task_id,),
                        )
                        row = cur.fetchone()
                        if row and row["status"] == "cancelled":
                            cancel_event.set()
                            return
                        # Background scan may fill title after user skipped scan UI
                        if row and row.get("novel_title") and not title_holder["title"]:
                            title_holder["title"] = row["novel_title"]
                            progress_snapshot["title"] = row["novel_title"]
                            if live_progress and progress_msg:
                                try:
                                    bot.edit_message_text(
                                        (
                                            f"📖 {row['novel_title']}\n\n"
                                            f"📥 Downloading Task {task_id}...\n\n"
                                            f"📊 Progress: {progress_snapshot.get('pct', 0):.1f}%\n"
                                            f"📌 Status: {progress_snapshot.get('status', 'running')}"
                                        ),
                                        chat_id,
                                        progress_msg.message_id,
                                    )
                                except Exception:
                                    pass
                        if row and row.get("total_chapters") and not range_desc_holder.get("desc"):
                            range_desc_holder["desc"] = format_range_desc(
                                chapter_range, row["total_chapters"]
                            )
                finally:
                    conn.close()
            time.sleep(10)

    threading.Thread(target=_watch_cancel_and_limits, daemon=True).start()

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
        elapsed = max(0.0, now - crawl_started_at)
        speed = elapsed / done if done else 0.0
        remaining = speed * max(0, total - done) if speed else 0.0

        # Always keep the in-memory snapshot fresh for local /progress
        progress_snapshot.update({
            "done": done,
            "total": total,
            "pct": pct,
            "status": f"Downloading chapters ({done}/{total})",
            "elapsed": elapsed,
            "remaining": remaining,
            "speed": speed,
            "range_desc": range_desc_holder["desc"],
            "title": title_holder["title"],
        })

        # Always touch DB so the reaper does not kill a healthy long crawl,
        # and so /progress on s4 can see workers s2–s4.
        if now - last_edit["t"] >= PROGRESS_UPDATE_INTERVAL or done == total:
            last_edit["t"] = now
            _touch_progress(task_id, progress_snapshot)

            if not live_progress or not progress_msg:
                return

            text = (
                f"{title_line()}"
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
            except telebot.apihelper.ApiTelegramException as e:
                if getattr(e, "error_code", None) == 429:
                    retry_after = e.result_json.get("parameters", {}).get("retry_after", 10)
                    print(f"[PROGRESS 429] task={task_id} chat={chat_id} retry_after={retry_after}s")
                # swallow — do not block the crawl
            except Exception:
                pass

    def on_image_progress(done, total):
        pct = (done / total * 100) if total else 0
        now = time.time()
        progress_snapshot.update({
            "done": done,
            "total": total,
            "pct": pct,
            "status": f"Fetching embedded images ({done}/{total})",
            "title": title_holder["title"],
        })
        if now - last_edit["t"] >= PROGRESS_UPDATE_INTERVAL or done == total:
            last_edit["t"] = now
            _touch_progress(task_id, progress_snapshot)
            if not live_progress or not progress_msg:
                return
            text = (
                f"{title_line()}"
                "🖼 Downloading images...\n\n"
                f"📊 Progress: {pct:.1f}%\n"
                f"📌 Status: Fetching embedded images ({done}/{total})"
            )
            try:
                bot.edit_message_text(text, chat_id, progress_msg.message_id)
            except Exception:
                pass

    output_format = normalize_output_format(task.get("output_format"))
    convert_done = threading.Event()

    # State of the "EPUB first" package sent to the user while Calibre converts.
    early = {
        "msg_ids": [],          # message ids to copy into the groups later
        "cover_done": False,    # cover + synopsis already delivered to the user
        "thread": None,
    }

    def on_epub_ready(info):
        """Send cover, synopsis and the EPUB to the user in the background, so they
        have something to read while the (slow) conversion runs."""
        try:
            root, _ext = split_artifact_name(os.path.basename(info["epub_path"]))
            suffix = range_suffix_for_filename(chapter_range, info["chapter_count"])
            display = f"{root} {suffix}" if suffix else root
            copy_path = os.path.join(temp_folder, f"{display}.epub")
            # Copy, never rename: Calibre still needs the original EPUB.
            shutil.copyfile(info["epub_path"], copy_path)
        except Exception as e:
            print(f"[EARLY EPUB] task={task_id} could not prepare file: {e}")
            return

        def _send():
            try:
                cover_msg = send_cover_post(
                    chat_id, info["cover_source"], display, url, task["crawler_version"]
                )
                if cover_msg:
                    early["msg_ids"].append(cover_msg.message_id)
                for synopsis_message in build_synopsis_messages(info["synopsis"]):
                    syn_msg = send_message_with_retry(
                        chat_id, synopsis_message, parse_mode="HTML"
                    )
                    if syn_msg:
                        early["msg_ids"].append(syn_msg.message_id)
                early["cover_done"] = bool(early["msg_ids"])

                caption = display.strip()
                if len(caption) > 1024:
                    caption = caption[:1021] + "..."
                size = os.path.getsize(copy_path)
                with _local_api_lock:
                    using_local = _local_api_available and bool(LOCAL_BOT_API_URL)
                if not using_local and size > 48 * 1024 * 1024:
                    print(f"[EARLY EPUB] task={task_id} skipped: {size} bytes > 48 MB and no local API")
                    return
                print(f"[EARLY EPUB SEND] task={task_id} caption={caption!r}")
                sent = send_document_with_retry(
                    chat_id=chat_id, file_path=copy_path, caption=caption, max_retries=1
                )
                if sent:
                    early["msg_ids"].append(sent.message_id)
                    print(f"[EARLY EPUB SENT] task={task_id} message_id={sent.message_id}")
                else:
                    print(f"[EARLY EPUB FAILED] task={task_id}")
            except Exception as e:
                print(f"[EARLY EPUB ERROR] task={task_id}: {type(e).__name__}: {e}")
            finally:
                try:
                    os.remove(copy_path)
                except OSError:
                    pass

        t = threading.Thread(target=_send, daemon=True)
        early["thread"] = t
        t.start()

    # Conversion in parts (big books): every finished part is sent to the user right
    # away by a background sender, so nothing waits for the last part.
    parts_state = {
        "active": False,
        "total": 0,
        "sent": 0,
        "failed": 0,
        "msg_ids": [],   # message ids of delivered parts (copied into the groups later)
        "unsent": [],    # [(path, caption)] parts Telegram refused: retry + direct group upload
        "queue": queue.Queue(),
        "thread": None,
    }
    convert_started = {"v": False}
    convert_note = {"text": ""}

    def _part_caption(res):
        caption = split_artifact_name(res["name"])[0].strip()
        if res.get("skipped"):
            caption += (
                "\n⚠️ Not rendered here (the text is in the EPUB): chapter "
                + ", ".join(str(n) for n in res["skipped"][:15])
            )
        if res.get("cleaned") or res.get("emoji_removed"):
            caption += "\nℹ️ Emoji and symbols were removed from this file so it could be built."
        return caption[:1021] + "..." if len(caption) > 1024 else caption

    def _deliver_part(res):
        if res.get("error"):
            parts_state["failed"] += 1
            err = str(res["error"]).strip().splitlines()[-1][:300] if str(res["error"]).strip() else "unknown error"
            try:
                send_task_message_24h(
                    chat_id,
                    (
                        f"⚠️ Part {res['index']} of {res['total']} (chapters {res['first']}-{res['last']}) "
                        f"could not be converted to {output_format.upper()}: {err}\n\n"
                        "The other parts continue. Those chapters are in the EPUB."
                    ),
                    user_id,
                )
            except Exception as e:
                print(f"[PART FAIL NOTIFY] task={task_id}: {e}")
            return
        file_path = res["path"]
        caption = _part_caption(res)
        size = os.path.getsize(file_path) if os.path.isfile(file_path) else 0
        with _local_api_lock:
            using_local = _local_api_available and bool(LOCAL_BOT_API_URL)
        if not using_local and size > 48 * 1024 * 1024:
            print(f"[PART SEND] task={task_id} part {res['index']} is {size} bytes > 48 MB and no local API")
            sent = None
        else:
            print(f"[PART SEND] task={task_id} part {res['index']}/{res['total']} caption={caption!r}")
            sent = send_document_with_retry(
                chat_id=chat_id, file_path=file_path, caption=caption, max_retries=1
            )
        if sent:
            parts_state["msg_ids"].append(sent.message_id)
            parts_state["sent"] += 1
        else:
            parts_state["unsent"].append((file_path, caption))

    def _parts_sender():
        # Keep the chat order right: cover, synopsis and the EPUB go first.
        early_thread = early["thread"]
        if early_thread is not None:
            early_thread.join(timeout=900)
        while True:
            res = parts_state["queue"].get()
            if res is None:
                return
            try:
                _deliver_part(res)
            except Exception as e:
                print(f"[PART DELIVER ERROR] task={task_id}: {type(e).__name__}: {e}")
                if res.get("path") and os.path.isfile(res["path"]):
                    parts_state["unsent"].append((res["path"], _part_caption(res)))

    def on_part_ready(res):
        """Called by Calibre as each part finishes (or fails)."""
        parts_state["active"] = True
        parts_state["total"] = res.get("total", 0)
        if res.get("path"):
            # Move it out of the novel cache now: that folder is wiped when the conversion ends.
            try:
                dest = os.path.join(temp_folder, os.path.basename(res["path"]))
                shutil.move(res["path"], dest)
                res["path"] = dest
            except Exception as e:
                print(f"[PART MOVE] task={task_id}: {e}")
        if parts_state["thread"] is None:
            t = threading.Thread(target=_parts_sender, daemon=True)
            parts_state["thread"] = t
            t.start()
        parts_state["queue"].put(res)

    def on_convert_note(report):
        """A non-split conversion had to clean or skip stuck chapters."""
        lines = []
        if report.get("placeholder"):
            lines.append(
                "⚠️ Chapter(s) that could not be rendered in this file (the text is in the EPUB): "
                + ", ".join(str(n) for n in report["placeholder"][:20])
            )
        if report.get("cleaned") or report.get("emoji_removed"):
            lines.append("ℹ️ Emoji and symbols were removed from this file so it could be built.")
        convert_note["text"] = "\n".join(lines)

    convert_last_edit = {"t": 0.0}
    # The progress message sits above the early EPUB in the chat. Once the EPUB is
    # delivered, the progress message is moved below it (old one deleted, new one sent).
    convert_msg = {"id": None, "moved": False}

    def on_convert_progress(info):
        """Live conversion progress from Calibre (percent, elapsed, rough ETA)."""
        pct = float(info.get("pct") or 0)
        stage = info.get("stage") or "working"
        elapsed = float(info.get("elapsed") or 0)
        eta = info.get("eta")
        _sd, _st = int(info.get("sub_done") or 0), int(info.get("sub_total") or 0)
        parts_n = int(info.get("parts") or 1)
        part_i = int(info.get("part") or 1)
        part_txt = f" (part {part_i}/{parts_n})" if parts_n > 1 else ""
        overall_eta = info.get("overall_eta")
        progress_snapshot.update({
            "done": _sd,
            "total": _st,
            "pct": (_sd / _st * 100) if _st else pct,
            "status": (
                f"Converting to {output_format.upper()}{part_txt}: rendering {_sd}/{_st}"
                if _st else f"Converting to {output_format.upper()}{part_txt} ({stage})"
            ),
            "elapsed": elapsed,
            "remaining": float(overall_eta or eta or 0),
            "speed": 0.0,
            "title": title_holder["title"],
        })
        now = time.time()
        early_thread = early["thread"]
        need_move = (
            live_progress and progress_msg is not None
            and not convert_msg["moved"]
            and early_thread is not None and not early_thread.is_alive()
            and bool(early["msg_ids"])
        )
        if not need_move and now - convert_last_edit["t"] < PROGRESS_UPDATE_INTERVAL:
            return
        convert_last_edit["t"] = now
        _touch_progress(task_id, progress_snapshot)

        if not live_progress or not progress_msg:
            return
        sub_done = int(info.get("sub_done") or 0)
        sub_total = int(info.get("sub_total") or 0)
        finishing = bool(sub_total) and pct >= 90
        if finishing:
            progress_lines = (
                f"📊 Rendering pages: done ({sub_total} chapters)\n"
                f"📌 Finishing the PDF: {pct:.0f}% - {stage}\n\n"
            )
        elif sub_total:
            render_pct = sub_done / sub_total * 100
            progress_lines = (
                f"📊 Rendering pages: {render_pct:.0f}% ({sub_done}/{sub_total} chapters)\n"
                f"📌 Calibre stage: {pct:.0f}% - {stage}\n\n"
            )
        else:
            progress_lines = (
                f"📊 Progress: {pct:.0f}%\n"
                f"📌 Status: {stage}\n\n"
            )
        if parts_n > 1 and overall_eta:
            remaining_line = f"⌛ Remaining (all parts): ~{format_progress_duration(overall_eta)} (rough estimate)"
        elif parts_n > 1:
            remaining_line = "⌛ Remaining: estimating (shown after the first part is done)..."
        elif eta and finishing:
            remaining_line = f"⌛ Remaining: ~{format_progress_duration(eta)} (rough estimate)"
        elif eta:
            remaining_line = (
                f"⌛ Remaining: ~{format_progress_duration(eta)} "
                "(rendering, plus a few minutes of finishing)"
            )
        elif finishing:
            remaining_line = "⌛ Remaining: almost done..."
        else:
            remaining_line = "⌛ Remaining: estimating..."
        if parts_n > 1:
            header = (
                f"🔄 Converting EPUB to {output_format.upper()} - part {part_i} of {parts_n} "
                f"(chapters {info.get('first')}-{info.get('last')})...\n"
                f"📦 Parts sent so far: {parts_state['sent']}/{parts_n}\n\n"
            )
        else:
            header = f"🔄 Converting EPUB to {output_format.upper()}...\n\n"
        stuck = info.get("skipped") or info.get("cleaned") or []
        if stuck or int(info.get("attempt") or 1) > 1:
            progress_lines += (
                "⚠️ A chapter got stuck, retrying without it"
                + (f" (chapter {', '.join(str(n) for n in sorted(set(stuck))[:6])})" if stuck else "")
                + "\n\n"
            )
        text = (
            f"{title_line()}"
            f"{header}"
            f"{progress_lines}"
            f"⏱ Elapsed: {format_progress_duration(elapsed)}\n"
            f"{remaining_line}\n\n"
            + (
                "📚 Your EPUB was already sent above, you can read it while you wait."
                if early["msg_ids"] else
                "This can take a long time for large novels."
            )
        )
        if need_move:
            # Post a fresh progress message below the EPUB, then drop the old one above it.
            new_msg = None
            try:
                new_msg = send_task_message_24h(chat_id, text, user_id)
            except Exception as e:
                print(f"[CONVERT PROGRESS MOVE] task={task_id}: {e}")
            convert_msg["moved"] = True  # only try once
            if new_msg:
                convert_msg["id"] = new_msg.message_id
                cleanup_bot_message(user_id, chat_id, progress_msg.message_id)
                return
        target_id = convert_msg["id"] or progress_msg.message_id
        try:
            bot.edit_message_text(text, chat_id, target_id)
        except telebot.apihelper.ApiTelegramException as e:
            if getattr(e, "error_code", None) == 429:
                retry_after = e.result_json.get("parameters", {}).get("retry_after", 10)
                print(f"[CONVERT PROGRESS 429] task={task_id} chat={chat_id} retry_after={retry_after}s")
        except Exception:
            pass

    def on_convert_start(fmt):
        """Tell the user the EPUB is being converted and keep the task from looking stuck."""
        convert_started["v"] = True
        # Calibre reports progress through this listener (keyed by the cancel event,
        # which is the `signal` handed to the binder).
        try:
            from lncrawl.services.binder import calibre as _calibre_mod
            _calibre_mod.progress_listeners[cancel_event] = on_convert_progress
        except Exception as e:
            print(f"[CONVERT LISTENER] task={task_id}: {e}")

        text = (
            f"{title_line()}"
            f"🔄 Converting EPUB to {fmt.upper()}...\n\n"
            "📊 Progress: starting...\n"
            "This can take a while for large novels."
        )
        edited = False
        if progress_msg is not None:
            try:
                bot.edit_message_text(text, chat_id, progress_msg.message_id)
                edited = True
            except Exception:
                pass
        if not edited:
            send_task_message(chat_id, text, user_id)

        def _keepalive():
            while not convert_done.wait(60):
                _touch_progress(task_id)
        threading.Thread(target=_keepalive, daemon=True).start()

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
                on_novel_id=on_novel_id,
                output_format=output_format,
                on_convert_start=on_convert_start,
                on_epub_ready=on_epub_ready,
                on_part_ready=on_part_ready,
                on_convert_note=on_convert_note,
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
        convert_done.set()
        if parts_state["thread"] is not None:
            parts_state["queue"].put(None)  # let the sender finish what it has and stop
        with running_lock:
            if running_processes.get(task_id) is cancel_event:
                running_processes.pop(task_id, None)
                progress_snapshots.pop(task_id, None)

        if progress_msg is not None:
            cleanup_bot_message(user_id, chat_id, progress_msg.message_id)
        if convert_msg["id"]:
            cleanup_bot_message(user_id, chat_id, convert_msg["id"])

        if str(e) == "Cancelled":
            # Hard wall-clock limit: keep v4 chapter cache, mark failed with a
            # retryable error so /retry (anyone) can resume from saved chapters.
            if hard_limit_hit.get("flag"):
                # /retry only exists for v4 (v3 keeps no resumable cache).
                if task["crawler_version"] != "v4":
                    conn = get_db_connection()
                    if conn:
                        try:
                            with conn.cursor() as cur:
                                cur.execute(
                                    """
                                    UPDATE tasks
                                    SET status='failed', completed_at=NOW(), error=%s,
                                        retry_count=GREATEST(retry_count, 2)
                                    WHERE id=%s AND status!='cancelled'
                                    """,
                                    (f"Hard limit of {HARD_TASK_LIMIT_HOURS}h reached", task_id),
                                )
                            conn.commit()
                        finally:
                            conn.close()
                    try:
                        send_task_message_24h(
                            chat_id,
                            (
                                f"⏰ Task {task_id} hit the {HARD_TASK_LIMIT_HOURS}-hour "
                                "download limit and was stopped.\n\n"
                                f"🔗 {url}\n\n"
                                "This crawler version cannot resume a stopped download. "
                                "Send /download to start it again, ideally with a smaller chapter range."
                            ),
                            user_id,
                        )
                    except Exception as notify_err:
                        print(f"[HARD LIMIT NOTIFY] {notify_err}")
                    shutil.rmtree(temp_folder, ignore_errors=True)
                    return

                # v4: treat like a partial/missing-chapters completion so /retry
                # pins to this same server (chapter cache lives here).
                conn = get_db_connection()
                if conn:
                    try:
                        with conn.cursor() as cur:
                            cur.execute(
                                """
                                UPDATE tasks
                                SET status='done',
                                    completed_at=NOW(),
                                    error=%s,
                                    server_id=%s,
                                    is_user_retry=FALSE,
                                    cache_novel_id=COALESCE(%s, cache_novel_id),
                                    retry_expires_at=COALESCE(
                                        retry_expires_at,
                                        NOW() + (%s * INTERVAL '1 hour')
                                    ),
                                    missing_chapters=GREATEST(COALESCE(missing_chapters, 0), 1)
                                WHERE id=%s AND status!='cancelled'
                                """,
                                (
                                    f"Hard limit of {HARD_TASK_LIMIT_HOURS}h reached; "
                                    "chapters already downloaded are kept for /retry",
                                    SERVER_ID,
                                    novel_id_holder["id"],
                                    USER_RETRY_WINDOW_HOURS,
                                    task_id,
                                ),
                            )
                        conn.commit()
                    finally:
                        conn.close()
                try:
                    hard_msg = send_task_message_24h(
                        chat_id,
                        (
                            f"⏰ Task {task_id} hit the {HARD_TASK_LIMIT_HOURS}-hour "
                            "download limit and was stopped.\n\n"
                            f"🆔 Task ID: {task_id}\n"
                            f"🔗 URL: {url}\n"
                            f"🖥 Worker: {SERVER_ID}\n\n"
                            "Chapters already fetched stay on this worker. "
                            "Send /retry (or /retry <task_id> / /retry <url>) "
                            f"within {USER_RETRY_WINDOW_HOURS} hours to continue "
                            "from the saved chapters on the same server. "
                            "Anyone may trigger the retry."
                        ),
                        user_id,
                        is_final=True,
                    )
                    if hard_msg:
                        # Keep it for the whole retry window, then remove it.
                        threading.Thread(
                            target=delete_message_later,
                            args=(chat_id, hard_msg.message_id, USER_RETRY_WINDOW_HOURS * 3600),
                            daemon=True,
                        ).start()
                except Exception as notify_err:
                    print(f"[HARD LIMIT NOTIFY] {notify_err}")
                # Do NOT wipe lncrawl novel cache; only the task temp folder.
                shutil.rmtree(temp_folder, ignore_errors=True)
                return

            # User /cancel — status already set by cancel handler.
            shutil.rmtree(temp_folder, ignore_errors=True)
            return

        print(f"[CRAWL ERROR] Task {task_id} ({url}): {type(e).__name__}: {e}")
        traceback.print_exc()

        try:
            err_text = f"{type(e).__name__}: {e}"
            if len(err_text) > 700:
                err_text = "..." + err_text[-700:]
            if convert_started["v"]:
                # The chapters were downloaded fine; only the format conversion failed.
                already = []
                if early["msg_ids"]:
                    already.append("📚 Your EPUB was already sent above.")
                if parts_state["sent"]:
                    already.append(f"📦 {parts_state['sent']} part(s) were already sent.")
                send_task_message(
                    chat_id,
                    f"⚠️ Conversion to {output_format.upper()} failed: {err_text}\n\n"
                    f"🔗 {url}\n\n"
                    + ("\n".join(already) + "\n" if already else "")
                    + "Try again later, or choose EPUB.",
                    user_id
                )
            else:
                send_task_message(
                    chat_id,
                    f"⚠️ Download failed: {err_text}\n\n"
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
    convert_done.set()
    if progress_msg is not None:
        cleanup_bot_message(user_id, chat_id, progress_msg.message_id)
    if convert_msg["id"]:
        cleanup_bot_message(user_id, chat_id, convert_msg["id"])

    # Parts were sent while converting; wait for the last uploads to finish.
    if parts_state["thread"] is not None:
        parts_state["queue"].put(None)
        sender = parts_state["thread"]
        wait_until = time.time() + 3 * 3600
        while sender.is_alive() and time.time() < wait_until:
            sender.join(timeout=60)
            _touch_progress(task_id)

    epub_folder = os.path.dirname(epub_path)
    original_name = os.path.basename(epub_path)
    parts_display = ""

    if parts_state["active"]:
        # Every part was already delivered (or queued as unsent); nothing to rename.
        epub_folder = temp_folder
        name_root, _ext = split_artifact_name(original_name)
        suffix = range_suffix_for_filename(chapter_range, actual_total_chapters)
        parts_display = f"{name_root} {suffix}" if suffix else name_root
        epub_files = []
    elif task['crawler_version'] == "v4":
        name_root, ext = split_artifact_name(original_name)
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
            (
                (
                    f"✅ All {parts_state['total']} parts are done!\n"
                    if parts_state["active"] else
                    "✅ Download completed!\n"
                    "You will receive your file soon...\n"
                )
                + "Backup file will be sent to "
                "@patiorjunrick1\n"
            ),
            user_id
        )

        groups = [MY_GROUP_ID, OTHER_GROUP_ID, BOREAL_PRIVATE_GROUP_ID, BOREAL_OTHER_GROUP]
        delay = 5

        # Wait for the early EPUB package (cover, synopsis, EPUB) if one is still
        # uploading, so message order and ids are settled before the final file.
        if early["thread"] is not None:
            early["thread"].join(timeout=900)
        message_ids_to_forward = list(early["msg_ids"]) + list(parts_state["msg_ids"])
        # Paths that failed user delivery — groups get a direct upload later.
        direct_group_epubs = []  # list of (file_path, caption)

        # Message 1: cover image and compact information caption.
        # The filename without ".epub" includes the requested range, e.g. c1-500.
        display_title = (
            parts_display if parts_state["active"]
            else split_artifact_name(epub_files[0])[0]
        )

        # Skipped when the cover + synopsis already went out with the early EPUB.
        if not early["cover_done"]:
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
            epub_caption = split_artifact_name(file_name)[0].strip()

            # Telegram document captions have a 1,024-character limit.
            if len(epub_caption) > 1024:
                epub_caption = epub_caption[:1021] + "..."

            print(f"[EPUB SEND] caption={epub_caption!r}")

            file_size = os.path.getsize(file_path) if os.path.isfile(file_path) else 0
            with _local_api_lock:
                using_local = _local_api_available and bool(LOCAL_BOT_API_URL)

            if not using_local and file_size > 48 * 1024 * 1024:
                print(
                    f"[EPUB SEND] Skipping Telegram cloud (file {file_size} bytes > 48 MB, "
                    f"local API unavailable). Going straight to external hosts."
                )
                user_msg = None
            else:
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
                # User chat is rate-limited / send failed → external fallback
                # + mark upload_failed + direct-upload the EPUB to groups.
                queued = queue_epub_upload_retry(task, file_path, epub_caption)
                direct_group_epubs.append((file_path, epub_caption))

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

        # Parts Telegram refused for the user: external links + direct group upload.
        if parts_state["unsent"]:
            for part_path, part_caption in parts_state["unsent"]:
                queue_epub_upload_retry(task, part_path, part_caption)  # one retry slot per task
                direct_group_epubs.append((part_path, part_caption))
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

        # Copy whatever we successfully sent to the user into groups.
        if message_ids_to_forward:
            forward_messages_to_groups_sequentially(
                chat_id,
                message_ids_to_forward,
                groups,
                delay,
            )

        # If the user upload fails, send the complete package directly to
        # every configured group. Do not depend on forwarding messages from
        # the user's chat because that chat may be the failing destination.
        if direct_group_epubs:
            def _direct_group_upload():
                for group_id in groups:
                    try:
                        if not parts_state["active"]:
                            group_cover = send_cover_post(
                                group_id,
                                cover_source,
                                display_title,
                                url,
                                task["crawler_version"],
                            )
                            if not group_cover:
                                print(f"[GROUP COVER FAILED] task={task['id']} group={group_id}")

                            for synopsis_message in build_synopsis_messages(novel_synopsis):
                                if not send_message_with_retry(
                                    group_id,
                                    synopsis_message,
                                    parse_mode="HTML",
                                    max_retries=1,
                                ):
                                    print(f"[GROUP SYNOPSIS FAILED] task={task['id']} group={group_id}")

                        for fpath, fcap in direct_group_epubs:
                            sent = send_document_with_retry(
                                chat_id=group_id,
                                file_path=fpath,
                                caption=fcap,
                                max_retries=1,
                            )
                            print(
                                f"[GROUP DIRECT EPUB] task={task['id']} "
                                f"group={group_id} {'ok' if sent else 'failed'}"
                            )
                    except Exception as err:
                        print(
                            f"[GROUP DIRECT PACKAGE ERROR] task={task['id']} "
                            f"group={group_id}: {err}"
                        )
                    time.sleep(delay)

            threading.Thread(target=_direct_group_upload, daemon=True).start()

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

        if convert_note["text"]:
            send_task_message_24h(chat_id, convert_note["text"], user_id, is_final=True)

        if missing_chapter_count:
            # Keep visible for the full 20-hour retry window, then auto-delete.
            msg = send_task_message_24h(
                chat_id,
                (
                    f"⚠️ This file was delivered with {missing_chapter_count} missing chapter(s).\n\n"
                    f"🆔 Task ID: {task_id}\n"
                    f"🔗 URL: {url}\n\n"
                    "Send /retry within 20 hours (or /retry <task_id> / /retry <url>) "
                    "to retry only the missing chapters. Anyone may retry this task."
                ),
                user_id,
                is_final=True,
            )
            if msg:
                # Override the default 24h delete with the retry-window length.
                threading.Thread(
                    target=delete_message_later,
                    args=(chat_id, msg.message_id, USER_RETRY_WINDOW_HOURS * 3600),
                    daemon=True,
                ).start()

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
            if running_processes.get(task_id) is cancel_event:
                running_processes.pop(task_id, None)
                progress_snapshots.pop(task_id, None)
        print(
            f"[CRAWL END] server={SERVER_ID} task={task_id} "
            f"user={user_id} title={title_holder.get('title')!r} url={url}"
        )
        shutil.rmtree(temp_folder, ignore_errors=True)
        # Best-effort cleanup of Firefox / geckodriver temp dirs left by lncrawl
        _cleanup_browser_temps()


def _cleanup_browser_temps():
    """Remove stale Firefox/geckodriver temp directories without touching active ones."""
    import glob
    import tempfile
    candidates = []
    try:
        tmp = tempfile.gettempdir()
        for pattern in (
            "rust_mozprofile*",
            "Temp-*",
            "firefox_*",
            "geckodriver*",
            "lncrawl-*",
        ):
            candidates.extend(glob.glob(os.path.join(tmp, pattern)))
        # Also clean under /tmp and /var/tmp common on containers
        for base in ("/tmp", "/var/tmp"):
            for pattern in ("rust_mozprofile*", "Temp-*", "firefox_*", "geckodriver*"):
                candidates.extend(glob.glob(os.path.join(base, pattern)))
    except Exception:
        return

    now = time.time()
    for path in candidates:
        try:
            # Only remove dirs/files older than 2 hours to avoid killing active profiles
            mtime = os.path.getmtime(path)
            if now - mtime < 2 * 3600:
                continue
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.remove(path)
        except Exception:
            pass


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
                  AND status IN ('done', 'upload_failed')
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
_start_task_loop()
def reap_orphaned_tasks():
    """
    Two cases:
    1. Classic orphan: task marked running but its server is dead / different run_id
       → mark failed so normal retry logic can pick it up.
    2. Local stuck: this process still holds the task in running_processes but
       last_progress_at is very old (library hung). Cancel the local event AND
       requeue the task as pending for a *different* server (clear server_id)
       so another worker can continue from cache.
    """
    while True:
        # --- local stuck detection (this process only) ---
        try:
            with running_lock:
                local_ids = list(running_processes.keys())
            if local_ids:
                conn = get_db_connection()
                if conn:
                    try:
                        with conn.cursor(cursor_factory=RealDictCursor) as cur:
                            cur.execute(
                                """
                                SELECT id, chat_id, user_id, last_progress_at
                                FROM tasks
                                WHERE id = ANY(%s)
                                  AND status = 'running'
                                  AND (
                                      last_progress_at IS NULL
                                      OR last_progress_at < NOW() - (%s * INTERVAL '1 minute')
                                  )
                                """,
                                (local_ids, STUCK_PROGRESS_MINUTES),
                            )
                            stuck = cur.fetchall()
                        for row in stuck:
                            tid = row["id"]
                            print(
                                f"[REAPER LOCAL] Requeueing stuck task {tid} "
                                f"(no progress for >{STUCK_PROGRESS_MINUTES} min) "
                                f"for another worker"
                            )
                            with running_lock:
                                ev = running_processes.get(tid)
                                if ev:
                                    ev.set()  # signal local crawl to stop
                                # Drop from busy set so this worker can claim
                                # *other* work even if the hung library thread
                                # never exits.
                                running_processes.pop(tid, None)
                                progress_snapshots.pop(tid, None)
                            # Keep server_id = this worker. The claim query
                            # excludes server_id = me for non-retry tasks, so
                            # another worker picks it up and this one cannot
                            # reclaim the same stuck task while the old thread
                            # may still be alive.
                            try:
                                with conn.cursor() as cur:
                                    cur.execute(
                                        """
                                        UPDATE tasks
                                        SET status='pending',
                                            run_id=NULL,
                                            completed_at=NULL,
                                            last_progress_at=NULL,
                                            is_user_retry=FALSE,
                                            retry_server_id=NULL,
                                            retry_count = retry_count + 1
                                        WHERE id=%s AND status='running'
                                        """,
                                        (tid,),
                                    )
                                conn.commit()
                            except Exception as e:
                                print(f"[REAPER LOCAL REQUEUE ERROR] {e}")
                            # The loop thread that ran this crawl may be hung inside
                            # crawl_task(). Start a replacement so this worker keeps
                            # claiming other tasks; the old one retires if it returns.
                            new_gen = _start_task_loop()
                            print(f"[REAPER LOCAL] replacement worker loop started (generation {new_gen})")
                            try:
                                send_task_message(
                                    row["chat_id"],
                                    (
                                        f"⚠️ Task {tid} stalled (no progress for "
                                        f"{STUCK_PROGRESS_MINUTES}+ minutes). "
                                        "It has been requeued for a different worker."
                                    ),
                                    row["user_id"],
                                )
                            except Exception:
                                pass
                    except Exception as e:
                        print(f"[REAPER LOCAL ERROR] {e}")
                    finally:
                        conn.close()
        except Exception as e:
            print(f"[REAPER LOCAL OUTER] {e}")

        # --- classic orphan reaping (any server) ---
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
        time.sleep(5)  # This affects how frequently s4 updates its own status in the servers table.

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

if LOCAL_BOT_API_URL:
    if SERVER_ID == "s4":
        threading.Thread(
            target=local_bot_api_controller_loop,
            daemon=True,
            name="local-bot-api-controller",
        ).start()
        print(f"[LOCAL BOT API] s4 controller started for {LOCAL_BOT_API_URL}")
    else:
        threading.Thread(
            target=local_bot_api_follower_loop,
            daemon=True,
            name="local-bot-api-follower",
        ).start()
        print(f"[LOCAL BOT API] {SERVER_ID} following shared API mode")

# ---------------------- RUN BOT ----------------------
if SERVER_ID == "s4":
    print("[MODE] s4 – polling enabled")
    bot.infinity_polling(skip_pending=True)  # drop messages sent while the bot was down

else:
    print(f"[MODE] {SERVER_ID} – worker only (no polling)")
    while True:
        time.sleep(10)

