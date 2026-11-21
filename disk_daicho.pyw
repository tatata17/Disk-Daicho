# -*- coding: utf-8 -*-
"""
ディスク台帳 (disk_daicho.pyw)
外付けHDD・DVD・BD・USBメモリなどの中身(フォルダ/ファイル構成)を記録しておき、
ディスクを繋がなくても中身の確認・検索ができるソフト。

・m2ts-encoder が動画と同名で出力する番組情報csv(rplsinfoの出力)も読み取り、
  番組名・放送日・チャンネル・番組内容を一緒に記録して検索できる
・ファイルやフォルダごとにメモを書ける (名前を付け直していないisoなどに「これは何のファイルか」を残せる)
・「ヘルプ → アップデートを確認」で、GitHubの最新版に更新できる(今の版は自動でバックアップ)
・専用のアイコン付き。タスクバーでも他のpywとは別のアイコンとして並ぶ
・標準ライブラリのみで動作 (Python 3.8 以降 / Windows 想定)
・記録データはこのファイルと同じフォルダの disk_daicho.db (SQLite) に保存
"""
import csv
import hashlib
import io
import ntpath
import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
from datetime import datetime
from tkinter import filedialog, messagebox, simpledialog, ttk

APP_NAME = "ディスク台帳"
APP_VERSION = "2026.11.21.5"
VIEWER_URL = "https://tatata17.github.io/Disk-Daicho/"   # 書き出したCSVをブラウザで見るページ
UPDATE_BASE_URL = "https://raw.githubusercontent.com/tatata17/Disk-Daicho/main/"
UPDATE_INFO_FILE = "version.json"
UPDATE_COMMIT_API = "https://api.github.com/repos/tatata17/Disk-Daicho/commits/main"
UPDATE_BACKUP_KEEP = 5
UPDATE_MIN_SIZE = 30_000   # これより小さいファイルは「壊れている」として受け付けない
PAGE_SIZE = 1000
ACCENT = "#166B5A"         # アクセント色(最上段の検索バーの帯)
ACCENT_SOFT = "#D5EBE5"    # 帯の上の補足の文字
BANNER_BG = "#E4F1ED"      # 「接続されています」のバナーの地
WARN_FG = "#B45309"        # 初期値と違う設定を知らせる色
IS_WIN = sys.platform.startswith("win")

TYPE_FILTERS = {
    "すべての種類": None,
    "フォルダのみ": "DIR",
    "動画": ["mp4", "mkv", "m2ts", "ts", "mts", "avi", "mov", "wmv", "mpg", "mpeg",
             "m4v", "webm", "flv", "vob", "rm", "rmvb", "asf", "3gp", "dv", "ogv"],
    "音声": ["mp3", "wav", "flac", "aac", "m4a", "ogg", "opus", "wma", "ac3", "dts",
             "aiff", "aif", "ape", "tak", "mka", "w64", "thd", "eac3"],
    "画像": ["jpg", "jpeg", "png", "gif", "bmp", "tif", "tiff", "webp", "heic", "avif",
             "psd", "raw", "cr2", "nef", "arw", "dng", "svg", "ico"],
    "文書": ["txt", "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv", "md",
             "rtf", "odt", "ods", "html", "htm", "epub"],
    "字幕・台本": ["srt", "ass", "ssa", "sup", "vtt", "sub", "idx", "avs", "vpy"],
    "圧縮": ["zip", "rar", "7z", "lzh", "tar", "gz", "bz2", "xz", "cab"],
    "ディスクイメージ": ["iso", "img", "bin", "cue", "mdf", "mds", "nrg", "vhd", "vhdx"],
}
# 番組情報csvと対になる動画 / csvの1行目が録画ファイルの行かを見分ける拡張子
VIDEO_EXTS = set(TYPE_FILTERS["動画"])
SOURCE_NAME_EXTS = {".m2ts", ".rpls", ".mts", ".ts", ".m2t"}
SIDECAR_MAX_BYTES = 1024 * 1024
SKIP_ROOT_NAMES = {"$recycle.bin", "system volume information", "recycler"}


# ---------------------------------------------------------------- 共通関数
def app_dir():
    try:
        return os.path.dirname(os.path.abspath(sys.argv[0]))
    except Exception:
        return os.getcwd()


def db_path():
    base = app_dir()
    if not os.access(base, os.W_OK):
        base = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "disk_daicho")
        os.makedirs(base, exist_ok=True)
    return os.path.join(base, "disk_daicho.db")


# ---------------------------------------------------------------- アップデート
def parse_version(text):
    """「2026.11.21.1」のような版番号を比較用のタプルにする。読めなければ空タプル"""
    try:
        return tuple(int(x) for x in str(text).strip().split("."))
    except ValueError:
        return ()


def _http_get(url, timeout=15.0):
    req = urllib.request.Request(url, headers={"User-Agent": "disk-daicho-updater",
                                               "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def resolve_update_base():
    """
    取得元のURLの先頭を決める。mainの最新コミットのIDを調べて、そのコミットに固定したURLにする
    (公開した直後に、配信側のキャッシュで version.json と本体の版が食い違うのを避ける)。
    調べられなければ main をそのまま使う。
    """
    try:
        sha = str(json.loads(_http_get(UPDATE_COMMIT_API, timeout=8.0).decode("utf-8")).get("sha", ""))
        if re.fullmatch(r"[0-9a-f]{40}", sha):
            return UPDATE_BASE_URL.replace("/main/", f"/{sha}/")
    except Exception:  # noqa: BLE001
        pass
    return UPDATE_BASE_URL


def fetch_update_info():
    """GitHub上の version.json を取得する。失敗したら例外"""
    base = resolve_update_base()
    raw = _http_get(base + UPDATE_INFO_FILE + f"?t={int(time.time())}")
    info = json.loads(raw.decode("utf-8"))
    if not isinstance(info, dict) or not parse_version(info.get("version", "")) or not info.get("file"):
        raise ValueError("version.json の形式が正しくありません")
    info["_base"] = base  # 本体も同じコミットから取る
    return info


def is_newer_version(remote, local=None):
    r, l = parse_version(remote), parse_version(APP_VERSION if local is None else local)
    return bool(r) and r > l


def download_update_source(info):
    """新しい版のファイルを取得し、壊れていないか確認してから返す(確認に通らなければ例外)。改行はLFにそろえる"""
    base = str(info.get("_base") or UPDATE_BASE_URL)
    raw = _http_get(base + str(info["file"]) + f"?t={int(time.time())}", timeout=60.0)
    raw = raw.replace(b"\r\n", b"\n")  # SHA-256 はこの形で計算する(tools/release.py と同じ)
    expected = str(info.get("sha256") or "").strip().lower()
    if expected and hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError(
            "ダウンロードしたファイルが、配布元の記録(SHA-256)と一致しません。\n"
            "公開直後なら、数分待つと直ります。しばらく待っても同じときは、"
            "配布ページから新しいファイルを入れ直してください")
    text = raw.decode("utf-8")
    if len(raw) < UPDATE_MIN_SIZE:
        raise ValueError("ダウンロードしたファイルが小さすぎます(壊れている可能性があります)")
    m = re.search(r'^APP_VERSION\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m or m.group(1) != str(info["version"]):
        raise ValueError("ダウンロードしたファイルの版番号が version.json と一致しません"
                         "(少し待ってからやり直してください)")
    compile(text, "disk_daicho_update", "exec")  # 構文エラーがないか確認
    return raw


def update_backup_dir():
    return os.path.join(os.path.dirname(db_path()), "backup")


def install_update(raw):
    """今のスクリプトをバックアップしてから、新しい版に置き換える。バックアップのパスを返す"""
    script = os.path.abspath(__file__)
    backup_dir = update_backup_dir()
    os.makedirs(backup_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = os.path.join(backup_dir, f"disk_daicho_{APP_VERSION}_{stamp}.pyw")
    shutil.copy2(script, backup_path)
    tmp = script + ".new"
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, script)
    old = sorted(n for n in os.listdir(backup_dir)
                 if re.fullmatch(r"disk_daicho_.+_\d{8}-\d{6}\.pyw", n))
    for n in old[:-UPDATE_BACKUP_KEEP]:  # 古いバックアップは UPDATE_BACKUP_KEEP 本を残して消す
        try:
            os.remove(os.path.join(backup_dir, n))
        except OSError:
            pass
    return backup_path


def fmt_size(n):
    if n is None:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:,.2f} {unit}"
        n /= 1024
    return ""


def fmt_time(t):
    if not t:
        return ""
    try:
        return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return ""


def hash_password(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, 100_000)


def list_drives():
    """[(表示名, ルートパス, ボリュームラベル, ボリュームシリアル番号)] を返す (Windowsのみ)"""
    out = []
    if not IS_WIN:
        return out
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.SetErrorMode(1)  # メディア未挿入時のエラーダイアログを抑止
    kinds = {2: "リムーバブル", 3: "HDD/SSD", 4: "ネットワーク", 5: "CD/DVD/BD", 6: "RAM"}
    mask = k32.GetLogicalDrives()
    for i in range(26):
        if not mask & (1 << i):
            continue
        root = f"{chr(65 + i)}:\\"
        dtype = k32.GetDriveTypeW(root)
        label_buf = ctypes.create_unicode_buffer(261)
        serial = ctypes.c_uint32(0)
        ok = k32.GetVolumeInformationW(root, label_buf, 261, ctypes.byref(serial),
                                       None, None, None, 0)
        label = label_buf.value if ok else ""
        size = ""
        if ok:
            try:
                size = " " + fmt_size(shutil.disk_usage(root).total)
            except OSError:
                pass
        else:
            label = "(メディアなし)"
        out.append((f"{root[:2]}  {label or 'ラベルなし'}  [{kinds.get(dtype, '不明')}]{size}",
                    root, label if ok else "", serial.value if ok else None))
    return out


def volume_identity(path):
    """
    ディスクの見分けに使う (ボリュームラベル, シリアル番号, ドライブ内のパス) を返す。
    シリアル番号はフォーマット時に付く番号で、ドライブ文字(D: G: など)が変わっても変わらない。
    取れないとき(Windows以外など)はシリアル番号が None。
    """
    drive, sub = os.path.splitdrive(os.path.abspath(path))
    sub = sub.strip("\\/").lower()
    if not IS_WIN or not drive:
        return "", None, sub
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.SetErrorMode(1)
    label_buf = ctypes.create_unicode_buffer(261)
    serial = ctypes.c_uint32(0)
    if not k32.GetVolumeInformationW(drive + "\\", label_buf, 261, ctypes.byref(serial),
                                     None, None, None, 0):
        return "", None, sub
    return label_buf.value, serial.value, sub


def find_recorded(conn, serial, sub):
    """同じディスク(シリアル番号とドライブ内パスが同じ)の記録があれば (id, 名前)"""
    if serial is None:
        return None
    return conn.execute("SELECT id,name FROM disks WHERE serial=? AND COALESCE(subpath,'')=? "
                        "ORDER BY recorded_at DESC", (serial, sub)).fetchone()


# ---------------------------------------------------------------- 記録の設定・接続中ディスク・差分
def rec_summary(folders_only, depth_mode, depth, info_csv):
    """詳細設定の要約(畳んでいても見せる文)と、初期値どおりかどうかを返す"""
    parts = ["フォルダのみ" if folders_only else "フォルダ＋ファイル",
             f"{depth}階層まで" if depth_mode else "全階層",
             "csv読取ON" if info_csv else "csv読取OFF"]
    return " / ".join(parts), (not folders_only and not depth_mode and bool(info_csv))


def load_rec_options(conn):
    """settings に保存した詳細設定を読む。無い・壊れている値は初期値にする"""
    got = dict(conn.execute("SELECT key,value FROM settings WHERE key IN "
                            "('rec_scope','rec_depth_mode','rec_depth','rec_info_csv')"))
    try:
        depth = max(1, min(99, int(got.get("rec_depth", "2"))))
    except (TypeError, ValueError):
        depth = 2
    return {"folders_only": got.get("rec_scope") == "1",
            "depth_mode": got.get("rec_depth_mode") == "1",
            "depth": depth,
            "info_csv": got.get("rec_info_csv", "1") != "0"}


def save_rec_options(conn, opts):
    with conn:
        conn.executemany("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", [
            ("rec_scope", "1" if opts["folders_only"] else "0"),
            ("rec_depth_mode", "1" if opts["depth_mode"] else "0"),
            ("rec_depth", str(int(opts["depth"]))),
            ("rec_info_csv", "1" if opts["info_csv"] else "0")])


def disk_info_csv(conn, disk_id):
    """
    そのディスクを前回記録したとき、番組情報csvを読み取ったかどうか(ワンクリック更新で引き継ぐ値)。
    記録のたびに settings の info_csv_<ディスクid> に残している。残っていない旧記録は、
    番組情報が1件でもあれば「読み取った」とみなす。それも無いときは、失うものが無いので既定(読み取る)にする。
    """
    r = conn.execute("SELECT value FROM settings WHERE key=?", (f"info_csv_{disk_id}",)).fetchone()
    if r:
        return r[0] != "0"
    return True


def info_csv_source(conn, disk_id):
    """disk_info_csv の値の出どころ: 'saved'(保存値) / 'programs'(番組情報ありから推定) / 'default'"""
    if conn.execute("SELECT 1 FROM settings WHERE key=?", (f"info_csv_{disk_id}",)).fetchone():
        return "saved"
    if conn.execute("SELECT 1 FROM programs WHERE disk_id=? LIMIT 1", (disk_id,)).fetchone():
        return "programs"
    return "default"


def connected_records(conn, drives, isdir=os.path.isdir):
    """
    いま接続されているドライブのうち、記録済みのもの(フォルダ指定で記録したものも含む)を返す:
    [(ディスクid, 名前, 今のパス, 前回の記録日時)]。
    ドライブ文字は変わるので、記録時のパス(source)は使わず、ボリュームシリアル番号で今のドライブを
    見つけて「今のドライブ + ドライブ内のパス」を組み立て、実際にあるものだけを返す。
    """
    roots = {}
    for _disp, root, _label, serial in drives:
        if serial is not None:
            roots.setdefault(serial, root)
    out, seen = [], set()
    if not roots:
        return out
    for did, name, source, serial, sub, rec_at, order in conn.execute(
            "SELECT id,name,source,serial,COALESCE(subpath,''),recorded_at,sort_order FROM disks "
            "WHERE serial IS NOT NULL ORDER BY recorded_at DESC").fetchall():
        root = roots.get(serial)
        if root is None or (serial, sub) in seen:
            continue  # 同じ場所を別々に記録してあるときは、新しいほうだけ
        seen.add((serial, sub))
        path = root
        if sub:
            # subpath は小文字にそろえてあるので、表示用に記録時のつづりを使う(同じ場所のときだけ)
            tail = ntpath.splitdrive(source or "")[1].strip("\\/")
            path = ntpath.join(root, tail if tail.lower() == sub else sub)
        if isdir(path):
            out.append((order, did, name, path, rec_at))
    return [x[1:] for x in sorted(out, key=lambda x: (x[0] is None, x[0], x[1]))]


def entry_paths(cur, disk_id, dirs_only=False):
    """{ディスク内のパス: サイズ} を返す(差分用)。dirs_only ならフォルダ、そうでなければファイルが対象"""
    dirs = {0: ""}
    out = {}
    for eid, pid, name, is_dir, size in cur.execute(
            "SELECT id,parent_id,name,is_dir,size FROM entries WHERE disk_id=? ORDER BY id",
            (disk_id,)):
        base = dirs.get(pid, "")
        path = f"{base}\\{name}" if base else name
        if is_dir:
            dirs[eid] = path  # id順に読むので、親は必ず子より先に来る
            if dirs_only:
                out[path] = size or 0
        elif not dirs_only:
            out[path] = size or 0
    return out


def diff_entries(old, new):
    """(新規の件数, 削除の件数, サイズが変わった件数) を返す。old/new は entry_paths の結果"""
    added = sum(1 for k in new if k not in old)
    removed = sum(1 for k in old if k not in new)
    changed = sum(1 for k, v in new.items() if k in old and old[k] != v)
    return added, removed, changed


def fmt_delta(n):
    if not n:
        return "±0"
    return ("+" if n > 0 else "-") + fmt_size(abs(n))


def fmt_diff(diff):
    """差分 (新規, 削除, サイズ変化, 合計の増減, フォルダ対象か) を1行の文にする"""
    added, removed, changed, delta, dirs_only = diff
    unit = "フォルダ" if dirs_only else "ファイル"
    if not (added or removed or changed):
        return f"前回の記録から{unit}の変化はありません (合計 {fmt_delta(delta)})"
    return (f"新規 {added:,}件 / 削除 {removed:,}件 / サイズ変化 {changed:,}件 / "
            f"合計 {fmt_delta(delta)}" + (" (フォルダで比較)" if dirs_only else ""))


def parse_sidecar_csv(path):
    """
    m2ts-encoder のバッチが作る番組情報csv (UTF-16・タブ区切り、1行目が
    ファイル名 / 番組名 / 放送日 / 開始時刻 / チャンネル名 / 番組内容…) を読む。
    カンマ区切り(ファイル名,番組名,チャンネル名,日付)にも対応。
    番組情報csvでなければ None。戻り値: (番組名, 放送日, 開始時刻, チャンネル, 番組内容)
    ※読み取り専用で開くだけで、csvにも動画にも一切書き込まない。
    """
    try:
        with open(path, "rb") as f:
            raw = f.read(SIDECAR_MAX_BYTES)
    except OSError:
        return None
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16", errors="replace")
    elif raw.startswith(b"\xef\xbb\xbf"):
        text = raw.decode("utf-8-sig", errors="replace")
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("cp932", errors="replace")
    first_line = next((ln for ln in text.splitlines() if ln.strip()), "")
    delim = "\t" if "\t" in first_line else ","
    row = None
    try:
        for r in csv.reader(io.StringIO(text), delimiter=delim):
            if any(c.strip() for c in r):
                row = [c.strip() for c in r]
                break
    except csv.Error:
        return None
    if not row or os.path.splitext(row[0])[1].lower() not in SOURCE_NAME_EXTS:
        return None
    row += [""] * 6
    if delim == "\t":
        title, bdate, btime, channel = row[1], row[2], row[3], row[4]
        detail = "\n".join(c for c in row[5:] if c)
    else:
        title, channel, bdate, btime, detail = row[1], row[2], row[3], "", ""
    return title, bdate, btime, channel, detail


# ---------------------------------------------------------------- データベース
SCHEMA = """
CREATE TABLE IF NOT EXISTS disks(
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, source TEXT, label TEXT,
    total INTEGER, free INTEGER, recorded_at REAL, folders_only INTEGER,
    max_depth INTEGER, sort_order INTEGER, pw_salt BLOB, pw_hash BLOB,
    n_files INTEGER, n_dirs INTEGER, used INTEGER, n_skipped INTEGER);
CREATE TABLE IF NOT EXISTS entries(
    id INTEGER PRIMARY KEY, disk_id INTEGER NOT NULL, parent_id INTEGER NOT NULL,
    name TEXT NOT NULL, is_dir INTEGER NOT NULL, size INTEGER, mtime REAL, ext TEXT);
CREATE INDEX IF NOT EXISTS idx_entries_parent ON entries(disk_id, parent_id);
CREATE TABLE IF NOT EXISTS programs(
    entry_id INTEGER PRIMARY KEY, disk_id INTEGER NOT NULL, title TEXT, bdate TEXT,
    btime TEXT, channel TEXT, detail TEXT);
CREATE INDEX IF NOT EXISTS idx_programs_disk ON programs(disk_id);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS notes(
    id INTEGER PRIMARY KEY, disk_id INTEGER NOT NULL, path TEXT NOT NULL,
    entry_id INTEGER, note TEXT);
CREATE INDEX IF NOT EXISTS idx_notes_entry ON notes(entry_id);
"""


def connect():
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(disks)")}
    for col, typ in (("serial", "INTEGER"), ("subpath", "TEXT")):
        if col not in have:  # 以前の版で作ったデータベースに列を足す
            conn.execute(f"ALTER TABLE disks ADD COLUMN {col} {typ}")
    conn.commit()
    return conn


def scan_disk(root, name, folders_only, max_depth, replace_id, cancel, q, read_info=True):
    """別スレッドで実行。ディスクを走査して1トランザクションで記録する。"""
    conn = connect()
    try:
        cur = conn.cursor()
        try:
            usage = shutil.disk_usage(root)
            total, free = usage.total, usage.free
        except OSError:
            total = free = None
        label, serial, sub = volume_identity(root)
        order = cur.execute("SELECT COALESCE(MAX(sort_order),0)+1 FROM disks").fetchone()[0]
        cur.execute("INSERT INTO disks(name,source,label,total,free,recorded_at,folders_only,"
                    "max_depth,sort_order,serial,subpath) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (name, root, label, total, free, time.time(), int(folders_only),
                     max_depth, order, serial, sub))
        disk_id = cur.lastrowid
        next_id = cur.execute("SELECT COALESCE(MAX(id),0)+1 FROM entries").fetchone()[0]

        dir_size = {0: 0}
        dir_parent = {}
        n_files = n_dirs = n_skipped = n_info = 0
        diff = None
        rows = []
        prog_rows = []
        stack = [(root, 0, 1)]
        last = time.time()
        while stack:
            if cancel.is_set():
                conn.rollback()
                q.put(("cancel",))
                return
            path, pid, depth = stack.pop()
            try:
                it = os.scandir(path)
            except OSError:
                n_skipped += 1
                continue
            videos, csvs = [], {}
            with it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if hasattr(e, "is_junction") and e.is_junction():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            if pid == 0 and e.name.lower() in SKIP_ROOT_NAMES:
                                continue
                            try:
                                mt = e.stat(follow_symlinks=False).st_mtime
                            except OSError:
                                mt = None
                            eid = next_id
                            next_id += 1
                            dir_size[eid] = 0
                            dir_parent[eid] = pid
                            n_dirs += 1
                            rows.append((eid, disk_id, pid, e.name, 1, 0, mt, ""))
                            if max_depth is None or depth < max_depth:
                                stack.append((e.path, eid, depth + 1))
                        else:
                            st = e.stat(follow_symlinks=False)
                            dir_size[pid] += st.st_size
                            n_files += 1
                            if not folders_only:
                                stem, ext = os.path.splitext(e.name)
                                ext = ext[1:].lower()
                                rows.append((next_id, disk_id, pid, e.name, 0,
                                             st.st_size, st.st_mtime, ext))
                                if read_info:
                                    if ext == "csv" and st.st_size <= SIDECAR_MAX_BYTES:
                                        csvs[stem.lower()] = (next_id, e.path)
                                    elif ext in VIDEO_EXTS:
                                        videos.append((next_id, stem.lower()))
                                next_id += 1
                    except OSError:
                        n_skipped += 1
            if csvs:  # 動画と同名のcsvがあれば番組情報として読む
                paired = set()
                for vid, stem in videos:
                    if stem in csvs:
                        csv_id, csv_path = csvs[stem]
                        info = parse_sidecar_csv(csv_path)
                        if info:
                            prog_rows.append((vid, disk_id) + info)
                            n_info += 1
                            paired.add(csv_id)
                if paired:
                    # 番組情報として動画に取り込んだcsvは、一覧には出さない(台帳に載せないだけで、
                    # ディスク上のcsvには触れない。フォルダの合計サイズには含めたまま)。
                    # 対の動画がないcsvや、番組情報ではないcsvは、普通のファイルとして載せる。
                    rows[:] = [r for r in rows if r[0] not in paired]
            if len(rows) >= 5000:
                cur.executemany("INSERT INTO entries VALUES(?,?,?,?,?,?,?,?)", rows)
                rows.clear()
            now = time.time()
            if now - last > 0.2:
                last = now
                q.put(("progress", n_files, n_dirs, path))
        if rows:
            cur.executemany("INSERT INTO entries VALUES(?,?,?,?,?,?,?,?)", rows)
        cur.executemany("INSERT INTO programs(entry_id,disk_id,title,bdate,btime,channel,detail) "
                        "VALUES(?,?,?,?,?,?,?)", prog_rows)

        # フォルダサイズを下の階層から積み上げる (子のidは必ず親より大きい)
        for eid in sorted(dir_parent, reverse=True):
            dir_size[dir_parent[eid]] += dir_size[eid]
        cur.executemany("UPDATE entries SET size=? WHERE id=?",
                        ((s, i) for i, s in dir_size.items() if i))
        cur.execute("UPDATE disks SET n_files=?,n_dirs=?,used=?,n_skipped=? WHERE id=?",
                    (n_files, n_dirs, dir_size[0], n_skipped, disk_id))
        if replace_id:  # 上書き: 並び順とパスワードを引き継いで古い記録を消す
            old = cur.execute("SELECT sort_order,pw_salt,pw_hash FROM disks WHERE id=?",
                              (replace_id,)).fetchone()
            if old:
                cur.execute("UPDATE disks SET sort_order=?,pw_salt=?,pw_hash=? WHERE id=?",
                            (old[0], old[1], old[2], disk_id))
                # 差分: 古い記録を消す前に、古い側と新しい側のパスを突き合わせる(その場で計算して捨てる)
                prev = cur.execute("SELECT folders_only,used FROM disks WHERE id=?",
                                   (replace_id,)).fetchone()
                dirs_only = bool(folders_only or prev[0])
                diff = diff_entries(entry_paths(cur, replace_id, dirs_only),
                                    entry_paths(cur, disk_id, dirs_only)) + \
                    (dir_size[0] - (prev[1] or 0), dirs_only)
            # メモは「ディスク内のパス」で覚えているので、記録し直しても同じ場所のファイルに付け直す。
            # 見つからない(移動・改名・削除された)メモも消さずに残し、次に見つかったとき付け直す。
            for nid, npath in cur.execute("SELECT id,path FROM notes WHERE disk_id=?",
                                          (replace_id,)).fetchall():
                eid, parent = None, 0
                for part in npath.split("\\"):
                    r = cur.execute("SELECT id FROM entries WHERE disk_id=? AND parent_id=? "
                                    "AND name=?", (disk_id, parent, part)).fetchone()
                    if not r:
                        eid = None
                        break
                    eid = parent = r[0]
                cur.execute("UPDATE notes SET disk_id=?,entry_id=? WHERE id=?",
                            (disk_id, eid, nid))
            cur.execute("DELETE FROM entries WHERE disk_id=?", (replace_id,))
            cur.execute("DELETE FROM programs WHERE disk_id=?", (replace_id,))
            cur.execute("DELETE FROM disks WHERE id=?", (replace_id,))
            cur.execute("DELETE FROM settings WHERE key=?", (f"info_csv_{replace_id}",))
        # 番組情報csvを読み取ったかどうかを、このディスクの値として残す(ワンクリック更新で引き継ぐ)
        cur.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)",
                    (f"info_csv_{disk_id}", "1" if read_info else "0"))
        conn.commit()
        q.put(("done", disk_id, n_files, n_dirs, n_skipped, n_info, diff))
    except Exception as ex:  # noqa: BLE001
        conn.rollback()
        q.put(("error", f"{type(ex).__name__}: {ex}"))
    finally:
        conn.close()


SELECT_ROWS = ("SELECT e.id,e.disk_id,e.parent_id,e.name,e.is_dir,e.size,e.mtime,e.ext,"
               "p.title,p.bdate,p.btime,p.channel,p.detail,nt.note "
               "FROM entries e LEFT JOIN programs p ON p.entry_id=e.id "
               "LEFT JOIN notes nt ON nt.entry_id=e.id")


# ---------------------------------------------------------------- アイコン
# タスクバーで他のpyw(m2ts-encoderなど)とまとめられないようにする、このアプリ専用のID
TASKBAR_APP_ID = "tatata17.disk-daicho"

# ウインドウとタスクバーに使うアイコン(64x64のPNG)。ディスク+目録の行
APP_ICON_PNG_B64 = (
    "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"
)


def set_taskbar_identity() -> str:
    """
    Windowsのタスクバーは、同じpython.exeで動く窓を1つのアイコンにまとめてしまう。
    このアプリ専用のID(AppUserModelID)を、プロセスに付けて、他のpywとは別のアイコンとして表示させる。
    戻り値は結果の説明(ログ用)。
    """
    if sys.platform != "win32":
        return "Windows以外"
    try:
        import ctypes
        func = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        func.argtypes = [ctypes.c_wchar_p]
        func.restype = ctypes.c_long
        hr = func(TASKBAR_APP_ID)
        return "プロセスにID設定 " + ("成功" if hr == 0 else f"失敗(0x{hr & 0xFFFFFFFF:08X})")
    except Exception as e:  # noqa: BLE001
        return f"プロセスにID設定 例外({e})"


def set_window_taskbar_identity(tk_window) -> str:
    """
    ウインドウ自体にもIDを付ける(プロセスのIDだけでは分かれない環境の保険)。
    SHGetPropertyStoreForWindow で PKEY_AppUserModel_ID を書く。結果の説明を返す。
    """
    if sys.platform != "win32":
        return "Windows以外"
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                        ("Data4", ctypes.c_ubyte * 8)]

        def make_guid(text: str) -> GUID:
            import uuid
            u = uuid.UUID(text)
            g = GUID()
            g.Data1, g.Data2, g.Data3 = u.time_low, u.time_mid, u.time_hi_version
            for i, b in enumerate(u.bytes[8:]):
                g.Data4[i] = b
            return g

        class PROPERTYKEY(ctypes.Structure):
            _fields_ = [("fmtid", GUID), ("pid", wintypes.DWORD)]

        class PROPVARIANT(ctypes.Structure):
            _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort),
                        ("r3", ctypes.c_ushort), ("ptr", ctypes.c_void_p), ("pad", ctypes.c_void_p)]

        tk_window.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(tk_window.winfo_id()) or tk_window.winfo_id()
        iid = make_guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99")   # IID_IPropertyStore
        store = ctypes.c_void_p()
        get_store = ctypes.windll.shell32.SHGetPropertyStoreForWindow
        get_store.argtypes = [wintypes.HWND, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
        get_store.restype = ctypes.c_long
        hr = get_store(hwnd, ctypes.byref(iid), ctypes.byref(store))
        if hr != 0 or not store.value:
            return f"ウインドウにID設定 失敗(0x{hr & 0xFFFFFFFF:08X})"
        text = ctypes.create_unicode_buffer(TASKBAR_APP_ID)
        pv = PROPVARIANT()
        pv.vt = 31   # VT_LPWSTR
        pv.ptr = ctypes.cast(text, ctypes.c_void_p).value
        key = PROPERTYKEY(make_guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"), 5)   # PKEY_AppUserModel_ID
        vtbl = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        set_value = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(PROPERTYKEY),
                                       ctypes.POINTER(PROPVARIANT))(vtbl[6])
        release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
        hr = set_value(store, ctypes.byref(key), ctypes.byref(pv))
        release(store)
        return "ウインドウにID設定 " + ("成功" if hr == 0 else f"失敗(0x{hr & 0xFFFFFFFF:08X})")
    except Exception as e:  # noqa: BLE001
        return f"ウインドウにID設定 例外({e})"


# ---------------------------------------------------------------- 画面
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} v{APP_VERSION}")
        try:
            self._app_icon = tk.PhotoImage(data=APP_ICON_PNG_B64)
            self.iconphoto(True, self._app_icon)  # True: 入力ダイアログなどにも同じアイコン
        except tk.TclError:
            pass
        self.geometry("1180x760")
        self.minsize(960, 600)
        self.conn = connect()
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.scanning = False
        self.drives = []
        self.custom_root = None
        self.disk_ids = []
        self.cur_disk = None
        self.cur_folder = 0
        self.history = []
        self.unlocked = set()
        self.search_mode = False
        self.page = 0
        self.sort_col, self.sort_desc = "name", False
        self.row_info = {}
        self.prog_info = {}
        self.note_info = {}
        self.path_cache = {}
        self._build()
        self.reload_drives()
        self.refresh_disks()
        self.after(100, self._poll)
        self._hide_paired_csv_once()
        self.after(200, self._initial_focus)
        if self.get_setting("auto_update_check", "1") != "0":
            self.after(1500, lambda: self.check_for_update(manual=False))

    # ---------------- 設定・アップデート
    def get_setting(self, key, default=""):
        r = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return r[0] if r else default

    def set_setting(self, key, value):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))

    def _hide_paired_csv_once(self):
        """以前の版で記録したデータから、番組情報として取り込み済みのcsvの行を外す(初回だけ)。
        台帳の記録を整理するだけで、ディスク上のファイルには触れない。"""
        if self.get_setting("paired_csv_hidden") == "1":
            return
        try:
            with self.conn:
                self.conn.execute(
                    "DELETE FROM entries WHERE is_dir=0 AND ext='csv' AND EXISTS ("
                    "SELECT 1 FROM programs p JOIN entries v ON v.id=p.entry_id "
                    "WHERE v.disk_id=entries.disk_id AND v.parent_id=entries.parent_id "
                    "AND lower(substr(v.name,1,length(v.name)-length(v.ext)-1))"
                    "=lower(substr(entries.name,1,length(entries.name)-4)))")
                self.conn.execute("UPDATE notes SET entry_id=NULL WHERE entry_id IS NOT NULL "
                                  "AND entry_id NOT IN (SELECT id FROM entries)")
                self.conn.execute("INSERT OR REPLACE INTO settings(key,value) "
                                  "VALUES('paired_csv_hidden','1')")
        except sqlite3.Error:
            pass

    def _build_menu(self):
        bar = tk.Menu(self)
        self._help_menu = tk.Menu(bar, tearoff=0)
        self._help_menu.add_command(label="アップデートを確認...",
                                    command=lambda: self.check_for_update(manual=True))
        self.auto_update_var = tk.BooleanVar(value=self.get_setting("auto_update_check", "1") != "0")
        self._help_menu.add_checkbutton(
            label="起動時に更新を確認する", variable=self.auto_update_var,
            command=lambda: self.set_setting("auto_update_check",
                                             "1" if self.auto_update_var.get() else "0"))
        self._help_menu.add_separator()
        self._help_menu.add_command(label="ビューアのサイトを開く", command=self.open_viewer)
        self._help_menu.add_command(label="バージョン情報", command=lambda: messagebox.showinfo(
            "バージョン情報", f"{APP_NAME}\nバージョン {APP_VERSION}\n\n"
            "配布元: https://github.com/tatata17/Disk-Daicho", parent=self))
        bar.add_cascade(label="ヘルプ", menu=self._help_menu)
        self.configure(menu=bar)

    def check_for_update(self, manual=False):
        """バックグラウンドで更新を確認する。manual=True は結果を必ずダイアログで知らせる"""
        if getattr(self, "_update_checking", False):
            return
        self._update_checking = True
        result = queue.Queue()

        def worker():
            try:
                result.put(("ok", fetch_update_info()))
            except Exception as e:  # noqa: BLE001
                result.put(("err", e))

        threading.Thread(target=worker, daemon=True).start()

        def poll():
            try:
                kind, value = result.get_nowait()
            except queue.Empty:
                self.after(300, poll)
                return
            self._update_checking = False
            if kind == "err":
                if manual:
                    messagebox.showerror("アップデート", "更新の確認に失敗しました。\n"
                                         f"ネットワークに接続できているか確認してください。\n\n{value}",
                                         parent=self)
                return
            if not is_newer_version(str(value["version"])):
                if manual:
                    messagebox.showinfo("アップデート", f"最新の版です。(バージョン {APP_VERSION})",
                                        parent=self)
                return
            self._help_menu.entryconfig(
                0, label=f"アップデートを確認... (新しい版 {value['version']} があります)")
            if self.scanning and not manual:  # 記録中は邪魔しない(メニューの表示だけ)
                return
            self._confirm_and_update(value)

        self.after(300, poll)

    def _confirm_and_update(self, info):
        if self.scanning:
            messagebox.showinfo("アップデート", "記録中は更新できません。終わってからやり直してください。",
                                parent=self)
            return
        notes = str(info.get("notes", "")).strip() or \
            "(細かい修正です。詳しくは、更新履歴(GitHubの CHANGELOG.md)を見てください)"
        if not messagebox.askyesno(
                "アップデート", f"新しい版があります。\n\n現在: {APP_VERSION}\n最新: {info['version']}\n\n"
                f"【更新内容】\n{notes}\n\n更新して、アプリを再起動しますか?\n"
                "(今の版は自動でバックアップします。記録データはそのまま残ります)", parent=self):
            return
        self.var_status.set("アップデートをダウンロード中...")
        result = queue.Queue()

        def worker():
            try:
                result.put(("ok", download_update_source(info)))
            except Exception as e:  # noqa: BLE001
                result.put(("err", e))

        threading.Thread(target=worker, daemon=True).start()

        def poll():
            try:
                kind, value = result.get_nowait()
            except queue.Empty:
                self.after(300, poll)
                return
            if kind == "err":
                self.var_status.set("アップデートに失敗しました。")
                messagebox.showerror("アップデート", "ダウンロードに失敗しました。今の版はそのままです。"
                                     f"\n\n{value}", parent=self)
                return
            if self.scanning:
                messagebox.showinfo("アップデート", "記録が始まったため、更新を中止しました。", parent=self)
                return
            try:
                backup_path = install_update(value)
            except Exception as e:  # noqa: BLE001
                self.var_status.set("アップデートに失敗しました。")
                messagebox.showerror("アップデート", "置き換えに失敗しました。今の版はそのままです。"
                                     f"\n\n{e}", parent=self)
                return
            messagebox.showinfo("アップデート", f"バージョン {info['version']} に更新しました。再起動します。"
                                f"\n\n(旧版のバックアップ: {backup_path})", parent=self)
            script = os.path.abspath(__file__)
            try:
                subprocess.Popen([sys.executable, script], cwd=os.path.dirname(script))
            except Exception as e:  # noqa: BLE001
                messagebox.showerror("アップデート", "自動で再起動できませんでした。"
                                     f"手動で起動し直してください。\n\n{e}", parent=self)
                return
            self.destroy()

        self.after(300, poll)

    # ---------------- 画面構築
    def _build(self):
        self._build_menu()
        st = ttk.Style(self)
        try:
            st.theme_use("vista" if IS_WIN else "clam")
        except tk.TclError:
            pass
        st.configure("Title.TLabel", font=("", 14, "bold"))
        st.configure("Treeview", rowheight=24)

        # ---- 最上段: 検索(アクセント色の帯)。検索の入口はここ1か所だけ
        band = tk.Frame(self, bg=ACCENT)
        band.pack(fill="x")
        row = tk.Frame(band, bg=ACCENT)
        row.pack(fill="x", padx=10, pady=(8, 2))
        try:
            self._band_icon = self._app_icon.subsample(2)
            tk.Label(row, image=self._band_icon, bg=ACCENT).pack(side="left")
        except (AttributeError, tk.TclError):
            pass
        tk.Label(row, text=APP_NAME, bg=ACCENT, fg="white",
                 font=("", 13, "bold")).pack(side="left", padx=(6, 14))
        self.var_q = tk.StringVar()
        self.ent_q = ttk.Entry(row, textvariable=self.var_q, font=("", 11))
        self.ent_q.pack(side="left", fill="x", expand=True, ipady=2)
        self.ent_q.bind("<Return>", lambda e: self.do_search())
        self.cmb_where = ttk.Combobox(row, state="readonly", width=14,
                                      values=["このディスク", "すべてのディスク"])
        self.cmb_where.current(1)
        self.cmb_where.pack(side="left", padx=(6, 3))
        self.cmb_type = ttk.Combobox(row, state="readonly", width=14, values=list(TYPE_FILTERS))
        self.cmb_type.current(0)
        self.cmb_type.pack(side="left")
        ttk.Button(row, text="検索", command=self.do_search).pack(side="left", padx=(6, 3))
        ttk.Button(row, text="解除", command=self.clear_search).pack(side="left")
        tk.Label(band, bg=ACCENT, fg=ACCENT_SOFT, anchor="w", font=("", 9),
                 text="ファイル名のほか、メモ・番組名・チャンネル・番組内容にもヒットします(Enterで検索)。"
                      "ロック中のディスクは「すべてのディスク」の検索では対象外です。"
                 ).pack(fill="x", padx=10, pady=(0, 6))

        # ---- 記録済みのディスクが接続されているときのバナー(無いときは高さ1で見えない)
        self.banner_box = tk.Frame(self, bg=BANNER_BG, height=1)
        self.banner_box.pack(fill="x")
        self.banner_buttons = []

        # ---- 記録する(普段使うものだけ。細かい設定は「詳細設定」に畳む)
        top = ttk.LabelFrame(self, text=" ディスクを記録する ", padding=8)
        top.pack(fill="x", padx=10, pady=(8, 4))
        self.cmb_drive = ttk.Combobox(top, state="readonly", width=44)
        self.cmb_drive.grid(row=0, column=0, sticky="we", padx=(0, 4))
        self.cmb_drive.bind("<<ComboboxSelected>>", self._drive_selected)
        ttk.Button(top, text="再読込", width=7, command=self.reload_drives).grid(row=0, column=1)
        ttk.Button(top, text="フォルダ指定…", command=self.pick_folder).grid(row=0, column=2, padx=(4, 14))
        ttk.Label(top, text="名前").grid(row=0, column=3, padx=(0, 4))
        self.var_name = tk.StringVar()
        ttk.Entry(top, textvariable=self.var_name, width=26).grid(row=0, column=4, sticky="we", padx=(0, 12))
        self.btn_rec = ttk.Button(top, text="記録する", command=self.start_scan)
        self.btn_rec.grid(row=0, column=5, padx=2)
        self.btn_stop = ttk.Button(top, text="中止", command=self.cancel.set, state="disabled")
        self.btn_stop.grid(row=0, column=6, padx=2)
        top.columnconfigure(0, weight=2)
        top.columnconfigure(4, weight=1)

        # 詳細設定の見出し。畳んでいても、今の設定の要約をいつも出す
        self.adv_open = False
        self.lbl_adv = tk.Label(top, anchor="w", cursor="hand2", takefocus=1, font=("", 9))
        self.lbl_adv.grid(row=1, column=0, columnspan=7, sticky="we", pady=(8, 0))
        for seq in ("<Button-1>", "<Return>", "<space>"):
            self.lbl_adv.bind(seq, self._toggle_adv)
        self.adv = ttk.Frame(top)
        opts = load_rec_options(self.conn)
        self._rec_loading = True
        ttk.Label(self.adv, text="記録範囲").pack(side="left")
        self.cmb_scope = ttk.Combobox(self.adv, state="readonly", width=16,
                                      values=["フォルダ＋ファイル", "フォルダのみ"])
        self.cmb_scope.current(1 if opts["folders_only"] else 0)
        self.cmb_scope.pack(side="left", padx=(4, 14))
        self.cmb_scope.bind("<<ComboboxSelected>>", self._rec_changed)
        ttk.Label(self.adv, text="階層").pack(side="left")
        self.cmb_depth = ttk.Combobox(self.adv, state="readonly", width=12,
                                      values=["すべての階層", "階層を指定"])
        self.cmb_depth.current(1 if opts["depth_mode"] else 0)
        self.cmb_depth.pack(side="left", padx=(4, 0))
        self.cmb_depth.bind("<<ComboboxSelected>>", self._rec_changed)
        self.var_depth = tk.IntVar(value=opts["depth"])
        self.spn_depth = ttk.Spinbox(self.adv, from_=1, to=99, width=4, textvariable=self.var_depth,
                                     state="normal" if opts["depth_mode"] else "disabled")
        self.spn_depth.pack(side="left", padx=3)
        ttk.Label(self.adv, text="階層まで (ディスク直下が1階層)").pack(side="left", padx=(0, 14))
        self.var_info_csv = tk.BooleanVar(value=opts["info_csv"])
        ttk.Checkbutton(self.adv, text="動画と同名のcsv(m2ts-encoderの番組情報)を読み取る",
                        variable=self.var_info_csv, command=self._rec_changed).pack(side="left")
        self.var_depth.trace_add("write", self._rec_changed)
        self._rec_loading = False
        self._update_rec_summary()

        self.var_status = tk.StringVar(value="記録したいディスクを選んで「記録する」を押してください。")
        ttk.Label(self, textvariable=self.var_status, anchor="w", relief="sunken",
                  padding=(6, 2)).pack(side="bottom", fill="x")

        pw = ttk.PanedWindow(self, orient="horizontal")
        pw.pack(fill="both", expand=True, padx=10, pady=6)

        # ---- 左: 記録したディスク
        left = ttk.Frame(pw, padding=4)
        pw.add(left, weight=0)
        hd = ttk.Frame(left)
        hd.pack(fill="x")
        ttk.Label(hd, text="記録したディスク").pack(side="left")
        mb = ttk.Menubutton(hd, text="保存 ▾")
        m = tk.Menu(mb, tearoff=0)
        m.add_command(label="選択中のディスクをCSVに書き出す", command=lambda: self.export_csv(False))
        m.add_command(label="すべてのディスクをCSVに書き出す", command=lambda: self.export_csv(True))
        m.add_command(label="ビューアのサイトを開く (書き出したCSVをブラウザで見る)",
                      command=self.open_viewer)
        m.add_separator()
        m.add_command(label="記録データ(DB)をバックアップ", command=self.backup_db)
        mb["menu"] = m
        mb.pack(side="right")
        lf = ttk.Frame(left)
        lf.pack(fill="both", expand=True, pady=4)
        self.lst = tk.Listbox(lf, width=30, exportselection=False, activestyle="none",
                              font=("", 10))
        sb = ttk.Scrollbar(lf, command=self.lst.yview)
        self.lst.configure(yscrollcommand=sb.set)
        self.lst.pack(side="left", fill="both", expand=True)
        sb.pack(side="left", fill="y")
        self.lst.bind("<<ListboxSelect>>", self._disk_selected)
        bf = ttk.Frame(left)
        bf.pack(fill="x")
        ttk.Button(bf, text="↑ 上へ", command=lambda: self.move_disk(-1)).pack(side="left", expand=True, fill="x")
        ttk.Button(bf, text="↓ 下へ", command=lambda: self.move_disk(1)).pack(side="left", expand=True, fill="x")
        ttk.Button(left, text="ビューアのサイトを開く", command=self.open_viewer).pack(fill="x", pady=(6, 0))

        # ---- 右: 中身
        right = ttk.Frame(pw, padding=4)
        pw.add(right, weight=1)
        r1 = ttk.Frame(right)
        r1.pack(fill="x")
        self.var_title = tk.StringVar(value="(ディスク未選択)")
        ttk.Label(r1, textvariable=self.var_title, style="Title.TLabel").pack(side="left")
        ttk.Button(r1, text="記録を削除", command=self.delete_disk).pack(side="right")
        ttk.Button(r1, text="パスワード", command=self.set_password).pack(side="right", padx=4)
        ttk.Button(r1, text="名前変更", command=self.rename_disk).pack(side="right")
        self.var_info = tk.StringVar()
        ttk.Label(right, textvariable=self.var_info, foreground="#555").pack(fill="x", pady=(2, 6))

        r2 = ttk.Frame(right)
        r2.pack(fill="x")
        ttk.Button(r2, text="←", width=3, command=self.go_back).pack(side="left")
        ttk.Button(r2, text="↑", width=3, command=self.go_up).pack(side="left", padx=3)
        self.var_path = tk.StringVar()
        ttk.Entry(r2, textvariable=self.var_path, state="readonly").pack(side="left", fill="x", expand=True)
        ttk.Frame(right, height=6).pack(fill="x")

        tf = ttk.Frame(right)
        cols = ("note", "size", "mtime", "ext", "bdate", "channel", "loc")
        self.tree = ttk.Treeview(tf, columns=cols, selectmode="browse")
        for cid, text, w, anchor in (("#0", "名前", 360, "w"), ("note", "メモ", 200, "w"),
                                     ("size", "サイズ", 100, "e"),
                                     ("mtime", "更新日時", 130, "w"), ("ext", "種類", 70, "w"),
                                     ("bdate", "放送日時", 130, "w"),
                                     ("channel", "チャンネル", 130, "w"),
                                     ("loc", "場所", 300, "w")):
            key = "name" if cid == "#0" else cid
            self.tree.heading(cid, text=text, command=lambda k=key: self.sort_by(k))
            self.tree.column(cid, width=w, anchor=anchor, stretch=(cid in ("#0", "note", "loc")))
        ys = ttk.Scrollbar(tf, command=self.tree.yview)
        self.tree.configure(yscrollcommand=ys.set)
        self.tree.pack(side="left", fill="both", expand=True)
        ys.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._show_detail)
        self.tree.bind("<Double-1>", self._open_item)
        self.tree.bind("<Return>", self._open_item)
        self.tree.bind("<BackSpace>", lambda e: self.go_up())
        self.tree.bind("<Button-3>", self._popup)
        self.menu = tk.Menu(self, tearoff=0)
        self.menu.add_command(label="パスをコピー", command=self.copy_path)
        self.menu.add_command(label="名前をコピー", command=lambda: self.copy_path(name_only=True))
        self.menu.add_command(label="このフォルダの場所へ移動", command=self.jump_to_location)
        self.menu.add_separator()
        self.menu.add_command(label="メモを書く…", command=self.edit_note)

        self.lock_frame = ttk.Frame(tf)
        ttk.Label(self.lock_frame, text="このディスクの記録はロックされています。\n"
                  "パスワードを入力すると内容を表示できます。", justify="center").pack(pady=10)
        ttk.Button(self.lock_frame, text="パスワードを入力", command=self.unlock).pack()

        r5 = ttk.Frame(right)
        r5.pack(side="bottom", fill="x", pady=(6, 0))
        self.txt_detail = tk.Text(right, height=5, wrap="word", state="disabled",
                                  relief="flat", background="#f4f4f4", font=("", 9))
        self.txt_detail.pack(side="bottom", fill="x", pady=(6, 0))
        self.var_count = tk.StringVar()
        ttk.Label(r5, textvariable=self.var_count).pack(side="left")
        self.btn_next = ttk.Button(r5, text="次へ", command=lambda: self.turn_page(1))
        self.btn_next.pack(side="right")
        self.btn_prev = ttk.Button(r5, text="前へ", command=lambda: self.turn_page(-1))
        self.btn_prev.pack(side="right", padx=4)
        ttk.Button(r5, text="メモを書く…", command=self.edit_note).pack(side="right", padx=(0, 12))
        tf.pack(fill="both", expand=True)

    # ---------------- 詳細設定(畳む・要約・保存)
    def _rec_options(self):
        try:
            depth = max(1, min(99, int(self.var_depth.get())))
        except (tk.TclError, ValueError):
            depth = 2
        return {"folders_only": self.cmb_scope.current() == 1,
                "depth_mode": self.cmb_depth.current() == 1,
                "depth": depth, "info_csv": bool(self.var_info_csv.get())}

    def _update_rec_summary(self):
        o = self._rec_options()
        text, is_default = rec_summary(o["folders_only"], o["depth_mode"], o["depth"], o["info_csv"])
        mark = "▾" if self.adv_open else "▸"
        self.lbl_adv.configure(
            text=f"{mark} 詳細設定:  {text}" + ("" if is_default else "   ← 初期値と違う設定です"),
            fg="#444444" if is_default else WARN_FG,
            font=("", 9) if is_default else ("", 9, "bold"))

    def _rec_changed(self, *_a):
        if getattr(self, "_rec_loading", True):
            return
        self.spn_depth.configure(state="normal" if self.cmb_depth.current() == 1 else "disabled")
        save_rec_options(self.conn, self._rec_options())
        self._update_rec_summary()

    def _toggle_adv(self, _e=None):
        self.adv_open = not self.adv_open
        if self.adv_open:
            self.adv.grid(row=2, column=0, columnspan=7, sticky="w", pady=(6, 0))
        else:
            self.adv.grid_remove()
        self._update_rec_summary()

    def _initial_focus(self):
        """起動時は検索欄へ。ただし「接続されています」のバナーが出ているときは、その更新ボタンへ"""
        try:
            (self.banner_buttons[0] if self.banner_buttons else self.ent_q).focus_set()
        except tk.TclError:
            pass

    # ---------------- 接続中の記録済みディスク(バナーとワンクリック更新)
    def refresh_banner(self):
        for w in self.banner_box.winfo_children():
            w.destroy()
        self.banner_buttons = []
        try:
            found = connected_records(self.conn, self.drives)
        except (sqlite3.Error, OSError):
            found = []
        if not found:
            self.banner_box.configure(height=1)
            return
        for disk_id, name, path, rec_at in found:
            row = tk.Frame(self.banner_box, bg=BANNER_BG)
            row.pack(fill="x", padx=10, pady=3)
            btn = ttk.Button(row, text="今の内容で更新",
                             command=lambda d=disk_id, p=path: self.quick_update(d, p))
            btn.pack(side="right")
            if self.scanning:
                btn.configure(state="disabled")
            self.banner_buttons.append(btn)
            tk.Label(row, bg=BANNER_BG, fg="#0F3D33", anchor="w",
                     text=f"「{name}」が接続されています ({path})  ―  前回の記録 {fmt_time(rec_at)}"
                     ).pack(side="left", fill="x", expand=True)

    def quick_update(self, disk_id, path):
        """バナーのボタン: 名前・記録範囲・階層・csv読み取りを前回の記録から引き継いで、すぐ記録し直す"""
        if self.scanning:
            return
        r = self.conn.execute("SELECT name,folders_only,max_depth FROM disks WHERE id=?",
                              (disk_id,)).fetchone()
        if not r:
            self.reload_drives()
            return
        if not os.path.isdir(path):
            messagebox.showerror(APP_NAME, f"{path} を開けません。\nディスクが外されていないか確認してください。")
            self.reload_drives()
            return
        self._begin_scan(path, r[0], bool(r[1]), r[2], disk_id, disk_info_csv(self.conn, disk_id))

    def _begin_scan(self, root, name, folders_only, max_depth, replace_id, read_info):
        self.cancel.clear()
        self.scanning = True
        self._scan_name = name
        self.btn_rec.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        for b in self.banner_buttons:
            b.configure(state="disabled")
        self.var_status.set(f"「{name}」の記録を開始します…")
        threading.Thread(target=scan_disk, daemon=True, args=(
            root, name, folders_only, max_depth, replace_id, self.cancel, self.q, read_info)).start()

    # ---------------- ドライブ選択・記録
    def reload_drives(self):
        self.drives = list_drives()
        vals = []
        for disp, _root, _label, serial in self.drives:
            hit = find_recorded(self.conn, serial, "")
            vals.append(disp + (f"  ★記録済:{hit[1]}" if hit else ""))
        if self.custom_root:
            vals.append(f"フォルダ: {self.custom_root}")
        self.cmb_drive["values"] = vals
        if self.custom_root:
            self.cmb_drive.current(len(vals) - 1)
        else:
            self.cmb_drive.set("ディスクを選択してください" if vals else "「フォルダ指定…」で選んでください")
        self.refresh_banner()

    def pick_folder(self):
        d = filedialog.askdirectory(title="記録するドライブまたはフォルダを選択")
        if d:
            self.custom_root = os.path.normpath(d)
            self.reload_drives()
            if not self.var_name.get().strip():
                self.var_name.set(os.path.basename(self.custom_root) or self.custom_root)

    def _drive_selected(self, _e=None):
        i = self.cmb_drive.current()
        if not 0 <= i < len(self.drives):
            return
        hit = find_recorded(self.conn, self.drives[i][3], "")
        if hit:  # ドライブ文字が変わっていても、同じディスクなら前の名前を出す
            self.var_name.set(hit[1])
            self.var_status.set(f"このディスクは「{hit[1]}」として記録済みです。"
                                "「記録する」で今の内容に更新できます。")
        elif self.drives[i][2]:
            self.var_name.set(self.drives[i][2])

    def _selected_root(self):
        i = self.cmb_drive.current()
        if i < 0:
            return None
        return self.drives[i][1] if i < len(self.drives) else self.custom_root

    def start_scan(self):
        if self.scanning:
            return
        root = self._selected_root()
        if not root:
            messagebox.showinfo(APP_NAME, "記録するディスクを選んでください。")
            return
        if not os.path.isdir(root):
            messagebox.showerror(APP_NAME, f"{root} を開けません。\nディスクが入っているか確認してください。")
            return
        name = self.var_name.get().strip()
        _label, serial, sub = volume_identity(root)
        same = find_recorded(self.conn, serial, sub)
        if not name and same:
            name = same[1]
        if not name:
            messagebox.showinfo(APP_NAME, "ディスクに付ける名前を入力してください。\n(例: 動画保管01)")
            return
        replace_id = None
        row = None
        if same:  # 同じディスクを読み直すとき
            msg = f"このディスクは「{same[1]}」として記録済みです。\n\n"
            msg += "はい: 今の内容で更新する"
            if name != same[1]:
                msg += f" (名前は「{name}」に変わります)"
            msg += "\nいいえ: 前の記録を残して、別に記録する"
            prev = self.conn.execute("SELECT folders_only,max_depth FROM disks WHERE id=?",
                                     (same[0],)).fetchone()
            o = self._rec_options()
            now_depth = o["depth"] if o["depth_mode"] else None
            if prev and (bool(prev[0]) != o["folders_only"] or prev[1] != now_depth):
                was = rec_summary(bool(prev[0]), prev[1] is not None, prev[1], True)[0].rsplit(" / ", 1)[0]
                now = rec_summary(o["folders_only"], o["depth_mode"], o["depth"], True)[0].rsplit(" / ", 1)[0]
                msg += f"\n\n※記録範囲が前回と違います。\n　前回: {was}\n　今回: {now}"
            ans = messagebox.askyesnocancel(APP_NAME, msg)
            if ans is None:
                return
            if ans:
                replace_id = same[0]
        else:
            row = self.conn.execute("SELECT id FROM disks WHERE name=?", (name,)).fetchone()
        if row:
            ans = messagebox.askyesnocancel(
                APP_NAME, f"「{name}」は既に記録されています。\n\n"
                "別のディスクのようですが(または以前の版での記録)、どうしますか？\n"
                "はい: 今の内容で上書き(更新)する\nいいえ: 同じ名前で別に記録する")
            if ans is None:
                return
            if ans:
                replace_id = row[0]
        o = self._rec_options()
        self._begin_scan(root, name, o["folders_only"], o["depth"] if o["depth_mode"] else None,
                         replace_id, o["info_csv"])

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "progress":
                    self.var_status.set(f"記録中… ファイル {msg[1]:,} / フォルダ {msg[2]:,}   {msg[3]}")
                    continue
                self.scanning = False
                self.btn_rec.configure(state="normal")
                self.btn_stop.configure(state="disabled")
                if msg[0] == "done":
                    skipped = f" (読めなかった場所 {msg[4]:,} 件)" if msg[4] else ""
                    info = f" / 番組情報 {msg[5]:,} 件" if msg[5] else ""
                    text = f"記録しました。ファイル {msg[2]:,} / フォルダ {msg[3]:,}{info}{skipped}"
                    diff = msg[6]
                    if diff:
                        text += "   【前回との差】" + fmt_diff(diff)
                    self.var_status.set(text)
                    self.path_cache.clear()
                    self.var_name.set("")
                    self.refresh_disks(select_id=msg[1])
                    self.reload_drives()
                    if diff and any(diff[:3]):  # 変化があったときは、見落とさないようダイアログでも出す
                        messagebox.showinfo(
                            APP_NAME, f"「{getattr(self, '_scan_name', '')}」を更新しました。\n\n"
                            f"前回の記録との差:\n{fmt_diff(diff)}", parent=self)
                elif msg[0] == "cancel":
                    self.refresh_banner()
                    self.var_status.set("記録を中止しました。(前の記録はそのまま残っています)")
                else:
                    self.refresh_banner()
                    self.var_status.set("記録に失敗しました。")
                    messagebox.showerror(APP_NAME, f"記録中にエラーが発生しました。\n{msg[1]}")
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # ---------------- ディスク一覧
    def refresh_disks(self, select_id=None):
        if select_id is None:
            select_id = self.cur_disk
        rows = self.conn.execute(
            "SELECT id,name,pw_hash IS NOT NULL FROM disks ORDER BY sort_order,id").fetchall()
        self.disk_ids = [r[0] for r in rows]
        self.lst.delete(0, "end")
        for _id, name, locked in rows:
            self.lst.insert("end", name + ("  (ロック中)" if locked else ""))
        if select_id in self.disk_ids:
            i = self.disk_ids.index(select_id)
        elif self.disk_ids:
            i = 0
        else:
            i = None
        if i is None:
            self.cur_disk = None
            self.var_title.set("(まだ何も記録されていません)")
            self.var_info.set("")
            self.var_path.set("")
            self._fill([])
            self.lock_frame.place_forget()
        else:
            self.lst.selection_clear(0, "end")
            self.lst.selection_set(i)
            self.lst.see(i)
            self.open_disk(self.disk_ids[i])

    def _disk_selected(self, _e=None):
        sel = self.lst.curselection()
        if sel:
            self.open_disk(self.disk_ids[sel[0]])

    def disk_row(self, disk_id):
        return self.conn.execute(
            "SELECT name,source,label,total,free,recorded_at,folders_only,max_depth,"
            "pw_hash IS NOT NULL,n_files,n_dirs,used FROM disks WHERE id=?", (disk_id,)).fetchone()

    def is_locked(self, disk_id):
        r = self.conn.execute("SELECT pw_hash IS NOT NULL FROM disks WHERE id=?", (disk_id,)).fetchone()
        return bool(r and r[0]) and disk_id not in self.unlocked

    def open_disk(self, disk_id, folder=0):
        r = self.disk_row(disk_id)
        if not r:
            return
        self.cur_disk, self.cur_folder = disk_id, folder
        self.history.clear()
        self.search_mode = False
        self.page = 0
        self.var_title.set(r[0])
        info = [f"記録日 {fmt_time(r[5])}", f"フォルダ {r[10] or 0:,}", f"ファイル {r[9] or 0:,}",
                f"合計 {fmt_size(r[11])}"]
        if r[3]:
            info.append(f"容量 {fmt_size(r[3])} (空き {fmt_size(r[4])})")
        info.append(f"記録元 {r[1]}")
        if r[6]:
            info.append("フォルダのみ記録")
        if r[7]:
            info.append(f"{r[7]}階層まで")
        self.var_info.set("  /  ".join(info))
        self.show()

    def move_disk(self, delta):
        sel = self.lst.curselection()
        if not sel:
            return
        i, j = sel[0], sel[0] + delta
        if not 0 <= j < len(self.disk_ids):
            return
        ids = self.disk_ids[:]
        ids[i], ids[j] = ids[j], ids[i]
        with self.conn:
            self.conn.executemany("UPDATE disks SET sort_order=? WHERE id=?",
                                  [(n, d) for n, d in enumerate(ids, 1)])
        self.refresh_disks()

    def rename_disk(self):
        if self.cur_disk is None:
            return
        old = self.disk_row(self.cur_disk)[0]
        new = simpledialog.askstring(APP_NAME, "新しい名前:", initialvalue=old, parent=self)
        if new and new.strip():
            with self.conn:
                self.conn.execute("UPDATE disks SET name=? WHERE id=?", (new.strip(), self.cur_disk))
            self.refresh_disks()
            self.reload_drives()

    def delete_disk(self):
        if self.cur_disk is None:
            return
        if self.is_locked(self.cur_disk) and not self.unlock():
            return
        name = self.disk_row(self.cur_disk)[0]
        if not messagebox.askyesno(APP_NAME, f"「{name}」の記録を削除しますか？\n"
                                   "(ディスク本体の中身には一切触れません)", icon="warning"):
            return
        with self.conn:
            self.conn.execute("DELETE FROM entries WHERE disk_id=?", (self.cur_disk,))
            self.conn.execute("DELETE FROM programs WHERE disk_id=?", (self.cur_disk,))
            self.conn.execute("DELETE FROM notes WHERE disk_id=?", (self.cur_disk,))
            self.conn.execute("DELETE FROM settings WHERE key=?", (f"info_csv_{self.cur_disk}",))
            self.conn.execute("DELETE FROM disks WHERE id=?", (self.cur_disk,))
        self.path_cache.clear()
        self.cur_disk = None
        self.refresh_disks()
        self.reload_drives()
        self.var_status.set(f"「{name}」の記録を削除しました。")

    # ---------------- パスワード (表示ロック)
    def _check_pw(self, disk_id, pw):
        salt, h = self.conn.execute("SELECT pw_salt,pw_hash FROM disks WHERE id=?", (disk_id,)).fetchone()
        return h is not None and hash_password(pw, salt) == h

    def unlock(self):
        if self.cur_disk is None:
            return False
        pw = simpledialog.askstring(APP_NAME, "パスワード:", show="●", parent=self)
        if pw is None:
            return False
        if not self._check_pw(self.cur_disk, pw):
            messagebox.showerror(APP_NAME, "パスワードが違います。")
            return False
        self.unlocked.add(self.cur_disk)
        self.show()
        return True

    def set_password(self):
        if self.cur_disk is None:
            return
        has_pw = self.disk_row(self.cur_disk)[8]
        if has_pw:
            if self.is_locked(self.cur_disk) and not self.unlock():
                return
            if messagebox.askyesno(APP_NAME, "このディスクのパスワードを解除しますか？\n"
                                   "(「いいえ」で新しいパスワードに変更)"):
                with self.conn:
                    self.conn.execute("UPDATE disks SET pw_salt=NULL,pw_hash=NULL WHERE id=?",
                                      (self.cur_disk,))
                self.unlocked.discard(self.cur_disk)
                self.refresh_disks()
                return
        pw = simpledialog.askstring(
            APP_NAME, "設定するパスワード:\n\n※このソフト上で中身を見えなくする簡易ロックです。\n"
            "　記録データ自体は暗号化されません。", show="●", parent=self)
        if not pw:
            return
        if simpledialog.askstring(APP_NAME, "確認のためもう一度:", show="●", parent=self) != pw:
            messagebox.showerror(APP_NAME, "パスワードが一致しません。")
            return
        salt = os.urandom(16)
        with self.conn:
            self.conn.execute("UPDATE disks SET pw_salt=?,pw_hash=? WHERE id=?",
                              (salt, hash_password(pw, salt), self.cur_disk))
        self.unlocked.add(self.cur_disk)  # 設定した本人はこの起動中は見える
        self.refresh_disks()
        self.var_status.set("パスワードを設定しました。次回起動時からロックされます。")

    # ---------------- 表示
    def path_of(self, entry_id):
        """entries.id からディスク内のパス(\\区切り)を作る"""
        if not entry_id:
            return ""
        if entry_id in self.path_cache:
            return self.path_cache[entry_id]
        r = self.conn.execute("SELECT parent_id,name FROM entries WHERE id=?", (entry_id,)).fetchone()
        if not r:
            return ""
        p = self.path_of(r[0])
        p = f"{p}\\{r[1]}" if p else r[1]
        if len(self.path_cache) > 200_000:
            self.path_cache.clear()
        self.path_cache[entry_id] = p
        return p

    def _order_sql(self):
        col = {"name": "e.name COLLATE NOCASE", "size": "e.size", "mtime": "e.mtime",
               "ext": "e.ext", "note": "COALESCE(nt.note,'') = '', nt.note", "bdate": "p.bdate || p.btime", "channel": "p.channel",
               "loc": "e.disk_id, e.parent_id"}[self.sort_col]
        return (f"ORDER BY e.is_dir DESC, {col} {'DESC' if self.sort_desc else 'ASC'}, "
                "e.name COLLATE NOCASE")

    def show(self):
        if self.search_mode:
            self._show_search()
            return
        if self.cur_disk is None:
            return
        if self.is_locked(self.cur_disk):
            self._fill([])
            self.var_path.set("")
            self.var_count.set("")
            self.lock_frame.place(relx=0.5, rely=0.45, anchor="center")
            return
        self.lock_frame.place_forget()
        rows = self.conn.execute(
            f"{SELECT_ROWS} WHERE e.disk_id=? AND e.parent_id=? {self._order_sql()} LIMIT ? OFFSET ?",
            (self.cur_disk, self.cur_folder, PAGE_SIZE + 1, self.page * PAGE_SIZE)).fetchall()
        name = self.disk_row(self.cur_disk)[0]
        sub = self.path_of(self.cur_folder)
        self.var_path.set(f"{name}\\{sub}" if sub else f"{name}\\")
        self._fill(rows, show_loc=False)

    def _fill(self, rows, show_loc=False):
        self.tree.delete(*self.tree.get_children())
        self.row_info = {}
        self.prog_info = {}
        self.note_info = {}
        more = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]
        names = {}
        has_info = False
        for (eid, disk_id, pid, name, is_dir, size, mtime, ext,
             title, bdate, btime, channel, detail, note) in rows:
            loc = ""
            if title is not None:
                has_info = True
            if show_loc:
                if disk_id not in names:
                    names[disk_id] = self.disk_row(disk_id)[0]
                loc = f"{names[disk_id]}\\{self.path_of(pid)}"
            self.tree.insert("", "end", iid=str(eid), text=("■ " if is_dir else "　 ") + name
                             + ("　［番組情報あり］" if title is not None else ""),
                             values=((note or "").replace("\n", " "), fmt_size(size), fmt_time(mtime),
                                     "フォルダ" if is_dir else ext,
                                     f"{bdate or ''} {btime or ''}".strip(), channel or "", loc))
            self.row_info[eid] = (disk_id, pid, name, is_dir)
            if title is not None:
                self.prog_info[eid] = (title, bdate, btime, channel, detail)
            if note:
                self.note_info[eid] = note
        shown = ["note", "size", "mtime", "ext"]
        if has_info:  # 番組情報がある一覧のときだけ列を出す
            shown += ["bdate", "channel"]
        if show_loc:
            shown.append("loc")
        self.tree.configure(displaycolumns=shown)
        self._show_detail()
        start = self.page * PAGE_SIZE
        if rows:
            self.var_count.set(f"{start + 1:,}～{start + len(rows):,} 件目を表示"
                               + (" (続きあり)" if more else ""))
        else:
            self.var_count.set("0 件" if self.cur_disk is not None else "")
        self.btn_prev.configure(state="normal" if self.page > 0 else "disabled")
        self.btn_next.configure(state="normal" if more else "disabled")

    def _show_detail(self, _e=None):
        sel = self.tree.selection()
        info = self.prog_info.get(int(sel[0])) if sel else None
        text = ""
        note = self.note_info.get(int(sel[0])) if sel else None
        if note:
            text = f"メモ: {note}\n"
        if info:
            title, bdate, btime, channel, detail = info
            head = "  /  ".join(x for x in (f"{bdate or ''} {btime or ''}".strip(), channel) if x)
            text += f"【{title}】  {head}"
            if detail:
                text += "\n" + detail
        self.txt_detail.configure(state="normal")
        self.txt_detail.delete("1.0", "end")
        self.txt_detail.insert("1.0", text)
        self.txt_detail.configure(state="disabled")

    def edit_note(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo(APP_NAME, "メモを書きたいファイルかフォルダを選んでください。")
            return
        eid = int(sel[0])
        disk_id, _pid, name, _d = self.row_info[eid]
        old = self.note_info.get(eid, "")
        new = simpledialog.askstring(
            APP_NAME, f"{name}\n\nメモ (空にして OK を押すとメモを消します):\n"
            "※台帳に書くだけで、ディスクのファイルには何も書き込みません。",
            initialvalue=old, parent=self)
        if new is None:
            return
        new = new.strip()
        with self.conn:
            self.conn.execute("DELETE FROM notes WHERE entry_id=?", (eid,))
            if new:
                self.conn.execute("INSERT INTO notes(disk_id,path,entry_id,note) VALUES(?,?,?,?)",
                                  (disk_id, self.path_of(eid), eid, new))
        yview = self.tree.yview()
        self.show()
        if self.tree.exists(str(eid)):
            self.tree.selection_set(str(eid))
            self.tree.yview_moveto(yview[0])
        self.var_status.set("メモを保存しました。" if new else "メモを消しました。")

    def sort_by(self, key):
        if key == "loc" and not self.search_mode:
            return
        if self.sort_col == key:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col, self.sort_desc = key, key in ("size", "mtime")
        self.page = 0
        self.show()

    def turn_page(self, d):
        self.page = max(0, self.page + d)
        self.show()

    # ---------------- 移動
    def goto(self, disk_id, folder, keep_history=True):
        if disk_id != self.cur_disk:
            if disk_id in self.disk_ids:
                self.lst.selection_clear(0, "end")
                self.lst.selection_set(self.disk_ids.index(disk_id))
            self.open_disk(disk_id, folder)
            return
        if keep_history and not self.search_mode:
            self.history.append(self.cur_folder)
        self.search_mode = False
        self.cur_folder = folder
        self.page = 0
        self.show()

    def _open_item(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        eid = int(sel[0])
        disk_id, pid, _name, is_dir = self.row_info[eid]
        if is_dir:
            self.goto(disk_id, eid)
        elif self.search_mode:
            self.goto(disk_id, pid)
            if self.tree.exists(str(eid)):
                self.tree.selection_set(str(eid))
                self.tree.see(str(eid))

    def go_up(self):
        if self.search_mode:
            self.clear_search()
        elif self.cur_disk is not None and self.cur_folder:
            r = self.conn.execute("SELECT parent_id FROM entries WHERE id=?", (self.cur_folder,)).fetchone()
            came_from = self.cur_folder
            self.goto(self.cur_disk, r[0] if r else 0)
            if self.tree.exists(str(came_from)):
                self.tree.selection_set(str(came_from))
                self.tree.see(str(came_from))

    def go_back(self):
        if self.search_mode:
            self.clear_search()
        elif self.history:
            self.cur_folder = self.history.pop()
            self.page = 0
            self.show()

    # ---------------- 検索
    def do_search(self):
        words = self.var_q.get().split()
        tfilter = TYPE_FILTERS[self.cmb_type.get()]
        if not words and tfilter is None:
            self.clear_search()
            return
        if self.cmb_where.current() == 0 and self.cur_disk is None:
            return
        self.search_mode = True
        self.page = 0
        self.sort_col, self.sort_desc = "name", False
        self._show_search()

    def _show_search(self):
        words = self.var_q.get().split()
        tfilter = TYPE_FILTERS[self.cmb_type.get()]
        where, args = [], []
        if self.cmb_where.current() == 0:
            if self.is_locked(self.cur_disk):
                messagebox.showinfo(APP_NAME, "このディスクはロックされています。")
                self.search_mode = False
                return
            targets = [self.cur_disk]
        else:
            targets = [d for d in self.disk_ids if not self.is_locked(d)]
        if not targets:
            self._fill([])
            return
        where.append(f"e.disk_id IN ({','.join('?' * len(targets))})")
        args += targets
        for w in words:
            w = w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("(e.name LIKE ? ESCAPE '\\' OR p.title LIKE ? ESCAPE '\\' "
                         "OR p.channel LIKE ? ESCAPE '\\' OR p.detail LIKE ? ESCAPE '\\' "
                         "OR nt.note LIKE ? ESCAPE '\\')")
            args += [f"%{w}%"] * 5
        if tfilter == "DIR":
            where.append("e.is_dir=1")
        elif tfilter:
            where.append(f"e.is_dir=0 AND e.ext IN ({','.join('?' * len(tfilter))})")
            args += tfilter
        rows = self.conn.execute(
            f"{SELECT_ROWS} WHERE {' AND '.join(where)} {self._order_sql()} LIMIT ? OFFSET ?",
            args + [PAGE_SIZE + 1, self.page * PAGE_SIZE]).fetchall()
        self.lock_frame.place_forget()
        self.var_path.set(f"検索結果: {self.var_q.get().strip()}  [{self.cmb_where.get()} / {self.cmb_type.get()}]")
        self._fill(rows, show_loc=True)
        locked = len(self.disk_ids) - len(targets) if self.cmb_where.current() == 1 else 0
        if locked:
            self.var_count.set(self.var_count.get() + f"   ※ロック中の {locked} 台は検索対象外")

    def clear_search(self):
        self.var_q.set("")
        self.cmb_type.current(0)
        self.search_mode = False
        self.page = 0
        self.sort_col, self.sort_desc = "name", False
        self.show()

    # ---------------- 右クリック
    def _popup(self, e):
        iid = self.tree.identify_row(e.y)
        if iid:
            self.tree.selection_set(iid)
            self.menu.entryconfigure(2, state="normal" if self.search_mode else "disabled")
            self.menu.tk_popup(e.x_root, e.y_root)

    def copy_path(self, name_only=False):
        sel = self.tree.selection()
        if not sel:
            return
        eid = int(sel[0])
        disk_id, _pid, name, _d = self.row_info[eid]
        text = name if name_only else f"{self.disk_row(disk_id)[0]}\\{self.path_of(eid)}"
        self.clipboard_clear()
        self.clipboard_append(text)
        self.var_status.set(f"コピーしました: {text}")

    def jump_to_location(self):
        sel = self.tree.selection()
        if sel:
            eid = int(sel[0])
            disk_id, pid, _n, _d = self.row_info[eid]
            self.goto(disk_id, pid)
            if self.tree.exists(str(eid)):
                self.tree.selection_set(str(eid))
                self.tree.see(str(eid))

    # ---------------- 書き出し
    def export_csv(self, all_disks):
        if all_disks:
            targets = [d for d in self.disk_ids if not self.is_locked(d)]
            default = "ディスク台帳_すべて.csv"
        else:
            if self.cur_disk is None:
                return
            if self.is_locked(self.cur_disk) and not self.unlock():
                return
            targets = [self.cur_disk]
            default = f"{self.disk_row(self.cur_disk)[0]}.csv"
        if not targets:
            messagebox.showinfo(APP_NAME, "書き出せるディスクがありません。")
            return
        for ch in '\\/:*?"<>|':
            default = default.replace(ch, "_")
        fn = filedialog.asksaveasfilename(defaultextension=".csv", initialfile=default,
                                          filetypes=[("CSV (Excelで開けます)", "*.csv")])
        if not fn:
            return
        self.configure(cursor="watch")
        self.update_idletasks()
        try:
            with open(fn, "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["ディスク", "パス", "種別", "拡張子", "サイズ(バイト)", "サイズ", "更新日時",
                            "番組名", "放送日", "開始時刻", "チャンネル", "番組内容", "メモ"])
                n = 0
                for d in targets:
                    dname = self.disk_row(d)[0]
                    paths = {0: ""}
                    for (eid, _d, pid, name, is_dir, size, mtime, ext, title, bdate, btime,
                         channel, detail, note) in self.conn.execute(
                            f"{SELECT_ROWS} WHERE e.disk_id=? ORDER BY e.id", (d,)):
                        base = paths.get(pid, "")
                        p = f"{base}\\{name}" if base else name
                        if is_dir:
                            paths[eid] = p
                        w.writerow([dname, p, "フォルダ" if is_dir else "ファイル", ext,
                                    size, fmt_size(size), fmt_time(mtime),
                                    title or "", bdate or "", btime or "", channel or "",
                                    detail or "", note or ""])
                        n += 1
            self.var_status.set(f"{n:,} 行を書き出しました: {fn}")
        except OSError as ex:
            messagebox.showerror(APP_NAME, f"書き出しに失敗しました。\n{ex}")
        finally:
            self.configure(cursor="")

    def open_viewer(self):
        """書き出したCSVを見るためのページを、既定のブラウザで開く(ページを開くだけで、何も送信しない)"""
        try:
            webbrowser.open(VIEWER_URL)
            self.var_status.set("ブラウザでビューアを開きました。「保存 → CSVに書き出す」で作ったCSVを選んでください。")
        except Exception as ex:  # noqa: BLE001
            messagebox.showerror(APP_NAME, f"ブラウザを開けませんでした。\n{VIEWER_URL}\n\n{ex}")

    def backup_db(self):
        fn = filedialog.asksaveasfilename(
            defaultextension=".db", filetypes=[("SQLite DB", "*.db")],
            initialfile=f"disk_daicho_{datetime.now():%Y%m%d}.db")
        if not fn:
            return
        try:
            dst = sqlite3.connect(fn)
            with dst:
                self.conn.backup(dst)
            dst.close()
            self.var_status.set(f"バックアップしました: {fn}")
        except (OSError, sqlite3.Error) as ex:
            messagebox.showerror(APP_NAME, f"バックアップに失敗しました。\n{ex}")


def main():
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:  # noqa: BLE001
            pass
    set_taskbar_identity()
    app = App()
    set_window_taskbar_identity(app)
    app.mainloop()


if __name__ == "__main__":
    main()
