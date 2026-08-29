/* app.js — 三幕剧控制器 + 同题对照(人机 PK 的严肃版)。
 *
 * 幕1: 基准田(无经验裸跑,当场算)。
 * 幕2: 口诀注入 → 搜索域收缩(魔法时刻)。
 * 幕3: 经验田逐批实跑,红线仪表联动。
 * 幕4: 结算 —— 一句话值多少钱。
 * 同题对照: 评委在热力图上亲手落一子,与 AI 用**同一个 oracle 的真值**结算。
 *
 * 诚实性纪律(答辩要经得起问):
 *   · 热力图在结算前只画"未知的迷雾",不画真实响应面 —— 答案不能先画在
 *     屏幕上,否则评委的落子就是一场戏。结算时才揭底。
 *   · 落子只显坐标,数值一律等亮牌 —— 否则连点几下就能扫出响应面。
 *   · 三方(人/AI裸跑/AI+经验)用同一把尺子:孪生沙盘的无噪声真值。
 *   · 两田同种子,唯一的差异是那句话。
 */
'use strict';

/* ---------- helpers ---------- */
const moneyFmt = (n) => '¥' + Math.round(n).toLocaleString('zh-CN');
const $id = (id) => document.getElementById(id);
// 卡面文字来自 IR(后端编译产物),仍然一律转义:它会流经 innerHTML,
// 而"经验来自现场自由输入"是这个作品的卖点 —— 输入迟早不是我们写的。
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* ---------- state ---------- */
const state = {
  scene: 'formation',
  card: null,
  deck: [],             // 卡面 + IR 合成的结果,renderDeck 时装填
  history: null,        // { baseline: 结果对象, injected: 结果对象|null }
  playing: false,
  phase: 'idle',        // idle | running | settled
  timer: null,
  // 同题对照:落子只记坐标,数值等亮牌
  pkMode: false,
  pkPoint: null,        // 完整旋钮坐标(平面两维=点击值,其余维=中点)
  twinX: null,          // 机理视窗当前画的那一批(resize 时按它重画,不重跑 BO)
};

const SCENES = { formation: FORMATION, casting: CASTING };
const MONEY = { formation: 80000, casting: 6000 };
let seedNow = 20260829;

/* ---------- 收敛曲线 ---------- */
function drawCurve(canvas, hist, color, maxY, maxX) {
  const g = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth, cssH = canvas.clientHeight;
  canvas.width = cssW * dpr; canvas.height = cssH * dpr;
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, cssW, cssH);

  const pad = { t: 16, r: 14, b: 22, l: 34 };
  const w = cssW - pad.l - pad.r, h = cssH - pad.t - pad.b;
  const yLo = maxY - 6;      // 纵轴窗口:峰值往下 6 个点,报废批画在底边
  const yOf = v => pad.t + h - (Math.max(v, yLo) - yLo) / (maxY - yLo) * h;

  g.strokeStyle = 'rgba(120,120,110,.25)'; g.lineWidth = 1;
  for (let gy = 0; gy <= 4; gy++) {
    const y = pad.t + (h / 4) * gy;
    g.beginPath(); g.moveTo(pad.l, y); g.lineTo(cssW - pad.r, y); g.stroke();
  }
  g.fillStyle = '#8a8a82'; g.font = '10px "SF Mono", ui-monospace, monospace';
  for (let gi = 0; gi <= 4; gi++) {
    const val = yLo + (maxY - yLo) * (gi / 4);
    g.textAlign = 'right';
    g.fillText(val.toFixed(1), pad.l - 6, pad.t + h - (h / 4) * gi + 3);
  }
  if (!hist || !hist.length) return;

  const xs = i => pad.l + w * (i / Math.max(maxX - 1, 1));
  // best-so-far 阶梯线
  g.strokeStyle = color; g.lineWidth = 2.2; g.lineJoin = 'round';
  g.beginPath();
  hist.forEach((p, i) => {
    const y = yOf(p.bestSoFar);
    i === 0 ? g.moveTo(xs(i), y) : g.lineTo(xs(i), y);
  });
  g.stroke();
  // 每批的实际读数:可行=淡点,报废=红叉(废品要看得见,它是价值故事的一半)
  hist.forEach((p, i) => {
    const x = xs(i), y = yOf(p.y);
    if (p.feasible) {
      g.globalAlpha = 0.35; g.fillStyle = color;
      g.beginPath(); g.arc(x, y, 2, 0, Math.PI * 2); g.fill();
      g.globalAlpha = 1;
    } else {
      g.strokeStyle = '#e05252'; g.lineWidth = 1.6;
      g.beginPath();
      g.moveTo(x - 3.5, y - 3.5); g.lineTo(x + 3.5, y + 3.5);
      g.moveTo(x + 3.5, y - 3.5); g.lineTo(x - 3.5, y + 3.5);
      g.stroke();
    }
  });
  // 当前最优点
  const last = hist[hist.length - 1];
  g.fillStyle = '#fff'; g.strokeStyle = color; g.lineWidth = 2;
  g.beginPath(); g.arc(xs(hist.length - 1), yOf(last.bestSoFar), 4.5, 0, Math.PI * 2);
  g.fill(); g.stroke();

  g.fillStyle = '#8a8a82'; g.textAlign = 'center';
  g.fillText('尝试批次 →', pad.l + w / 2, cssH - 5);
}

/* ---------- 热力图 ----------
 * 结算前画"迷雾"(均匀的未知,只有量程框),结算后才揭底画真实响应面。
 * 答案不能先画在屏幕上 —— 这是同题对照能站住的前提。 */
let heatShrunk = false;
let heatInfo = null;    // 像素↔参数 的映射,落子和标记共用

function heatMapping(cssW, cssH) {
  const scene = SCENES[state.scene];
  const [px, py] = scene.plane;
  const active = currentBounds();
  return {
    px, py, cssW, cssH,
    aLo: active[px].lo, aHi: active[px].hi,
    bLo: active[py].lo, bHi: active[py].hi,
  };
}
const heatToX = (m, xPix) => m.aLo + (xPix / m.cssW) * (m.aHi - m.aLo);
// 纵轴向上增大(顶部=高值),与"高温在上"的直觉一致
const heatToY = (m, yPix) => m.bHi - (yPix / m.cssH) * (m.bHi - m.bLo);
const heatPx = (m, v) => (v - m.aLo) / (m.aHi - m.aLo) * m.cssW;
const heatPy = (m, v) => (m.bHi - v) / (m.bHi - m.bLo) * m.cssH;

function drawHeat(hm) {
  const scene = SCENES[state.scene];
  const g = hm.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const cssW = hm.clientWidth, cssH = hm.clientHeight;
  hm.width = cssW * dpr; hm.height = cssH * dpr;
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, cssW, cssH);

  const m = heatMapping(cssW, cssH);
  heatInfo = m;
  const stride = 4;
  const revealed = state.phase === 'settled';

  for (let yi = 0; yi < cssH; yi += stride) {
    for (let xi = 0; xi < cssW; xi += stride) {
      let col;
      if (!revealed) {
        // 迷雾:种子噪声的浅纹理,不携带任何响应面信息
        const rng = mulberry32(7 + yi * 373 + xi * 7919);
        const t = 0.10 + rng() * 0.08;
        col = [Math.round(38 + t * 60), Math.round(44 + t * 60), Math.round(48 + t * 50)];
      } else {
        const tv = heatToX(m, xi + stride / 2);
        const tV = heatToY(m, yi + stride / 2);
        const xProbe = scene.params.map((p, i) => {
          if (i === m.px) return tv;
          if (i === m.py) return tV;
          return (p.lo + p.hi) / 2;   // 其余维切中点 —— 落子结算同一口径
        });
        if (!scene.feasible(xProbe)) {
          col = [82, 42, 42];          // 红线区:暗红
        } else {
          const r0 = scene.rewardMax - 3.5, r1 = scene.rewardMax + 0.2;
          let t = (scene.reward(xProbe) - r0) / (r1 - r0);
          t = Math.max(0, Math.min(1, t));
          col = [Math.round(20 + t * 200), Math.round(110 + t * 70), Math.round(105 - t * 55)];
        }
      }
      g.fillStyle = `rgb(${col[0]},${col[1]},${col[2]})`;
      g.fillRect(xi, yi, stride, stride);
    }
  }

  // 量程框(注入后收缩的那圈虚线)
  g.strokeStyle = heatShrunk ? 'rgba(240,200,90,.9)' : 'rgba(200,200,190,.5)';
  g.lineWidth = 2; g.setLineDash([5, 4]);
  g.strokeRect(1, 1, cssW - 2, cssH - 2);
  g.setLineDash([]);

  // 挖掉的禁区(exclude 那一类口诀)
  if (heatShrunk) drawExclusions(g, m, scene);

  // 轴注记
  const pA = scene.params[m.px], pB = scene.params[m.py];
  g.fillStyle = 'rgba(235,232,220,.85)'; g.font = '10px "SF Mono", ui-monospace, monospace';
  g.textAlign = 'left';
  g.fillText(`${pA.name} ${m.aLo.toFixed(pA.decimals)}→${m.aHi.toFixed(pA.decimals)}${pA.unit}`, 8, cssH - 8);
  g.save(); g.translate(12, 14); g.fillText(`${pB.name}↑`, 0, 0); g.restore();

  if (revealed) drawMechLine(g, m, scene);
  drawHeatMarks(g, m);
}

/* 当前切片上**真的挡着**的那几块禁区。
 *
 * 只认**显示平面上的投影**。禁区是五维盒子,屏幕是二维;其余维在图上切的是
 * 中点,所以只有当中点确实落在禁区那几维的区间里,这块禁区才真的挡着当前这张
 * 切片 —— 否则画出来就是骗人的(那个禁区在别的切片上,不在你看的这张上)。
 *
 * 画图和图上那行说明必须读同一个判断。原先说明写死成"口诀已收窄搜索域",
 * 而实测九张卡里有四张一维都没收窄(cold 是挖角,temp/age/p1 两样都不动) ——
 * 屏幕上那行字说收窄了、亮框纹丝不动,评委先信字还是先信图?
 * 所以这里只有一个函数说"看得见什么",drawExclusions 和 heatNote 都问它。 */
function visibleExclusions(m, scene) {
  const ex = (state.card && state.card.prior && state.card.prior.exclusions) || [];
  if (!ex.length) return [];
  const mid = scene.params.map(p => (p.lo + p.hi) / 2);
  return ex.map(e => e.box || e).filter(box => {
    if (!box || !box.length || !box[m.px] || !box[m.py]) return false;
    // 其余维:中点不在禁区区间内 → 这块禁区不挡当前切片,不画。
    // 禁区是五维盒子而屏幕是二维,画一块不在这张切片上的禁区就是骗人。
    for (let i = 0; i < box.length; i++) {
      if (i === m.px || i === m.py) continue;
      const b = box[i];
      if (!b) continue;
      if (mid[i] < b[0] || mid[i] > b[1]) return false;
    }
    return true;
  });
}

/* 热力图那行说明。**照实说这张卡在图上做了什么**,做不到的不说。
 *
 * 原先这行写死成"口诀已收窄搜索域 —— 亮框即经验圈出的窗口",九张卡一个样。
 * 实测只有五张真收窄了显示平面上的维度;cold 是挖角(框不动),而 temp/age/p1
 * 两样都不做 —— 它们改的是先验形状、读数可信度、软惩罚,全都不在这张图上。
 * 于是那三张卡的现场是:字说"已收窄",框一动不动。这种不一致比没有魔法时刻
 * 更贵 —— 评委会开始怀疑屏幕上另外那些字。
 *
 * 收窄区间从 prior.bounds 读,禁区从 visibleExclusions 读(与画图同一个判断),
 * 两样都没有时退回**编译器自己写的 notes** —— 那是编译期生成的记录,不是我在
 * 这儿替它编一句解释。 */
function heatNoteFor(card) {
  const scene = SCENES[state.scene];
  const prior = card && card.prior;
  if (!prior) return '悬浮：把口诀的关键维度照亮';
  const [px, py] = scene.plane;
  const b = prior.bounds || [];
  const fmt = (v, q) => v.toFixed(q.decimals) + (q.unit || '');

  // 一、显示平面上的收窄(评委眼睛能直接对上的那种)
  const onPlane = [];
  for (const i of [px, py]) {
    const q = scene.params[i];
    if (!b[i]) continue;
    if (b[i].lo > q.lo + 1e-9 || b[i].hi < q.hi - 1e-9) {
      onPlane.push(`${q.name} 收到 ${fmt(b[i].lo, q)}~${fmt(b[i].hi, q)}`);
    }
  }
  if (onPlane.length) return `口诀收窄搜索域：${onPlane.join('、')} —— 亮框即经验圈出的窗口`;

  // 二、挖掉的禁区(框不动,但图上确实少了一块)
  if (visibleExclusions({ px, py }, scene).length) {
    return '口诀在框内划掉一块 —— 斜纹区是这句话判的禁区，不是沙盘判的废';
  }

  // 三、图外的收窄:确实收了,只是收在没画出来的那几维上。说清"图上看不见",
  // 别让评委以为亮框应该动而没动。
  const off = [];
  for (let i = 0; i < scene.params.length; i++) {
    if (i === px || i === py || !b[i]) continue;
    const q = scene.params[i];
    if (b[i].lo > q.lo + 1e-9 || b[i].hi < q.hi - 1e-9) off.push(q.name);
  }
  if (off.length) return `口诀收窄的是 ${off.join('、')} —— 不在这两轴上，图上看不见`;

  // 四、两样都没有。这句话动的是先验形状/读数可信度/软惩罚 —— 照编译器写的
  // notes 念第一条,并明说搜索域没动。
  const n = (prior.notes && prior.notes[0]) || '';
  return n
    ? `搜索域没动 —— 这句话改的是：${n}`
    : '搜索域没动 —— 这句话改的不是搜索边界（见结算）';
}

/* 画出被口诀**挖掉**的那块禁区。
 *
 * 为什么非画不可:「低温别上倍率,析锂没商量」是全场叙事最强的一张卡,而它
 * 走的不是 narrow(收窄箱子)而是 exclude(在箱子里挖掉一角)。热力图原先只画
 * bounds 那个框 —— 于是沙盘知道有个禁区、屏幕上什么也不动,这张卡的"魔法时刻"
 * 整个落空(实测:平面面积 100%,亮框纹丝不动,而 volume_cut 其实有 0.1042)。
 * 台上正要说"看,这句话把这块划掉了",画面却毫无变化,是最难圆的一种冷场。
 *
 * 不泄题:禁区来自那句话,不来自 oracle。结算前照旧只有迷雾,这里画的是**人说
 * 的话**,不是答案。 */
function drawExclusions(g, m, scene) {
  for (const box of visibleExclusions(m, scene)) {
    const bx = box[m.px], by = box[m.py];
    if (!bx || !by) continue;
    const x0 = cl2(heatPx(m, bx[0]), 0, m.cssW), x1 = cl2(heatPx(m, bx[1]), 0, m.cssW);
    const y0 = cl2(heatPy(m, by[1]), 0, m.cssH), y1 = cl2(heatPy(m, by[0]), 0, m.cssH);
    const w = x1 - x0, h = y1 - y0;
    if (w <= 1 || h <= 1) continue;
    // 斜线阴影 + 实边:与"红线区暗红块"要能一眼分开 —— 那块是沙盘判的废,
    // 这块是**人划的**。两件事长得像会让评委以为我们把答案画上去了。
    g.save();
    g.beginPath(); g.rect(x0, y0, w, h); g.clip();
    g.fillStyle = 'rgba(20,20,24,.42)';
    g.fillRect(x0, y0, w, h);
    g.strokeStyle = 'rgba(240,200,90,.5)'; g.lineWidth = 1;
    g.beginPath();
    for (let d = -h; d < w + h; d += 9) {
      g.moveTo(x0 + d, y1); g.lineTo(x0 + d + h, y0);
    }
    g.stroke();
    g.restore();
    g.strokeStyle = 'rgba(240,200,90,.85)'; g.lineWidth = 1.5;
    g.setLineDash([4, 3]);
    g.strokeRect(x0, y0, w, h);
    g.setLineDash([]);
    // 标签只在框够大时画,免得在窄条上糊成一团
    if (w > 74 && h > 20) {
      g.fillStyle = 'rgba(245,240,225,.92)';
      g.font = '10px "SF Mono", ui-monospace, monospace';
      g.textAlign = 'center';
      g.fillText('口诀划掉', x0 + w / 2, y0 + h / 2 + 3);
      g.textAlign = 'left';
    }
  }
}

const cl2 = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

/* 只描视野内那几段。夹到画布边缘的点参与填充(带子确实延伸到画外),但不能
 * 参与描边 —— 那会在画布顶边或底边画出一条不存在的直线,而虚线一旦贴着边框,
 * 看上去就像我们多画了一道红线。 */
function strokeRuns(g, pts) {
  let open = false;
  for (const p of pts) {
    if (!p.inView) { if (open) { g.stroke(); open = false; } continue; }
    if (!open) { g.beginPath(); g.moveTo(p.xi, p.y); open = true; }
    else g.lineTo(p.xi, p.y);
  }
  if (open) g.stroke();
}

/* 报废墙的上沿:沿纵轴二分找 feasible 的翻转点。
 *
 * 注意这里**只问 scene.feasible**,不复述任何阈值公式。判废的权威是沙盘,
 * 这个项目已经因为"视窗自己推一遍析锂阈值"栽过一次:同一批在沙盘可行、在
 * 视窗被画成报废。二分法多花几十次函数调用,换来的是这条线不可能画错 ——
 * 它只会画出沙盘真正的判决边界。 */
function scrapWallPts(m, scene) {
  const mid = scene.params.map(p => (p.lo + p.hi) / 2);
  const ok = (aSet, bSet) => {
    const x = mid.slice();
    x[m.px] = aSet; x[m.py] = bSet;
    return scene.feasible(x);
  };
  const pts = [];
  for (let xi = 0; xi <= m.cssW; xi += 6) {
    const a = heatToX(m, xi);
    // 整列可行 / 整列判废时,沿在视野**外**。这时候不能直接跳过这一列 ——
    // 跳过会让点列比起始线短一截,风险带那个多边形闭合时就从带子的一端斜着
    // 割回另一端,画出一个不存在的楔形。所以贴到视野边缘并记下 inView=false:
    // 填充照旧用它(带子确实延伸到画外),描边跳过它(那里没有真的沿可画)。
    if (ok(a, m.bLo)) { pts.push({ xi, y: m.cssH, inView: false }); continue; }
    if (!ok(a, m.bHi)) { pts.push({ xi, y: 0, inView: false }); continue; }
    let lo = m.bLo, hi = m.bHi;
    for (let i = 0; i < 24; i++) { const md = (lo + hi) / 2; if (ok(a, md)) hi = md; else lo = md; }
    pts.push({ xi, y: heatPy(m, hi), inView: true });
  }
  return pts;
}

/* 机理斜边。只在结算揭底后画,静态虚线 —— 不破同屏单动。
 *
 * 为什么值得多这十几行:红线区已经被画成暗红色块了,可暗红色块只告诉评委
 * "这儿会废",不告诉他**为什么是这个形状**。而这个形状正是整个故事的关键:
 * 析锂线是斜的,所以一句"低于 30℃ 别超 0.2C"能盖住它的一段,盖不住整条 ——
 * 老师傅那句话是对的,只是不完备,补完它的是 AI。把线画出来,这句话就从
 * 解说词变成了图上看得见的东西。
 *
 * 线本身取自 scene.mechLine.at() → platingOnset,与判废走同一个函数;这里
 * 只做像素换算,不做任何机理判断。没有 mechLine 的场景什么都不画。 */
function drawMechLine(g, m, scene) {
  const ml = scene.mechLine;
  if (!ml) return;
  // 逐列采样而不是两点连线:斜边现在是线性的,但 platingOnset 一旦改成非线性
  // (真实体系就是弯的),这里不必跟着改 —— 画的永远是那个函数本身。
  // 出视野的列夹到画布边缘并记 inView=false,与 scrapWallPts 同一套约定:
  // 填充用全部点(带子确实延伸到画外),描边只走视野内那几段。
  const pts = [];
  for (let xi = 0; xi <= m.cssW; xi += 6) {
    const y = ml.at(scene, heatToX(m, xi));
    pts.push({
      xi, y: heatPy(m, cl2(y, m.bLo, m.bHi)),
      inView: y >= m.bLo && y <= m.bHi,
    });
  }
  const wall = scrapWallPts(m, scene);
  if (!pts.some(p => p.inView)) return;

  g.save();
  // 报废墙先画(它就是暗红块的上沿),再画起始线。两条线之间那条带子是
  // **渐变风险带**:析锂已经开始,但还没到整托判废。带子必须画出来,否则
  // 评委会看到一条标着"起始线"的虚线浮在暗红块上方 5℃ 处,合理的第一个问题
  // 就是"你的线怎么对不上你的红区" —— 而正确答案是两者说的不是同一件事:
  // 起始线是"锂开始沉积",红块上沿是"废到要整托扔"。实测两者恒差 5.5℃。
  if (wall.length === pts.length && wall.length >= 2) {
    g.beginPath();
    g.moveTo(pts[0].xi, pts[0].y);
    for (const p of pts.slice(1)) g.lineTo(p.xi, p.y);
    for (let i = wall.length - 1; i >= 0; i--) g.lineTo(wall[i].xi, wall[i].y);
    g.closePath();
    g.fillStyle = 'rgba(255,170,80,.13)';
    g.fill();
    g.strokeStyle = 'rgba(240,120,90,.85)';
    g.lineWidth = 1.4; g.setLineDash([3, 3]);
    strokeRuns(g, wall);
  }
  g.strokeStyle = 'rgba(255,205,120,.95)';
  g.lineWidth = 1.8; g.setLineDash([7, 5]);
  strokeRuns(g, pts);
  g.setLineDash([]);

  // 标注贴在**视野内**那一段的中点上方 —— 贴在夹到边缘的假点上会飘到角上。
  // 带一层半透明底:底下是热力图的高饱和色块,纯文字在绿色上几乎读不出来。
  const vis = pts.filter(p => p.inView);
  const mid = vis[Math.floor(vis.length / 2)];
  const txt = `${ml.label} ${ml.formula} · ${ml.note}`;
  g.font = '10px "SF Mono", ui-monospace, monospace';
  const tw = g.measureText(txt).width;
  const tx = cl2(mid.xi - tw / 2, 4, Math.max(4, m.cssW - tw - 6));
  const ty = cl2(mid.y - 8, 14, m.cssH - 6);
  g.fillStyle = 'rgba(18,20,22,.72)';
  g.fillRect(tx - 3, ty - 10, tw + 6, 13);
  g.fillStyle = 'rgba(255,215,140,.98)';
  g.textAlign = 'left';
  g.fillText(txt, tx, ty);
  g.restore();
}

/* 落子/推荐点标记。静态叠加,无动画 —— 不破同屏单动。 */
function drawHeatMarks(g, m) {
  const marks = [];
  if (state.pkPoint) marks.push({ x: state.pkPoint, icon: '🧑', col: '#e8e4d8' });
  if (state.phase === 'settled' && state.history) {
    const b = state.history.baseline, j = state.history.injected;
    if (b && b.bestX) marks.push({ x: b.bestX, icon: '🤖', col: '#5eead4' });
    if (j && j.bestX) marks.push({ x: j.bestX, icon: '🤝', col: '#fbbf24' });
  }
  for (const mk of marks) {
    const x = heatPx(m, mk.x[m.px]), y = heatPy(m, mk.x[m.py]);
    g.strokeStyle = mk.col; g.lineWidth = 1.6;
    g.beginPath(); g.arc(x, y, 8, 0, Math.PI * 2); g.stroke();
    g.beginPath();
    g.moveTo(x - 12, y); g.lineTo(x - 5, y); g.moveTo(x + 5, y); g.lineTo(x + 12, y);
    g.moveTo(x, y - 12); g.lineTo(x, y - 5); g.moveTo(x, y + 5); g.lineTo(x, y + 12);
    g.stroke();
    g.font = '13px system-ui'; g.textAlign = 'center';
    g.fillText(mk.icon, x, y - 13);
  }
}

/* 当前生效的搜索域(注入后被卡收缩) */
function currentBounds() {
  const scene = SCENES[state.scene];
  if (heatShrunk && state.card) return boundsOf(state.card, scene);
  return scene.params.map(p => ({ lo: p.lo, hi: p.hi }));
}

/* ---------- 红线仪表(静态数字,装饰不是证据) ---------- */
function updateMeters(scene, hist, idx) {
  const at = hist && hist.length
    ? hist[Math.min(idx == null ? hist.length - 1 : idx, hist.length - 1)]
    : null;
  const r = at ? scene.risks(at.x) : { li: 0, gas: 0 };
  const yieldT = at
    ? Math.max(0, Math.min(1, (at.y - (scene.rewardMax - 3.5)) / 3.5))
    : 0;
  const vals = { li: r.li, gas: r.gas, yield: yieldT };
  const names = {
    li: scene.redLines[0].label, gas: scene.redLines[1].label, yield: scene.redLines[2].label,
  };
  for (const k of ['li', 'gas', 'yield']) {
    const cap = k === 'li' ? 'Li' : k === 'gas' ? 'Gas' : 'Yield';
    const el = $id('meter' + cap);
    if (el) el.style.width = (vals[k] * 100).toFixed(0) + '%';
    const nm = $id('meter' + cap + 'Name');
    if (nm) nm.textContent = names[k];
  }
}

/* 报废的量词。化成线整托判废、压铸按模次算 —— 页面上所有"报废几X"都念这个
 * 词,不硬编码"托"。缺省回落到"托"只是防御,两个场景都显式声明了。 */
function scrapUnit(scene) {
  return (scene.settleMixin && scene.settleMixin.scrapUnit) || '托';
}

/* ---------- 逐批解说(确定性模板,种子驱动 —— 不用 Math.random) ---------- */
function lineFor(scene, idx, hist) {
  const p = hist[idx];
  if (!p.feasible) {
    const which = scene.risks(p.x);
    const worst = which.li >= which.gas ? scene.redLines[0].label : scene.redLines[1].label;
    return `第 ${idx + 1} 批撞了${worst} —— 这一${scrapUnit(scene)}报废了`;
  }
  const rng = mulberry32(seedNow + idx * 7919);
  const lines = [
    '按采集函数挑的下一批 …', '往不确定的角落探一批 …', '在好点附近再压实一批 …',
    '这排走得快 …', '有点眉目了 …',
  ];
  return `第 ${idx + 1} 批:${lines[Math.floor(rng() * lines.length)]}`;
}

/* ---------- 卡面 + IR 合成 ----------
 * 卡面(念出来的话、卡背机理、逐批解说)在 scenarios.js;先验在 priors_ir.js,
 * 由 server/export_ir.py 从 Python 编译器导出。前端不编译先验,只解释它 ——
 * 校验、红线族授权、体积上限、降级判断全部只在后端那一侧发生。
 *
 * 这样做的原因不抽象:两份手写先验已经分叉过一次,同一张歪经卡后端否决
 * (-3.76)而前端白省 2.00 批。合成到一处之后,那种分叉没有生长的地方。 */
function cardsOf(scene) {
  const bank = (window.PRIORS_IR && window.PRIORS_IR.scenes[scene.id]) || null;
  return scene.cards.map(face => {
    const ir = bank && bank.cards[face.id];
    if (!ir) {
      // IR 没导出这张卡:宁可显示"未编译"也不偷偷用一份手写先验糊过去。
      return { id: face.id, kind: 'good', text: face.id, why: '（先验未编译）',
               narration: face.narration, ir: null, prior: null, broken: true };
    }
    return {
      id: face.id,
      kind: ir.card.kind,
      text: ir.card.text,
      why: ir.card.why,
      external: ir.card.external,
      pairWith: ir.card.pair_with,
      speaksTo: ir.card.speaks_to || [],
      narration: face.narration,
      notes: ir.notes || [],
      parked: ir.parked || [],
      audit: ir.audit || null,
      ir,
      prior: priorFromIR(ir),
    };
  });
}

/* 当前生效的搜索域来自 IR 的 bounds(经验声明的箱子),不是重新算一遍 */
function boundsOf(card, scene) {
  if (card && card.prior && card.prior.bounds && card.prior.bounds.length) {
    return card.prior.bounds;
  }
  return scene.params.map(p => ({ lo: p.lo, hi: p.hi }));
}

/* ---------- 可信度自证 ----------
 * 别队的孪生画一张响应面,评委只能选择信或不信。我们让孪生**当场验自己**:
 * 每批的预测区间都是观测之前记下的,事后对一遍真读数 —— 这是唯一能回答
 * "你这图是不是先画好的"的东西。
 *
 * 两条诚实纪律:
 *   · 算的是**16 轮留出验证**,不是评委刚看完的那一轮。单轮只有 6~13 批可用
 *     样本,估 90% 覆盖率的抖动有 ±12% —— 拿它当精确数字是假精度。所以标题
 *     里写清"16 轮",不冒充"本轮"。
 *   · 可行/报废两栏不平均。混算出来是"σ 严重过度自信"的假警报,拆开才是
 *     真结论:光滑处诚实,红线上瞎 —— 而那份瞎每轮要付几托废品。
 * 只在场景装载时算一次(约 190ms),不进逐批节拍 —— 不抢同屏单动。 */
let credCache = {};

function renderCred(scene) {
  const a = credCache[scene.id]
    || (credCache[scene.id] = calibrationAudit(scene, 16, 20, seedNow));
  const f = a.feasible, w = a.infeasible;
  $id('credSub').textContent =
    `留出验证 · ${a.rounds} 轮裸跑 · 预测区间都在看到读数之前记下`;

  if (!f || f.n < 20) {
    // 样本不足就说样本不足。宁可空着,也不显示一个估不出来的百分比。
    $id('credFeasV').textContent = '样本不足';
    $id('credFeasN').textContent = f ? `n=${f.n}` : '';
    $id('credFeas').className = 'cred-row';
  } else {
    $id('credFeasV').textContent =
      `90% 区间命中 ${(f.cover * 100).toFixed(0)}% · 偏差 ${f.bias >= 0 ? '+' : ''}${f.bias.toFixed(2)}σ`;
    $id('credFeasN').textContent = `n=${f.n}`;
    $id('credFeas').className = 'cred-row ' + (a.honest ? 'cred-ok' : 'cred-warn');
  }

  if (w && w.n >= 5) {
    // 这一栏是**价值主张的度量**,不是失败项 —— 所以用中性色,不用红。
    // 措辞用"比预测低 N σ"而不是"意外 −Nσ":负号的方向对评委不是自明的,
    // 而这一栏的全部意思就在方向上 —— 读数**掉**下去了,孪生完全没料到。
    $id('credScrapV').textContent =
      `实测比预测低 ${Math.abs(w.bias).toFixed(0)}σ · 区间命中 ${(w.cover * 100).toFixed(0)}%`;
    $id('credScrapN').textContent = `n=${w.n} · 每轮撞 ${a.scrapPerRun.toFixed(1)} ${scrapUnit(scene)}`;
    $id('credScrap').className = 'cred-row cred-blind';
    $id('credFoot').textContent =
      `σ 在响应面光滑处是诚实的,在红线上是瞎的 —— 红线是 ${scene.scrapPenalty.toFixed(0)} 分的阶跃,`
      + `平稳核表达不了,撞第五次时意外程度照旧。这份瞎每轮要付 ${a.scrapPerRun.toFixed(1)} ${scrapUnit(scene)}废品,`
      + `那几${scrapUnit(scene)}正是老师傅那句话卖的东西。`;
  } else {
    $id('credScrapV').textContent = '本轮未撞线';
    $id('credScrapN').textContent = '';
    $id('credScrap').className = 'cred-row';
    $id('credFoot').textContent = '预测区间在观测前记下,事后逐批对账 —— 这一栏没有作弊的空间。';
  }
}

/* ---------- 场景装载 ---------- */
function loadScene(sceneId) {
  state.scene = sceneId;
  document.querySelectorAll('.scene-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.scene === sceneId));

  const scene = SCENES[sceneId];
  $id('fieldRSub').textContent = '待一张经验卡';

  // 基准田:无先验裸跑,当场算(毫秒级)。徽章上照实写"当场裸跑" ——
  // 原先那行"存档 · 今早 9:02 实跑"是编的,见 index.html 里那段。
  const base = runBO(scene, null, seedNow, 24, scene.baseStart());
  state.history = { baseline: base, injected: null };

  drawHeat($id('heat'));
  renderDeck(scene);
  renderCred(scene);
  renderTwin(scene, null);
  drawCurve($id('curveL'), base.history, '#0f766e', scene.rewardMax, 24);
  $id('statL').textContent = base.nBatches + ' 批';
  drawCurve($id('curveR'), [], '#c2410c', scene.rewardMax, 20);
  $id('statR').textContent = '—';
  updateMeters(scene, null, null);
  // 评委正在读卡面的这几秒,把结算要用的 16 轮基准田悄悄算掉(见下面那段)
  prewarmSettleBaseline(scene);
}

/* ---------- 结算基准田的空闲预热 ----------
 * 结算一共 32 轮活:16 轮基准田 + 16 轮注入田。而回放只有 6~19 拍(收敛早的
 * 卡拍数最少),一拍一轮根本摊不完 —— 剩下的照旧压在"弹结算页"那一瞬,实测
 * casting/p1 卡 854ms。
 *
 * 但这 32 轮里有一半**根本不用等卡片**:基准田是裸跑,与哪句话无关,loadScene
 * 之后就能算,而那段时间评委正在读卡面。16 轮实测共 128~175ms、单轮最慢 27ms,
 * 拆成一轮一个 idle 回调,一帧都占不满。
 *
 * 预热是纯提前,不是第二条计算路径:走的还是 baselineStep,缓存也是同一个
 * settleBaseCache。预热没跑完就点卡也不会错 —— advanceSettle 里照旧会补。
 * 这里省的是时间,不是正确性。 */
const HAS_IDLE = typeof window.requestIdleCallback === 'function';
const idleRun = f => (HAS_IDLE ? window.requestIdleCallback(f) : setTimeout(f, 60));
const idleStop = h => (HAS_IDLE ? window.cancelIdleCallback(h) : clearTimeout(h));
let warmH = null;

function prewarmSettleBaseline(scene) {
  if (warmH != null) { idleStop(warmH); warmH = null; }
  const seed = seedNow, id = scene.id;
  const tick = () => {
    warmH = null;
    // 已经切到别的场景了就停手 —— 别拿主线程替一块没人看的缓存干活
    if (state.scene !== id) return;
    if (baselineStep(scene, SETTLE_SEEDS, seed)) return;
    warmH = idleRun(tick);
  };
  warmH = idleRun(tick);
}

/* ---------- 机理视窗的门 ----------
 * 只有化成场景有这块 —— 压铸是底仓,它的机理(充型/凝固)不是一条充电曲线,
 * 硬套一张图上去就是编。没有就整块收起来,不留一个空画布在那儿装样子。
 * state.twinX 记住当前画的是哪一批,resize 时按它重画(不重跑 BO)。 */
function renderTwin(scene, x) {
  const wrap = document.querySelector('.twin-wrap');
  const has = scene.id === 'formation';
  wrap.classList.toggle('hidden', !has);
  if (!has) { state.twinX = null; return; }
  state.twinX = x;
  drawTwin($id('twinR'), x, scene);
}

/* ---------- 卡组 ---------- */
function renderDeck(scene) {
  const wrap = $id('deckCards'); wrap.innerHTML = '';
  state.deck = cardsOf(scene);
  state.deck.forEach((card, i) => {
    const el = document.createElement('button');
    el.className = 'card ' + (card.kind === 'wrong' ? 'kind-wrong wrong' : 'kind-good');
    if (card.broken) el.className += ' disabled';
    // 停靠数写在卡面上:"接住 2 条、停靠 1 条"比"全接住了"更可信,
    // 而且它是编译期的事实,不是这里现算的。
    const park = card.parked && card.parked.length
      ? `<span class="card-park" title="${esc(card.parked[0].reason)}">停靠 ${card.parked.length}</span>`
      : '';
    el.innerHTML = `<span class="card-tag">${String(i + 1).padStart(2, '0')}</span>
      <span class="card-text">${esc(card.text)}</span>
      <span class="card-why">${esc(card.why)}</span>${park}`;
    el.addEventListener('click', () => playAct(card));
    wrap.appendChild(el);
  });
}

/* ---------- 三幕 ---------- */
function playAct(card) {
  if (state.playing) return;
  state.playing = true;
  state.phase = 'running';
  state.card = card;
  clearTimeout(state.timer);

  // 幕2:搜索域收缩 —— 魔法时刻(此刻动的只有热力图)
  heatShrunk = true;
  drawHeat($id('heat'));
  $id('heatNote').textContent = heatNoteFor(card);
  document.querySelectorAll('.card').forEach(c => {
    c.classList.toggle('active', c.querySelector('.card-text').textContent === card.text);
  });

  // 幕3:经验田实跑。两田同种子 —— 唯一的差异是那句话。
  // 台上放的是 seedNow 这一轮(可复现回放);结算的账另按 16 种子算,见下面。
  const scene = SCENES[state.scene];
  const inj = runBO(scene, card.prior, seedNow, 20, scene.injStart());
  state.history.injected = inj;
  const hist = inj.history;

  // 16 种子的账**摊进回放的空隙里算**。它原先整块压在"最后一批画完 → 弹结算
  // 页"那一瞬,实测卡 0.29~1.60 秒 —— 那是全场最关键的一下,不能卡。回放本身
  // 要走 4 秒左右的 setTimeout,主线程在这 4 秒里基本闲着;把算账摊进去,这笔
  // 时间就藏在已经付过的钱里。摊不完的部分 settle() 里补齐(同一条计算路径,
  // 所以摊着算和一次算完永远是同一个数)。
  const plan = settlePlan(scene, card.prior, {
    speaksTo: card.speaksTo || [], seed0: seedNow,
  });

  let i = 0;
  $id('statR').textContent = '0 批';
  drawCurve($id('curveR'), [], '#c2410c', scene.rewardMax, 20);
  const stepFn = () => {
    if (i < hist.length) {
      drawCurve($id('curveR'), hist.slice(0, i + 1), '#c2410c', scene.rewardMax, 20);
      $id('statR').textContent = (i + 1) + ' 批';
      updateMeters(scene, hist, i);
      // 机理视窗:画**这一批**的充电曲线。与收敛曲线同一次重绘、同一个节拍 ——
      // 它不自己起动画,所以同屏单动仍然只有"逐批推进"这一个动作。
      renderTwin(scene, hist[i].x);
      $id('fieldRSub').textContent = lineFor(scene, i, hist);
      i++;
      // 画完这一帧再算下一颗种子:顺序很要紧。先画后算,这一拍的画面已经落屏,
      // 算的那几十毫秒落在两帧之间;反过来就是"算完才画",卡顿直接看得见。
      state.timer = setTimeout(() => {
        advanceSettle(scene, plan, hist.length - i);
        stepFn();
      }, i > 8 ? 260 : 320);
    } else {
      settle(inj, state.history.baseline, card, plan);
    }
  };
  stepFn();
}

/* 一拍推进一点结算的账。先补基准田(它跨卡共用,而且 loadScene 之后就在空闲里
 * 预热了,到这儿通常已经算完),再推注入田。
 *
 * `left` = 还剩几拍。**每拍固定推一轮是不够的** —— 回放的拍数等于注入田实际
 * 跑的批数,收敛早的卡只有 6 拍,而账要跑 16 轮;固定一轮一拍就只摊掉 6/16,
 * 剩下 10 轮照旧压在弹结算页那一瞬(实测 casting/p1 卡 854ms)。所以按"还剩
 * 几轮 / 还剩几拍"取整向上,把活铺满剩下的拍子:6 拍的卡每拍推 3 轮,19 拍的
 * 卡每拍推 1 轮,两种都在最后一拍前刚好算完。
 *
 * 上限 3 轮是给节拍留的余量:单轮实测中位 18~55ms、最慢 136ms,3 轮最坏约
 * 400ms 会撑破 260ms 的间隔 —— 但那是最坏值同时出现三次的情形,而 setTimeout
 * 撑破一点只是这一拍稍长,不是白屏。宁愿这里松一格,也不要把 800ms 攒到结算
 * 那一下:回放中途多 100ms 没人看得出,结算页迟半秒钟所有人都在看。 */
function advanceSettle(scene, plan, left) {
  const per = need => Math.min(3, Math.max(1, Math.ceil(need / Math.max(left, 1))));
  // 基准田也按同一个铺法补。评委没读卡面、刚 loadScene 就点卡时预热一轮都没跑
  // 完,这时一拍一轮会把整个回放耗在基准田上,注入田那 16 轮全压回结算那一瞬
  // (实测最坏 580ms)。按剩余拍数铺,两边都能在最后一拍前算完。
  if (!baselineFill(scene, plan.seeds, seedNow, per(plan.seeds))) return;
  const need = plan.seeds - plan.done;
  if (need <= 0) return;
  plan.step(per(need));
}

/* ---------- 结算 ---------- */
function settle(inj, base, card, plan) {
  state.phase = 'settled'; state.playing = false;
  const scene = SCENES[state.scene];
  drawHeat($id('heat'));   // 揭底:此刻才画真实响应面 + 三方标记
  // 揭底那一句要把评委的眼睛引到斜边上去 —— 那条线是"经验对但不完备"的证据,
  // 不指一下,它就只是背景里一条好看的虚线。没有斜边的场景不提。
  $id('heatNote').textContent = scene.mechLine
    ? '结算揭底:孪生响应面 + 三方落点 · 黄色虚线是机理斜边(口诀只盖住了它的一段)'
    : '结算揭底:孪生响应面 + 三方落点(同一把尺子)';

  // 结算只读**测出来的数**。原先这里按 card.kind === 'wrong' 分支决定金额正负
  // —— 那是循环论证:结论来自我们自己贴的标签,不是来自这一轮的度量。评委真会
  // 问"你怎么知道它是歪经" ,而正确答案必须是"我们不知道,是数据算出来的"。
  // 歪经在这个沙盘里本来就该自己露馅:批次不省、真值更低、虚高更大。
  //
  // 而且不能只看"省了几批"折钱:省批次是四条轴里最容易刷的一条,把箱子收进
  // 红线墙里就能白省。cardValue 与后端 card_score 同公式(四轴 + 折扣 + 否决),
  // 单位是"批次等价",乘单批成本就是钱 —— 前后端同一张记分卡。
  //
  // **账按 16 个种子算,不按刚演的这一轮算。** 舞台上放的仍然是 seedNow 那一轮
  // (可复现回放),但单种子的 gain_best 抖动实测 σ≈0.97,而否决线是 0.10 ——
  // 拿一轮下判决,好卡有 33.6% 的概率被误判成"被数据否决"(旗舰卡"首充慢一点"
  // 在这个种子上就正好中枪,结算出 −68 万)。演的是一轮,判的是分布。
  // 详见 engine.js 里 cardValue 上方那段。
  //
  // 这 16 轮是**回放时一拍一轮摊着算完**的(playAct 里的 advanceSettle)。
  // 整块压在这一瞬实测卡 0.29~1.60 秒,而那正是全场最关键的一下。plan.value()
  // 只把还没算完的种子补齐 —— 只有一条计算路径,所以"摊着算"和"一次算完"
  // 永远是同一个数;另写一份快路径就是又开一处会分叉的口子。
  // 没带 plan 的调用方(重置路径)照旧现算,拿到的也是同一个数。
  const v = (plan || settlePlan(scene, card.prior, {
    speaksTo: card.speaksTo || [], seed0: seedNow,
  })).value();
  const delta = v.savedBatches;
  const scrapSaved = v.savedScrap;
  const gainBest = v.gainBest;
  const money = v.score * MONEY[scene.id];
  const harmful = v.vetoed || v.score < 0;

  // 结算词挑**贡献最大的那条轴**说话 —— 每类经验的价值落点不同,一句
  // "省了 N 批"会把安全卡、观测卡、校准卡全说成零。
  // 各轴的读数都是**期望**(16 种子均值),所以带一位小数。原先是 Math.round
  // —— 单轮时批次本来就是整数,取整不丢东西;改成均值后取整会把"少试 3.4 批"
  // 说成"少试 3 批",等于在画面上把口径又偷偷改回单轮。
  const axisWhy = {
    batches: () => `这句话,让 AI 平均少试了 ${delta.toFixed(1)} 个${scene.settleMixin.noun}`,
    scrap: () => `这句话把每轮 ${v.scrapTerm.toFixed(1)} ${scrapUnit(scene)}报废挡在了它点名的那条红线外`,
    quality: () => `批次没省多少,但推荐点的真值高了 ${gainBest.toFixed(2)}${scene.rewardUnit} —— 它买的是质量不是速度`,
    honesty: () => `它把交付读数的虚高压掉了 ${v.gainHonesty.toFixed(1)}${scene.rewardUnit} —— 拦住的是把假数字发上产线的那次事故`,
    fidelity: () => `这句话声明的区间没有被执行到 —— 它在产线上是空的`,
  };
  const top = v.terms.slice().sort((a, b) => b.w - a.w)[0];
  const why = harmful
    ? (v.vetoed && gainBest < 0
      ? `这句话被数据否决了:推荐点的真值低了 ${Math.abs(gainBest).toFixed(2)}${scene.rewardUnit} —— 早点得到一个更差的答案不是加速`
      : `这句话没帮上忙 —— 数据把它压了回去`)
    : (top && top.w > 0.05 ? axisWhy[top.id]() : `这句话这一轮几乎没起作用`);

  // 大字也跟着主轴走:安全卡的大字是"少报废几托",观测卡的大字是"压掉多少
  // 虚高" —— 大字与解释词说同一件事,不能大字念批次、小字讲废品。
  // 一位小数,同上:大字是 16 种子的期望,不是某一轮的整数。
  const sgn = (n, digits) => (n > 0 ? '−' : n < 0 ? '+' : '±') + Math.abs(n).toFixed(digits);
  const axisHead = {
    batches: () => [sgn(delta, 1), ' 个' + scene.settleMixin.noun],
    scrap: () => [sgn(v.scrapTerm, 1), ' ' + scrapUnit(scene) + '报废'],
    quality: () => [(gainBest > 0 ? '+' : '') + gainBest.toFixed(2) + scene.rewardUnit, ' 推荐点真值'],
    honesty: () => [sgn(v.gainHonesty, 2) + scene.rewardUnit, ' 交付虚高'],
    fidelity: () => [sgn(delta, 1), ' 个' + scene.settleMixin.noun],
  };
  const headOf = harmful || !top || top.w <= 0.05 ? axisHead.batches() : axisHead[top.id]();
  $id('setDelta').textContent = headOf[0];
  $id('setNoun').textContent = headOf[1];
  // 废品尾巴现在是**均值**,会带小数(每轮少报废 1.4 托)。保留一位小数,
  // 别四舍五入成整数 —— "1.4 托"是期望,写成"1 托"就把口径偷偷改回单轮了。
  // 小于 0.1 托当没发生:那量级已经在种子噪声里,说出来是硬凑。
  const su = scrapUnit(scene);
  const scrapTail = Math.abs(scrapSaved) < 0.1 ? ''
    : (scrapSaved > 0
      ? (!top || top.id !== 'scrap' ? ` · 每轮少报废 ${scrapSaved.toFixed(1)} ${su}` : '')
      : ` · 每轮多报废 ${(-scrapSaved).toFixed(1)} ${su}`);
  $id('setWhy').textContent = why + scrapTail;
  $id('setMoney').textContent = (money < 0 ? '−' : '') + moneyFmt(Math.abs(money));
  $id('setMoney').classList.toggle('settle-neg', money < 0);
  // 脚注里明写种子数。台上演的是一轮,账是 16 轮的期望 —— 这个口径必须写在
  // 画面上,不能只活在注释里:评委看到"少试 3 批"却数出屏幕上少了 5 批时,
  // 差异的原因得是他能读到的,不是他得来问的。
  $id('setFoot').textContent = scene.settleMixin.line
    + ` · ${v.seeds} 种子均值 · 金额量级为行业公开估算`;
  $id('settle').classList.remove('hidden');

  $id('statR').textContent = inj.nBatches + ' 批';
  $id('fieldRSub').textContent = '收束 → ' + inj.nBatches + ' 批,真值 ' + inj.trueBest.toFixed(1) + scene.rewardUnit;
  updateMeters(scene, inj.history, inj.history.length - 1);

  if (state.pkPoint) fillPkPanel(scene, inj, base, card);
}

/* ---------- 同题对照三栏 ----------
 * 三方用同一把尺子:孪生沙盘的无噪声真值(reward),撞红线按整托报废计。
 * 人的落点在"其余维切中点"的同一切片上结算 —— 和评委看的热力图同一口径。 */
function fillPkPanel(scene, inj, base, card) {
  const xH = state.pkPoint;
  const feasH = scene.feasible(xH);
  const yH = scene.reward(xH) - (feasH ? 0 : scene.scrapPenalty);
  const su = scrapUnit(scene);
  const cols = [
    { el: 'pkColH', x: xH, batches: '一次试制', val: yH, feas: feasH },
    { el: 'pkColB', x: base.bestX, batches: base.nBatches + ' 批搜索 · 报废 ' + base.scrapped + ' ' + su, val: base.trueBest, feas: true },
    { el: 'pkColI', x: inj.bestX, batches: inj.nBatches + ' 批搜索 · 报废 ' + inj.scrapped + ' ' + su, val: inj.trueBest, feas: true },
  ];
  const m = heatInfo;
  let win = 0;
  cols.forEach((c, i) => { if (c.val > cols[win].val) win = i; });
  cols.forEach((c, i) => {
    const root = $id(c.el);
    root.classList.toggle('pk-win', i === win);
    root.querySelector('.pk-batches').textContent = c.batches;
    const pA = scene.params[m.px], pB = scene.params[m.py];
    root.querySelector('.pk-params').textContent =
      `${c.x[m.px].toFixed(pA.decimals)}${pA.unit} / ${c.x[m.py].toFixed(pB.decimals)}${pB.unit}`;
    const vEl = root.querySelector('.pk-val');
    if (!c.feas) {
      vEl.textContent = `这一${su}报废了`;
      vEl.classList.add('pk-scrap');
    } else {
      vEl.textContent = scene.rewardName + ' ' + c.val.toFixed(1) + scene.rewardUnit;
      vEl.classList.remove('pk-scrap');
    }
  });

  let verdict;
  if (!feasH) {
    const r = scene.risks(xH);
    const worst = r.li >= r.gas ? scene.redLines[0] : scene.redLines[1];
    verdict = `您落进了${worst.label}区(${worst.desc})—— 老师傅那句话拦住的正是这个。`;
  } else if (win === 0) {
    // 这里也按测出来的数分支,不按 card.kind:这一轮注入田是不是真的不如
    // 裸跑,是这一轮算出来的,不是卡片标签说的。
    verdict = inj.trueBest < base.trueBest
      ? '您的直觉赢了,而这句话反而把 AI 带偏了 —— 这正是要把**对的**经验注入 AI 的原因。'
      : '您的直觉赢了 —— 这样的手感,值得变成下一张经验卡。';
  } else if (win === 2) {
    verdict = '人给方向,AI 给验证 —— 相乘,不是替代。';
  } else {
    verdict = `AI 裸跑这次领先,但它多花了 ${base.nBatches} 批和 ${base.scrapped} ${su}废品才到这儿。`;
  }
  $id('pkVerdict').textContent = verdict;
  $id('pkPanel').classList.remove('hidden');
}

/* ---------- 落子 ---------- */
function onHeatClick(ev) {
  if (!state.pkMode || state.playing || state.phase === 'settled') return;
  const hm = $id('heat');
  const rect = hm.getBoundingClientRect();
  const m = heatInfo || heatMapping(hm.clientWidth, hm.clientHeight);
  const scene = SCENES[state.scene];
  const xv = heatToX(m, ev.clientX - rect.left);
  const yv = heatToY(m, ev.clientY - rect.top);
  state.pkPoint = scene.params.map((p, i) => {
    if (i === m.px) return xv;
    if (i === m.py) return yv;
    return (p.lo + p.hi) / 2;
  });
  drawHeat(hm);   // 只画标记与坐标,不显示任何预测/真值 —— 落子无悔
  const pA = scene.params[m.px], pB = scene.params[m.py];
  $id('heatNote').textContent =
    `已落子 ${xv.toFixed(pA.decimals)}${pA.unit} / ${yv.toFixed(pB.decimals)}${pB.unit}` +
    ' —— 数值等亮牌。现在抽一张经验卡,看 AI 怎么走';
}

function togglePk() {
  state.pkMode = !state.pkMode;
  $id('pkToggle').classList.toggle('active', state.pkMode);
  // 十字光标 + 描边:告诉手"这块现在可以点"。不加动画。
  $id('heat').classList.toggle('pk-armed', state.pkMode);
  $id('pkToggle').textContent = state.pkMode ? '退出手感对照' : '试试您的手感';
  $id('heatNote').textContent = state.pkMode
    ? '同题对照:在图上点一个您认为最优的位置(只能试一次)'
    : '悬浮:把口诀的关键维度照亮';
  if (!state.pkMode) {
    state.pkPoint = null;
    $id('pkPanel').classList.add('hidden');
    drawHeat($id('heat'));
  }
}

/* ---------- 重置 ---------- */
function resetAll() {
  state.playing = false; state.card = null; state.phase = 'idle';
  state.pkPoint = null;
  clearTimeout(state.timer);
  heatShrunk = false;
  $id('settle').classList.add('hidden');
  $id('pkPanel').classList.add('hidden');
  loadScene(state.scene);
  $id('heatNote').textContent = state.pkMode
    ? '同题对照:在图上点一个您认为最优的位置(只能试一次)'
    : '悬浮:把口诀的关键维度照亮';
}

/* ---------- boot ---------- */
function boot() {
  document.body.insertAdjacentHTML('beforeend', document.getElementById('tpl-main').innerHTML);
  document.getElementById('tpl-main').remove();

  document.querySelectorAll('.scene-btn').forEach(b =>
    b.addEventListener('click', () => {
      if (!state.playing) { state.card = null; state.pkPoint = null; state.phase = 'idle'; heatShrunk = false; loadScene(b.dataset.scene); }
    }));

  $id('resetBtn').addEventListener('click', resetAll);
  $id('settleAgain').addEventListener('click', resetAll);
  $id('settleClose').addEventListener('click', () => $id('settle').classList.add('hidden'));
  $id('pkToggle').addEventListener('click', togglePk);
  $id('heat').addEventListener('click', onHeatClick);

  // ③ 标定页。每次打开都**重新跑**一遍特征自检(不缓存):这页卖的就是
  // "当场算",缓存住第一次的结果等于把它变回一张写死的奖状。切场景后再打开,
  // 自检也会照当前场景重算 —— 两个场景的机理骨架本来就不同。
  $id('calibBtn').addEventListener('click', () => {
    renderCalib(state.scene);
    $id('calib').classList.remove('hidden');
  });
  $id('calibClose').addEventListener('click', () => $id('calib').classList.add('hidden'));
  // 标定页是旁证不是流程,Esc 就该能退。结算页不给 Esc —— 那一下是要人读完的。
  window.addEventListener('keydown', e => {
    if (e.key === 'Escape') $id('calib').classList.add('hidden');
  });

  let rT;
  window.addEventListener('resize', () => {
    clearTimeout(rT);
    rT = setTimeout(() => {
      drawHeat($id('heat'));
      if (state.history) {
        drawCurve($id('curveL'), state.history.baseline.history, '#0f766e', SCENES[state.scene].rewardMax, 24);
        if (state.history.injected) drawCurve($id('curveR'), state.history.injected.history, '#c2410c', SCENES[state.scene].rewardMax, 20);
      }
      // 机理视窗按记住的那一批重画,不重跑 BO —— 拉窗口不该改变演示的内容。
      renderTwin(SCENES[state.scene], state.twinX);
    }, 150);
  });

  loadScene('formation');
}

window.addEventListener('DOMContentLoaded', boot);
