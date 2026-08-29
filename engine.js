/* engine.js — 兜底引擎。与 server/bo.py 同一套数学:RBF-GP + 受约束 EI。
 *
 * 它不是"简化版",是**同一个模型的第二次实现**。断网时前端独立跑,曲线不能
 * 跳形、结论不能反 —— 所以下面这几件事必须和 Python 端逐字对应:
 *   1) 中心化常数 y0:目标值在 ~90 量级而核幅值 sf=1.0。不减掉这个常数,
 *      未探索区的 mu 会回落到 0,相对 f_best≈90 显得极差 → EI 全域归零,
 *      优化器彻底停止探索。这是必须的,不是调参。
 *   2) 观测走 scenario.observe():带批次噪声、带测量偏置、越红线扣整托报废。
 *      原先直接读 reward()(无噪声真值)会让"静置不到位内阻是假的"和所有
 *      安全类口诀在兜底模式里失去全部价值 —— 废品恒 0、虚高恒 0。
 *   3) 候选池只按**先验声明的**域筛,不按 scenario.feasible() 筛。沙盘的红线
 *      是物理事实,优化器事先并不知道,必须自己撞出来。否则安全类经验卡毫无
 *      价值,整个价值故事变成循环论证。
 *   4) incumbent 用后验均值,不用 argmax(y):某一批读数虚高会把 EI 门槛顶到
 *      天上,让优化器提前"收敛"在一个幻影上,还把这个幻影发出去。
 *   5) σ² = sf² − kᵀK⁻¹k,不加 sn²。σ 是**函数值**的不确定度;把观测噪声也
 *      算进去会让 σ 系统性偏宽,而 EI 的探索项按 σ 计价 —— 等于花批次去买
 *      并不存在的不确定性。
 * 确定性:同种子逐位可复现。Vanilla ES,file:// 可直开。
 */
'use strict';

// Math.SQRT2PI 不存在。原先 normpdf 除以 undefined 恒返回 NaN,于是 EI 只剩
// 期望改进项、探索项整个失效 —— 一个静默了很久的"半个采集函数"。
const SQRT_2PI = 2.5066282746310002;

/* ---------------- IR 解释器 ----------------
 * 先验不在这边编译,只在这边**求值**。编译(校验、红线族授权、体积上限、
 * 降级)全在 server/prior_dsl.py,产物是 cache/priors_ir.json 里的纯数据。
 *
 * 为什么这么分:原先 scenarios.js 手写了第二份卡片先验,和后端编译出来的那份
 * 悄悄分叉了 —— 同一张歪经卡,后端综合分 -3.76(否决),前端却因为「倍率箱子
 * 收窄」白省 2.00 批,成了全场最省批次的卡。同一句话两个引擎给相反的结论,
 * 这在答辩现场是致命的。补第二份手写代码只会再分叉一次。
 *
 * 所以这里刻意**只有算术,没有判断**:JS 没有权限决定一条硬剪能不能生效,
 * 也就没有地方长出第二套语义。下面每个函数都是 prior_dsl.py 里 _ir_*_eval 的
 * 逐字对译,改一边必须改另一边(irSelfTest 会当场抓到不一致)。
 */
function irSigmoid(z) {
  if (z < -40) return 0;
  if (z > 40) return 1;
  return 1 / (1 + Math.exp(-z));
}
function irGateRaw(v, thr, side, width) {
  const w = width > 1e-9 ? width : 1e-9;
  const z = side === 'below' ? (thr - v) / w : (v - thr) / w;
  return irSigmoid(2.5 * z);
}
function irGate(g, x) {
  const v = x[g.dim];
  switch (g.g) {
    case 'below': return irGateRaw(v, g.thr, 'below', g.w);
    case 'above': return irGateRaw(v, g.thr, 'above', g.w);
    case 'between':
      return irGateRaw(v, g.a, 'above', g.w) * irGateRaw(v, g.b, 'below', g.w);
    case 'near': return Math.exp(-Math.pow((v - g.mu) / g.w, 2));
    default: throw new Error('未知门 ' + g.g);
  }
}
function irGates(gates, x) {
  let v = 1;
  for (const g of gates) v *= irGate(g, x);
  return v;
}
function irClamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }
function irTerm(t, x) {
  switch (t.kind) {
    case 'const': return t.a;
    case 'gauss': return t.a * Math.exp(-Math.pow((x[t.dim] - t.mu) / t.w, 2));
    case 'linear': return t.a * (x[t.dim] - t.lo) / t.span;
    case 'plateau':
      return t.sign > 0
        ? t.a * irClamp((x[t.dim] - t.lo) / t.d, 0, 1)
        : t.a * irClamp((t.hi - x[t.dim]) / t.d, 0, 1);
    case 'region': return t.a * irGates(t.gates, x);
    default: throw new Error('未知项 ' + t.kind);
  }
}
function irInBox(e, x) {
  for (let i = 0; i < e.box.length; i++) {
    if (!(x[i] >= e.box[i][0] && x[i] <= e.box[i][1])) return false;
  }
  return true;
}

/* IR → runBO 的 prior 入参。字段名与 /api/run_bo 的编译结果同构,
 * 所以 app.js 两种模式下拿到的是同一种东西。 */
function priorFromIR(ir) {
  if (!ir) return null;
  const mean = ir.mean_terms || [];
  const conf = ir.confidence == null ? 1 : ir.confidence;
  const excl = ir.exclusions || [];
  const noiseT = ir.noise_terms || [];
  const costT = ir.cost_terms || [];
  const out = {
    bounds: (ir.bounds || []).map(b => ({ lo: b[0], hi: b[1] })),
    meanFn: mean.length
      ? (x) => conf * mean.reduce((s, t) => s + irTerm(t, x), 0)
      : (() => 0),
    lsScale: ir.ls_scale || null,
    recal: ir.recal || null,
    pseudoObs: (ir.pseudo_obs || []).map(p => ({ x: p.x, y: p.y, inflate: p.inflate })),
    notes: ir.notes || [],
    parked: ir.parked || [],
    audit: ir.audit || null,
    card: ir.card || null,
  };
  if (excl.length) {
    out.feasibleFn = (x) => !excl.some(e => irInBox(e, x));
    // 盒子本身也带出去,给热力图画那块"口诀划掉的禁区"用。
    // 判定仍然只走 feasibleFn 这一条路 —— 画图读的是同一份 excl,不是第二份
    // 拷贝。这条纪律这个项目已经栽过一次:视窗自己推一遍阈值,于是同一批在
    // 沙盘可行、在屏幕上被画成报废。
    out.exclusions = excl;
  }
  if (noiseT.length) {
    // 与 Python 同序:sigma_n 由 runBO 传进来的 scenario.theta.sn 定,
    // 这里只算倍率之积,且每项都不小于 1(经验只会说"这儿的读数更不可信")
    out.noiseScale = (x) => {
      let m = 1;
      for (const t of noiseT) m *= Math.max(1, 1 + t.lam * irGates(t.gates, x));
      return m;
    };
  }
  if (costT.length) {
    out.costFn = (x) => {
      let m = 1;
      for (const t of costT) {
        let s = (x[t.dim] - t.lo) / t.span;
        s = t.sign > 0 ? s : 1 - s;
        m *= Math.max(1e-3, 1 + (t.mult - 1) * irClamp(s, 0, 1));
      }
      return m;
    };
  }
  if (ir.acq && ir.acq.kappa != null) out.kappa = ir.acq.kappa;
  return out;
}

/* ---------------- 种子 RNG(与 sandbox.py 的 mulberry32 同算法) ---------- */
function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/* ---------------- Cholesky ---------------- */
function cholFactor(K) {
  const n = K.length;
  const L = [];
  for (let i = 0; i < n; i++) L.push(new Array(n).fill(0));
  for (let i = 0; i < n; i++) {
    for (let j = 0; j <= i; j++) {
      let sum = K[i][j];
      for (let k = 0; k < j; k++) sum -= L[i][k] * L[j][k];
      if (i === j) L[i][j] = Math.sqrt(Math.max(sum, 1e-12));
      else L[i][j] = sum / Math.max(L[j][j], 1e-12);
    }
  }
  return L;
}
function cholSolve(L, b) {
  const n = L.length;
  const x = b.slice();
  for (let i = 0; i < n; i++) {
    for (let j = 0; j < i; j++) x[i] -= L[i][j] * x[j];
    x[i] /= Math.max(L[i][i], 1e-12);
  }
  for (let i = n - 1; i >= 0; i--) {
    for (let j = i + 1; j < n; j++) x[i] -= L[j][i] * x[j];
    x[i] /= Math.max(L[i][i], 1e-12);
  }
  return x;
}

/* ---------------- 正态 CDF / PDF ---------------- */
function erf(z) {
  const sign = z < 0 ? -1 : 1;
  z = Math.abs(z);
  const t = 1 / (1 + 0.3275911 * z);
  const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t
    - 0.284496736) * t + 0.254829592) * t * Math.exp(-z * z);
  return sign * y;
}
function phiCdf(z) { return 0.5 * (1 + erf(z / Math.SQRT2)); }
function phiPdf(z) { return Math.exp(-0.5 * z * z) / SQRT_2PI; }

/* EI 的"尘埃线"。低于它的 EI 一律当 0 —— 见 runBO 里挑点那一段:实测 7.0%
 * 的挑点整池 EI 都塌到 1e-12 以下,那时 argmax 挑的是浮点尾数,不是判据。
 * 取 1e-12 而不是更松的值:1e-6..1e-3 那一档(2.4%)是**真的**微小收益,
 * 那是收敛末期的正常状态,不该被压掉。 */
const EI_DUST = 1e-12;

/* ---------------- GP:先验均值拟合时减掉、预测时加回 ---------------- */
class GP {
  constructor(scenario, meanFn, lsScale, noiseFn) {
    this.s = scenario;
    this.d = scenario.params.length;
    const t = scenario.theta;
    this.sf = t.sf; this.sn = t.sn; this.length = t.length;
    this.meanFn = meanFn || (() => 0);
    this.ls = [];
    for (let i = 0; i < this.d; i++) {
      this.ls.push(this.length * (lsScale ? lsScale[i] : 1));
    }
    this.noiseFn = noiseFn || (() => this.sn);
    this.X = []; this.yRaw = []; this.noise = [];
    this.L = null; this.alpha = null;
    this.y0 = null;          // 中心化常数:第一批就是天然基线
  }
  kernel(x1, x2) {
    let d2 = 0;
    for (let i = 0; i < this.d; i++) {
      const rg = this.s.params[i].range * this.ls[i];
      const di = (x1[i] - x2[i]) / rg;
      d2 += di * di;
    }
    return this.sf * this.sf * Math.exp(-0.5 * Math.max(d2, 0));
  }
  base(x) { return (this.y0 === null ? 0 : this.y0) + this.meanFn(x); }
  add(x, y, noiseInflate) {
    if (this.y0 === null) this.y0 = y;
    this.X.push(x.slice());
    this.yRaw.push(y);
    this.noise.push(this.noiseFn(x) * (noiseInflate || 1));
    const n = this.X.length;
    const res = [];
    for (let i = 0; i < n; i++) res.push(this.yRaw[i] - this.base(this.X[i]));
    const K = [];
    for (let i = 0; i < n; i++) {
      K.push(new Array(n));
      for (let j = 0; j < n; j++) {
        K[i][j] = this.kernel(this.X[i], this.X[j])
          + (i === j ? this.noise[i] * this.noise[i] + 1e-8 : 0);
      }
    }
    this.L = cholFactor(K);
    this.alpha = cholSolve(this.L, res);
  }
  predict(x) {
    const n = this.X.length;
    const m = this.base(x);
    if (!this.L || n === 0) return { mu: m, sigma: this.sf };
    const k = new Array(n);
    for (let i = 0; i < n; i++) k[i] = this.kernel(this.X[i], x);
    let mu = m;
    for (let i = 0; i < n; i++) mu += k[i] * this.alpha[i];
    // σ² = sf² − kᵀK⁻¹k。用前代解 z=L⁻¹k,则 kᵀK⁻¹k = zᵀz。
    const z = new Array(n).fill(0);
    for (let i = 0; i < n; i++) {
      let s = k[i];
      for (let j = 0; j < i; j++) s -= this.L[i][j] * z[j];
      z[i] = s / Math.max(this.L[i][i], 1e-12);
    }
    let zz = 0;
    for (let i = 0; i < n; i++) zz += z[i] * z[i];
    return { mu, sigma: Math.sqrt(Math.max(this.sf * this.sf - zz, 1e-9)) };
  }
  // 去噪 incumbent:已观测点上的后验均值最大值。用 argmax(y) 会让一次虚高
  // 读数把 EI 门槛顶上天,优化器提前"收敛"在幻影上。
  bestSeen() {
    if (!this.X.length) return { x: null, y: -Infinity };
    let bi = 0, bv = -Infinity;
    for (let i = 0; i < this.X.length; i++) {
      const m = this.predict(this.X[i]).mu;
      if (m > bv) { bv = m; bi = i; }
    }
    return { x: this.X[bi], y: bv };
  }
}

/* ---------------- 候选池 ----------------
 * keep 只施加"先验声明的"硬剪除 + box,**不**施加沙盘真实可行域。
 * 沙盘的红线是物理事实,优化器事先并不知道,必须自己撞出来 —— 否则安全类
 * 经验卡毫无价值,整个价值故事就是循环论证。与 bo._pool 同一条纪律。
 */
function poolOf(bounds, keep, seed, n) {
  const N = n || 220;
  const out = [];
  for (let i = 0; i < N; i++) {
    const rng = mulberry32(seed + i * 131);
    const x = bounds.map(b => b.lo + rng() * (b.hi - b.lo));
    if (keep(x)) out.push(x);
  }
  if (out.length < 12) {   // 先验把域剪得极窄:放宽到 bounds-only,靠软惩罚兜
    for (let i = 0; i < N; i++) {
      const rng = mulberry32(seed + 99991 + i * 137);
      out.push(bounds.map(b => b.lo + rng() * (b.hi - b.lo)));
    }
  }
  return out;
}

/* ---------------- 一次完整寻优 ----------------
 * prior 形如 { bounds, meanFn, feasibleFn, noiseFn, costFn, lsScale, recal, kappa }
 * —— 与 /api/run_bo 返回的编译结果同构,两种模式下 app.js 拿到的是同一种东西。
 */
function runBO(scenario, prior, seed, maxIters, startPoints) {
  const d = scenario.params.length;
  const p = prior || {};
  const full = scenario.params.map(pp => ({ lo: pp.lo, hi: pp.hi }));
  const meanFn = p.meanFn || (() => 0);
  const priorFeas = p.feasibleFn || (() => true);
  // 噪声接口有两种给法:noiseFn 给绝对值,noiseScale 给倍率(IR 走后者 ——
  // 经验说的是"这儿的读数更不可信",倍率才是它真正声明的东西,σ_n 的绝对
  // 量级归沙盘的 θ 管)。两者都没有就用场景自己的 σ_n。
  const noiseFn = p.noiseFn
    || (p.noiseScale ? (x => scenario.theta.sn * p.noiseScale(x))
                     : (() => scenario.theta.sn));
  const costFn = p.costFn || (() => 1);
  const kappa = p.kappa == null ? 1 : p.kappa;

  // 坐标校准。两套坐标必须分清:
  //   旋钮坐标 x —— 人在机台上设的那个数,唯一能真正下达的指令
  //   工件坐标 toModel(x) = x + recal —— 实际落到工件上的值
  // 先验声明的一切说的都是**工件**。
  const recal = p.recal || new Array(d).fill(0);
  const hasRecal = recal.some(v => Math.abs(v) > 1e-12);
  const toModel = x => hasRecal ? x.map((v, i) => v + recal[i]) : x.slice();

  // 只换算**声明过的**边:默认 box 是机台量程,说的本来就是旋钮不是工件。
  const declared = p.bounds || full;
  const bounds = [];
  for (let i = 0; i < d; i++) {
    let lo = declared[i].lo, hi = declared[i].hi;
    if (Math.abs(lo - full[i].lo) > 1e-12) lo = Math.max(lo - recal[i], full[i].lo);
    if (Math.abs(hi - full[i].hi) > 1e-12) hi = Math.min(hi - recal[i], full[i].hi);
    if (lo > hi) { lo = hi = Math.max(full[i].lo, Math.min(full[i].hi, lo)); }
    bounds.push({ lo, hi });
  }

  // box 在旋钮坐标下比对(那是能设的数),硬剪除在工件坐标下比对(话里说的事)
  function allowed(x) {
    for (let i = 0; i < d; i++) {
      if (x[i] < bounds[i].lo - 1e-9 || x[i] > bounds[i].hi + 1e-9) return false;
    }
    return priorFeas(toModel(x));
  }

  const gp = new GP(scenario, meanFn, p.lsScale, noiseFn);
  const obsRng = mulberry32(seed);
  const history = [];
  const visited = new Set();
  const keyOf = x => x.map(v => v.toFixed(4)).join(',');
  let bestY = -Infinity, scrapped = 0;

  function observe(x, tag) {
    const xm = toModel(x);
    const pre = gp.predict(xm);
    const y = scenario.observe(x, obsRng);
    gp.add(xm, y);
    visited.add(keyOf(x));
    if (y > bestY) bestY = y;
    const feas = scenario.feasible(x);
    if (!feas) scrapped++;
    history.push({
      i: history.length, x: x.slice(), y, mu: pre.mu, sigma: pre.sigma,
      prior: meanFn(xm), bestSoFar: bestY, feasible: feas,
      risks: scenario.risks(x), tag, ei: 0, cost: 1,
    });
  }

  // 伪观测(口述记忆)先进,噪声放大
  for (const po of (p.pseudoObs || [])) {
    if (allowed(po.x)) gp.add(toModel(po.x), po.y, po.inflate || 1.5);
  }

  for (const sp of (startPoints || [])) {
    const x = sp.map((v, i) => Math.max(bounds[i].lo, Math.min(bounds[i].hi, v)));
    if (allowed(x)) observe(x, 'warm');
  }
  if (!history.length) {
    const centre = bounds.map(b => (b.lo + b.hi) / 2);
    if (allowed(centre)) observe(centre, 'warm');
    else {
      const cs = poolOf(bounds, allowed, seed + 5, 60);
      if (cs.length) observe(cs[0], 'warm');
    }
  }

  const pool = poolOf(bounds, allowed, seed + 777);
  let prevBest = -Infinity, stagnation = 0, stoppedBy = 'cap';
  const MAX = maxIters || 24;

  for (let it = history.length; it < MAX; it++) {
    const cands = pool.filter(c => !visited.has(keyOf(c)));
    if (!cands.length) { stoppedBy = 'pool_exhausted'; break; }
    const fBest = gp.bestSeen().y;
    let bestKey = -Infinity, bestAlt = -Infinity, arg = null, argEI = 0, argCost = 1;
    for (const c of cands) {
      const xm = toModel(c);
      const pr = gp.predict(xm);
      const imp = pr.mu - fBest;
      const z = pr.sigma > 1e-9 ? imp / pr.sigma : 0;
      // 探索项按"一批真能买到多少"折价:σ·φ(z) 卖的是"测一批能消掉多少不
      // 确定性",而一批观测实际消掉的只有 σ²/(σ²+σn²) —— GP 自己的收缩因子,
      // 不是自由参数。噪声区里剩下的方差你出钱也买不走。
      // 期望改进项 imp·Φ(z) 不打折:读数吵不代表那儿的真值不好。
      const snDec = noiseFn(xm);
      const v = pr.sigma * pr.sigma;
      const harvest = v / Math.max(v + snDec * snDec, 1e-12);
      let ei = imp * phiCdf(z) + kappa * pr.sigma * phiPdf(z) * harvest;
      ei = Math.max(ei, 0);
      const cost = Math.max(costFn(c), 1e-6);
      const eff = ei / cost;
      // 尘埃区:整池 EI 全塌到 1e-12 以下时,argmax 挑的其实是浮点尾数。实测
      // 1571 次挑点里有 7.0% 落在这个区间 —— 而两种语言的尾数永远不会一致,
      // 于是 formation/wrong 在第 4 批分叉,Python 跑 14 批、JS 跑 10 批,同一
      // 张卡两个引擎给出不同的账(−4.16 vs −4.11)。这不是精度问题,是**判据
      // 在这个区间里没有定义**:EI 说"哪儿都没得赚"时,它对"去哪儿"是沉默的,
      // 而代码却硬要从沉默里读出一个答案。
      //
      // 所以把尘埃全部压成 0(承认这一区间 EI 无话可说),再用一条**有量纲、
      // 有量级**的次级判据接手:σ²/(σ²+σn²)·σ/成本 —— 每花一个批次的钱能消掉
      // 多少不确定性。它的取值差是 O(1e-2) 级,两种语言比得出同一个赢家;
      // 语义上也正是 EI 沉默时该做的事:不赌收益了,那就买信息。
      const key = eff > EI_DUST ? eff : 0;
      const alt = pr.sigma * harvest / cost;
      if (key > bestKey || (key === bestKey && alt > bestAlt)) {
        bestKey = key; bestAlt = alt; arg = c; argEI = ei; argCost = cost;
      }
    }
    if (!arg) { stoppedBy = 'pool_exhausted'; break; }
    observe(arg, 'bo');
    history[history.length - 1].ei = argEI;
    history[history.length - 1].cost = argCost;

    if (bestY > prevBest + 1e-6) { prevBest = bestY; stagnation = 0; }
    else if (++stagnation >= 4) { stoppedBy = 'converged'; break; }
  }

  // 推荐点 = 后验均值最高的已观测点(去噪),再看它的真实值。
  // 不用 argmax(y):那等于把"读数运气最好的一批"当成推荐,是自欺。
  let bestX = null, trueBest = -Infinity, reportBest = -Infinity;
  if (history.length) {
    let bi = 0, bv = -Infinity;
    history.forEach((h, i) => {
      const m = gp.predict(toModel(h.x)).mu;
      if (m > bv) { bv = m; bi = i; }
      reportBest = Math.max(reportBest, h.bestSoFar);
    });
    bestX = history[bi].x.slice();
    trueBest = scenario.reward(bestX)
      - (scenario.feasible(bestX) ? 0 : scenario.scrapPenalty);
  }

  // 报废分账:这一托该记在哪条红线上。一句只讲冷隔的话,不该因为卷气废品的
  // 涨落被判成有害 —— 它一个字也没提卷气,那是另一句话的活。
  // 与 bo.py 的 res.scrap_by_cause 逐字对应。
  const scrapByCause = {};
  for (const h of history) {
    if (h.feasible) continue;
    for (const k of Object.keys(h.risks)) {
      if (h.risks[k] > 0.5) scrapByCause[k] = (scrapByCause[k] || 0) + 1;
    }
  }

  // 执行保真度:这句话声明过的区间,在**工件**上真被落到的批次比例。
  // 校准类经验("那台表偏高三度")只作用在这条轴上 —— 它不改响应面、不改
  // 搜索域,只保证"你以为设了 200,工件真拿到 200"。没声明任何区间的卡
  // 保真度恒为 1(没说过的话不该被追责)。与 bo.py 的 _compliance 一致。
  const edges = [];
  if (p.bounds && p.bounds.length) {
    for (let i = 0; i < d; i++) {
      if (Math.abs(p.bounds[i].lo - full[i].lo) > 1e-12
        || Math.abs(p.bounds[i].hi - full[i].hi) > 1e-12) {
        edges.push({ i, lo: p.bounds[i].lo, hi: p.bounds[i].hi });
      }
    }
  }
  let compliance = 1;
  if (edges.length && history.length) {
    let hit = 0;
    for (const h of history) {
      const a = scenario.actual(h.x);
      if (edges.every(e => a[e.i] >= e.lo - 1e-9 && a[e.i] <= e.hi + 1e-9)) hit++;
    }
    compliance = hit / history.length;
  }

  // 交付诚实度的原料:报告值 − 真值 = 发出去的那个数**虚高**了多少。
  // 观测可信度类的经验("静置不到位内阻是假的")只作用在这条轴上,批次和
  // 废品都体现不了它 —— 少了这个数,那类经验在结算页上永远是零。
  const overclaim = history.length ? reportBest - trueBest : 0;

  // 先验存活度:先验均值 vs 拟合后验的相关系数 —— "称量每句话"的定量版
  let survival = null;
  if (prior && prior.meanFn && history.length >= 3) {
    const pri = history.map(h => meanFn(toModel(h.x)));
    const post = history.map(h => gp.predict(toModel(h.x)).mu);
    const mean = a => a.reduce((s, v) => s + v, 0) / a.length;
    const mp = mean(pri), mq = mean(post);
    let num = 0, dp = 0, dq = 0;
    for (let i = 0; i < pri.length; i++) {
      num += (pri[i] - mp) * (post[i] - mq);
      dp += (pri[i] - mp) ** 2; dq += (post[i] - mq) ** 2;
    }
    if (dp > 1e-12 && dq > 1e-12) survival = num / Math.sqrt(dp * dq);
  }

  return {
    history, bestY, bestX, trueBest, reportBest,
    nBatches: history.length, scrapped, scrapByCause, compliance, overclaim,
    stoppedBy, priorSurvival: survival,
  };
}

/* ---------- 记分卡 ----------
 * 与 server/test_align.py 的 card_score 是**同一个公式**,逐项对应。
 *
 * 为什么前端也必须用这个公式:结算页原先只按"省了几批"折钱。多种子实测下,
 * 那句"1C 拉满省电费"的歪经平均省 +0.93 批(全场最省),而推荐点真值低了
 * 0.93 个百分点 —— 后端按四轴综合分把它否决在 -3.76,前端却会给它发一笔奖金。
 * 同一句话两个引擎给相反的结论,和之前手写两份先验是同一类错,只是这次错在
 * 记分卡而不是先验。省批次是四条轴里最容易刷的一条:把箱子收进红线墙里就能
 * 白省 —— 单看它等于奖励"把搜索域切小",而不是奖励"说对了话"。
 *
 * 综合分 = 省批次 + 1.5×省废品 + 4×真值提升 + 4×少虚高 − 2×(说了没做到)
 * 单位统一成"批次等价",所以乘一次单批成本就是钱。
 *
 * **单种子结算是错的,这里记下为什么。** 原先舞台上只跑一个固定种子,并把它
 * 说成"有意的口径差异:评委看到的数必须来自他刚看完的那一轮"。那句话听着
 * 讲究,其实掩盖了一个硬事实:单种子下 gain_best 的标准差实测 0.97(formation,
 * 60 种子),而否决线 QUALITY_NOISE 是 0.10 —— 等于在 0.1σ 处下判决。后果不是
 * "更颤一点",是**判决基本随机**:块扫描实测,单种子下好卡被误否的比例 33.6%,
 * 其中"首充慢一点"这张旗舰卡在舞台种子上结算出 −¥683,642「被数据否决」。
 * 同一张卡在 40 种子均分是 +2.73。
 *
 * 后端(test_align.py)之所以一直是绿的,正因为它先平均 12 个种子再判 —— 阈值
 * 是照着**均值的**抖动定的。前端拿同一个阈值去判单次实现,是把阈值用在了它
 * 没被标定的地方,和"拿一条只说预充段的规则去管主恒流段"是同一类错。
 *
 * 定为 16 种子:块扫描下误否率 33.6%(N=1)→ 11.0%(N=12)→ 2.7%(N=16),
 * 而 24 种子只再降到 2.0%,不值那 0.5 秒。实测 16 种子约 0.4~0.7 秒,基准田
 * 那一半还能跨卡缓存 —— 放在幕3 的回放里绰绰有余。
 *
 * 舞台上**演**的仍然是一个固定种子(state.history.injected,可复现回放);
 * 变的只是**结算的账**要按 16 种子算。演的是一轮,判的是分布 —— 这才是
 * 老老实实的口径,也正好是台上那句"这数是当场算的"该有的意思。
 */
const QUALITY_NOISE = 0.10;   // 比这更小的真值差异不算差异(照 16 种子均值标定)
const SCRAP_BLOWUP = 1.5;     // 无论说了什么,把总废品顶高这么多就是有害
const SETTLE_SEEDS = 16;      // 结算用的种子数 —— 见上面那段:1 个种子判不出来

function cardValue(base, inj, opts) {
  const speaksTo = (opts && opts.speaksTo) || [];
  const savedBatches = base.nBatches - inj.nBatches;
  const savedScrap = base.scrapped - inj.scrapped;
  // 废品按成因分账:只算这句话**提到过**的红线。
  //
  // ⚠ 已知缺陷,实测过,别信上面那句话的下半段。原来这里写的是"没声明红线的卡
  // 按总废品计 —— 没有依据就不做任何折扣",本意是宽容,实际效果是反的:
  //   · cold 点名了析锂线 → 只结析锂那一笔 0.63(它总共动了 1.13)
  //   · cast 一条红线都没点名 → 把 gas 0.69 + li 0.25 全额领走 1.44
  // 于是**话说得越含糊,能领的废品钱越多**,而这恰恰是翻译层拒绝犯的错(不替
  // 一句话补它没说的内容)。记分卡不该犯翻译器拒绝犯的错 —— 这条纪律写在
  // test_align.py 的 card_score 文档串里,而这段代码违反了它。
  //
  // 为什么现在不改:改法不是把无标签的卡记 0 分那么简单。sei(「首充慢一点」)
  // 低倍率成膜,物理上确实压的是析锂废品,只是卡面上没写"析锂"三个字;按标签
  // 记 0 分是在罚文案,不是在罚物理。正确的改法是**由先验实际收窄的维度反推
  // 它把守着哪条红线**,而那会动到全部结算数字。距交稿只剩几小时,而且实测
  // 表明这个口子并不是 cast 拿全场第一的原因(它靠的是 gainBest 0.748,四条轴
  // 里最高的一个),补这个口子不改变名次。所以先把缺陷写在这儿,不假装没有。
  const bc = base.scrapByCause || {}, ic = inj.scrapByCause || {};
  const scrapTerm = speaksTo.length
    ? speaksTo.reduce((s, k) => s + ((bc[k] || 0) - (ic[k] || 0)), 0)
    : savedScrap;
  const gainBest = inj.trueBest - base.trueBest;
  // 少虚高 = 基准田的(报告值−真值) − 注入田的。观测可信度类的经验只落在这条轴上。
  const gainHonesty = (base.overclaim || 0) - (inj.overclaim || 0);
  // 保真度按**折扣**算,不按双田之差:基准田从没被告知这句话,它"违规"是
  // 天经地义的,拿两田之差当增益等于给任何收窄搜索域的卡白送一笔。
  const unmet = 1 - (inj.compliance == null ? 1 : inj.compliance);
  const score = savedBatches + 1.5 * scrapTerm
    + 4 * gainBest + 4 * gainHonesty - 2 * unmet;
  // 两条否决权:早点拿到一个更差的答案不是加速;分账不是无限透支的口子。
  const vetoed = gainBest < -QUALITY_NOISE || savedScrap < -SCRAP_BLOWUP;
  return {
    savedBatches, savedScrap, scrapTerm, gainBest, gainHonesty, unmet,
    score, vetoed,
    // 四条增益轴 + 一条折扣轴的**批次等价**贡献,给结算页挑主轴用
    terms: [
      { id: 'batches', w: savedBatches },
      { id: 'scrap', w: 1.5 * scrapTerm },
      { id: 'quality', w: 4 * gainBest },
      { id: 'honesty', w: 4 * gainHonesty },
      { id: 'fidelity', w: -2 * unmet },
    ],
  };
}

/* ---------- 多种子结算 ----------
 * 与 server/bo.py 的 eval_prior 同做法:**先把各条度量在种子上平均,再打分**。
 * 顺序不能颠倒。先打分再平均,等于让每个种子各投一次否决票,那是在平均一堆
 * 硬阈值的输出;先平均再打分,判的才是"这句话的期望效果",阈值也正是照着
 * 均值的抖动标定的(见 QUALITY_NOISE 上面那段)。
 *
 * cardValue 对这些度量是线性的(否决除外),所以"平均后的一对虚拟田"喂给
 * cardValue,与 Python 先平均 metrics 再 card_score 是同一个数 —— 一份公式,
 * 两处解释,还是那条规矩。
 *
 * 基准田那一半与卡无关,按 (场景, 种子起点, 种子数) 缓存,而且是**可断续**的:
 * 缓存里存的是已经跑完的那几轮,不是"算好了/没算好"的布尔量。这样第一张卡也
 * 能把基准田那 16 轮摊进回放的空隙里,不必在开场先卡 0.8 秒。 */
const settleBaseCache = new Map();

function baselineSlot(scenario, seeds, seed0) {
  const key = scenario.id + '|' + seeds + '|' + seed0;
  let slot = settleBaseCache.get(key);
  if (!slot) { slot = { runs: [], mean: null }; settleBaseCache.set(key, slot); }
  return slot;
}

/* 推进基准田一轮,返回"是否已经凑够 seeds 轮"。
 * 参数与 bo.py 的 eval_prior 逐字对应:24 批上限、baseStart()、种子步长 7919。 */
function baselineStep(scenario, seeds, seed0) {
  const slot = baselineSlot(scenario, seeds, seed0);
  if (slot.runs.length < seeds) {
    slot.runs.push(runBO(scenario, null, seed0 + slot.runs.length * 7919,
      24, scenario.baseStart()));
  }
  if (slot.runs.length >= seeds && !slot.mean) slot.mean = meanRuns(slot.runs);
  return !!slot.mean;
}

/* 一次补 n 轮基准田,返回"是否已经凑够"。
 *
 * 为什么要有这个而不是让调用方连着调 n 次 baselineStep:调用方要的是"这一拍
 * 花掉大约多少毫秒",而 baselineStep 每次只推一轮。回放拍数少的卡(实测最少
 * 6 拍)如果基准田还没预热完,一拍一轮就把整个回放全用在基准田上,注入田那
 * 16 轮只能压回结算那一瞬 —— 那正是要躲的东西。
 *
 * 仍然只有一条计算路径:里面就是循环调 baselineStep,种子序号由它自己推进。 */
function baselineFill(scenario, seeds, seed0, n) {
  let done = false;
  for (let i = 0; i < (n || 1); i++) {
    done = baselineStep(scenario, seeds, seed0);
    if (done) break;
  }
  return done;
}

function meanRuns(runs) {
  const n = runs.length || 1;
  const avg = f => runs.reduce((s, r) => s + f(r), 0) / n;
  const causes = {};
  for (const r of runs) {
    for (const k of Object.keys(r.scrapByCause || {})) {
      causes[k] = (causes[k] || 0) + r.scrapByCause[k] / n;
    }
  }
  return {
    nBatches: avg(r => r.nBatches),
    scrapped: avg(r => r.scrapped),
    trueBest: avg(r => r.trueBest),
    overclaim: avg(r => r.overclaim || 0),
    compliance: avg(r => r.compliance == null ? 1 : r.compliance),
    scrapByCause: causes,
  };
}

/* 结算用的基准田均值。缺的轮次就地补齐 —— 与逐拍推进走同一个 baselineStep,
 * 所以"摊着算"和"一次算完"永远是同一个数。 */
function settleBaseline(scenario, seeds, seed0) {
  while (!baselineStep(scenario, seeds, seed0));
  return baselineSlot(scenario, seeds, seed0).mean;
}

/* 一张卡的结算,做成**可分期**的:16 轮注入田不是一口气算完,而是随回放
 * 的节拍一轮一轮推进。
 *
 * 为什么要分期。热缓存下 16 轮注入田实测 0.29~1.60 秒(casting/p3 最慢),
 * 而它原先整块压在"最后一批画完 → 弹结算页"那一瞬 —— 那正是全场最关键的
 * 一下,页面却卡死一秒半。回放本身要走 4 秒左右的 setTimeout,那 4 秒里主
 * 线程基本闲着:把算账的活摊进这些空隙,时间就藏在已经付过的钱里了。
 *
 * 关键纪律:**分期和一口气算必须是同一个数**。所以只有一条计算路径 ——
 * settleValue 就是"把剩下的种子跑完再取值",与逐拍推进走的是同一个 step。
 * 另写一份快路径就是又开一处会分叉的口子,这个项目在这上面已经栽过四次。 */
function settlePlan(scenario, prior, opts) {
  const o = opts || {};
  const seeds = o.seeds || SETTLE_SEEDS;
  const seed0 = o.seed0 == null ? 20260829 : o.seed0;
  const runs = [];
  let k = 0;
  const plan = {
    seeds,
    get progress() { return k / seeds; },
    /* 已经算完几轮。调用方要按"还剩几轮 / 还剩几拍"决定这一拍推几轮 ——
     * 用 progress 反推会带浮点尾巴,而这里要的是整数。 */
    get done() { return k; },
    /* 推进 n 个种子(默认 1),返回是否已算完。种子步长 7919 与 bo.py 同源。 */
    step(n) {
      for (let j = 0; j < (n || 1) && k < seeds; j++, k++) {
        runs.push(runBO(scenario, prior, seed0 + k * 7919, 20, scenario.injStart()));
      }
      return k >= seeds;
    },
    /* 取值。没算完就先算完 —— 调用方不必关心分期到哪一步了。 */
    value() {
      while (k < seeds) plan.step(1);
      const base = settleBaseline(scenario, seeds, seed0);
      const v = cardValue(base, meanRuns(runs), { speaksTo: o.speaksTo || [] });
      v.seeds = seeds;
      return v;
    },
  };
  return plan;
}

/* 一次算完的写法,给 Node 侧对账和单元测试用。页面上走 settlePlan 分期。 */
function settleValue(scenario, prior, opts) {
  return settlePlan(scenario, prior, opts).value();
}

/* ---------- 校准自审:秤本身准不准 ----------
 * 与 server/test_align.py 的 _z_stats / test_calibration 逐字对应。
 *
 * 为什么这件事必须搬到台前:别队的孪生是"画出一张响应面",而一张画出来的
 * 响应面无法回答"你这图是不是先画好的"。能回答的只有一件事 —— 让孪生**当场
 * 验它自己报的不确定度**:每一批的 (mu, sigma) 都是**观测之前**记下的预测,
 * 天然是留出验证集,事后拿真读数对一遍,谁也没机会作弊。
 *
 * 两个坑,都写在这儿免得以后有人"顺手优化"掉:
 *   1) 分母必须带 σ_n。sigma 是**函数值**的不确定度,而 y 是带批次噪声的读数;
 *      只用 sigma 当分母会把 z 系统性放大,把一个校准良好的 GP 判成过度自信。
 *   2) 必须按可行性**分账**。混在一起算是偏差 −2.0/覆盖 61%(像"σ 严重过度
 *      自信"),拆开看却是:可行批次 σ 诚实(覆盖 ~89%),报废批次完全没预料到
 *      (−10σ 量级)。后者不是 GP 的毛病,是 scrap_penalty 那个 6 分阶跃 ——
 *      平稳核表达不了断崖,而且撞第五次墙时意外程度照旧,一点没学乖。
 *      把两栏平均成一个数,等于用一个自己造的假警报盖掉一条真结论。
 */
function zStats(zs) {
  const n = zs.length;
  if (!n) return null;
  const bias = zs.reduce((s, v) => s + v, 0) / n;
  const spread = Math.sqrt(zs.reduce((s, v) => s + (v - bias) ** 2, 0) / Math.max(1, n - 1));
  const cover = zs.filter(v => Math.abs(v) < 1.64).length / n;
  return { n, bias, spread, cover };
}

/* 留出验证:跑 runs 轮无先验裸跑,收集观测前预测的标准化残差。
 * 用无先验裸跑而不是注入田 —— 审的是**孪生+GP 这把秤**,不是某句话的功劳。 */
function calibrationAudit(scenario, runs, iters, seed0) {
  const sn = scenario.theta.sn;
  const feas = [], scrap = [];
  let scrapped = 0, rounds = runs || 16;
  for (let k = 0; k < rounds; k++) {
    const r = runBO(scenario, null, (seed0 || 20260829) + k * 7919, iters || 20, scenario.baseStart());
    scrapped += r.scrapped;
    for (const h of r.history) {
      if (h.tag !== 'bo') continue;     // warm start 没有真正的预测可言
      const sd = Math.sqrt(h.sigma * h.sigma + sn * sn);
      if (sd <= 1e-9) continue;
      (h.feasible ? feas : scrap).push((h.y - h.mu) / sd);
    }
  }
  const f = zStats(feas);
  return {
    rounds, scrapPerRun: scrapped / rounds,
    feasible: f, infeasible: zStats(scrap),
    // 判据与 Python 同门限:百来批数据的覆盖率本身有 ±10% 抖动,卡太死会变成
    // "每次重跑都红一次"的假警报。
    honest: !!f && Math.abs(f.bias) < 0.6 && f.cover >= 0.75 && f.cover <= 0.99,
  };
}

/* ---------- 双田对照:同 oracle、同优化器、同噪声种子,唯一差异是先验 ---- */
function runDemo(scenario, prior, seed) {
  // 两田同种子。种子不同的对照不是对照 —— 唯一的差异必须是那句话。
  const base = runBO(scenario, null, seed, 24, scenario.baseStart());
  const inj = runBO(scenario, prior, seed, 20, scenario.injStart());
  return { baseline: base, injected: inj };
}

