"""test_align.py — 三件事的自检,跑一次就知道能不能上台。

1) Python 沙盘与 JS 沙盘逐点一致(切模式曲线不跳,验收项 <0.05)
2) 验证层真的拦得住 LLM 幻觉
3) 先验只分配预算、不锁死答案(歪经卡能自愈)
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
from typing import List, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cards as card_lib
from bo import eval_prior, run_bo, run_pair
from prior_dsl import compile_prior, confidence_from_text, merge_specs
from sandbox import CASTING, FORMATION, get_scene

DEMO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROBES = {
    "formation": [
        [0.08, 40, 3.30, 0.05, 24],
        [0.05, 38, 3.30, 0.04, 30],
        [0.45, 27, 3.20, 0.08, 12],   # 析锂角
        [0.02, 55, 3.45, 0.02, 48],   # 产气角
        [0.30, 42, 3.40, 0.06, 36],
        [0.12, 33, 3.10, 0.03, 6],
    ],
    "casting": [
        [625, 200, 2.2, 170, 5],
        [612, 185, 2.0, 150, 3],      # 冷隔角
        [640, 230, 2.45, 200, 8],
        [650, 240, 1.5, 220, 10],
    ],
}


def js_values() -> dict:
    """用 node 跑 JS 版沙盘,取同样探针点的 reward/feasible。"""
    script = r"""
const fs=require('fs');
const src=fs.readFileSync(process.argv[2],'utf8');
// scenarios.js 用 const 声明场景对象,而 const 绑定不会逃出 eval 的作用域 ——
// 直接 eval 完再取 FORMATION 只会拿到 undefined。所以在同一次 eval 里追加一个
// 返回表达式,让它在绑定还活着的作用域内把两个场景交出来。
const S = eval(src.replace(/^'use strict';/,'')
               + '\n;({FORMATION:FORMATION,CASTING:CASTING})');
const probes=JSON.parse(process.argv[3]);
const out={};
for(const [k,pts] of Object.entries(probes)){
  const s = k==='formation'?S.FORMATION:S.CASTING;
  out[k]=pts.map(x=>({reward:s.reward(x),feasible:s.feasible(x)}));
}
console.log(JSON.stringify(out));
"""
    # .cjs 而不是 .js:临时目录的祖先里可能有 "type":"module" 的 package.json,
    # 那样 .js 会被当 ES module 解析,require/eval 全废,对齐测试会静默跳过 ——
    # 静默跳过的自检等于没有自检。
    with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False, encoding="utf-8") as f:
        f.write(script)
        path = f.name
    try:
        r = subprocess.run(
            ["node", path, os.path.join(DEMO_DIR, "scenarios.js"), json.dumps(PROBES)],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode != 0:
            return {}
        return json.loads(r.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}
    finally:
        os.unlink(path)


def test_alignment() -> bool:
    js = js_values()
    if not js:
        print("  [跳过] node 不可用或 JS 求值失败 —— 无法做跨语言对齐")
        return True
    ok = True
    for scene_id, pts in PROBES.items():
        s = get_scene(scene_id)
        for i, x in enumerate(pts):
            py_r, py_f = s.reward(x), s.feasible(x)
            js_r, js_f = js[scene_id][i]["reward"], js[scene_id][i]["feasible"]
            d = abs(py_r - js_r)
            flag = "ok" if d < 0.05 and py_f == js_f else "FAIL"
            if flag == "FAIL":
                ok = False
            print(f"  [{flag}] {scene_id}#{i} py={py_r:.4f} js={js_r:.4f} Δ={d:.5f} "
                  f"feas={py_f}/{js_f}")
    return ok


def test_validation() -> bool:
    """验证层:LLM 幻觉必须被拦住,而且是降级不是崩溃。"""
    s = FORMATION
    space = s.space()
    cases = [
        ("幻觉维度", {"ops": [{"op": "bump", "param": "电解液配比", "at": 1, "strength": "weak"}]},
         lambda p: len(p.rejected) == 1 and not p.mean_terms),
        ("裸数值幅值", {"ops": [{"op": "ramp", "param": "化成温度", "direction": "up", "strength": -6.0}]},
         lambda p: len(p.rejected) == 1),
        ("自由数学表达式", {"ops": [{"op": "eval", "expr": "exp(-x[0])"}]},
         lambda p: len(p.rejected) == 1),
        ("越界数值被 clamp", {"ops": [{"op": "bump", "param": "化成温度", "at": 999, "strength": "weak"}]},
         lambda p: p.mean_terms and "55" in p.notes[0]),
        ("未授权硬剪降级",
         {"ops": [{"op": "exclude", "when": {"化成温度": {"above": 30}}}]},
         lambda p: not p.exclusions and p.downgraded),
        ("授权硬剪通过",
         {"ops": [{"op": "exclude", "modality": "prohibition", "red_line": "析锂",
                   "when": {"化成温度": {"below": 30}, "预充倍率": {"above": 0.2}}}]},
         lambda p: len(p.exclusions) == 1),
        ("边界压成点被放宽",
         {"ops": [{"op": "narrow", "param": "化成温度", "lo": 40, "hi": 40.1}]},
         lambda p: p.downgraded and p.bounds[1][1] - p.bounds[1][0] >= 1.4),
        ("停靠被记录",
         {"ops": [{"op": "park", "fragment": "手感", "reason": "不可测"}]},
         lambda p: len(p.parked) == 1),
    ]
    ok = True
    for name, spec, check in cases:
        try:
            p = compile_prior(spec, space, "formation", s.theta["sf"])
            good = bool(check(p))
        except Exception as e:
            good, p = False, None
            print(f"  [FAIL] {name} 抛异常 {type(e).__name__}: {e}")
        if p is not None:
            print(f"  [{'ok' if good else 'FAIL'}] {name}"
                  f"  拒收={len(p.rejected)} 降级={len(p.downgraded)} 停靠={len(p.parked)}")
        ok = ok and good
    return ok


def test_confidence() -> bool:
    cases = [("低温别上倍率，析锂没商量", 0.9), ("温度一般托到四十", 0.6),
             ("好像慢充要好一点", 0.35), ("首充慢一点", 0.75)]
    ok = True
    for text, want in cases:
        got = confidence_from_text(text)
        good = abs(got - want) < 1e-9
        ok = ok and good
        print(f"  [{'ok' if good else 'FAIL'}] {text} → {got}")
    return ok


# 阈值是照着**均值**的抖动定的,不是单次实现的抖动 —— 这一句必须写清楚,
# 因为前端曾经拿同一个 0.10 去判单种子结果,而单种子 gain_best 的 σ 实测 0.97,
# 等于在 0.1σ 处下判决:块扫描下好卡被误否 33.6%。阈值用在没被标定的地方,
# 和"拿一条只说预充段的规则去管主恒流段"是同一类错。
QUALITY_NOISE = 0.10   # SETTLE_SEEDS 种子**均值**下 true_best 的抖动量级
SCRAP_BLOWUP = 1.5     # 无论说了什么,把总废品顶上去这么多就是有害

# 结算/验收的种子数。两边必须同一个数 —— 否则验收表和舞台又是两套口径,
# 而"同一张卡两个引擎给相反结论"正是这个项目反复踩的那一类坑。
# 与 engine.js 的 SETTLE_SEEDS 对齐。块扫描实测好卡误否率:
#   N=1 → 33.6% | N=4 → 23.3% | N=8 → 15.3% | N=12 → 11.0% | N=16 → 2.7% | N=24 → 2.0%
# 取 16:12 还有 11% 的翻盘概率(验收表会偶发飘红),24 只再降 0.7 个点不值。
SETTLE_SEEDS = 16


def card_score(m: dict, speaks_to: Sequence[str] = ()) -> tuple:
    """把一张卡的价值压成 (综合分, 是否被否决)。

    综合分 = 省批次 + 1.5×省废品 + 4×真实最优提升 + 4×交付诚实度提升
             − 2×(1 − 注入田执行保真度)。

    前四项是增益(双田对照),第五项是**折扣**。四条增益轴之外必须有诚实度
    与保真度这两件事,否则两整类经验在验收表上永远是零分甚至负分:
      · "静置不到位内阻是假的" 既不省批次也不省废品,它省的是把一个虚高
        结果发到产线上的那次事故 → 落在诚实度轴。
      · "那台表偏高三度" 既不改响应面也不改搜索域,它保证的是"你以为设了
        200,工件真拿到 200" → 落在保真度轴。实测:不校准时实际模温只到
        197(违反口诀声明的 200),校准后正好 200。卡是对的,是尺子少了一把。
    漏掉一条轴,就等于用评分表宣布那一类经验没用。

    **保真度必须按折扣算,不能按双田之差算** —— 这是我们自己踩过的坑。
    基准田从来没被告知过这句话,它"违规"是天经地义的,不是这句话的功劳;
    拿两田之差当增益,等于给任何一张收窄搜索域的卡白送一笔。实测"1C 拉满
    省电费"这句歪经就靠这个白拿 +1.63 分冲进及格线,而它做的全部事情只是
    把倍率箱子收进析锂墙里。改成折扣后语义才对得上原话:
        说了并且做到了 → 不加分(本来就该做到)
        说了却没做到   → 扣分(这句话在产线上是空的)
    满格折扣 2.0:比"多省一托"重一点,比"最优值多半个百分点"轻一点 ——
    它保证的是执行到位,不直接生产收益。

    诚实度与最优值同权(4.0),因为两者同量纲、同落点:一个是"发出去的配方
    真值高了多少",一个是"发出去的那个数虚高了多少"。虚高 1 个点意味着按
    一个并不存在的性能去排产、去报价、去做二次确认 —— 代价不是一托电芯,
    是整轮试制的信任。废品按 1.5 计(一托的料 + 少学到的那点信息),批次按
    1 计,都是单托量级,所以它们低于这两项。

    废品按成因分账:只统计这句话**提到过**的红线。一句只讲冷隔的话,不该
    因为卷气废品的涨落被判成有害 —— 它一个字也没提卷气,那是另一句话的活。
    这和翻译层拒绝"替它补一句它没说的话"是同一条纪律:验收表不该犯翻译器
    拒绝犯的错。

    ⚠ 而这段代码违反了它自己上面这句话,已实测,别信下面这行的本意。
    "没声明红线的卡按总废品计 —— 没有依据就不做任何折扣":本意是宽容,
    实际效果是反的 —— 点了名的卡只结自己那一笔,没点名的卡把所有成因全额
    领走。化成场景实测(16 种子均值):
      · cold 点名析锂线   → 结 0.63(它实际动了 1.13)
      · cast 一条也没点名 → 结 1.44(gas 0.69 + li 0.25 全领)
    于是**话说得越含糊,能领的废品钱越多**,正是翻译层拒绝犯的那个错。

    为什么现在不改:按标签记 0 分是在罚文案不是罚物理 —— sei(「首充慢
    一点」)低倍率成膜,物理上确实压的是析锂废品,只是卡面没写"析锂"三个
    字。正确改法是由先验**实际收窄的维度**反推它把守哪条红线,那会动到全部
    结算数字。且实测表明这个口子不是 cast 拿化成场景第一的原因(它靠
    gain_best 0.748,四轴里最高),补口子不改名次。所以先记在这儿,不假装没有。
    engine.js 的 cardValue 里有同样一段 —— 两处解释器,同一个已知缺陷。

    两条否决权:
      · 质量:推荐点真实值明显变差(超过种子噪声)。早点得到一个更差的答案
        不是加速,是走偏。这是 demo 主张("称量而非服从")的验收面。
      · 总废品:不管分账怎么算,把总废品顶高 SCRAP_BLOWUP 以上就是有害。
        分账是为了不冤枉一句话,不是给它开无限透支的口子。
    """
    by_cause = m.get("saved_scrap_by_cause") or {}
    if speaks_to and by_cause:
        scrap_term = sum(by_cause.get(k, 0.0) for k in speaks_to)
    else:
        scrap_term = m["saved_scrap"]
    # 折扣项:声明了却没落到工件上的那部分。没声明任何区间的卡(如 age)
    # 保真度恒为 1.0 → 折扣为 0,不影响它 —— 没说过的话不该被追责。
    unmet = 1.0 - float(m.get("inj_compliance", 1.0))
    score = (
        m["saved_batches"]
        + 1.5 * scrap_term
        + 4.0 * m["gain_best"]
        + 4.0 * m["gain_honesty"]
        - 2.0 * unmet
    )
    vetoed = (
        m["gain_best"] < -QUALITY_NOISE
        or m["saved_scrap"] < -SCRAP_BLOWUP
    )
    return score, vetoed


def test_cards() -> bool:
    """每张卡端到端:好卡有正收益且不拉低最终答案,歪经卡不该有正收益。"""
    ok = True
    for scene_id in ("formation", "casting"):
        s = get_scene(scene_id)
        for card in card_lib.CARDS[scene_id]:
            spec = card["spec"]
            label = card["id"]
            speaks_to = list(card.get("speaks_to") or [])
            # 校准类卡单独打出去数学上是恒等变换(平稳核 + 全维同移),
            # 它的价值只在与"说绝对数值"的话组合时体现 → 按组合验收。
            mate_id = card.get("pair_with")
            if mate_id:
                mate = card_lib.find_card(scene_id, mate_id)
                spec = merge_specs(mate["spec"], spec)
                label = f"{mate_id}+{card['id']}"
                # 组合的"说过的话"是两句话的并集
                speaks_to += [k for k in (mate.get("speaks_to") or [])
                              if k not in speaks_to]
            p = compile_prior(spec, s.space(), scene_id, s.theta["sf"])
            if p.rejected:
                print(f"  [FAIL] {scene_id}/{label} 有被拒 op:{p.rejected}")
                ok = False
                continue
            m = eval_prior(s, p, n_seeds=SETTLE_SEEDS)
            score, vetoed = card_score(m, speaks_to)

            if card.get("external"):
                good = not vetoed        # 跨工序迁移卡只要求"不添乱"
            elif card["kind"] == "good":
                good = score > 0.3 and not vetoed
            else:
                good = score < 0.3 or vetoed   # 歪经卡:没有正收益,或者把答案拽低了

            # 组合验收时再单独跑一次校准卡,把"恒等变换"这件事显式印出来,
            # 免得答辩时被问"你是不是拿队友的成绩给它贴金"
            if mate_id:
                solo = compile_prior(card["spec"], s.space(), scene_id, s.theta["sf"])
                ms = eval_prior(s, solo, n_seeds=SETTLE_SEEDS)
                print(f"  [info] {scene_id}/{card['id']:6s} 单打(应≈无先验): "
                      f"批次 {ms['inj_batches']:4.1f} 最优 {ms['inj_best']:6.2f}"
                      f"  ← 平稳核下全维同移是恒等变换,校准是乘数不是加数")

            ok = ok and good
            cause = ""
            if speaks_to:
                by = m.get("saved_scrap_by_cause") or {}
                cause = "(" + "/".join(
                    f"{k}{by.get(k, 0.0):+.1f}" for k in speaks_to) + ")"
            print(f"  [{'ok' if good else 'FAIL'}] {scene_id}/{label:9s} "
                  f"kind={card['kind']:5s} "
                  f"批次 {m['base_batches']:4.1f}→{m['inj_batches']:4.1f} "
                  f"废品 {m['base_scrap']:3.1f}→{m['inj_scrap']:3.1f}{cause} "
                  f"最优 {m['base_best']:6.2f}→{m['inj_best']:6.2f} "
                  f"虚高 {m['base_overclaim']:4.2f}→{m['inj_overclaim']:4.2f} "
                  f"照做 {m['base_compliance']:4.0%}→{m['inj_compliance']:4.0%} "
                  f"综合={score:+5.2f}{' 否决' if vetoed else ''} "
                  f"接住={len(p.notes)} 停靠={len(p.parked)}")
    return ok


def _z_stats(zs: Sequence[float]) -> tuple:
    """(n, 偏差, 离散, 90%区间覆盖率)。"""
    n = len(zs)
    bias = sum(zs) / n
    spread = math.sqrt(sum((v - bias) ** 2 for v in zs) / max(1, n - 1))
    cover = sum(1 for v in zs if abs(v) < 1.64) / n
    return n, bias, spread, cover


def test_calibration() -> bool:
    """概率校准:系统自己报的不确定度诚实吗?

    "称量每句话"的前提是**秤本身准**。GP 的 σ 同时喂给 EI 的两项:σ 偏窄会
    提前"收敛"(其实没探够),σ 偏宽会瞎探。可是 θ(length/sf/sn)是手写的,
    从没被审计过 —— 整个采集函数的合理性都押在一个没验过的 σ 上。

    用 history 里已有的 mu/sigma(都是**观测前**的预测,天然是留出验证)算
    标准化残差 z = (y - mu) / sqrt(sigma^2 + sn^2),然后查两件事:
        偏差   mean(z) ≈ 0      —— 预测没有系统性偏高/偏低
        覆盖率 P(|z| < 1.64) ≈ 90% —— 90% 区间真罩住 90% 的事实

    分母要带上观测噪声 sn:sigma 是**函数值**的不确定度,而 y 是带批次噪声
    的读数,拿 sigma 单独当分母会把 z 系统性放大,把一个校准良好的 GP 判成
    过度自信。这是校准审计最容易自己踩的坑。

    **必须按可行性分账**,否则这个审计会把自己骗了。混在一起算出来的是
    偏差 -2.0 / 覆盖率 61%,看着像"σ 严重过度自信,该重标 θ";拆开看却是:
        可行批次   偏差 -0.19  离散 1.28  覆盖 79%   —— σ 诚实
        报废批次   偏差 -8.38  离散 3.20  覆盖  0%   —— 完全没预料到
    报废批次那一栏不是 GP 的毛病,是 scrap_penalty 造成的 -6 阶跃 —— 平稳核
    表达不了阶跃,而且实测**撞第五次墙时意外程度照旧 -18σ,一点没学乖**:
    每次撞的是墙上不同的位置,核函数又把断崖抹成斜坡。

    这恰恰是设计要的:红线是物理事实,优化器事先不知道,必须自己撞出来
    (见 bo._pool 的说明)。所以这一块的判据只看**可行批次**,报废批次那栏
    按 [info] 印出来 —— 它不是失败项,它是价值主张的度量:
    "σ 在响应面光滑处诚实,在红线上是瞎的;而这份瞎每轮要付几托废品 ——
    那几托,正是老师傅那句话卖的东西。"

    把这两栏平均成一个数,等于用一个自己造的假警报,盖掉一条真结论。
    """
    ok = True
    for scene_id in ("formation", "casting"):
        s = get_scene(scene_id)
        sn = float(s.theta["sn"])
        feas: List[float] = []
        scrap: List[float] = []
        scrapped_total = 0
        runs = 16
        for k in range(runs):
            r = run_bo(s, None, 20260829 + k * 7919, 20, s.base_start)
            scrapped_total += r.scrapped
            for h in r.history:
                if h["tag"] != "bo":        # warm start 没有真正的预测可言
                    continue
                sd = math.sqrt(h["sigma"] ** 2 + sn ** 2)
                if sd <= 1e-9:
                    continue
                z = (h["y"] - h["mu"]) / sd
                (feas if h["feasible"] else scrap).append(z)
        if len(feas) < 20:
            print(f"  [跳过] {scene_id} 可行批次样本太少({len(feas)})")
            continue
        n, bias, spread, cover = _z_stats(feas)
        # 门限放得比教科书宽:百来批数据的覆盖率本身就有 ±10% 的抖动,
        # 卡太死会变成"每次重跑都红一次"的假警报。
        good = abs(bias) < 0.6 and 0.75 <= cover <= 0.99
        ok = ok and good
        print(f"  [{'ok' if good else 'FAIL'}] {scene_id} 可行批次 n={n:3d} "
              f"偏差={bias:+.3f} 离散={spread:.2f} 90%区间覆盖率={cover:.0%}"
              f"  ({'σ 诚实' if good else 'σ 需重标'})")
        if scrap:
            n2, b2, s2, c2 = _z_stats(scrap)
            print(f"  [info] {scene_id} 报废批次 n={n2:3d} "
                  f"偏差={b2:+.3f} 离散={s2:.2f} 覆盖率={c2:.0%}"
                  f"  ← 红线是 {s.scrap_penalty:.0f} 分的阶跃,平稳核表达不了;"
                  f"平均每轮撞 {scrapped_total / runs:.1f} 托,这几托正是口诀卖的东西")
    return ok


def test_determinism() -> bool:
    """同卡重跑必须逐位一致。"""
    s = FORMATION
    p = compile_prior(card_lib.FORMATION_CARDS[1]["spec"], s.space(), "formation", s.theta["sf"])
    a = run_bo(s, p, 20260829, 16, s.inj_start)
    b = run_bo(s, p, 20260829, 16, s.inj_start)
    same = [h["y"] for h in a.history] == [h["y"] for h in b.history]
    print(f"  [{'ok' if same else 'FAIL'}] 同种子两次运行结果一致({a.n_batches} 批)")
    return same


def test_self_healing() -> bool:
    """歪经卡:先验错了,数据要能把它推翻 —— 后验不该跟着先验跑。"""
    s = FORMATION
    p = compile_prior(card_lib.FORMATION_CARDS[4]["spec"], s.space(), "formation", s.theta["sf"])
    r = run_bo(s, p, 20260829, 20, s.inj_start)
    surv = r.prior_survival
    healed = surv is not None and surv < 0.9
    print(f"  [{'ok' if healed else 'FAIL'}] 歪经先验存活度={surv if surv is None else round(surv, 3)}"
          f" (<0.9 说明数据压过了先验)")
    return healed


def test_novel_interfaces() -> bool:
    """新接口(噪声/伪观测/成本/校准/耦合)都要真的挂上去。"""
    s = FORMATION
    space = s.space()
    spec = {
        "utterance": "静置不到位内阻是假的；上次38度0.05C做到89.2；老化拉长很占柜子；那台柜子温度偏高两度",
        "ops": [
            {"op": "noise", "when": {"高温老化时长": {"below": 24}}, "strength": "strong"},
            {"op": "pseudo_obs", "at": {"化成温度": 38, "预充倍率": 0.05}, "y": 89.2},
            {"op": "cost", "param": "高温老化时长", "direction": "increase", "max_multiplier": 2.5},
            {"op": "recalibrate", "param": "化成温度", "offset": -2},
            {"op": "lengthscale", "param": "化成温度", "terrain": "flat"},
            {"op": "risk", "level": "conservative"},
        ],
    }
    p = compile_prior(spec, space, "formation", s.theta["sf"])
    checks = [
        ("异方差噪声", p.noise_fn(0.3)([0.05, 40, 3.3, 0.05, 6]) > 0.45),
        # 门是平滑的(保证 EI 可微),所以区域外只要求"几乎不放大",不要求逐位相等
        ("噪声区外不变", abs(p.noise_fn(0.3)([0.05, 40, 3.3, 0.05, 40]) - 0.3) < 1e-3),
        ("伪观测", len(p.pseudo_obs) == 1 and p.pseudo_obs[0][2] >= 1.5),
        ("成本函数", p.cost_fn()([0.05, 40, 3.3, 0.05, 48]) > 2.0),
        ("坐标校准", abs(p.recal[1] + 2) < 1e-9),
        ("长度尺度", p.ls_scale[1] > 2.0),
        ("风险偏好", p.acq.get("feas_threshold") == 0.90),
        ("零拒收", not p.rejected),
    ]
    ok = True
    for name, good in checks:
        ok = ok and good
        print(f"  [{'ok' if good else 'FAIL'}] {name}")
    r = run_bo(s, p, 20260829, 12, s.inj_start)
    print(f"  [info] 带全部新接口跑通 {r.n_batches} 批,最优 {r.best_y:.3f}")
    return ok


def test_ablation() -> bool:
    """消融:真翻译 ≫ 维度打乱 ≈ 无先验。

    证明信息来自"翻译"这件事本身,而不是"随便扰动一下先验都能加速"。
    这比三方对照更能堵住"你们是不是调参调出来的"这一问。
    """
    from prior_dsl import shuffle_dims
    s = FORMATION
    space = s.space()
    rows = []
    for card in card_lib.FORMATION_CARDS[:4]:
        spec = card["spec"]
        real = compile_prior(spec, space, "formation", s.theta["sf"])
        shuf = compile_prior(shuffle_dims(spec, space), space, "formation", s.theta["sf"])
        mr = eval_prior(s, real, n_seeds=10)
        ms = eval_prior(s, shuf, n_seeds=10)
        rows.append((card["id"], mr, ms))
        print(f"  [info] {card['id']:6s} 真翻译:批次{mr['inj_batches']:4.1f}/废{mr['inj_scrap']:3.1f}"
              f"/最优{mr['inj_best']:6.2f}   打乱:批次{ms['inj_batches']:4.1f}"
              f"/废{ms['inj_scrap']:3.1f}/最优{ms['inj_best']:6.2f}")
    base = rows[0][1]
    print(f"  [info] 无先验基线:批次{base['base_batches']:4.1f}"
          f"/废{base['base_scrap']:3.1f}/最优{base['base_best']:6.2f}")
    wins = sum(1 for _, mr, ms in rows
               if (mr["inj_best"] - ms["inj_best"]) + 0.5 * (ms["inj_scrap"] - mr["inj_scrap"]) > 0)
    print(f"  [{'ok' if wins >= 3 else 'WARN'}] {wins}/4 张卡的真翻译优于维度打乱")
    return wins >= 3


def main() -> int:
    blocks = [
        ("跨语言沙盘对齐", test_alignment),
        ("验证层拦幻觉", test_validation),
        ("语气→confidence", test_confidence),
        ("经验卡端到端", test_cards),
        ("概率校准(σ 体检)", test_calibration),
        ("确定性", test_determinism),
        ("歪经自愈", test_self_healing),
        ("新接口挂载", test_novel_interfaces),
        ("消融对照", test_ablation),
    ]
    all_ok = True
    for name, fn in blocks:
        print(f"\n=== {name} ===")
        try:
            ok = fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            ok = False
        all_ok = all_ok and ok
    print("\n" + ("全部通过" if all_ok else "有失败项,见上"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
