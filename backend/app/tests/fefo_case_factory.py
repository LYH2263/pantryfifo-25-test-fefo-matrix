"""FEFO 不变量测例的造数助手（独立文件）。

职责：
- 为每个参数行准备一个全新的 SQLite 库文件（DATA_DIR 指向临时目录），
  使种子数据不干扰参数行自定义批次；
- 直接 SQL 插入各种"非标准"批次：dirty、负余量、已 expired 的在架批等
  （HTTP /api/lots 只能造 clean 正数批，无法直接造这些数据）；
- 提供读取全层（DB 全量 lots）与对应层页（GET /api/fridge?layer=）余量的方法，
  以及 expire-sweep / consume 的 HTTP 封装。

注意：seed.init_db() 会在 FastAPI startup 时以当前 DATA_DIR 建库；
因此必须先 set DATA_DIR，再 import app.main。
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient


def _iso(d: date) -> str:
    return d.isoformat()


@dataclass
class LotSpec:
    """单个批次的造数规格。

    alias   本地批次别名，方便参数行用别名定位 DB id；
    expiry  相对今天的天数偏移（负=已过期），或具体 ISO 字符串；
    status  初始状态 on_shelf / expired / consumed；
    dq      data_quality：clean / dirty。
    """

    alias: str
    qty_remain: float
    expiry_offset: int | None = None
    expiry: str | None = None
    qty_in: float | None = None
    status: str = "on_shelf"
    dq: str = "clean"

    def expiry_iso(self, today: date) -> str | None:
        if self.expiry is not None:
            return self.expiry
        if self.expiry_offset is None:
            return None
        return _iso(today + timedelta(days=self.expiry_offset))


@dataclass
class CaseData:
    """一行参数对应的可执行环境。"""

    key: str
    client: TestClient
    dbfile: Path
    item_id: int
    alias2id: dict[str, int] = field(default_factory=dict)
    today: date = field(default_factory=date.today)

    # ---- 写操作 ----
    def engine_consume(self, qty: float) -> dict:
        """先调引擎：用与 HTTP 完全相同的选批口径取候选，再跑 consume_fefo。"""
        from app.engines.fefo import consume_fefo
        return consume_fefo(self.candidates_for_engine(), qty)

    def http_consume(self, qty: float, note: str = ""):
        """打 HTTP 消费，返回 (status_code, payload)。"""
        r = self.client.post(
            "/api/consume", json={"item_id": self.item_id, "qty": qty, "note": note}
        )
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"_raw": r.text}

    @staticmethod
    def engine_deductions(engine_result: dict) -> list[dict]:
        """引擎 deductions 归一化为 (alias, take) 列表。"""
        return [(d["lot_id"], float(d["take"]))
                for d in engine_result.get("deductions", [])]

    def http_deductions(self, http_status: int, payload: dict) -> list[dict]:
        """HTTP deductions 归一化为 (alias, take)。

        200 时 body 即引擎 result；409 short 时 body={"detail": <result>}；
        400 qty_non_positive 时无 deductions。
        """
        result = payload if http_status == 200 else payload.get("detail", {})
        if not isinstance(result, dict):
            return []
        return [(self._alias(d["lot_id"]), float(d["take"]))
                for d in result.get("deductions", [])]

    def sweep(self):
        r = self.client.post("/api/expire-sweep")
        return r.status_code, r.json()

    # ---- 读全层：DB 全量批次（含 off-shelf、负余量） ----
    def all_lots(self) -> dict[str, dict]:
        c = sqlite3.connect(self.dbfile)
        c.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in c.execute("SELECT * FROM lots ORDER BY id")]
        finally:
            c.close()
        out: dict[str, dict] = {}
        for r in rows:
            # 只把属于本行目标 item 的批记下来（每个 case 只造一个 item）
            if r["item_id"] == self.item_id:
                out[self._alias(r["id"])] = r
        return out

    # ---- 读对应层页：前端 Layer.vue 实际调用的接口 ----
    def layer_page(self, layer: str) -> list[dict]:
        """对应层页：GET /api/fridge?layer=（前端 Layer.vue 的实际取数口径）。

        返回该层页上属于本行 item 的原始行（含 qty_remain/status 等）。
        """
        r = self.client.get(f"/api/fridge?layer={layer}")
        assert r.status_code == 200, r.text
        return [row for row in r.json() if row["item_id"] == self.item_id]

    def layer_page_remaining(self, layer: str) -> dict[str, float]:
        """对应层页上，本行目标 item 各批余量（alias -> qty_remain），走 HTTP。"""
        return {self._alias(row["id"]): row["qty_remain"]
                for row in self.layer_page(layer)}

    def _alias(self, lot_id: int) -> str:
        for a, i in self.alias2id.items():
            if i == lot_id:
                return a
        return f"id:{lot_id}"

    # ---- 供"先调引擎"使用：按与 /api/consume 完全相同的选批口径取候选 ----
    def candidates_for_engine(self) -> list[dict]:
        """返回引擎输入：id 字段填 alias，便于与 HTTP  deductions 对照。

        口径必须与 main.py 的 /api/consume 选批 SQL 保持一致：
        item 相同、status='on_shelf'、qty_remain>0、data_quality<>'dirty'。
        """
        c = sqlite3.connect(self.dbfile)
        c.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in c.execute(
                "SELECT id,qty_remain,expiry FROM lots"
                " WHERE item_id=? AND status='on_shelf' AND qty_remain>0"
                " AND COALESCE(data_quality,'clean') <> 'dirty'",
                (self.item_id,))]
        finally:
            c.close()
        return [{"id": self._alias(r["id"]),
                 "qty_remain": r["qty_remain"],
                 "expiry": r["expiry"]} for r in rows]

    def snapshot(self) -> dict[str, dict]:
        """全层快照：本行 item 的每个批 alias -> {qty_remain,status,data_quality}。"""
        c = sqlite3.connect(self.dbfile)
        c.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in c.execute(
                "SELECT id,qty_remain,status,data_quality FROM lots"
                " WHERE item_id=? ORDER BY id", (self.item_id,))]
        finally:
            c.close()
        return {self._alias(r["id"]):
                {"qty_remain": r["qty_remain"],
                 "status": r["status"],
                 "data_quality": r["data_quality"]} for r in rows}


def build_case(key: str, lots: list[LotSpec], *,
               layer: str = "upper", unit: str = "盒",
               extra_items: list[tuple[str, str, str]] | None = None) -> CaseData:
    """造一行参数所需的世界：临时 DATA_DIR、items、lots，并返回已启动的 client。

    extra_items: 额外 (name, layer, unit)，用于验证层页"只显示该层"等。
    """
    tmpdir = tempfile.mkdtemp(prefix=f"fefoinv_{key}_")
    os.environ["DATA_DIR"] = tmpdir

    # 延迟 import，确保读到本行的 DATA_DIR；移除已配置的 app 以触发 startup 重新建库
    import importlib

    import app.db as db_mod
    importlib.reload(db_mod)
    import app.seed as seed_mod
    importlib.reload(seed_mod)
    import app.main as main_mod
    importlib.reload(main_mod)

    client = TestClient(main_mod.app)
    # TestClient 作为上下文管理器才会触发 startup 事件
    client.__enter__()

    # startup 已建库并塞入种子数据；为得到干净可控的世界，清空目标 item/lots，
    # 但保留 settings 表。
    c = db_mod.connect()
    c.execute("DELETE FROM lots")
    c.execute("DELETE FROM items")
    c.commit()
    cur = c.execute(
        "INSERT INTO items(name,layer,unit) VALUES (?,?,?)",
        (f"item_{key}", layer, unit),
    )
    item_id = cur.lastrowid
    if extra_items:
        c.executemany("INSERT INTO items(name,layer,unit) VALUES (?,?,?)", extra_items)
    today = date.today()
    alias2id: dict[str, int] = {}
    for sp in lots:
        exp = sp.expiry_iso(today)
        qin = sp.qty_in if sp.qty_in is not None else sp.qty_remain
        cur = c.execute(
            "INSERT INTO lots(item_id,qty_in,qty_remain,expiry,status,data_quality)"
            " VALUES (?,?,?,?,?,?)",
            (item_id, qin, sp.qty_remain, exp, sp.status, sp.dq),
        )
        alias2id[sp.alias] = cur.lastrowid
    c.commit()
    c.close()

    return CaseData(
        key=key,
        client=client,
        dbfile=db_mod.db_path(),
        item_id=item_id,
        alias2id=alias2id,
        today=today,
    )


def teardown_case(case: CaseData) -> None:
    try:
        case.client.__exit__(None, None, None)
    finally:
        # 清理临时目录（db 文件与临时目录）
        try:
            os.unlink(case.dbfile)
        except OSError:
            pass
        d = case.dbfile.parent
        try:
            os.rmdir(d)
        except OSError:
            pass
        os.environ.pop("DATA_DIR", None)


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
