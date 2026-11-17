import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

import app as appmod
from app import app, init_db


@pytest.fixture(autouse=True)
def _db(tmp_path, monkeypatch):
    monkeypatch.setattr("app.DB_PATH", tmp_path / "t.db")
    init_db()
    yield


def _login(c, user="admin", pw="123456"):
    r = c.post("/api/login", json={"username": user, "password": pw})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _add_slice(c, h, no, biz_date, rows=10, expect=10):
    r = c.post("/api/slices", headers=h, json={
        "slice_no": no, "biz_date": biz_date, "partition_key": "p",
        "row_count": rows, "expected_count": expect})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_pipeline_baseline():
    c = TestClient(app)
    assert c.get("/api/health").json()["product"] == "日切放行"
    h = _login(c)
    assert len(c.get("/api/slices", headers=h).json()["items"]) >= 1
    bad = c.post("/api/slices/1/release", headers=h, json={"consumer": "x"})
    assert bad.status_code == 409
    sid = _add_slice(c, h, "SL-T-1", "2026-11-04")
    rel = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_x"})
    assert rel.status_code == 200
    assert any(w["source_slice"] == "SL-T-1" for w in c.get("/api/watermarks", headers=h).json()["items"])


def test_close_gate_blocks_release_and_watermark():
    c = TestClient(app)
    h = _login(c)
    sid = _add_slice(c, h, "SL-C-1", "2026-11-05")
    assert c.post("/api/day-closes", headers=h,
                  json={"biz_date": "2026-11-05", "mode": "甲", "note": "值班关日"}).status_code == 200
    before_wm = len(c.get("/api/watermarks", headers=h).json()["items"])

    rel = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_y"})
    assert rel.status_code == 409
    assert "关日" in rel.json()["detail"]
    assert len(c.get("/api/watermarks", headers=h).json()["items"]) == before_wm
    slice_row = [s for s in c.get("/api/slices", headers=h).json()["items"] if s["id"] == sid][0]
    assert slice_row["release_status"] == "未放行"

    # 单独的水位推进写入口同样硬拒绝
    wm = c.post("/api/watermarks", headers=h,
                json={"consumer": "ads_y", "biz_date": "2026-11-05", "source_slice": "SL-C-1"})
    assert wm.status_code == 409
    assert len(c.get("/api/watermarks", headers=h).json()["items"]) == before_wm

    # 关日后仅只读查询可用
    assert c.get("/api/snapshot", headers=h).status_code == 200


def test_closed_day_blocks_register_evaluate_and_patch():
    c = TestClient(app)
    h = _login(c)
    sid = _add_slice(c, h, "SL-C-2", "2026-11-06")
    rel = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_ok"})
    assert rel.status_code == 200
    release_id = [r for r in c.get("/api/releases", headers=h).json()["items"]
                  if r["slice_no"] == "SL-C-2"][0]["id"]
    assert c.post("/api/day-closes", headers=h, json={"biz_date": "2026-11-06", "mode": "甲"}).status_code == 200

    assert c.post("/api/slices", headers=h, json={
        "slice_no": "SL-C-2-late", "biz_date": "2026-11-06", "partition_key": "p",
        "row_count": 5, "expected_count": 10}).status_code == 409
    assert c.post(f"/api/slices/{sid}/evaluate", headers=h).status_code == 409

    patched = c.patch(f"/api/releases/{release_id}", headers=h, json={"note": "关日后改流水"})
    assert patched.status_code == 409
    assert "关日口径偏了" in patched.json()["detail"]


def test_second_gate_mixed_mode_rejected_and_audited():
    c = TestClient(app)
    h = _login(c)
    bad = c.post("/api/day-closes", headers=h, json={"biz_date": "2026-11-07", "mode": "乙"})
    assert bad.status_code == 409
    assert "关日口径偏了" in bad.json()["detail"]

    late = c.post("/api/late-freezes", headers=h, json={"biz_date": "2026-11-07", "slice_no": "x"})
    assert late.status_code == 409
    assert "关日口径偏了" in late.json()["detail"]

    notes = [a["note"] for a in c.get("/api/audit", headers=h).json()["items"]]
    assert any("关日口径偏了" in n for n in notes)


def test_close_day_is_immutable_within_day():
    c = TestClient(app)
    h = _login(c)
    assert c.post("/api/day-closes", headers=h, json={"biz_date": "2026-11-08", "mode": "甲"}).status_code == 200
    again = c.post("/api/day-closes", headers=h, json={"biz_date": "2026-11-08", "mode": "甲"})
    assert again.status_code == 409
    assert "不许更换口径" in again.json()["detail"]
    rows = c.get("/api/day-closes", headers=h).json()["items"]
    assert len([r for r in rows if r["biz_date"] == "2026-11-08"]) == 1


def test_close_and_release_contention_recorded():
    c = TestClient(app)
    h = _login(c)
    _add_slice(c, h, "SL-C-3", "2026-11-09")

    # 模拟另一值班已持该业务日排队锁，后手必须被拒并留痕，禁默成。
    lock = appmod.day_lock("2026-11-09")
    lock.acquire()
    try:
        close = c.post("/api/day-closes", headers=h, json={"biz_date": "2026-11-09", "mode": "甲"})
        assert close.status_code == 409
        assert "关日争用" in close.json()["detail"]
        rel = c.post("/api/slices/1/release", headers=h, json={"consumer": "ads_z"})
        assert rel.status_code == 409
    finally:
        lock.release()

    notes = [a["note"] for a in c.get("/api/audit", headers=h).json()["items"]]
    assert any("关日争用" in n for n in notes)


def test_real_concurrent_close_and_release_never_leaks_past_gate():
    c = TestClient(app)
    h = _login(c)
    biz = "2026-11-12"
    sid = _add_slice(c, h, "SL-C-5", biz)

    def close():
        return c.post("/api/day-closes", headers=h, json={"biz_date": biz, "mode": "甲"})

    def release():
        return c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_race"})

    threads = [threading.Thread(target=close), threading.Thread(target=release)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = c.get("/api/snapshot", headers=h).json()
    close_row = [x for x in snap["day_closes"] if x["biz_date"] == biz]
    ok_rel = [r for r in snap["releases"] if r["biz_date"] == biz and r["status"] == "成功"]
    # 放行若成功，必须发生在关日之前；关日后永远不得再有成功放行。
    if close_row and ok_rel:
        assert ok_rel[0]["created_at"] <= close_row[0]["closed_at"]
    # 争用的后手必须留痕，禁默成。
    if not close_row:
        assert any("关日争用" in a["note"] for a in snap["audit"])
    assert snap["consistency"]["ok"] is True, snap["consistency"]


def test_repeat_release_rejected_before_close():
    c = TestClient(app)
    h = _login(c)
    sid = _add_slice(c, h, "SL-R-1", "2026-11-13")
    assert c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_a"}).status_code == 200
    again = c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_b"})
    assert again.status_code == 409
    assert "禁止重复放行" in again.json()["detail"]
    ok = [r for r in c.get("/api/releases", headers=h).json()["items"]
          if r["slice_no"] == "SL-R-1" and r["status"] == "成功"]
    assert len(ok) == 1
    # 关日仍可正常登记
    assert c.post("/api/day-closes", headers=h,
                  json={"biz_date": "2026-11-13", "mode": "甲"}).status_code == 200


def test_snapshot_reads_same_db_and_consistent_after_gate():
    c = TestClient(app)
    h = _login(c)
    sid = _add_slice(c, h, "SL-C-4", "2026-11-10")
    assert c.post(f"/api/slices/{sid}/release", headers=h, json={"consumer": "ads_q"}).status_code == 200
    assert c.post("/api/day-closes", headers=h, json={"biz_date": "2026-11-10", "mode": "甲"}).status_code == 200

    snap = c.get("/api/snapshot", headers=h).json()
    assert snap["policy"]["mode"] == "甲"
    assert snap["policy"]["locked"] is True
    row = [x for x in snap["catalog"] if x["biz_date"] == "2026-11-10"][0]
    rels = [r for r in snap["releases"] if r["biz_date"] == "2026-11-10" and r["status"] == "成功"]
    assert row["closed"] is True
    assert row["released"] == len(rels) == 1
    assert snap["consistency"]["ok"] is True, snap["consistency"]

    # 关日画面与放行流水读数不得错位：同一快照逐日对账
    for cat in snap["catalog"]:
        ok_rel = [r for r in snap["releases"] if r["biz_date"] == cat["biz_date"] and r["status"] == "成功"]
        assert cat["released"] <= len(ok_rel)


def test_closed_day_marker_lives_in_server_db_only():
    c = TestClient(app)
    h = _login(c)
    assert c.post("/api/day-closes", headers=h, json={"biz_date": "2026-11-11", "mode": "甲"}).status_code == 200
    conn = sqlite3.connect(appmod.DB_PATH)
    try:
        n = conn.execute("SELECT COUNT(*) FROM day_closes WHERE biz_date='2026-11-11'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1
    assert c.get("/api/slices", headers=h).status_code == 200
