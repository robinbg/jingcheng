"""prior_dsl.py — 自然语言经验 → GP 先验的确定性编译器。

设计原则:经验的种类是开放的,它能作用的数学接口是封闭的。
LLM 不做"分类",它针对下面这份接口手册编程;编译器把 op 列表变成
GP/BO 需要的几个函数对象(均值、可行域、噪声、核尺度、成本、采集配置)。

任何 op 必须满足三条准入:
  1) 影响有界 —— 能被 k 次观测推翻(硬约束是唯一例外,须显式授权)
  2) 可审计   —— 能回译成中文,放进翻译卡给师傅点头
  3) 无量纲   —— 强度是枚举,由 sigma_f 标定,不接受裸数值幅值
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# 强度枚举 → sigma_f 的倍数。LLM 只能给枚举。
STRENGTH: Dict[str, float] = {
    "weak": 0.3,
    "moderate": 0.8,
    "strong": 1.5,
    "prohibitive": 3.0,
}
# 单句话最多允许硬剪掉的可行域体积比例
MAX_VOLUME_CUT = 0.40
# 边界收缩后每维至少保留的量程比例
MIN_BOUND_SPAN = 0.05


def _cl(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else (hi if v > hi else v)


def _sigmoid(z: float) -> float:
    if z < -40.0:
        return 0.0
    if z > 40.0:
        return 1.0
    return 1.0 / (1.0 + math.exp(-z))


def _gate(v: float, thr: float, side: str, width: float) -> float:
    """平滑门。side='below' → v 远小于 thr 时趋 1。平滑保证 EI 可微。"""
    w = width if width > 1e-9 else 1e-9
    z = (thr - v) / w if side == "below" else (v - thr) / w
    return _sigmoid(2.5 * z)


# ---------------------------------------------------------------- 中间表示 IR
# 编译产物不再是 Python 闭包,而是一棵**可序列化的项树**;闭包由解释器现场生成。
#
# 为什么多这一层:前端断网时要用 engine.js 独立跑,那就需要同一份先验。原先的
# 做法是在 scenarios.js 里**手写第二份**卡片先验 —— 手写的那份和这边编译出来的
# 已经悄悄分叉了:实测同一张歪经卡,后端综合分 -3.76(否决),前端却因为
# 「倍率箱子收窄」白省 2.00 批,成了全场最省批次的卡。同一句话,两个引擎给出
# 相反的结论,这种分叉在答辩现场是致命的。
#
# 补第二份手写代码只会再分叉一次。所以改成:Python 编译 → IR(JSON)→ 两边各有
# 一个**解释器**。校验层、授权、体积上限、降级记录全部只在 Python 这一侧;
# JS 只做算术,没有权限做判断,也就没有地方长出第二套语义。
#
# 项的形状是封闭的(五种均值项 + 四种门 + 噪声/成本/盒),对应接口手册里
# 那几个数学接口。新增 op 只能用这些形状拼,拼不出来就得显式 park —— 这条
# 约束是故意的:能落到孪生上的东西必须是两个引擎都算得出来的东西。
IR_TERM_KINDS = ("gauss", "linear", "plateau", "const", "region")
IR_GATE_KINDS = ("below", "above", "between", "near")


def _ir_gate_eval(g: Dict[str, Any], x: Sequence[float]) -> float:
    v = x[g["dim"]]
    k = g["g"]
    if k == "below":
        return _gate(v, g["thr"], "below", g["w"])
    if k == "above":
        return _gate(v, g["thr"], "above", g["w"])
    if k == "between":
        return _gate(v, g["a"], "above", g["w"]) * _gate(v, g["b"], "below", g["w"])
    if k == "near":
        return math.exp(-((v - g["mu"]) / g["w"]) ** 2)
    raise ValueError(f"未知门 {k!r}")


def _ir_gates_eval(gates: Sequence[Dict[str, Any]], x: Sequence[float]) -> float:
    v = 1.0
    for g in gates:
        v *= _ir_gate_eval(g, x)
    return v


def _ir_term_eval(t: Dict[str, Any], x: Sequence[float]) -> float:
    k = t["kind"]
    if k == "const":
        return t["a"]
    if k == "gauss":
        return t["a"] * math.exp(-((x[t["dim"]] - t["mu"]) / t["w"]) ** 2)
    if k == "linear":
        return t["a"] * (x[t["dim"]] - t["lo"]) / t["span"]
    if k == "plateau":
        if t["sign"] > 0:
            return t["a"] * _cl((x[t["dim"]] - t["lo"]) / t["d"], 0.0, 1.0)
        return t["a"] * _cl((t["hi"] - x[t["dim"]]) / t["d"], 0.0, 1.0)
    if k == "region":
        return t["a"] * _ir_gates_eval(t["gates"], x)
    raise ValueError(f"未知项 {k!r}")


def _ir_noise_eval(t: Dict[str, Any], x: Sequence[float]) -> float:
    return 1.0 + t["lam"] * _ir_gates_eval(t["gates"], x)


def _ir_cost_eval(t: Dict[str, Any], x: Sequence[float]) -> float:
    s = (x[t["dim"]] - t["lo"]) / t["span"]
    s = s if t["sign"] > 0 else 1.0 - s
    return 1.0 + (t["mult"] - 1.0) * _cl(s, 0.0, 1.0)


def _ir_in_box(t: Dict[str, Any], x: Sequence[float]) -> bool:
    for i, (lo, hi) in enumerate(t["box"]):
        if not (lo <= x[i] <= hi):
            return False
    return True


# ---------------------------------------------------------------- 参数空间
@dataclass
class ParamSpace:
    """场景参数空间。维度名白名单 + 物理范围,验证层的唯一事实来源。"""

    names: List[str]
    lo: List[float]
    hi: List[float]
    units: List[str] = field(default_factory=list)
    aliases: Dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_params(cls, params: Sequence[Dict[str, Any]]) -> "ParamSpace":
        return cls(
            names=[p["name"] for p in params],
            lo=[float(p["lo"]) for p in params],
            hi=[float(p["hi"]) for p in params],
            units=[p.get("unit", "") for p in params],
        )

    def __len__(self) -> int:
        return len(self.names)

    def index(self, name: str) -> Optional[int]:
        if name in self.names:
            return self.names.index(name)
        canon = self.aliases.get(name)
        if canon and canon in self.names:
            return self.names.index(canon)
        return None

    def span(self, i: int) -> float:
        s = self.hi[i] - self.lo[i]
        return s if s > 1e-12 else 1e-12

    def clamp(self, i: int, v: float) -> float:
        return _cl(float(v), self.lo[i], self.hi[i])

    def norm(self, i: int, v: float) -> float:
        return (float(v) - self.lo[i]) / self.span(i)

    def full_bounds(self) -> List[Tuple[float, float]]:
        return [(self.lo[i], self.hi[i]) for i in range(len(self.names))]


# ---------------------------------------------------------------- 编译产物
@dataclass
class CompiledPrior:
    """编译产物。BO 只认这个对象,不认自然语言,也不认 LLM。"""

    space: ParamSpace
    sigma_f: float = 1.0
    confidence: float = 1.0

    # 下面这几条都是**可序列化的项**,不是闭包。函数对象由 mean_fn/noise_fn/…
    # 现场从项树生成 —— 这样"发给前端的东西"和"后端自己跑的东西"是同一份。
    # 1) 先验均值 m(x)
    mean_terms: List[Dict[str, Any]] = field(default_factory=list)
    # 2) 搜索域(box)
    bounds: List[Tuple[float, float]] = field(default_factory=list)
    # 3) 可行域硬剪除(唯一不可被数据推翻的接口,须显式授权)
    exclusions: List[Dict[str, Any]] = field(default_factory=list)
    # 4) 异方差观测噪声倍率
    noise_terms: List[Dict[str, Any]] = field(default_factory=list)
    # 5) 核长度尺度倍率(每维)
    ls_scale: List[float] = field(default_factory=list)
    # 6) 伪观测 [(x, y, noise_inflate)]
    pseudo_obs: List[Tuple[List[float], float, float]] = field(default_factory=list)
    # 7) 评估成本倍率
    cost_terms: List[Dict[str, Any]] = field(default_factory=list)
    # 8) 采集函数风险配置
    acq: Dict[str, float] = field(default_factory=dict)
    # 9) 坐标校准偏移(读数 → 真值)
    recal: List[float] = field(default_factory=list)

    # 审计轨迹
    notes: List[str] = field(default_factory=list)      # 回译成中文的人话
    parked: List[Dict[str, str]] = field(default_factory=list)
    rejected: List[Dict[str, str]] = field(default_factory=list)
    downgraded: List[str] = field(default_factory=list)
    volume_cut: float = 0.0
    affected_dims: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        d = len(self.space)
        if not self.bounds:
            self.bounds = self.space.full_bounds()
        if not self.ls_scale:
            self.ls_scale = [1.0] * d
        if not self.recal:
            self.recal = [0.0] * d

    # ---- 交给 GP/BO 的函数对象 ----
    # 都是从项树现场生成的解释器闭包。语义只写在 _ir_*_eval 里一处,
    # engine.js 那边的 irMean/irNoise/… 是它逐字的对译。
    def mean_fn(self) -> Callable[[Sequence[float]], float]:
        """m(x)。confidence 乘在最外层 —— "系统称量每句话" 的实现体。"""
        terms = list(self.mean_terms)
        c = self.confidence
        if not terms:
            return lambda x: 0.0

        def m(x: Sequence[float]) -> float:
            return c * sum(_ir_term_eval(t, x) for t in terms)

        return m

    def feasible_fn(self) -> Callable[[Sequence[float]], bool]:
        ex = list(self.exclusions)
        if not ex:
            return lambda x: True

        def f(x: Sequence[float]) -> bool:
            return not any(_ir_in_box(e, x) for e in ex)

        return f

    def noise_fn(self, sigma_n: float) -> Callable[[Sequence[float]], float]:
        """异方差:sigma_n(x) = sigma_n * prod(倍率)。"""
        terms = list(self.noise_terms)
        if not terms:
            return lambda x: sigma_n

        def s(x: Sequence[float]) -> float:
            m = 1.0
            for t in terms:
                m *= max(1.0, _ir_noise_eval(t, x))
            return sigma_n * m

        return s

    def cost_fn(self) -> Callable[[Sequence[float]], float]:
        terms = list(self.cost_terms)
        if not terms:
            return lambda x: 1.0

        def c(x: Sequence[float]) -> float:
            m = 1.0
            for t in terms:
                m *= max(1e-3, _ir_cost_eval(t, x))
            return m

        return c

    def to_ir(self) -> Dict[str, Any]:
        """发给前端的先验。纯数据 —— 没有代码,也没有可执行的东西。

        注意这里**不含**任何校验/授权信息:那些判断已经在编译期做完了,
        IR 里留下的只是判断的**结果**。前端拿到 IR 也无法绕过红线族白名单
        或体积上限,因为它连相应的输入都看不到。
        """
        return {
            "dims": len(self.space),
            "confidence": round(self.confidence, 6),
            "sigma_f": self.sigma_f,
            "mean_terms": self.mean_terms,
            "bounds": [[lo, hi] for lo, hi in self.bounds],
            "exclusions": self.exclusions,
            "noise_terms": self.noise_terms,
            "ls_scale": list(self.ls_scale),
            "pseudo_obs": [
                {"x": list(x), "y": y, "inflate": inf} for x, y, inf in self.pseudo_obs
            ],
            "cost_terms": self.cost_terms,
            "acq": dict(self.acq),
            "recal": list(self.recal),
            "notes": list(self.notes),
            "parked": list(self.parked),
        }

    def summary(self) -> Dict[str, Any]:
        """给翻译卡用的人话摘要。"""
        return {
            "confidence": round(self.confidence, 3),
            "affected_dims": self.affected_dims,
            "notes": self.notes,
            "bounds": [
                {
                    "param": self.space.names[i],
                    "lo": self.bounds[i][0],
                    "hi": self.bounds[i][1],
                    "narrowed": self.bounds[i] != (self.space.lo[i], self.space.hi[i]),
                }
                for i in range(len(self.space))
            ],
            "hard_cuts": len(self.exclusions),
            "volume_cut": round(self.volume_cut, 3),
            "parked": self.parked,
            "rejected": self.rejected,
            "downgraded": self.downgraded,
        }


# ---------------------------------------------------------------- 接口手册
# 每个 op:(需要的字段, 是否需要硬约束授权, 中文回译模板)
OP_TABLE: Dict[str, Dict[str, Any]] = {
    "bump":        {"iface": "mean",  "hard": False},
    "ramp":        {"iface": "mean",  "hard": False},
    "plateau":     {"iface": "mean",  "hard": False},
    "region_score": {"iface": "mean", "hard": False},
    "joint_penalty": {"iface": "mean", "hard": False},
    "shift":       {"iface": "mean",  "hard": False},
    "narrow":      {"iface": "bounds", "hard": False},
    "exclude":     {"iface": "feasible", "hard": True},
    "lengthscale": {"iface": "kernel", "hard": False},
    "couple":      {"iface": "kernel", "hard": False},
    "noise":       {"iface": "noise", "hard": False},
    "pseudo_obs":  {"iface": "data",  "hard": False},
    "cost":        {"iface": "cost",  "hard": False},
    "risk":        {"iface": "acq",   "hard": False},
    "recalibrate": {"iface": "recal", "hard": False},
    "park":        {"iface": "park",  "hard": False},
}

# 预注册红线族 —— 只有落在这里的 prohibition 才有资格硬剪可行域
RED_LINE_FAMILIES: Dict[str, List[str]] = {
    "formation": ["析锂", "产气", "过充", "热失控"],
    "casting":   ["卷气", "冷隔", "缩孔", "飞料"],
}


class OpReject(Exception):
    """该 op 不合法,丢弃(不影响其余 op)。"""


def _strength(op: Dict[str, Any], default: str = "moderate") -> float:
    """强度只能是枚举。裸数值一律拒收 —— 无量纲准入。"""
    s = op.get("strength", default)
    if isinstance(s, (int, float)):
        raise OpReject("强度必须是 weak/moderate/strong/prohibitive 枚举,不接受裸数值")
    key = str(s).strip().lower()
    if key not in STRENGTH:
        raise OpReject(f"未知强度 {s!r}")
    return STRENGTH[key]


def _dim(space: ParamSpace, op: Dict[str, Any], key: str = "param") -> int:
    name = op.get(key)
    if not isinstance(name, str):
        raise OpReject(f"缺少维度名 {key}")
    i = space.index(name)
    if i is None:
        raise OpReject(f"维度 {name!r} 不在参数空间白名单内")
    return i


def _sign(op: Dict[str, Any]) -> float:
    d = str(op.get("direction", "increase")).strip().lower()
    if d in ("increase", "up", "higher", "+", "more"):
        return 1.0
    if d in ("decrease", "down", "lower", "-", "less"):
        return -1.0
    raise OpReject(f"未知方向 {op.get('direction')!r}")


def _region(
    space: ParamSpace, spec: Dict[str, Any]
) -> Tuple[Callable[[Sequence[float]], float], List[int], List[str], List[Dict[str, Any]]]:
    """把 {维度: {below/above/near/between}} 变成软盒指示函数(各维平滑门之积)。

    软盒之和能逼近任意有界形状 —— 这是"接不住的形状"的兜底接口。
    返回 (指示函数, 涉及维度, 中文回译, **门的 IR**) —— 最后一项让调用方
    既能就地求值,也能把同一个区域原样序列化给前端。
    """
    gates: List[Dict[str, Any]] = []
    dims: List[int] = []
    words: List[str] = []
    for name, cond in (spec or {}).items():
        i = space.index(name)
        if i is None:
            raise OpReject(f"维度 {name!r} 不在白名单内")
        if not isinstance(cond, dict):
            raise OpReject(f"{name} 的条件必须是对象")
        span = space.span(i)
        unit = space.units[i] if i < len(space.units) else ""
        dims.append(i)
        if "below" in cond:
            thr = space.clamp(i, cond["below"])
            gates.append({"g": "below", "dim": i, "thr": thr, "w": span * 0.08})
            words.append(f"{name} 低于 {thr:g}{unit}")
        elif "above" in cond:
            thr = space.clamp(i, cond["above"])
            gates.append({"g": "above", "dim": i, "thr": thr, "w": span * 0.08})
            words.append(f"{name} 高于 {thr:g}{unit}")
        elif "between" in cond:
            pair = cond["between"]
            if not (isinstance(pair, (list, tuple)) and len(pair) == 2):
                raise OpReject(f"{name} 的 between 需要 [lo, hi]")
            a, b = sorted(space.clamp(i, v) for v in pair)
            w = max(span * 0.05, (b - a) * 0.15, 1e-9)
            gates.append({"g": "between", "dim": i, "a": a, "b": b, "w": w})
            words.append(f"{name} 在 {a:g}~{b:g}{unit}")
        elif "near" in cond:
            mu = space.clamp(i, cond["near"])
            w = float(cond.get("width", span * 0.12)) or span * 0.12
            w = _cl(abs(w), span * 0.02, span)
            gates.append({"g": "near", "dim": i, "mu": mu, "w": w})
            words.append(f"{name} 靠近 {mu:g}{unit}")
        else:
            raise OpReject(f"{name} 的条件缺少 below/above/between/near")
    if not gates:
        raise OpReject("区域条件为空")

    def ind(x: Sequence[float]) -> float:
        return _ir_gates_eval(gates, x)

    return ind, dims, words, gates


def _box_of(space: ParamSpace, spec: Dict[str, Any]) -> List[Tuple[float, float]]:
    """把区域条件近似成硬盒(用于体积估算与硬剪除)。"""
    box = [(space.lo[i], space.hi[i]) for i in range(len(space))]
    for name, cond in (spec or {}).items():
        i = space.index(name)
        if i is None:
            continue
        lo, hi = box[i]
        if "below" in cond:
            hi = min(hi, space.clamp(i, cond["below"]))
        elif "above" in cond:
            lo = max(lo, space.clamp(i, cond["above"]))
        elif "between" in cond:
            a, b = sorted(space.clamp(i, v) for v in cond["between"])
            lo, hi = max(lo, a), min(hi, b)
        elif "near" in cond:
            mu = space.clamp(i, cond["near"])
            w = abs(float(cond.get("width", space.span(i) * 0.12))) or space.span(i) * 0.12
            lo, hi = max(lo, mu - w), min(hi, mu + w)
        box[i] = (lo, max(lo, hi))
    return box


def _box_volume_frac(space: ParamSpace, box: Sequence[Tuple[float, float]]) -> float:
    v = 1.0
    for i, (lo, hi) in enumerate(box):
        v *= max(0.0, hi - lo) / space.span(i)
    return v


LS_FACTOR = {"flat": 2.2, "smooth": 1.6, "normal": 1.0, "sensitive": 0.6, "sharp": 0.4}
RISK_LEVEL = {
    "conservative": {"kappa": 0.6, "feas_threshold": 0.90},
    "neutral": {"kappa": 1.0, "feas_threshold": 0.60},
    "aggressive": {"kappa": 1.6, "feas_threshold": 0.35},
}
# ---- OP 处理器:每个都只做一件事,失败抛 OpReject ----


def _op_bump(cp: CompiledPrior, op: Dict[str, Any]) -> str:
    sp = cp.space
    i = _dim(sp, op)
    a = _strength(op) * cp.sigma_f
    mu = sp.clamp(i, op.get("at", op.get("near", (sp.lo[i] + sp.hi[i]) / 2)))
    w = abs(float(op.get("width", sp.span(i) * 0.15))) or sp.span(i) * 0.15
    w = _cl(w, sp.span(i) * 0.03, sp.span(i))
    cp.mean_terms.append({"kind": "gauss", "dim": i, "a": a, "mu": mu, "w": w})
    return f"{sp.names[i]} 在 {mu:g}{sp.units[i]} 附近加分(±{w:g})"


def _op_ramp(cp: CompiledPrior, op: Dict[str, Any]) -> str:
    sp = cp.space
    i = _dim(sp, op)
    a = _strength(op) * cp.sigma_f * _sign(op)
    cp.mean_terms.append(
        {"kind": "linear", "dim": i, "a": a, "lo": sp.lo[i], "span": sp.span(i)}
    )
    word = "越大越好" if a > 0 else "越小越好"
    return f"{sp.names[i]} {word}(线性先验)"


def _op_plateau(cp: CompiledPrior, op: Dict[str, Any]) -> str:
    """饱和/拐点:到 knee 之前有收益,之后不再加分。"""
    sp = cp.space
    i = _dim(sp, op)
    s = _sign(op)
    a = _strength(op) * cp.sigma_f
    knee = sp.clamp(i, op.get("knee", op.get("at", (sp.lo[i] + sp.hi[i]) / 2)))
    d = ((knee - sp.lo[i]) if s > 0 else (sp.hi[i] - knee)) or 1e-12
    cp.mean_terms.append({
        "kind": "plateau", "dim": i, "a": a, "sign": s,
        "lo": sp.lo[i], "hi": sp.hi[i], "d": d,
    })
    side = "之后" if s > 0 else "之前"
    return f"{sp.names[i]} 到 {knee:g}{sp.units[i]} 收益饱和,再{'加' if s > 0 else '减'}无意义({side}持平)"


def _op_region_score(cp: CompiledPrior, op: Dict[str, Any]) -> str:
    """兜底接口:任意"这块好/这块坏"。软盒之和逼近任意有界形状。"""
    sp = cp.space
    ind, dims, words, gates = _region(sp, op.get("when") or op.get("region") or {})
    sign = 1.0 if str(op.get("polarity", "good")).lower() in ("good", "+", "reward") else -1.0
    a = _strength(op) * cp.sigma_f * sign
    cp.mean_terms.append({"kind": "region", "a": a, "gates": gates})
    cp.affected_dims.extend(sp.names[i] for i in dims)
    return ("加分区:" if sign > 0 else "扣分区:") + " 且 ".join(words)


def _op_joint_penalty(cp: CompiledPrior, op: Dict[str, Any]) -> str:
    sp = cp.space
    ind, dims, words, gates = _region(sp, op.get("when") or {})
    a = -_strength(op, "strong") * cp.sigma_f
    cp.mean_terms.append({"kind": "region", "a": a, "gates": gates})
    cp.affected_dims.extend(sp.names[i] for i in dims)
    return "联合扣分:" + " 且 ".join(words)


def _op_shift(cp: CompiledPrior, op: Dict[str, Any]) -> str:
    a = _strength(op, "weak") * cp.sigma_f * (1.0 if _sign(op) > 0 else -1.0)
    cp.mean_terms.append({"kind": "const", "a": a})
    return f"整体{'上' if a > 0 else '下'}移常数先验"


def _op_narrow(cp: CompiledPrior, op: Dict[str, Any]) -> str:
    sp = cp.space
    i = _dim(sp, op)
    lo, hi = cp.bounds[i]
    if op.get("lo") is not None:
        lo = max(lo, sp.clamp(i, op["lo"]))
    if op.get("hi") is not None:
        hi = min(hi, sp.clamp(i, op["hi"]))
    if hi <= lo:
        raise OpReject(f"{sp.names[i]} 的收缩区间为空")
    # 每维至少保留 MIN_BOUND_SPAN 量程,防止一句话把空间压成一个点
    min_span = sp.span(i) * MIN_BOUND_SPAN
    if hi - lo < min_span:
        mid = (lo + hi) / 2
        lo = max(sp.lo[i], mid - min_span / 2)
        hi = min(sp.hi[i], mid + min_span / 2)
        cp.downgraded.append(f"{sp.names[i]} 收缩过窄,已放宽到最小 {MIN_BOUND_SPAN:.0%} 量程")
    cp.bounds[i] = (lo, hi)
    cp.affected_dims.append(sp.names[i])
    return f"{sp.names[i]} 搜索范围收到 {lo:g}~{hi:g}{sp.units[i]}"


def _op_exclude(cp: CompiledPrior, op: Dict[str, Any], scene_id: str, authorized: bool) -> str:
    """硬剪除:唯一不可被数据推翻的接口。授权不通过 → 降级为软惩罚。"""
    sp = cp.space
    spec = op.get("when") or op.get("region") or {}
    ind, dims, words, gates = _region(sp, spec)
    phrase = " 且 ".join(words)

    if not authorized:
        a = -STRENGTH["strong"] * cp.sigma_f
        cp.mean_terms.append({"kind": "region", "a": a, "gates": gates})
        cp.downgraded.append(f"未落在预注册红线族,硬剪除降级为强软惩罚:{phrase}")
        cp.affected_dims.extend(sp.names[i] for i in dims)
        return f"重罚(非硬剪):{phrase}"

    box = _box_of(sp, spec)
    frac = _box_volume_frac(sp, box)
    if cp.volume_cut + frac > MAX_VOLUME_CUT:
        a = -STRENGTH["prohibitive"] * cp.sigma_f
        cp.mean_terms.append({"kind": "region", "a": a, "gates": gates})
        cp.downgraded.append(
            f"硬剪除会削掉 {frac:.0%} 可行域(累计超上限 {MAX_VOLUME_CUT:.0%}),降级为最强软惩罚:{phrase}"
        )
        cp.affected_dims.extend(sp.names[i] for i in dims)
        return f"重罚(超体积上限,未硬剪):{phrase}"

    cp.exclusions.append({"box": [[lo, hi] for lo, hi in box]})
    cp.volume_cut += frac
    # 硬剪 + 软罚双写:即使候选生成放宽,EI 地形也不会把优化器领进禁区
    a = -STRENGTH["prohibitive"] * cp.sigma_f
    cp.mean_terms.append({"kind": "region", "a": a, "gates": gates})
    cp.affected_dims.extend(sp.names[i] for i in dims)
    return f"硬剪除(红线):{phrase}"


def _op_lengthscale(cp: CompiledPrior, op: Dict[str, Any]) -> str:
    """经验说"这段差不多" → 核长度尺度放大,别在平坦区浪费批次。"""
    sp = cp.space
    i = _dim(sp, op)
    key = str(op.get("terrain", op.get("level", "flat"))).strip().lower()
    if key not in LS_FACTOR:
        raise OpReject(f"未知地形 {key!r}")
    cp.ls_scale[i] *= LS_FACTOR[key]
    cp.affected_dims.append(sp.names[i])
    word = "平坦,少花批次" if LS_FACTOR[key] > 1 else "敏感,需要密采"
    return f"{sp.names[i]} 响应{word}(长度尺度 ×{LS_FACTOR[key]:g})"


def _op_couple(cp: CompiledPrior, op: Dict[str, Any]) -> str:
    """"这俩得配着调" → 两维同时缩短尺度,GP 才学得到交互项。"""
    sp = cp.space
    names = op.get("params") or []
    if len(names) < 2:
        raise OpReject("couple 需要至少两个维度")
    idx = []
    for n in names:
        i = sp.index(n)
        if i is None:
            raise OpReject(f"维度 {n!r} 不在白名单内")
        idx.append(i)
    for i in idx:
        cp.ls_scale[i] *= 0.7
        cp.affected_dims.append(sp.names[i])
    return "×".join(sp.names[i] for i in idx) + " 存在耦合,联动搜索"


def _op_noise(cp: CompiledPrior, op: Dict[str, Any]) -> str:
    """"静置不到位,内阻是假的" —— 不改最优点,改的是读数可信度。"""
    sp = cp.space
    ind, dims, words, gates = _region(sp, op.get("when") or op.get("region") or {})
    lam = _strength(op, "strong") / STRENGTH["moderate"]  # 1.0 ≈ 噪声翻倍
    cp.noise_terms.append({"lam": lam, "gates": gates})
    cp.affected_dims.extend(sp.names[i] for i in dims)
    return "读数不可信(观测噪声放大):" + " 且 ".join(words)


def _op_pseudo_obs(cp: CompiledPrior, op: Dict[str, Any]) -> str:
    """"上次38度0.05C做到89.2" —— 记忆当伪观测,噪声放大3倍。"""
    sp = cp.space
    at = op.get("at") or {}
    y = op.get("y")
    if y is None:
        raise OpReject("pseudo_obs 缺少观测值 y")
    x = [(sp.lo[i] + sp.hi[i]) / 2 for i in range(len(sp))]
    named: List[str] = []
    for n, v in at.items():
        i = sp.index(n)
        if i is None:
            raise OpReject(f"维度 {n!r} 不在白名单内")
        x[i] = sp.clamp(i, v)
        named.append(f"{sp.names[i]}={x[i]:g}{sp.units[i]}")
        cp.affected_dims.append(sp.names[i])
    if not named:
        raise OpReject("pseudo_obs 未指定任何维度")
    inflate = float(op.get("noise_inflate", 3.0))
    cp.pseudo_obs.append((x, float(y), _cl(inflate, 1.5, 8.0)))
    return f"口述记忆当一次弱观测({', '.join(named)} → {float(y):g}),噪声放大 {_cl(inflate, 1.5, 8.0):g}×"


def _op_cost(cp: CompiledPrior, op: Dict[str, Any]) -> str:
    """"老化拉长很占柜子" —— 进 EI/cost,不进目标函数。"""
    sp = cp.space
    i = _dim(sp, op)
    mult = _cl(float(op.get("max_multiplier", 2.0)), 1.05, 6.0)
    s = _sign(op)
    cp.cost_terms.append(
        {"dim": i, "mult": mult, "sign": s, "lo": sp.lo[i], "span": sp.span(i)}
    )
    cp.affected_dims.append(sp.names[i])
    word = "越大" if s > 0 else "越小"
    return f"{sp.names[i]} {word}越占资源(评估成本最高 {mult:g}×,EI 按性价比折算)"


def _op_risk(cp: CompiledPrior, op: Dict[str, Any]) -> str:
    """"宁可慢点也别出废品" —— 改采集函数的风险偏好。"""
    key = str(op.get("level", "conservative")).strip().lower()
    if key not in RISK_LEVEL:
        raise OpReject(f"未知风险档 {key!r}")
    cp.acq.update(RISK_LEVEL[key])
    word = {"conservative": "保守", "neutral": "中性", "aggressive": "激进"}[key]
    return f"探索姿态转为{word}(可行概率门槛 {RISK_LEVEL[key]['feas_threshold']:.0%})"


def _op_recalibrate(cp: CompiledPrior, op: Dict[str, Any]) -> str:
    """"我们那台柜子温度显示偏高两度" —— 坐标校准,读数→真值。"""
    sp = cp.space
    i = _dim(sp, op)
    off = float(op.get("offset", 0.0))
    off = _cl(off, -sp.span(i) * 0.25, sp.span(i) * 0.25)
    cp.recal[i] += off
    cp.affected_dims.append(sp.names[i])
    return f"{sp.names[i]} 读数校准 {off:+g}{sp.units[i]}"


def _op_park(cp: CompiledPrior, op: Dict[str, Any]) -> str:
    """显式停靠 —— 接不住就老实说接不住,并写清原因。"""
    cp.parked.append({
        "fragment": str(op.get("fragment", "")),
        "reason_code": str(op.get("reason_code", "unsupported")),
        "reason": str(op.get("reason", "当前参数空间与接口无法承载")),
    })
    return f"未接住:{op.get('fragment', '')} —— {op.get('reason', '')}"


# ---------------------------------------------------------------- 语气 → confidence
HEDGE_STRONG = ["没商量", "必", "一定", "绝对", "肯定", "铁定", "从来"]
HEDGE_MID = ["一般", "通常", "多半", "大概", "基本", "差不多"]
HEDGE_WEAK = ["好像", "似乎", "可能", "也许", "听说", "据说"]


def confidence_from_text(text: str, given: Optional[float] = None) -> float:
    """语气副词 → 先验权重。LLM 给了就 clamp,没给就从原话推。"""
    if given is not None:
        try:
            return _cl(float(given), 0.05, 1.0)
        except (TypeError, ValueError):
            pass
    t = text or ""
    if any(w in t for w in HEDGE_STRONG):
        return 0.9
    if any(w in t for w in HEDGE_WEAK):
        return 0.35
    if any(w in t for w in HEDGE_MID):
        return 0.6
    return 0.75


def _authorized_hard_cut(scene_id: str, op: Dict[str, Any], rationale: str) -> bool:
    """硬约束授权:modality=prohibition 且命中预注册红线族。"""
    if str(op.get("modality", "")).strip().lower() not in ("prohibition", "forbid", "safety"):
        return False
    fam = RED_LINE_FAMILIES.get(scene_id, [])
    blob = " ".join([str(op.get("red_line", "")), str(op.get("reason", "")), rationale])
    return any(k in blob for k in fam)


# ---------------------------------------------------------------- 编译器
def compile_prior(
    spec: Dict[str, Any],
    space: ParamSpace,
    scene_id: str,
    sigma_f: float = 1.0,
) -> CompiledPrior:
    """PriorSpec(dict) → CompiledPrior。任何单个 op 失败只丢它自己。"""
    utterance = str(spec.get("utterance", ""))
    rationale = str(spec.get("rationale", ""))
    cp = CompiledPrior(
        space=space,
        sigma_f=max(1e-6, float(sigma_f)),
        confidence=confidence_from_text(utterance, spec.get("confidence")),
    )

    ops = spec.get("ops")
    if ops is None:
        ops = _legacy_to_ops(spec)
    if not isinstance(ops, list):
        cp.rejected.append({"op": "<root>", "reason": "ops 必须是数组"})
        ops = []

    for raw in ops:
        if not isinstance(raw, dict):
            cp.rejected.append({"op": str(raw)[:40], "reason": "op 必须是对象"})
            continue
        kind = str(raw.get("op", raw.get("type", ""))).strip().lower()
        if kind not in OP_TABLE:
            cp.rejected.append({"op": kind or "<empty>", "reason": "不在接口手册内(拒收自由数学表达式)"})
            continue
        try:
            if kind == "bump":
                note = _op_bump(cp, raw)
            elif kind == "ramp":
                note = _op_ramp(cp, raw)
            elif kind == "plateau":
                note = _op_plateau(cp, raw)
            elif kind == "region_score":
                note = _op_region_score(cp, raw)
            elif kind == "joint_penalty":
                note = _op_joint_penalty(cp, raw)
            elif kind == "shift":
                note = _op_shift(cp, raw)
            elif kind == "narrow":
                note = _op_narrow(cp, raw)
            elif kind == "exclude":
                note = _op_exclude(cp, raw, scene_id, _authorized_hard_cut(scene_id, raw, rationale))
            elif kind == "lengthscale":
                note = _op_lengthscale(cp, raw)
            elif kind == "couple":
                note = _op_couple(cp, raw)
            elif kind == "noise":
                note = _op_noise(cp, raw)
            elif kind == "pseudo_obs":
                note = _op_pseudo_obs(cp, raw)
            elif kind == "cost":
                note = _op_cost(cp, raw)
            elif kind == "risk":
                note = _op_risk(cp, raw)
            elif kind == "recalibrate":
                note = _op_recalibrate(cp, raw)
            else:
                note = _op_park(cp, raw)
            cp.notes.append(note)
        except OpReject as e:
            cp.rejected.append({"op": kind, "reason": str(e)})
        except (KeyError, TypeError, ValueError, IndexError) as e:
            cp.rejected.append({"op": kind, "reason": f"字段错误:{e}"})

    # 去重,保序
    seen = set()
    dims = []
    for n in cp.affected_dims:
        if n not in seen:
            seen.add(n)
            dims.append(n)
    cp.affected_dims = dims
    return cp


def _legacy_to_ops(spec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """兼容 plan §2 里那版扁平 schema(bounds/prior_mean/feasibility)。"""
    ops: List[Dict[str, Any]] = []
    for b in spec.get("bounds") or []:
        ops.append({"op": "narrow", "param": b.get("param"), "lo": b.get("lo"), "hi": b.get("hi")})
    for m in spec.get("prior_mean") or []:
        o = dict(m)
        o["op"] = o.pop("type", "shift")
        o.setdefault("strength", "moderate")
        if "weight" in o:
            w = abs(float(o.pop("weight") or 0))
            o["strength"] = "weak" if w < 1 else ("moderate" if w < 2.5 else ("strong" if w < 5 else "prohibitive"))
        ops.append(o)
    for f in spec.get("feasibility") or []:
        o = dict(f)
        o["op"] = "exclude"
        o.setdefault("modality", "prohibition")
        if o.pop("same_as_above", False) and ops:
            for prev in reversed(ops):
                if prev.get("when"):
                    o["when"] = prev["when"]
                    break
        ops.append(o)
    return ops


def spec_hash(utterance: str, scene_id: str) -> str:
    """同一句话 + 同一场景 → 同一结果。确定性要求的实现。"""
    h = hashlib.sha256(f"{scene_id}||{utterance.strip()}".encode("utf-8"))
    return h.hexdigest()[:16]


def merge_specs(*specs: Dict[str, Any]) -> Dict[str, Any]:
    """把多句话并成一个 spec —— 车间里没人只说一句。

    ops 顺序拼接(编译器本身对顺序不敏感:mean 项相加、bounds 取交、
    exclusions 取并),confidence 取最低的那句 —— 一组话里最没底的那句
    决定整组的分量,这比取平均更保守也更符合"称量"的语义。
    """
    ops: List[Dict[str, Any]] = []
    utters: List[str] = []
    confs: List[float] = []
    for sp in specs:
        if not sp:
            continue
        ops.extend(sp.get("ops") or _legacy_to_ops(sp))
        u = str(sp.get("utterance", "")).strip()
        if u:
            utters.append(u)
        c = sp.get("confidence")
        if c is not None:
            confs.append(float(c))
    out: Dict[str, Any] = {"utterance": "；".join(utters), "ops": ops}
    if confs:
        out["confidence"] = min(confs)
    return out


def shuffle_dims(spec: Dict[str, Any], space: ParamSpace, seed: int = 7) -> Dict[str, Any]:
    """消融实验:把翻译结果里的维度名打乱,其余不变。

    若 (b) LLM先验 ≫ (c) 打乱先验 ≈ (a) 无先验,说明翻译真的携带了信息,
    而不是"随便扰动一下先验都能加速"。
    """
    rng = random.Random(seed)
    perm = list(space.names)
    rng.shuffle(perm)
    mapping = dict(zip(space.names, perm))
    raw = json.dumps(spec, ensure_ascii=False)
    out = json.loads(raw)

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            new: Dict[str, Any] = {}
            for k, v in node.items():
                nk = mapping.get(k, k)
                nv = mapping.get(v, v) if isinstance(v, str) else walk(v)
                new[nk] = nv
            return new
        if isinstance(node, list):
            return [mapping.get(v, v) if isinstance(v, str) else walk(v) for v in node]
        return node

    out = walk(out)
    out["utterance"] = spec.get("utterance", "") + "(维度打乱·消融对照)"
    return out

