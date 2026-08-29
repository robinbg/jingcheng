"""test_bo_parity.py — BO 轨迹的双引擎逐批对账。

test_ir_parity.py 比的是"先验项在网格点上的取值",这里比的是"两个引擎照着
同一份 IR 走出来的路"。后者不是前者的加强版,是它**结构上覆盖不到**的一层:
分叉恰恰发生在先验取值全都一致之后 —— 取值一致只保证每一步的输入相同,不
保证从相同输入选出相同的下一批。实测被这个测试抓出来的两个真 bug:

  1) 采集函数 argmax 被浮点尾数决定。EI 在候选池里有多个近乎相等的极大值,
     两侧的求和顺序差一个 ulp,argmax 就落到不同候选上 —— 第 0~6 批逐位相同,
     第 7 批开始整条轨迹分道扬镳,最终推荐点差 0.4 个百分点。
  2) tie-break 饱和后取的不是同一个:np.lexsort 取末位,而 JS 的 `>` 严格
     比较保留首位。候选池里出现完全相同的 EI 时(EI 下溢到 0 就会成片相同),
     两边选的是同一批候选里的**头尾两端**。

这两个都不是"先验解释错了",是"解释对了之后走岔了"。所以这份对账必须留在
仓库里:它是唯一能看见轨迹分叉的自检。

    cd server && python -X utf8 test_bo_parity.py     (需要 node 在 PATH 上)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cards as card_lib
from bo import run_bo
from prior_dsl import compile_prior, merge_specs
from sandbox import get_scene

HERE = os.path.dirname(os.path.abspath(__file__))
DUMPER = os.path.join(HERE, "parity_dump.cjs")

# 判据 1e-6:两侧都是 float64 同一套算术,同一条轨迹的差应当是 0 或者 ulp 级。
# 1e-6 已经比 ulp 宽九个数量级,放这么松是为了不被打印时的 9 位圆整波及 ——
# 真分叉的量级从来不是 1e-6,是"选了另一个候选点",维度上差的是量程的百分几。
TOL = 1e-6

# 种子日程与轮数在 parity_dump.cjs 里**又写了一份**,不从命令行灌过去 ——
# 灌过去就等于把两侧的日程绑成一根,而"两个引擎跑的其实不是同一批实验"这类
# 手滑正是对账要拦的。与 bo.eval_prior / engine.js 的 evalPrior 同口径。
SEED0, NSEED, SEED_STEP = 20260829, 8, 7919
ITERS_BASE, ITERS_INJ = 24, 20


def jobs_for(scene_id: str):
    """[(标签, 先验)]:先跑无先验基线,再逐卡。基线那一栏不能省 —— 采集函数
    的 argmax 分叉与先验无关,无先验时反而更容易撞上(EI 地形最平)。"""
    s = get_scene(scene_id)
    out = [("__base__", None)]
    for card in card_lib.CARDS[scene_id]:
        # 与 export_ir.py::_effective_spec 同口径:校准类卡(pair_with)必须按
        # **组合**编译。平稳核下给所有输入加同一个常数是恒等变换,"我们那台机
        # 模温表偏高三度"单打出去数学上等于没打 —— 而 IR 里存的就是组合后的
        # 先验。所以这里若按 solo 编译,等于拿两份**不同的先验**去比两个引擎,
        # 比出来的分叉是这个测试自己编的,不是引擎的。
        # 换句话说:口径错了会得到一个假红灯,而假红灯比没有测试更糟 ——
        # 它会把人引去改 engine.js 里根本没错的那几行。
        spec = card["spec"]
        mate_id = card.get("pair_with")
        if mate_id:
            mate = card_lib.find_card(scene_id, mate_id)
            if mate is not None:
                spec = merge_specs(mate["spec"], spec)
        prior = compile_prior(spec, s.space(), scene_id, s.theta["sf"])
        out.append((card["id"], prior))
    return s, out


def py_side() -> dict:
    """Python 侧全量轨迹。字段与 parity_dump.cjs 的输出逐字对应。"""
    rows = {}
    for scene_id in ("formation", "casting"):
        s, jobs = jobs_for(scene_id)
        for label, prior in jobs:
            iters = ITERS_BASE if prior is None else ITERS_INJ
            start = s.base_start if prior is None else s.inj_start
            for k in range(NSEED):
                r = run_bo(s, prior, SEED0 + k * SEED_STEP, iters, start)
                rows[f"{scene_id}/{label}/{k}"] = {
                    "n": r.n_batches,
                    "stopped": r.stopped_by,
                    "true_best": round(r.true_best, 9),
                    "scrap": r.scrapped,
                    "xs": [[round(v, 9) for v in h["x"]] for h in r.history],
                    "ys": [round(h["y"], 9) for h in r.history],
                }
    return rows


def js_side() -> dict:
    """用 node 跑 parity_dump.cjs 取 JS 侧轨迹。

    cwd 故意设成临时目录之外的 HERE 都不给 —— 用 cwd=None 从当前目录调起,
    dumper 自己按 __dirname 解析仓库路径。它要是偷偷依赖 cwd,这里就会炸。
    """
    proc = subprocess.run(
        ["node", DUMPER], capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        print("[fail] node 执行失败:\n" + (proc.stderr or "")[-2000:])
        return {}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        print(f"[fail] JS 输出不是合法 JSON({e});stderr 尾部:\n"
              + (proc.stderr or "")[-1000:])
        return {}


def diff_one(key: str, py: dict, js: dict, params) -> list:
    """一组(场景/卡/种子)的全部分叉,每条都带**精确定位**。

    定位必须给到"第几批 / 哪一维 / 两边各是多少 / 差多少"。只报一句"轨迹不
    一致"等于把人扔回去从头二分 —— 而这两个引擎各七百行,靠肉眼二分找浮点
    尾数差是找不到的。上一次抓 argmax 那个 bug,就是靠"第 7 批第 1 维 py=38.2
    js=41.6"这一行直接定位到候选池的排序上。
    """
    probs = []
    # 逐批 x/y 排在最终指标**前面**报:最终指标不同几乎总是轨迹分叉的下游
    # 后果,先报第一次分叉的批次才是根因所在。同理只报**第一处** —— 分叉之后
    # 两边跑的已经是两条不同的实验,后面每一批都不一样,全打出来是几百行噪音。
    n = min(len(py["xs"]), len(js["xs"]))
    for i in range(n):
        a, b = py["xs"][i], js["xs"][i]
        worst = max(range(len(a)), key=lambda d: abs(a[d] - b[d]))
        if abs(a[worst] - b[worst]) > TOL:
            name = params[worst] if worst < len(params) else f"dim{worst}"
            probs.append(
                f"第{i}批 x 分叉:第{worst}维「{name}」 "
                f"py={a[worst]:.9g} js={b[worst]:.9g} Δ={abs(a[worst] - b[worst]):.3g}"
                f"  (全维 py={a} js={b})")
            break
        dy = abs(py["ys"][i] - js["ys"][i])
        if dy > TOL:
            probs.append(
                f"第{i}批 y 分叉:py={py['ys'][i]:.9g} js={js['ys'][i]:.9g} Δ={dy:.3g}")
            break
    if len(py["xs"]) != len(js["xs"]):
        probs.append(f"轨迹长度 py={len(py['xs'])} js={len(js['xs'])}")
    if py["n"] != js["n"]:
        probs.append(f"批次数 py={py['n']} js={js['n']}")
    if py["stopped"] != js["stopped"]:
        probs.append(f"停机原因 py={py['stopped']} js={js['stopped']}")
    if py["scrap"] != js["scrap"]:
        probs.append(f"废品数 py={py['scrap']} js={js['scrap']}")
    dt = abs(py["true_best"] - js["true_best"])
    if dt > TOL:
        probs.append(f"真值 py={py['true_best']:.9g} js={js['true_best']:.9g} Δ={dt:.3g}")
    return probs


def main() -> int:
    print("=== BO 轨迹双引擎对账 ===")
    print(f"  [info] 种子 {SEED0} 步长 {SEED_STEP} × {NSEED} 个,"
          f"基线 {ITERS_BASE} 批 / 带先验 {ITERS_INJ} 批,容差 {TOL:g}")
    print("  [info] Python 侧生成轨迹...", flush=True)
    py = py_side()
    print(f"  [info] Python 侧 {len(py)} 组;调 node 跑 JS 侧...", flush=True)
    js = js_side()
    if not js:
        # 不静默跳过。node 拿不到就等于这一层自检没跑,而"静默跳过的自检等于
        # 没有自检" —— 这条坑 test_align.py 的 .cjs 注释里已经写过一次了。
        print("\nJS 侧没拿到轨迹 —— 这一层对账没有跑,不能算通过")
        return 1

    params = {sid: [p["name"] for p in get_scene(sid).params]
              for sid in ("formation", "casting")}

    # 键集合先对齐。缺失/多出几乎总是同一个原因:priors_ir.js 是旧构建物,
    # 没跟上 cards.py。这跟"轨迹分叉"是两种完全不同的病,得分开报。
    bad = 0
    only_py = sorted(set(py) - set(js))
    only_js = sorted(set(js) - set(py))
    for k in only_py:
        bad += 1
        print(f"  [fail] {k:26s} JS 侧缺这一组 —— priors_ir.js 可能是旧的,"
              f"重跑 export_ir.py")
    for k in only_js:
        bad += 1
        print(f"  [fail] {k:26s} Python 侧缺这一组 —— cards.py 与 scenarios.js 的卡表不一致")

    checked = 0
    for key in sorted(set(py) & set(js)):
        scene_id = key.split("/")[0]
        probs = diff_one(key, py[key], js[key], params[scene_id])
        checked += 1
        if probs:
            bad += 1
            print(f"  [fail] {key:26s}")
            for p in probs:
                print(f"         {p}")
        else:
            r = py[key]
            print(f"  [ok] {key:26s} {r['n']:2d} 批 停机={r['stopped']:14s} "
                  f"废品={r['scrap']:2d} 真值={r['true_best']:7.3f} 逐批 Δ<{TOL:g}")

    print(f"\n对账 {checked} 组(场景×卡×种子),分叉 {bad} 组")
    if bad:
        print("两个引擎照同一份 IR 走出了不同的路 —— 先按上面报的批次/维度定位")
        return 1
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
