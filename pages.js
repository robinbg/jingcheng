/* pages.js — 三个旁证页的渲染:慢演一遍 / 三方对照 / 关于。
 *
 * 这三页都**不在 60 秒主链路上**。主链路只给三样东西:热力图收缩、双田赛跑、
 * 三个结算数字(§5A 台前/台后)。这三页是评委看完之后开始问的那几句 ——
 * 放在页脚,主链路走完了再往这儿点。
 *
 * 共同纪律:**它们不另算一份数据。**
 *   慢演   读 state.history.injected.history[i] 的 mu/sigma/ei/risks
 *   三方   注入田/基准田直接用 state.history,打乱那条现跑一次(同种子同批数)
 *   关于   纯静态,HTML 里写完了,这里一行都不用
 *
 * 为什么这条纪律要写在文件头:回放时屏幕上那条曲线是一份数,慢演页要是自己
 * 再跑一遍 BO 拿另一份数,两处就会不一致 —— 而慢演页存在的全部意义就是
 * "把刚才那条轨迹停下来看清楚"。它一旦另算,就成了第二场演示。
 */
'use strict';

/* ================================================================
 * 一 · 慢演一遍
 * ================================================================ */
const SLOW = { i: 0 };

function slowRun() {
  const h = state.history && state.history.injected;
  return h && h.history && h.history.length ? h : null;
}

/* 一批的参数读数,按各维自己的小数位与单位念。 */
function xLine(scene, x) {
  return scene.params.map((p, i) => p.name + ' ' + x[i].toFixed(p.decimals) + (p.unit || ''))
    .join(' · ');
}

/* 上一批 / 本批两栏。版式必须一样 —— 版式一变,眼睛就以为数也换了口径。 */
function slowBox(el, scene, h) {
  el.innerHTML = '';
  if (!h) {
    const p = document.createElement('p');
    p.className = 'sd-none';
    p.textContent = '这是第一批 —— 它是起点,没有"上一批"可比。起点两田相同,唯一的差异是那句话。';
    el.appendChild(p);
    return;
  }
  const rows = [
    ['参数', xLine(scene, h.x), false],
    ['读数', h.y.toFixed(2) + scene.rewardUnit + (h.feasible ? '' : '（这一' + scrapUnit(scene) + '报废）'), !h.feasible],
    ['当前最优', h.bestSoFar.toFixed(2) + scene.rewardUnit, false],
  ];
  for (const [k, v, bad] of rows) {
    const d = document.createElement('div');
    d.className = 'sd-kv';
    const ke = document.createElement('span'); ke.className = 'sd-k'; ke.textContent = k;
    const ve = document.createElement('span');
    ve.className = 'sd-v' + (bad ? ' sd-scrap' : '');
    ve.textContent = v;
    d.appendChild(ke); d.appendChild(ve);
    el.appendChild(d);
  }
}

/* 五维参数条。**五维都画** —— 热力图只画两维,余下三维只在这儿看得见,
 * 而"这句话收窄的是图上看不见的那几维"正是最容易被当成"什么都没发生"的
 * 情形(实测九张卡里有三张是这样)。 */
function slowParams(scene, h) {
  const wrap = $id('slowParams');
  wrap.innerHTML = '';
  const b = (state.card && state.card.prior && state.card.prior.bounds) || null;
  let narrowed = 0;
  scene.params.forEach((p, i) => {
    const row = document.createElement('div'); row.className = 'ps-row';
    const k = document.createElement('span'); k.className = 'ps-k';
    k.textContent = p.name; k.title = p.name;
    const track = document.createElement('div'); track.className = 'ps-track';
    const span = p.hi - p.lo;
    // 玫红段**只在这一维真的收窄了**才画。铺满整条的玫红段是最坏的一种图:
    // 它在说"这句话把搜索域收到了这里",而那个"这里"就是原始窗口本身 ——
    // 实测九张卡里有四张一维都没收窄(cold 是挖角,temp/age/p1 两样都不动),
    // 于是五条轨道全被染成玫红,页面上写着"收窄后的搜索域"。这跟主舞台那行
    // 说明曾经犯过的错是同一个:字说收窄了,图纹丝不动。
    if (b && b[i]) {
      const lo = Math.max(p.lo, b[i].lo), hi = Math.min(p.hi, b[i].hi);
      if (lo > p.lo + 1e-9 || hi < p.hi - 1e-9) {
        narrowed++;
        const win = document.createElement('span'); win.className = 'ps-win';
        win.style.left = ((lo - p.lo) / span * 100).toFixed(2) + '%';
        win.style.width = (Math.max(0, hi - lo) / span * 100).toFixed(2) + '%';
        win.title = '这句话收窄后的搜索域';
        track.appendChild(win);
      }
    }
    const dot = document.createElement('span'); dot.className = 'ps-dot';
    dot.style.left = ((h.x[i] - p.lo) / span * 100).toFixed(2) + '%';
    track.appendChild(dot);
    const v = document.createElement('span'); v.className = 'ps-v';
    v.textContent = h.x[i].toFixed(p.decimals) + (p.unit || '');
    row.appendChild(k); row.appendChild(track); row.appendChild(v);
    wrap.appendChild(row);
  });

  // 一条随卡变的说明。**照实说这句话对搜索域做了什么**,做不到的不说 ——
  // 与主舞台 heatNoteFor 同一条纪律,而且必须与上面画出来的东西一致:
  // 一条玫红段都没画时,这行就得解释为什么没画,不能让静态文案替它撒谎。
  const p = document.createElement('p');
  p.className = 'sd-none';
  const excl = (state.card && state.card.prior && state.card.prior.exclusions) || [];
  p.textContent = narrowed
    ? `玫红段是这句话真正收窄的 ${narrowed} 维 —— 其余几维它没有意见,灰条就是原始工艺窗口。`
    : (excl.length
      // 这一段走 textContent,不是 slowReason 那条 ⟦…⟧/** 渲染路径 ——
      // 所以这里一个记号都不能写,写了就原样印在屏幕上。
      ? '这句话一维的区间都没收窄:它是在框内划掉一块(搜索空间图上那片斜纹区)—— '
        + '所以五条灰条都还是原始窗口,少掉的那块在二维图上才看得见。'
      : '这句话没有动搜索域 —— 它改的是先验形状、读数可信度或评估成本。'
        + '五条灰条都是原始窗口,它的作用在结算页那五个小数里。');
  wrap.appendChild(p);
}

/* 本轮推理四步。每一步都必须挂一个**这一批实际算过的数** ——
 * 挂不上真数的那一步就不写,宁可只有三步,也不要一句没有出处的解说。
 * 返回 [文本, 是否报废行] 的数组;文本里 ⟦…⟧ 包住的段落染成等宽金色。 */
function slowReason(scene, h, prev) {
  const out = [];
  // 起点那一批**没有预测可言**:此刻 GP 手里零个观测,predict() 直接返回
  // (先验偏移, 核幅度) —— 那个 mu 不是"孪生预计能拿到多少首效",它是先验
  // 在这一点上的偏移量,对着 ~88% 的量纲念出来就是屏幕上那句
  // "预测是 -0.00 ± 1.00%"。可信度自证那一栏是对的:calibrationAudit 里
  // `if (h.tag !== 'bo') continue` 就是同一条判断("warm start 没有真正的
  // 预测可言")。同一份数,两处不能一处当预测念、一处当噪声扔。
  if (h.tag === 'bo') {
    out.push(['观测**前**,孪生对这一点的预测是 ⟦' + h.mu.toFixed(2) + ' ± ' + h.sigma.toFixed(2)
      + scene.rewardUnit + '⟧ —— 这个区间是在看到读数之前记下的,所以它天然是留出验证。', false]);
  } else {
    out.push(['这一批之前孪生手里**一个观测都没有**,所以它给不出预测 —— '
      + '第一批的读数是这条曲线的原点,可信度自证那一栏也把起点批次排除在外,'
      + '因为拿"还没学过"的时候去算它报的不确定度准不准,是在冤枉它。', false]);
  }
  if (Math.abs(h.prior) > 1e-6) {
    out.push(['那句话在这一点上把先验均值推了 ⟦' + (h.prior > 0 ? '+' : '') + h.prior.toFixed(3)
      + '⟧ —— 它只改"往哪儿多试",不改真实响应面。', false]);
  } else {
    out.push(['那句话在这一点上的先验贡献是 ⟦0.000⟧ —— 这一点不在它说的范围里,'
      + '它对这儿没有意见。', false]);
  }
  if (h.tag === 'bo') {
    out.push(['采集函数给这一点的收益是 ⟦' + h.ei.toFixed(4) + '⟧,评估成本倍率 ⟦'
      + h.cost.toFixed(2) + '⟧ —— 池子里它的"收益/成本"最高,所以选它。', false]);
  } else {
    out.push(['这一批是**起点**,不由采集函数挑 —— 两田同起点,唯一的差异是那句话。', false]);
  }
  if (!h.feasible) {
    const r = h.risks || {};
    const worst = (r.li || 0) >= (r.gas || 0) ? scene.redLines[0] : scene.redLines[1];
    out.push(['撞了' + worst.label + '(' + worst.desc + '),这一' + scrapUnit(scene)
      + '报废。**优化器事先并不知道这条线在哪** —— 它得自己撞、自己学,'
      + '这正是安全类经验省下来的东西。', true]);
  } else {
    const up = prev ? h.bestSoFar - prev.bestSoFar : 0;
    out.push(['读数 ⟦' + h.y.toFixed(2) + scene.rewardUnit + '⟧ 落在可行域内,'
      + (up > 1e-6
        ? '把当前最优往上推了 ⟦+' + up.toFixed(2) + scene.rewardUnit + '⟧。'
        : '没有推动当前最优(仍是 ⟦' + h.bestSoFar.toFixed(2) + scene.rewardUnit + '⟧)—— '
          + '连续几批不动就判收敛,停在哪一批不是拍板出来的。'), false]);
  }
  return out;
}

/* 三条红线的逐批读数。**一个阈值都不在这儿写** —— riskLevels/riskLabel 都在
 * app.js,与主舞台那三条计是同一份判断。抄一套过来就会出现"同一批数据,主舞台
 * 说安全、慢演说中风险",而这两页恰恰是评委会对着看的两页。
 * 复用主舞台的 .meter 那套 class,样式不另写;id 不复用(meterLi 那几个是
 * 主舞台专有的,慢演这儿现生成节点)。 */
function slowMeters(scene, h) {
  const wrap = $id('slowMeters');
  if (!wrap) return;
  wrap.innerHTML = '';
  const vals = riskLevels(scene, h);
  const keys = [['li', 0], ['gas', 1], ['yield', 2]];
  for (const [k, li] of keys) {
    const row = document.createElement('div');
    row.className = 'meter';
    row.dataset.line = k;
    const nm = document.createElement('span');
    nm.className = 'meter-name';
    nm.textContent = scene.redLines[li].label;
    nm.title = scene.redLines[li].desc || '';
    const bg = document.createElement('div'); bg.className = 'meter-bg';
    const fill = document.createElement('div'); fill.className = 'meter-fill';
    fill.style.width = (vals[k] * 100).toFixed(0) + '%';
    bg.appendChild(fill);
    const lv = document.createElement('span');
    const { cls, txt } = riskLabel(k, h ? vals[k] : null);
    lv.className = 'meter-lv ' + cls;
    lv.textContent = txt;
    row.appendChild(nm); row.appendChild(bg); row.appendChild(lv);
    wrap.appendChild(row);
  }
}

function renderSlow(i) {
  const run = slowRun();
  const scene = SCENES[state.scene];
  $id('slowScene').textContent = ' · ' + scene.name;
  if (!run) {
    $id('slowNow').textContent = '—';
    $id('slowTot').textContent = '/—';
    $id('slowBar').style.width = '0%';
    slowBox($id('slowPrevBox'), scene, null);
    $id('slowNowBox').innerHTML = '';
    const p = document.createElement('p');
    p.className = 'sd-none';
    p.textContent = '还没有跑过经验田 —— 先在主舞台抽一张经验卡,或说一句自己的经验并注入,再回来看。';
    $id('slowNowBox').appendChild(p);
    $id('slowParams').innerHTML = '';
    $id('slowReason').innerHTML = '';
    // 空态也要重画:不画的话上一次跑完留下的三条读数会挂在这儿,
    // 而上面的批次计数器已经回到"—",两处对不上。
    slowMeters(scene, null);
    return;
  }
  const hist = run.history;
  SLOW.i = Math.max(0, Math.min(i, hist.length - 1));
  const h = hist[SLOW.i], prev = SLOW.i > 0 ? hist[SLOW.i - 1] : null;

  $id('slowNow').textContent = String(SLOW.i + 1).padStart(2, '0');
  $id('slowTot').textContent = '/' + String(hist.length).padStart(2, '0');
  $id('slowBar').style.width = ((SLOW.i + 1) / hist.length * 100).toFixed(1) + '%';
  $id('slowPrev').disabled = SLOW.i === 0;
  $id('slowNext').disabled = SLOW.i >= hist.length - 1;

  slowBox($id('slowPrevBox'), scene, prev);
  slowBox($id('slowNowBox'), scene, h);
  slowParams(scene, h);
  slowMeters(scene, h);

  const ol = $id('slowReason');
  ol.innerHTML = '';
  for (const [txt, scrap] of slowReason(scene, h, prev)) {
    const li = document.createElement('li');
    li.className = 'rs-row' + (scrap ? ' rs-scrap' : '');
    const t = document.createElement('span'); t.className = 'rs-t';
    // ⟦…⟧ 里的数染成等宽金色,** 之间加粗。两种记号都只认自己那一种 ——
    // 这些文案是我们自己拼的常量,不引 Markdown 解析器。
    txt.split(/(⟦[^⟧]*⟧|\*\*[^*]+\*\*)/).forEach(seg => {
      if (!seg) return;
      if (seg.startsWith('⟦')) {
        const s = document.createElement('span'); s.className = 'rs-num';
        s.textContent = seg.slice(1, -1); t.appendChild(s);
      } else if (seg.startsWith('**')) {
        const b = document.createElement('strong');
        b.textContent = seg.slice(2, -2); t.appendChild(b);
      } else {
        t.appendChild(document.createTextNode(seg));
      }
    });
    li.appendChild(t);
    ol.appendChild(li);
  }
}

/* ================================================================
 * 二 · 三方对照(消融)
 *
 * 这页回答全场最难的一问:**"你怎么证明省批次是翻译的功劳,不是随便加个
 * 先验都能省?"**
 *
 * 答案只能是消融:拿同一句话的**维度打乱版**当第三条曲线 —— 同样的强度、
 * 同样的 op、同样的置信度,只是接到了错的维度上。它一样是一份先验,一样收窄
 * 了搜索域,但它不该省批次。这一栏在,"你就是给了个更小的搜索域"这句话才驳
 * 不倒我们。
 *
 * 打乱那份 IR **不在前端生成**。它由 server/export_ir.py 调 prior_dsl.shuffle_dims
 * 在编译期导出(与 test_align.py 的消融用同一个函数、同一颗种子),随 priors_ir.js
 * 一起发过来。这样 file:// 下这页也能真跑,而浏览器照旧一行先验都没编译。
 *
 * 三条曲线的口径必须逐项对齐,否则这页反而成了新的把柄:
 *   真翻译   = 台上刚跑完那一条(state.history.injected),seedNow / 20 批 / injStart
 *   无先验   = 台上左边那一条(state.history.baseline),seedNow / 24 批 / baseStart
 *   维度打乱 = 这里现跑,seedNow / 20 批 / injStart —— 与真翻译**逐项相同**
 * 前两条直接读已经画在屏幕上的那份数,不重跑;第三条是这页唯一新算的东西。
 * ================================================================ */

/* 打乱那一条按 场景|卡|种子 缓存 —— 反复开关这页不该反复跑 BO,更不该因为
 * 重跑而出现两个版本的数字(runBO 是确定性的,但缓存让"同一页同一个数"变成
 * 结构保证,不是靠信任)。 */
const AB_CACHE = new Map();

function abShuffledRun(scene, card) {
  const irs = card && card.ir && card.ir.ir_shuffled;
  if (!irs) return null;
  const key = scene.id + '|' + card.id + '|' + seedNow;
  if (AB_CACHE.has(key)) return AB_CACHE.get(key);
  const run = runBO(scene, priorFromIR(irs), seedNow, 20, scene.injStart());
  AB_CACHE.set(key, run);
  return run;
}

/* 三条曲线画在一张图上。几何与主舞台的 drawCurve 一致(同样的 pad、同样的
 * "峰值往下 6 个点"纵轴窗口)—— 版式一变,评委就会怀疑这是另一套尺子。 */
function abDraw(canvas, series, maxY, maxX) {
  const g = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth, cssH = canvas.clientHeight;
  canvas.width = cssW * dpr; canvas.height = cssH * dpr;
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, cssW, cssH);

  const pad = { t: 18, r: 16, b: 26, l: 40 };
  const w = cssW - pad.l - pad.r, h = cssH - pad.t - pad.b;
  const yLo = maxY - 6;
  const yOf = v => pad.t + h - (Math.max(v, yLo) - yLo) / (maxY - yLo) * h;
  const xs = i => pad.l + w * (i / Math.max(maxX - 1, 1));

  g.strokeStyle = THEME.grid; g.lineWidth = 1;
  for (let gy = 0; gy <= 4; gy++) {
    const y = pad.t + (h / 4) * gy;
    g.beginPath(); g.moveTo(pad.l, y); g.lineTo(cssW - pad.r, y); g.stroke();
  }
  g.fillStyle = THEME.ink3; g.font = '10px "SF Mono", ui-monospace, monospace';
  g.textAlign = 'right';
  for (let gi = 0; gi <= 4; gi++) {
    const val = yLo + (maxY - yLo) * (gi / 4);
    g.fillText(val.toFixed(1), pad.l - 6, pad.t + h - (h / 4) * gi + 3);
  }

  for (const s of series) {
    if (!s.hist || !s.hist.length) continue;
    g.strokeStyle = s.color; g.lineWidth = 2.2; g.lineJoin = 'round';
    if (s.dash) g.setLineDash(s.dash); else g.setLineDash([]);
    g.beginPath();
    s.hist.forEach((p, i) => {
      const y = yOf(p.bestSoFar);
      i === 0 ? g.moveTo(xs(i), y) : g.lineTo(xs(i), y);
    });
    g.stroke();
    g.setLineDash([]);
    // 收束点:三条线停在不同批次上,终点是这页的主论点,得画出来
    const last = s.hist[s.hist.length - 1];
    g.fillStyle = THEME.screen; g.strokeStyle = s.color; g.lineWidth = 2;
    g.beginPath();
    g.arc(xs(s.hist.length - 1), yOf(last.bestSoFar), 4.5, 0, Math.PI * 2);
    g.fill(); g.stroke();
    // 报废批画红叉。三条线的废品数不一样,而那正是安全类经验的价值所在。
    s.hist.forEach((p, i) => {
      if (p.feasible) return;
      const x = xs(i), y = yOf(p.y);
      g.strokeStyle = THEME.li; g.lineWidth = 1.4;
      g.beginPath();
      g.moveTo(x - 3, y - 3); g.lineTo(x + 3, y + 3);
      g.moveTo(x + 3, y - 3); g.lineTo(x - 3, y + 3);
      g.stroke();
    });
  }

  g.fillStyle = THEME.ink3; g.textAlign = 'center';
  g.fillText('尝试批次 →', pad.l + w / 2, cssH - 7);
}

/* 表格。每一行都是这三条轨迹**自己**的读数,没有一格是推算出来的。 */
function abTable(scene, rows) {
  const t = $id('abTable');
  t.innerHTML = '';
  const su = scrapUnit(scene);
  const head = document.createElement('tr');
  for (const h of ['', '收束批次', '报废 ' + su, '推荐点真值', '声明区间']) {
    const th = document.createElement('th');
    th.textContent = h;
    head.appendChild(th);
  }
  t.appendChild(head);
  for (const r of rows) {
    const tr = document.createElement('tr');
    if (r.cls) tr.className = r.cls;
    const cells = [
      r.name,
      r.run ? r.run.nBatches + ' 批' : '—',
      r.run ? String(r.run.scrapped) : '—',
      r.run ? r.run.trueBest.toFixed(2) + scene.rewardUnit : '—',
      r.cut,
    ];
    for (const c of cells) {
      const td = document.createElement('td');
      td.textContent = c;
      tr.appendChild(td);
    }
    t.appendChild(tr);
  }
}

function renderAb() {
  const scene = SCENES[state.scene];
  $id('abScene').textContent = ' · ' + scene.name;
  const card = state.card;
  const base = state.history && state.history.baseline;
  const inj = state.history && state.history.injected;

  if (!card || !inj) {
    abDraw($id('abCanvas'), [], scene.rewardMax, 24);
    $id('abTable').innerHTML = '';
    $id('abNote').textContent =
      '还没有注入过经验 —— 先在主舞台抽一张卡(或说一句自己的经验并注入),'
      + '这页要拿那一句话的打乱版当第三条曲线,没有那句话就没有可打乱的东西。';
    return;
  }

  const shuf = abShuffledRun(scene, card);
  const maxX = Math.max(
    base ? base.history.length : 0, inj.history.length,
    shuf ? shuf.history.length : 0, 20);

  abDraw($id('abCanvas'), [
    { hist: base ? base.history : null, color: THEME.base },
    { hist: inj.history, color: THEME.xper },
    { hist: shuf ? shuf.history : null, color: THEME.gold, dash: [6, 4] },
  ], scene.rewardMax, maxX);

  const pct = v => (v == null ? '—' : (v * 100).toFixed(1) + '% 被剪掉');
  const aud = card.audit || {};
  const audS = (card.ir && card.ir.audit_shuffled) || null;
  abTable(scene, [
    { name: '真翻译', run: inj, cls: 'ab-inj', cut: pct(aud.volume_cut) },
    { name: '无先验裸跑', run: base, cut: '整张窗口' },
    {
      name: '维度打乱(同强度，错维度)', run: shuf, cls: 'ab-shuf',
      cut: shuf ? pct(audS ? audS.volume_cut : null) : '编译期被否决',
    },
  ]);

  // 结语只念**这三条轨迹算出来的数**。没有第三条曲线时照实说没有 ——
  // 这一栏空着比编一条曲线可信得多,而"为什么空"本身就是一条硬证据:
  // 打乱之后那句话在编译期就通不过,说明它声明的东西是与维度绑死的。
  const su = scrapUnit(scene);
  if (!shuf) {
    // 两种"没有第三条曲线"的原因完全不同,不能用同一句话糊过去 ——
    // 一种是这句话强到打乱就编不出先验(是证据),一种是我们没为现场输入导出
    // 打乱版(是我们的边界)。把后者说成前者,就是拿自己的短处冒充长处。
    // 这一栏走 textContent(不是 slowReason 那条 ⟦…⟧/** 渲染路径),
    // 所以一个记号都不能写 —— 写了就原样印在屏幕上。
    $id('abNote').textContent = card.said
      ? '这一句是现场输入,没有预导出的打乱版 —— 打乱由 server/export_ir.py 在'
        + '编译期做(与后端消融测试同一个函数、同一颗种子),浏览器这一侧不生成先验,'
        + '所以也没法在这儿给现场输入现编一份打乱版。想看这一栏,请抽一张经验卡。'
      : '这张卡没有可比的打乱版:把它的维度打乱之后,声明的区间塌成空集,'
        + '编译期就被否决了,压根跑不出一条曲线。这本身就是"它说的话和维度'
        + '绑死"的证据 —— 换个维度接上去,连一份合法的先验都构不成。';
    return;
  }
  // 判据必须与后端消融测试同一条 —— test_align.py::test_ablation 记的是
  //     (真翻译最优 − 打乱最优) + 0.5·(打乱废品 − 真翻译废品)
  // 在 10 颗种子上取胜,**不是批次数**。批次数在单颗种子上噪声极大(收敛判定
  // 是"连续几批不动",一次抖动就差一批),这张卡台上这一轮就是
  // 真翻译 8 批 / 裸跑 7 批 / 打乱 6 批 —— 拿批次当主论点,这页会自己念出
  // "接对维度省 -1 批,接错维度省 1 批",然后紧接着断言"所以是接对维度起了
  // 作用"。评委只需要读这一句就能把整页推翻,而真正的证据(真值高 1.38%、
  // 废品少一半)明明就在同一张表里。
  const dBest = inj.trueBest - shuf.trueBest;
  const dScrap = shuf.scrapped - inj.scrapped;
  const score = dBest + 0.5 * dScrap;
  const parts = ['推荐点真值 ' + inj.trueBest.toFixed(2) + ' → ' + shuf.trueBest.toFixed(2)
    + scene.rewardUnit + '(接错维度' + (dBest >= 0 ? '低了 ' + dBest.toFixed(2) : '高了 ' + (-dBest).toFixed(2))
    + scene.rewardUnit + ')'];
  if (inj.scrapped !== shuf.scrapped) {
    parts.push('报废 ' + inj.scrapped + ' → ' + shuf.scrapped + ' ' + su);
  }

  // 批次照旧报,但**降级成旁证并且当场说清它为什么不做主论点** ——
  // 藏起来更糟:表格第二列就写着收束批次,评委自己会减。
  const sInj = base ? base.nBatches - inj.nBatches : null;
  const sShuf = base ? base.nBatches - shuf.nBatches : null;
  let batchNote = '';
  if (sInj != null && sShuf != null) {
    batchNote = '批次这一栏(接对省 ' + sInj.toFixed(0) + ' 批、接错省 ' + sShuf.toFixed(0)
      + ' 批)在单颗种子上不足以判胜负:收敛判定是"连续几批不动",一次读数抖动就差一批 —— '
      + '所以后端消融测试记的是真值与废品,在 10 颗种子上取胜,不记批次。';
  }

  $id('abNote').textContent = parts.join(' · ')
    + '。三条曲线的种子、批次上限、起点、优化器逐项相同,唯一的差别是强度接在哪几维上。'
    + (score > 0
      ? '按后端消融测试的同一条判据(真值差 + 0.5×废品差 = '
        + (score >= 0 ? '+' : '') + score.toFixed(2) + '),这一轮真翻译胜过维度打乱。'
      : '按后端消融测试的同一条判据(真值差 + 0.5×废品差 = ' + score.toFixed(2)
        + '),这一轮台上的种子没有分出胜负 —— 照实说。判据要在 10 颗种子上看,'
        + '后端 test_ablation 的结论是 4 张卡里 3 张真翻译胜出,单颗种子翻车属于预期内。')
    + batchNote
    + '这页读的是台上那一轮(种子 ' + seedNow + ');结算页那三个数是 '
    + SETTLE_SEEDS + ' 个种子的均值,口径不同,别拿这里的批次去对结算的账。';
}

/* ================================================================
 * 三 · 关于
 *
 * 四个编号段落 + 团队口径行,全部写在 index.html 里 —— 它是**静态**的,
 * 这里一行都不用。写在这儿是为了让人别去找那个不存在的 renderAbout():
 * 这页说的是口径、边界、不做什么,那些话不该由代码现拼,它们是承诺。
 * ================================================================ */

