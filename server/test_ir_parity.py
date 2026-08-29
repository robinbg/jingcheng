"""test_ir_parity.py — IR 双解释器一致性。

Python 编译出 IR,Python 和 JS 各自解释一遍,同一批探针上的先验均值/噪声/
成本/可行性必须逐位一致。这块是"两个引擎不再分叉"的**唯一**保证 ——
engine.js 里的 irTerm/irGate/irGates 是 prior_dsl 里 _ir_*_eval 的逐字对译,
谁改了一边而没改另一边,这里当场红。

    python -X utf8 test_ir_parity.py     (需要 node 在 PATH 上)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from export_ir import build, _effective_spec
from prior_dsl import ParamSpace, compile_prior
from sandbox import SCENES

HERE = os.path.dirname(os.path.abspath(__file__))
DEMO = os.path.dirname(HERE)
TOL = 1e-9

# 探针:每维在量程上取 7 个位置的确定性网格(第 k 个探针把第 k 维扫过去,
# 其余维停在若干个固定分位上)。要的是覆盖每个门的两侧,不是随机撒点。
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


def py_side():
    """{scene/card: {"probes": [...], "mean": [...], "noise": [...], ...}}"""
    data = build()
    rows = {}
    for scene_id, scene in SCENES.items():
        space = ParamSpace.from_params(scene.params)
        sn = float(scene.theta["sn"])
        xs = probes(space)
        for card_id, ir in data["scenes"][scene_id]["cards"].items():
            # 用与导出器**同一个** effective spec(校准卡按组合编译)。
            # 这里若各写一份"哪些话合起来算"的规则,就是又一处会分叉的地方。
            card = next(c for c in __import__("cards").CARDS[scene_id] if c["id"] == card_id)
            spec, _, _ = _effective_spec(scene_id, card)
            cp = compile_prior(spec, space, scene_id, sigma_f=float(scene.theta["sf"]))
            m, nz, ct, fs = cp.mean_fn(), cp.noise_fn(sn), cp.cost_fn(), cp.feasible_fn()
            rows[f"{scene_id}/{card_id}"] = {
                "probes": xs,
                "mean": [m(x) for x in xs],
                "noise": [nz(x) for x in xs],
                "cost": [ct(x) for x in xs],
                "feas": [1 if fs(x) else 0 for x in xs],
                "sn": sn,
            }
    return data, rows


JS = r"""
const fs = require('fs');
const src = s => fs.readFileSync(s, 'utf8').replace(/^'use strict';/, '');
const S = eval(src(process.argv[2]) + '\n;({priorFromIR:priorFromIR})');
const job = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const out = {};
for (const key of Object.keys(job.rows)) {
  const [sceneId, cardId] = key.split('/');
  const ir = job.ir.scenes[sceneId].cards[cardId];
  const p = S.priorFromIR(ir);
  const xs = job.rows[key].probes, sn = job.rows[key].sn;
  out[key] = {
    mean: xs.map(x => p.meanFn(x)),
    noise: xs.map(x => (p.noiseScale ? sn * p.noiseScale(x) : sn)),
    cost: xs.map(x => (p.costFn ? p.costFn(x) : 1)),
    feas: xs.map(x => (p.feasibleFn ? (p.feasibleFn(x) ? 1 : 0) : 1)),
  };
}
process.stdout.write(JSON.stringify(out));
"""


def main() -> int:
    ir, rows = py_side()
    tmp_job = os.path.join(HERE, "__ir_job.json")
    tmp_js = os.path.join(HERE, "__ir_run.cjs")
    with open(tmp_job, "w", encoding="utf-8") as f:
        json.dump({"ir": ir, "rows": rows}, f, ensure_ascii=False)
    with open(tmp_js, "w", encoding="utf-8") as f:
        f.write(JS)
    try:
        proc = subprocess.run(
            ["node", tmp_js, os.path.join(DEMO, "engine.js"), tmp_job],
            capture_output=True, text=True, encoding="utf-8",
        )
        if proc.returncode != 0:
            print("[fail] node 执行失败:\n" + (proc.stderr or "")[-2000:])
            return 1
        js = json.loads(proc.stdout)
    finally:
        for p in (tmp_job, tmp_js):
            if os.path.exists(p):
                os.remove(p)

    print("=== IR 双解释器一致性 ===")
    bad = 0
    for key, py in rows.items():
        j = js[key]
        worst = {}
        for field in ("mean", "noise", "cost", "feas"):
            d = max(abs(a - b) for a, b in zip(py[field], j[field])) if py[field] else 0.0
            worst[field] = d
        top = max(worst.values())
        n = len(py["probes"])
        if top > TOL:
            bad += 1
            detail = " ".join(f"{k}Δ={v:.3g}" for k, v in worst.items() if v > TOL)
            print(f"  [fail] {key:18s} n={n} {detail}")
        else:
            print(f"  [ok] {key:18s} n={n} 均值/噪声/成本/可行性 Δ=0.00000")
    if bad:
        print(f"\n{bad} 张卡的两个解释器不一致 —— 同一句话会给出两种结论")
        return 1
    print("\n全部一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
