"""HTTP 层端到端校验：通过真实 uvicorn 服务验证命令接口与归属记账。"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "http://127.0.0.1:8020"


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode("utf-8"))


def command(module: str, name: str, entry_id=None, **args) -> dict:
    payload = {"command": name, "args": args}
    if entry_id is not None:
        payload["entry_id"] = entry_id
    return call("POST", f"/api/{module}/commands", payload)


def balances() -> dict[str, str]:
    items = call("GET", "/api/collect_metrics")["items"]
    return {i["站点名称"]: i["产出量"] for i in items if i["站点名称"] in ("北站", "南站", "南区组")}


def main():
    assert balances() == {"北站": "5", "南站": "8", "南区组": "0"}, balances()
    print("1) 初始账目:", balances())

    # 未确认先恢复：保留不计入
    r = command("failure_quarantine", "提交恢复", 4, handler="北站处理人")
    assert r["ok"] and r["data"]["counted"] is False
    assert balances() == {"北站": "5", "南站": "8", "南区组": "0"}
    print("2) 未确认先恢复，仅保留:", r["message"])

    # 中断重投：同凭据
    r2 = command("failure_quarantine", "提交恢复", 4, handler="北站处理人")
    assert r2["data"]["duplicate"] is True and r2["data"]["counted"] is False
    print("3) 重投同凭据、不计入:", r2["data"]["receipt"]["receipt_no"])

    # 收件站点冒充来源
    r = command("failure_quarantine", "补核来源", 4, source_site="北站", receipt_no="QS-S21")
    assert r["ok"] is False
    print("4) 收件站点冒充来源驳回:", r["message"])

    # 签收单不符
    r = command("failure_quarantine", "补核来源", 4, source_site="南站", receipt_no="QS-WRONG")
    assert r["ok"] is False
    print("5) 签收单不符驳回:", r["message"])

    # 站点目录更正：南站别名并入南区组
    r = command("shop_site", "并入别名", 5, target_group="南区组")
    assert r["ok"] and r["data"]["account_site"] == "南区组"
    print("6) 别名并入:", r["message"])

    # 补核通过 -> 自动完成、计入南区组
    r = command("failure_quarantine", "补核来源", 4,
                source_site="南站", receipt_no="QS-S21", handler="北站处理人")
    assert r["ok"] and r["data"]["counted"] is True
    assert balances() == {"北站": "5", "南站": "8", "南区组": "1"}, balances()
    print("7) 补核计入:", r["message"])

    # 再次重投：不增加
    r = command("failure_quarantine", "提交恢复", 4, handler="北站处理人")
    assert r["data"]["duplicate"] and r["data"]["counted"] is True
    assert balances() == {"北站": "5", "南站": "8", "南区组": "1"}
    print("8) 再重投不增加:", r["message"])

    # 回执
    r = command("failure_quarantine", "恢复回执", 4, handler="北站处理人")
    rec = r["data"]["receipt"]
    assert r["data"]["counted"] is True
    assert rec["source_site"] == "南站" and rec["source_receipt"] == "QS-S21"
    assert rec["account_site"] == "南区组"
    print("9) 回执:", rec["receipt_no"], rec["source_receipt"], "->", rec["account_site"])

    # 指标凭据核对
    r = command("collect_metrics", "恢复产出明细")
    assert r["ok"] and r["data"]["mismatches"] == []
    assert r["data"]["recovery_output_by_site"] == {"南区组": 1}
    print("10) 指标凭据核对:", r["data"]["recovery_output_by_site"])

    # 无归属样本保留
    r = command("failure_quarantine", "提交恢复", 5, handler="北站处理人")
    assert r["data"]["counted"] is False
    r = command("failure_quarantine", "补核来源", 5, source_site="北站", receipt_no="X")
    assert r["ok"] is False
    assert balances() == {"北站": "5", "南站": "8", "南区组": "1"}
    print("11) 无归属样本保留待确认，账目不动")

    # 转交后查看授权
    command("shop_site", "归属转交", 5, target_site="南区组", handler="北站处理人")
    denied = command("failure_quarantine", "恢复回执", 4, handler="陌生人")
    allowed = command("failure_quarantine", "恢复回执", 4, handler="北站处理人")
    assert denied["ok"] is False and allowed["ok"] is True
    print("12) 转交后仅授权处理人可查看单次依据")

    # 列表行展示来源签收单与实际记账站点
    items = call("GET", "/api/failure_quarantine")["items"]
    row = next(i for i in items if i["样本指纹"] == "fp-S21")
    assert row["来源签收单"] == "QS-S21" and row["记账站点"] == "南区组"
    assert row["来源站点"] == "南站" and row["恢复计入"] == "是"
    print("13) 隔离列表行:", {k: row[k] for k in
          ("来源签收单", "来源站点", "记账站点", "归属状态", "恢复计入")})

    # 常规动作接口回归
    r = call("POST", "/api/failure_quarantine/1/actions", {"action": "立即重试"})
    assert r["ok"]
    print("14) 通用动作接口回归:", r["message"])

    print("\nHTTP 端到端全部断言通过 ✔")


if __name__ == "__main__":
    main()
