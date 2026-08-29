/* scenarios.js — the two sandboxes. Same engine interface; everything that
 * differs between them (param space, mechanism rules, 口诀 cards, narration,
 * numbers) lives here. Public-knowledge mechanism rules only. */
'use strict';

/* ====================================================================
 * shared helpers
 * ==================================================================== */
function hill(x, peak, w) { return Math.exp(-Math.pow((x - peak) / w, 2)); }
function cl(x, lo, hi) { return Math.max(lo, Math.min(hi, x)); }
// 方向门:v 明显低于/高于阈值时趋近 1。坡宽而非硬跳变 —— 保证 EI 可微。
// 与 server/sandbox.py 的 lo_gate / hi_gate 逐字对应。
function loGate(v, thr, w) { return cl((thr - v) / w, 0, 1); }
function hiGate(v, thr, w) { return cl((v - thr) / w, 0, 1); }
// 析锂起始温度(℃),随预充倍率上移 —— 所以析锂线是 (倍率,温度) 平面上的
// 一道**斜边**,不是两条独立红线。也正因为是斜边,一句只说"低于30度别超
// 0.2C"的口诀只能盖住它的一部分:经验是对的,但不完备。
function platingOnset(pre) { return 24 + 36 * pre; }
// SEI 致密度(0..1),随预充电流密度**对数**下降:电流每翻一倍就多一档
// 不可逆锂损失,量程内没有平台。有平台的话,"首充慢一点"在半个量程上
// 无事可做,"1C 拉满省电费"这句歪经也几乎不受惩罚。
function seiQuality(pre) { return cl(1 - 0.22 * Math.log2(Math.max(pre, 0.005) / 0.03), 0, 1); }

/* 带批次噪声的观测 —— 与 server/sandbox.py 的 Scene.observe 逐字对应。
 *
 * 这个函数必须存在,不能让引擎直接读 reward():
 *   · reward() 是"上产线真拿到的值",observe() 是"仪表读到的值"。两者的差
 *     (bias)正是"静置不到位内阻是假的"这句口诀唯一的落点 —— 光有噪声会被
 *     平均掉,系统性偏高不会,它会让你把一个虚高的配方当成最优解发出去。
 *   · 越过红线照样返回一个数,只是扣掉 scrapPenalty(整托报废)。优化器不该
 *     免费拿到安全知识,它得自己撞、自己学 —— 这正是安全类口诀省下的东西。
 * 少了这一层,兜底模式里废品恒为 0、虚高恒为 0,一半的经验卡当场失去价值。
 */
function observeWith(s, x, rng) {
  const u1 = Math.max(1e-12, rng());
  const u2 = rng();
  const z = Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
  const sd = s.noiseSigma * s.noiseAt(x);
  const y = s.reward(x) + s.biasAt(x) + sd * z;
  return s.feasible(x) ? y : y - s.scrapPenalty;
}

/* ====================================================================
 * SCENARIO A — 磷酸铁锂·化成 (forward face)
 * ==================================================================== */
const FORMATION = {
  id: 'formation',
  name: '磷酸铁锂 · 化成',
  subtitle: '新体系导入 · 首轮试制',
  // reward = 首次库仑效率 (%). Higher better. Display baseline ~88–91.
  rewardName: '首次库仑效率',
  rewardUnit: '%',
  // rewardMax:热力图与收敛曲线的色标/纵轴上界。原先缺这个字段,drawHeat 里
  // scene.rewardMax 求出 NaN,整张热力图会画成一片死色。
  rewardMax: 91.5,
  noiseSigma: 0.3,
  // 越过红线不是"分数低一点",是整托报废。与 sandbox.py 的 scrap_penalty 一致。
  scrapPenalty: 6.0,
  // θ 与 server/sandbox.py 逐字一致。它不是手写的:按留出预测密度在网格上
  // 选出,并要求同时通过校准约束(|偏差|<0.35、90%区间覆盖 80~97%)。
  // 详见 sandbox.py 文件头 —— 原手写的 sf=1.2 是照全域 std 定的,而那个 std
  // 几乎全来自撞红线的断崖,GP 只在可行域里学,于是 σ 系统性偏宽。
  theta: { length: 0.22, sf: 1.0, sn: 0.32 },
  // two params shown on the heatmap plane
  plane: [0, 1], // pre-charge x temp

  params: [
    { name: '预充倍率', unit: 'C', lo: 0.02, hi: 0.50, range: 0.48, decimals: 2 },
    { name: '化成温度', unit: 'C', lo: 25, hi: 55, range: 30, decimals: 0 },
    { name: '预充切换电压', unit: 'V', lo: 3.0, hi: 3.45, range: 0.45, decimals: 2 },
    { name: '恒压截止电流', unit: 'C', lo: 0.02, hi: 0.10, range: 0.08, decimals: 3 },
    { name: '高温老化时长', unit: 'h', lo: 6, hi: 48, range: 42, decimals: 0 },
  ],

  redLines: [
    { id: 'li', label: '析锂线', desc: '低温 × 高倍率 → 锂沉积报废' },
    { id: 'gas', label: '产气线', desc: '高温副反应 → 鼓气' },
    { id: 'yield', label: '首效达标线', desc: 'FCE ≥ 88.0% 才放行' },
  ],

  // 两田同起点。起点不同的对照不是对照 —— 唯一的差异必须是那句话。
  // 而且这个起点是"新体系导入时的教科书起点":室温、保守倍率、短老化,
  // 离最优点很远。没有经验的时候,人就是从这儿开始的。
  deviceBias: [0, 0, 0, 0, 0],
  actual(x) { return x.map((v, i) => v - this.deviceBias[i]); },
  baseStart() { return [[0.22, 30, 3.15, 0.08, 12]]; },
  injStart()  { return [[0.22, 30, 3.15, 0.08, 12]]; },

  // 内部机理函数一律吃"实际落到工件上的值";公开接口吃"设定读数",
  // 各自只做一次 actual() 换算。两边都换算就会重复扣一次设备偏差。
  _feasible(x) {
    const [pre, T, sw, cut, age] = x;
    if (this._liRisk(x) > 0.55) return false;   // 析锂墙(斜边)
    if (T > 51 && age > 30) return false;       // 产气墙
    return true;
  },
  // 析锂风险 = 温度低于"该倍率下的析锂起始温度"多少。单变量门,但门槛
  // 是倍率的函数 —— 所以它在 (倍率,温度) 平面上是一道斜边。
  _liRisk(x)  { const [pre, T] = x; return loGate(T, platingOnset(pre), 10); },
  _gasRisk(x) { const [ , T, , , age] = x; return cl((T - 44) / 16, 0, 1) * cl(age / 48, 0, 1); },

  // 异方差:老化不足 → SEI 未稳定、浸润未平衡,读数偏离稳态
  _noiseAt(x) { return 1 + 2.2 * cl((24 - x[4]) / 18, 0, 1); },
  // 测量偏置:老化不足的读数不是"飘",是**假高** —— 噪声会被平均掉,
  // 系统性偏高不会。这是 observe 与 reward 的分岔点。
  _biasAt(x)  { return 1.1 * cl((24 - x[4]) / 18, 0, 1); },

  feasible(x) { return this._feasible(this.actual(x)); },
  liRisk(x)   { return this._liRisk(this.actual(x)); },
  gasRisk(x)  { return this._gasRisk(this.actual(x)); },
  noiseAt(x)  { return this._noiseAt(this.actual(x)); },
  biasAt(x)   { return this._biasAt(this.actual(x)); },
  reward(x)   { return this._reward(this.actual(x)); },
  // 统一风险接口:键 = redLines 的 id。两个场景的红线名字不同(析锂/冷隔),
  // 但机理槽位一致 —— 画图和仪表只认 id,不认名字,换场景就不会画不出线。
  risks(x)    { return { li: this.liRisk(x), gas: this.gasRisk(x) }; },
  observe(x, rng) { return observeWith(this, x, rng); },

  _reward(x) {
    const [pre, T, sw, cut, age] = x;
    const tResp = hill(T, 42, 9);
    const preResp = seiQuality(pre);
    const swResp = hill(sw, 3.40, 0.25);
    const cutResp = cl(1 - (cut - 0.03) / 0.10, 0.7, 1);
    const ageResp = cl(age / 30, 0.9, 1) * (age < 8 ? 0.9 : 1);
    // 倍率权重 2.2 与 server/sandbox.py 一致(原先是 1.0)。首充电流密度是
    // 化成阶段决定 SEI 质量的首要变量,必须压得住温度的 1.7 —— 否则歪经卡
    // "1C 拉满"被迫升温后白捡的温度增益比 SEI 罚还大,在沙盘里真能赚钱。
    let fce = 89.4
      + 1.7 * (tResp - 0.8)
      + 2.2 * (preResp - 0.6)
      + 0.5 * (swResp - 0.8)
      + 0.5 * (cutResp - 0.85)
      + 0.4 * (ageResp - 0.95);
    // 调下划线版本:x 在这里已经是实际值了,再走公开接口会二次换算
    if (this._liRisk(x) > 0.7) fce -= 8;
    if (this._gasRisk(x) > 0.6) fce -= 3.5;
    return cl(fce, 79, 91.5);
  },

  /* ------------ cards ------------
   * 只留**卡面**(念出来的话 + 卡背机理 + 逐批解说)。先验本身不在这里,
   * 它在 priors_ir.js —— Python 编译一次,两个引擎解释同一份数据。
   *
   * 这里原先手写过第二份 bounds/priorMean,和后端编译出来的那份悄悄分叉了:
   * 同一张歪经卡,后端综合分 -3.76(否决),前端却因为「倍率箱子收窄」白省
   * 2.00 批,成了全场最省批次的卡。同一句话两个引擎给相反的结论 —— 那是在
   * 答辩现场会被一句话问倒的东西。删掉手写版,不是简化,是去掉一个真 bug。 */
  cards: [
    { id: 'sei', narration: '首充 0.06C 的 SEI 又薄又匀，内阻和首效一起落下来——这跟您说的一样' },
    { id: 'cold', narration: '低于 30℃ 还上大倍率，锂直接析在负极面上——这尖角不能碰' },
    { id: 'temp', narration: '化成温度顶到 40℃ 上下，膜长得快还不伤极片' },
    { id: 'age', narration: '老化不满 24 小时，内阻数据都是虚的' },
    { id: 'wrong', narration: '1C 拉满——首效掉、内阻飙，析锂风险拉满。这句是歪经' },
    { id: 'cast', narration: '这条是压铸车间的老话，放到化成上先清一下温度窗口' },
  ],
  /* ------------ 机理斜边 ------------
   * 结算揭底时叠在热力图上的那条虚线。它存在的理由不是好看:
   *
   * 析锂不是"低温"一条红线加"大倍率"一条红线,是 (倍率,温度) 平面上的一道
   * **斜边** —— 倍率每高 0.1C,能安全用的最低温度就抬高 3.6℃。而口诀说的是
   * "低于 30℃ 别超 0.2C",那是斜边上的**一个直角**:0.2C 以下它盖得住,
   * 0.3C 时真正的起始温度已经到 34.8℃,口诀那句话不管了。经验是对的,但不完备
   * —— 这正是"人给方向、AI 给验证"里 AI 那一半在干的事,得让评委在图上看见。
   *
   * 只能调 platingOnset:_feasible 判废用的就是它。另写一份斜率就是第五次
   * 分叉(机理视窗曾经自己推了一遍析锂阈值,于是同一批在沙盘可行、在视窗报废)。
   *
   * 压铸场景没有这一项:那边两道墙(卷气 v>2.5、冷隔熔模温双低)在所选平面上
   * 都是轴对齐的,没有斜边可讲。字段缺失就不画,不必编一条出来。 */
  mechLine: {
    label: '析锂起始线',
    formula: 'T = 24 + 36 × 倍率',
    note: '斜边,不是直角',
    /* 横轴设定值 → 纵轴设定值。机理函数吃的是**实际落到工件上的值**,而坐标轴
     * 上读的是**设定值**,所以进出各换算一次。偏差只写在 deviceBias 里,这条
     * 线不留第二份 —— 否则改了仪表偏差,红线区挪了而这条线还停在原处。 */
    at(scene, aSet) {
      const [ia, ib] = scene.plane;
      const probe = scene.params.map(p => (p.lo + p.hi) / 2);
      probe[ia] = aSet;
      return platingOnset(scene.actual(probe)[ia]) + scene.deviceBias[ib];
    },
  },
  settleMixin: {
    noun: '试验批次',
    line: '一个批次 = 一托电芯 + 化成柜占用数天',
    // 报废的量词。化成线上报废的单位是"托"(一托电芯整体判废),压铸是"模次"。
    // 可信度面板和结算页都念这个词 —— 硬编码"托"会让压铸场景说出车间不用的话。
    scrapUnit: '托',
  },
};

/* ====================================================================
 * SCENARIO B — 压铸 · 开机废 (底仓)
 * ==================================================================== */
const CASTING = {
  id: 'casting',
  name: '压铸 · 开机',
  subtitle: '今日早班 · 环境漂移',
  rewardName: '良品率',
  rewardUnit: '%',
  rewardMax: 98.5,
  noiseSigma: 0.25,
  scrapPenalty: 6.0,
  // 同上,与 server/sandbox.py 一致。原手写 sf=1.1 让 90% 区间覆盖率变成 100%
  // (离散只有 0.58),σ 宽了近一倍 —— EI 的探索项在花批次买不存在的不确定性。
  theta: { length: 0.35, sf: 0.8, sn: 0.28 },
  plane: [1, 2], // melt temp x fast-shot speed

  params: [
    { name: '熔料温度', unit: 'C', lo: 610, hi: 650, range: 40, decimals: 0 },
    { name: '模具温度', unit: 'C', lo: 180, hi: 240, range: 60, decimals: 0 },
    { name: '快速压射速度', unit: 'm/s', lo: 1.5, hi: 4.0, range: 2.5, decimals: 2 },
    { name: '压射切换点', unit: 'mm', lo: 120, hi: 220, range: 100, decimals: 0 },
    { name: '保压时间', unit: 's', lo: 2, hi: 10, range: 8, decimals: 1 },
  ],

  redLines: [
    // id 与后端 sandbox.py 对齐:冷隔走 li 槽,卷气走 gas 槽。
    // 前端 drawHeat 按 id 取风险,名字对不上就画不出线。
    { id: 'li', label: '冷隔线', desc: '熔/模温双低 → 冷隔' },
    { id: 'gas', label: '卷气线', desc: '快压超 2.5 m/s → 卷气' },
    { id: 'yield', label: '良率达标线', desc: '≥ 96% 才放行' },
  ],

  // 这台机的模温表偏高 3℃:设定 200,实际只有 197。
  // "我们那台机模温表偏高" 这句话的价值,就是把这 3℃ 找回来。
  deviceBias: [0, 3, 0, 0, 0],
  actual(x) { return x.map((v, i) => v - this.deviceBias[i]); },
  // 早班开机的保守起点(低模温 + 偏快压射),离良率峰很远
  baseStart() { return [[618, 188, 2.35, 140, 3]]; },
  injStart()  { return [[618, 188, 2.35, 140, 3]]; },

  _feasible(x) {
    if (x[2] > 2.5) return false;                // 卷气 hard wall
    if (x[0] < 615 && x[1] < 190) return false;  // 冷隔
    return true;
  },
  _gasRisk(x)  { return cl((x[2] - 2.4) / 0.3, 0, 1); },
  // 冷隔 = 熔温与模温**同时**偏低。熔温够高时低模温只是表面质量问题,
  // 模温够高时低熔温也还能补 —— 所以是两个"低门"的乘积。
  _coldRisk(x) { return loGate(x[0], 618, 6) * loGate(x[1], 195, 12); },

  feasible(x) { return this._feasible(this.actual(x)); },
  gasRisk(x)  { return this._gasRisk(this.actual(x)); },
  coldRisk(x) { return this._coldRisk(this.actual(x)); },
  // 压铸场景里冷隔占 li 槽 —— 与后端 _risks 的键一致
  liRisk(x)   { return this.coldRisk(x); },
  noiseAt(x)  { return 1; },
  biasAt(x)   { return 0; },
  reward(x)   { return this._reward(this.actual(x)); },
  risks(x)    { return { li: this.liRisk(x), gas: this.gasRisk(x) }; },
  observe(x, rng) { return observeWith(this, x, rng); },

  _reward(x) {
    const [Tm, Td, v, sw, tp] = x;
    const tm = hill(Tm, 635, 12);
    const td = hill(Td, 215, 25);
    const vR = cl(1 - Math.max(0, v - 2.2) / 0.6, 0.7, 1);
    const swR = hill(sw, 180, 50);
    const tpR = cl(tp / 6, 0.85, 1);
    let y = 96.5 + 0.9 * (tm - 0.75) + 0.7 * (td - 0.7) + 1.0 * (vR - 0.5) + 0.4 * (swR - 0.8) + 0.3 * (tpR - 0.9);
    // 同理:x 已是实际值,走下划线版本
    if (this._gasRisk(x) > 0.75) y -= 9;
    if (this._coldRisk(x) > 0.7) y -= 7;
    return cl(y, 82, 98.5);
  },

  // 同上:只留卡面,先验在 priors_ir.js
  cards: [
    { id: 'p1', narration: '快压顶到 2.5 就收到——您这句把卷气区整个划掉了' },
    { id: 'p2', narration: '模温守到 200 以上，冷隔就没机会' },
    { id: 'p3', narration: '模温表偏高三度——这句话把别人的坐标搬正了' },
  ],
  settleMixin: {
    noun: '模次',
    line: '一模次 = 一台机的料 + 能耗 + 开机废件',
    scrapUnit: '模次',
  },
};

/* Hi-fi narration — one line per observed point, shown as it "runs". */
function narrationOf(scenario, point, idx, state) {
  // (implemented in app.js using scenario + state; kept here for schema clarity)
  return { x: point.x.slice(), y: point.y, tag: point.tag, s: state };
}