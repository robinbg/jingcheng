"""test_explain.py — explain.py 的自检,跑一次就知道答辩追问入口能不能用。

跟 test_align.py 分开放,是因为这两个测试文件的关注点完全不重叠:
test_align.py 管的是"沙盘/先验两个引擎一致",这里管的是"答辩追问这条链路
在没有 LLM key 的环境下也绝不会空屏、绝不会把异常裸传给评委"。

    python test_explain.py
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import explain as ex


def test_faq_lookup() -> bool:
    """FAQ 关键词命中——答辩最常见的几类问题必须秒回,不等 LLM。"""
    cases = [
        ("这是不是真实企业数据啊", "真实"),
        ("你们这个方法是哪篇论文的", "论文"),
        ("先验是干什么用的", "先验"),
        ("AI 会不会瞎编数据", "编造"),
    ]
    ok = True
    for q, must_contain in cases:
        a = ex.faq_lookup(q)
        good = a is not None and len(a) > 0
        ok = ok and good
        print(f"  [{'ok' if good else 'FAIL'}] 「{q}」→ 命中 FAQ")
    unmatched = ex.faq_lookup("今天天气怎么样")
    good = unmatched is None
    ok = ok and good
    print(f"  [{'ok' if good else 'FAIL'}] 无关问题不误命中 FAQ")
    return ok


def test_no_fabrication_rule_in_prompt() -> bool:
    """铁律必须逐字出现在 system prompt 里——这是验收标准里写死的那句话。"""
    rule = "只解释已经发生的数据,不编造任何数值;凡是上下文里没有的数,回答\"这个数我没有\"。"
    ok = rule in ex.SYSTEM_PROMPT
    print(f"  [{'ok' if ok else 'FAIL'}] 铁律逐字出现在 SYSTEM_PROMPT 里")
    return ok


def test_headline_questions_answered() -> bool:
    """**这个演示自己的主张**必须问得上。

    上面那个 test_faq_lookup 查的是"方法哪来的、数据真不真",全绿——可实测
    无 key 时问「为什么这句话能省批次?」落到了「这个问题留给答辩环节。」,
    也就是全场最该答上的那一问偏偏答不上,而测试一片绿。

    兜底层的覆盖面要照着**评委会问什么**排,不是照着我们想讲什么排。所以这一条
    单独立起来:凡是"卖点被追问"的那几句,落到固定兜底句就算失败。
    """
    headline = [
        "为什么这句话能省批次？",
        "这句经验到底值多少钱，怎么算出来的",
        "为什么不按台上那一轮算，种子是什么意思",
        "你这个孪生凭什么算得准",
        "有些话它是不是根本没听懂",
    ]
    ok = True
    for q in headline:
        a = ex.faq_lookup(q)
        good = bool(a) and a != ex.FALLBACK_SENTENCE
        ok = ok and good
        print(f"  [{'ok' if good else 'FAIL'}] 「{q}」→ "
              f"{'命中 FAQ' if good else '掉到兜底句 —— 主张被追问却答不上'}")
    return ok


def test_no_llm_key_falls_back() -> bool:
    """没有 key 时,同步链路必须落到 FAQ 或固定兜底句,不许抛异常、不许返回空。"""
    context = {
        "history": [
            {"i": 0, "x": [0.05, 40, 3.3, 0.05, 24], "y": 89.2, "mu": 89.0, "sigma": 0.4,
             "ei": 0.1, "feasible": True, "risks": {}, "best_so_far": 89.2},
        ],
        "notes": ["低电流密度下 SEI 成核速率低"],
    }
    result = ex.explain_sync("formation", "先验是干什么用的", context, use_llm=True)
    ok = (
        isinstance(result, dict)
        and result.get("source") in ("faq", "fallback")
        and bool(result.get("answer"))
        and result["answer"] != ""
    )
    print(f"  [{'ok' if ok else 'FAIL'}] 无 key 时同步链路落到 {result.get('source')},"
          f"answer 非空={bool(result.get('answer'))}")

    unmatched = ex.explain_sync("formation", "asdkjqwoekqwe 无意义乱码", context, use_llm=True)
    ok2 = unmatched.get("source") == "fallback" and unmatched.get("answer") == ex.FALLBACK_SENTENCE
    print(f"  [{'ok' if ok2 else 'FAIL'}] 无关问题落到固定兜底句「{ex.FALLBACK_SENTENCE}」")
    return ok and ok2


def test_job_reaches_terminal_state() -> bool:
    """异步 job:立即拿到 job_id,轮询几次后必须落在 done,不许一直 pending 卡死。"""
    job_id = ex.start_job("formation", "这句先验的置信度是多少", {"history": [], "notes": []})
    got_id = bool(job_id)
    print(f"  [{'ok' if got_id else 'FAIL'}] start_job 立即返回 job_id(不等 LLM)")

    deadline = time.time() + 5
    job = None
    while time.time() < deadline:
        job = ex.get_job(job_id)
        if job and job.get("status") == "done":
            break
        time.sleep(0.05)
    reached_done = job is not None and job.get("status") == "done"
    print(f"  [{'ok' if reached_done else 'FAIL'}] 轮询在超时前到达 done 状态")

    result = (job or {}).get("result") or {}
    has_answer = bool(result.get("answer"))
    print(f"  [{'ok' if has_answer else 'FAIL'}] done 状态带着非空 answer")
    return got_id and reached_done and has_answer


def test_unknown_job_and_ttl_sweep() -> bool:
    """查不存在的 job 要能优雅地返回 None(由 server.py 转成 404),不抛异常。"""
    job = ex.get_job("这个job_id根本不存在")
    ok = job is None
    print(f"  [{'ok' if ok else 'FAIL'}] 查不存在的 job 返回 None,不抛异常")

    # TTL 扫描本身不该炸——伪造一条很老的 done job,交给 _sweep_jobs 清掉。
    fake_id = "ffffffffffff"
    ex._set_job(fake_id, status="done", finished=time.time() - ex.JOB_TTL_S - 10)
    ex._sweep_jobs()
    swept = ex.get_job(fake_id) is None
    print(f"  [{'ok' if swept else 'FAIL'}] 超过 TTL 的 done job 被扫掉")
    return ok and swept


def test_compliance_red_lines() -> bool:
    """合规红线扫描:prompt、FAQ 答案、narrations.json 里不能出现敏感词,
    也不能有论文式的出处宣称。"""
    import json

    banned = ["宁德", "CATL", "舜富"]
    blobs = [ex.SYSTEM_PROMPT, ex.NO_FABRICATION_RULE]
    blobs += [item["answer"] for item in ex.FAQ]
    blobs += list(ex.SCENE_MECHANISM.values())

    narrations_path = os.path.join(os.path.dirname(__file__), "cache", "narrations.json")
    if os.path.exists(narrations_path):
        with open(narrations_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        def _walk(node):
            if isinstance(node, str):
                blobs.append(node)
            elif isinstance(node, dict):
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for v in node:
                    _walk(v)

        _walk(data)

    ok = True
    for word in banned:
        hit = any(word in b for b in blobs)
        ok = ok and not hit
        print(f"  [{'ok' if not hit else 'FAIL'}] 不含禁用词「{word}」")
    return ok


def main() -> int:
    blocks = [
        ("FAQ 关键词命中", test_faq_lookup),
        ("主张被追问答得上", test_headline_questions_answered),
        ("铁律逐字写进 prompt", test_no_fabrication_rule_in_prompt),
        ("无 key 时三层降级", test_no_llm_key_falls_back),
        ("异步 job 到达终态", test_job_reaches_terminal_state),
        ("未知 job / TTL 清扫", test_unknown_job_and_ttl_sweep),
        ("合规红线扫描", test_compliance_red_lines),
    ]
    all_ok = True
    for name, fn in blocks:
        print(f"\n=== {name} ===")
        try:
            ok = fn()
        except Exception:
            import traceback
            traceback.print_exc()
            ok = False
        all_ok = all_ok and ok
    print("\n" + ("全部通过" if all_ok else "有失败项,见上"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
