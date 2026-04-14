#!/usr/bin/env python3
"""
cloudflare_watcher.py

Runs cloudflared with configured args, captures the quick-tunnel URL printed to stdout/stderr,
writes it to a file atomically, sends it to a Discord webhook (hardcoded), and restarts if the process exits.
"""

import subprocess
import time
import re
import signal
import sys
import os
import logging
import json
import urllib.request
import urllib.error
import tempfile
import shutil

# --------------------  -----
# Config (tweak if needed)
# -------------------------
CLOUDFLARED_BIN = "/usr/bin/cloudflared"    # will fallback to PATH if not found here
ARGS = [
    "--url", "http://localhost:5000",
    "--logfile", "cloudflared.log",
    "--loglevel", "info",
    "--no-autoupdate",
    "--metrics", "localhost:9090"
]
OUTPUT_URL_FILE = "/var/run/cloudflared_quick_url.txt"
LAST_SENT_FILE = "/var/run/cloudflared_last_sent_url.txt"
BACKOFF_BASE = 2
BACKOFF_MAX = 300
LOG_FILE = "/var/log/cloudflare_watcher.log"

# -------------------------
# Hardcoded Discord webhook URL (YOU REQUESTED IT INSIDE THE CODE)
# Replace with your webhook if different
# -------------------------
DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/1427611879613665281/4R0t20oxZTBTLZCew06NMO5EGLASpof7gizvgPt1_zto6vOT2suH7zSEV83ba3GKM8B0"

# -------------------------
# Logging setup
# -------------------------
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
console = logging.StreamHandler()
console.setLevel(logging.INFO)
logging.getLogger("").addHandler(console)

stop_requested = False

os.system('clear')

def handle_sig(signum, frame):
    global stop_requested
    logging.info("Received signal %s, stopping.", signum)
    stop_requested = True

signal.signal(signal.SIGTERM, handle_sig)
signal.signal(signal.SIGINT, handle_sig)

url_regex = re.compile(r"https?://[^\s]+trycloudflare\.com[^\s]*", re.IGNORECASE)

# -------------------------
# Helpers
# -------------------------
def find_cloudflared():
    # try configured path then PATH
    if os.path.isfile(CLOUDFLARED_BIN) and os.access(CLOUDFLARED_BIN, os.X_OK):
        return CLOUDFLARED_BIN
    found = shutil.which("cloudflared")
    if found:
        logging.info("Using cloudflared from PATH: %s", found)
        return found
    logging.error("cloudflared not found at %s or in PATH", CLOUDFLARED_BIN)
    return None

def atomic_write(path, text, mode="w", perms=None):
    """Write `text` to `path` atomically using a temp file in same directory."""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory)
    try:
        with os.fdopen(fd, mode) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if perms is not None:
            os.chmod(tmp, perms)
        os.replace(tmp, path)
        return True
    except Exception:
        logging.exception("atomic_write failed for %s", path)
        try:
            os.remove(tmp)
        except Exception:
            pass
        return False

def read_last_sent():
    try:
        with open(LAST_SENT_FILE, "r") as f:
            return f.read().strip()
    except Exception:
        return ""

def write_last_sent(url):
    try:
        atomic_write(LAST_SENT_FILE, url.strip() + "\n", perms=0o644)
    except Exception:
        logging.exception("Failed to write last sent file")

def send_discord_webhook(url, webhook_url=DISCORD_WEBHOOK_URL, username="cloudflared-watcher", max_retries=3):
    """Send the URL to Discord webhook. Returns True on success (or skipped because same), False otherwise."""
    if not webhook_url:
        logging.debug("No Discord webhook configured; skipping send.")
        return False

    last = read_last_sent()
    if last == url:
        logging.info("URL same as last sent; skipping Discord webhook.")
        return True

    payload = {
        "username": username,
        "embeds": [
            {
                "title": "New Cloudflare quick tunnel",
                "description": f"Detected URL: {url}",
                "color": 0x00A8FF,
                "fields": [
                    {"name": "Host", "value": url, "inline": False},
                    {"name": "Time (UTC)", "value": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), "inline": True}
                ]
            }
        ]
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": "cloudflare-watcher/1.0"}
    )

    attempt = 0
    backoff = 1
    while attempt <= max_retries:
        attempt += 1
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                status = resp.getcode()
                logging.info("Discord webhook sent, HTTP %s", status)
                write_last_sent(url)
                return True
        except urllib.error.HTTPError as e:
            # Read body for diagnostics (but DON'T log the webhook URL/token)
            try:
                body = e.read().decode("utf-8", errors="ignore")
            except Exception:
                body = "<no body>"
            logging.error("Discord webhook HTTPError %s: %s", e.code, body)
            if e.code == 403:
                logging.error("403 Forbidden from Discord webhook. Check webhook validity, channel permissions, or network blocks.")
                return False
            if e.code == 429:
                # Rate limited: try to parse retry_after from body JSON
                retry_after = None
                try:
                    j = json.loads(body)
                    retry_after = j.get("retry_after") or j.get("retry_after_ms")
                except Exception:
                    pass
                if retry_after is None:
                    retry_after = backoff
                logging.warning("Rate limited by Discord. Sleeping %s seconds (attempt %s/%s)", retry_after, attempt, max_retries)
                time.sleep(float(retry_after))
                backoff *= 2
                continue
            return False
        except urllib.error.URLError as e:
            logging.error("Discord webhook URLError: %s (attempt %s/%s) - retrying", e, attempt, max_retries)
            time.sleep(backoff)
            backoff *= 2
            continue
        except Exception:
            logging.exception("Unexpected error sending Discord webhook (attempt %s/%s)", attempt, max_retries)
            time.sleep(backoff)
            backoff *= 2
            continue

    logging.error("Exceeded retries sending Discord webhook.")
    return False

# -------------------------
# Core logic
# -------------------------
def write_url(url):
    try:
        atomic_write(OUTPUT_URL_FILE, url.strip() + "\n", perms=0o644)
        logging.info("Wrote URL to %s: %s", OUTPUT_URL_FILE, url)
    except Exception:
        logging.exception("Failed to write URL file")

def run_once(cloudflared_bin):
    cmd = [cloudflared_bin] + ARGS
    logging.info("Starting: %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    except FileNotFoundError:
        logging.exception("cloudflared binary not found: %s", cloudflared_bin)
        return 127, False
    except Exception:
        logging.exception("Failed to start cloudflared")
        return 1, False

    url_found = False
    try:
        if proc.stdout is None:
            logging.error("No stdout from cloudflared process")
        else:
            for raw in proc.stdout:
                line = raw.rstrip("\n")
                logging.info("cloudflared: %s", line)
                if not url_found:
                    m = url_regex.search(line)
                    if m:
                        url = m.group(0)
                        write_url(url)
                        try:
                            sent = send_discord_webhook(url)
                            if sent:
                                logging.info("URL delivered to Discord webhook (if configured).")
                            else:
                                logging.info("Discord webhook not sent or failed.")
                        except Exception:
                            logging.exception("Exception while sending Discord webhook")
                        url_found = True
                if stop_requested:
                    logging.info("Stop requested; terminating cloudflared.")
                    proc.terminate()
                    break
    except Exception:
        logging.exception("Error while reading cloudflared output")
    finally:
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            proc.wait()
    logging.info("cloudflared exited with code %s", proc.returncode)
    return proc.returncode, url_found

def main():
    # Warn if not root, but don't forcibly exit (useful for local testing).
    if os.getuid() != 0:
        logging.warning("Not running as root (UID=%s). Some paths may be unwritable. Consider running under systemd with appropriate permissions.", os.getuid())

    cloudflared_bin = find_cloudflared()
    if not cloudflared_bin:
        logging.error("cloudflared binary not found; exiting.")
        sys.exit(127)

    # Start the Flask server (server.py)
    try:
        logging.info("Starting server.py...")
        server_proc = subprocess.Popen(["python3", "server.py"])
        time.sleep(3)  # Wait 3 seconds for server.py to initialize
    except Exception as e:
        logging.exception("Failed to start server.py")
        sys.exit(1)

    # Ensure output directory exists
    try:
        os.makedirs(os.path.dirname(OUTPUT_URL_FILE), exist_ok=True)
        with open(OUTPUT_URL_FILE, "a"):
            pass
    except PermissionError:
        logging.warning("Insufficient permission to write %s. Running UID=%s. Adjust permissions or run with elevated privileges.", OUTPUT_URL_FILE, os.getuid())

    backoff = BACKOFF_BASE
    try:
        while not stop_requested:
            rc, url_found = run_once(cloudflared_bin)
            if stop_requested:
                break
            if url_found:
                backoff = BACKOFF_BASE
            else:
                backoff = min(backoff * 2, BACKOFF_MAX)
            logging.info("cloudflared stopped (rc=%s). Will restart after %s seconds.", rc, backoff)
            slept = 0
            while slept < backoff and not stop_requested:
                time.sleep(1)
                slept += 1
    finally:
        logging.info("Stopping server.py...")
        server_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server_proc.kill()
            server_proc.wait()
        logging.info("server.py stopped.")
        logging.info("Watcher exiting.")


if __name__ == "__main__":
    main()
