"""采集站点目录领域服务：管理多区域站点的采集范围、语言与货币口径，负责站点验证与封禁。

数据链路位置：采集入口层，是商品价格数据进入平台的第一道关口，负责圈定合法采集
站点范围，并向爬取任务与频控规则提供已验证站点目录。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ShopSiteService(BaseModuleService):
    MODULE = "shop_site"
    ENTITY = "站点配置"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["站点名称", "站点域名", "采集区域", "语言标识", "生效货币", "站点别名", "归属组别", "账目状态"]
    REQUIRED_FIELDS = ["站点名称", "站点域名", "采集区域"]
    STATUS_ORDER = ["待验证", "已验证", "已封禁"]
    ACTION_RULES = {"验证站点": "已验证", "封禁站点": "已封禁"}
    NEGATIVE_ACTIONS = ["封禁站点"]
    COMMANDS = {
        "并入别名": "merge_alias",
        "归属转交": "transfer_attribution",
        "解析记账站点": "resolve_account_command",
    }

    def __init__(self) -> None:
        # 别名 -> 当前生效站点名（别名并入后的解析依据）
        self._alias_map: dict[str, str] = {}
        # 站点名 -> 可查看该站单次恢复依据的处理人
        self._receipt_grants: dict[str, set[str]] = {}
        # 归属转交留痕
        self.transfer_log: list[dict[str, Any]] = []

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is None:
            return entry, missing
        entry["站点别名"] = str(values.get("站点别名", "") or "")
        entry["归属组别"] = str(values.get("归属组别", "") or "")
        entry["账目状态"] = "独立账目"
        return entry

    # ---- 业务扩展：站点目录与采集范围治理 ----

    def verify_site(self, entry_id: int) -> dict[str, Any]:
        """验证站点域名与货币口径，通过后站点进入可采集状态。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        domain = entry.get("站点域名", "")
        currency = entry.get("生效货币", "")
        if not domain or "." not in domain:
            return {"ok": False, "message": "站点域名格式无效，无法验证"}
        if not currency:
            return {"ok": False, "message": "生效货币未配置，无法验证"}
        entry["status"] = "已验证"
        entry["pending"] = False
        entry["abnormal"] = False
        return {"ok": True, "message": f"站点 {domain} 验证通过", "currency": currency}

    def ban_site(self, entry_id: int, reason: str) -> dict[str, Any]:
        """封禁站点并联动下线关联的爬取任务。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        entry["status"] = "已封禁"
        entry["abnormal"] = True
        domain = entry.get("站点域名", "")
        stopped = 0
        for task in store.rows("price_crawler"):
            if task.get("目标站点") == domain and task.get("status") == "运行中":
                task["status"] = "已暂停"
                task["pending"] = True
                stopped += 1
        return {"ok": True, "message": f"站点已封禁: {reason}", "paused_tasks": stopped}

    def list_verified_sites(self, region: str | None = None) -> list[dict[str, Any]]:
        """列出已验证站点目录，供爬取任务选择目标。"""
        rows = [r for r in store.rows(self.MODULE) if r.get("status") == "已验证"]
        if region:
            rows = [r for r in rows if region in str(r.get("采集区域", ""))]
        return rows

    def emit_to_rate_limit(self, entry_id: int) -> dict[str, Any]:
        """向抓取频控写入站点默认频控规则，形成站点→频控链路。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None or entry.get("status") != "已验证":
            return {"ok": False, "message": "站点不存在或未验证，不能下发频控"}
        rows = store.rows("rate_limit")
        rid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append({
            "id": rid,
            "规则名称": f"{entry.get('站点名称', '')}默认频控",
            "目标站点": entry.get("站点域名", ""),
            "每分钟上限": "30",
            "并发上限": "3",
            "退避策略": "指数退避",
            "status": "待生效",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "message": "默认频控规则已生成", "rule_id": rid}

    def sync_currency_to_conv(self, entry_id: int) -> dict[str, Any]:
        """把站点生效货币同步到货币换算器快照需求列表。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "站点不存在"}
        currency = entry.get("生效货币", "")
        if not currency:
            return {"ok": False, "message": "站点未配置生效货币"}
        rows = store.rows("currency_conv")
        exists = any(
            r.get("源货币") == currency or r.get("目标货币") == currency for r in rows
        )
        if exists:
            return {"ok": True, "message": "货币快照已存在", "currency": currency}
        cid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append({
            "id": cid,
            "快照日期": datetime.now(timezone.utc).date().isoformat(),
            "源货币": currency,
            "目标货币": "CNY",
            "汇率值": "1.0000",
            "取数来源": f"shop_site:{entry_id}",
            "status": "待生效",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "message": "货币换算需求已登记", "currency": currency}

    def register_sample_baseline(self, entry_id: int) -> dict[str, Any]:
        """为站点登记页面样本基线指纹，供解析回归对比。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": "站点不存在"}
        domain = entry.get("站点域名", "")
        rows = store.rows("sample_store")
        sid = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        rows.append({
            "id": sid,
            "样本批次": f"site-baseline-{entry_id}",
            "站点名称": entry.get("站点名称", ""),
            "原始HTML摘要": f"{domain} 首页基线快照",
            "抓取时间": datetime.now(timezone.utc).isoformat(),
            "样本指纹": f"fp-site-{entry_id}-{sid}",
            "采集站点": entry.get("站点名称", ""),
            "采集签收单": "",
            "采集处理人": "",
            "status": "已保存",
            "pending": True,
            "abnormal": False,
        })
        return {"ok": True, "message": "站点基线样本已保存", "sample_id": sid}

    # ---- 业务扩展：归属核对、别名并入与账目可追溯 ----

    def merge_alias(self, entry_id: int, target_group: str = "", **_: Any) -> dict[str, Any]:
        """更正站点归属：把该站别名并入指定区域组，旧名只作别名可回查。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        group = target_group.strip()
        if not group:
            return {"ok": False, "message": "并入目标组别不能为空"}
        alias = str(entry.get("站点名称", ""))
        target = self._find_site_by_name(group)
        if target is None:
            # 目标组首次出现：建立组账目站点
            rows = store.rows(self.MODULE)
            new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
            target = {
                "id": new_id,
                "站点名称": group,
                "站点域名": f"{group}.local",
                "采集区域": group,
                "语言标识": entry.get("语言标识", ""),
                "生效货币": entry.get("生效货币", ""),
                "站点别名": "",
                "归属组别": group,
                "账目状态": "组账目",
                "status": "已验证",
                "pending": False,
                "abnormal": False,
            }
            rows.append(target)
        self._alias_map[alias] = str(target.get("站点名称", ""))
        entry["站点别名"] = alias
        entry["归属组别"] = group
        entry["账目状态"] = f"已并入{group}"
        entry["pending"] = False
        return {
            "ok": True,
            "message": f"站点 {alias} 的别名已并入 {group}",
            "alias": alias,
            "account_site": target.get("站点名称", ""),
        }

    def transfer_attribution(
        self,
        entry_id: int,
        target_site: str = "",
        handler: str = "",
        **_: Any,
    ) -> dict[str, Any]:
        """归属转交：把站点产出账目转到目标可追溯站点，并登记单次查看授权。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        target = self._find_site_by_name(target_site.strip())
        if target is None:
            return {"ok": False, "message": f"目标站点 {target_site} 不在站点目录，不能转交"}
        source = str(entry.get("站点名称", ""))
        if source == str(target.get("站点名称", "")):
            return {"ok": False, "message": "目标站点与来源相同，无需转交"}
        self._alias_map[source] = str(target.get("站点名称", ""))
        entry["归属组别"] = str(target.get("归属组别", "") or target.get("站点名称", ""))
        entry["账目状态"] = f"已转交{target.get('站点名称', '')}"
        actor = handler.strip()
        if actor:
            self._receipt_grants.setdefault(source, set()).add(actor)
        self.transfer_log.append({
            "时间": datetime.now(timezone.utc).isoformat(),
            "来源站点": source,
            "目标站点": target.get("站点名称", ""),
            "处理人": actor,
        })
        return {
            "ok": True,
            "message": f"归属已转交：{source} → {target.get('站点名称', '')}",
            "account_site": target.get("站点名称", ""),
        }

    def resolve_account_site(self, site: str) -> str:
        """把来源站点（含已并入的别名/已转交站点）解析为当前记账站点。"""
        seen: set[str] = set()
        current = site
        while current in self._alias_map and current not in seen:
            seen.add(current)
            current = self._alias_map[current]
        return current

    def resolve_account_command(self, entry_id: int, **_: Any) -> dict[str, Any]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return {"ok": False, "message": f"{self.ENTITY} {entry_id} 不存在"}
        source = str(entry.get("站点名称", ""))
        account = self.resolve_account_site(source)
        return {
            "ok": True,
            "message": f"{source} 当前记账站点为 {account}",
            "source_site": source,
            "account_site": account,
        }

    def receipt_view_granted(self, source_site: str, handler: str) -> bool:
        """归属转交后，原处理人只能查看被授权的单次恢复依据。"""
        if not handler:
            return False
        grants = self._receipt_grants.get(source_site, set())
        account = self.resolve_account_site(source_site)
        return handler in grants or handler in self._receipt_grants.get(account, set())

    def _find_site_by_name(self, name: str) -> dict[str, Any] | None:
        if not name:
            return None
        return next(
            (r for r in store.rows(self.MODULE) if r.get("站点名称") == name), None
        )
