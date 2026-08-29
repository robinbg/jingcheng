"""export_ir.py — 把所有经验卡编译成 IR,写到 demo/cache/priors_ir.json。

为什么需要它:前端断网时用 engine.js 独立跑,那也得用**同一份先验**。原先
scenarios.js 里手写了第二份卡片先验,两份已经悄悄分叉 —— 实测同一张歪经卡,
后端综合分 -3.76(否决),前端却因为「倍率箱子收窄」白省 2.00 批,成了全场
最省批次的卡。同一句话两个引擎给出相反结论,这种事在答辩现场是致命的。

所以:Python 编译一次 → IR(纯数据)→ 前端解释执行。校验层、红线族授权、
体积上限、降级记录全部只在 Python 这一侧;JS 只做算术。

    python -X utf8 export_ir.py

产物是构建物,不是手写文件。改了卡或编译器就重跑一次。
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict

from typing import List, Tuple

from cards import CARDS, find_card
from prior_dsl import ParamSpace, compile_prior, merge_specs
from sandbox import SCENES

OUT = os.path.join(os.path.dirname(__file__), "..", "cache", "priors_ir.json")
# 同一份数据的 <script> 版。file:// 下 fetch 读不了本地 JSON(CORS),而"双击
# index.html 就能演"是断网兜底的最后一层 —— 所以额外落一个赋值到全局的 .js。
OUT_JS = os.path.join(os.path.dirname(__file__), "..", "priors_ir.js")


def _effective_spec(scene_id: str, card: Dict[str, Any]) -> Tuple[Dict[str, Any], List[str], Dict[str, Any] | None]:
    """这张卡真正拿去编译的 spec,以及它声明过的红线。

    校准类卡(pair_with)必须按**组合**编译。平稳核下给所有输入加同一个常数
    是恒等变换 —— "我们那台机模温表偏高三度"单独打出去,数学上等于没打:
    批次、废品、真值全部纹丝不动。实测综合分正好 +0.00。

    那不是这张卡没价值,是它的价值形态不同:校准是**乘数**不是加数,它把
    别人那句"模温守到 200 以上"的坐标搬正 —— 单独存在时没有被搬正的对象。
    后端验收(test_align.py)本来就按组合算,这里必须用同一个口径,否则评委
    在页面上点这张卡会看到一排零,而我们的验收表说它是好卡。

    合并只发生在 Python 这一侧 —— 前端不会得到 merge_specs,也就不可能长出
    第二套"哪些话该合起来算"的规则。
    """
    mate_id = card.get("pair_with")
    speaks_to = list(card.get("speaks_to") or [])
    if not mate_id:
        return card["spec"], speaks_to, None
    mate = find_card(scene_id, mate_id)
    if mate is None:
        return card["spec"], speaks_to, None
    spec = merge_specs(mate["spec"], card["spec"])
    # 组合"说过的话"是两句话的并集:废品分账要按两句话都提过的红线算
    for k in (mate.get("speaks_to") or []):
        if k not in speaks_to:
            speaks_to.append(k)
    return spec, speaks_to, mate


def build() -> Dict[str, Any]:
    out: Dict[str, Any] = {"version": 1, "scenes": {}}
    for scene_id, scene in SCENES.items():
        space = ParamSpace.from_params(scene.params)
        sigma_f = float(scene.theta["sf"])
        cards: Dict[str, Any] = {}
        for card in CARDS.get(scene_id, []):
            spec, speaks_to, mate = _effective_spec(scene_id, card)
            cp = compile_prior(spec, space, scene_id, sigma_f=sigma_f)
            ir = cp.to_ir()
            # 审计轨迹一并带上:翻译卡要显示"这句话被接住了几条、停靠了几条"。
            # 前端不重新判断,只显示 —— 判断已经在编译期做完了。
            ir["card"] = {
                "id": card["id"],
                "kind": card["kind"],
                "text": card["text"],
                "why": card["why"],
                "utterance": card["spec"].get("utterance", ""),
                "rationale": card["spec"].get("rationale", ""),
                "speaks_to": speaks_to,
                "external": bool(card.get("external", False)),
                "pair_with": card.get("pair_with"),
            }
            if mate is not None:
                # 组合过就说出来。前端要在卡面上写清"这句话要和那句话一起用",
                # 不能让评委以为一张校准卡自己就省了那么多批次。
                ir["card"]["paired"] = {
                    "with_id": mate["id"],
                    "with_text": mate["text"],
                    "note": f"校准类经验单独用是恒等变换,已与「{mate['text']}」组合结算",
                }
            ir["audit"] = {
                "rejected": cp.rejected,
                "downgraded": cp.downgraded,
                "volume_cut": round(cp.volume_cut, 4),
                "affected_dims": sorted(set(cp.affected_dims)),
                "hard_cuts": len(cp.exclusions),
            }
            cards[card["id"]] = ir
        out["scenes"][scene_id] = {
            "params": [p["name"] for p in scene.params],
            "sigma_f": sigma_f,
            "cards": cards,
        }
    return out


def main() -> None:
    data = build()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    with open(OUT_JS, "w", encoding="utf-8") as f:
        f.write(
            "/* priors_ir.js — 构建物,别手改。由 server/export_ir.py 生成。\n"
            " * 卡片先验的唯一来源:Python 编译一次,两个引擎解释同一份数据。\n"
            " * 改了 cards.py 或 prior_dsl.py 就重跑 python -X utf8 export_ir.py */\n"
            "window.PRIORS_IR = " + blob + ";\n"
        )
    n = sum(len(s["cards"]) for s in data["scenes"].values())
    size = os.path.getsize(OUT)
    print(f"[ok] {n} 张卡 → {os.path.relpath(OUT)}  ({size/1024:.1f} KB)")
    print(f"[ok] file:// 兜底 → {os.path.relpath(OUT_JS)}  ({os.path.getsize(OUT_JS)/1024:.1f} KB)")
    for sid, s in data["scenes"].items():
        for cid, ir in s["cards"].items():
            print(
                f"  {sid}/{cid:6s} 均值项={len(ir['mean_terms'])} 硬剪={len(ir['exclusions'])}"
                f" 噪声={len(ir['noise_terms'])} 成本={len(ir['cost_terms'])}"
                f" 伪观测={len(ir['pseudo_obs'])} 置信={ir['confidence']:.2f}"
                f" 停靠={len(ir['parked'])}"
            )


if __name__ == "__main__":
    main()
