# Backup to Nextcloud

A Python script that backs up multiple projects (files + MySQL databases) to Nextcloud via WebDAV, with optional **Grandfather–Father–Son (GFS)** retention.

## Features

- **Multi-project**: Define many projects in config; each gets its own zip (files + DB) and upload path.
- **Per-project**: Choose which files and which database to back up per project.
- **Nextcloud WebDAV**: Uploads to your Nextcloud using the DAV API; each project can use its own folder (`nextcloud_backup_base_dir` / `nextcloud_backup_dir`).
- **MySQL dump**: Optional `mysqldump` per project if database credentials are set.
- **GFS retention**: Optional Grandfather–Father–Son retention (daily / weekly / monthly) so you keep a fixed number of backups over time.
- **Filename-based dates**: Backup dates are taken from the zip filename (`backup_<project>_YYYYMMDD_HHMMSS.zip`) so retention works even when the server doesn’t return last-modified.

## Requirements

- **Python 3.6+** (uses only the standard library except for WebDAV over HTTPS).
- **MySQL client** (`mysqldump`) if you use database backups (optional).
- **Nextcloud** instance with WebDAV enabled (default for most installs).

## Installation

1. Clone the repo and go into the folder:
   ```bash
   git clone <repo-url>
   cd Backup-to-NextCloud
   ```

2. Copy and edit the config:
   ```bash
   cp config.json config.json.my
   # Edit config.json.my (or rename to config.json)
   ```

3. Run:
   ```bash
   python backup.py
   ```

No `pip install` is required; the script uses only the Python standard library.

## Configuration

All settings are in **`config.json`** at the project root.

### `nextcloud`

Shared Nextcloud account used for all uploads.

| Key        | Description                    |
|-----------|--------------------------------|
| `url`     | Nextcloud URL (e.g. `https://nc.example.com`) |
| `user`    | Nextcloud username             |
| `password`| Nextcloud password             |

### `backup`

Global backup and retention options.

| Key              | Description |
|------------------|-------------|
| `temp_dir`       | Directory where zips are created before upload; deleted after upload. Absolute or relative to repo root. |
| `retention_days` | Used only when **GFS is not** set: delete backups older than this many days. |
| `gfs`            | Optional. If present, GFS retention is used instead of `retention_days`. |

**GFS** (optional):

| Key                 | Default | Description |
|---------------------|--------|-------------|
| `son_days`          | 7      | Keep one backup per day for the last N days. |
| `father_weeks`      | 4      | Keep one backup per week for the next N weeks. |
| `grandfather_months`| 12     | Keep one backup per month for the next N months. |

If `gfs` is set, `retention_days` is ignored.

### `projects`

Array of project objects. Each project is backed up as one zip and uploaded to its own Nextcloud path.

| Key                         | Description |
|-----------------------------|-------------|
| `name`                      | Project id (used in zip name and logic). |
| `database_host`             | MySQL host (optional). |
| `database_port`             | MySQL port (default 3306). |
| `database_name`             | Database name. |
| `database_username`         | Database user. |
| `database_password`         | Database password. |
| `files`                     | Array of paths to include (files or directories; directories are recursed). |
| `nextcloud_backup_base_dir` | Base folder on Nextcloud (e.g. `"Backups"`). Use `"/"` for root. |
| `nextcloud_backup_dir`      | Project folder under the base (e.g. `"mysite"`). Use `"/"` for no extra segment. |

**Example `config.json`:**

```json
{
  "nextcloud": {
    "url": "https://your-nextcloud.example.com",
    "user": "your_username",
    "password": "your_password"
  },
  "backup": {
    "retention_days": 7,
    "temp_dir": "/tmp/backup_temp",
    "gfs": {
      "son_days": 7,
      "father_weeks": 4,
      "grandfather_months": 12
    }
  },
  "projects": [
    {
      "name": "mysite",
      "database_host": "localhost",
      "database_port": "3306",
      "database_name": "mysite_db",
      "database_username": "mysite_user",
      "database_password": "secret",
      "files": [
        "/var/www/mysite/storage/",
        "/var/www/mysite/public/uploads/"
      ],
      "nextcloud_backup_base_dir": "Backups",
      "nextcloud_backup_dir": "mysite"
    }
  ]
}
```

## Usage

Run a full backup (create zips, upload, then apply retention):

```bash
python backup.py
```

For each project the script will:

1. Create a zip in `temp_dir` containing the project’s `files` and, if configured, a MySQL dump.
2. Upload the zip to Nextcloud at `nextcloud_backup_base_dir` / `nextcloud_backup_dir`.
3. Delete the local zip.
4. After all projects, run retention (GFS or `retention_days`) and delete old backups on Nextcloud.

### Scheduling (e.g. daily)

Use cron:

```bash
0 2 * * * cd /path/to/Backup-to-NextCloud && python backup.py
```

(Adjust time and path as needed.)

## Retention behaviour

- **Without `gfs`**: Backups older than `retention_days` are deleted.
- **With `gfs`**:
  - **Son**: One backup per day for the last `son_days` days.
  - **Father**: One backup per week for the last `father_weeks` weeks.
  - **Grandfather**: One backup per month for the last `grandfather_months` months.
  - Backups from the last hour are always kept (avoids deleting the one just uploaded).

Retention uses the date encoded in the filename (`backup_<name>_YYYYMMDD_HHMMSS.zip`); if that can’t be parsed, the file is still kept (treated as current).

## Security

- Keep `config.json` out of version control (it contains passwords). Add it to `.gitignore` if you use git.
- Prefer a dedicated Nextcloud user with access only to the backup folders.

## License

Use and modify as you like.
