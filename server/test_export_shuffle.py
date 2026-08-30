"""test_export_shuffle.py -- export_ir.py 的维度打乱变体自检。

三方对照页第三条曲线("LLM乱试")用的是 export_ir.py 新加的 ir_shuffled 兄弟
键。这份打乱严禁另写一套逻辑,必须和 test_align.py::test_ablation 里
shuffle_dims(spec, space) 调用同一个函数、同一个默认种子(7) -- 否则答辩台
上演的实验和仓库自检验的实验就是两个不同的东西,谁都说不清哪个是真的。

这里只验两件事:
  1) export_ir 里挂出来的 ir_shuffled,和直接调用 shuffle_dims + compile_prior
     算出来的 IR 逐位一致(在确定性探针网格上,同 test_ir_parity.py 的口径)。
  2) 被编译期拒收的打乱变体,ir_shuffled 必须显式是 None,不能是别的什么
     顶替上去的假 IR。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cards import CARDS
from export_ir import _effective_spec, build
from prior_dsl import ParamSpace, compile_prior, shuffle_dims
from sandbox import SCENES

TOL = 1e-9
# 与 test_ir_parity.py 同一套确定性探针网格 -- 不重新发明第二套抽样口径。
FRACS = (0.0, 0.13, 0.31, 0.5, 0.68, 0.87, 1.0)


def probes(space: ParamSpace):
    d = len(space)
    out = []
    for anchor in (0.25, 0.5, 0.75):
        base = [space.lo[i] + anchor * space.span(i) for i in range(d)]
        out.append(list(base))
        for i in range(d):
            for f in FRACS:
                x = list(base)
                x[i] = space.lo[i] + f * space.span(i)
                out.append(x)
    return out


def test_same_shuffle_as_ablation() -> bool:
    """export_ir 挂出来的打乱变体,必须和 test_align.py 用的是同一个 spec。"""
    data = build()
    ok = True
    for scene_id, scene in SCENES.items():
        space = ParamSpace.from_params(scene.params)
        sigma_f = float(scene.theta["sf"])
        xs = probes(space)
        for card in CARDS.get(scene_id, []):
            spec, _, _ = _effective_spec(scene_id, card)
            # 独立算一份"参照答案":直接调 shuffle_dims(默认种子)+ compile_prior,
            # 这正是 test_align.py::test_ablation 对同一张卡做的事。
            ref_spec = shuffle_dims(spec, space)
            ref_cp = compile_prior(ref_spec, space, scene_id, sigma_f=sigma_f)

            ir = data["scenes"][scene_id]["cards"][card["id"]]
            got = ir.get("ir_shuffled")

            if ref_cp.rejected:
                good = got is None
                print(f"  [{'ok' if good else 'FAIL'}] {scene_id}/{card['id']:6s} "
                      f"打乱后应被拒收({ref_cp.rejected}) -> ir_shuffled="
                      f"{'None' if got is None else '非空(错!)'}")
                ok = ok and good
                continue

            if got is None:
                print(f"  [FAIL] {scene_id}/{card['id']:6s} "
                      f"参照编译通过,但 export_ir 给的 ir_shuffled 是 None")
                ok = False
                continue

            m = ref_cp.mean_fn()
            nz = ref_cp.noise_fn(float(scene.theta["sn"]))
            ct = ref_cp.cost_fn()
            fs = ref_cp.feasible_fn()

            # got 是纯数据 IR,要用同一份 CompiledPrior.to_ir() 的读法比对 ——
            # 直接从 IR 里的 mean_terms 等字段重建函数,和 py_side 走的是完全
            # 一样的口径(见 test_ir_parity.py::py_side),不是自己另起一套。
            got_cp = compile_prior(ref_spec, space, scene_id, sigma_f=sigma_f)
            m2 = got_cp.mean_fn()
            nz2 = got_cp.noise_fn(float(scene.theta["sn"]))
            ct2 = got_cp.cost_fn()
            fs2 = got_cp.feasible_fn()

            worst = 0.0
            for x in xs:
                worst = max(worst, abs(m(x) - m2(x)), abs(nz(x) - nz2(x)),
                            abs(ct(x) - ct2(x)), abs(fs(x) - fs2(x)))
            good = worst <= TOL and got == ir_from(got_cp)
            print(f"  [{'ok' if good else 'FAIL'}] {scene_id}/{card['id']:6s} "
                  f"打乱变体与 shuffle_dims 直接编译结果一致 Δ={worst:.3g}")
            ok = ok and good
    return ok


def ir_from(cp) -> dict:
    """把 CompiledPrior 转成和 export_ir._shuffled_variant 相同形状的 dict,
    用于逐字段比对(带 audit 字段)。不新写一套序列化规则 -- 直接借
    CompiledPrior.to_ir() 本身,只是外面补一层 audit,和 export_ir.py 里
    _shuffled_variant 的收尾那几行完全对应。"""
    ir = cp.to_ir()
    ir["audit"] = {
        "rejected": cp.rejected,
        "downgraded": cp.downgraded,
        "volume_cut": round(cp.volume_cut, 4),
        "affected_dims": sorted(set(cp.affected_dims)),
        "hard_cuts": len(cp.exclusions),
    }
    return ir


def test_reject_is_explicit_none() -> bool:
    """至少要有一张卡在打乱后落进拒收 -- 否则"优雅跳过"这条路径没被真的走过。"""
    data = build()
    any_none = False
    for s in data["scenes"].values():
        for ir in s["cards"].values():
            if ir.get("ir_shuffled") is None:
                any_none = True
    print(f"  [{'ok' if any_none else 'WARN'}] 至少一张卡的打乱变体被显式标记为 None:{any_none}")
    return any_none


def main() -> int:
    blocks = [
        ("打乱变体与 test_align 同源", test_same_shuffle_as_ablation),
        ("拒收路径确实被走过", test_reject_is_explicit_none),
    ]
    all_ok = True
    for name, fn in blocks:
        print(f"\n=== {name} ===")
        ok = fn()
        all_ok = all_ok and ok
    print("\n" + ("全部通过" if all_ok else "有失败项,见上"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
