"""bo.py — numpy 手写 GP(RBF + Cholesky)+ 受约束 EI。

不用 sklearn 的 GP,因为要注入自定义 prior mean、异方差噪声、每维长度尺度
倍率和伪观测 —— 这四件事正是 PriorSpec 的落点。

先验进入 GP 的两个位置(与前端 engine.js 一致):
    拟合:  y_res = y - m(X)
    预测:  mu(x) = m(x) + k(x)^T (K + Σ)^-1 y_res
所以在采样过的点上,单次观测就消掉先验误差的 sf^2/(sf^2+sn^2) ≈ 92%。
先验不锁死答案,它只分配试验预算 —— 这是歪经卡能自愈的数学理由。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from prior_dsl import CompiledPrior, ParamSpace
from sandbox import Scene, mulberry32

SQRT_2PI = 2.5066282746310002   # 前端 Math.SQRT2PI 不存在,这里写死常数

# EI 的"尘埃线"。低于它的 EI 一律当 0 —— 见 run_bo 里挑点那一段。
# 与 engine.js 的 EI_DUST 必须是同一个数:它决定"哪些挑点交给次级判据",
# 两边取值不同就等于两边在不同的批次上分叉,比不设这条线还糟。
EI_DUST = 1e-12


def _phi_cdf(z: np.ndarray) -> np.ndarray:
    return 0.5 * (1.0 + np.vectorize(math.erf)(z / math.sqrt(2.0)))


def _phi_pdf(z: np.ndarray) -> np.ndarray:
    return np.exp(-0.5 * z * z) / SQRT_2PI


class GP:
    """精确 GP。prior mean 在拟合时减掉、预测时加回;噪声可按点变化。"""

    def __init__(
        self,
        space: ParamSpace,
        theta: Dict[str, float],
        mean_fn: Optional[Callable[[Sequence[float]], float]] = None,
        ls_scale: Optional[Sequence[float]] = None,
        noise_fn: Optional[Callable[[Sequence[float]], float]] = None,
    ) -> None:
        self.space = space
        self.d = len(space)
        self.sf = float(theta.get("sf", 1.2))
        self.sn = float(theta.get("sn", 0.28))
        self.length = float(theta.get("length", 0.42))
        self.mean_fn = mean_fn or (lambda x: 0.0)
        self.ls = np.array(
            [self.length * (ls_scale[i] if ls_scale else 1.0) for i in range(self.d)],
            dtype=float,
        )
        self.noise_fn = noise_fn or (lambda x: self.sn)
        self.ranges = np.array([space.span(i) for i in range(self.d)], dtype=float)
        self.X = np.zeros((0, self.d))
        self.y_raw = np.zeros(0)
        self.noise = np.zeros(0)
        self.L: Optional[np.ndarray] = None
        self.alpha: Optional[np.ndarray] = None
        # 中心化常数。目标值活在 ~90 的量级,核幅值 sf 只有 1.2;若不中心化,
        # 未探索区的 mu 会回落到 0,相对 f_best≈90 显得极差 → EI 全域归零,
        # 优化器彻底停止探索。这是必须减掉的常数基线。
        self.y0: Optional[float] = None

    def _k(self, A: np.ndarray, B: np.ndarray) -> np.ndarray:
        """ARD-RBF,按每维量程归一化后再除以该维长度尺度。"""
        a = A / (self.ranges * self.ls)
        b = B / (self.ranges * self.ls)
        d2 = (
            np.sum(a * a, axis=1)[:, None]
            + np.sum(b * b, axis=1)[None, :]
            - 2.0 * a @ b.T
        )
        return self.sf * self.sf * np.exp(-0.5 * np.maximum(d2, 0.0))

    def _base(self, x: Sequence[float]) -> float:
        """完整先验均值 = 中心化常数 + 经验先验 m(x)。"""
        return (self.y0 or 0.0) + self.mean_fn(x)

    def add(self, x: Sequence[float], y: float, noise_inflate: float = 1.0) -> None:
        xa = np.asarray(x, dtype=float)
        if self.y0 is None:
            self.y0 = float(y)          # 第一批就是天然的中心化基线
        self.X = np.vstack([self.X, xa])
        self.y_raw = np.append(self.y_raw, y)
        self.noise = np.append(self.noise, self.noise_fn(xa) * noise_inflate)
        self._refit()

    def _refit(self) -> None:
        n = len(self.X)
        res = self.y_raw - np.array([self._base(r) for r in self.X])
        K = self._k(self.X, self.X) + np.diag(self.noise ** 2) + 1e-8 * np.eye(n)
        try:
            self.L = np.linalg.cholesky(K)
        except np.linalg.LinAlgError:
            self.L = np.linalg.cholesky(K + 1e-4 * np.eye(n))
        self.alpha = np.linalg.solve(self.L.T, np.linalg.solve(self.L, res))

    def predict(self, Xs: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        Xs = np.atleast_2d(np.asarray(Xs, dtype=float))
        m = np.array([self._base(row) for row in Xs])
        if self.L is None or len(self.X) == 0:
            return m, np.full(len(Xs), self.sf)
        Ks = self._k(self.X, Xs)
        mu = m + Ks.T @ self.alpha
        v = np.linalg.solve(self.L, Ks)
        var = self.sf * self.sf - np.sum(v * v, axis=0)
        return mu, np.sqrt(np.maximum(var, 1e-9))

    def best_seen(self) -> Tuple[Optional[np.ndarray], float]:
        """去噪 incumbent = 已观测点上的后验均值最大值。

        用 argmax(y_raw) 当 EI 的门槛有个隐患:某一批读数虚高(异方差抖动
        或系统性偏置)会把门槛顶到天上 → EI 全域塌陷 → 优化器提前"收敛"在
        一个幻影上,还把这个幻影发出去。改用后验均值:GP 已按每点噪声做过
        收缩,凡是被声明过"这儿读数不可信"的批次自然被压回先验附近。

        这也是"静置不到位内阻是假的"这句话真正的落点 —— 它保护的不是
        搜索路径,是**门槛的诚实**。
        """
        if len(self.X) == 0:
            return None, -math.inf
        mu, _ = self.predict(self.X)
        i = int(np.argmax(mu))
        return self.X[i], float(mu[i])


@dataclass
class BOResult:
    history: List[Dict[str, Any]] = field(default_factory=list)
    best_y: float = -math.inf
    best_x: Optional[List[float]] = None
    n_batches: int = 0
    stopped_by: str = "cap"
    prior_survival: Optional[float] = None
    scrapped: int = 0          # 撞了红线的批次数(整托报废)
    # 按成因分账的报废数。用来回答"这张卡该为哪几托负责":一句只讲冷隔的话,
    # 不该因为卷气废品的涨落被判成有害 —— 它一个字也没提卷气。
    scrap_by_cause: Dict[str, int] = field(default_factory=dict)
    # 推荐点的真实价值(无噪声)。这才是"上产线会拿到什么"的诚实度量:
    # best_y 是带噪观测,运气好的一次读数不等于找到了好参数。
    true_best: float = -math.inf


def _pool(
    bounds: Sequence[Tuple[float, float]],
    keep: Callable[[Sequence[float]], bool],
    seed: int,
    n: int = 220,
) -> List[List[float]]:
    """候选池。

    注意 keep 只施加"先验声明的"硬剪除,不施加沙盘真实可行域 ——
    沙盘的红线是物理事实,优化器事先并不知道,必须自己撞出来。
    否则安全类经验卡毫无价值,整个价值故事就是循环论证。
    """
    out: List[List[float]] = []
    for i in range(n):
        rng = mulberry32(seed + i * 131)
        x = [lo + rng() * (hi - lo) for (lo, hi) in bounds]
        if keep(x):
            out.append(x)
    if len(out) < 12:   # 先验把域剪得极窄:放宽到 bounds-only,靠软惩罚兜
        for i in range(n):
            rng = mulberry32(seed + 99991 + i * 137)
            out.append([lo + rng() * (hi - lo) for (lo, hi) in bounds])
    return out


def run_bo(
    scene: Scene,
    prior: Optional[CompiledPrior] = None,
    seed: int = 20260829,
    max_iters: int = 24,
    start_points: Optional[Sequence[Sequence[float]]] = None,
    stagnation_limit: int = 4,
) -> BOResult:
    """一次完整寻优。每轮暴露 mu/sigma/ei/best_so_far,供慢演模式与孪生视窗使用。"""
    space = scene.space()
    d = len(space)
    scene_feas = scene.feasible                     # 物理事实,优化器看不到
    prior_feas = prior.feasible_fn() if prior else (lambda x: True)

    # 坐标校准:先验说"这台表偏高 3 度",于是把 GP 的坐标系搬正。
    # 校准正确 → GP 学到的响应面落在真值坐标上,泛化更准;校准错了 → 反受其害。
    #
    # 两套坐标必须分清,否则校准这句话会白说:
    #   旋钮坐标 x —— 人在机台上设的那个数,也是唯一能真正下达的指令
    #   工件坐标 to_model(x) = x + recal —— 实际落到工件上的值
    # 先验声明的一切(box / 硬剪除 / 均值项)说的都是**工件**:"模温托住 200
    # 以上"讲的是熔体真感受到的温度,不是表盘上的字。
    recal = list(prior.recal) if prior else [0.0] * d
    has_recal = any(abs(v) > 1e-12 for v in recal)

    def to_model(x: Sequence[float]) -> List[float]:
        return [x[i] + recal[i] for i in range(d)] if has_recal else list(x)

    # 把工件坐标下声明的 box 换算成旋钮坐标,再与机台量程求交 —— 旋钮拧不出
    # 量程外的数,这是物理约束。表偏高 3℃ 时,"实际 200 以上"意味着表要打到
    # 203,于是可达的实际区间从 (200,240) 缩成 (200,237):校准的代价是诚实的,
    # 不换算才是把这 3℃ 白扔了 —— 那样"模温托住 200"实际只托到 197,
    # 而 197 正落在冷隔那一侧。校准是乘数不是加数,乘的就是这里。
    #
    # 只换算**声明过的**边:默认 box 是机台量程,说的本来就是旋钮,不是工件 ——
    # 把它也搬走会让校准卡单打不再是恒等变换,凭空剪掉一条量程。
    declared = list(prior.bounds) if prior else space.full_bounds()
    full = space.full_bounds()
    bounds: List[Tuple[float, float]] = []
    for i in range(d):
        lo, hi = declared[i]
        flo, fhi = full[i]
        if abs(lo - flo) > 1e-12:
            lo = max(lo - recal[i], flo)
        if abs(hi - fhi) > 1e-12:
            hi = min(hi - recal[i], fhi)
        if lo > hi:                     # 换算后落到量程外:退回机台能给的那一段
            lo = hi = max(flo, min(fhi, lo))
        bounds.append((lo, hi))

    def allowed(x: Sequence[float]) -> bool:
        """优化器唯一的准入判据:先验声明的硬剪除 + box。物理红线不在此列。

        box 在旋钮坐标下比对(那是能设的数),硬剪除在工件坐标下比对
        (那是话里说的事)。
        """
        for i, (lo, hi) in enumerate(bounds):
            if not (lo - 1e-9 <= x[i] <= hi + 1e-9):
                return False
        return prior_feas(to_model(x))

    mean_fn = prior.mean_fn() if prior else (lambda x: 0.0)
    noise_fn = prior.noise_fn(scene.theta["sn"]) if prior else (lambda x: scene.theta["sn"])
    cost_fn = prior.cost_fn() if prior else (lambda x: 1.0)
    ls_scale = prior.ls_scale if prior else None
    acq_cfg = dict(prior.acq) if prior and prior.acq else {}
    kappa = float(acq_cfg.get("kappa", 1.0))

    gp = GP(space, scene.theta, mean_fn, ls_scale, noise_fn)
    obs_rng = mulberry32(seed)
    res = BOResult()
    visited: set = set()

    def key_of(x: Sequence[float]) -> str:
        return ",".join(f"{v:.4f}" for v in x)

    def observe(x: Sequence[float], tag: str) -> None:
        # x 是"设定读数";GP 在校准后的坐标系里学习
        xm = to_model(x)
        mu_pre, sig_pre = gp.predict(np.array([xm]))
        y = scene.observe(x, obs_rng)
        gp.add(xm, y)
        visited.add(key_of(x))
        if y > res.best_y:
            res.best_y = y
            res.best_x = list(x)
        res.history.append({
            "i": len(res.history),
            "x": [float(v) for v in x],
            "y": float(y),
            "mu": float(mu_pre[0]),
            "sigma": float(sig_pre[0]),
            "prior": float(mean_fn(xm)),   # 与 GP 同坐标系,否则审计轨迹和数学对不上
            "best_so_far": float(res.best_y),
            "feasible": bool(scene_feas(x)),     # 事后才知道这批是不是废了
            "risks": {k: round(float(v), 4) for k, v in scene.risks(x).items()},
            "tag": tag,
        })
        if not scene_feas(x):
            res.scrapped += 1
            for k, v in scene.risks(x).items():
                if v > 0.5:
                    res.scrap_by_cause[k] = res.scrap_by_cause.get(k, 0) + 1

    # 伪观测(口述记忆)先进,噪声放大
    if prior:
        for px, py, inflate in prior.pseudo_obs:
            if allowed(px):
                gp.add(to_model(px), py, noise_inflate=inflate)

    for sp_ in (start_points or []):
        x = [max(bounds[i][0], min(bounds[i][1], sp_[i])) for i in range(len(space))]
        if allowed(x):
            observe(x, "warm")
    if not res.history:
        centre = [(lo + hi) / 2 for lo, hi in bounds]
        if allowed(centre):
            observe(centre, "warm")
        else:
            for c in _pool(bounds, allowed, seed + 5, 60):
                observe(c, "warm")
                break

    pool = _pool(bounds, allowed, seed + 777)
    prev_best, stagnation = -math.inf, 0

    for _ in range(max_iters - len(res.history)):
        cands = [c for c in pool if key_of(c) not in visited]
        if not cands:
            res.stopped_by = "pool_exhausted"
            break
        Xc = np.array([to_model(c) for c in cands], dtype=float)
        mu, sigma = gp.predict(Xc)
        _, f_best = gp.best_seen()
        imp = mu - f_best
        z = np.divide(imp, sigma, out=np.zeros_like(imp), where=sigma > 1e-9)
        costs = np.array([cost_fn(c) for c in cands])
        # 探索项按"一批真能买到多少"折价。纯 EI 有个反直觉的病:声明某区域
        # "读数不可信"会让该处后验方差永久偏高,而 EI 奖励方差 → 越说不准
        # 越往那儿跑,恰好和"静置不到位内阻是假的"的本意相反。
        #
        # 但折价只该打在**探索项**上:σ·φ(z) 卖的是"测一批能消掉多少不确定性",
        # 而一批观测实际消掉的只有 σ²/(σ²+σ_n²) —— 这正是 GP 自己的收缩因子,
        # 不是自由参数。噪声区里剩下的方差你出钱也买不走,所以那部分不该计价。
        # 真实的期望改进项 imp·Φ(z) 不打折:读数吵不代表那儿的真值不好。
        #
        # 早先按 1/k² 折**整个** EI 是过度翻译:strong 噪声下等于 9 倍压制,
        # 把一句 confidence 0.85 的话执行成了禁令,还把批次挤到高温长时的
        # 产气墙上 —— 而那句话一个字也没提产气。收缩因子天然落在 [0,1),
        # 永远压不成禁令,这才配得上原话的语气。
        # 只用"先验声明的"噪声 —— 沙盘真实噪声优化器看不见,不能作弊。
        sn_dec = np.array([noise_fn(x) for x in Xc])
        var = sigma ** 2
        harvest = var / np.maximum(var + sn_dec ** 2, 1e-12)
        ei = imp * _phi_cdf(z) + kappa * sigma * _phi_pdf(z) * harvest
        ei = np.maximum(ei, 0.0)
        ei_eff = ei / np.maximum(costs, 1e-6)
        # 尘埃区:整池 EI 全塌到 EI_DUST 以下时,argmax 挑的其实是浮点尾数。
        # 实测 1571 次挑点里 7.0% 落在这个区间,而两种语言的尾数永远不会一致
        # —— formation/wrong 就在第 4 批分叉:Python 跑 14 批、JS 跑 10 批,
        # 同一张卡两个引擎给出不同的账(−4.16 vs −4.11)。这不是精度问题,是
        # **判据在这个区间里没有定义**:EI 说"哪儿都没得赚"时,它对"去哪儿"
        # 是沉默的,而代码却硬要从沉默里读出一个答案。
        #
        # 所以把尘埃压成 0(承认 EI 在这儿无话可说),再用一条有量纲、有量级
        # 的次级判据接手:σ²/(σ²+σn²)·σ/成本 —— 每花一个批次能消掉多少不确定
        # 性。取值差是 O(1e-2) 级,两种语言比得出同一个赢家;语义上也正是 EI
        # 沉默时该做的事:不赌收益了,那就买信息。
        # 与 engine.js runBO 里挑点那一段逐字对应 —— 一份判据,两处解释。
        key = np.where(ei_eff > EI_DUST, ei_eff, 0.0)
        alt = sigma * harvest / np.maximum(costs, 1e-6)
        # 字典序取最大:先比 key,平手再比 alt,**再平手取池中最先出现的那个**。
        # 第三级不是凑数 —— alt 自己也会饱和:第一批过后,离已观测点足够远的候选
        # σ 全等于先验方差,harvest 和 cost 又同值,于是几百个候选的 alt 一模一样。
        # 这时"取第几个"就成了唯一的判据,而 np.lexsort 取**末位**、JS 的 `>` 取
        # **首位** —— 两边必然分叉(实测 formation 基准田 8 个种子里 3 个第 1 批
        # 就分道扬镳)。所以判据必须是**全序**的:Python 的 max 与 JS 的严格大于
        # 都保留首次出现者,池顺序两边又逐点一致,这才真正定死。
        j = max(range(len(cands)), key=lambda i: (key[i], alt[i]))
        chosen = cands[j]
        observe(chosen, "bo")
        res.history[-1]["ei"] = float(ei[j])
        res.history[-1]["cost"] = float(costs[j])

        if res.best_y > prev_best + 1e-6:
            prev_best, stagnation = res.best_y, 0
        else:
            stagnation += 1
            if stagnation >= stagnation_limit:
                res.stopped_by = "converged"
                break

    res.n_batches = len(res.history)
    for h in res.history:
        h.setdefault("ei", 0.0)
        h.setdefault("cost", 1.0)

    # 推荐点 = GP 后验均值最高的已观测点(去噪),再看它的真实值。
    # 不用 argmax(y):那等于把"读数运气最好的一批"当成推荐,是自欺。
    if res.history:
        Xs = np.array([to_model(h["x"]) for h in res.history])
        mu_all, _ = gp.predict(Xs)
        j = int(np.argmax(mu_all))
        res.best_x = list(res.history[j]["x"])
        rec = res.best_x
        res.true_best = scene.reward(rec) - (scene.scrap_penalty if not scene_feas(rec) else 0.0)

    # 先验存活度:先验均值 vs 拟合后验的相关系数 —— "系统称量每句话"的定量版
    if prior and prior.mean_terms and res.n_batches >= 3:
        X = np.array([to_model(h["x"]) for h in res.history])
        pri = np.array([mean_fn(r) for r in X])
        post, _ = gp.predict(X)
        if np.std(pri) > 1e-9 and np.std(post) > 1e-9:
            res.prior_survival = float(np.corrcoef(pri, post)[0, 1])
    return res


def _declared_edges(
    scene: Scene, prior: Optional[CompiledPrior]
) -> List[Tuple[int, float, float]]:
    """这句话在哪几维上声明了区间,声明的是什么(工件坐标)。

    只取真被收窄过的边 —— 没说的维度不该参与评判,那是别的话的活。
    """
    if prior is None:
        return []
    full = scene.space().full_bounds()
    out: List[Tuple[int, float, float]] = []
    for i, (lo, hi) in enumerate(prior.bounds):
        flo, fhi = full[i]
        if abs(lo - flo) > 1e-12 or abs(hi - fhi) > 1e-12:
            out.append((i, lo, hi))
    return out


def _compliance(
    scene: Scene, r: BOResult, edges: Sequence[Tuple[int, float, float]]
) -> float:
    """这一田里,实际落到工件上的值有多大比例满足这句话声明的区间(0..1)。

    量的是**工件**不是旋钮:一句"模温托住 200 以上"讲的是熔体真感受到的
    温度。表偏高 3℃ 时,旋钮打到 200 只有 197 落到工件上 —— 声明没被执行,
    而批次数、废品数、最终良率全都看不出这件事。校准类经验就活在这条轴上。

    **只对注入田有意义。** 拿基准田的合规率当对照是个陷阱:基准田从来没被
    告知过这句话,它"违规"是天经地义的,不是损失。按对照算的话,任何一张
    收窄搜索域的卡都能白拿一笔 —— 实测"1C 拉满省电费"这句歪经就靠这个
    拿到 +1.63 分,它做的全部事情只是把倍率箱子收进析锂墙里。
    所以这条轴的正确读法是**折扣而非增益**:说了没做到,扣分;做到了,不加分。
    """
    if not edges or not r.history:
        return 1.0
    hit = 0
    for h in r.history:
        a = scene.actual(h["x"])
        if all(lo - 1e-9 <= a[i] <= hi + 1e-9 for i, lo, hi in edges):
            hit += 1
    return hit / len(r.history)


def eval_prior(
    scene: Scene,
    prior: Optional[CompiledPrior],
    n_seeds: int = 12,
    seed0: int = 20260829,
    base_iters: int = 24,
    inj_iters: int = 20,
) -> Dict[str, float]:
    """多种子平均。单种子对比是运气,期望差距才是价值。

    舞台上用固定种子(可复现回放),验收用种子扫描 —— 两件事不冲突。

    四条**增益**轴 + 一条**折扣**轴,一句话通常只作用在其中一两条上:
        saved_batches    少花几个批次          (增益,双田对照)
        saved_scrap      少报废几托            (增益,双田对照)
        gain_best        最终答案的真实值更高  (增益,双田对照)
        gain_honesty     发出去的那个数更接近产线真值,少虚高(增益,双田对照)
        inj_compliance   这句话声明的区间在工件上真被执行到的比例(折扣,只看注入田)
    只看前三条会漏掉两整类经验:"这个读数不可信"既不省批次也不省废品,
    它省的是把一个虚高结果发到产线上的那次事故;"那台表偏高三度"既不改
    响应面也不改搜索域,它保证的是"你以为设了 200,工件真拿到 200"。
    每加一条轴都不是为了让某张卡及格,而是因为漏了它就等于用评分表宣布
    那一类经验没用 —— 而那恰恰是老师傅最常说的两类话。

    但**第五条轴不能按双田之差算**,这是我们自己踩过的坑:基准田从来没被
    告知过这句话,它"违规"是天经地义的,拿它当对照等于给任何一张收窄搜索
    域的卡白送一笔分。实测"1C 拉满省电费"这句歪经就靠 gain_compliance
    白拿 +1.63 —— 它做的全部事情只是把倍率箱子收进析锂墙里。
    所以保真度按**折扣**用:说了没做到才扣分,做到了不加分。
    base_compliance 仍然返回(答辩时"本来有多少批合规"是个有用的旁证),
    但它不进综合分。
    """
    sb = si = fb = fi = wb = wi = hb = hi = cb = ci = 0.0
    cause_b: Dict[str, float] = {}
    cause_i: Dict[str, float] = {}
    # 这句话声明过的区间(工件坐标)。两田都拿它当尺子 —— 尺子必须同一把。
    declared_bounds = _declared_edges(scene, prior)
    for k in range(n_seeds):
        s = seed0 + k * 7919
        b = run_bo(scene, None, s, base_iters, scene.base_start)
        i = run_bo(scene, prior, s, inj_iters, scene.inj_start)
        sb += b.n_batches
        si += i.n_batches
        wb += b.scrapped
        wi += i.scrapped
        for acc, r in ((cause_b, b), (cause_i, i)):
            for key in scene._risks:
                acc[key] = acc.get(key, 0.0) + r.scrap_by_cause.get(key, 0)
        fb += b.true_best      # 用去噪后的真实值,不用运气最好的那次读数
        fi += i.true_best
        # 交付诚实度 = 报告值 - 真值。报告值是要发出去的那个数,真值是产线
        # 真拿到的。这个差就是"虚高"的量,越小越好 —— 观测可信度类的经验
        # (如"静置不到位内阻是假的")只作用在这条轴上,批次和废品都体现不了。
        hb += max(h["best_so_far"] for h in b.history) - b.true_best
        hi += max(h["best_so_far"] for h in i.history) - i.true_best
        # 执行保真度 = 真落到工件上的值有多大比例满足这句话声明的区间。
        # 校准类经验("那台表偏高三度")只作用在这条轴上 —— 它不改响应面、
        # 不改搜索域,只保证"你以为设了 200,工件真拿到 200"。
        # 注意:注入田那个数才是判据(说了有没有做到);基准田那个数只是旁证
        # (本来碰巧有多少批合规),不能拿来做差 —— 见本函数 docstring。
        cb += _compliance(scene, b, declared_bounds)
        ci += _compliance(scene, i, declared_bounds)
    n = float(n_seeds)
    return {
        "n_seeds": n_seeds,
        "base_batches": sb / n, "inj_batches": si / n,
        "base_scrap": wb / n, "inj_scrap": wi / n,
        "base_best": fb / n, "inj_best": fi / n,
        "base_overclaim": hb / n, "inj_overclaim": hi / n,
        "base_compliance": cb / n, "inj_compliance": ci / n,
        "saved_batches": (sb - si) / n,
        "saved_scrap": (wb - wi) / n,
        "gain_best": (fi - fb) / n,
        "gain_honesty": (hb - hi) / n,
        # 保留但不再进综合分:它是"双田之差"的读法,而基准田没被告知过这句话,
        # 差值会给任何收窄搜索域的卡白送分。留着是因为答辩时有人会问"基准田
        # 本来合规率多少" —— 那时它是旁证。评分请用 inj_compliance 做折扣。
        "gain_compliance": (ci - cb) / n,
        "saved_scrap_by_cause": {
            key: (cause_b.get(key, 0.0) - cause_i.get(key, 0.0)) / n
            for key in scene._risks
        },
    }


def run_pair(
    scene: Scene,
    prior: Optional[CompiledPrior],
    seed: int = 20260829,
    base_iters: int = 24,
    inj_iters: int = 16,
) -> Dict[str, Any]:
    """双田对照。同一 oracle、同一优化器、同一噪声种子,唯一差异是先验。"""
    base = run_bo(scene, None, seed, base_iters, scene.base_start)
    inj = run_bo(scene, prior, seed, inj_iters, scene.inj_start)

    target = base.best_y - 0.15   # 达到基准最优(容差内)所需批次
    def first_reach(r: BOResult) -> Optional[int]:
        for h in r.history:
            if h["best_so_far"] >= target:
                return h["i"] + 1
        return None

    b_reach, i_reach = first_reach(base), first_reach(inj)
    saved = (b_reach - i_reach) if (b_reach and i_reach) else (base.n_batches - inj.n_batches)

    saved_scrap = base.scrapped - inj.scrapped
    return {
        "baseline": {
            "history": base.history,
            "n_batches": base.n_batches,
            "best_y": base.best_y,
            "best_x": base.best_x,
            "reach": b_reach,
            "scrapped": base.scrapped,
            "stopped_by": base.stopped_by,
        },
        "injected": {
            "history": inj.history,
            "n_batches": inj.n_batches,
            "best_y": inj.best_y,
            "best_x": inj.best_x,
            "reach": i_reach,
            "scrapped": inj.scrapped,
            "stopped_by": inj.stopped_by,
            "prior_survival": inj.prior_survival,
        },
        "saved_batches": int(saved),
        "saved_scrap": int(saved_scrap),
        "money": max(0, int(saved)) * scene.money_per_batch,
        "target": target,
    }
