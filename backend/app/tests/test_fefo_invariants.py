"""FEFO 不变量参数化测例。

每一行（表格键名 = ROWS[*].key）独立造数，每个 consume 步骤严格按：
  1) 先调引擎 consume_fefo（候选取数口径与 /api/consume 完全一致）；
  2) 再打 HTTP POST /api/consume；
  3) 读全层（DB 全量 lots 快照）与对应层页（GET /api/fridge?layer=）余量；
  4) 与写死的期望列逐项对账，对不上即失败并在断言信息里打印表格键名。

覆盖（至少）：
  earlier_only          两批到期先后，只扣较早那批；
  cross_lot_sum         跨批 take 可加总，扣完的批置 consumed、余量钳 0；
  qty_non_positive      数量非正（0、负数）失败 400，零写出；
  short_zero_write      在架正余量不够，409，零写出（与现行写回同一口径）；
  dirty_negative_skip   dirty 或负余量永不出现在 deductions；
  expire_interleaved    消费与下架交错，status 已 expired 的批永不被 HTTP 扣减。
"""

import pytest

from app.tests.fefo_case_factory import (
    LotSpec,
    build_case,
    teardown_case,
)

LAYER = "upper"
EPS = 1e-9


def _close(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) <= EPS


def _ded_equal(actual: list[tuple[str, float]], expected: list[tuple[str, float]]) -> bool:
    if len(actual) != len(expected):
        return False
    return all(a[0] == e[0] and _close(a[1], e[1]) for a, e in zip(actual, expected))


# ---------------------------------------------------------------------------
# 参数表：期望列（ded / remain / status / page / short）全部写死
# ---------------------------------------------------------------------------
ROWS = [
    {
        "key": "earlier_only",
        "desc": "两批到期先后，只扣较早到期的批",
        "lots": [
            LotSpec("A", qty_remain=5, expiry_offset=10),
            LotSpec("B", qty_remain=3, expiry_offset=2),   # 较早
        ],
        "steps": [
            {
                "op": "consume", "qty": 2,
                "http": 200, "engine_ok": True, "reason": "", "short": 0.0,
                "ded": [("B", 2)],
                "remain": {"A": 5, "B": 1},
                "status": {"A": "on_shelf", "B": "on_shelf"},
                "page": {"A": 5, "B": 1},
            },
        ],
    },
    {
        "key": "cross_lot_sum",
        "desc": "跨批 take 可加总；较早批扣光置 consumed、余量钳 0",
        "lots": [
            LotSpec("A", qty_remain=2, expiry_offset=1),
            LotSpec("B", qty_remain=3, expiry_offset=5),
        ],
        "steps": [
            {
                "op": "consume", "qty": 4,
                "http": 200, "engine_ok": True, "reason": "", "short": 0.0,
                "ded": [("A", 2), ("B", 2)],       # 2 + 2 = 4
                "remain": {"A": 0, "B": 1},
                "status": {"A": "consumed", "B": "on_shelf"},
                "page": {"B": 1},                   # A 已 consumed，层页不再显示
            },
        ],
    },
    {
        "key": "qty_non_positive",
        "desc": "数量为 0 或负数：引擎失败、HTTP 400、零写出",
        "lots": [
            LotSpec("A", qty_remain=3, expiry_offset=3),
        ],
        "steps": [
            {
                "op": "consume", "qty": 0,
                "http": 400, "engine_ok": False, "reason": "qty_non_positive",
                "short": 0.0, "ded": [],
                "remain": {"A": 3},
                "status": {"A": "on_shelf"},
                "page": {"A": 3},
            },
            {
                "op": "consume", "qty": -3,
                "http": 400, "engine_ok": False, "reason": "qty_non_positive",
                "short": 0.0, "ded": [],
                "remain": {"A": 3},
                "status": {"A": "on_shelf"},
                "page": {"A": 3},
            },
        ],
    },
    {
        "key": "short_zero_write",
        "desc": "在架正余量不够：409 short，deductions 只是计划，DB 零写出",
        "lots": [
            LotSpec("A", qty_remain=2, expiry_offset=1),
        ],
        "steps": [
            {
                "op": "consume", "qty": 5,
                "http": 409, "engine_ok": False, "reason": "short", "short": 3.0,
                # 现行口径：short 时引擎给部分计划，HTTP 把它放进 409 detail，
                # 但不执行任何 UPDATE（零写出）。
                "ded": [("A", 2)],
                "remain": {"A": 2},                 # 余量原样
                "status": {"A": "on_shelf"},
                "page": {"A": 2},
            },
        ],
    },
    {
        "key": "dirty_negative_skip",
        "desc": "dirty（含最早到期）与负余量批永不进 deductions，也不参与 short 计划",
        "lots": [
            LotSpec("C", qty_remain=4, expiry_offset=5, dq="clean"),
            LotSpec("D", qty_remain=2, expiry_offset=1, dq="dirty"),   # 最早但 dirty
            LotSpec("N", qty_remain=-3, expiry_offset=2, dq="dirty"),  # 负余量
        ],
        "steps": [
            {
                "op": "consume", "qty": 3,
                "http": 200, "engine_ok": True, "reason": "", "short": 0.0,
                "ded": [("C", 3)],
                "remain": {"C": 1, "D": 2, "N": -3},
                "status": {"C": "on_shelf", "D": "on_shelf", "N": "on_shelf"},
                # /api/fridge 只按 status='on_shelf' 过滤：dirty/负余量在架批仍上页
                "page": {"C": 1, "D": 2, "N": -3},
            },
            {
                "op": "consume", "qty": 5,
                "http": 409, "engine_ok": False, "reason": "short", "short": 4.0,
                # 即便不够，也只计划 clean 正余量批 C，D/N 永不出现
                "ded": [("C", 1)],
                "remain": {"C": 1, "D": 2, "N": -3},                  # 零写出
                "status": {"C": "on_shelf", "D": "on_shelf", "N": "on_shelf"},
                "page": {"C": 1, "D": 2, "N": -3},
            },
        ],
    },
    {
        "key": "expire_interleaved",
        "desc": "消费与下架交错：sweep 后 status='expired' 的批永不被 HTTP 扣减",
        "lots": [
            LotSpec("E", qty_remain=2, expiry_offset=-2),   # 到期日已过但仍在架
            LotSpec("G", qty_remain=2, expiry_offset=-1),
            LotSpec("F", qty_remain=3, expiry_offset=4),
        ],
        "steps": [
            # 交错点 1：sweep 之前，到日期但 status 仍 on_shelf 的批照常 FEFO
            {
                "op": "consume", "qty": 1,
                "http": 200, "engine_ok": True, "reason": "", "short": 0.0,
                "ded": [("E", 1)],
                "remain": {"E": 1, "G": 2, "F": 3},
                "status": {"E": "on_shelf", "G": "on_shelf", "F": "on_shelf"},
                "page": {"E": 1, "G": 2, "F": 3},
            },
            # 下架：E(余1)、G(余2) 到期日已过且有余量 -> expired，离开层页
            {
                "op": "sweep",
                "expired": ["E", "G"],
                "remain": {"E": 1, "G": 2, "F": 3},
                "status": {"E": "expired", "G": "expired", "F": "on_shelf"},
                "page": {"F": 3},
            },
            # 交错点 2：只扣仍在架的 F，E/G 即便余量为正也不得出现
            {
                "op": "consume", "qty": 2,
                "http": 200, "engine_ok": True, "reason": "", "short": 0.0,
                "ded": [("F", 2)],
                "remain": {"E": 1, "G": 2, "F": 1},
                "status": {"E": "expired", "G": "expired", "F": "on_shelf"},
                "page": {"F": 1},
            },
            # 交错点 3：不足时 short，计划里同样不得出现 expired 批，零写出
            {
                "op": "consume", "qty": 5,
                "http": 409, "engine_ok": False, "reason": "short", "short": 4.0,
                "ded": [("F", 1)],
                "remain": {"E": 1, "G": 2, "F": 1},
                "status": {"E": "expired", "G": "expired", "F": "on_shelf"},
                "page": {"F": 1},
            },
        ],
    },
]


@pytest.fixture(params=ROWS, ids=[r["key"] for r in ROWS])
def case(request):
    row = request.param
    data = build_case(row["key"], row["lots"], layer=LAYER)
    yield row, data
    teardown_case(data)


def _run_consume(row, data, step, errs):
    key, idx = row["key"], step.get("_i")
    pre = data.snapshot()                       # 引擎/HTTP 之前的全层状态

    # 1) 先调引擎
    engine_result = data.engine_consume(step["qty"])
    # 2) 再打 HTTP
    code, payload = data.http_consume(step["qty"], note=f"{key}:{idx}")

    e_ded = data.engine_deductions(engine_result)
    h_ded = data.http_deductions(code, payload)

    def chk(cond, msg):
        if not cond:
            errs.append(f"[{key}] step{idx} {msg}")

    # --- 引擎期望 ---
    chk(engine_result.get("ok") is step["engine_ok"],
        f"engine.ok 期望 {step['engine_ok']} 实得 {engine_result.get('ok')}")
    chk(engine_result.get("reason", "") == step["reason"],
        f"engine.reason 期望 {step['reason']!r} 实得 {engine_result.get('reason')!r}")
    chk(_close(engine_result.get("short", 0.0), step["short"]),
        f"engine.short 期望 {step['short']} 实得 {engine_result.get('short')}")
    chk(_ded_equal(e_ded, step["ded"]), f"engine.deductions 期望 {step['ded']} 实得 {e_ded}")

    # --- HTTP 期望，并与引擎计划同口径 ---
    chk(code == step["http"], f"HTTP 状态 期望 {step['http']} 实得 {code} payload={payload}")
    chk(_ded_equal(h_ded, step["ded"]), f"HTTP.deductions 期望 {step['ded']} 实得 {h_ded}")
    chk(_ded_equal(h_ded, e_ded), f"HTTP 与引擎 deductions 不一致: http={h_ded} engine={e_ded}")

    # --- 不变量：被扣批在扣减前必须在架、正余量、非 dirty；expired 永不变动 ---
    for alias, take in h_ded:
        lot = pre.get(alias)
        chk(lot is not None, f"deduction 引用了不存在的批 {alias}")
        if lot is not None:
            chk(lot["status"] == "on_shelf",
                f"非在架批 {alias}(status={lot['status']}) 出现在 deductions")
            chk(lot["data_quality"] != "dirty",
                f"dirty 批 {alias} 出现在 deductions")
            chk(float(lot["qty_remain"]) > 0,
                f"非正余量批 {alias}(qty={lot['qty_remain']}) 出现在 deductions")
            chk(take > 0, f"批 {alias} 的 take 非正: {take}")

    # 3) 读全层
    post = data.snapshot()
    for alias, exp_rem in step["remain"].items():
        got = post[alias]["qty_remain"]
        chk(_close(got, exp_rem), f"全层余量 {alias} 期望 {exp_rem} 实得 {got}")
    for alias, exp_status in step["status"].items():
        got = post[alias]["status"]
        chk(got == exp_status, f"全层状态 {alias} 期望 {exp_status} 实得 {got}")

    # expired 批余量必须与扣减前完全一致（status 已 expired 不得被 HTTP 扣减）
    for alias, lot in pre.items():
        if lot["status"] == "expired":
            chk(alias not in [a for a, _ in h_ded],
                f"expired 批 {alias} 出现在 HTTP deductions")
            chk(_close(post[alias]["qty_remain"], lot["qty_remain"]),
                f"expired 批 {alias} 余量被改动: {lot['qty_remain']} -> {post[alias]['qty_remain']}")

    # 4) 读对应层页余量，并与写死期望、全层在架子集对账
    page = data.layer_page_remaining(LAYER)
    chk(_page_close(page, step["page"]),
        f"层页余量 期望 {step['page']} 实得 {page}")
    on_shelf_from_full = {
        a: post[a]["qty_remain"] for a in step["remain"] if post[a]["status"] == "on_shelf"
    }
    chk(_page_close(page, on_shelf_from_full),
        f"层页与全层在架子集对不上: page={page} on_shelf={on_shelf_from_full}")


def _page_close(actual: dict, expected: dict) -> bool:
    if set(actual) != set(expected):
        return False
    return all(_close(actual[k], expected[k]) for k in expected)


def _run_sweep(row, data, step, errs):
    key, idx = row["key"], step.get("_i")
    code, payload = data.sweep()

    def chk(cond, msg):
        if not cond:
            errs.append(f"[{key}] step{idx} {msg}")

    chk(code == 200, f"sweep HTTP 状态 期望 200 实得 {code}")
    got_exp = set(data.alias2id[a] for a in step["expired"])
    chk(set(payload.get("expired_ids", [])) == got_exp,
        f"sweep expired_ids 期望 {step['expired']} 实得 {payload}")

    post = data.snapshot()
    for alias, exp_rem in step["remain"].items():
        chk(_close(post[alias]["qty_remain"], exp_rem),
            f"全层余量 {alias} 期望 {exp_rem} 实得 {post[alias]['qty_remain']}")
    for alias, exp_status in step["status"].items():
        chk(post[alias]["status"] == exp_status,
            f"全层状态 {alias} 期望 {exp_status} 实得 {post[alias]['status']}")
    page = data.layer_page_remaining(LAYER)
    chk(_page_close(page, step["page"]), f"层页余量 期望 {step['page']} 实得 {page}")


def test_fefo_invariants(case):
    row, data = case
    errs = []
    for i, step in enumerate(row["steps"]):
        step["_i"] = i
        if step["op"] == "consume":
            _run_consume(row, data, step, errs)
        elif step["op"] == "sweep":
            _run_sweep(row, data, step, errs)
        else:
            errs.append(f"[{row['key']}] 未知步骤类型 {step['op']}")
    assert not errs, "FEFO 不变量失败（表格键名=%s）:\n  %s" % (
        row["key"], "\n  ".join(errs))
