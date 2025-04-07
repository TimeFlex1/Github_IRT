from flask import Flask, render_template, jsonify, Response, request, redirect, url_for, send_file
import requests
import threading
import time
from collections import deque
import csv
import json
import io
import logging
import os
from datetime import datetime, timezone
import pytz  # For timezone handling
import platform  # To detect the operating system
import subprocess  # To sync system time (if needed)

# Optional: For NTP time sync
try:
    import ntplib
    NTP_AVAILABLE = True
except ImportError:
    NTP_AVAILABLE = False
    print("ntplib not found. Install it with 'pip install ntplib' to enable NTP time sync.")

logging.getLogger('werkzeug').setLevel(logging.ERROR)

app = Flask(__name__)
recent_repos = deque(maxlen=1000)
is_monitoring = False
lock = threading.Lock()
user_cache = {}

# Configuration
FETCH_INTERVAL_SECONDS = 10  # Time between GitHub API fetches in seconds
TOKENS_FILE = "api_tokens.json"  # File to store API tokens
INITIAL_PAT = ""  # Initial PAT with 'admin:public_key' scope to generate new tokens (replace with your PAT)

# Metrics for UI
last_update = "Never"
rate_limit_remaining = 5000
rate_limit_reset = "N/A"
total_requests_made = 0

# File paths
LAST_UPDATED_FILE = "last-updated-repos.json"
BACKUP_FILE = "repo-list-backup.jsonl"
CLIENT_LOG_FILE = "client-logs.jsonl"
MAX_BACKUP_SIZE = 50 * 1024 * 1024  # 50MB in bytes

# Admin credentials
ADMIN_PASSWORD = "admin123"  # Change this to a secure password

# Helper function to format timestamps in a human-readable way
def format_timestamp(timestamp):
    if isinstance(timestamp, str) and timestamp == "Never":
        return "Never"
    if isinstance(timestamp, str):
        try:
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError:
            return timestamp  # Return as-is if it can't be parsed
    return timestamp.strftime("%B %d, %Y %I:%M %p")  # e.g., "April 06, 2025 03:45 PM"

# Function to get the system's timezone
def get_system_timezone():
    try:
        # Get the local timezone name
        local_tz = datetime.now().astimezone().tzinfo
        return local_tz
    except Exception as e:
        print(f"Error getting system timezone: {e}")
        return None

# Function to sync system time with an NTP server
def sync_system_time():
    if not NTP_AVAILABLE:
        print("NTP sync unavailable: ntplib not installed.")
        return False

    try:
        ntp_client = ntplib.NTPClient()
        response = ntp_client.request('pool.ntp.org')
        ntp_time = datetime.fromtimestamp(response.tx_time)
        system_time = datetime.now()

        # Check if the system time is significantly off (e.g., more than 5 seconds)
        time_diff = abs((ntp_time - system_time).total_seconds())
        if time_diff > 5:
            print(f"System time is off by {time_diff:.2f} seconds. Attempting to sync...")

            # Convert NTP time to epoch seconds
            ntp_epoch = int(response.tx_time)

            # Sync system time based on the OS
            os_name = platform.system().lower()
            if os_name == "windows":
                # On Windows, use w32tm to sync time (requires admin privileges)
                subprocess.run(["w32tm", "/resync"], check=True)
                print("System time synced on Windows using w32tm.")
            elif os_name in ("linux", "darwin"):  # Linux or macOS
                # On Linux/macOS, use date command (requires sudo)
                subprocess.run(["sudo", "date", "--set", f"@{ntp_epoch}"], check=True)
                print("System time synced on Linux/macOS using date command.")
            else:
                print(f"Unsupported OS: {os_name}. Cannot sync system time.")
                return False
            return True
        else:
            print("System time is within acceptable range. No sync needed.")
            return True
    except Exception as e:
        print(f"Failed to sync system time with NTP: {e}")
        return False

# Log system time and timezone at startup
def log_system_time():
    system_time = datetime.now()
    system_tz = get_system_timezone()
    print(f"System time at startup: {system_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"System timezone: {system_tz if system_tz else 'Unknown'}")

# Load tokens from file if it exists, otherwise initialize with an empty list
def load_tokens():
    if os.path.exists(TOKENS_FILE):
        try:
            with open(TOKENS_FILE, 'r') as f:
                data = json.load(f)
                if not isinstance(data, list) or not all(isinstance(token, str) for token in data):
                    print(f"Error: {TOKENS_FILE} does not contain a valid list of strings. Expected format: [\"token1\", \"token2\", ...]")
                    return []
                return data
        except Exception as e:
            print(f"Error loading tokens from {TOKENS_FILE}: {e}")
            return []
    return []

# Save tokens to file
def save_tokens(tokens):
    try:
        with open(TOKENS_FILE, 'w') as f:
            json.dump(tokens, f)
        print(f"Saved tokens to {TOKENS_FILE}")
    except Exception as e:
        print(f"Error saving tokens to {TOKENS_FILE}: {e}")

# Generate a new GitHub Personal Access Token using an existing PAT
def generate_github_token(initial_pat):
    url = "https://api.github.com/authorizations"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {initial_pat}",
        "X-GitHub-Api-Version": "2022-11-28"
    }
    payload = {
        "note": "Auto-generated token for Flask app",
        "scopes": ["public_repo", "read:user", "repo"]
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()
        new_token = data.get("token")
        if new_token:
            print(f"Successfully generated new GitHub token: {new_token[:10]}...")
            return new_token
        else:
            print("Failed to generate new token: No token in response")
            return None
    except requests.exceptions.RequestException as e:
        print(f"Error generating new GitHub token: {e}")
        return None

# Validate a token by making a simple API request
def validate_token(token):
    url = "https://api.github.com/user"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}"
    }
    try:
        response = requests.get(url, headers=headers)
        return response.status_code == 200
    except Exception as e:
        print(f"Error validating token {token[:10]}...: {e}")
        return False

# Sync time and log system time at startup
log_system_time()
if NTP_AVAILABLE:
    print("Attempting to sync system time with NTP server...")
    sync_system_time()
else:
    print("Skipping NTP sync. Ensure your system time is correct or install ntplib to enable automatic sync.")

# Initialize tokens on startup
tokens = load_tokens()
if not tokens:
    print("No API tokens found.")
    if INITIAL_PAT:
        print("Attempting to generate a new GitHub token using the initial PAT...")
        new_token = generate_github_token(INITIAL_PAT)
        if new_token:
            tokens.append(new_token)
            save_tokens(tokens)
        else:
            print("Failed to generate a new token. Please manually create a GitHub Personal Access Token and add it to the 'tokens' list in app.py.")
            print("Steps to create a PAT:")
            print("1. Go to https://github.com/settings/tokens")
            print("2. Click 'Generate new token'")
            print("3. Select scopes: 'public_repo', 'read:user', 'repo'")
            print("4. Copy the token and add it to the 'tokens' list in app.py")
            print("5. Restart the application")
    else:
        print("No initial PAT provided to generate a new token. Please manually create a GitHub Personal Access Token and add it to the 'tokens' list in app.py.")
        print("Steps to create a PAT:")
        print("1. Go to https://github.com/settings/tokens")
        print("2. Click 'Generate new token'")
        print("3. Select scopes: 'public_repo', 'read:user', 'repo'")
        print("4. Copy the token and add it to the 'tokens' list in app.py")
        print("5. Restart the application")
else:
    valid_tokens = []
    for token in tokens:
        if validate_token(token):
            valid_tokens.append(token)
        else:
            print(f"Token {token[:10]}... is invalid and will be skipped.")
    tokens = valid_tokens
    if not tokens:
        print("No valid API tokens found after validation.")
        if INITIAL_PAT:
            print("Attempting to generate a new GitHub token using the initial PAT...")
            new_token = generate_github_token(INITIAL_PAT)
            if new_token:
                tokens.append(new_token)
                save_tokens(tokens)
    else:
        print(f"Loaded {len(tokens)} valid API tokens.")

current_token_idx = 0

def load_recent_repos():
    global recent_repos
    if os.path.exists(LAST_UPDATED_FILE):
        try:
            with open(LAST_UPDATED_FILE, 'r') as f:
                data = json.load(f)
                # Convert created_at timestamps to local time and reformat
                for repo in data:
                    if repo.get("created_at") and repo["created_at"] != "Unknown":
                        try:
                            # Parse the UTC timestamp and convert to local time
                            dt = datetime.fromisoformat(repo["created_at"].replace("Z", "+00:00"))
                            local_tz = get_system_timezone()
                            if local_tz:
                                dt = dt.replace(tzinfo=timezone.utc).astimezone(local_tz)
                            repo["created_at"] = dt.strftime("%Y-%m-%d %H:%M:%S")
                        except ValueError as e:
                            print(f"Error parsing created_at timestamp: {e}")
                            pass
                recent_repos = deque(data, maxlen=1000)
        except Exception as e:
            print(f"Error loading recent repos: {e}")

def save_recent_repos():
    with lock:
        try:
            with open(LAST_UPDATED_FILE, 'w') as f:
                json.dump(list(recent_repos), f)
        except Exception as e:
            print(f"Error saving recent repos: {e}")

def append_to_backup(new_repos):
    with lock:
        try:
            with open(BACKUP_FILE, 'a') as f:
                for repo in new_repos:
                    json.dump(repo, f)
                    f.write('\n')
        except Exception as e:
            print(f"Error appending to backup: {e}")
            return

        while os.path.getsize(BACKUP_FILE) > MAX_BACKUP_SIZE:
            try:
                with open(BACKUP_FILE, 'r') as f:
                    lines = f.readlines()
                keep_count = int(len(lines) * 0.9)
                with open(BACKUP_FILE, 'w') as f:
                    f.writelines(lines[-keep_count:])
            except Exception as e:
                print(f"Error trimming backup file: {e}")
                break

def log_client_action(ip, action, user_agent):
    with lock:
        log_entry = {
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),  # Use local time
            "ip": ip,
            "action": action,
            "user_agent": user_agent
        }
        try:
            with open(CLIENT_LOG_FILE, 'a') as f:
                json.dump(log_entry, f)
                f.write('\n')
        except Exception as e:
            print(f"Error logging client action: {e}")

def log_bad_request(ip, headers, raw_data=None):
    print(f"Bad request from {ip}: Headers={headers}, RawData={raw_data}")

def log_successful_request(ip, path, user_agent):
    print(f"Successful request from {ip} to {path}: User-Agent={user_agent}")

def fetch_github_events():
    global is_monitoring, current_token_idx, last_update, rate_limit_remaining, rate_limit_reset, total_requests_made
    if not tokens:
        print("No GitHub API tokens available. Cannot fetch events.")
        return

    url = "https://api.github.com/events"
    user_url = "https://api.github.com/users/{username}"
    repo_url = "https://api.github.com/repos/{reponame}"
    last_fetch_time = 0
    
    while True:
        with lock:
            if not is_monitoring:
                time.sleep(1)
                continue
        
        current_time = time.time()
        elapsed = current_time - last_fetch_time
        if elapsed < FETCH_INTERVAL_SECONDS:
            time.sleep(FETCH_INTERVAL_SECONDS - elapsed)
        
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {tokens[current_token_idx]}"
        }
        try:
            print(f"Fetching GitHub events with Token {current_token_idx + 1}...")
            requests_made_this_cycle = 0
            
            response = requests.get(url, headers=headers)
            requests_made_this_cycle += 1
            total_requests_made += 1
            
            if response.status_code == 403 and int(response.headers.get("X-RateLimit-Remaining", 0)) == 0:
                print(f"Token {current_token_idx + 1} hit limit, switching...")
                current_token_idx = (current_token_idx + 1) % len(tokens)
                rate_limit_remaining = 0
                rate_limit_reset = time.strftime("%B %d, %Y %I:%M %p", time.localtime(int(response.headers.get("X-RateLimit-Reset", time.time() + 3600))))
                if current_token_idx == 0:
                    print("All tokens exhausted! Pausing until reset...")
                    reset_time = int(response.headers.get("X-RateLimit-Reset", time.time() + 3600))
                    time.sleep(max(reset_time - time.time(), 60))
                    rate_limit_remaining = 5000
                    rate_limit_reset = "N/A"
                continue
            
            response.raise_for_status()
            rate_limit_remaining = int(response.headers.get("X-RateLimit-Remaining", rate_limit_remaining))
            rate_limit_reset = "N/A" if rate_limit_remaining > 0 else rate_limit_reset
            events = response.json()
            new_repos = []
            
            for event in events:
                if event["type"] == "CreateEvent" and event["payload"]["ref_type"] == "repository":
                    creator = event.get("actor", {}).get("login", "Unknown")
                    repo_name = event.get("repo", {}).get("name", "Unknown")
                    
                    if creator == "Unknown" or repo_name == "Unknown":
                        print(f"Skipping event with missing creator or repo name: {event}")
                        continue
                    
                    with lock:
                        followers = user_cache.get(creator, None)
                    if followers is None:
                        user_response = requests.get(user_url.format(username=creator), headers=headers)
                        requests_made_this_cycle += 1
                        total_requests_made += 1
                        followers = user_response.json().get("followers", 0) if user_response.ok else "N/A"
                        with lock:
                            user_cache[creator] = followers
                    
                    repo_response = requests.get(repo_url.format(reponame=repo_name), headers=headers)
                    requests_made_this_cycle += 1
                    total_requests_made += 1
                    description = repo_response.json().get("description", "No description") if repo_response.ok else "N/A"
                    
                    # Convert created_at from UTC to local time
                    created_at = event.get("created_at", "Unknown")
                    if created_at != "Unknown":
                        try:
                            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                            local_tz = get_system_timezone()
                            if local_tz:
                                dt = dt.replace(tzinfo=timezone.utc).astimezone(local_tz)
                            created_at = dt.strftime("%Y-%m-%d %H:%M:%S")
                        except ValueError as e:
                            print(f"Error converting created_at to local time: {e}")
                            pass
                    
                    repo_info = {
                        "name": repo_name,
                        "creator": creator,
                        "followers": followers,
                        "created_at": created_at,
                        "url": f"https://github.com/{repo_name}",
                        "description": description
                    }
                    new_repos.append(repo_info)
            
            with lock:
                for repo in new_repos:
                    recent_repos.appendleft(repo)
            
            append_to_backup(new_repos)
            save_recent_repos()
            
            last_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # Use local time
            remaining = response.headers.get("X-RateLimit-Remaining", "Unknown")
            print(f"Fetch complete. Token {current_token_idx + 1} requests remaining: {remaining}. Requests made this cycle: {requests_made_this_cycle}")
            last_fetch_time = time.time()

        except requests.exceptions.RequestException as e:
            print(f"Error during fetch: {e}")
            time.sleep(5)

def manual_fetch():
    global last_update, rate_limit_remaining, rate_limit_reset, total_requests_made
    if not tokens:
        return False, "No GitHub API tokens available."

    url = "https://api.github.com/events"
    user_url = "https://api.github.com/users/{username}"
    repo_url = "https://api.github.com/repos/{reponame}"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {tokens[current_token_idx]}"
    }
    try:
        requests_made_this_cycle = 0
        
        response = requests.get(url, headers=headers)
        requests_made_this_cycle += 1
        total_requests_made += 1
        
        if response.status_code == 403 and int(response.headers.get("X-RateLimit-Remaining", 0)) == 0:
            return False, "Rate limit exceeded"
        response.raise_for_status()
        rate_limit_remaining = int(response.headers.get("X-RateLimit-Remaining", rate_limit_remaining))
        rate_limit_reset = "N/A" if rate_limit_remaining > 0 else rate_limit_reset
        events = response.json()
        new_repos = []
        
        for event in events:
            if event["type"] == "CreateEvent" and event["payload"]["ref_type"] == "repository":
                creator = event.get("actor", {}).get("login", "Unknown")
                repo_name = event.get("repo", {}).get("name", "Unknown")
                
                if creator == "Unknown" or repo_name == "Unknown":
                    print(f"Skipping event with missing creator or repo name: {event}")
                    continue
                
                with lock:
                    followers = user_cache.get(creator, None)
                if followers is None:
                    user_response = requests.get(user_url.format(username=creator), headers=headers)
                    requests_made_this_cycle += 1
                    total_requests_made += 1
                    followers = user_response.json().get("followers", 0) if user_response.ok else "N/A"
                    with lock:
                        user_cache[creator] = followers
                
                repo_response = requests.get(repo_url.format(reponame=repo_name), headers=headers)
                requests_made_this_cycle += 1
                total_requests_made += 1
                description = repo_response.json().get("description", "No description") if repo_response.ok else "N/A"
                
                # Convert created_at from UTC to local time
                created_at = event.get("created_at", "Unknown")
                if created_at != "Unknown":
                    try:
                        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                        local_tz = get_system_timezone()
                        if local_tz:
                            dt = dt.replace(tzinfo=timezone.utc).astimezone(local_tz)
                        created_at = dt.strftime("%Y-%m-%d %H:%M:%S")
                    except ValueError as e:
                        print(f"Error converting created_at to local time: {e}")
                        pass
                
                repo_info = {
                    "name": repo_name,
                    "creator": creator,
                    "followers": followers,
                    "created_at": created_at,
                    "url": f"https://github.com/{repo_name}",
                    "description": description
                }
                new_repos.append(repo_info)
        
        with lock:
            for repo in new_repos:
                recent_repos.appendleft(repo)
        
        append_to_backup(new_repos)
        save_recent_repos()
        last_update = datetime.now().strftime("%Y-%m-%d %H:%M:%S")  # Use local time
        print(f"Manual fetch complete. Requests made: {requests_made_this_cycle}")
        return True, "Fetch successful"
    except Exception as e:
        return False, str(e)

load_recent_repos()
thread = threading.Thread(target=fetch_github_events, daemon=True)
thread.start()

@app.before_request
def filter_requests():
    valid_methods = ['GET', 'POST', 'HEAD', 'OPTIONS']
    if not request.headers.get('User-Agent') or request.method not in valid_methods:
        log_bad_request(request.remote_addr, dict(request.headers), request.get_data())
        return "Invalid request", 400
    log_successful_request(request.remote_addr, request.path, request.headers.get('User-Agent'))

@app.route("/")
def index():
    client_ip = request.remote_addr
    user_agent = request.headers.get('User-Agent', 'Unknown')
    log_client_action(client_ip, "visited_index", user_agent)
    return render_template("index.html")

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        password = request.form.get("password")
        if password == ADMIN_PASSWORD:
            response = redirect(url_for("admin_dashboard"))
            response.set_cookie("admin", "true", max_age=3600)
            return response
        return render_template("admin_login.html", error="Invalid password")
    return render_template("admin_login.html", error=None)

@app.route("/admin/dashboard")
def admin_dashboard():
    admin_cookie = request.cookies.get("admin")
    if admin_cookie != "true":
        return redirect(url_for("admin_login"))
    return render_template("admin_dashboard.html")

@app.route("/toggle_monitoring", methods=["POST"])
def toggle_monitoring():
    admin_cookie = request.cookies.get("admin")
    if admin_cookie != "true":
        return jsonify({"error": "Unauthorized"}), 403
    global is_monitoring
    with lock:
        is_monitoring = not is_monitoring
        status = "started" if is_monitoring else "stopped"
        print(f"Real-time monitoring {status}")
    return jsonify({"monitoring": is_monitoring})

@app.route("/admin/clear_repos", methods=["POST"])
def clear_repos():
    admin_cookie = request.cookies.get("admin")
    if admin_cookie != "true":
        return jsonify({"error": "Unauthorized"}), 403
    with lock:
        global recent_repos
        recent_repos.clear()
        save_recent_repos()
    return jsonify({"message": "Repo list cleared"})

@app.route("/admin/clear_backup", methods=["POST"])
def clear_backup():
    admin_cookie = request.cookies.get("admin")
    if admin_cookie != "true":
        return jsonify({"error": "Unauthorized"}), 403
    with lock:
        try:
            open(BACKUP_FILE, 'w').close()
            return jsonify({"message": "Backup file cleared"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

@app.route("/admin/manual_fetch", methods=["POST"])
def manual_fetch_endpoint():
    admin_cookie = request.cookies.get("admin")
    if admin_cookie != "true":
        return jsonify({"error": "Unauthorized"}), 403
    success, message = manual_fetch()
    if success:
        return jsonify({"message": message})
    return jsonify({"error": message}), 500

@app.route("/admin/download_backup")
def download_backup():
    admin_cookie = request.cookies.get("admin")
    if admin_cookie != "true":
        return redirect(url_for("admin_login"))
    if os.path.exists(BACKUP_FILE):
        return send_file(BACKUP_FILE, as_attachment=True, download_name="repo-list-backup.jsonl")
    return "Backup file not found", 404

@app.route("/admin/view_backup")
def view_backup():
    admin_cookie = request.cookies.get("admin")
    if admin_cookie != "true":
        return redirect(url_for("admin_login"))
    page = int(request.args.get("page", 1))
    per_page = 10
    backup_entries = []
    total_entries = 0

    if os.path.exists(BACKUP_FILE):
        with open(BACKUP_FILE, 'r') as f:
            lines = f.readlines()
            total_entries = len(lines)
            start = (page - 1) * per_page
            end = start + per_page
            for line in lines[start:end]:
                try:
                    entry = json.loads(line.strip())
                    if entry.get("created_at") and entry["created_at"] != "Unknown":
                        try:
                            dt = datetime.fromisoformat(entry["created_at"].replace("Z", "+00:00"))
                            entry["created_at"] = format_timestamp(dt)
                        except ValueError:
                            pass
                    backup_entries.append(entry)
                except:
                    continue

    total_pages = (total_entries + per_page - 1) // per_page
    return render_template("view_backup.html", entries=backup_entries, page=page, total_pages=total_pages)

@app.route("/admin/view_logs")
def view_logs():
    admin_cookie = request.cookies.get("admin")
    if admin_cookie != "true":
        return redirect(url_for("admin_login"))
    page = int(request.args.get("page", 1))
    per_page = 10
    log_entries = []
    total_entries = 0

    if os.path.exists(CLIENT_LOG_FILE):
        with open(CLIENT_LOG_FILE, 'r') as f:
            lines = f.readlines()
            total_entries = len(lines)
            start = (page - 1) * per_page
            end = start + per_page
            for line in lines[start:end]:
                try:
                    entry = json.loads(line.strip())
                    if entry.get("timestamp"):
                        try:
                            dt = datetime.strptime(entry["timestamp"], "%Y-%m-%d %H:%M:%S")
                            entry["timestamp"] = format_timestamp(dt)
                        except ValueError:
                            pass
                    log_entries.append(entry)
                except:
                    continue

    total_pages = (total_entries + per_page - 1) // per_page
    return render_template("view_logs.html", entries=log_entries, page=page, total_pages=total_pages)

@app.route("/admin/export_recent/<format>", methods=["POST"])
def export_recent_repos(format):
    admin_cookie = request.cookies.get("admin")
    if admin_cookie != "true":
        return jsonify({"error": "Unauthorized"}), 403
    with lock:
        repos = list(recent_repos)
    if not repos:
        return "No data to export", 400
    
    if format == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["name", "creator", "followers", "created_at", "url", "description"])
        writer.writeheader()
        writer.writerows(repos)
        return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment;filename=recent_repos.csv"})
    
    elif format == "json":
        output = io.StringIO()
        json.dump(repos, output, indent=2)
        return Response(output.getvalue(), mimetype="application/json", headers={"Content-Disposition": "attachment;filename=recent_repos.json"})
    
    elif format == "txt":
        output = "\n".join([f"{repo['name']} | {repo['creator']} | Followers: {repo['followers']} | {repo['created_at']} | {repo['url']} | {repo['description']}" for repo in repos])
        return Response(output, mimetype="text/plain", headers={"Content-Disposition": "attachment;filename=recent_repos.txt"})
    
    return "Invalid format", 400

@app.route("/events")
def get_events():
    with lock:
        return jsonify(list(recent_repos))

@app.route("/status")
def get_status():
    with lock:
        return jsonify({
            "is_monitoring": is_monitoring,
            "last_update": last_update,
            "rate_limit": f"{rate_limit_remaining}/{total_requests_made}",
            "reset_time": rate_limit_reset,
            "total_repos": len(recent_repos)
        })

@app.route("/export/<format>", methods=["POST"])
def export_repos(format):
    client_ip = request.remote_addr
    user_agent = request.headers.get('User-Agent', 'Unknown')
    log_client_action(client_ip, f"exported_{format}", user_agent)
    filtered_repos = request.json
    if not filtered_repos:
        return "No data to export", 400
    
    if format == "csv":
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["name", "creator", "followers", "created_at", "url", "description"])
        writer.writeheader()
        writer.writerows(filtered_repos)
        return Response(output.getvalue(), mimetype="text/csv", headers={"Content-Disposition": "attachment;filename=repos.csv"})
    
    elif format == "json":
        output = io.StringIO()
        json.dump(filtered_repos, output, indent=2)
        return Response(output.getvalue(), mimetype="application/json", headers={"Content-Disposition": "attachment;filename=repos.json"})
    
    elif format == "txt":
        output = "\n".join([f"{repo['name']} | {repo['creator']} | Followers: {repo['followers']} | {repo['created_at']} | {repo['url']} | {repo['description']}" for repo in filtered_repos])
        return Response(output, mimetype="text/plain", headers={"Content-Disposition": "attachment;filename=repos.txt"})
    
    return "Invalid format", 400

if __name__ == "__main__":
    print(f"Starting... Open http://127.0.0.1:5000 in your browser. Monitoring is {'on' if is_monitoring else 'off'}.")
    app.run(host="0.0.0.0", port=5000)