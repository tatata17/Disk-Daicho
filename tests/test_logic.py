#!/usr/bin/env python3
"""画面を出さずに確かめられる部分のテスト。

  python tests/test_logic.py

設定の保存・復元、詳細設定の要約、csv読み取りの引き継ぎ、接続中ディスクの判定、
再記録の差分を確かめる。tkinter が無い環境でも動く(画面は使わない)。
記録データは一時フォルダに作り、手元の disk_daicho.db には触れない。
"""
import os
import queue
import shutil
import sys
import tempfile
import threading
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent

try:
    import tkinter  # noqa: F401
except ImportError:  # 画面の部品は使わないので、無ければ代わりを置く
    for name in ("tkinter", "tkinter.filedialog", "tkinter.messagebox",
                 "tkinter.simpledialog", "tkinter.ttk"):
        sys.modules[name] = mock.MagicMock()
    sys.modules["tkinter"].Tk = object

WORK = tempfile.mkdtemp(prefix="disk_daicho_test_")
sys.argv = [os.path.join(WORK, "disk_daicho.pyw")]   # db はこのフォルダに作られる
app = SourceFileLoader("disk_daicho", str(ROOT / "disk_daicho.pyw")).load_module()

PASSED = 0


def check(label, got, want):
    global PASSED
    if got != want:
        raise AssertionError(f"{label}\n  期待: {want!r}\n  実際: {got!r}")
    PASSED += 1


def scan(root, name, folders_only=False, max_depth=None, replace_id=None, read_info=True):
    q = queue.Queue()
    app.scan_disk(root, name, folders_only, max_depth, replace_id, threading.Event(), q, read_info)
    msg = q.get()
    while msg[0] == "progress":
        msg = q.get()
    assert msg[0] == "done", msg
    return msg


def write(path, data=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)


def test_settings():
    conn = app.connect()
    check("初期値", app.load_rec_options(conn),
          {"folders_only": False, "depth_mode": False, "depth": 2, "info_csv": True})
    opts = {"folders_only": True, "depth_mode": True, "depth": 5, "info_csv": False}
    app.save_rec_options(conn, opts)
    conn.close()
    conn = app.connect()   # 起動し直したのと同じ
    check("保存した値が戻る", app.load_rec_options(conn), opts)
    with conn:
        conn.execute("UPDATE settings SET value='abc' WHERE key='rec_depth'")
    check("壊れた階層数は初期値", app.load_rec_options(conn)["depth"], 2)
    app.save_rec_options(conn, {"folders_only": False, "depth_mode": False, "depth": 2, "info_csv": True})
    conn.close()


def test_summary():
    check("初期値の要約", app.rec_summary(False, False, 2, True),
          ("フォルダ＋ファイル / 全階層 / csv読取ON", True))
    check("フォルダのみは要約から分かる", app.rec_summary(True, False, 2, True),
          ("フォルダのみ / 全階層 / csv読取ON", False))
    check("階層指定とcsvOFF", app.rec_summary(False, True, 3, False),
          ("フォルダ＋ファイル / 3階層まで / csv読取OFF", False))


def sidecar(path, title):
    with open(path, "w", encoding="utf-16", newline="") as f:
        f.write(f"00001.m2ts\t{title}\t2026/11/01\t19:00:00\tテスト放送\t内容\r\n")


def test_info_csv_inherit():
    root = os.path.join(WORK, "diskA")
    write(os.path.join(root, "rec", "番組A.mkv"))
    sidecar(os.path.join(root, "rec", "番組A.csv"), "番組A")
    conn = app.connect()

    d1 = scan(root, "A", read_info=True)[1]
    check("ONで記録 → 保存値ON", (app.disk_info_csv(conn, d1), app.info_csv_source(conn, d1)), (True, "saved"))
    check("番組情報が入る", conn.execute("SELECT COUNT(*) FROM programs WHERE disk_id=?", (d1,)).fetchone()[0], 1)

    d2 = scan(root, "A", replace_id=d1, read_info=False)[1]
    check("OFFで更新 → 保存値OFF", (app.disk_info_csv(conn, d2), app.info_csv_source(conn, d2)), (False, "saved"))
    check("古いディスクの保存値は消える",
          conn.execute("SELECT COUNT(*) FROM settings WHERE key=?", (f"info_csv_{d1}",)).fetchone()[0], 0)

    # ワンクリック更新と同じ呼び方: 前回の値(OFF)を引き継ぐ
    d3 = scan(root, "A", replace_id=d2, read_info=app.disk_info_csv(conn, d2))[1]
    check("OFFを引き継ぐ", app.disk_info_csv(conn, d3), False)

    # 旧記録(保存値なし): 番組情報があれば ON と推定
    d4 = scan(root, "A", replace_id=d3, read_info=True)[1]
    with conn:
        conn.execute("DELETE FROM settings WHERE key=?", (f"info_csv_{d4}",))
    check("旧記録・番組情報あり", (app.disk_info_csv(conn, d4), app.info_csv_source(conn, d4)), (True, "programs"))
    d5 = scan(root, "A", replace_id=d4, read_info=app.disk_info_csv(conn, d4))[1]
    check("推定ONで更新しても番組情報が残る",
          conn.execute("SELECT COUNT(*) FROM programs WHERE disk_id=?", (d5,)).fetchone()[0], 1)

    # 旧記録(保存値なし)で番組情報も無い: 既定(ON)
    other = os.path.join(WORK, "diskPlain")
    write(os.path.join(other, "a.txt"))
    d6 = scan(other, "Plain")[1]
    with conn:
        conn.execute("DELETE FROM settings WHERE key=?", (f"info_csv_{d6}",))
    check("旧記録・番組情報なし", (app.disk_info_csv(conn, d6), app.info_csv_source(conn, d6)), (True, "default"))
    conn.close()


def test_connected_records():
    conn = app.connect()
    with conn:
        for table in ("disks", "entries", "programs", "notes"):   # 前のテストの記録を片づける
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM settings WHERE key LIKE 'info_csv_%'")
        rows = [  # name, source(記録時のパス), serial, subpath(小文字), recorded_at, sort_order
            ("動画保管01", "E:\\", 111, "", 100.0, 2),
            ("録画フォルダ", "E:\\Rec\\Anime", 111, "rec\\anime", 200.0, 1),
            ("外したHDD", "F:\\", 222, "", 300.0, 3),
            ("消えたフォルダ", "E:\\Old", 111, "old", 400.0, 4),
            ("動画保管01の古い記録", "D:\\", 111, "", 50.0, 5),
            ("番号なしの旧記録", "E:\\", None, "", 500.0, 6),
        ]
        conn.executemany("INSERT INTO disks(name,source,serial,subpath,recorded_at,sort_order) "
                         "VALUES(?,?,?,?,?,?)", rows)
    # 今は G: に繋がっている(記録したときは E: だった)
    drives = [("G:  TEST", "G:\\", "TEST", 111), ("C:  OS", "C:\\", "OS", 999)]
    exists = {"G:\\", "G:\\Rec\\Anime"}
    got = app.connected_records(conn, drives, isdir=lambda p: p in exists)
    check("今のドライブ文字で組み立て、実在するものだけ・並び順どおり",
          [(name, path) for _id, name, path, _t in got],
          [("録画フォルダ", "G:\\Rec\\Anime"), ("動画保管01", "G:\\")])
    check("繋がっていなければ空", app.connected_records(conn, [("C:", "C:\\", "", 999)]), [])
    check("シリアル番号が取れない環境では空", app.connected_records(conn, [("X", "/mnt/x", "", None)]), [])
    with conn:
        conn.execute("DELETE FROM disks")
    conn.close()


def test_diff():
    root = os.path.join(WORK, "diskD")
    write(os.path.join(root, "a.bin"), b"1" * 100)
    write(os.path.join(root, "sub", "b.bin"), b"2" * 200)
    write(os.path.join(root, "sub", "c.bin"), b"3" * 300)
    first = scan(root, "D")
    check("初回の記録に差分は無い", first[6], None)

    write(os.path.join(root, "sub", "new.bin"), b"4" * 1000)       # 追加
    r = scan(root, "D", replace_id=first[1])
    check("ファイル追加", r[6], (1, 0, 0, 1000, False))
    check("追加の文", app.fmt_diff(r[6]), "新規 1件 / 削除 0件 / サイズ変化 0件 / 合計 +1000 B")

    os.remove(os.path.join(root, "a.bin"))                         # 削除
    r2 = scan(root, "D", replace_id=r[1])
    check("ファイル削除", r2[6], (0, 1, 0, -100, False))

    write(os.path.join(root, "sub", "b.bin"), b"2" * 250)          # サイズ変化
    write(os.path.join(root, "sub2", "d.bin"), b"5" * 10)          # 新しいフォルダに追加
    os.remove(os.path.join(root, "sub", "c.bin"))
    r3 = scan(root, "D", replace_id=r2[1])
    check("追加・削除・サイズ変化が同時", r3[6], (1, 1, 1, 50 + 10 - 300, False))

    r4 = scan(root, "D", replace_id=r3[1])
    check("変化なし", r4[6], (0, 0, 0, 0, False))
    check("変化なしの文", app.fmt_diff(r4[6]), "前回の記録からファイルの変化はありません (合計 ±0)")

    # フォルダのみの記録は、フォルダで比べる
    f1 = scan(root, "Dfolders", folders_only=True)
    os.makedirs(os.path.join(root, "sub3"))
    shutil.rmtree(os.path.join(root, "sub2"))
    f2 = scan(root, "Dfolders", folders_only=True, replace_id=f1[1])
    check("フォルダのみ: フォルダで比較", f2[6], (1, 1, 0, -10, True))

    conn = app.connect()
    check("古い記録は残らない",
          conn.execute("SELECT COUNT(*) FROM disks WHERE name='D'").fetchone()[0], 1)
    conn.close()


def main():
    try:
        for fn in (test_settings, test_summary, test_info_csv_inherit, test_connected_records, test_diff):
            fn()
            print(f"OK  {fn.__name__}")
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    print(f"すべて通りました ({PASSED} 項目)")


if __name__ == "__main__":
    main()
