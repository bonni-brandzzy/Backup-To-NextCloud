import base64
import json
import os
import re
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote
import http.client

CONFIG_FILE = "config.json"
_config = None


def _project_root():
    return Path(__file__).resolve().parent


def load_config():
    global _config
    if _config is not None:
        return _config
    base = _project_root()
    path = base / CONFIG_FILE
    _config = {}
    if not path.is_file():
        return _config
    with open(path, encoding="utf-8") as f:
        _config = json.load(f)
    return _config


def _get_projects():
    """Return list of project dicts from config['projects']."""
    cfg = load_config()
    projects = cfg.get("projects") or []
    return [p for p in projects if isinstance(p, dict) and p.get("name")]


def _get_project_files(project):
    """Paths to include for this project (list from project['files'])."""
    files = project.get("files")
    if isinstance(files, list):
        return [str(p).strip() for p in files if p and str(p).strip()]
    return []


def _get_project_database(project):
    """Database config for this project as dict or None if incomplete."""
    if not project:
        return None
    host = (project.get("database_host") or "").strip()
    database = (project.get("database_name") or "").strip()
    username = (project.get("database_username") or "").strip()
    if not host or not database or not username:
        return None
    return {
        "host": host,
        "port": str(project.get("database_port") or "3306").strip(),
        "database": database,
        "username": username,
        "password": (project.get("database_password") or "").strip(),
    }


def _get_temp_dir():
    """Directory for zip and temp files; created if needed, resolved against project root if relative."""
    cfg = load_config()
    backup = cfg.get("backup") or {}
    temp = (backup.get("temp_dir") or "storage/backup_temp").strip()
    base = _project_root()
    p = Path(temp)
    return p if p.is_absolute() else base / p


def _nextcloud_conn(project=None):
    """Return (url, remote_dir, auth). Uses project's nextcloud_backup_base_dir/nextcloud_backup_dir when given."""
    cfg = load_config()
    nc = cfg.get("nextcloud") or {}
    url = (nc.get("url") or "").replace("https://", "").replace("http://", "").strip("/")
    user = (nc.get("user") or "").strip()
    password = (nc.get("password") or "").strip()
    # "/" or empty for either is allowed; strip("/") normalizes and filter(None, ...) drops empty segments
    path1 = (project.get("nextcloud_backup_base_dir") or "").strip("/") if project else ""
    path2 = (project.get("nextcloud_backup_dir") or "").strip("/") if project else ""
    if not url or not user:
        return None, None, None
    parts = filter(None, [user.strip("/"), path1, path2])
    remote_dir = "/remote.php/dav/files/" + "/".join(quote(p, safe="") for p in parts)
    auth = base64.b64encode(f"{user}:{password}".encode()).decode()
    return url, remote_dir, auth


def backup_project(project):
    """Create one zip for this project: its files + its database dump. Returns path to the zip."""
    project_id = project.get("name", "unknown")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = _project_root()
    work = _get_temp_dir()
    work.mkdir(parents=True, exist_ok=True)
    zip_path = work / f"backup_{project_id}_{ts}.zip"
    files = _get_project_files(project)

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in files:
                full = Path(p) if Path(p).is_absolute() else base / p
                if not full.exists():
                    continue
                if full.is_file():
                    zf.write(full, p)
                else:
                    for root, _, filenames in os.walk(full):
                        for name in filenames:
                            fp = Path(root) / name
                            try:
                                arc = fp.relative_to(full)
                            except ValueError:
                                arc = fp.name
                            zf.write(fp, f"{full.name}/{arc}")

            db = _get_project_database(project)
            if db:
                sql_path = work / f"dump_{project_id}.sql"
                cmd = [
                    "mysqldump", "-h", db["host"], "-P", db["port"],
                    "-u", db["username"], db["database"]
                ]
                if db.get("password"):
                    cmd.insert(-1, f"--password={db['password']}")
                try:
                    with open(sql_path, "w") as out:
                        subprocess.run(cmd, stdout=out, stderr=subprocess.DEVNULL, check=True, cwd=base)
                    zf.write(sql_path, "dump.sql")
                except (subprocess.CalledProcessError, FileNotFoundError):
                    pass
    finally:
        if work.exists():
            for f in work.iterdir():
                if f != zip_path and f.is_file():
                    f.unlink(missing_ok=True)

    return str(zip_path)


def upload(zip_path, project=None):
    url, remote_dir, auth = _nextcloud_conn(project)
    if not url:
        return False, "Missing nextcloud settings in config.json"
    remote_path = remote_dir + "/" + quote(Path(zip_path).name, safe="")
    with open(zip_path, "rb") as f:
        body = f.read()
    conn = http.client.HTTPSConnection(url)
    conn.request("PUT", remote_path, body, {"Content-Type": "application/octet-stream", "Authorization": f"Basic {auth}"})
    res = conn.getresponse()
    data = res.read()
    code = res.status
    if code in (201, 204):
        return True, f"Upload OK ({code})"
    if code == 409:
        return False, "Parent folder does not exist (409)"
    if code == 403:
        return False, "Permission denied (403)"
    return False, f"HTTP {code}: {data.decode('utf-8', errors='replace')}"


def delete(zip_path):
    try:
        Path(zip_path).unlink(missing_ok=False)
        return True
    except FileNotFoundError:
        return False


def _date_from_backup_filename(name):
    """
    Parse date from backup filename like backup_othersite_20250209_120000.zip.
    Returns datetime in UTC, or None if the pattern doesn't match.
    """
    if not name or not isinstance(name, str):
        return None
    m = re.match(r"backup_.+_(\d{8})_(\d{6})\.zip$", name, re.IGNORECASE)
    if not m:
        return None
    try:
        dt = datetime.strptime(f"{m.group(1)}_{m.group(2)}", "%Y%m%d_%H%M%S")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def get_backup_files(project=None):
    url, remote_dir, auth = _nextcloud_conn(project)
    if not url:
        return []
    conn = http.client.HTTPSConnection(url)
    conn.request("PROPFIND", remote_dir + "/", "", {"Depth": "1", "Authorization": f"Basic {auth}"})
    res = conn.getresponse()
    data = res.read()
    if res.status not in (200, 207):
        return []
    ns = {"d": "DAV:"}
    root = ET.fromstring(data.decode("utf-8"))
    out = []
    for resp in root.findall(".//d:response", ns):
        href_el = resp.find("d:href", ns)
        prop = resp.find(".//d:prop", ns)
        if href_el is None or prop is None or href_el.text is None:
            continue
        href = href_el.text.rstrip("/")
        if href == remote_dir.rstrip("/") or href.endswith("/"):
            continue
        name = href.split("/")[-1]
        mod_dt = _date_from_backup_filename(name)
        if mod_dt is None:
            mod_el = prop.find("d:getlastmodified", ns)
            if mod_el is not None and mod_el.text:
                try:
                    mod_dt = parsedate_to_datetime(mod_el.text)
                except Exception:
                    mod_dt = None
            else:
                mod_dt = None
        out.append({"name": name, "last_modified": mod_dt})
    return out


def _gfs_to_keep(files_with_dates, now_utc, son_days, father_weeks, grandfather_months):
    """
    GFS retention: which backup names to keep.
    - Son: last son_days days, one most-recent backup per day.
    - Father: next father_weeks weeks, one most-recent backup per week.
    - Grandfather: next grandfather_months months, one most-recent backup per month.
    Returns set of backup names to keep.
    """
    to_keep = set()
    cutoff_son = now_utc - timedelta(days=son_days)
    cutoff_father = now_utc - timedelta(weeks=father_weeks)
    cutoff_grandfather = now_utc - timedelta(days=grandfather_months * 30)

    by_day = {}
    by_week = {}
    by_month = {}

    for item, mod_utc in files_with_dates:
        name = item.get("name")
        if not name or mod_utc is None:
            continue
        if mod_utc.tzinfo is None:
            mod_utc = mod_utc.replace(tzinfo=timezone.utc)
        else:
            mod_utc = mod_utc.astimezone(timezone.utc)

        if mod_utc >= cutoff_son:
            key = mod_utc.date()
            if key not in by_day or mod_utc > by_day[key][1]:
                by_day[key] = (item, mod_utc)
        elif mod_utc >= cutoff_father:
            key = (mod_utc.year, mod_utc.isocalendar()[1])
            if key not in by_week or mod_utc > by_week[key][1]:
                by_week[key] = (item, mod_utc)
        elif mod_utc >= cutoff_grandfather:
            key = (mod_utc.year, mod_utc.month)
            if key not in by_month or mod_utc > by_month[key][1]:
                by_month[key] = (item, mod_utc)

    for _, (item, _) in by_day.items():
        to_keep.add(item["name"])
    for _, (item, _) in by_week.items():
        to_keep.add(item["name"])
    for _, (item, _) in by_month.items():
        to_keep.add(item["name"])

    return to_keep


def delete_from_server():
    cfg = load_config()
    backup_cfg = cfg.get("backup") or {}
    gfs = backup_cfg.get("gfs")

    for project in _get_projects():
        url, remote_dir, auth = _nextcloud_conn(project)
        if not url:
            continue
        files = get_backup_files(project)
        now = datetime.now(timezone.utc)

        if gfs and isinstance(gfs, dict):
            son_days = int(gfs.get("son_days") or 7)
            father_weeks = int(gfs.get("father_weeks") or 4)
            grandfather_months = int(gfs.get("grandfather_months") or 12)
            files_with_dates = []
            for item in files:
                mod = item.get("last_modified")
                if mod is None:
                    mod_utc = now  # unknown date → treat as now so we keep it
                else:
                    mod_utc = mod.astimezone(timezone.utc) if mod.tzinfo else mod.replace(tzinfo=timezone.utc)
                files_with_dates.append((item, mod_utc))
            to_keep = _gfs_to_keep(files_with_dates, now, son_days, father_weeks, grandfather_months)
            for item in files:
                if item.get("name") not in to_keep:
                    conn = http.client.HTTPSConnection(url)
                    conn.request("DELETE", remote_dir + "/" + quote(item["name"], safe=""), "", {"Authorization": f"Basic {auth}"})
                    conn.getresponse().read()
        else:
            retention_days = int(backup_cfg.get("retention_days") or 7)
            for item in files:
                mod = item.get("last_modified")
                if mod is None:
                    continue
                mod_utc = mod.astimezone(timezone.utc) if mod.tzinfo else mod.replace(tzinfo=timezone.utc)
                if (now - mod_utc).days >= retention_days:
                    conn = http.client.HTTPSConnection(url)
                    conn.request("DELETE", remote_dir + "/" + quote(item["name"], safe=""), "", {"Authorization": f"Basic {auth}"})
                    conn.getresponse().read()


def main():
    projects = _get_projects()
    if not projects:
        print("No projects found in config.json. Add at least one entry to the 'projects' array.")
        return

    for project in projects:
        project_id = project.get("name", "unknown")
        print(f"Backing up project: {project_id}")
        zip_path = backup_project(project)
        success, message = upload(zip_path, project)
        if success:
            print(f"  {message}")
        else:
            print(f"  {message}")
        delete(zip_path)

    print("Backup completed successfully")
    delete_from_server()

if __name__ == "__main__":
    main()