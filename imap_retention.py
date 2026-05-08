import imaplib
import email
import os
import sys
import yaml
import logging
import hashlib
import time
import stat
import tempfile
from datetime import datetime, timezone, timedelta
from email import policy as epolicy
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
 
# ── Load environment ────────────────────────────────────────────────────────
 
load_dotenv()
 
# ── Config validation ────────────────────────────────────────────────────────
 
REQUIRED_ENV = ["GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "NAS_PATH"]
ALLOWED_LABEL_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_ ")
 
def validate_config(cfg: dict) -> None:
    """Validate config values before use — raises ValueError on bad input."""
    rules = cfg.get("rules", {})
    opts = cfg.get("options", {})
 
    age = rules.get("age_months", 12)
    if not isinstance(age, int) or not (1 <= age <= 120):
        raise ValueError(f"age_months must be an integer between 1 and 120, got: {age}")
 
    size = rules.get("size_mb", 10)
    if not isinstance(size, (int, float)) or not (0.1 <= size <= 500):
        raise ValueError(f"size_mb must be between 0.1 and 500, got: {size}")
 
    label = rules.get("label", "Archive")
    if not all(c in ALLOWED_LABEL_CHARS for c in label):
        raise ValueError(f"label contains invalid characters: {label!r}")
 
    batch = opts.get("batch_size", 500)
    if not isinstance(batch, int) or not (1 <= batch <= 2000):
        raise ValueError(f"batch_size must be between 1 and 2000, got: {batch}")
 
def load_cfg(path: str = "config.yaml") -> dict:
    if not Path(path).exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(path) as f:
        cfg = yaml.safe_load(f)
    validate_config(cfg)
    return cfg
 
def check_env() -> None:
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        raise EnvironmentError(
            f"Missing required environment variables: {', '.join(missing)}\n"
            f"Check your .env file."
        )
 
# ── Logging ──────────────────────────────────────────────────────────────────
 
class ScrubFilter(logging.Filter):
    """Remove sensitive values from all log output."""
    def __init__(self):
        super().__init__()
        self._secrets: list[str] = []
 
    def add_secret(self, secret: str) -> None:
        if secret:
            self._secrets.append(secret)
 
    def filter(self, record: logging.LogRecord) -> bool:
        for secret in self._secrets:
            record.msg = str(record.msg).replace(secret, "***REDACTED***")
        return True
 
scrub_filter = ScrubFilter()
 
def setup_logging(log_path: str) -> None:
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    handler_file = logging.FileHandler(log_path)
    handler_console = logging.StreamHandler(sys.stdout)
    for h in (handler_file, handler_console):
        h.addFilter(scrub_filter)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[handler_file, handler_console],
    )
 
# ── IMAP connection ───────────────────────────────────────────────────────────
 
def connect_imap() -> imaplib.IMAP4_SSL:
    """Open TLS-verified IMAP connection. Never logs credentials."""
    address = os.environ["GMAIL_ADDRESS"]
    password = os.environ["GMAIL_APP_PASSWORD"]
 
    # Register password with scrub filter so it never appears in logs
    scrub_filter.add_secret(password)
    scrub_filter.add_secret(address)
 
    import ssl
    ctx = ssl.create_default_context()  # Verifies certificate by default
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
 
    try:
        conn = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=ctx)
        conn.login(address, password)
        logging.info("IMAP connection established (TLS verified)")
        return conn
    except imaplib.IMAP4.error as e:
        # Scrub any credential echoes from IMAP error messages
        msg = str(e).replace(password, "***").replace(address, "***")
        raise RuntimeError(f"IMAP login failed: {msg}") from None
 
# ── Secure file writing ───────────────────────────────────────────────────────
 
def atomic_write(dest: Path, data: bytes) -> None:
    """
    Write data atomically: write to .tmp file first, then rename.
    Prevents partial/corrupt files if interrupted mid-write.
    Sets file permissions to 600 (owner read/write only).
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=dest.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(data)
        # Set permissions before rename so file is never world-readable
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # 600
        os.replace(tmp_path, dest)  # Atomic on POSIX
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
 
def checksum(data: bytes) -> str:
    """SHA-256 checksum for audit log integrity verification."""
    return hashlib.sha256(data).hexdigest()[:16]
 
# ── Email processing ──────────────────────────────────────────────────────────
 
def ensure_dirs(nas: str) -> None:
    for sub in ("emails", "attachments", "logs"):
        Path(nas, sub).mkdir(parents=True, exist_ok=True)
 
def save_email(uid: bytes, raw: bytes, nas: str, dry_run: bool) -> str:
    dest = Path(nas, "emails", f"{uid.decode()}.eml")
    if not dry_run:
        atomic_write(dest, raw)
    return str(dest)
 
def save_attachments(uid: bytes, msg, nas: str, dry_run: bool) -> list[str]:
    saved = []
    for part in msg.walk():
        filename = part.get_filename()
        if not filename:
            continue
        # Sanitize filename — strip path separators and dangerous chars
        safe_name = "".join(
            c for c in filename
            if c not in r'\/:*?"<>|'
        ).strip()
        if not safe_name:
            continue
        dest = Path(nas, "attachments", f"{uid.decode()}_{safe_name}")
        payload = part.get_payload(decode=True)
        if payload and not dry_run:
            atomic_write(dest, payload)
        saved.append(str(dest))
    return saved
 
def process_folder(
    conn: imaplib.IMAP4_SSL,
    folder: str,
    query: str,
    action: str,
    cfg: dict,
    nas: str,
    counter: list,
) -> int:
    dry = cfg["options"]["dry_run"]
    batch = cfg["options"].get("batch_size", 500)
    rate_delay = cfg["options"].get("rate_delay_ms", 200) / 1000
 
    try:
        status, _ = conn.select(f'"{folder}"')
        if status != "OK":
            logging.warning(f"Could not select folder '{folder}' — skipping")
            return 0
    except imaplib.IMAP4.error as e:
        logging.warning(f"Folder select error for '{folder}': {e}")
        return 0
 
    _, ids = conn.search(None, query)
    if not ids[0]:
        logging.info(f"[{action}] no matches in '{folder}'")
        return 0
 
    all_ids = ids[0].split()
    batch_ids = all_ids[:batch]
    logging.info(f"[{action}] {len(all_ids)} matched, processing {len(batch_ids)} (batch limit)")
 
    processed = 0
    for uid in batch_ids:
        try:
            _, data = conn.fetch(uid, "(RFC822)")
            if not data or not data[0]:
                logging.warning(f"[{action}] empty fetch for uid {uid.decode()} — skipping")
                continue
 
            raw = data[0][1]
            msg = email.message_from_bytes(raw, policy=epolicy.default)
            subj = str(msg.get("subject", "(no subject)"))[:80]
 
            # Checksum for audit trail
            csum = checksum(raw)
            dest = save_email(uid, raw, nas, dry)
 
            counter[0] += 1
            prefix = "[DRY] " if dry else ""
            logging.info(
                f"{prefix}[{action}] #{counter[0]:04d} sha256:{csum} "
                f'"{subj}" → {dest}'
            )
 
            if cfg["options"].get("save_attachments"):
                atts = save_attachments(uid, msg, nas, dry)
                for a in atts:
                    logging.info(f"  attachment → {a}")
 
            if not dry and cfg["options"].get("delete_after_archive", False):
                conn.store(uid, "+FLAGS", "\\Deleted")
 
            processed += 1
            time.sleep(rate_delay)  # Rate limiting — be nice to Gmail
 
        except Exception as e:
            # Isolate per-email failures — log and continue
            logging.error(f"[{action}] failed on uid {uid.decode()}: {e}")
            continue
 
    if not dry and cfg["options"].get("delete_after_archive", False):
        conn.expunge()
 
    return processed
 
# ── Main ──────────────────────────────────────────────────────────────────────
 
def run() -> None:
    # 1. Load and validate everything before connecting
    check_env()
    cfg = load_cfg()
 
    nas = os.environ["NAS_PATH"]
    log_path = cfg["options"].get("log_file", f"{nas}/logs/retention.log")
    setup_logging(log_path)
 
    logging.info("=" * 60)
    logging.info("Retention run started")
    logging.info(f"NAS path:   {nas}")
    logging.info(f"Dry run:    {cfg['options']['dry_run']}")
    logging.info(f"Rules:      age>{cfg['rules']['age_months']}mo  "
                 f"size>{cfg['rules']['size_mb']}MB  "
                 f"label='{cfg['rules']['label']}'")
    logging.info("=" * 60)
 
    ensure_dirs(nas)
 
    # 2. Build IMAP queries
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=cfg["rules"]["age_months"] * 30
    )
    date_str = cutoff.strftime("%d-%b-%Y")
    size_kb = int(cfg["rules"]["size_mb"] * 1024)
    label = cfg["rules"]["label"]
 
    # 3. Connect
    conn = connect_imap()
 
    total = 0
    counter = [0]  # Shared mutable counter across calls
 
    try:
        # Rule priority: label first, then size, then age
        total += process_folder(conn, label,   "ALL",            "label-move",   cfg, nas, counter)
        total += process_folder(conn, "INBOX", f"LARGER {size_kb}", "size-offload", cfg, nas, counter)
        total += process_folder(conn, "INBOX", f"BEFORE {date_str}", "age-archive",  cfg, nas, counter)
    finally:
        try:
            conn.close()
            conn.logout()
        except Exception:
            pass
 
    logging.info("=" * 60)
    logging.info(f"Run complete — {total} emails processed")
    if cfg["options"]["dry_run"]:
        logging.info("DRY RUN — no emails were moved or deleted")
    logging.info("=" * 60)
 
if __name__ == "__main__":
    try:
        run()
    except (FileNotFoundError, EnvironmentError, ValueError) as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"Runtime error: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrupted by user", file=sys.stderr)
        sys.exit(0)
 
