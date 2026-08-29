"""sandbox.py — 数字孪生沙盘(化成 / 压铸)。

响应面 = 机理定性规律(公开电化学/压铸知识)+ 公开数据标定锚点 + 批次噪声。
数值与前端 scenarios.js 逐点对齐(见 test_align.py),兜底切换时曲线不跳。

明确边界:这不是 P2D/Newman 电化学求解,也不是 CFD。它是机理启发的
超仿真响应面 —— 倒U型温度响应、倍率-SEI 权衡、低温×高倍率联合断崖。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from prior_dsl import ParamSpace


def hill(x: float, peak: float, w: float) -> float:
    return math.exp(-((x - peak) / w) ** 2)


def cl(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def lo_gate(v: float, thr: float, w: float) -> float:
    """v 明显低于 thr 时趋近 1。坡宽 w 而非硬跳变 —— 保证 EI 可微。"""
    return cl((thr - v) / w, 0.0, 1.0)


def hi_gate(v: float, thr: float, w: float) -> float:
    """v 明显高于 thr 时趋近 1。"""
    return cl((v - thr) / w, 0.0, 1.0)


def plating_onset(pre: float) -> float:
    """析锂起始温度(℃),随预充倍率上移。

    机理:析锂的判据是负极极化把电位压到 0 V vs Li/Li+ 以下,而极化同时
    随电流密度上升、随温度下降。所以"多少度会析锂"不是一个常数 ——
    0.05C 下要接近冰点才析,0.5C 下四十度出头就开始析。

    这条斜线是"低温别上倍率"这句口诀之所以是**联合**条件的原因:它不是
    两条独立的红线,是 (倍率, 温度) 平面上的一道斜边。也正因为是斜边,
    一句只说"低于30度别超0.2C"的话只能盖住它的一部分 —— 经验是对的,
    但不完备,剩下的边界得靠数据补。
    """
    return 24.0 + 36.0 * pre


def sei_quality(pre: float) -> float:
    """SEI 致密度(0..1),随预充电流密度对数下降。

    机理:成核密度随电流密度上升,高电流下 SEI 以多点快速生长、疏松多孔,
    消耗更多不可逆锂;低电流下成核点少、膜层致密均匀。经验上电流每翻一倍,
    不可逆锂损失就上一个台阶 —— 所以是对数关系,不是线性。

    关键是量程内**没有平台**:任何一次加倍都要付代价。原先的实现在 0.09C
    就撞到下限,于是 0.1C 和 0.5C 在沙盘里一样便宜 —— 那样"首充慢一点"
    这句话在半个量程上无事可做,而"1C 拉满省电费"这句歪经也几乎不受惩罚。
    响应面不实现的物理,经验卡就无从体现价值。
    """
    return cl(1.0 - 0.22 * math.log2(max(pre, 0.005) / 0.03), 0.0, 1.0)


# ==================================================================
# 核超参 θ 的来历(别再手写了)
# ==================================================================
# 下面两个场景的 theta 不是拍的,是按**留出预测密度**在网格上选的,并且要求
# 同时通过校准约束(|偏差|<0.35、覆盖率 80~97%)。审计脚本见 test_align.
# test_calibration —— 那一块就是这组数的验收面。
#
# 原先手写的 sf(1.2 / 1.1)是照着响应面的**全域** std 定的(2.74 / 4.67),
# 而那个 std 几乎全部来自 scrap_penalty 造成的断崖。可是 GP 只在可行域里学:
#     formation  可行域 std=0.567  全域 std=2.735
#     casting    可行域 std=0.368  全域 std=4.666
# 拿全域方差去标一个平稳核,等于把"撞墙那一下的落差"算进"平滑起伏"的预算里,
# σ 于是系统性偏宽 —— 实测 casting 的 90% 区间覆盖率是 100%、离散只有 0.58,
# 也就是 σ 比该有的宽了近一倍。σ 偏宽的后果不是"保守一点"而已:EI 的探索项
# 按 σ 计价,偏宽就是花真金白银的批次去买并不存在的不确定性。
#
# 这件事本身就是"校准"这个概念在项目里的第一笔收益:θ 手写了两天没人查,
# 一上审计就查出一处系统性偏宽。我们敢说"系统称量每句话",前提是秤准。
def mulberry32(seed: int) -> Callable[[], float]:
    """与前端同名同算法的 PRNG。同种子 → 同噪声序列。"""
    a = seed & 0xFFFFFFFF

    def nxt() -> float:
        nonlocal a
        a = (a + 0x6D2B79F5) & 0xFFFFFFFF
        t = a
        t = (t ^ (t >> 15)) * (1 | t) & 0xFFFFFFFF
        t = (t + ((t ^ (t >> 7)) * (61 | t) & 0xFFFFFFFF)) & 0xFFFFFFFF ^ t
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296.0

    return nxt


@dataclass
class Scene:
    """场景接口。BO 只通过这几个方法看沙盘,看不到内部机理。"""

    id: str
    name: str
    reward_name: str
    reward_unit: str
    reward_max: float
    theta: Dict[str, float]
    plane: List[int]
    params: List[Dict[str, Any]]
    red_lines: List[Dict[str, str]]
    money_per_batch: int
    settle_line: str
    _reward: Callable[["Scene", Sequence[float]], float]
    _feasible: Callable[["Scene", Sequence[float]], bool]
    _risks: Dict[str, Callable[["Scene", Sequence[float]], float]]
    base_start: List[List[float]]
    inj_start: List[List[float]]
    noise_sigma: float = 0.3
    # 越过红线的批次不是"分数低一点",是整托报废
    scrap_penalty: float = 6.0
    # 异方差:某些区域的读数本身就不可信(机理层面,不是先验层面)
    _noise_at: Optional[Callable[["Scene", Sequence[float]], float]] = None
    # 测量偏置:读数 - 真值。某些区域的读数不是"飘",是"假" —— 系统性偏高。
    # 这是 reward(上产线真拿到的) 与 observe(仪表读到的) 的分岔点,
    # 也是"静置不到位内阻是假的"这句话唯一的落点:光是噪声大会被平均掉,
    # 系统性偏高不会 —— 它会让你把一个虚高的配方当成最优解发出去。
    _bias_at: Optional[Callable[["Scene", Sequence[float]], float]] = None
    # 设备个体偏差:仪表读数 - 真值。这台机就是这样,不管有没有人知道。
    # recalibrate 先验的价值就在于抵消它。
    device_bias: List[float] = field(default_factory=list)

    def actual(self, x: Sequence[float]) -> List[float]:
        """把"设定读数"换算成"实际落到工件上的值"。"""
        if not self.device_bias:
            return list(x)
        return [float(x[i]) - self.device_bias[i] for i in range(len(x))]

    def space(self) -> ParamSpace:
        return ParamSpace.from_params(self.params)

    def reward(self, x: Sequence[float]) -> float:
        return self._reward(self, self.actual(x))

    def feasible(self, x: Sequence[float]) -> bool:
        return self._feasible(self, self.actual(x))

    def risk(self, key: str, x: Sequence[float]) -> float:
        fn = self._risks.get(key)
        return fn(self, self.actual(x)) if fn else 0.0

    def risks(self, x: Sequence[float]) -> Dict[str, float]:
        a = self.actual(x)
        return {k: fn(self, a) for k, fn in self._risks.items()}

    def observe(self, x: Sequence[float], rng: Callable[[], float]) -> float:
        """带批次噪声的观测。噪声由外部种子 RNG 驱动 → 可复现。

        越过红线照样返回一个数 —— 优化器不该免费拿到安全知识,它得自己撞、
        自己学。这也是"经验卡值钱"的前提:老师傅的话省掉的正是这几托废品。
        """
        u1 = max(1e-12, rng())
        u2 = rng()
        z = math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)
        a = self.actual(x)
        s = self.noise_sigma * (self._noise_at(self, a) if self._noise_at else 1.0)
        b = self._bias_at(self, a) if self._bias_at else 0.0
        y = self.reward(x) + b + s * z
        if not self.feasible(x):
            y -= self.scrap_penalty
        return y


# ==================================================================
# 场景 A — 磷酸铁锂 · 化成
# ==================================================================
def _f_li(s: Scene, x: Sequence[float]) -> float:
    """析锂风险 = 温度低于该倍率的析锂起始温度多少。

    单变量门,但门槛是倍率的函数 —— 所以它在 (倍率,温度) 平面上是一道斜边。
    """
    pre, T = x[0], x[1]
    return lo_gate(T, plating_onset(pre), 10.0)


def _f_feasible(s: Scene, x: Sequence[float]) -> bool:
    pre, T, _sw, _cut, age = x
    if _f_li(s, x) > 0.55:                      # 析锂墙(斜边)
        return False
    if T > 51.0 and age > 30.0:                 # 产气墙
        return False
    return True


def _f_gas(s: Scene, x: Sequence[float]) -> float:
    return cl((x[1] - 44.0) / 16.0, 0.0, 1.0) * cl(x[4] / 48.0, 0.0, 1.0)


def _f_bias(s: Scene, x: Sequence[float]) -> float:
    """老化不足 → 读数系统性偏高(不只是飘)。

    机理:SEI 未稳定、电解液浸润未完成时,首次放电容量里混进了尚未固化的
    可逆锂与浸润不均带来的虚高,测得的 FCE 比稳态值高。所以"内阻是假的"
    这句口诀的准确含义是**假高**,不是"读数不准"。

    这一项是 observe 与 reward 的分岔:优化器只看得到 observe。所以在这个
    区域里,它会一路挑到一批"读数最好但真值平庸"的配方,然后把它发出去 ——
    这正是老师傅那句话要拦住的事故,也是这张卡唯一的价值来源。
    """
    return 1.1 * cl((24.0 - x[4]) / 18.0, 0.0, 1.0)


def _f_noise(s: Scene, x: Sequence[float]) -> float:
    """老化不足 → SEI 未稳定、浸润未平衡,读数偏离稳态。

    机理层面的异方差。"静置不到位内阻是假的"这句话之所以值钱,是因为
    沙盘里那个区域的观测真的不可信 —— 不是我们为了让卡片生效而编的。
    """
    age = x[4]
    return 1.0 + 2.2 * cl((24.0 - age) / 18.0, 0.0, 1.0)


def _f_fce(s: Scene, x: Sequence[float]) -> float:
    """双潜变量结构:SEI质量(脆弱)驱动 FCE/DCR,活性锂存量(耐受)驱动容量。

    分层失效("先坏内阻,后坏容量")由此自然涌现,而非硬编码。
    """
    pre, T, sw, cut, age = x
    t_resp = hill(T, 42.0, 9.0)
    pre_resp = sei_quality(pre)
    sw_resp = hill(sw, 3.40, 0.25)
    cut_resp = cl(1.0 - (cut - 0.03) / 0.10, 0.7, 1.0)
    age_resp = cl(age / 30.0, 0.9, 1.0) * (0.9 if age < 8.0 else 1.0)
    # 预充倍率的权重必须压得住温度。机理上首充电流密度是化成阶段决定 SEI
    # 质量与不可逆锂损失的**首要**变量,温度是次要变量(它加速成膜动力学,
    # 但不改变"电流越大膜越疏松"这件事)。原先倍率权重 1.0、温度 1.7,等于
    # 让沙盘宣布"温度比倍率重要一倍" —— 那是反的。
    #
    # 后果不是抽象的:歪经卡"1C 拉满省电费"把倍率锁进 [0.40,0.50],而析锂
    # 起始温度随倍率上移(plating_onset),于是它被迫把温度顶到 38℃ 以上 ——
    # 正好撞进 42℃ 的温度甜区。白捡的温度增益 +1.33 点比 SEI 罚 -0.70 点还大,
    # 于是这句歪经在沙盘里**真的**赚钱。那不是评分表的错,是响应面写错了:
    # 我们不该靠调评分表把一句在自己物理里确实有利的话判成有害。
    # 权重 2.2 让倍率量程撬动 1.96 点、压过温度的 1.65 点,歪经净收益转负。
    fce = (
        89.4
        + 1.7 * (t_resp - 0.8)
        + 2.2 * (pre_resp - 0.6)
        + 0.5 * (sw_resp - 0.8)
        + 0.5 * (cut_resp - 0.85)
        + 0.4 * (age_resp - 0.95)
    )
    if _f_li(s, x) > 0.7:
        fce -= 8.0
    if _f_gas(s, x) > 0.6:
        fce -= 3.5
    return cl(fce, 79.0, 91.5)


FORMATION = Scene(
    id="formation",
    name="磷酸铁锂 · 化成",
    reward_name="首次库仑效率",
    reward_unit="%",
    reward_max=91.5,
    # θ 由留出预测密度选出(见文件头说明):lpd=-1.299,偏差 +0.21,覆盖 96%。
    # 原手写值 length=0.42 sf=1.2 —— length 偏长把响应面抹得太平,sf 偏大。
    theta={"length": 0.22, "sf": 1.0, "sn": 0.32},
    plane=[0, 1],
    params=[
        {"name": "预充倍率", "unit": "C", "lo": 0.02, "hi": 0.50, "decimals": 2},
        {"name": "化成温度", "unit": "℃", "lo": 25, "hi": 55, "decimals": 0},
        {"name": "预充切换电压", "unit": "V", "lo": 3.0, "hi": 3.45, "decimals": 2},
        {"name": "恒压截止电流", "unit": "C", "lo": 0.02, "hi": 0.10, "decimals": 3},
        {"name": "高温老化时长", "unit": "h", "lo": 6, "hi": 48, "decimals": 0},
    ],
    red_lines=[
        {"id": "li", "label": "析锂线", "desc": "低温 × 高倍率 → 锂沉积报废"},
        {"id": "gas", "label": "产气线", "desc": "高温副反应 → 鼓气"},
        {"id": "yield", "label": "首效达标线", "desc": "FCE ≥ 88.0% 才放行"},
    ],
    money_per_batch=80000,
    settle_line="一个批次 = 一托电芯 + 化成柜占用数天",
    _reward=_f_fce,
    _feasible=_f_feasible,
    _risks={"li": _f_li, "gas": _f_gas},
    _noise_at=_f_noise,
    _bias_at=_f_bias,
    # 基准田的第一批 = 新体系导入时的"教科书起点":室温、保守倍率、短老化。
    # 它离最优点很远,这正是没有经验时的真实处境。
    base_start=[[0.22, 30, 3.15, 0.08, 12]],
    inj_start=[[0.22, 30, 3.15, 0.08, 12]],
    noise_sigma=0.3,
)


# ==================================================================
# 场景 B — 压铸 · 开机废
# ==================================================================
def _c_feasible(s: Scene, x: Sequence[float]) -> bool:
    if x[2] > 2.5:                              # 卷气硬墙
        return False
    if x[0] < 615.0 and x[1] < 190.0:           # 冷隔
        return False
    return True


def _c_gas(s: Scene, x: Sequence[float]) -> float:
    return cl((x[2] - 2.4) / 0.3, 0.0, 1.0)


def _c_cold(s: Scene, x: Sequence[float]) -> float:
    """冷隔风险 = 熔料温度与模具温度**同时**偏低的程度。

    机理:冷隔是熔体前锋在汇合前就失去流动性。散热由模具带走、热量由熔体
    带来,所以只有两边同时不足才成型不良 —— 熔温够高时低模温只是表面质量
    问题,模温够高时低熔温也还能补。这就是为什么它是两个"低门"的乘积,
    而不是任何单变量阈值。
    """
    return lo_gate(x[0], 618.0, 6.0) * lo_gate(x[1], 195.0, 12.0)


def _c_yield(s: Scene, x: Sequence[float]) -> float:
    Tm, Td, v, sw, tp = x
    tm = hill(Tm, 635.0, 12.0)
    td = hill(Td, 215.0, 25.0)
    v_r = cl(1.0 - max(0.0, v - 2.2) / 0.6, 0.7, 1.0)
    sw_r = hill(sw, 180.0, 50.0)
    tp_r = cl(tp / 6.0, 0.85, 1.0)
    y = (
        96.5
        + 0.9 * (tm - 0.75)
        + 0.7 * (td - 0.7)
        + 1.0 * (v_r - 0.5)
        + 0.4 * (sw_r - 0.8)
        + 0.3 * (tp_r - 0.9)
    )
    if _c_gas(s, x) > 0.75:
        y -= 9.0
    if _c_cold(s, x) > 0.7:
        y -= 7.0
    return cl(y, 82.0, 98.5)


CASTING = Scene(
    id="casting",
    name="压铸 · 开机",
    reward_name="良品率",
    reward_unit="%",
    reward_max=98.5,
    # 同上:lpd=-1.025,偏差 +0.30,覆盖 94%。原手写 sf=1.1 让 σ 宽了近一倍
    # (覆盖率 100%、离散 0.58),EI 的探索项因此在买不存在的不确定性。
    theta={"length": 0.35, "sf": 0.8, "sn": 0.28},
    plane=[1, 2],
    params=[
        {"name": "熔料温度", "unit": "℃", "lo": 610, "hi": 650, "decimals": 0},
        {"name": "模具温度", "unit": "℃", "lo": 180, "hi": 240, "decimals": 0},
        {"name": "快速压射速度", "unit": "m/s", "lo": 1.5, "hi": 4.0, "decimals": 2},
        {"name": "压射切换点", "unit": "mm", "lo": 120, "hi": 220, "decimals": 0},
        {"name": "保压时间", "unit": "s", "lo": 2, "hi": 10, "decimals": 1},
    ],
    red_lines=[
        {"id": "li", "label": "冷隔线", "desc": "熔/模温双低 → 冷隔"},
        {"id": "gas", "label": "卷气线", "desc": "快压超 2.5 m/s → 卷气"},
        {"id": "yield", "label": "良率达标线", "desc": "≥ 96% 才放行"},
    ],
    money_per_batch=6000,
    settle_line="一模次 = 一台机的料 + 能耗 + 开机废件",
    _reward=_c_yield,
    _feasible=_c_feasible,
    _risks={"li": _c_cold, "gas": _c_gas},
    # 同理:早班开机的保守起点(低模温 + 偏快压射),离良率峰很远
    base_start=[[618, 188, 2.35, 140, 3]],
    inj_start=[[618, 188, 2.35, 140, 3]],
    noise_sigma=0.25,
    # 这台机的模温表偏高 3℃:设定 200 实际只有 197。
    # "我们那台机模温表偏高" 这句话的价值,就是把这 3℃ 找回来。
    device_bias=[0.0, 3.0, 0.0, 0.0, 0.0],
)

SCENES: Dict[str, Scene] = {"formation": FORMATION, "casting": CASTING}


def get_scene(scene_id: str) -> Scene:
    if scene_id not in SCENES:
        raise KeyError(f"未知场景 {scene_id!r}")
    return SCENES[scene_id]


def scene_meta(s: Scene) -> Dict[str, Any]:
    """给前端的场景描述(不含机理实现)。"""
    return {
        "id": s.id,
        "name": s.name,
        "reward_name": s.reward_name,
        "reward_unit": s.reward_unit,
        "reward_max": s.reward_max,
        "plane": s.plane,
        "params": s.params,
        "red_lines": s.red_lines,
        "money_per_batch": s.money_per_batch,
        "settle_line": s.settle_line,
        "noise_sigma": s.noise_sigma,
    }

