"""失败隔离区领域服务：隔离解析与匹配失败的样本供延迟重试，负责重试次数与丢弃治理。

数据链路位置：失败治理层，承接解析模板、比价计算、调度计划与代理线路
写入的失败上下文，恢复结果回写采集量指标与相关模块。

归属治理要点：
- 恢复必须以「可回查的样本采集归属」（页面样本库登记的采集站点+采集签收单）
  为准，禁止把收件站点或任意活跃站点当作来源；
- 来源未确定时只保留恢复申请、原因与待确认记录，签收单只读，不能计入产出；
- 每条隔离记录至多开出一张恢复凭据，重复提交只返回原凭据且不重复增加产出；
- 产出按来源归属记账，站点目录发生别名并入/归属转交时记账到当前可追溯账目。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class FailureQuarantineService(BaseModuleService):
    MODULE = "failure_quarantine"
    ENTITY = "隔离记录"
    OPERATION_TERM = "解析"
    LIST_FIELDS = [
        "样本指纹", "失败阶段", "失败原因", "来源签收单", "来源站点",
        "归属状态", "记账站点", "处理人", "隔离时间", "重试次数",
    ]
    REQUIRED_FIELDS = ["样本指纹", "失败阶段", "失败原因"]
    STATUS_ORDER = ["待处理", "重试中", "已丢弃", "已恢复"]
    ACTION_RULES = {"立即重试": "待处理", "丢弃样本": "已丢弃"}
    NEGATIVE_ACTIONS = []
    COMMANDS = {
        "提交恢复": "submit_recovery",
        "补核来源": "confirm_source",
        "恢复回执": "view_receipt",
    }

    def __init__(self) -> None:
        # entry_id -> 恢复凭据（一条隔离样本至多一张凭据，重复提交只回读）
        self._receipts: dict[int, dict[str, Any]] = {}

    # ---- 登记：新建隔离记录时默认来源未确定，保留待确认原因 ----

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is None:
            return entry, missing
        entry["来源签收单"] = str(values.get("来源签收单", "") or "")
        entry["来源站点"] = str(values.get("来源站点", "") or "")
        entry["收件站点"] = str(values.get("收件站点", "") or "")
        entry["处理人"] = str(values.get("处理人", "") or "")
        entry["归属状态"] = "待确认"
        entry["归属依据"] = ""
        entry["记账站点"] = ""
        entry["恢复凭据号"] = ""
        entry["恢复计入"] = "否"
        entry["待确认原因"] = str(
            values.get("待确认原因", "") or "恢复申请待确认：样本采集归属尚未补核"
        )
        entry["待确认记录"] = [
            {"时间": _now(), "动作": "隔离登记", "原因": entry["待确认原因"]}
        ]
        return entry, []

    # ---- 业务扩展：失败重试与恢复治理 ----

    def retry_now(self, entry_id: int) -> dict[str, Any]:
        """立即重试隔离样本：按失败阶段回放到对应模块。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        stage = entry.get("失败阶段", "")
        if stage not in ("采集", "解析", "计算", "匹配", "调度", "出口"):
            return {"ok": False, "message": f"未知失败阶段: {stage}"}
        retries = int(str(entry.get("重试次数", "0") or 0)) + 1
        if retries > 5:
            entry["status"] = "已丢弃"
            entry["pending"] = False
            return {"ok": False, "message": "重试次数超限，样本已丢弃"}
        entry["重试次数"] = str(retries)
        entry["status"] = "重试中"
        entry["pending"] = True
        return {
            "ok": True,
            "message": f"样本已按 {stage} 阶段重试",
            "retries": retries,
        }

    # ---- 来源补核：只认可回查的样本采集归属 ----

    def _provenance(self, fingerprint: str) -> dict[str, Any] | None:
        """从页面样本库读取可回查的样本采集归属。"""
        from app.services import service_for
        sample_service = service_for("sample_store")
        if sample_service is None:
            return None
        return sample_service.collection_provenance(fingerprint)

    def confirm_source(
        self,
        entry_id: int,
        source_site: str = "",
        receipt_no: str = "",
        handler: str = "",
        **_: Any,
    ) -> dict[str, Any]:
        """补核来源：与样本库登记的采集归属逐项核对，通过后才允许完成恢复。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "隔离记录不存在"}
        fingerprint = str(entry.get("样本指纹", ""))
        provenance = self._provenance(fingerprint)
        if provenance is None:
            entry.setdefault("待确认记录", []).append({
                "时间": _now(), "动作": "补核来源",
                "原因": "样本库缺少可回查的采集归属，补核被驳回",
            })
            return {"ok": False, "message": f"样本 {fingerprint} 缺少可回查的采集归属，不能确认来源"}

        claimed_site = str(source_site or entry.get("来源站点", "")).strip()
        claimed_receipt = str(receipt_no or entry.get("来源签收单", "")).strip()
        recipient = str(entry.get("收件站点", "")).strip()

        # 收件人/收件站点不是来源：若主张来源恰好是收件站点而采集归属另有其站，驳回。
        if recipient and claimed_site == recipient and provenance["source_site"] != recipient:
            reason = f"收件站点 {recipient} 不是采集来源，禁止按收件人确认归属"
            entry["待确认原因"] = reason
            entry.setdefault("待确认记录", []).append(
                {"时间": _now(), "动作": "补核来源", "原因": reason})
            return {"ok": False, "message": reason}

        if claimed_site != provenance["source_site"]:
            reason = (
                f"主张来源 {claimed_site or '（空）'} 与样本采集归属 "
                f"{provenance['source_site']} 不一致"
            )
            entry["待确认原因"] = reason
            entry.setdefault("待确认记录", []).append(
                {"时间": _now(), "动作": "补核来源", "原因": reason})
            return {"ok": False, "message": reason}
        # 签收单在来源未确定前只读：未显式主张时直接采信样本库登记的采集签收单；
        # 只有显式传入且与采集签收单不符才驳回，防止任意签收单蒙混。
        if claimed_receipt and claimed_receipt != provenance["receipt_no"]:
            reason = (
                f"签收单 {claimed_receipt} 与样本采集签收单 "
                f"{provenance['receipt_no']} 不一致"
            )
            entry["待确认原因"] = reason
            entry.setdefault("待确认记录", []).append(
                {"时间": _now(), "动作": "补核来源", "原因": reason})
            return {"ok": False, "message": reason}

        entry["来源站点"] = provenance["source_site"]
        entry["来源签收单"] = provenance["receipt_no"]
        entry["归属依据"] = (
            f"样本{provenance['sample_id']}指纹{fingerprint}/"
            f"采集签收单{provenance['receipt_no']}/采集处理人{provenance['handler']}"
        )
        entry["归属状态"] = "已确认"
        entry["待确认原因"] = ""
        entry.setdefault("待确认记录", []).append({
            "时间": _now(),
            "动作": "补核来源",
            "原因": f"采集归属核对通过：来源={provenance['source_site']} 签收单={provenance['receipt_no']}",
            "处理人": handler,
        })

        # 已有保留中的恢复申请：补核通过后自动完成，且只计一次。
        pending_receipt = self._receipts.get(entry_id)
        if pending_receipt is not None and not pending_receipt.get("counted"):
            return self._complete_recovery(entry, pending_receipt, handler)
        return {"ok": True, "message": "来源采集归属已确认，可提交恢复", "entry": entry}

    # ---- 提交恢复：幂等凭据 + 按来源归属记账 ----

    def submit_recovery(
        self,
        entry_id: int,
        handler: str = "",
        source_site: str = "",
        receipt_no: str = "",
        **_: Any,
    ) -> dict[str, Any]:
        """提交恢复申请：开出恢复凭据；来源未确定只保留申请，确定后才计入产出。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "隔离记录不存在"}
        if entry.get("status") == "已丢弃":
            return {"ok": False, "message": "已丢弃样本不能恢复"}

        actor = handler or str(entry.get("处理人", ""))
        existing = self._receipts.get(entry_id)
        if existing is not None:
            # 重复递交（含回执中断后的重投）：只登记请求，不重复计入。
            existing.setdefault("requests", []).append({
                "时间": _now(), "处理人": actor, "重复递交": True,
            })
            return {
                "ok": True,
                "duplicate": True,
                "counted": bool(existing.get("counted")),
                "message": (
                    "恢复凭据已存在，本次为重复递交，未重复增加产出"
                    if existing.get("counted")
                    else "恢复申请已保留，来源补核通过后才会计入产出"
                ),
                "receipt": existing,
            }

        receipt_no_id = f"RCV-{entry_id}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        receipt: dict[str, Any] = {
            "receipt_no": receipt_no_id,
            "entry_id": entry_id,
            "fingerprint": entry.get("样本指纹", ""),
            "handler": actor,
            "source_site": str(source_site or entry.get("来源站点", "")),
            "source_receipt": str(receipt_no or entry.get("来源签收单", "")),
            "account_site": "",
            "counted": False,
            "counted_at": "",
            "metrics_id": None,
            "status": "待确认",
            "reason": "",
            "requests": [{"时间": _now(), "处理人": actor, "重复递交": False}],
        }
        self._receipts[entry_id] = receipt
        entry["恢复凭据号"] = receipt_no_id
        entry.setdefault("待确认记录", []).append({
            "时间": _now(), "动作": "提交恢复", "处理人": actor,
        })

        if entry.get("归属状态") != "已确认":
            # 来源尚未确定：保留原因和待确认记录，绝不当作已归属产出供下游累计。
            reason = str(entry.get("待确认原因", "") or "样本采集归属未补核，恢复暂不计入产出")
            receipt["reason"] = reason
            receipt["status"] = "待确认"
            entry["待确认原因"] = reason
            entry["待确认记录"].append({
                "时间": _now(), "动作": "保留待确认", "原因": reason, "处理人": actor,
            })
            entry["恢复计入"] = "否"
            return {
                "ok": True,
                "counted": False,
                "duplicate": False,
                "message": "来源尚未确定：恢复申请与原因已保留，补核通过后才可完成恢复",
                "receipt": receipt,
            }

        return self._complete_recovery(entry, receipt, actor)

    def _complete_recovery(
        self, entry: dict[str, Any], receipt: dict[str, Any], handler: str
    ) -> dict[str, Any]:
        """归属已确认后完成恢复：按来源归属解析记账站点并只增加一次产出。"""
        from app.services import service_for
        shop_service = service_for("shop_site")
        source_site = str(entry.get("来源站点", ""))
        account_site = (
            shop_service.resolve_account_site(source_site) if shop_service else source_site
        )

        metrics_id, before, after = self._credit_output(account_site)
        entry["status"] = "已恢复"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["归属状态"] = "已确认"
        entry["记账站点"] = account_site
        entry["恢复计入"] = "是"
        entry["待确认原因"] = ""
        entry.setdefault("待确认记录", []).append({
            "时间": _now(), "动作": "恢复计入",
            "原因": f"按来源 {source_site} 记账到 {account_site}，产出 {before}→{after}",
            "处理人": handler,
        })

        receipt.update({
            "source_site": source_site,
            "source_receipt": entry.get("来源签收单", ""),
            "account_site": account_site,
            "counted": True,
            "counted_at": _now(),
            "metrics_id": metrics_id,
            "status": "已计入",
            "reason": "",
        })
        return {
            "ok": True,
            "counted": True,
            "duplicate": False,
            "message": f"恢复已计入：来源 {source_site} → 记账站点 {account_site}（产出 {before}→{after}）",
            "receipt": receipt,
        }

    def _credit_output(self, account_site: str) -> tuple[int, int, int]:
        """把一次恢复产出计入指定记账站点的采集量指标，返回(指标id, 计前, 计后)。"""
        rows = store.rows("collect_metrics")
        row = next((m for m in rows if m.get("站点名称") == account_site), None)
        if row is None:
            row = {
                "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
                "统计时间": _now(),
                "站点名称": account_site,
                "抓取量": "0",
                "解析量": "0",
                "产出量": "0",
                "status": "正常",
                "pending": True,
                "abnormal": False,
            }
            rows.append(row)
        before = int(str(row.get("产出量", "0") or 0))
        row["产出量"] = str(before + 1)
        row["统计时间"] = _now()
        return int(row["id"]), before, before + 1

    def mark_restored(self, entry_id: int) -> dict[str, Any]:
        """标记样本已恢复：必须先通过采集归属补核，且全程幂等。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "隔离记录不存在"}
        if entry.get("归属状态") != "已确认":
            return {"ok": False, "message": "来源采集归属未补核，不能标记恢复"}
        return self.submit_recovery(entry_id, handler=str(entry.get("处理人", "")))

    def discard(self, entry_id: int, reason: str) -> dict[str, Any]:
        """丢弃无法恢复的样本并记录最终原因。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "隔离记录不存在"}
        if entry.get("status") == "已恢复":
            return {"ok": False, "message": "已恢复样本不能丢弃"}
        receipt = self._receipts.get(entry_id)
        if receipt is not None and receipt.get("counted"):
            return {"ok": False, "message": "恢复产出已计入，不能丢弃"}
        entry["status"] = "已丢弃"
        entry["失败原因"] = f"{entry.get('失败原因', '')};丢弃:{reason}"
        entry["pending"] = False
        entry.setdefault("待确认记录", []).append(
            {"时间": _now(), "动作": "丢弃样本", "原因": reason})
        return {"ok": True, "message": f"样本已丢弃: {reason}"}

    def retryable_by_stage(self, stage: str) -> dict[str, Any]:
        """筛选某失败阶段可重试的样本清单。"""
        rows = [
            r for r in store.rows(self.MODULE)
            if r.get("失败阶段") == stage and r.get("status") == "待处理"
            and int(str(r.get("重试次数", "0") or 0)) <= 3
        ]
        return {
            "ok": True,
            "message": "可重试样本已筛选",
            "stage": stage,
            "retryable": len(rows),
            "fingerprints": [r.get("样本指纹", "") for r in rows][:10],
        }

    def escalate_to_review(self, entry_id: int) -> dict[str, Any]:
        """多次重试仍失败的样本升级人工复核。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "隔离记录不存在"}
        retries = int(str(entry.get("重试次数", "0") or 0))
        if retries < 3:
            return {"ok": False, "message": "重试次数不足，暂不升级人工复核"}
        rows = store.rows("match_review")
        rid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append({
            "id": rid,
            "记录编号": f"FQ-{entry_id}-{rid}",
            "匹配记录": entry.get("样本指纹", ""),
            "复核结论": f"重试 {retries} 次仍未恢复",
            "复核角色": "数据治理专员",
            "复核时间": _now(),
            "status": "待复核",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "message": "已升级人工复核", "review_id": rid}

    def quarantine_stats(self) -> dict[str, Any]:
        """输出隔离区统计快照供采集量指标消费。"""
        rows = store.rows(self.MODULE)
        by_stage: dict[str, int] = {}
        for r in rows:
            stage = str(r.get("失败阶段", "未知"))
            by_stage[stage] = by_stage.get(stage, 0) + 1
        return {
            "ok": True,
            "message": "隔离区统计完成",
            "total": len(rows),
            "by_stage": by_stage,
            "restored": sum(1 for r in rows if r.get("status") == "已恢复"),
            "pending_confirmation": sum(
                1 for r in rows if r.get("归属状态") == "待确认"
                and r.get("status") != "已丢弃"
            ),
        }

    # ---- 恢复回执与采集量指标消费 ----

    def view_receipt(self, entry_id: int, handler: str = "", **_: Any) -> dict[str, Any]:
        """查看单次恢复依据；归属转交后原处理人只能查看被授权的单次凭据。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "隔离记录不存在"}
        receipt = self._receipts.get(entry_id)
        if receipt is None:
            return {"ok": False, "message": "该隔离记录尚未提交恢复，没有恢复凭据"}
        actor = (handler or "").strip()
        if actor and not self._can_view(receipt, entry, actor):
            return {"ok": False, "message": f"处理人 {actor} 无权查看该单次恢复依据"}
        return {
            "ok": True,
            "message": "恢复回执已出具",
            "counted": bool(receipt.get("counted")),
            "receipt": receipt,
            "entry": {
                "id": entry.get("id"),
                "样本指纹": entry.get("样本指纹", ""),
                "来源签收单": entry.get("来源签收单", ""),
                "来源站点": entry.get("来源站点", ""),
                "归属依据": entry.get("归属依据", ""),
                "记账站点": entry.get("记账站点", ""),
                "恢复计入": entry.get("恢复计入", "否"),
            },
        }

    def _can_view(
        self, receipt: dict[str, Any], entry: dict[str, Any], handler: str
    ) -> bool:
        if handler == str(receipt.get("handler", "")) or handler == str(entry.get("处理人", "")):
            return True
        from app.services import service_for
        shop_service = service_for("shop_site")
        if shop_service is None:
            return False
        return shop_service.receipt_view_granted(
            str(receipt.get("source_site") or entry.get("来源站点", "")), handler
        )

    def all_receipts(self) -> list[dict[str, Any]]:
        """供采集量指标读取每笔恢复的归属与恢复凭据。"""
        return [dict(r) for r in self._receipts.values()]
