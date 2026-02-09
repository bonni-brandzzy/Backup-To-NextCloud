import base64
import os
import shutil
import subprocess
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote
import http.client

files = [    
    "/var/www/larapush/storage/logs/server.json",
    "/var/www/larapush/storage/logs/it/",
    "/var/www/larapush/public/uploads/",
    "/var/www/larapush/scripts/format.js",
]
envfile = ".env"

RetentionDays = 7

def load_env(path):
    env = {}
    if not os.path.isfile(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _project_root():
    # backup.py lives at project root 
    return Path(__file__).resolve().parent


def _nextcloud_conn(env):
    base = _project_root()
    env = env or load_env(base / envfile)
    url = (env.get("NEXTCLOUD_URL") or "").replace("https://", "").replace("http://", "").strip("/")
    user = env.get("NEXTCLOUD_USER", "")
    path1 = env.get("NEXTCLOUD_BACKUP_BASE_DIR", "").strip("/")
    path2 = (env.get("NEXTCLOUD_BACKUP_DIR") or "").strip("/")
    if not all([url, user, path1]):
        return None, None, None
    parts = filter(None, [user.strip("/"), path1, path2])
    remote_dir = "/remote.php/dav/files/" + "/".join(quote(p, safe="") for p in parts)
    auth = base64.b64encode(f"{user}:{env.get('NEXTCLOUD_PASS', '')}".encode()).decode()
    return url, remote_dir, auth

def backup():
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = _project_root()
    work = base / "storage" / "backup_temp"
    work.mkdir(parents=True, exist_ok=True)
    zip_path = base / "storage" / f"backup_{ts}.zip"

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p in files:
                full = base / p
                if not full.exists():
                    continue
                if full.is_file():
                    zf.write(full, p)
                else:
                    for root, _, filenames in os.walk(full):
                        for name in filenames:
                            fp = Path(root) / name
                            arc = fp.relative_to(base)
                            zf.write(fp, str(arc))

            env = load_env(base / envfile)
            db_keys = ("DB_HOST", "DB_PORT", "DB_DATABASE", "DB_USERNAME", "DB_PASSWORD")
            if all(k in env for k in db_keys):
                sql_path = work / "dump.sql"
                cmd = ["mysqldump", "-h", env["DB_HOST"], "-P", env["DB_PORT"], "-u", env["DB_USERNAME"], env["DB_DATABASE"]]
                if env["DB_PASSWORD"]:
                    cmd.insert(-1, f"--password={env['DB_PASSWORD']}")
                try:
                    with open(sql_path, "w") as out:
                        subprocess.run(cmd, stdout=out, stderr=subprocess.DEVNULL, check=True, cwd=base)
                    zf.write(sql_path, "dump.sql")
                except (subprocess.CalledProcessError, FileNotFoundError):
                    pass
    finally:
        if work.exists():
            shutil.rmtree(work, ignore_errors=True)

    return str(zip_path)


def upload(zip_path):
    url, remote_dir, auth = _nextcloud_conn(None)
    if not url:
        return False, "Missing NEXTCLOUD_* env keys"
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


def get_backup_files():
    base = _project_root()
    env = load_env(base / envfile)
    url, remote_dir, auth = _nextcloud_conn(env)
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


def delete_from_server():
    base = _project_root()
    env = load_env(base / envfile)
    url, remote_dir, auth = _nextcloud_conn(env)
    if not url:
        return
    files = get_backup_files()
    now = datetime.now(timezone.utc)
    for item in files:
        mod = item.get("last_modified")
        if mod is None:
            continue
        mod_utc = mod.astimezone(timezone.utc) if mod.tzinfo else mod.replace(tzinfo=timezone.utc)
        if (now - mod_utc).days >= RetentionDays:
            conn = http.client.HTTPSConnection(url)
            conn.request("DELETE", remote_dir + "/" + quote(item["name"], safe=""), "", {"Authorization": f"Basic {auth}"})
            conn.getresponse().read()


def main():
    zip_path = backup()
    success, message = upload(zip_path)    
    if success:
        print(message)
    else:
        print(message)
    delete(zip_path)

    print("Backup completed successfully")
    delete_from_server()

if __name__ == "__main__":
    main()