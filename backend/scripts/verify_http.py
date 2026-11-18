"""端到端 HTTP 场景验证：覆盖批次幂等、冲突保留/撤回、续做边界、双入口审计回写。

运行：backend/.venv/bin/python -I scripts/verify_http.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BASE = "http://127.0.0.1:8000"
failures: list[str] = []


def call(method: str, path: str, body: object = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" -- {detail}" if detail else ""))
    if not condition:
        failures.append(name)


def find_task(code: str) -> dict:
    _, payload = call("GET", "/api/migration/tasks")
    for task in payload["items"]:
        if task["task_code"] == code:
            return task
    raise AssertionError(f"task {code} not found")


def main() -> int:
    # 0. 健康检查与播种
    _, health = call("GET", "/api/health")
    check("服务健康", health["ok"] is True)
    main_task = find_task("orders_2026Q4")
    tid = main_task["task_id"]
    check("orders_2026Q4 64 段", main_task["segment_count"] == 64)
    check("每段 50 万行", main_task["rows_per_segment"] == 500_000)
    check("目标分区 2026-10-01~12-31",
          main_task["partition_start"] == "2026-10-01" and main_task["partition_end"] == "2026-12-31")
    check("播种已发布 6 段、续做点 7",
          main_task["progress"]["published_count"] == 6 and main_task["progress"]["next_resume_seq"] == 7)

    # 1. 分段明细：源目标行数与校验规则逐段可查
    _, seg_resp = call("GET", f"/api/migration/tasks/{tid}/segments")
    segs = seg_resp["items"]
    check("分段明细 64 条", len(segs) == 64)
    s1 = segs[0]
    check("首段源端 50 万行且已有目标口径",
          s1["source_row_count"] == 500_000 and s1["target_row_count"] == 500_000)
    check("首段已发布并绑定校验结果与血缘",
          s1["status"] == "已发布" and s1["check_result_id"] and s1["lineage_record_id"])

    # 2. 幂等：重复提交已发布分段不追加记录
    _, audits_before = call("GET", "/api/migration/audits?task_id=" + tid)
    _, lineage_before = call("GET", f"/api/migration/tasks/{tid}/lineage")
    _, replay = call("POST", f"/api/migration/tasks/{tid}/batches",
                     {"segment_seqs": [1, 2], "mode": "批量提交"})
    check("重复提交已发布段按幂等处理",
          replay["ok"] and all(o["idempotent"] for o in replay["batch"]["segment_outcomes"].values()))
    _, audits_after = call("GET", "/api/migration/audits?task_id=" + tid)
    _, lineage_after = call("GET", f"/api/migration/tasks/{tid}/lineage")
    check("审计与血缘均未追加",
          audits_after["total"] == audits_before["total"] and lineage_after["total"] == lineage_before["total"])

    # 3. 冲突（保留策略）：第 7 段血缘故障 → 已校验待补血缘，边界停在 7
    _, b7 = call("POST", f"/api/migration/tasks/{tid}/batches",
                 {"segment_seqs": [7], "mode": "整组校验"})
    check("第 7 段批次成功受理", b7["ok"])
    _, detail = call("GET", f"/api/migration/tasks/{tid}")
    seg7 = next(s for s in detail["segments"] if s["seq"] == 7)
    check("第 7 段保留待补血缘", seg7["status"] == "已校验待补血缘")
    check("待补血缘段有校验结果无血缘", bool(seg7["check_result_id"]) and not seg7["lineage_record_id"])
    check("续做位置仍为 7", detail["progress"]["next_resume_seq"] == 7)
    check("任务进入待补血缘", detail["status"] == "待补血缘")

    # 4. 审计清单同步出现待补血缘条目
    _, audits = call("GET", "/api/migration/audits?task_id=" + tid)
    a7 = next(a for a in audits["items"] if a["segment_seq"] == 7)
    check("审计条目与校验结果绑定", a7["check_result_id"] == seg7["check_result_id"])
    check("审计条目保存目标表映射", a7["target_table_mapping"].get("order_amount") == "order_amt")
    check("审计标注待补血缘", "补血缘" in (a7["conclusion_remark"] or ""))

    # 5. 故障恢复后续做：从第 7 段补齐血缘并继续到第 9 段
    _, cleared = call("POST", f"/api/migration/tasks/{tid}/faults/clear", {"clear_all": True})
    check("故障解除接口", cleared["ok"])
    chk7_before = seg7["check_result_id"]
    _, audits7_before = call("GET", "/api/migration/audits?task_id=" + tid)
    audit7_before = sum(1 for a in audits7_before["items"] if a["segment_seq"] == 7)
    _, resume = call("POST", f"/api/migration/tasks/{tid}/resume", {"mode": "批量提交", "limit": 3})
    check("续做批次锁定 7/8/9 段", resume["batch"]["segment_seqs"] == [7, 8, 9])
    _, detail = call("GET", f"/api/migration/tasks/{tid}")
    seg7 = next(s for s in detail["segments"] if s["seq"] == 7)
    check("第 7 段补血缘后发布", seg7["status"] == "已发布" and bool(seg7["lineage_record_id"]))
    check("续做位置推进到 10", detail["progress"]["next_resume_seq"] == 10)
    check("重跑复用同一份校验结果", seg7["check_result_id"] == chk7_before)
    _, audits7_after = call("GET", "/api/migration/audits?task_id=" + tid)
    audit7_after = sum(1 for a in audits7_after["items"] if a["segment_seq"] == 7)
    check("第 7 段重跑未新增审计条目", audit7_before == audit7_after == 1)

    # 6. 一键续做到底：完成状态只在数据与血缘同版后出现
    _, rest = call("POST", f"/api/migration/tasks/{tid}/resume", {"mode": "批量提交"})
    check("剩余分段一次续做完成", rest["ok"] and rest["progress"]["completed"] is True)
    _, detail = call("GET", f"/api/migration/tasks/{tid}")
    check("64 段全部已发布", all(s["status"] == "已发布" for s in detail["segments"]))
    check("任务状态已完成", detail["status"] == "已完成")
    check("完成百分比 100", detail["progress"]["percent"] == 100.0)
    _, lineage = call("GET", f"/api/migration/tasks/{tid}/lineage")
    check("血缘 64 条与分段同版", lineage["total"] == 64)
    check("全部血缘与分段同批次", all(r["batch_id"] for r in lineage["items"]))

    # 7. 审计详情与双入口回写：刷新后两边结论一致
    _, results = call("GET", "/api/migration/check-results?task_id=" + tid)
    chk = results["items"][0]
    _, detail_view = call("GET", f"/api/migration/check-results/{chk['result_id']}")
    check("审计详情返回同一份校验结果",
          detail_view["check_result"]["result_id"] == chk["result_id"] and detail_view["audit_item"] is not None)

    code, bad = call("POST", "/api/migration/audits/conclusion",
                     {"check_result_id": chk["result_id"], "conclusion": "非法结论"})
    check("非法结论被拦截", code == 400)
    _, r1 = call("POST", "/api/migration/audits/conclusion",
                 {"check_result_id": chk["result_id"], "conclusion": "审计通过", "remark": "审计清单入口通过"})
    check("审计清单入口回写成功", r1["ok"] and r1["audit_item"]["status"] == "审计通过")
    _, r2 = call("POST", "/api/migration/entries/conclusion",
                 {"check_result_id": chk["result_id"], "conclusion": "审计驳回", "remark": "其它入口复核驳回"})
    check("其它业务入口同步结论", r2["ok"] and r2["audit_item"]["status"] == "审计驳回")
    # 刷新审计清单，结论以同一份结果为准
    _, audits = call("GET", "/api/migration/audits?task_id=" + tid)
    refreshed = next(a for a in audits["items"] if a["check_result_id"] == chk["result_id"])
    check("刷新后审计清单与另一入口结论相同", refreshed["status"] == "审计驳回" and refreshed["source_entry"] == "其它业务入口")
    _, same = call("GET", f"/api/migration/check-results/{chk['result_id']}")
    check("校验结果保留两次回写痕迹", len(same["check_result"]["conclusions"]) == 2)

    # 8. 撤回策略任务：只发布前一段，续做始终回到撤回段
    small = find_task("payments_2026Q4_demo")
    sid = small["task_id"]
    _, sdetail0 = call("GET", f"/api/migration/tasks/{sid}")
    seg3 = next(s for s in sdetail0["segments"] if s["seq"] == 3)
    check("撤回策略：第 3 段已撤回", seg3["status"] == "已撤回")
    check("只发布前一段（1/2 发布）",
          [s["status"] for s in sdetail0["segments"][:2]] == ["已发布", "已发布"])
    check("续做位置停在撤回段 3", sdetail0["progress"]["next_resume_seq"] == 3)
    # 故障未恢复再续做 → 仍撤回，边界不变
    _, again = call("POST", f"/api/migration/tasks/{sid}/resume", {"mode": "逐条执行"})
    _, sdetail1 = call("GET", f"/api/migration/tasks/{sid}")
    check("故障未恢复续做边界仍停 3", sdetail1["progress"]["next_resume_seq"] == 3)
    # 解除血缘故障、保留第 5 段校验故障后续做
    call("POST", f"/api/migration/tasks/{sid}/faults/clear", {"lineage_seqs": [3]})
    _, sdetail_mid = call("GET", f"/api/migration/tasks/{sid}")
    check("仅清血缘故障时校验故障保留", sdetail_mid["check_fail_seqs"] == [5] and sdetail_mid["lineage_fail_seqs"] == [])
    _, go = call("POST", f"/api/migration/tasks/{sid}/resume", {"mode": "整组校验", "limit": 3})
    check("续做从第 3 段起步", go["batch"]["segment_seqs"] == [3, 4, 5])
    _, sdetail2 = call("GET", f"/api/migration/tasks/{sid}")
    seg5 = next(s for s in sdetail2["segments"] if s["seq"] == 5)
    check("第 5 段校验失败阻断发布", seg5["status"] == "校验失败")
    check("续做位置停在失败段 5", sdetail2["progress"]["next_resume_seq"] == 5)
    # 解除第 5 段校验故障重跑：同一份校验结果原地刷新为通过
    chk5_before = seg5["check_result_id"]
    call("POST", f"/api/migration/tasks/{sid}/faults/clear", {"check_seqs": [5]})
    call("POST", f"/api/migration/tasks/{sid}/batches", {"segment_seqs": [5], "mode": "逐条执行"})
    _, sdetail3 = call("GET", f"/api/migration/tasks/{sid}")
    seg5b = next(s for s in sdetail3["segments"] if s["seq"] == 5)
    check("失败段恢复后发布且校验结果编号不变",
          seg5b["status"] == "已发布" and seg5b["check_result_id"] == chk5_before)
    _, chk5_view = call("GET", f"/api/migration/check-results/{chk5_before}")
    check("原校验结果已刷新为通过", chk5_view["check_result"]["passed"] is True)
    # 此前不限长的续做已把 4~8 段处理完，第 5 段恢复后整个撤回策略任务即全部发布
    _, sfinal = call("GET", f"/api/migration/tasks/{sid}")
    check("撤回策略任务最终全部发布",
          sfinal["progress"]["completed"] is True and sfinal["status"] == "已完成"
          and all(s["status"] == "已发布" for s in sfinal["segments"]))

    # 9. 新建任务接口与参数校验
    code, bad_create = call("POST", "/api/migration/tasks",
                            {"values": {"source_table": "a", "target_table": "b"}})
    check("缺参任务登记被拒", code == 200 and bad_create["ok"] is False)
    _, created = call("POST", "/api/migration/tasks", {"values": {
        "task_code": "manual_demo",
        "source_table": "src.t",
        "target_table": "dst.t",
        "segment_count": 3,
        "rows_per_segment": 100,
    }})
    check("新任务登记并分段", created["ok"] and created["entry"]["segment_count"] == 3)
    nid = created["entry"]["task_id"]
    _, b = call("POST", f"/api/migration/tasks/{nid}/batches",
                {"segment_seqs": [1, 2, 3], "mode": "批量提交"})
    check("新任务三段一次发布", b["progress"]["completed"] is True)

    print()
    if failures:
        print(f"共 {len(failures)} 项失败：{failures}")
        return 1
    print("全部 HTTP 场景验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
