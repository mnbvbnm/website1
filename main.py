from flask import (
    Flask,
    request,
    jsonify,
    render_template,
    send_from_directory,
    session,
    redirect,
    url_for
)
import json
import os
import secrets
import hmac
import shutil
import threading
import requests
import re
from flask_compress import Compress
from collections import OrderedDict
from flask import Response
from datetime import datetime
from dotenv import load_dotenv
from functools import wraps
load_dotenv()
app = Flask(__name__)

app.config.update(
    SECRET_KEY=os.getenv("SECRET_KEY", secrets.token_hex(32)),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax"
)

Compress(app)


DATA_DIR = "json"
DATA_FILE = os.path.join(DATA_DIR, "data.json")
DATA_BACKUP_FILE = os.path.join(DATA_DIR, "data.backup.json")
DATA_BACKUPS_DIR = os.path.join(DATA_DIR, "backups")
DATA_BEFORE_RESTORE_FILE = os.path.join(
    DATA_DIR,
    "data.before_restore.json"
)
ADMIN_PASSWORD = os.getenv(
    "STREAM_ZONE_ADMIN_PASSWORD",
    ""
)

DATA_LOCK = threading.RLock()

EPISODE_CODE_RE = re.compile(
    r"^(?P<series_code>.+)S(?P<season>\d+)E(?P<episode>\d+)$"
)

with open("json/trailer_ids.json", "r", encoding="utf-8") as f:
    trailer_map = {entry["title"]: entry["trailer_id"] for entry in json.load(f)}

# Load data once at server startup
with open(DATA_FILE, "r", encoding="utf-8") as f:
    data_json = json.load(f)

with open("json/film_series_info.json", "r", encoding="utf-8") as f:
    info_dict = json.load(f)

def ensure_csrf_token():
    token = session.get("csrf_token")

    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token

    return token


def csrf_is_valid(token):
    expected = session.get("csrf_token", "")

    return bool(
        token and
        expected and
        hmac.compare_digest(str(token), str(expected))
    )


def admin_required(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        if not session.get("stream_zone_admin"):
            if request.path.startswith("/api/"):
                return jsonify({
                    "error": "Admin login required."
                }), 401

            return redirect(url_for("admin_login"))

        return function(*args, **kwargs)

    return wrapper


def read_data_file():
    with open(DATA_FILE, "r", encoding="utf-8") as file:
        data = json.load(file)

    if not isinstance(data, list):
        raise ValueError(
            "data.json must contain a JSON array."
        )

    return data


def format_data_json(data):
    lines = ["["]

    for index, entry in enumerate(data):
        comma = "," if index < len(data) - 1 else ""

        if not isinstance(entry, dict) or len(entry) != 1:
            value = json.dumps(
                entry,
                ensure_ascii=False
            )

            lines.append(f"  {value}{comma}")
            continue

        key, value = next(iter(entry.items()))

        encoded_key = json.dumps(
            key,
            ensure_ascii=False
        )

        if not isinstance(value, dict):
            encoded_value = json.dumps(
                value,
                ensure_ascii=False
            )

            lines.append(
                f"  {{{encoded_key}: {encoded_value}}}{comma}"
            )
            continue

        lines.append(f"  {{{encoded_key}: {{")

        fields = list(value.items())

        for field_index, (field, field_value) in enumerate(fields):
            field_comma = (
                ","
                if field_index < len(fields) - 1
                else ""
            )

            encoded_field = json.dumps(
                field,
                ensure_ascii=False
            )

            encoded_value = json.dumps(
                field_value,
                ensure_ascii=False
            )

            lines.append(
                f"      {encoded_field}: "
                f"{encoded_value}{field_comma}"
            )

        lines.append(f"    }}}}{comma}")

    lines.append("]")

    return "\n".join(lines) + "\n"


def write_data_file(data):
    temporary_file = DATA_FILE + ".tmp"

    if os.path.exists(DATA_FILE):
        shutil.copyfile(
            DATA_FILE,
            DATA_BACKUP_FILE
        )

    with open(
        temporary_file,
        "w",
        encoding="utf-8",
        newline="\n"
    ) as file:
        file.write(format_data_json(data))
        file.flush()
        os.fsync(file.fileno())

    os.replace(
        temporary_file,
        DATA_FILE
    )


def build_series_title_map(data):
    result = {}

    for entry in data:
        if not isinstance(entry, dict) or len(entry) != 1:
            continue

        key, value = next(iter(entry.items()))

        if EPISODE_CODE_RE.fullmatch(str(key)):
            continue

        if isinstance(value, str) and value:
            result.setdefault(value, key)

    return result


def normalize_episode(value):
    if isinstance(value, str):
        return {
            "link": value,
            "video_provider": "drive",
            "video_embed_url": "",
            "description": "",
            "rating": "",
            "image": "",
            "status": "published"
        }

    if isinstance(value, dict):
        return {
            "link": str(value.get("link", "") or ""),

            "video_provider": str(
                value.get("video_provider", "drive") or "drive"
            ),

            "video_embed_url": str(
                value.get("video_embed_url", "") or ""
            ),

            "description": str(
                value.get("description", "") or ""
            ),

            "rating": str(
                value.get("rating", "") or ""
            ),

            "image": str(
                value.get("image", "") or ""
            ),

            "status": str(
                value.get("status", "published") or "published"
            )
        }

    return {
        "link": "",
        "video_provider": "drive",
        "video_embed_url": "",
        "description": "",
        "rating": "",
        "image": "",
        "status": "draft"
    }


def create_episode_record(code, value, title_map):
    match = EPISODE_CODE_RE.fullmatch(code)

    if not match:
        return None

    episode = normalize_episode(value)
    series_code = match.group("series_code")

    return {
        "code": code,
        "series_code": series_code,
        "series_title": title_map.get(
            series_code,
            series_code
        ),
        "season": int(match.group("season")),
        "episode": int(match.group("episode")),
        **episode
    }


def validate_rating(value):
    rating = str(value or "").strip()

    if not rating:
        return ""

    try:
        number = float(rating)
    except ValueError:
        raise ValueError(
            "Rating must be a number."
        )

    if number < 0 or number > 10:
        raise ValueError(
            "Rating must be between 0 and 10."
        )

    return rating
@app.route(
    "/admin/login",
    methods=["GET", "POST"]
)
def admin_login():
    if session.get("stream_zone_admin"):
        return redirect(
            url_for("admin_episodes_page")
        )

    error = None
    csrf_token = ensure_csrf_token()

    if request.method == "POST":
        submitted_token = request.form.get(
            "csrf_token",
            ""
        )

        if not csrf_is_valid(submitted_token):
            return "Invalid security token.", 400

        password = request.form.get(
            "password",
            ""
        )

        if not ADMIN_PASSWORD:
            error = (
                "STREAM_ZONE_ADMIN_PASSWORD "
                "is not configured."
            )

        elif hmac.compare_digest(
            password,
            ADMIN_PASSWORD
        ):
            session.clear()
            session["stream_zone_admin"] = True
            session["csrf_token"] = (
                secrets.token_urlsafe(32)
            )

            return redirect(
                url_for("admin_episodes_page")
            )

        else:
            error = "Incorrect admin password."

    return render_template(
        "admin_login.html",
        error=error,
        csrf_token=csrf_token,
        config_missing=not bool(ADMIN_PASSWORD)
    )


@app.route(
    "/admin/logout",
    methods=["POST"]
)
@admin_required
def admin_logout():
    token = request.form.get(
        "csrf_token",
        ""
    )

    if not csrf_is_valid(token):
        return "Invalid security token.", 400

    session.clear()

    return redirect(
        url_for("admin_login")
    )


@app.route("/admin/episodes")
@admin_required
def admin_episodes_page():
    return render_template(
        "admin_episodes.html",
        csrf_token=ensure_csrf_token()
    )


@app.route("/api/admin/episodes")
@admin_required
def admin_episodes_api():
    try:
        with DATA_LOCK:
            data = read_data_file()

    except Exception as error:
        return jsonify({
            "error": f"Could not read data.json: {error}"
        }), 500

    title_map = build_series_title_map(data)
    episodes = []

    for entry in data:
        if not isinstance(entry, dict) or len(entry) != 1:
            continue

        code, value = next(iter(entry.items()))

        record = create_episode_record(
            str(code),
            value,
            title_map
        )

        if record:
            episodes.append(record)

    episodes.sort(
        key=lambda item: (
            item["series_title"].lower(),
            item["season"],
            item["episode"]
        )
    )

    response = jsonify({
        "episodes": episodes,
        "total": len(episodes)
    })

    response.headers["Cache-Control"] = "no-store"

    return response


@app.route(
    "/api/admin/episodes/<code>",
    methods=["PUT"]
)
@admin_required
def admin_update_episode(code):
    csrf_token = request.headers.get(
        "X-CSRF-Token",
        ""
    )

    if not csrf_is_valid(csrf_token):
        return jsonify({
            "error": "Invalid security token."
        }), 400

    if not EPISODE_CODE_RE.fullmatch(code):
        return jsonify({
            "error": "Invalid episode code."
        }), 400

    payload = request.get_json(silent=True)

    if not isinstance(payload, dict):
        return jsonify({
            "error": "JSON data is required."
        }), 400

    try:
        rating = validate_rating(
            payload.get("rating", "")
        )
    except ValueError as error:
        return jsonify({
            "error": str(error)
        }), 400

    video_provider = str(
        payload.get("video_provider", "drive") or "drive"
    ).strip().lower()

    allowed_providers = {
        "drive",
        "bunny",
        "cloudflare",
        "direct"
    }

    if video_provider not in allowed_providers:
        return jsonify({
            "error": "Invalid video provider."
        }), 400

    status = str(
        payload.get("status", "published") or "published"
    ).strip().lower()

    allowed_statuses = {
        "draft",
        "needs_review",
        "published",
        "rejected"
    }

    if status not in allowed_statuses:
        return jsonify({
            "error": "Invalid episode status."
        }), 400

    values = {
        "link": str(
            payload.get("link", "") or ""
        ).strip(),

        "video_provider": video_provider,

        "video_embed_url": str(
            payload.get("video_embed_url", "") or ""
        ).strip(),

        "description": str(
            payload.get("description", "") or ""
        ).strip(),

        "rating": rating,

        "image": str(
            payload.get("image", "") or ""
        ).strip(),

        "status": status
    }

    global data_json

    try:
        with DATA_LOCK:
            data = read_data_file()
            target = None

            for entry in data:
                if (
                    isinstance(entry, dict)
                    and code in entry
                ):
                    target = entry
                    break

            if target is None:
                return jsonify({
                    "error": f"{code} was not found."
                }), 404

            old_value = target.get(code)
            extra_fields = {}

            if isinstance(old_value, dict):
                extra_fields = {
                    key: value
                    for key, value in old_value.items()
                    if key not in {
                        "link",
                        "video_provider",
                        "video_embed_url",
                        "description",
                        "rating",
                        "image",
                        "status"
                    }
                }

            target[code] = {
                "link": values["link"],
                "video_provider": values["video_provider"],
                "video_embed_url": values["video_embed_url"],
                "description": values["description"],
                "rating": values["rating"],
                "image": values["image"],
                "status": values["status"],
                **extra_fields
            }

            write_data_file(data)

            # Update the globally cached data too.
            data_json = data

            title_map = build_series_title_map(data)

            episode = create_episode_record(
                code,
                target[code],
                title_map
            )

    except Exception as error:
        return jsonify({
            "error": f"Could not save data.json: {error}"
        }), 500

    return jsonify({
        "status": "success",
        "episode": episode
    })
@app.route("/admin/films")
@admin_required
def admin_films_page():
    return render_template(
        "admin_films.html",
        csrf_token=ensure_csrf_token()
    )


def get_film_code_map(data):
    with open(
        os.path.join(DATA_DIR, "film_categories.json"),
        "r",
        encoding="utf-8"
    ) as file:
        film_categories = json.load(
            file,
            object_pairs_hook=OrderedDict
        )

    result = OrderedDict()

    for title in film_categories.keys():
        film_code = ""

        for entry in data:
            if (
                isinstance(entry, dict)
                and title in entry
            ):
                film_code = str(entry[title])
                break

        if film_code:
            result[title] = film_code

    return result


def find_data_entry_by_key(data, target_key):
    for entry in data:
        if (
            isinstance(entry, dict)
            and target_key in entry
        ):
            return entry

    return None
def extract_iframe_src(value):
    text = str(value or "").strip()

    if not text:
        return ""

    match = re.search(
        r'src=["\']([^"\']+)["\']',
        text,
        flags=re.IGNORECASE
    )

    if match:
        return match.group(1).strip()

    return text


def is_direct_video_url(value):
    text = str(value or "").strip().lower()

    return text.endswith((
        ".mp4",
        ".webm",
        ".ogg",
        ".m3u8"
    ))


def normalize_film_video(value):
    if isinstance(value, str):
        link = value.strip()

        if is_direct_video_url(link):
            return {
                "link": link,
                "video_provider": "direct",
                "video_embed_url": link,
                "status": "published"
            }

        return {
            "link": link,
            "video_provider": "drive",
            "video_embed_url": "",
            "status": "published"
        }

    if isinstance(value, dict):
        link = str(
            value.get("link", "") or ""
        ).strip()

        provider = str(
            value.get("video_provider", "drive") or "drive"
        ).strip().lower()

        embed_url = extract_iframe_src(
            value.get("video_embed_url", "") or ""
        )

        status = str(
            value.get("status", "published") or "published"
        ).strip().lower()

        return {
            "link": link,
            "video_provider": provider,
            "video_embed_url": embed_url,
            "status": status
        }

    return {
        "link": "",
        "video_provider": "drive",
        "video_embed_url": "",
        "status": "draft"
    }


def validate_video_provider(value):
    provider = str(
        value or "drive"
    ).strip().lower()

    allowed = {
        "drive",
        "bunny",
        "cloudflare",
        "direct"
    }

    if provider not in allowed:
        raise ValueError(
            "Invalid video provider."
        )

    return provider


def validate_publish_status(value):
    status = str(
        value or "published"
    ).strip().lower()

    allowed = {
        "draft",
        "needs_review",
        "published",
        "rejected"
    }

    if status not in allowed:
        raise ValueError(
            "Invalid publish status."
        )

    return status


def should_store_film_as_plain_string(film_video):
    return (
        film_video["video_provider"] == "drive"
        and not film_video["video_embed_url"]
        and film_video["status"] == "published"
    )

@app.route("/api/admin/films-links")
@admin_required
def admin_films_links_api():
    try:
        with DATA_LOCK:
            data = read_data_file()

        film_map = get_film_code_map(data)

        with open(
            os.path.join(DATA_DIR, "film_series_info.json"),
            "r",
            encoding="utf-8"
        ) as file:
            info = json.load(file)

        films = []

        for title, code in film_map.items():
            link_entry = find_data_entry_by_key(
                data,
                code
            )

            film_video = {
                "link": "",
                "video_provider": "drive",
                "video_embed_url": "",
                "status": "published"
            }

            if link_entry:
                raw_value = link_entry.get(code, "")

                if isinstance(raw_value, str):
                    film_video = {
                        "link": raw_value,
                        "video_provider": "drive",
                        "video_embed_url": "",
                        "status": "published"
                    }

                elif isinstance(raw_value, dict):
                    film_video = {
                        "link": str(
                            raw_value.get("link", "") or ""
                        ),

                        "video_provider": str(
                            raw_value.get("video_provider", "drive") or "drive"
                        ),

                        "video_embed_url": str(
                            raw_value.get("video_embed_url", "") or ""
                        ),

                        "status": str(
                            raw_value.get("status", "published") or "published"
                        )
                    }

            details = info.get(code, {})

            films.append({
                "title": title,
                "code": code,
                "link": film_video["link"],
                "video_provider": film_video["video_provider"],
                "video_embed_url": film_video["video_embed_url"],
                "status": film_video["status"],
                "year": details.get("year", ""),
                "rating": details.get("rating", ""),
                "duration": details.get("duration", ""),
                "age_rating": details.get("age_rating", "")
            })

    except Exception as error:
        return jsonify({
            "error": f"Could not load film links: {error}"
        }), 500

    response = jsonify({
        "films": films,
        "total": len(films)
    })

    response.headers["Cache-Control"] = "no-store"

    return response
@app.route(
    "/api/admin/films/<code>/link",
    methods=["PUT"]
)
@admin_required
def admin_update_film_link(code):
    csrf_token = request.headers.get(
        "X-CSRF-Token",
        ""
    )

    if not csrf_is_valid(csrf_token):
        return jsonify({
            "error": "Invalid security token."
        }), 400

    payload = request.get_json(silent=True)

    if not isinstance(payload, dict):
        return jsonify({
            "error": "JSON data is required."
        }), 400

    try:
        provider = validate_video_provider(
            payload.get("video_provider", "drive")
        )

        status = validate_publish_status(
            payload.get("status", "published")
        )

    except ValueError as error:
        return jsonify({
            "error": str(error)
        }), 400

    link = str(
        payload.get("link", "") or ""
    ).strip()

    video_embed_url = extract_iframe_src(
        payload.get("video_embed_url", "") or ""
    )

    # If provider is direct and embed URL is empty,
    # allow using the main link as the direct video URL.
    if (
        provider == "direct"
        and not video_embed_url
        and is_direct_video_url(link)
    ):
        video_embed_url = link

    film_video = {
        "link": link,
        "video_provider": provider,
        "video_embed_url": video_embed_url,
        "status": status
    }

    global data_json

    try:
        with DATA_LOCK:
            data = read_data_file()

            film_map = get_film_code_map(data)

            if code not in film_map.values():
                return jsonify({
                    "error": "Film code was not found."
                }), 404

            target = find_data_entry_by_key(
                data,
                code
            )

            if target is None:
                target = {}
                data.append(target)

            if should_store_film_as_plain_string(film_video):
                target[code] = film_video["link"]
            else:
                target[code] = {
                    "link": film_video["link"],
                    "video_provider": film_video["video_provider"],
                    "video_embed_url": film_video["video_embed_url"],
                    "status": film_video["status"]
                }

            write_data_file(data)
            data_json = data

    except Exception as error:
        return jsonify({
            "error": f"Could not save film link: {error}"
        }), 500

    return jsonify({
        "status": "success",
        "code": code,
        **film_video
    })
def ensure_backups_dir():
    os.makedirs(
        DATA_BACKUPS_DIR,
        exist_ok=True
    )


def make_backup_filename():
    timestamp = datetime.now().strftime(
        "%Y-%m-%d_%H-%M-%S"
    )

    return f"data_{timestamp}.json"


def get_backup_history():
    ensure_backups_dir()

    backups = []

    for filename in os.listdir(DATA_BACKUPS_DIR):
        if not (
            filename.startswith("data_")
            and filename.endswith(".json")
        ):
            continue

        path = os.path.join(
            DATA_BACKUPS_DIR,
            filename
        )

        backups.append({
            "filename": filename,
            "size": os.path.getsize(path),
            "modified": datetime.fromtimestamp(
                os.path.getmtime(path)
            ).strftime("%Y-%m-%d %H:%M:%S")
        })

    backups.sort(
        key=lambda item: item["modified"],
        reverse=True
    )

    return backups


def validate_backup_filename(filename):
    filename = str(filename or "").strip()

    if (
        not filename.startswith("data_")
        or not filename.endswith(".json")
        or "/" in filename
        or "\\" in filename
        or ".." in filename
    ):
        return ""

    return filename
@app.route("/admin/backups")
@admin_required
def admin_backups_page():
    return render_template(
        "admin_backups.html",
        csrf_token=ensure_csrf_token()
    )


@app.route("/api/admin/backups/status")
@admin_required
def admin_backups_status():
    def file_info(path):
        exists = os.path.exists(path)

        return {
            "exists": exists,
            "size": os.path.getsize(path) if exists else 0,
            "modified": (
                datetime.fromtimestamp(
                    os.path.getmtime(path)
                ).strftime("%Y-%m-%d %H:%M:%S")
                if exists
                else ""
            )
        }

    return jsonify({
        "current": file_info(DATA_FILE),
        "backup": file_info(DATA_BACKUP_FILE),
        "before_restore": file_info(DATA_BEFORE_RESTORE_FILE),
        "history": get_backup_history()
    })
@app.route("/admin/backups/history/<filename>/download")
@admin_required
def admin_download_history_backup(filename):
    safe_filename = validate_backup_filename(filename)

    if not safe_filename:
        return "Invalid backup file.", 400

    backup_path = os.path.join(
        DATA_BACKUPS_DIR,
        safe_filename
    )

    if not os.path.exists(backup_path):
        return "Backup file was not found.", 404

    return send_from_directory(
        DATA_BACKUPS_DIR,
        safe_filename,
        as_attachment=True,
        download_name=safe_filename
    )
@app.route(
    "/api/admin/backups/history/<filename>/restore",
    methods=["POST"]
)
@admin_required
def admin_restore_history_backup(filename):
    csrf_token = request.headers.get(
        "X-CSRF-Token",
        ""
    )

    if not csrf_is_valid(csrf_token):
        return jsonify({
            "error": "Invalid security token."
        }), 400

    safe_filename = validate_backup_filename(filename)

    if not safe_filename:
        return jsonify({
            "error": "Invalid backup file."
        }), 400

    payload = request.get_json(silent=True)

    if not isinstance(payload, dict):
        return jsonify({
            "error": "JSON data is required."
        }), 400

    confirmation = str(
        payload.get("confirmation", "") or ""
    ).strip()

    if confirmation != "RESTORE":
        return jsonify({
            "error": "Type RESTORE to confirm."
        }), 400

    backup_path = os.path.join(
        DATA_BACKUPS_DIR,
        safe_filename
    )

    if not os.path.exists(backup_path):
        return jsonify({
            "error": "Backup file was not found."
        }), 404

    global data_json

    try:
        with DATA_LOCK:
            with open(
                backup_path,
                "r",
                encoding="utf-8"
            ) as backup_file:
                backup_data = json.load(backup_file)

            if not isinstance(backup_data, list):
                return jsonify({
                    "error": "Backup file is not a valid data.json array."
                }), 400

            if os.path.exists(DATA_FILE):
                shutil.copyfile(
                    DATA_FILE,
                    DATA_BEFORE_RESTORE_FILE
                )

            shutil.copyfile(
                backup_path,
                DATA_FILE
            )

            shutil.copyfile(
                backup_path,
                DATA_BACKUP_FILE
            )

            data_json = backup_data

    except Exception as error:
        return jsonify({
            "error": f"Could not restore selected backup: {error}"
        }), 500

    return jsonify({
        "status": "success",
        "message": "Selected backup restored successfully.",
        "restored_from": safe_filename
    })
@app.route("/admin/backups/download/current")
@admin_required
def admin_download_current_data():
    if not os.path.exists(DATA_FILE):
        return "data.json was not found.", 404

    return send_from_directory(
        DATA_DIR,
        "data.json",
        as_attachment=True,
        download_name="data.json"
    )


@app.route("/admin/backups/download/backup")
@admin_required
def admin_download_backup_data():
    if not os.path.exists(DATA_BACKUP_FILE):
        return "data.backup.json was not found.", 404

    return send_from_directory(
        DATA_DIR,
        "data.backup.json",
        as_attachment=True,
        download_name="data.backup.json"
    )


@app.route("/api/admin/backups/create", methods=["POST"])
@admin_required
def admin_create_manual_backup():
    csrf_token = request.headers.get(
        "X-CSRF-Token",
        ""
    )

    if not csrf_is_valid(csrf_token):
        return jsonify({
            "error": "Invalid security token."
        }), 400

    if not os.path.exists(DATA_FILE):
        return jsonify({
            "error": "data.json was not found."
        }), 404

    try:
        with DATA_LOCK:
            ensure_backups_dir()

            # Keep the latest simple backup.
            shutil.copyfile(
                DATA_FILE,
                DATA_BACKUP_FILE
            )

            # Also create timestamped backup history.
            backup_filename = make_backup_filename()

            backup_path = os.path.join(
                DATA_BACKUPS_DIR,
                backup_filename
            )

            shutil.copyfile(
                DATA_FILE,
                backup_path
            )

    except Exception as error:
        return jsonify({
            "error": f"Could not create backup: {error}"
        }), 500

    return jsonify({
        "status": "success",
        "message": "Backup created successfully.",
        "backup_filename": backup_filename
    })

@app.route("/api/admin/backups/restore", methods=["POST"])
@admin_required
def admin_restore_backup():
    csrf_token = request.headers.get(
        "X-CSRF-Token",
        ""
    )

    if not csrf_is_valid(csrf_token):
        return jsonify({
            "error": "Invalid security token."
        }), 400

    payload = request.get_json(silent=True)

    if not isinstance(payload, dict):
        return jsonify({
            "error": "JSON data is required."
        }), 400

    confirmation = str(
        payload.get("confirmation", "") or ""
    ).strip()

    if confirmation != "RESTORE":
        return jsonify({
            "error": "Type RESTORE to confirm."
        }), 400

    if not os.path.exists(DATA_BACKUP_FILE):
        return jsonify({
            "error": "data.backup.json was not found."
        }), 404

    global data_json

    try:
        with DATA_LOCK:
            # Validate backup JSON before replacing anything.
            with open(
                DATA_BACKUP_FILE,
                "r",
                encoding="utf-8"
            ) as backup_file:
                backup_data = json.load(backup_file)

            if not isinstance(backup_data, list):
                return jsonify({
                    "error": "Backup file is not a valid data.json array."
                }), 400

            # Save current data.json before restore.
            if os.path.exists(DATA_FILE):
                shutil.copyfile(
                    DATA_FILE,
                    DATA_BEFORE_RESTORE_FILE
                )

            # Restore backup into data.json.
            shutil.copyfile(
                DATA_BACKUP_FILE,
                DATA_FILE
            )

            # Update cached data for routes that use data_json.
            data_json = backup_data

    except Exception as error:
        return jsonify({
            "error": f"Could not restore backup: {error}"
        }), 500

    return jsonify({
        "status": "success",
        "message": "Backup restored successfully.",
        "restored_from": DATA_BACKUP_FILE,
        "safety_copy": DATA_BEFORE_RESTORE_FILE
    })

@app.route("/")
def serve_home():
    return render_template("home.html" ,current_year=datetime.now().year)


import requests
@app.route("/api/films")
def get_films():
    with open(os.path.join(DATA_DIR, "film_categories.json"), "r", encoding="utf-8") as f:
        categories = json.load(f, object_pairs_hook=OrderedDict)

    with open(os.path.join(DATA_DIR, "film_series_info.json"), "r", encoding="utf-8") as f:
        info_dict = json.load(f)

    result = OrderedDict()

    for title, category_genres in categories.items():
        film_id = None

        for entry in data_json:
            if title in entry:
                film_id = entry[title]
                break

        details = info_dict.get(film_id, {})

        result[title] = {
            "title": title,
            "genres": details.get("genres", category_genres[:-1]),
            "year": details.get("year", category_genres[-1] if category_genres else ""),
            "rating": details.get("rating", "⭐ N/A"),
            "duration": details.get("duration", ""),
            "description": details.get("description", ""),
            "age_rating": details.get("age_rating", "")
        }

    return Response(
        json.dumps(result, ensure_ascii=False),
        mimetype="application/json"
    )

@app.route("/api/series")
def get_series():
    with open(os.path.join(DATA_DIR, "series_categories.json"), "r", encoding="utf-8") as f:
        categories = json.load(f, object_pairs_hook=OrderedDict)

    with open(os.path.join(DATA_DIR, "film_series_info.json"), "r", encoding="utf-8") as f:
        info_dict = json.load(f)

    result = OrderedDict()

    for title, category_genres in categories.items():
        series_id = None

        for entry in data_json:
            if title in entry:
                series_id = entry[title]
                break

        details = info_dict.get(series_id, {})

        result[title] = {
            "title": title,
            "genres": details.get("genres", category_genres[:-1]),
            "year": details.get("year", category_genres[-1] if category_genres else ""),
            "rating": details.get("rating", "⭐ N/A"),
            "episodes": details.get("episodes", ""),
            "description": details.get("description", ""),
            "age_rating": details.get("age_rating", "")
        }

    return Response(
        json.dumps(result, ensure_ascii=False),
        mimetype="application/json"
    )

@app.route("/api/episodes")
def get_episodes():
    with open(os.path.join(DATA_DIR, "data.json"), "r", encoding="utf-8") as f:
        return jsonify(json.load(f))

@app.route("/save", methods=["POST"])
def save_user_info():
    data = request.json
    user_id = str(data.get("user_id"))
    device = data.get("device")
    ip_address = requests.get("https://api.ipify.org").text

    print(device)
    # Get country from IP using ipinfo.io (or similar)
    try:
        ip_info = requests.get(f"https://ipinfo.io/{ip_address}/json").json()
        country = ip_info.get("country", "Unknown")
        print(ip_info)
        print(country)
    except:
        country = "Unknown"

    try:
        with open("json/user_history.json", "r", encoding="utf-8") as f:
            history = json.load(f)
    except:
        history = {}

    if user_id not in history:
        history[user_id] = {
            "first_use": "",
            "country":"",
            "ip":"",
            "device":"",
            "banned": [],
            "unbanned": [],
            "last_active": ""
        }

    history[user_id]["ip"] = ip_address
    history[user_id]["device"] = device
    history[user_id]["country"] = country

    with open("json/user_history.json", "w", encoding="utf-8") as f:
        json.dump(history, f, indent=4)

    return jsonify({"status": "success", "ip": ip_address, "country": country})

from information import *


@app.route("/film/<title>")
def film_detail(title):
    title = title.strip()

    film_id = None
    watch_link = ""

    # Always read the latest data.json.
    # This avoids using old cached data_json after you edit the file.
    try:
        data = read_data_file()
    except Exception as error:
        return f"Failed to load data.json: {error}", 500

    # Step 1:
    # Find the film ID from the title.
    # Example: {"Shelter": "S71011"}
    for entry in data:
        if not isinstance(entry, dict):
            continue

        if title in entry:
            film_id = str(entry.get(title, "") or "").strip()
            break

    if not film_id:
        return f"Film '{title}' not found.", 404

    # Step 2:
    # Find the film link object from the film ID.
    # Example:
    # {"S71011": {"link": "...", "video_provider": "drive"}}
    for entry in data:
        if not isinstance(entry, dict):
            continue

        if film_id not in entry:
            continue

        raw_value = entry.get(film_id)

        # Old system:
        # {"S71011": "https://drive.google.com/open?id=..."}
        if isinstance(raw_value, str):
            watch_link = raw_value.strip()

        # New system:
        # {"S71011": {"link": "https://drive.google.com/open?id=..."}}
        elif isinstance(raw_value, dict):
            watch_link = str(
                raw_value.get("link", "") or ""
            ).strip()

        break

    try:
        with open("json/film_series_info.json", "r", encoding="utf-8") as f:
            info_dict = json.load(f)
    except:
        return "Failed to load film info.", 500

    if film_id not in info_dict:
        return f"Film '{title}' not found.", 404

    details = info_dict[film_id]
    genres = details.get("genres", [])

    print("FILM DEBUG:", title, film_id, repr(watch_link))

    return render_template(
        "film.html",
        title=title,
        genres=genres,
        details=details,
        watch_link=watch_link,
        trailer_id=trailer_map.get(title),
        current_year=datetime.now().year
    )


@app.route("/series/<title>")
def series_detail(title):
    title = title.strip()

    series_id = None
    watch_link = None

    for entry in data_json:
        if title in entry:
            series_id = entry[title]
        if series_id and series_id in entry:
            watch_link = entry[series_id]

    try:
        with open("json/film_series_info.json", "r", encoding="utf-8") as f:
            info_dict = json.load(f)
    except:
        return "Failed to load series info.", 500

    if not series_id or series_id not in info_dict:
        return f"Series '{title}' not found.", 404

    details = info_dict[series_id]

    genres = details.get("genres", [])

    return render_template("series.html",
                           title=title,
                           genres=genres,
                           details=details,
                           watch_link=watch_link,
                           series_code=series_id,
                           trailer_id=trailer_map.get(title))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
