"""export_faq.py — 把断网兜底需要的两份**文字资料**导成前端构建物 faq.js。

导两样东西,都不是代码,都只有一个来源:
  1) explain.py 的 FAQ 表 + 兜底句 → window.EXPLAIN_FAQ
  2) translate.py 的维度关键词表     → window.LOCAL_DIM_WORDS

为什么要导而不是在 JS 里再写一份:
  `file://` 双击演示下没有后端,"没听懂?问它"那一格和自由输入框必须照旧能用。
  而答辩现场被问到的问题恰恰是最不能临场编的那几类("这是不是真数据""方法
  哪来的"),答案必须逐字符合合规红线。

  在 JS 里手写第二份 FAQ = 两份合规文案会分叉。这个项目在"两份先验"上已经
  栽过一次:后端否决、前端白省两批。合规文案分叉比那个更贵 —— 前端那份要是
  哪天多了一句不该说的话,没有任何测试会红。

  维度关键词表同理:它决定"这句话说的是哪几维",前端兜底和后端规则翻译器
  必须给同一个答案,否则同一句话在有网/断网两场演示里命中不同的维度。

  **注意这里导的是词表,不是编译器。** 前端拿到 dim_words 只能判断"这句话
  提到了哪几维",不能把它编成先验 —— 编译(校验、红线族授权、体积上限、
  降级)照旧只在 prior_dsl.py 里发生。JS 侧没有第二套语义可长。

用法:  python -X utf8 export_faq.py
"""
from __future__ import annotations

import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import explain as ex  # noqa: E402
import translate as tr  # noqa: E402

OUT = os.path.join(os.path.dirname(HERE), "faq.js")

HEAD = """/* faq.js —— 构建物,别手改。由 server/export_faq.py 从 explain.py / translate.py 生成。
 * 两样断网兜底用的文字资料:
 *   window.EXPLAIN_FAQ    追问的 FAQ + 兜底句 + "不编数"铁律原文
 *   window.LOCAL_DIM_WORDS 维度关键词表(判断"这句话说的是哪几维",不编先验)
 * 与后端是同一份文案/同一份词表 —— 手写第二份就会分叉,而合规文案分叉不会有
 * 任何测试变红。
 * 改文案请改 server/explain.py 或 translate.py 后重跑 python -X utf8 export_faq.py */
"""

# 合规复检:这几个词一个都不许出现在发到浏览器的文案里(PRD v2 §10 验收条件)。
# 放在导出这一步而不是放在测试里,是因为它守的是**构建物**:文案改了、忘了跑
# 测试,这道门照旧会拦住。
BANNED = ["宁德", "CATL", "catl", "舜富", "工艺大脑"]


def main() -> None:
    items = [{"keywords": list(it["keywords"]), "answer": it["answer"]} for it in ex.FAQ]
    if not items:
        raise SystemExit("FAQ 是空的 —— 兜底那一层会直接掉到固定句,先检查 explain.py")

    blob = json.dumps(items, ensure_ascii=False) + ex.FALLBACK_SENTENCE
    hit = [w for w in BANNED if w in blob]
    if hit:
        raise SystemExit(f"FAQ 文案里出现了不该出现的词:{hit}")

    payload = {
        "items": items,
        "fallback": ex.FALLBACK_SENTENCE,
        # 铁律原文也带上:前端要把它写在兜底答案的脚注里,让"不编数"这件事
        # 在没有后端的那一场也看得见。
        "rule": ex.NO_FABRICATION_RULE,
    }
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    # 维度词表。只导"词 → 维度名"的对应,不导任何 op 生成逻辑。
    dim_words = {k: list(v) for k, v in tr.DIM_WORDS.items()}
    words = json.dumps({
        "dim_words": dim_words,
        # 方向/语气词也一起导:前端要凭它们说清"这句话我听出了哪些意思",
        # 而不是只说"接住了/没接住"。判断说的是**措辞**,不是先验。
        "forbid": list(tr.FORBID_WORDS),
        "up": list(tr.UP_WORDS),
        "down": list(tr.DOWN_WORDS),
        "flat": list(tr.FLAT_WORDS),
        "fake": list(tr.FAKE_WORDS),
        "couple": list(tr.COUPLE_WORDS),
        "risk": list(tr.RISK_WORDS),
    }, ensure_ascii=False, separators=(",", ":"))

    with io.open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(HEAD)
        f.write("window.EXPLAIN_FAQ = " + body + ";\n")
        f.write("window.LOCAL_DIM_WORDS = " + words + ";\n")
    print(f"[ok] {len(items)} 条 FAQ + 兜底句 + {len(dim_words)} 维词表 → {OUT}")


if __name__ == "__main__":
    main()
