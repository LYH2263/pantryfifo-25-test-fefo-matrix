"""造数助手: FEFO 不变量测例的夹具构造与直连读取。

只负责造数/读数(直连 sqlite), 不发 HTTP; HTTP 对账在 test_fefo_invariants.py。
日期一律用相对今天的天数偏移渲染, 保证任何日期运行语义不变。
"""
from __future__ import annotations

from datetime import date, timedelta

from app import seed
from app.db import connect

# 固定商品目录: 规格键 -> 品名/层/单位。层用于全层页与分层页对账。
ITEM_CATALOG = {
    "A": {"name": "牛奶", "layer": "upper", "unit": "盒"},
    "B": {"name": "鸡蛋", "layer": "mid", "unit": "个"},
    "C": {"name": "冻饺", "layer": "lower", "unit": "袋"},
}


def iso(offset_days: int) -> str:
    """今天偏移 offset_days 天的 ISO 日期; 负数表示过去(会被 expire-sweep 下架)。"""
    return (date.today() + timedelta(days=offset_days)).isoformat()


def reset_db() -> None:
    """建 schema(与应用启动同一份 DDL)并清空业务表, 保证用例间互不影响。"""
    seed.init_db()
    c = connect()
    for table in ("lots", "items", "consumptions"):
        c.execute(f"DELETE FROM {table}")
    c.commit()
    c.close()


def insert_lots(lot_specs: list[dict]) -> tuple[dict[str, int], list[int]]:
    """按规格造数, 返回 (商品键 -> item_id, 批次 id 列表(与规格同序))。

    规格键: item(商品键) / qty(入库量) / remain(余量, 默认同 qty) /
    exp(到期日偏移天数) / status(默认 on_shelf) / quality(默认 clean)。
    """
    c = connect()
    item_ids: dict[str, int] = {}
    lot_ids: list[int] = []
    for spec in lot_specs:
        key = spec["item"]
        if key not in item_ids:
            meta = ITEM_CATALOG[key]
            cur = c.execute(
                "INSERT INTO items(name,layer,unit) VALUES (?,?,?)",
                (meta["name"], meta["layer"], meta["unit"]),
            )
            item_ids[key] = cur.lastrowid
        qty = spec["qty"]
        cur = c.execute(
            "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality)"
            " VALUES (?,?,?,?,?,?)",
            (
                item_ids[key],
                qty,
                spec.get("remain", qty),
                iso(spec["exp"]),
                spec.get("status", "on_shelf"),
                spec.get("quality", "clean"),
            ),
        )
        lot_ids.append(cur.lastrowid)
    c.commit()
    c.close()
    return item_ids, lot_ids


def load_consume_candidates(item_id: int) -> list[dict]:
    """与 /api/consume 完全相同的取数口径(app/main.py 里的查询)。"""
    c = connect()
    rows = [
        dict(r)
        for r in c.execute(
            "SELECT * FROM lots WHERE item_id=? AND status='on_shelf' AND qty_remain>0",
            (item_id,),
        )
    ]
    c.close()
    return rows


def load_on_shelf() -> list[dict]:
    """与 /api/expire-sweep 完全相同的取数口径。"""
    c = connect()
    rows = [dict(r) for r in c.execute("SELECT * FROM lots WHERE status='on_shelf'")]
    c.close()
    return rows


def snapshot_lots() -> dict[int, dict]:
    """lots 全表快照 {lot_id: row}, 用于零写回对账与最终状态断言。"""
    c = connect()
    rows = {r["id"]: dict(r) for r in c.execute("SELECT * FROM lots")}
    c.close()
    return rows
