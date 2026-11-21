# -*- coding: utf-8 -*-
"""
ディスク台帳 (disk_daicho.pyw)
外付けHDD・DVD・BD・USBメモリなどの中身(フォルダ/ファイル構成)を記録しておき、
ディスクを繋がなくても中身の確認・検索ができるソフト。

・m2ts-encoder が動画と同名で出力する番組情報csv(rplsinfoの出力)も読み取り、
  番組名・放送日・チャンネル・番組内容を一緒に記録して検索できる
・標準ライブラリのみで動作 (Python 3.8 以降 / Windows 想定)
・記録データはこのファイルと同じフォルダの disk_daicho.db (SQLite) に保存
"""
import csv
import hashlib
import io
import os
import queue
import shutil
import sqlite3
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, simpledialog, ttk

APP_NAME = "ディスク台帳"
PAGE_SIZE = 1000
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
                                        csvs[stem.lower()] = e.path
                                    elif ext in VIDEO_EXTS:
                                        videos.append((next_id, stem.lower()))
                                next_id += 1
                    except OSError:
                        n_skipped += 1
            if csvs:  # 動画と同名のcsvがあれば番組情報として読む
                for vid, stem in videos:
                    if stem in csvs:
                        info = parse_sidecar_csv(csvs[stem])
                        if info:
                            prog_rows.append((vid, disk_id) + info)
                            n_info += 1
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
            cur.execute("DELETE FROM entries WHERE disk_id=?", (replace_id,))
            cur.execute("DELETE FROM programs WHERE disk_id=?", (replace_id,))
            cur.execute("DELETE FROM disks WHERE id=?", (replace_id,))
        conn.commit()
        q.put(("done", disk_id, n_files, n_dirs, n_skipped, n_info))
    except Exception as ex:  # noqa: BLE001
        conn.rollback()
        q.put(("error", f"{type(ex).__name__}: {ex}"))
    finally:
        conn.close()


SELECT_ROWS = ("SELECT e.id,e.disk_id,e.parent_id,e.name,e.is_dir,e.size,e.mtime,e.ext,"
               "p.title,p.bdate,p.btime,p.channel,p.detail "
               "FROM entries e LEFT JOIN programs p ON p.entry_id=e.id")


# ---------------------------------------------------------------- 画面
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
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
        self.path_cache = {}
        self._build()
        self.reload_drives()
        self.refresh_disks()
        self.after(100, self._poll)

    # ---------------- 画面構築
    def _build(self):
        st = ttk.Style(self)
        try:
            st.theme_use("vista" if IS_WIN else "clam")
        except tk.TclError:
            pass
        st.configure("Title.TLabel", font=("", 14, "bold"))
        st.configure("Treeview", rowheight=24)

        top = ttk.LabelFrame(self, text=" ディスクを記録する ", padding=8)
        top.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(top, text="① ディスク").grid(row=0, column=0, sticky="w")
        self.cmb_drive = ttk.Combobox(top, state="readonly", width=44)
        self.cmb_drive.grid(row=1, column=0, sticky="we", padx=(0, 4))
        self.cmb_drive.bind("<<ComboboxSelected>>", self._drive_selected)
        ttk.Button(top, text="再読込", width=7, command=self.reload_drives).grid(row=1, column=1)
        ttk.Button(top, text="フォルダ指定…", command=self.pick_folder).grid(row=1, column=2, padx=(4, 12))
        ttk.Label(top, text="② 付ける名前").grid(row=0, column=3, sticky="w")
        self.var_name = tk.StringVar()
        ttk.Entry(top, textvariable=self.var_name, width=26).grid(row=1, column=3, sticky="we", padx=(0, 12))
        ttk.Label(top, text="③ 記録範囲").grid(row=0, column=4, sticky="w")
        self.cmb_scope = ttk.Combobox(top, state="readonly", width=16,
                                      values=["フォルダ＋ファイル", "フォルダのみ"])
        self.cmb_scope.current(0)
        self.cmb_scope.grid(row=1, column=4, padx=(0, 12))
        ttk.Label(top, text="④ 階層").grid(row=0, column=5, sticky="w")
        fr = ttk.Frame(top)
        fr.grid(row=1, column=5, padx=(0, 12))
        self.cmb_depth = ttk.Combobox(fr, state="readonly", width=12,
                                      values=["すべての階層", "階層を指定"])
        self.cmb_depth.current(0)
        self.cmb_depth.pack(side="left")
        self.cmb_depth.bind("<<ComboboxSelected>>", lambda e: self.spn_depth.configure(
            state="normal" if self.cmb_depth.current() == 1 else "disabled"))
        self.var_depth = tk.IntVar(value=2)
        self.spn_depth = ttk.Spinbox(fr, from_=1, to=99, width=4, textvariable=self.var_depth,
                                     state="disabled")
        self.spn_depth.pack(side="left", padx=3)
        ttk.Label(fr, text="階層まで").pack(side="left")
        self.btn_rec = ttk.Button(top, text="記録する", command=self.start_scan)
        self.btn_rec.grid(row=1, column=6, padx=2)
        self.btn_stop = ttk.Button(top, text="中止", command=self.cancel.set, state="disabled")
        self.btn_stop.grid(row=1, column=7, padx=2)
        self.var_info_csv = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="動画と同名のcsv(m2ts-encoderの番組情報)も読み取る",
                        variable=self.var_info_csv).grid(row=2, column=0, columnspan=6,
                                                         sticky="w", pady=(6, 0))
        top.columnconfigure(0, weight=2)
        top.columnconfigure(3, weight=1)

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

        r3 = ttk.Frame(right)
        r3.pack(fill="x", pady=6)
        self.var_q = tk.StringVar()
        ent = ttk.Entry(r3, textvariable=self.var_q)
        ent.pack(side="left", fill="x", expand=True)
        ent.bind("<Return>", lambda e: self.do_search())
        self.cmb_where = ttk.Combobox(r3, state="readonly", width=14,
                                      values=["このディスク", "すべてのディスク"])
        self.cmb_where.current(1)
        self.cmb_where.pack(side="left", padx=3)
        self.cmb_type = ttk.Combobox(r3, state="readonly", width=14, values=list(TYPE_FILTERS))
        self.cmb_type.current(0)
        self.cmb_type.pack(side="left")
        ttk.Button(r3, text="検索", command=self.do_search).pack(side="left", padx=3)
        ttk.Button(r3, text="解除", command=self.clear_search).pack(side="left")

        tf = ttk.Frame(right)
        cols = ("size", "mtime", "ext", "bdate", "channel", "loc")
        self.tree = ttk.Treeview(tf, columns=cols, selectmode="browse")
        for cid, text, w, anchor in (("#0", "名前", 380, "w"), ("size", "サイズ", 100, "e"),
                                     ("mtime", "更新日時", 130, "w"), ("ext", "種類", 70, "w"),
                                     ("bdate", "放送日時", 130, "w"),
                                     ("channel", "チャンネル", 130, "w"),
                                     ("loc", "場所", 300, "w")):
            key = "name" if cid == "#0" else cid
            self.tree.heading(cid, text=text, command=lambda k=key: self.sort_by(k))
            self.tree.column(cid, width=w, anchor=anchor, stretch=(cid in ("#0", "loc")))
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
        tf.pack(fill="both", expand=True)

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
        max_depth = None
        if self.cmb_depth.current() == 1:
            try:
                max_depth = max(1, int(self.var_depth.get()))
            except (tk.TclError, ValueError):
                max_depth = 2
        self.cancel.clear()
        self.scanning = True
        self.btn_rec.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.var_status.set("記録を開始します…")
        threading.Thread(target=scan_disk, daemon=True, args=(
            root, name, self.cmb_scope.current() == 1, max_depth, replace_id,
            self.cancel, self.q, self.var_info_csv.get())).start()

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
                    self.var_status.set(f"記録しました。ファイル {msg[2]:,} / フォルダ {msg[3]:,}{info}{skipped}")
                    self.path_cache.clear()
                    self.var_name.set("")
                    self.refresh_disks(select_id=msg[1])
                    self.reload_drives()
                elif msg[0] == "cancel":
                    self.var_status.set("記録を中止しました。(何も保存されていません)")
                else:
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
            self.conn.execute("DELETE FROM disks WHERE id=?", (self.cur_disk,))
        self.path_cache.clear()
        self.cur_disk = None
        self.refresh_disks()
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
               "ext": "e.ext", "bdate": "p.bdate || p.btime", "channel": "p.channel",
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
        more = len(rows) > PAGE_SIZE
        rows = rows[:PAGE_SIZE]
        names = {}
        has_info = False
        for (eid, disk_id, pid, name, is_dir, size, mtime, ext,
             title, bdate, btime, channel, detail) in rows:
            loc = ""
            if title is not None:
                has_info = True
            if show_loc:
                if disk_id not in names:
                    names[disk_id] = self.disk_row(disk_id)[0]
                loc = f"{names[disk_id]}\\{self.path_of(pid)}"
            self.tree.insert("", "end", iid=str(eid), text=("■ " if is_dir else "　 ") + name,
                             values=(fmt_size(size), fmt_time(mtime),
                                     "フォルダ" if is_dir else ext,
                                     f"{bdate or ''} {btime or ''}".strip(), channel or "", loc))
            self.row_info[eid] = (disk_id, pid, name, is_dir)
            if title is not None:
                self.prog_info[eid] = (title, bdate, btime, channel, detail)
        shown = ["size", "mtime", "ext"]
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
        if info:
            title, bdate, btime, channel, detail = info
            head = "  /  ".join(x for x in (f"{bdate or ''} {btime or ''}".strip(), channel) if x)
            text = f"【{title}】  {head}"
            if detail:
                text += "\n" + detail
        self.txt_detail.configure(state="normal")
        self.txt_detail.delete("1.0", "end")
        self.txt_detail.insert("1.0", text)
        self.txt_detail.configure(state="disabled")

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
                         "OR p.channel LIKE ? ESCAPE '\\' OR p.detail LIKE ? ESCAPE '\\')")
            args += [f"%{w}%"] * 4
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
                            "番組名", "放送日", "開始時刻", "チャンネル", "番組内容"])
                n = 0
                for d in targets:
                    dname = self.disk_row(d)[0]
                    paths = {0: ""}
                    for (eid, _d, pid, name, is_dir, size, mtime, ext, title, bdate, btime,
                         channel, detail) in self.conn.execute(
                            f"{SELECT_ROWS} WHERE e.disk_id=? ORDER BY e.id", (d,)):
                        base = paths.get(pid, "")
                        p = f"{base}\\{name}" if base else name
                        if is_dir:
                            paths[eid] = p
                        w.writerow([dname, p, "フォルダ" if is_dir else "ファイル", ext,
                                    size, fmt_size(size), fmt_time(mtime),
                                    title or "", bdate or "", btime or "", channel or "",
                                    detail or ""])
                        n += 1
            self.var_status.set(f"{n:,} 行を書き出しました: {fn}")
        except OSError as ex:
            messagebox.showerror(APP_NAME, f"書き出しに失敗しました。\n{ex}")
        finally:
            self.configure(cursor="")

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
    App().mainloop()


if __name__ == "__main__":
    main()
