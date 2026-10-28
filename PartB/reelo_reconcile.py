#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Reelo「AI 带来收入」报表对账脚本
==================================

用途
----
把 `ai_revenue_monthly`（报表落库结果 / 首页卡片数据源）与 `orders` + `attributions`
（事实数据）逐月对账，自动发现不一致，并**自动定位根因**（时报、币种、状态过滤、
测试单、归属重复）——可重复运行，适合放进 CI / 每日巡检。

指标口径（来自产品文档，脚本的判定基准）
----------------------------------------
AI 带来收入 = 本月（按商家所在时区）通过 Reelo 贴文带来的、已支付且未退款的
真实订单金额，统一以美元计（1 USD = 7.8 HKD）。

拆成 5 条硬规则：
  R1 月份归属：created_at 是 UTC，必须先转成商家所在时区的本地时间再取月份
  R2 支付状态：仅 status='paid'（退款/取消/待支付都不算）
  R3 真实订单：is_test=0（测试单必须剔除）
  R4 贴文归属：订单必须能关联到 Reelo 贴文（attributions）；同一订单被多个贴文
     归属时，金额只能算一次（订单维度去重，否则收入翻倍）
  R5 币种换算：非 USD 按汇率折算成 USD（HKD/7.8），再汇总

用法
----
    python reelo_reconcile.py                      # 默认连 127.0.0.1:3306/test
    python reelo_reconcile.py --from 2026-08 --to 2026-11
    python reelo_reconcile.py --merchant 101 --tz 101=America/Los_Angeles
    python reelo_reconcile.py --json-out recon.json --verbose
    python reelo_reconcile.py --no-fail             # 有差异也返回 0（仅告警）

环境变量（优先级低于命令行参数）：MYSQL_HOST/MYSQL_PORT/MYSQL_USER/
MYSQL_PASSWORD/MYSQL_DB

退出码：0 = 无差异；1 = 存在差异；2 = 运行错误（连库失败等）
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Optional, Tuple

try:
    import pymysql
except ImportError:  # pragma: no cover
    sys.stderr.write("缺少依赖：pip install pymysql\n")
    raise SystemExit(2)

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    sys.stderr.write("需要 Python 3.9+（zoneinfo）\n")
    raise SystemExit(2)


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

#: 商家时区注册表（生产环境应改为读商家表 / 配置中心）
MERCHANT_TZ: Dict[int, str] = {
    101: "America/Los_Angeles",
}

#: 币种 → 每 1 USD 对应的原币数量（1 USD = 7.8 HKD）
FX_PER_USD: Dict[str, Decimal] = {
    "USD": Decimal("1"),
    "HKD": Decimal("7.8"),
}

Q2 = Decimal("0.01")

# 严重级别
BLOCKER = "BLOCKER"   # 报表数字错，必须修
WARN = "WARN"         # 数据脏 / 潜在风险
INFO = "INFO"         # 提示


def money(x: Decimal) -> Decimal:
    return Decimal(x).quantize(Q2, rounding=ROUND_HALF_UP)


def fmt(x: Decimal) -> str:
    return f"{money(x):,.2f}"


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #

@dataclass
class Order:
    order_id: int
    merchant_id: Optional[int]
    amount: Optional[Decimal]
    currency: str
    status: str
    is_test: bool
    created_at_raw: str
    ts_utc: Optional[datetime]
    parse_error: Optional[str] = None

    def month_utc(self) -> Optional[str]:
        return self.ts_utc.strftime("%Y-%m") if self.ts_utc else None

    def month_local(self, tz: ZoneInfo) -> Optional[str]:
        if not self.ts_utc:
            return None
        return self.ts_utc.astimezone(tz).strftime("%Y-%m")

    def usd(self) -> Optional[Decimal]:
        """折算成美元；未知币种返回 None（由调用方记为问题，不做静默透传）。"""
        rate = FX_PER_USD.get(self.currency)
        if rate is None or self.amount is None:
            return None
        return self.amount / rate


@dataclass
class ReportRow:
    merchant_id: int
    month: str
    revenue: Decimal
    order_cnt: int


@dataclass
class Issue:
    code: str
    severity: str
    scope: str
    message: str
    detail: Dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 口径开关（用于枚举“错误口径”，反推报表到底用了哪种算法）
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Basis:
    """一种汇总口径。CORRECT 是全对的那个。"""
    month_basis: str        # 'local' | 'utc'
    convert_fx: bool        # 是否做币种换算
    require_paid: bool      # 是否仅统计 paid
    exclude_test: bool      # 是否剔除测试单
    join_mode: str          # 'dedupe'（订单去重）| 'raw'（按行 JOIN）| 'none'（不要求归属）
    count_mode: str = "distinct"   # 'distinct'（COUNT DISTINCT 订单）| 'rows'（COUNT(*)）
    label: str = ""

    def key(self) -> Tuple[Any, ...]:
        """身份（不含 label），用于比较两种口径是否等价。"""
        return (self.month_basis, self.convert_fx, self.require_paid,
                self.exclude_test, self.join_mode, self.count_mode)

    def deviations(self) -> List[str]:
        d = []
        c = CORRECT
        if self.month_basis != c.month_basis:
            d.append("月份按 UTC 划分，未按商家所在时区（R1）")
        if self.convert_fx != c.convert_fx:
            d.append("未做币种换算，原币金额直接当 USD（R5）")
        if self.require_paid != c.require_paid:
            d.append("未过滤支付状态，退款/非 paid 订单也计入（R2）")
        if self.exclude_test != c.exclude_test:
            d.append("未剔除 is_test 测试订单（R3）")
        if self.join_mode != c.join_mode:
            if self.join_mode == "raw":
                d.append("attributions 按行 JOIN 求和，同一订单被多贴文归属时金额重复累加（R4）")
            else:
                d.append("未要求订单必须归属 Reelo 贴文（R4）")
        if self.count_mode != c.count_mode:
            d.append("订单数用 COUNT(*) 而非 COUNT(DISTINCT order_id)（R4）")
        return d


CORRECT = Basis(month_basis="local", convert_fx=True, require_paid=True,
                exclude_test=True, join_mode="dedupe", count_mode="distinct",
                label="产品文档口径")


# --------------------------------------------------------------------------- #
# 取数
# --------------------------------------------------------------------------- #

def parse_utc(s: Optional[str]) -> Tuple[Optional[datetime], Optional[str]]:
    if s is None or str(s).strip() == "":
        return None, "created_at 为空"
    txt = str(s).strip()
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S",
              "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            return datetime.strptime(txt, f).replace(tzinfo=timezone.utc), None
        except ValueError:
            continue
    return None, f"created_at 无法解析: {txt!r}"


def load_data(conn, allow_missing_report: bool = False):
    cur = conn.cursor()

    cur.execute("""
        SELECT order_id, merchant_id, amount, currency, status, is_test, created_at
        FROM orders
    """)
    orders: List[Order] = []
    for oid, mid, amt, cur_, st, is_test, ca in cur.fetchall():
        ts, err = parse_utc(ca)
        orders.append(Order(
            order_id=int(oid),
            merchant_id=int(mid) if mid is not None else None,
            amount=Decimal(str(amt)) if amt is not None else None,
            currency=(cur_ or "").strip().upper(),
            status=(st or "").strip().lower(),
            is_test=bool(is_test),
            created_at_raw=str(ca) if ca is not None else "",
            ts_utc=ts,
            parse_error=err,
        ))

    cur.execute("SELECT order_id, post_id FROM attributions")
    attributions: List[Tuple[int, str]] = [
        (int(r[0]), r[1]) for r in cur.fetchall() if r[0] is not None
    ]

    report: Dict[Tuple[int, str], ReportRow] = {}
    try:
        cur.execute("""
            SELECT merchant_id, report_month, revenue_usd, order_cnt
            FROM ai_revenue_monthly
        """)
        for mid, m, rev, cnt in cur.fetchall():
            report[(int(mid), str(m))] = ReportRow(
                merchant_id=int(mid), month=str(m),
                revenue=Decimal(str(rev)), order_cnt=int(cnt),
            )
    except Exception as e:
        if not allow_missing_report:
            raise
        sys.stderr.write(f"[warn] 读取 ai_revenue_monthly 失败，仅做数据体检: {e}\n")

    cur.close()
    return orders, attributions, report


# --------------------------------------------------------------------------- #
# 汇总引擎
# --------------------------------------------------------------------------- #

class Engine:
    def __init__(self, orders: List[Order], attributions: List[Tuple[int, str]],
                 tz_of: Dict[int, Optional[ZoneInfo]]):
        self.orders = orders
        self.tz_of = tz_of
        # order_id -> 归属行数
        self.attr_rows: Dict[int, int] = defaultdict(int)
        for oid, _ in attributions:
            self.attr_rows[oid] += 1
        self.attr_order_ids = set(self.attr_rows)
        self.by_id = {o.order_id: o for o in orders}

    # -- 单条订单在某口径下是否计入 -------------------------------------- #
    def classify(self, o: Order, b: Basis) -> Tuple[bool, str]:
        if o.ts_utc is None:
            return False, "时间无效"
        if o.merchant_id is None:
            return False, "merchant_id 为空"
        if b.exclude_test and o.is_test:
            return False, "测试单"
        if b.require_paid and o.status != "paid":
            return False, f"非 paid（{o.status}）"
        if b.join_mode != "none" and o.order_id not in self.attr_order_ids:
            return False, "无贴文归属"
        if b.convert_fx and o.usd() is None:
            return False, "币种未知/金额缺失"
        if o.amount is None:
            return False, "金额为空"
        return True, "计入"

    # -- 汇总 (merchant, month) -> (revenue, order_cnt) ------------------ #
    def aggregate(self, b: Basis) -> Dict[Tuple[int, str], Tuple[Decimal, int]]:
        out: Dict[Tuple[int, str], List[Any]] = defaultdict(lambda: [Decimal("0"), 0])
        for o in self.orders:
            ok, _ = self.classify(o, b)
            if not ok:
                continue
            tz = self.tz_of.get(o.merchant_id)
            if b.month_basis == "local" and tz is not None:
                month = o.month_local(tz)
            else:
                month = o.month_utc()
            if month is None:
                continue
            amount = o.usd() if b.convert_fx else o.amount
            if amount is None:
                continue
            mult = self.attr_rows.get(o.order_id, 1) if b.join_mode == "raw" else 1
            bucket = out[(o.merchant_id, month)]
            bucket[0] += amount * mult
            bucket[1] += 1 if b.count_mode == "distinct" else mult
        return {k: (money(v[0]), v[1]) for k, v in out.items()}

    # -- 明细（用于逐单解释） -------------------------------------------- #
    def line_detail(self, merchant_id: int, month: str, b: Basis = CORRECT) -> List[Dict[str, Any]]:
        rows = []
        for o in sorted(self.orders, key=lambda x: x.order_id):
            if o.merchant_id != merchant_id:
                continue
            tz = self.tz_of.get(merchant_id)
            lm = o.month_local(tz) if tz else o.month_utc()
            if lm != month:
                continue
            ok, why = self.classify(o, b)
            rows.append({
                "order_id": o.order_id,
                "created_at_utc": o.created_at_raw,
                "local_time": o.ts_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M %z") if (o.ts_utc and tz) else None,
                "amount": str(o.amount),
                "currency": o.currency,
                "amount_usd": str(money(o.usd())) if o.usd() is not None else None,
                "status": o.status,
                "is_test": o.is_test,
                "attribution_rows": self.attr_rows.get(o.order_id, 0),
                "counted": ok,
                "reason": why,
            })
        return rows


# --------------------------------------------------------------------------- #
# 根因指纹：枚举所有口径组合，看哪种能精确复现报表数字
# --------------------------------------------------------------------------- #

def fingerprint(engine: Engine, report: Dict[Tuple[int, str], ReportRow],
                scope_months: List[Tuple[int, str]]) -> List[Dict[str, Any]]:
    combos = []
    for mb, fx, paid, test, jm, cm in itertools.product(
        ("local", "utc"), (True, False), (True, False), (True, False),
        ("dedupe", "raw", "none"), ("distinct", "rows")
    ):
        combos.append(Basis(mb, fx, paid, test, jm, cm))

    scored = []
    for b in combos:
        agg = engine.aggregate(b)
        hit = total = 0
        for key in scope_months:
            exp = agg.get(key, (Decimal("0"), 0))
            rep = report.get(key)
            rep_rev = rep.revenue if rep else Decimal("0")
            rep_cnt = rep.order_cnt if rep else 0
            if rep_rev == 0 and rep_cnt == 0 and exp[0] == 0 and exp[1] == 0:
                continue
            total += 1
            if exp[0] == rep_rev and exp[1] == rep_cnt:
                hit += 1
        if total and hit == total and b.key() != CORRECT.key():
            scored.append({
                "basis": {
                    "month_basis": b.month_basis, "convert_fx": b.convert_fx,
                    "require_paid": b.require_paid, "exclude_test": b.exclude_test,
                    "join_mode": b.join_mode, "count_mode": b.count_mode,
                },
                "deviations": b.deviations(),
                "matched_months": total,
            })
    # 偏离项越少越可能是真根因
    scored.sort(key=lambda x: (len(x["deviations"]), x["matched_months"] * -1))
    return scored


# --------------------------------------------------------------------------- #
# 对账主流程
# --------------------------------------------------------------------------- #

def reconcile(orders, attributions, report, tz_map: Dict[int, str],
              month_from: Optional[str], month_to: Optional[str],
              verbose: bool) -> Dict[str, Any]:
    issues: List[Issue] = []

    # ---- 时区解析 ------------------------------------------------------- #
    tz_of: Dict[int, Optional[ZoneInfo]] = {}
    merchants = sorted({o.merchant_id for o in orders if o.merchant_id is not None}
                       | {m for (m, _) in report})
    for m in merchants:
        name = tz_map.get(m)
        if not name:
            tz_of[m] = None
            issues.append(Issue("UNKNOWN_MERCHANT_TZ", WARN, f"merchant={m}",
                                f"商家 {m} 未配置时区，本月按 UTC 兜底，报表口径无法验证"))
            continue
        try:
            tz_of[m] = ZoneInfo(name)
        except ZoneInfoNotFoundError:
            tz_of[m] = None
            issues.append(Issue("INVALID_TZ", BLOCKER, f"merchant={m}",
                                f"商家 {m} 时区 {name!r} 不存在，系统时区数据缺失"))

    engine = Engine(orders, attributions, tz_of)
    expected = engine.aggregate(CORRECT)

    # ---- 数据体检（与报表无关，但会污染报表） ---------------------------- #
    for oid, n in sorted(engine.attr_rows.items()):
        if n > 1:
            o = engine.by_id.get(oid)
            amt = money(o.usd()) if (o and o.usd() is not None) else None
            issues.append(Issue(
                "DUPLICATE_ATTRIBUTION", BLOCKER, f"order={oid}",
                f"订单 {oid} 在 attributions 中有 {n} 条归属记录，按行 JOIN 会重复计收入"
                + (f"（该单 {fmt(amt)} USD，重复后多算 {fmt(amt * (n - 1))} USD）" if amt else ""),
                {"order_id": oid, "rows": n, "amount_usd": str(amt) if amt else None},
            ))
    for oid in sorted(engine.attr_order_ids - set(engine.by_id)):
        issues.append(Issue("ORPHAN_ATTRIBUTION", WARN, f"order={oid}",
                            f"attributions 中的订单 {oid} 在 orders 表中不存在（脏数据/已删单）",
                            {"order_id": oid}))

    for o in orders:
        if o.parse_error:
            issues.append(Issue("INVALID_CREATED_AT", BLOCKER, f"order={o.order_id}",
                                o.parse_error, {"order_id": o.order_id}))
        if o.amount is not None and o.amount < 0:
            issues.append(Issue("NEGATIVE_AMOUNT", BLOCKER, f"order={o.order_id}",
                                f"订单金额为负（{o.amount}），是否为部分退款/冲正？",
                                {"order_id": o.order_id}))
        if o.currency and o.currency not in FX_PER_USD:
            issues.append(Issue("UNKNOWN_CURRENCY", BLOCKER, f"order={o.order_id}",
                                f"币种 {o.currency!r} 无汇率配置，无法折算 USD",
                                {"order_id": o.order_id, "currency": o.currency}))
        if o.status and o.status not in ("paid", "refunded", "cancelled", "pending", "failed"):
            issues.append(Issue("UNKNOWN_STATUS", WARN, f"order={o.order_id}",
                                f"未知的订单状态 {o.status!r}，需确认是否应计入",
                                {"order_id": o.order_id, "status": o.status}))
        # 时区跨月风险提示
        if o.ts_utc and o.merchant_id in tz_of and tz_of[o.merchant_id]:
            mu, ml = o.month_utc(), o.month_local(tz_of[o.merchant_id])
            if mu != ml:
                issues.append(Issue(
                    "TZ_MONTH_BOUNDARY", INFO, f"order={o.order_id}",
                    f"订单 {o.order_id} UTC {o.created_at_raw} → 本地 "
                    f"{o.ts_utc.astimezone(tz_of[o.merchant_id]).strftime('%Y-%m-%d %H:%M')}，"
                    f"UTC 归 {mu}、本地归 {ml}，按 UTC 分组会串月",
                    {"order_id": o.order_id, "utc_month": mu, "local_month": ml},
                ))
        if (not o.is_test and o.status == "paid" and o.order_id not in engine.attr_order_ids
                and o.ts_utc is not None):
            issues.append(Issue(
                "PAID_BUT_UNATTRIBUTED", INFO, f"order={o.order_id}",
                f"订单 {o.order_id} 已支付但无贴文归属，正确不计入 AI 收入"
                f"（若报表含它，说明口径串了）",
                {"order_id": o.order_id},
            ))

    # ---- 逐月对账 ------------------------------------------------------- #
    months = sorted({m for (_, m) in report} | {m for (_, m) in expected})
    if month_from:
        months = [m for m in months if m >= month_from]
    if month_to:
        months = [m for m in months if m <= month_to]

    diffs = []
    for m in merchants:
        for mon in months:
            rep = report.get((m, mon))
            exp_rev, exp_cnt = expected.get((m, mon), (Decimal("0"), 0))
            if rep is None and exp_rev == 0 and exp_cnt == 0:
                continue
            if rep is None:
                diffs.append({
                    "merchant_id": m, "month": mon, "type": "MISSING_REPORT_ROW",
                    "expected_revenue": str(exp_rev), "expected_orders": exp_cnt,
                    "reported_revenue": None, "reported_orders": None,
                    "revenue_diff": str(exp_rev), "orders_diff": exp_cnt,
                    "lines": engine.line_detail(m, mon),
                })
                issues.append(Issue(
                    "MISSING_REPORT_ROW", BLOCKER, f"merchant={m} month={mon}",
                    f"{mon} 报表无此行，但按口径应有 {fmt(exp_rev)} USD / {exp_cnt} 单，"
                    f"首页与报表页会漏掉该月收入",
                    {"merchant_id": m, "month": mon},
                ))
                continue
            rev_diff = rep.revenue - exp_rev
            cnt_diff = rep.order_cnt - exp_cnt
            if rev_diff == 0 and cnt_diff == 0:
                continue
            kind = ("EXTRA_REPORT_ROW" if (exp_rev == 0 and exp_cnt == 0)
                    else "MISMATCH")
            diffs.append({
                "merchant_id": m, "month": mon, "type": kind,
                "expected_revenue": str(exp_rev), "expected_orders": exp_cnt,
                "reported_revenue": str(rep.revenue), "reported_orders": rep.order_cnt,
                "revenue_diff": str(rev_diff), "orders_diff": cnt_diff,
                "lines": engine.line_detail(m, mon),
            })
            delta_pct = (rev_diff / exp_rev * 100) if exp_rev else None
            issues.append(Issue(
                "REPORT_MISMATCH", BLOCKER, f"merchant={m} month={mon}",
                f"{mon} 报表 {fmt(rep.revenue)} USD / {rep.order_cnt} 单，"
                f"实际应为 {fmt(exp_rev)} USD / {exp_cnt} 单；"
                f"差额 {fmt(rev_diff)} USD（{cnt_diff:+d} 单）"
                + (f"，虚高 {delta_pct:.1f}%" if delta_pct is not None and delta_pct > 0
                   else (f"，偏低 {abs(delta_pct):.1f}%" if delta_pct is not None else "")),
                {"merchant_id": m, "month": mon},
            ))

    # ---- 根因指纹 ------------------------------------------------------- #
    scope = [(m, mon) for m in merchants for mon in months
             if ((m, mon) in report or expected.get((m, mon), (Decimal("0"), 0)) != (Decimal("0"), 0))]
    hypotheses = fingerprint(engine, report, scope) if diffs else []

    # ---- 跨月边界单独提示（最常见的口径错） ----------------------------- #
    boundary = [i for i in issues if i.code == "TZ_MONTH_BOUNDARY"]

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "params": {"months": months, "merchants": merchants,
                   "tz_map": {str(k): str(v) for k, v in tz_of.items() if v}},
        "summary": {
            "orders": len(orders),
            "attributions": len(attributions),
            "report_rows": len(report),
            "months_with_diff": len(diffs),
            "issues": len(issues),
            "blockers": len([i for i in issues if i.severity == BLOCKER]),
        },
        "diffs": diffs,
        "issues": [i.__dict__ for i in issues],
        "root_cause_hypotheses": hypotheses,
        "tz_boundary_orders": [i.detail for i in boundary],
        "expected": [
            {"merchant_id": m, "month": mon, "revenue_usd": str(v[0]), "order_cnt": v[1]}
            for (m, mon), v in sorted(expected.items())
        ],
        "reported": [
            {"merchant_id": r.merchant_id, "month": r.month,
             "revenue_usd": str(r.revenue), "order_cnt": r.order_cnt}
            for r in sorted(report.values(), key=lambda x: (x.merchant_id, x.month))
        ],
    }


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #

C = {"r": "\033[31m", "g": "\033[32m", "y": "\033[33m", "b": "\033[36m",
     "d": "\033[90m", "B": "\033[1m", "0": "\033[0m"}


def use_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(txt: str, k: str) -> str:
    return f"{C[k]}{txt}{C['0']}" if use_color() else txt


def print_report(res: Dict[str, Any], verbose: bool) -> None:
    s = res["summary"]
    print()
    print(c("=" * 78, "d"))
    print(c(" Reelo「AI 带来收入」报表对账", "B"))
    print(c("=" * 78, "d"))
    print(f" 生成时间   : {res['generated_at']}")
    print(f" 对账范围   : 商家 {res['params']['merchants']}  月份 {res['params']['months']}")
    print(f" 数据规模   : {s['orders']} 单 / {s['attributions']} 条归属 / {s['report_rows']} 行报表")
    verdict = (c("✓ 报表数字一致", "g") if s["months_with_diff"] == 0
               else c(f"✗ 发现 {s['months_with_diff']} 处报表不一致", "r"))
    print(f" 结论       : {verdict}")
    if s["blockers"]:
        print(f" 数据侧     : "
              + c(f"另有 {s['blockers']} 个阻断级数据缺陷（见问题清单，尚未反映到报表）", "y"))
    print()

    # 逐月对照
    print(c("── 逐月对账 ─────────────────────────────────────────────", "d"))
    print(f" {'商家':>5} {'月份':<8} {'报表收入':>12} {'应有收入':>12} {'差额':>12} "
          f"{'报表单数':>8} {'应有单数':>8}")
    for d in res["diffs"]:
        rep = d["reported_revenue"]
        print(f" {d['merchant_id']:>5} {d['month']:<8} "
              f"{(fmt(Decimal(rep)) if rep else '—'):>12} "
              f"{fmt(Decimal(d['expected_revenue'])):>12} "
              f"{c(fmt(Decimal(d['revenue_diff'])), 'r'):>12} "
              f"{(d['reported_orders'] if d['reported_orders'] is not None else '—'):>8} "
              f"{d['expected_orders']:>8}")
    if not res["diffs"]:
        print(c("  （无差异）", "g"))
    print()

    # 明细：为什么差
    for d in res["diffs"]:
        print(c(f"── 明细 · 商家 {d['merchant_id']} / {d['month']} · {d['type']} "
                f"────────────────────", "d"))
        lines = d["lines"]
        if lines:
            print(f" {'订单':>6} {'本地时间':<20} {'原币':>10} {'USD':>10} "
                  f"{'归属行':>6} {'计入':>4}  说明")
            for ln in lines:
                mark = c("✓", "g") if ln["counted"] else c("✗", "y")
                print(f" {ln['order_id']:>6} {(ln['local_time'] or '—'):<20} "
                      f"{ln['amount']+' '+ln['currency']:>10} "
                      f"{(ln['amount_usd'] or '—'):>10} {ln['attribution_rows']:>6} "
                      f"{mark:>4}  {ln['reason']}")
        else:
            print(c("  （该月无归属订单明细）", "d"))
        print()

    # 根因
    if res["root_cause_hypotheses"]:
        print(c("── 根因指纹（自动枚举口径组合，精确复现报表数字者）────────", "d"))
        for i, h in enumerate(res["root_cause_hypotheses"][:3], 1):
            tag = c("最可能", "r") if i == 1 else f"备选{i}"
            print(f" [{tag}] 命中 {h['matched_months']} 个月，偏离项 {len(h['deviations'])} 条：")
            for dv in h["deviations"]:
                print(f"        · {dv}")
        print()

    # 问题清单
    order = {BLOCKER: 0, WARN: 1, INFO: 2}
    grouped = defaultdict(list)
    for it in res["issues"]:
        grouped[it["severity"]].append(it)
    print(c("── 问题清单 ─────────────────────────────────────────────", "d"))
    for sev in (BLOCKER, WARN, INFO):
        if not grouped[sev]:
            continue
        color = {"BLOCKER": "r", "WARN": "y", "INFO": "d"}[sev]
        print(c(f" [{sev}] {len(grouped[sev])} 项", color))
        for it in grouped[sev]:
            if sev == INFO and not verbose and it["code"] in ("PAID_BUT_UNATTRIBUTED",):
                continue
            print(f"   - [{it['code']}] {it['scope']}: {it['message']}")
        print()

    if res["tz_boundary_orders"]:
        print(c("── 时区跨月订单（必须验证的关键用例）────────────────────", "d"))
        for b in res["tz_boundary_orders"]:
            print(f"   订单 {b['order_id']}: UTC 月 {b['utc_month']} ≠ 本地月 {b['local_month']}")
        print()


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Reelo AI 收入报表对账")
    ap.add_argument("--host", default=os.environ.get("MYSQL_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MYSQL_PORT", "3306")))
    ap.add_argument("--user", default=os.environ.get("MYSQL_USER", "root"))
    ap.add_argument("--password", default=os.environ.get("MYSQL_PASSWORD", ""))
    ap.add_argument("--database", default=os.environ.get("MYSQL_DB", "test"))
    ap.add_argument("--merchant", type=int, action="append",
                    help="只对账指定商家（可重复）；默认全部")
    ap.add_argument("--tz", action="append", default=[],
                    help="补充商家时区，格式 101=America/Los_Angeles（可重复）")
    ap.add_argument("--from", dest="month_from", help="起始月 YYYY-MM")
    ap.add_argument("--to", dest="month_to", help="结束月 YYYY-MM")
    ap.add_argument("--json-out", help="把完整结果写入 JSON 文件")
    ap.add_argument("--no-fail", action="store_true", help="有差异也返回 0")
    ap.add_argument("--allow-missing-report", action="store_true",
                    help="报表表不存在时仅做数据体检")
    ap.add_argument("--verbose", "-v", action="store_true", help="打印全部 INFO 级明细")
    args = ap.parse_args(argv)

    tz_map = dict(MERCHANT_TZ)
    for item in args.tz:
        if "=" not in item:
            ap.error(f"--tz 需要 k=v 格式，收到 {item!r}")
        k, v = item.split("=", 1)
        tz_map[int(k)] = v

    try:
        conn = pymysql.connect(host=args.host, port=args.port, user=args.user,
                               password=args.password, database=args.database,
                               charset="utf8mb4", connect_timeout=5)
    except Exception as e:
        sys.stderr.write(f"[FATAL] 连接 MySQL 失败: {e}\n")
        return 2

    try:
        orders, attributions, report = load_data(conn, args.allow_missing_report)
    finally:
        conn.close()

    if args.merchant:
        keep = set(args.merchant)
        orders = [o for o in orders if o.merchant_id in keep]
        report = {k: v for k, v in report.items() if k[0] in keep}
        attributions = [a for a in attributions if a[0] in {o.order_id for o in orders}]

    res = reconcile(orders, attributions, report, tz_map,
                    args.month_from, args.month_to, args.verbose)
    print_report(res, args.verbose)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=2)
        print(c(f"完整结果已写入 {args.json_out}", "d"))
        print()

    bad = res["summary"]["months_with_diff"] > 0
    if bad and not args.no_fail:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
