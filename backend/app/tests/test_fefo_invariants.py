"""FEFO 不变量参数化测例: 引擎先行 -> HTTP 复核 -> 全层/层页余量对账。

每行用例流程:
  1. fefo_factory 造数;
  2. consume 步: 先按 /api/consume 同款查询取数、直接调 consume_fefo,
     再 POST /api/consume, 两者结果必须一致, 且与写死的期望列一致;
     sweep 步: 先调 expire_lots, 再 POST /api/expire-sweep;
  3. 读 /api/fridge 全层与每个涉及层的层页, 与写死的最终余量对账,
     对不上即失败并打印用例键名。

写回口径(与现行 app/main.py 一致, 期望列写死):
  - 成功: 按 deductions 逐批扣减; 扣尽的批次 status='consumed' 且 qty_remain 归零;
  - 数量非正: 400, 零写回;
  - 在架正余量不足: 409, 零写回(整单不落库, deductions 仅在响应 detail 回显)。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.engines.fefo import consume_fefo, expire_lots
from app.main import app
from app.tests import fefo_factory as fx

# ---------------------------------------------------------------------------
# 参数化表格。lots 按下标引用; final 为 {批次下标: (status, 余量)}, 全部写死。
# ---------------------------------------------------------------------------
CASES = [
    # 两批到期有先后: 只扣较早一批, 较晚一批原样不动
    {
        "key": "earlier_expiry_only",
        "lots": [
            {"item": "A", "qty": 2, "exp": 4},
            {"item": "A", "qty": 5, "exp": 30},
        ],
        "steps": [
            {"op": "consume", "item": "A", "qty": 1.5, "http": 200,
             "deductions": [{"lot": 0, "take": 1.5}]},
        ],
        "final": {0: ("on_shelf", 0.5), 1: ("on_shelf", 5.0)},
    },
    # 跨批 take: 早批扣尽(consumed 归零), 晚批补足, take 可加总等于请求量
    {
        "key": "cross_batch_take_sums",
        "lots": [
            {"item": "A", "qty": 2, "exp": 4},
            {"item": "A", "qty": 5, "exp": 30},
        ],
        "steps": [
            {"op": "consume", "item": "A", "qty": 4, "http": 200,
             "deductions": [{"lot": 0, "take": 2}, {"lot": 1, "take": 2}]},
        ],
        "final": {0: ("consumed", 0.0), 1: ("on_shelf", 3.0)},
    },
    # 数量非正: 0 -> 引擎拒绝, HTTP 400, 零写回
    {
        "key": "qty_zero_rejected",
        "lots": [{"item": "A", "qty": 2, "exp": 4}],
        "steps": [
            {"op": "consume", "item": "A", "qty": 0, "http": 400},
        ],
        "final": {0: ("on_shelf", 2.0)},
    },
    # 数量非正: 负数 -> 引擎拒绝, HTTP 400, 零写回
    {
        "key": "qty_negative_rejected",
        "lots": [{"item": "A", "qty": 2, "exp": 4}],
        "steps": [
            {"op": "consume", "item": "A", "qty": -3, "http": 400},
        ],
        "final": {0: ("on_shelf", 2.0)},
    },
    # 在架正余量不够: 409, 现行口径 = 整单零写回(不是部分写出),
    # deductions 只在 409 detail 里回显, lots 表一行不动
    {
        "key": "short_zero_write",
        "lots": [
            {"item": "A", "qty": 1, "exp": 4},
            {"item": "A", "qty": 1, "exp": 30},
        ],
        "steps": [
            {"op": "consume", "item": "A", "qty": 5, "http": 409, "short": 3.0,
             "deductions": [{"lot": 0, "take": 1}, {"lot": 1, "take": 1}]},
        ],
        "final": {0: ("on_shelf", 1.0), 1: ("on_shelf", 1.0)},
    },
    # dirty / 负余量批次永不出现在 deductions(含到期最早的陷阱位)
    {
        "key": "dirty_negative_never_deducted",
        "lots": [
            {"item": "A", "qty": 2, "exp": 10},
            {"item": "A", "qty": -3, "exp": 2, "quality": "dirty"},  # 到期最早, 陷阱位
            {"item": "A", "qty": 0, "exp": 3, "quality": "dirty"},
            {"item": "A", "qty": 1, "exp": 20},
        ],
        "steps": [
            {"op": "consume", "item": "A", "qty": 1.5, "http": 200,
             "deductions": [{"lot": 0, "take": 1.5}]},
        ],
        "final": {
            0: ("on_shelf", 0.5),
            1: ("on_shelf", -3.0),  # 负余量批次留在层页上, 但永不被扣
            2: ("on_shelf", 0.0),
            3: ("on_shelf", 1.0),
        },
    },
    # 消费与下架交错: 已 expired 的批次此后绝不被 HTTP 扣减, 全层页也不得出现
    {
        "key": "interleave_consume_expire_sweep",
        "lots": [
            {"item": "A", "qty": 2, "exp": -2},  # 步骤1先被扣1, 步骤2下架
            {"item": "A", "qty": 3, "exp": 20},
            {"item": "B", "qty": 4, "exp": -4},  # 步骤2下架
            {"item": "B", "qty": 2, "exp": 30},
            {"item": "C", "qty": -3, "exp": 40, "quality": "dirty"},  # 负余量: 不下架不扣减
        ],
        "steps": [
            {"op": "consume", "item": "A", "qty": 1, "http": 200,
             "deductions": [{"lot": 0, "take": 1}]},
            {"op": "sweep", "expired": [0, 2]},
            {"op": "consume", "item": "A", "qty": 1, "http": 200,
             "deductions": [{"lot": 1, "take": 1}]},
            {"op": "consume", "item": "B", "qty": 1, "http": 200,
             "deductions": [{"lot": 3, "take": 1}]},
            {"op": "sweep", "expired": []},
        ],
        "final": {
            0: ("expired", 1.0),  # 下架后余量原样保留, 未被后续 consume 碰
            1: ("on_shelf", 2.0),
            2: ("expired", 4.0),
            3: ("on_shelf", 1.0),
            4: ("on_shelf", -3.0),
        },
    },
]


def check(key: str, what: str, got, want) -> None:
    assert got == want, f"[case={key}] {what}: got={got!r} want={want!r}"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with TestClient(app) as c:
        yield c


@pytest.mark.parametrize("case", CASES, ids=[c["key"] for c in CASES])
def test_fefo_invariant(client, case):
    key = case["key"]
    fx.reset_db()
    item_ids, lot_ids = fx.insert_lots(case["lots"])
    expired_so_far: set[int] = set()

    for step in case["steps"]:
        if step["op"] == "sweep":
            # 先调引擎(expire_lots), 再打 HTTP(/api/expire-sweep)
            engine_ids = set(expire_lots(fx.load_on_shelf(), fx.iso(0)))
            resp = client.post("/api/expire-sweep")
            check(key, "sweep http status", resp.status_code, 200)
            got_ids = set(resp.json()["expired_ids"])
            check(key, "sweep ids == 引擎结果", got_ids, engine_ids)
            check(key, "sweep ids == 期望列", got_ids, {lot_ids[i] for i in step["expired"]})
            expired_so_far |= got_ids
            continue

        # consume 步: 先调引擎(与 HTTP 同款取数), 再打 HTTP
        item_id = item_ids[step["item"]]
        shelf = fx.load_consume_candidates(item_id)
        engine = consume_fefo(shelf, step["qty"])
        before = fx.snapshot_lots()
        resp = client.post(
            "/api/consume",
            json={"item_id": item_id, "qty": step["qty"], "note": key},
        )
        check(key, "consume http status", resp.status_code, step["http"])
        body = resp.json()
        want_ded = [
            {
                "lot_id": lot_ids[d["lot"]],
                "take": d["take"],
                "expiry": fx.iso(case["lots"][d["lot"]]["exp"]),
            }
            for d in step.get("deductions", [])
        ]

        if step["http"] == 200:
            check(key, "engine ok", engine["ok"], True)
            check(key, "http 响应 == 引擎结果", body, engine)
            check(key, "deductions == 期望列", body["deductions"], want_ded)
            check(key, "take 可加总", sum(d["take"] for d in body["deductions"]),
                  float(step["qty"]))
            shelf_by_id = {lot["id"]: lot for lot in shelf}
            for d in body["deductions"]:
                src = shelf_by_id.get(d["lot_id"])
                assert src is not None, f"[case={key}] 扣了不在架的批次 {d}"
                assert src["qty_remain"] > 0, f"[case={key}] 扣了非正余量批次 {src}"
                assert src["data_quality"] != "dirty", f"[case={key}] 扣了 dirty 批次 {src}"
                assert d["lot_id"] not in expired_so_far, (
                    f"[case={key}] 批次 {d['lot_id']} 已 expired 仍被 HTTP 扣减")
        elif step["http"] == 400:
            check(key, "engine 失败原因", engine["reason"], "qty_non_positive")
            check(key, "400 detail", body["detail"], "qty_non_positive")
            check(key, "400 零写回", fx.snapshot_lots(), before)
        elif step["http"] == 409:
            detail = body["detail"]
            check(key, "engine 失败原因", engine["reason"], "short")
            check(key, "409 回显 == 引擎结果", detail, engine)
            check(key, "409 short == 期望列", detail["short"], step["short"])
            check(key, "409 回显 deductions == 期望列", detail["deductions"], want_ded)
            # 现行口径: 不足则整单零写回, 不允许部分写出
            check(key, "409 零写回", fx.snapshot_lots(), before)
        else:  # pragma: no cover
            raise AssertionError(f"[case={key}] 未支持的期望状态 {step['http']}")

    # 读全层与对应层页余量对账
    all_page = {r["id"]: r for r in client.get("/api/fridge").json()}
    layers = sorted({fx.ITEM_CATALOG[s["item"]]["layer"] for s in case["lots"]})
    union: dict[int, dict] = {}
    for layer in layers:
        page = {r["id"]: r for r in client.get("/api/fridge", params={"layer": layer}).json()}
        for lid, row in page.items():
            assert lid in all_page, f"[case={key}] 层页 {layer} 的批次 {lid} 不在全层页"
            check(key, f"层页 {layer} 余量 == 全层页",
                  row["qty_remain"], all_page[lid]["qty_remain"])
            union[lid] = row
    check(key, "全层页 == 各层页并集", sorted(all_page), sorted(union))

    db = fx.snapshot_lots()
    for idx, (want_status, want_remain) in case["final"].items():
        lid = lot_ids[idx]
        check(key, f"批次#{idx} status", db[lid]["status"], want_status)
        check(key, f"批次#{idx} 余量", db[lid]["qty_remain"], want_remain)
        if want_status == "on_shelf":
            assert lid in all_page, f"[case={key}] 批次#{idx} 应在架却不在全层页"
            check(key, f"批次#{idx} 页上余量", all_page[lid]["qty_remain"], want_remain)
        else:
            assert lid not in all_page, (
                f"[case={key}] 批次#{idx} status={want_status} 不得出现在全层页")
