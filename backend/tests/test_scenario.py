"""题面场景端到端校验：S21 归属补核、幂等恢复、别名并入与按归属记账。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services import service_for
from app.store import store


def metrics_snapshot():
    return {
        r["站点名称"]: int(r["产出量"])
        for r in store.rows("collect_metrics")
        if r["站点名称"] in ("北站", "南站", "南区组")
    }


def main():
    fq = service_for("failure_quarantine")
    shop = service_for("shop_site")
    cm = service_for("collect_metrics")

    # 起点：北站 5、南站 8、南区组 0
    snap = metrics_snapshot()
    assert snap == {"北站": 5, "南站": 8, "南区组": 0}, snap
    print("1) 初始账目:", snap)

    # 2. 来源未确定先提交恢复：只保留申请，不计入任何站点
    r = fq.submit_recovery(4, handler="北站处理人")
    assert r["ok"] and r["counted"] is False, r
    assert metrics_snapshot() == {"北站": 5, "南站": 8, "南区组": 0}
    entry4 = store.find("failure_quarantine", 4)
    assert entry4["恢复计入"] == "否" and entry4["归属状态"] == "待确认"
    print("2) 未补核先恢复 -> 仅保留，产出不变:", r["message"])

    # 3. 中断重投：重复提交只能拿到同一张凭据，仍不计入
    r2 = fq.submit_recovery(4, handler="北站处理人")
    assert r2["duplicate"] and r2["counted"] is False
    receipt_no = r2["receipt"]["receipt_no"]
    assert r["receipt"]["receipt_no"] == receipt_no
    assert metrics_snapshot() == {"北站": 5, "南站": 8, "南区组": 0}
    print("3) 中断重投 -> 同凭据、不重复计数:", receipt_no)

    # 4. 禁止把收件站点北站当来源
    r = fq.confirm_source(4, source_site="北站", receipt_no="QS-S21")
    assert not r["ok"], r
    print("4) 收件站点冒充来源被驳回:", r["message"])

    # 5. 签收单不符也要驳回
    r = fq.confirm_source(4, source_site="南站", receipt_no="QS-WRONG")
    assert not r["ok"], r
    print("5) 签收单不符被驳回:", r["message"])

    # 6. 站点目录同时更正：南站别名并入南区组
    r = shop.merge_alias(5, target_group="南区组")
    assert r["ok"] and r["account_site"] == "南区组", r
    assert shop.resolve_account_site("南站") == "南区组"
    print("6) 南站别名并入南区组:", r["message"])

    # 7. 补核通过（南站 / QS-S21，与样本库采集归属一致）-> 自动完成恢复
    r = fq.confirm_source(4, source_site="南站", receipt_no="QS-S21", handler="北站处理人")
    assert r["ok"] and r["counted"] is True, r
    snap = metrics_snapshot()
    assert snap == {"北站": 5, "南站": 8, "南区组": 1}, snap
    entry4 = store.find("failure_quarantine", 4)
    assert entry4["status"] == "已恢复" and entry4["记账站点"] == "南区组"
    assert entry4["来源签收单"] == "QS-S21" and entry4["来源站点"] == "南站"
    print("7) 补核通过并计入 -> 仅南区组 +1:", snap, "| 记账:", entry4["记账站点"])

    # 8. 处理人再次重投凭据：回执显示已计入，产出不再增加
    r = fq.submit_recovery(4, handler="北站处理人")
    assert r["duplicate"] and r["counted"] is True
    assert metrics_snapshot() == {"北站": 5, "南站": 8, "南区组": 1}
    print("8) 再次重投 -> 已计入、不再增加:", r["message"])

    # 9. 恢复回执可显示本次是否已计入、来源签收单与实际记账站点
    r = fq.view_receipt(4, handler="北站处理人")
    assert r["ok"] and r["counted"] is True
    assert r["receipt"]["source_site"] == "南站"
    assert r["receipt"]["source_receipt"] == "QS-S21"
    assert r["receipt"]["account_site"] == "南区组"
    print("9) 回执:", r["receipt"]["receipt_no"], r["receipt"]["source_receipt"],
          "->", r["receipt"]["account_site"], "counted =", r["counted"])

    # 10. 采集量指标按恢复凭据核对归属
    r = cm.recovery_output_detail()
    assert r["ok"] and r["mismatches"] == []
    assert r["recovery_output_by_site"] == {"南区组": 1}, r["recovery_output_by_site"]
    print("10) 指标侧凭据核对:", r["recovery_output_by_site"])

    # 11. 无采集归属的样本：恢复保留待确认，补核驳回，绝不计入
    r = fq.submit_recovery(5, handler="北站处理人")
    assert r["ok"] and r["counted"] is False
    r = fq.confirm_source(5, source_site="北站", receipt_no="X")
    assert not r["ok"]
    entry5 = store.find("failure_quarantine", 5)
    assert entry5["恢复计入"] == "否" and entry5["归属状态"] == "待确认"
    assert entry5["待确认原因"]
    assert len(entry5["待确认记录"]) >= 1
    assert metrics_snapshot() == {"北站": 5, "南站": 8, "南区组": 1}
    print("11) 无归属样本保留待确认，账目不动:", entry5["待确认原因"])

    # 12. 归属转交后的单次恢复依据查看授权
    r = shop.transfer_attribution(5, target_site="南区组", handler="北站处理人")
    assert r["ok"]
    assert shop.receipt_view_granted("南站", "北站处理人") is True
    assert shop.receipt_view_granted("南站", "陌生人") is False
    denied = fq.view_receipt(4, handler="陌生人")
    assert not denied["ok"]
    allowed = fq.view_receipt(4, handler="北站处理人")
    assert allowed["ok"]
    print("12) 转交后仅授权处理人可查看单次恢复依据")

    print("\n全部断言通过 ✔")


if __name__ == "__main__":
    main()
