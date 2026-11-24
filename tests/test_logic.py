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


def test_same_name_labels():
    check("シリアルの表示形式", app.fmt_serial(0xA3F29B1C), "A3F2-9B1C")
    check("先頭のゼロを落とさない", app.fmt_serial(0x00012ABC), "0001-2ABC")
    check("None は空文字", app.fmt_serial(None), "")
    check("0 は空文字", app.fmt_serial(0), "")

    check("記録時のつづりで出す", app.recorded_subpath("E:\\Rec\\Anime", "rec\\anime"), "Rec\\Anime")
    check("ドライブ直下の記録は空", app.recorded_subpath("E:\\", ""), "")
    check("食い違うときは保存値", app.recorded_subpath("E:\\Other", "rec\\anime"), "rec\\anime")

    rows = [  # 名前, シリアル, subpath, source
        ("動画保管01", 0xA3F29B1C, "", "E:\\"),
        ("音楽HDD", 0x11112222, "", "F:\\"),
        ("作業用", 0xA3F29B1C, "rec\\anime", "E:\\Rec\\Anime"),
    ]
    check("重複が無ければ従来どおり", app.disk_labels(rows), ["動画保管01", "音楽HDD", "作業用"])
    rows += [
        ("動画保管01", 0x5555AAAA, "", "G:\\"),                    # 別のディスクが同名
        ("作業用", 0xA3F29B1C, "rec\\drama", "E:\\Rec\\Drama"),      # 同じボリュームの別フォルダが同名
        ("旧記録", None, "", "H:\\"), ("旧記録", None, "old\\x", "H:\\Old\\X"),
    ]
    check("同名のときだけ見分けを添える", app.disk_labels(rows), [
        "動画保管01 (A3F2-9B1C)", "音楽HDD", "作業用 (A3F2-9B1C / Rec\\Anime)",
        "動画保管01 (5555-AAAA)", "作業用 (A3F2-9B1C / Rec\\Drama)",
        "旧記録", "旧記録 (Old\\X)"])
    check("片方を消すと添え表示も消える", app.disk_labels(rows[1:3] + rows[3:4]),
          ["音楽HDD", "作業用", "動画保管01"])


def test_name_clash_message():
    msg = app.name_clash_message("動画保管01", (0xA3F29B1C, 2 * 1024 ** 4, 0, "E:\\"),
                                 ("G:\\", 0x5555AAAA, "NEWHDD", 4 * 1024 ** 4), others=0)
    for part in ("シリアル番号が違うため、別のディスクと思われます。",
                 "【既存の記録】", "シリアル: A3F2-9B1C", "容量: 2.00 TB", "記録元: E:\\",
                 "【今回のディスク】", "場所: G:\\", "シリアル: 5555-AAAA", "ラベル: NEWHDD", "容量: 4.00 TB",
                 "いいえ: 同じ名前のまま、別のディスクとして記録する"):
        check(f"確認文に「{part}」がある", part in msg, True)
    check("ほかの同名が無ければ注記なし", "ほかに" in msg, False)
    old = app.name_clash_message("X", (None, None, None, ""), ("G:\\", 1, "", None), others=2)
    check("シリアル不明の旧記録では言い切らない", "確かめられません" in old and "別のディスクと思われます" not in old, True)
    check("ほかの同名の件数を知らせる", "ほかに 2 件" in old, True)


def test_hidden_export():
    root = os.path.join(WORK, "diskH")
    write(os.path.join(root, "公開", "a.mkv"), b"1" * 100)
    write(os.path.join(root, "公開", "ひみつ", "x.mkv"), b"2" * 1000)
    write(os.path.join(root, "公開", "ひみつ", "深い", "y.mkv"), b"3" * 2000)
    write(os.path.join(root, "公開", "b.txt"), b"4" * 10)
    write(os.path.join(root, "はずかしい.iso"), b"5" * 500)
    write(os.path.join(root, "ひみつでない", "z.mkv"), b"6" * 7)
    did = scan(root, "H")[1]
    conn = app.connect()

    def mark(path):
        eid, parent = None, 0
        for part in path.split("\\"):
            eid = parent = conn.execute("SELECT id FROM entries WHERE disk_id=? AND parent_id=? AND name=?",
                                        (did, parent, part)).fetchone()[0]
        with conn:
            conn.execute("INSERT INTO hidden(disk_id,path,entry_id) VALUES(?,?,?)", (did, path, eid))

    def export(hide=True):
        stats = {}
        rows = list(app.export_rows(conn, did, "H", hide, stats))
        return {r[1]: r[4] for r in rows}, stats.get("skipped", 0)

    everything, _ = export()
    check("設定が無ければ全部出る", len(everything), 10)
    check("フォルダの合計(隠す前)", (everything["公開"], everything["公開\\ひみつ"]), (3110, 3000))

    mark("公開\\ひみつ")           # フォルダ(中身ごと)
    mark("公開\\ひみつ\\深い")     # その中をさらに設定しても、二重に引かない
    mark("はずかしい.iso")         # ファイル
    got, skipped = export()
    check("隠したフォルダ・その中身・隠したファイルが出ない", sorted(got),
          ["ひみつでない", "ひみつでない\\z.mkv", "公開", "公開\\a.mkv", "公開\\b.txt"])
    check("名前が似ているだけのフォルダは出る", "ひみつでない" in got, True)
    check("除いた行数", skipped, 5)
    check("上のフォルダの合計から、隠した分を引く", got["公開"], 110)
    check("どの行にも隠した名前が残らない", [p for p in got if "ひみつ\\" in p or p.endswith("ひみつ") or "はずかしい" in p], [])
    full, skipped_full = export(hide=False)
    check("「全部書き出す」なら今までどおり", (len(full), skipped_full, full["公開"]), (10, 0, 3110))

    # 記録し直しても、同じ場所に付いたまま
    write(os.path.join(root, "公開", "ひみつ", "新しい.mkv"), b"7" * 50)
    r = scan(root, "H", replace_id=did)
    did = r[1]
    check("記録し直しても設定は残る(見つからないもの無し)", r[7], [])
    got, _ = export()
    check("あとから足したファイルも隠れる", sorted(got),
          ["ひみつでない", "ひみつでない\\z.mkv", "公開", "公開\\a.mkv", "公開\\b.txt"])
    check("合計も引き直される", got["公開"], 110)

    # フォルダの名前を変えると、設定の場所が見つからなくなる → 知らせる
    os.rename(os.path.join(root, "公開", "ひみつ"), os.path.join(root, "公開", "改名した"))
    r = scan(root, "H", replace_id=did)
    did = r[1]
    check("名前を変えたら、見つからない設定として返す", sorted(r[7]), ["公開\\ひみつ", "公開\\ひみつ\\深い"])
    marks = app.hidden_marks(conn, [did])
    check("設定は消さずに残す(見つからない印つき)",
          [(p, e is not None) for _d, p, e in marks],
          [("はずかしい.iso", True), ("公開\\ひみつ", False), ("公開\\ひみつ\\深い", False)])
    r = scan(root, "H", replace_id=did)
    check("同じ知らせを繰り返さない", r[7], [])
    os.rename(os.path.join(root, "公開", "改名した"), os.path.join(root, "公開", "ひみつ"))
    r = scan(root, "H", replace_id=r[1])
    got = {row[1] for row in app.export_rows(conn, r[1], "H", True)}
    check("元の名前に戻せば、また隠れる", any("ひみつ\\" in p for p in got), False)
    conn.close()


def main():
    try:
        for fn in (test_settings, test_summary, test_info_csv_inherit, test_connected_records, test_diff,
                   test_same_name_labels, test_name_clash_message, test_hidden_export):
            fn()
            print(f"OK  {fn.__name__}")
    finally:
        shutil.rmtree(WORK, ignore_errors=True)
    print(f"すべて通りました ({PASSED} 項目)")


if __name__ == "__main__":
    main()
