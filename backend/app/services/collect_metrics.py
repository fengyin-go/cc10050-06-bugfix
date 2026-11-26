"""采集量指标领域服务：统计各站点采集-解析-产出的量级健康度，负责波动检测与断采告警。

数据链路位置：观测层，汇聚爬取任务、调度计划、样本库与隔离区上报的量级数据，
产出的健康度结论供采集治理决策与告警联动。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class CollectMetricsService(BaseModuleService):
    MODULE = "collect_metrics"
    ENTITY = "指标记录"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["统计时间", "站点名称", "抓取量", "解析量", "产出量"]
    REQUIRED_FIELDS = ["统计时间", "站点名称", "抓取量"]
    STATUS_ORDER = ["正常", "量级波动", "断采"]
    ACTION_RULES = {"查看趋势": "量级波动", "忽略波动": "断采"}
    NEGATIVE_ACTIONS = ["忽略波动"]
    COMMANDS = {"恢复产出明细": "recovery_output_detail"}

    # ---- 业务扩展：量级统计与健康度判定 ----

    def record_stats(self, site: str, crawled: int, parsed: int, output: int) -> dict[str, Any]:
        """记录一次站点量级统计。"""
        if crawled < 0 or parsed < 0 or output < 0:
            return {"ok": False, "message": "量级不能为负数"}
        rows = store.rows(self.MODULE)
        row = next((r for r in rows if r.get("站点名称") == site), None)
        now = datetime.now(timezone.utc).isoformat()
        if row is None:
            mid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            row = {
                "id": mid,
                "统计时间": now,
                "站点名称": site,
                "抓取量": str(crawled),
                "解析量": str(parsed),
                "产出量": str(output),
                "status": "正常",
                "pending": True,
                "abnormal": False,
            }
            rows.append(row)
        else:
            row["抓取量"] = str(int(row.get("抓取量", "0") or 0) + crawled)
            row["解析量"] = str(int(row.get("解析量", "0") or 0) + parsed)
            row["产出量"] = str(int(row.get("产出量", "0") or 0) + output)
            row["统计时间"] = now
        return {"ok": True, "message": "量级已记录", "site": site}

    def detect_anomaly(self, entry_id: int) -> dict[str, Any]:
        """检测站点量级异常：断采与波动判定。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        crawled = int(str(entry.get("抓取量", "0") or 0))
        parsed = int(str(entry.get("解析量", "0") or 0))
        output = int(str(entry.get("产出量", "0") or 0))
        if crawled == 0:
            entry["status"] = "断采"
            entry["abnormal"] = True
            return {"ok": True, "message": "检测到断采", "anomaly": "断采"}
        if parsed < crawled * 0.5:
            entry["status"] = "量级波动"
            entry["pending"] = True
            return {"ok": True, "message": "解析量占比过低，量级波动", "anomaly": "量级波动"}
        entry["status"] = "正常"
        entry["abnormal"] = False
        entry["pending"] = False
        return {"ok": True, "message": "量级健康", "anomaly": ""}

    def cross_check_site(self, entry_id: int) -> dict[str, Any]:
        """与站点目录交叉核对指标站点是否仍有效。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "指标记录不存在"}
        site = entry.get("站点名称", "")
        site_row = next(
            (s for s in store.rows("shop_site") if s.get("站点域名") == site), None
        )
        if site_row is None:
            return {"ok": False, "message": f"站点 {site} 不在站点目录"}
        if site_row.get("status") == "已封禁":
            entry["status"] = "断采"
            entry["abnormal"] = True
            return {"ok": True, "message": "站点已封禁，指标标记断采"}
        return {"ok": True, "message": "站点有效", "site_status": site_row.get("status")}

    def aggregate_health(self) -> dict[str, Any]:
        """汇总全部站点的量级健康度。"""
        rows = store.rows(self.MODULE)
        normal = sum(1 for r in rows if r.get("status") == "正常")
        broken = sum(1 for r in rows if r.get("status") == "断采")
        total_crawled = sum(int(str(r.get("抓取量", "0") or 0)) for r in rows)
        total_output = sum(int(str(r.get("产出量", "0") or 0)) for r in rows)
        return {
            "ok": True,
            "message": "健康度汇总完成",
            "sites": len(rows),
            "normal": normal,
            "broken": broken,
            "total_crawled": total_crawled,
            "total_output": total_output,
            "yield_rate": f"{(total_output / total_crawled):.0%}" if total_crawled else "0%",
        }

    def notify_quarantine_pressure(self, entry_id: int) -> dict[str, Any]:
        """隔离区压力大时把压力传导到指标异常标记。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "指标记录不存在"}
        pending_quarantine = sum(
            1 for q in store.rows("failure_quarantine")
            if q.get("status") in ("待处理", "重试中")
        )
        output = int(str(entry.get("产出量", "0") or 0))
        if pending_quarantine > max(output * 0.2, 5):
            entry["status"] = "量级波动"
            entry["abnormal"] = True
            return {
                "ok": True,
                "message": "隔离区压力已传导",
                "pending_quarantine": pending_quarantine,
            }
        return {"ok": True, "message": "隔离区压力正常", "pending_quarantine": pending_quarantine}

    def trend_report(self, site: str) -> dict[str, Any]:
        """输出站点量级趋势报告供告警规则参考。"""
        rows = [r for r in store.rows(self.MODULE) if r.get("站点名称") == site]
        if not rows:
            return {"ok": False, "message": f"站点 {site} 没有指标记录"}
        latest = rows[-1]
        return {
            "ok": True,
            "message": "趋势报告已生成",
            "site": site,
            "crawled": latest.get("抓取量", ""),
            "parsed": latest.get("解析量", ""),
            "output": latest.get("产出量", ""),
            "status": latest.get("status", ""),
        }

    # ---- 业务扩展：按恢复凭据核对恢复产出的归属 ----

    def recovery_output_detail(self, entry_id: int | None = None, **_: Any) -> dict[str, Any]:
        """读取每笔恢复的归属和恢复凭据，核对产出是否按来源归属计入。

        只统计 counted=True 的凭据；归属未确定的恢复保留在隔离区，不会出现在这里，
        因此不会被下游累计成已归属产出。
        """
        from app.services import service_for
        quarantine = service_for("failure_quarantine")
        if quarantine is None:
            return {"ok": False, "message": "失败隔离区服务不可用"}
        receipts = [
            r for r in quarantine.all_receipts()
            if r.get("counted") and (entry_id is None or r.get("entry_id") == entry_id)
        ]
        per_site: dict[str, int] = {}
        for receipt in receipts:
            account_site = str(receipt.get("account_site", ""))
            per_site[account_site] = per_site.get(account_site, 0) + 1
        mismatches: list[dict[str, Any]] = []
        for receipt in receipts:
            row = next(
                (m for m in store.rows(self.MODULE)
                 if m.get("站点名称") == receipt.get("account_site")),
                None,
            )
            if row is None:
                mismatches.append({
                    "receipt_no": receipt.get("receipt_no"),
                    "reason": f"记账站点 {receipt.get('account_site')} 缺少指标账目",
                })
        return {
            "ok": True,
            "message": "恢复产出已按归属凭据核对",
            "receipts": receipts,
            "recovery_output_by_site": per_site,
            "mismatches": mismatches,
        }
