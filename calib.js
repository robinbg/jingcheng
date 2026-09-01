/* calib.js — ③ 标定页「孪生从哪来」。
 *
 * 这一页存在的唯一理由:评委看完沙盘之后必然要问的那句话是"你这孪生凭什么算
 * 得准"。前两幕答不了 —— 收敛曲线是统计口吻,机理视窗是定性口吻,两者都只能
 * 说"我的规则长这样",说不出"我的规则凭什么"。
 *
 * 所以这页不讲故事,只交三样东西:
 *   1) 机理骨架 —— 每一条规律都是公开教科书级的定性事实,并指名沙盘里**哪个
 *      函数**落实了它。规律可以查证,实现可以点开,两头都不靠信任。
 *   2) 特征自检 —— 清单上的勾**不是写死的**,是打开这页时当场跑沙盘算出来的。
 *      写死的勾等于自己给自己发奖状;当场算的勾,错了就会在评委面前变成叉。
 *      这一条是整页的分量所在,也是唯一值得多花那几十行的地方。
 *   3) 路线声明 —— 现在没有接实测数据,就说没有。公开数据集只写成**下一步的
 *      锚点**,并且写清它和化成首效的口径差在哪。
 *
 * 一条红线,写在最前面免得后人"顺手补强":**不叠任何文献曲线**。把别人论文里
 * 的曲线画到我们的图上做"拟合得很好"的暗示,是这个作品里最便宜也最致命的一步
 * —— 我们没做过那件事,做过的事已经够说了。
 */
'use strict';

/* 机理骨架:公开定性规律 → 沙盘里落实它的函数名。
 * 只收**教科书级**的定性事实(方向、单调性、耦合形状),不收任何带具体数值的
 * 论文结论 —— 数值量级一律标为公开估算,不冒充实测。 */
const CALIB_SKELETON = {
  formation: [
    ['磷酸铁锂是两相反应,充电中段是电压平台而非斜坡', 'twin.js · twinOcv()'],
    ['首充电流密度越大,不可逆锂损失越多,SEI 越疏松;量程内无平台',
      'scenarios.js · seiQuality()'],
    ['温度越低,离子输运越慢,同电流下极化压差越大', 'twin.js · twinPolar()'],
    ['低温与大倍率**共同**把负极电位压到 0V vs Li 以下才镀锂 —— 所以析锂线是斜边',
      'scenarios.js · platingOnset() / twin.js · twinAnodeMargin()'],
    ['高温 + 长时老化促进副反应产气', 'scenarios.js · _gasRisk()'],
    ['老化不到位时内阻/首效读数系统性偏高,且离散更大',
      'scenarios.js · _biasAt() / _noiseAt()'],
    ['越过工艺红线不是"分数低一点",是整托判废', 'scenarios.js · observeWith()'],
  ],
  casting: [
    ['熔温与模温**同时**偏低才冷隔:任一足够高都能补', 'scenarios.js · _coldRisk()'],
    ['快压速度越过临界值卷气,是硬墙不是渐变', 'scenarios.js · _gasRisk()'],
    ['良率对熔温/模温是单峰响应,过高过低都掉', 'scenarios.js · _reward()'],
    ['仪表偏差使设定值与实际落到工件上的值不同', 'scenarios.js · actual()'],
  ],
};

/* 特征自检:每一条都是**当场跑沙盘**算出来的。
 *
 * 这是整页唯一有分量的部分,所以纪律也最严:
 *   · 每条只问沙盘的**公开接口**(feasible / reward / liRisk / biasAt / twinCurve),
 *     不复述任何阈值公式。复述一遍就是第二份实现,而这个项目已经因为"某处自己
 *     推一遍析锂阈值"栽过一次:同一批在沙盘可行、在视窗被画成报废。
 *   · 判据是**定性方向**(单调、单峰、有平台、有耦合),不是"等于某个数"。
 *     数值等式会因为一次调参全线飘红,而飘红的原因与"孪生像不像"无关。
 *   · 结果里带上实测到的数,即使它通过了。评委真正会读的是那个数,不是那个勾。
 *
 * 每条返回 { ok, got }:ok 决定勾还是叉,got 是当场读到的数。
 * 一条都不许写成 `ok: true`。
 */
const CALIB_CHECKS = {
  formation: [
    {
      title: '充电中段是电压平台,不是斜坡',
      expect: '平台段斜率 < 入口段的 1/5',
      run(scene) {
        // 只问 twinOcv:平台的形状是它一个人的事
        const dv = (a, b) => (twinOcv(b) - twinOcv(a)) / (b - a);
        const inlet = dv(0.00, 0.04), plateau = dv(0.20, 0.80);
        return {
          ok: plateau > 0 && plateau < inlet / 5,
          got: `入口 ${inlet.toFixed(2)} V/SOC · 平台 ${plateau.toFixed(3)} V/SOC`,
        };
      },
    },
    {
      title: '首充越快,SEI 越疏松 —— 量程内无平台',
      expect: '全量程单调下降,且没有任何一段是平的',
      run(scene) {
        const lo = scene.params[0].lo, hi = scene.params[0].hi;
        let mono = true, flat = 0;
        let prev = seiQuality(lo);
        for (let i = 1; i <= 40; i++) {
          const q = seiQuality(lo + (hi - lo) * i / 40);
          if (q > prev + 1e-12) mono = false;
          if (Math.abs(q - prev) < 1e-6) flat++;
          prev = q;
        }
        return {
          ok: mono && flat === 0,
          got: `${lo.toFixed(2)}C→${seiQuality(lo).toFixed(2)} · `
            + `${hi.toFixed(2)}C→${seiQuality(hi).toFixed(2)} · 平段 ${flat}/40`,
        };
      },
    },
    {
      title: '析锂线是斜边,不是直角',
      expect: '起始温度随倍率单调上移,跨量程抬升 > 10℃',
      run(scene) {
        const p = scene.params[0];
        const at = a => scene.mechLine.at(scene, a);
        let mono = true;
        for (let i = 1; i <= 20; i++) {
          if (at(p.lo + (p.hi - p.lo) * i / 20) <= at(p.lo + (p.hi - p.lo) * (i - 1) / 20)) {
            mono = false;
          }
        }
        const rise = at(p.hi) - at(p.lo);
        return {
          ok: mono && rise > 10,
          got: `${p.lo.toFixed(2)}C→${at(p.lo).toFixed(1)}℃ · `
            + `${p.hi.toFixed(2)}C→${at(p.hi).toFixed(1)}℃ · 抬升 ${rise.toFixed(1)}℃`,
        };
      },
    },
    {
      title: '低温 × 大倍率才判废;单独低温或单独大倍率不判废',
      expect: '耦合成立 —— 三个对照点里只有"双高"那个判废',
      run(scene) {
        // 直接问 scene.feasible。这条检验的正是"耦合"而不是"两条独立红线"
        const at = (pre, T) => {
          const x = scene.params.map(q => (q.lo + q.hi) / 2);
          x[0] = pre; x[1] = T;
          return scene.feasible(x);
        };
        const cold = at(0.06, 26), fast = at(0.46, 52), both = at(0.46, 26);
        return {
          ok: cold && fast && !both,
          got: `0.06C/26℃ ${cold ? '可行' : '判废'} · 0.46C/52℃ ${fast ? '可行' : '判废'}`
            + ` · 0.46C/26℃ ${both ? '可行' : '判废'}`,
        };
      },
    },
    {
      title: '慢充买的是 SEI,代价落在预充段时长上',
      expect: '预充段时长差 > 8 倍,而总工时差 < 2 倍',
      run(scene) {
        // 这条要证的是"慢充的代价不在总工时" —— 台上那句解说词的凭据
        const one = pre => {
          const x = scene.params.map(q => (q.lo + q.hi) / 2);
          x[0] = pre; x[1] = 40;
          const c = twinCurve(x, scene);
          return { pre: c.swAt ? c.swAt.t : 0, all: c.hours };
        };
        const slow = one(0.06), fast = one(0.46);
        const rp = slow.pre / Math.max(fast.pre, 1e-9);
        const ra = (slow.all || TWIN_XMAX) / Math.max(fast.all || TWIN_XMAX, 1e-9);
        return {
          ok: rp > 8 && ra < 2,
          got: `预充 ${slow.pre.toFixed(2)}h vs ${fast.pre.toFixed(2)}h(${rp.toFixed(0)}×)`
            + ` · 总工时 ${(slow.all || 0).toFixed(1)}h vs ${(fast.all || 0).toFixed(1)}h(${ra.toFixed(2)}×)`,
        };
      },
    },
    {
      title: '老化不到位:读数假高,且更散',
      expect: '偏置 > 0 且异方差倍数 > 1,老化足够时两者都归零',
      run(scene) {
        const at = age => {
          const x = scene.params.map(q => (q.lo + q.hi) / 2);
          x[4] = age;
          return { b: scene.biasAt(x), n: scene.noiseAt(x) };
        };
        const short = at(6), long = at(48);
        return {
          ok: short.b > 0 && short.n > 1 && Math.abs(long.b) < 1e-9
            && Math.abs(long.n - 1) < 1e-9,
          got: `6h 偏置 +${short.b.toFixed(2)}${scene.rewardUnit} 离散 ×${short.n.toFixed(2)}`
            + ` · 48h 偏置 ${long.b.toFixed(2)} 离散 ×${long.n.toFixed(2)}`,
        };
      },
    },
    {
      title: '温度对首效是单峰,不是越高越好',
      expect: '量程内存在内点极大,两端都低于它',
      run(scene) {
        const p = scene.params[1];
        let best = -Infinity, bx = null;
        const at = T => {
          const x = scene.params.map(q => (q.lo + q.hi) / 2);
          x[1] = T;
          return scene.reward(x);
        };
        for (let i = 0; i <= 60; i++) {
          const T = p.lo + (p.hi - p.lo) * i / 60, y = at(T);
          if (y > best) { best = y; bx = T; }
        }
        const inner = bx > p.lo + 1e-9 && bx < p.hi - 1e-9;
        return {
          ok: inner && at(p.lo) < best - 1e-6 && at(p.hi) < best - 1e-6,
          got: `峰在 ${bx.toFixed(0)}℃ → ${best.toFixed(2)}${scene.rewardUnit}`
            + ` · 两端 ${at(p.lo).toFixed(2)} / ${at(p.hi).toFixed(2)}`,
        };
      },
    },
    {
      title: '越过红线是整托判废,不是扣几分',
      expect: '判废点的读数比可行点低一整个报废罚',
      run(scene) {
        // 用固定种子的 observe,不用 reward —— 判废罚加在观测那一层
        const mk = (pre, T) => {
          const x = scene.params.map(q => (q.lo + q.hi) / 2);
          x[0] = pre; x[1] = T; return x;
        };
        const rng = () => 0.5;   // 固定分位,只为把噪声那一项定住
        const okX = mk(0.06, 40), badX = mk(0.46, 26);
        const gap = scene.observe(okX, rng) - scene.observe(badX, rng);
        return {
          ok: !scene.feasible(badX) && gap > scene.scrapPenalty * 0.9,
          got: `落差 ${gap.toFixed(2)}${scene.rewardUnit} · 报废罚 ${scene.scrapPenalty.toFixed(1)}`,
        };
      },
    },
  ],
  casting: [
    {
      title: '冷隔要熔温与模温同时低',
      expect: '双低判废,任一足够高即可行',
      run(scene) {
        const at = (Tm, Td) => {
          const x = scene.params.map(q => (q.lo + q.hi) / 2);
          x[0] = Tm; x[1] = Td; x[2] = 2.0;
          return scene.feasible(x);
        };
        const both = at(612, 184), hotMelt = at(640, 184), hotDie = at(612, 230);
        return {
          ok: !both && hotMelt && hotDie,
          got: `612/184 ${both ? '可行' : '判废'} · 640/184 ${hotMelt ? '可行' : '判废'}`
            + ` · 612/230 ${hotDie ? '可行' : '判废'}`,
        };
      },
    },
    {
      title: '卷气是硬墙,不是渐变',
      expect: '临界速度两侧一格之内翻转可行性',
      run(scene) {
        const at = v => {
          const x = scene.params.map(q => (q.lo + q.hi) / 2);
          x[2] = v; return scene.feasible(x);
        };
        let lo = scene.params[2].lo, hi = scene.params[2].hi;
        if (!at(lo) || at(hi)) return { ok: false, got: '量程内找不到翻转' };
        for (let i = 0; i < 40; i++) { const md = (lo + hi) / 2; if (at(md)) lo = md; else hi = md; }
        return { ok: hi - lo < 0.01, got: `墙在 ${lo.toFixed(3)} m/s,过渡带宽 ${(hi - lo).toExponential(1)}` };
      },
    },
    {
      title: '良率对模温是单峰',
      expect: '量程内存在内点极大',
      run(scene) {
        const p = scene.params[1];
        let best = -Infinity, bx = null;
        const at = Td => {
          const x = scene.params.map(q => (q.lo + q.hi) / 2);
          x[1] = Td; x[2] = 2.0; return scene.reward(x);
        };
        for (let i = 0; i <= 60; i++) {
          const T = p.lo + (p.hi - p.lo) * i / 60, y = at(T);
          if (y > best) { best = y; bx = T; }
        }
        return {
          ok: bx > p.lo + 1e-9 && bx < p.hi - 1e-9 && at(p.lo) < best && at(p.hi) < best,
          got: `峰在 ${bx.toFixed(0)}℃ → ${best.toFixed(2)}${scene.rewardUnit}`
            + ` · 两端 ${at(p.lo).toFixed(2)} / ${at(p.hi).toFixed(2)}`,
        };
      },
    },
    {
      title: '仪表偏差让设定值 ≠ 实际值',
      expect: '模温设定 200 时实际低 3℃,而其余维不偏',
      run(scene) {
        const x = scene.params.map(q => (q.lo + q.hi) / 2);
        x[1] = 200;
        const a = scene.actual(x);
        const others = x.every((v, i) => i === 1 || Math.abs(a[i] - v) < 1e-9);
        return {
          ok: Math.abs(a[1] - 197) < 1e-9 && others,
          got: `设定 200℃ → 实际 ${a[1].toFixed(0)}℃ · 其余维零偏差 ${others ? '是' : '否'}`,
        };
      },
    },
  ],
};

/* 路线声明。这一段是**文字**而不是图,是刻意的:一旦画成图,就会有人把公开
 * 数据集的曲线叠到我们的曲线上去暗示"拟合得很好"。我们没做过那件事。
 *
 * 公开数据集只写成下一步的锚点,并且必须写清口径差 —— 循环寿命数据标定不了
 * 化成首效,这一点自己先说出来,比等评委指出来强得多。 */
const CALIB_ROUTE = [
  ['现在这一版', '孪生 = 公开定性机理规则 + 公开量级估算,**无实测数据参与**。'
    + '页面上每个数都能追到左边那张骨架表里的某个函数。'],
  ['为什么先这样', '标定要的是配方-结果配对数据,一条化成线攒够一批要数周。'
    + '而我们要证的是"经验能不能被翻译成先验",这件事在定性正确的孪生上就能证 ——'
    + '两田同起点、同 oracle、唯一差异是那句话。'],
  ['下一步锚点', '公开电池数据集(如 Severson 2019 快充循环寿命)可作为 PoC 阶段的'
    + '标定锚点。口径差要先说清:那批数据标的是循环寿命,不是化成首效,'
    + '只能校"倍率-衰减"这一族的方向与量级,校不了首效的绝对值。'],
  ['接厂里数据时', '机理骨架不变,换的是三样:reward 的系数、噪声/偏置的量级、'
    + '红线的位置。前端和先验编译器一行都不用改 —— 这是"一份编译器两个解释器"'
    + '那套结构本来就该有的收益。'],
];

/* ---------- 渲染 ----------
 * 静态一帧,打开时算一次。不做动画 —— 同屏单动的规矩在这页也算数。 */
function renderCalib(sceneId) {
  const scene = SCENES[sceneId];
  const skel = CALIB_SKELETON[sceneId] || [];
  const checks = CALIB_CHECKS[sceneId] || [];

  $id('calibScene').textContent = scene.name;
  $id('calibSkel').innerHTML = skel.map(([law, where]) => `
    <li class="ck-row">
      <span class="ck-law"></span>
      <span class="ck-where"></span>
    </li>`).join('');
  // 文本走 textContent 塞回去:卡面和函数名里有 · / 括号,拼进 HTML 迟早出事
  document.querySelectorAll('#calibSkel .ck-row').forEach((li, i) => {
    // 与 CALIB_ROUTE 同一条渲染路径:只认 ** 这一种记号,用 createTextNode
    // 而不是 innerHTML —— 机理文案里有 · 和括号,拼进 HTML 迟早被咬一口。
    const law = li.querySelector('.ck-law');
    skel[i][0].split('**').forEach((seg, k) => {
      if (k % 2 === 0) { law.appendChild(document.createTextNode(seg)); return; }
      const b = document.createElement('strong');
      b.textContent = seg;
      law.appendChild(b);
    });
    li.querySelector('.ck-where').textContent = skel[i][1];
  });

  let pass = 0;
  $id('calibChecks').innerHTML = checks.map(() => `
    <li class="cq-row">
      <span class="cq-mark"></span>
      <div class="cq-body">
        <span class="cq-title"></span>
        <span class="cq-expect"></span>
        <span class="cq-got"></span>
      </div>
    </li>`).join('');
  document.querySelectorAll('#calibChecks .cq-row').forEach((li, i) => {
    const c = checks[i];
    let r;
    // 自检本身也可能抛 —— 抛了就是叉,不是白屏。这页的价值全在"当场算",
    // 那就得连"当场算崩了"这种结果也如实显示。
    try { r = c.run(scene); } catch (e) { r = { ok: false, got: '自检异常:' + e.message }; }
    if (r.ok) pass++;
    li.classList.add(r.ok ? 'cq-ok' : 'cq-bad');
    li.querySelector('.cq-mark').textContent = r.ok ? '✓' : '✗';
    li.querySelector('.cq-title').textContent = c.title;
    li.querySelector('.cq-expect').textContent = '判据:' + c.expect;
    li.querySelector('.cq-got').textContent = '当场读到:' + r.got;
  });
  $id('calibScore').textContent = `${pass}/${checks.length} 条通过`;
  $id('calibScore').className = 'calib-score '
    + (pass === checks.length ? 'cs-ok' : 'cs-bad');

  $id('calibRoute').innerHTML = CALIB_ROUTE.map(() => `
    <div class="cr-row"><span class="cr-k"></span><span class="cr-v"></span></div>`).join('');
  document.querySelectorAll('#calibRoute .cr-row').forEach((d, i) => {
    d.querySelector('.cr-k').textContent = CALIB_ROUTE[i][0];
    // ** 包起来的那一段加粗。奇数段落在 ** 之间 —— 只认这一种记号,不引
    // Markdown 解析器:这页的文案是我们自己写的常量,不是用户输入。
    // 仍然走 createTextNode 而不是拼 innerHTML —— 文案里有 / 和括号,
    // 拼字符串的写法迟早会被某个字符咬一口,而这页正是要显得可靠的那一页。
    const v = d.querySelector('.cr-v');
    CALIB_ROUTE[i][1].split('**').forEach((seg, k) => {
      const t = document.createTextNode(seg);
      if (k % 2 === 0) { v.appendChild(t); return; }
      const b = document.createElement('strong');
      b.appendChild(t);
      v.appendChild(b);
    });
  });
}

