/* twin.js — 机理视窗:把一批的五个参数,还原成那一批真正会发生的事。
 *
 * 为什么要有这块。收敛曲线只说"第几批到了多少分",热力图只说"哪块地好" ——
 * 两者都是**统计口吻**。评委问的下一句一定是"你这沙盘凭什么算得准",而统计
 * 口吻答不了这句。能答的只有机理口吻:同样这批参数,电芯里的电压、电流、
 * 负极电位是怎么走的,为什么 0.06C 的 SEI 更好、为什么低温大倍率会析锂。
 *
 * 纪律三条,写在最前面免得后人"顺手美化":
 *   1) 这条曲线是**沙盘机理规则的可视化**,不是实测数据,也不冒充实测。
 *      画面上永远带"定性"字样,来源在③标定页里逐条写明是哪条公开电化学规律。
 *   2) 完全确定性 —— 同一组参数永远得到同一条线,没有 Math.random。
 *      演示要能复现,曲线也一样。
 *   3) 横轴**固定 24h**,不按每批自适应。快慢必须看得见:0.06C 那批画到图外,
 *      1C 那批只占左边一小截 —— 自适应横轴会把"慢"这个代价悄悄藏掉。
 *
 * 一处诚实的缺口:五个参数里"高温老化时长"在这张图上**看不见** —— 老化发生在
 * 充电之后。那正是老化卡的价值不落在这张图上、而落在结算页"交付虚高"那条轴
 * 上的原因。图里显式写出这句,比假装五维全可视化可信。
 */
'use strict';

const TWIN_XMAX = 18;        // 小时,固定图幅
const TWIN_VLO = 2.85, TWIN_VHI = 3.78;
const TWIN_VCUT = 3.65;      // 恒压段电压
const TWIN_MAIN_C = 0.33;    // 预充切换之后的主恒流倍率(工艺常量,不是被优化的参数)
const TWIN_IMAX = 1.10;      // 电流副轴上界(C)

const tcl = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

/* 开路电压。磷酸铁锂是两相反应 —— 中段是一条**平台**,不是斜坡。
 * 这条平台正是它难测 SOC 的原因,也是化成阶段只能靠电流密度而不能靠电压
 * 判断 SEI 长得好不好的原因。三段:入口陡升 / 平台微斜 / 出口陡升。 */
function twinOcv(s) {
  if (s < 0.04) return 3.00 + (3.26 - 3.00) * (s / 0.04);
  if (s < 0.92) return 3.26 + 0.075 * ((s - 0.04) / 0.88);
  return 3.335 + (3.62 - 3.335) * Math.pow((s - 0.92) / 0.08, 1.4);
}

/* 极化系数(V per C):温度越低,离子输运越慢,同样电流要多顶起来这么多电压。
 * 与沙盘里"低温 × 高倍率 → 析锂"是同一个物理源头 —— 顶起来的那部分电压,
 * 有一份落在负极上,把负极电位压到 0V vs Li 以下就开始镀锂。 */
function twinPolar(T) { return 0.16 * (1 + 1.1 * tcl((35 - T) / 22, 0, 1.2)); }

/* 端电压 = 开路电压 + 极化。高 SOC 段极化再放大 —— 满电附近离子迁移更吃力。 */
function twinV(s, I, k) {
  return twinOcv(s) + k * I * (1 + 0.6 * Math.max(0, (s - 0.85) / 0.15));
}

/* 负极电位余量(V vs Li/Li+)。**这是整张图的要害**:镀锂不是"温度低"或
 * "电流大"单独造成的,是两者一起把负极电位推到 0 以下造成的 —— 所以析锂线
 * 在 (倍率,温度) 平面上是一道**斜边**,而不是两条独立的直线。
 * 与 scenarios.js 的 platingOnset(pre) = 24 + 36×倍率 同源:余量在
 * T = platingOnset(pre) 处刚好归零,这样视窗与沙盘不会各说一套。 */
function twinAnodeMargin(I, T) {
  return 0.09 * (T - (24 + 36 * I)) / 30;
}

/* 预充段的 SOC 上限(荷电分数)。预充段有**两个**出口,谁先到算谁:
 *   · 电压先顶到切换电压 —— 切换电压设在平台(3.26V)以下时走这条,
 *     于是预充段止于平台入口,这一段积到的电量 ≈ 0.04,时长 ≈ 0.04/倍率。
 *   · SOC 先到这个上限 —— 切换电压设在平台以上时走这条(如 3.40V 那张卡)。
 * 没有第二个出口的话,高切换电压那几张卡的预充段会一路开到 3.65V,主恒流段
 * 直接消失,图上看不出"预充"和"主充"两段 —— 那不是简化,是把工艺画错了。
 *
 * 顺带说清一件容易误读的事:总时长主要由主恒流段(固定 0.33C)决定,所以
 * "首充 0.06C"和"1C 拉满"的**总工时**差得并不多(约 6.9h vs 5.1h)。首充慢
 * 一点买的不是工时,是 SEI 致密度 —— 代价落在预充段这一小截的时长上
 * (0.67h vs 0.04h,差 16 倍),收益落在首效上。图上要让人看清的是这个,
 * 不是总长度。 */
const TWIN_PRE_SOC = 0.15;

/* ---------- 一批 = 一条 CC-CV 曲线 ----------
 * 四段,与真实化成工艺同序:
 *   预充恒流(小倍率养 SEI)→ 到切换电压 → 主恒流 → 到 3.65V → 恒压电流衰减。
 * 返回等时间步的采样点,外加落在曲线上的关键事件标记。
 * 纯函数,无随机 —— 同一组参数永远同一条线。 */
function twinCurve(x, scene) {
  const [pre, T, sw, cut] = x;      // age 不在这张图上 —— 老化发生在充电之后
  const k = twinPolar(T);
  const dt = 0.02;                  // 小时

  /* 析锂判据只看**预充段**,而且是一个标量,不是逐点扫出来的:
   *   · 只看预充段 —— 沙盘的 platingOnset(pre) 说的就是预充倍率那一段。原先
   *     逐点用当时电流判,于是主恒流 0.33C 在 30℃ 下也算析锂,基准批平白 203
   *     个点全红。那不是更严格,是拿一条规则去管它没说过的地方:视窗会在沙盘
   *     判可行的批次上画满红,两边各说一套。
   *   · 是标量 —— 预充段里 I=pre、T 恒定,余量根本不随时间变。而逐点扫有个更
   *     阴的坑:1C 那批的预充段短到一个采样点都落不进去(电压一上电就顶过切换
   *     电压),扫出来的结果是"1C 拉满毫无析锂风险",与沙盘的判决正好相反。
   *     整张图最该讲清的那句歪经,会被一个采样精度问题讲成反的。 */
  const preMargin = twinAnodeMargin(pre, T);

  /* **红不红由沙盘说,不由视窗自己说。**
   * 余量是给人看的连续量(离析锂还有多远),但"这一批算不算析锂"必须回到
   * scene.liRisk(x) —— 沙盘用的是有坡度的 loGate,墙在 0.55;视窗要是自己拿
   * margin<0 当判据,就是同一条规则的第二份实现,立刻分叉:基准批 0.22C/30℃
   * 在沙盘是 liRisk=0.19(可行,照样跑完),视窗却把它画成析锂报废。评委同屏
   * 能看到收敛曲线上这批有读数、机理图上这批却全红 —— 一眼的自相矛盾。
   * 拿不到 scene 时退回 margin<0,只用于单元测试;页面上永远传 scene。 */
  const liRisk = scene && scene.liRisk ? scene.liRisk(x) : null;
  const plating = liRisk == null ? preMargin < 0 : liRisk > 0.55;

  const pts = [];
  let s = 0, t = 0, phase = 'pre', I = pre;
  let swAt = null, cvAt = null, doneAt = null;
  let cutShort = false;             // 预充被电压提前掐断 = SEI 没长够

  while (t <= TWIN_XMAX + 1e-9) {
    if (phase === 'pre') {
      I = pre;
      // 两个出口:养够了(SOC 到位),或者电压先顶到切换点(低温高倍率的情形)。
      if (s >= TWIN_PRE_SOC) { phase = 'cc'; swAt = { t, v: twinV(s, I, k) }; }
      else if (twinV(s, I, k) >= sw) {
        phase = 'cc'; cutShort = true; swAt = { t, v: twinV(s, I, k) };
      }
    }
    if (phase === 'cc') {
      I = TWIN_MAIN_C;
      if (twinV(s, I, k) >= TWIN_VCUT) { phase = 'cv'; cvAt = { t, v: TWIN_VCUT }; }
    }
    if (phase === 'cv') {
      // 恒压段:电压钉在 3.65V,电流按容量缺口指数衰减,到截止电流收工。
      I = Math.max(0.004, TWIN_MAIN_C * Math.exp(-(t - cvAt.t) / 1.5));
      if (I <= cut) { doneAt = { t, v: TWIN_VCUT }; break; }
    }
    const v = phase === 'cv' ? TWIN_VCUT : twinV(s, I, k);
    pts.push({ t, v, i: I, phase, margin: phase === 'pre' ? preMargin : 1 });
    s = Math.min(1, s + I * dt);
    t += dt;
  }
  return {
    pts, swAt, cvAt, doneAt, cutShort,
    preMargin, liRisk, plating,
    // 预充段的起止。析锂底纹按**这段区间**画,不按"余量为负的采样点" ——
    // 短到没有采样点的预充段照样要能画出来(见 preMargin 上面那段注释)。
    preSpan: { from: 0, to: swAt ? swAt.t : (doneAt ? doneAt.t : TWIN_XMAX) },
    // 图外收工 = 这批**慢到画不完**。不裁剪横轴,就得显式说出来。
    overrun: !doneAt,
    hours: doneAt ? doneAt.t : null,
  };
}

/* ---------- 画一批 ----------
 * 深底小图,嵌在经验田标题下方。规矩:
 *   · 只画这一批,不叠历史 —— 叠上去就变成"一团线",看不出任何一批的形状。
 *   · 电压主轴(左)、电流副轴(右)。电流用虚线,免得与电压抢主体。
 *   · 析锂批画红色底纹 + 一枚落在预充段上的红点;可行批什么都不画。
 *     报废的**物理原因**要看得见,不能只是一个"不可行"的布尔量。
 * 静态一帧,由外部逐批调用 —— 自己不起动画,不破同屏单动。 */
function drawTwin(canvas, x, scene, label) {
  const g = canvas.getContext('2d');
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 400, cssH = canvas.clientHeight || 96;
  canvas.width = cssW * dpr; canvas.height = cssH * dpr;
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, cssW, cssH);

  // 屏底与热力图/仪表同一个值,取 app.js 的 THEME(运行时才求值,加载顺序无关)
  g.fillStyle = THEME.screen;
  g.fillRect(0, 0, cssW, cssH);

  const padL = 30, padR = 26, padT = 9, padB = 15;
  const w = cssW - padL - padR, h = cssH - padT - padB;
  if (w < 40 || h < 24) return;
  const px = t => padL + (t / TWIN_XMAX) * w;
  const py = v => padT + (1 - (v - TWIN_VLO) / (TWIN_VHI - TWIN_VLO)) * h;
  const pi = i => padT + (1 - i / TWIN_IMAX) * h;

  if (!x) {
    g.fillStyle = 'rgba(186,198,213,.5)';
    g.font = '10.5px system-ui'; g.textAlign = 'center';
    g.fillText('等一张经验卡 —— 每跑一批,这里画那一批的充电曲线', cssW / 2, cssH / 2 + 3);
    return;
  }

  const c = twinCurve(x, scene);

  // 析锂底纹:预充段那一截。宽度按时间算,至少 2px —— 1C 的预充段只有 0.04h,
  // 按比例画出来不到一个像素,而那恰恰是最该被看见的一批。
  if (c.plating) {
    const x0 = px(c.preSpan.from), x1 = Math.max(px(c.preSpan.to), x0 + 2);
    g.fillStyle = 'rgba(146,47,55,.42)';
    g.fillRect(x0, padT, x1 - x0, h);
  }

  // 3.65V 截止线 + 3.26V 平台线。两条参考线是"读图的坐标",不是装饰:
  // 没有它们,那条平台看起来只是一段随便的斜线。
  g.setLineDash([3, 3]); g.lineWidth = 1;
  g.strokeStyle = 'rgba(186,198,213,.26)';
  for (const [v, txt] of [[TWIN_VCUT, '3.65'], [3.26, '3.26']]) {
    g.beginPath(); g.moveTo(padL, py(v)); g.lineTo(padL + w, py(v)); g.stroke();
    g.fillStyle = 'rgba(186,198,213,.55)';
    g.font = '8.5px "SF Mono", ui-monospace, monospace';
    g.textAlign = 'right'; g.fillText(txt, padL - 3, py(v) + 3);
  }
  g.setLineDash([]);

  // 电流(副轴,虚线)
  g.strokeStyle = 'rgba(42,107,255,.85)'; g.lineWidth = 1.3;
  g.setLineDash([4, 3]); g.beginPath();
  c.pts.forEach((p, n) => { const X = px(p.t), Y = pi(p.i); n ? g.lineTo(X, Y) : g.moveTo(X, Y); });
  g.stroke(); g.setLineDash([]);

  // 电压(主轴,实线)
  g.strokeStyle = THEME.gold; g.lineWidth = 1.9; g.beginPath();
  c.pts.forEach((p, n) => { const X = px(p.t), Y = py(p.v); n ? g.lineTo(X, Y) : g.moveTo(X, Y); });
  g.stroke();

  // 切换点 / 转恒压点。两个工艺事件,标出来才知道三段是怎么分的。
  for (const [ev, col] of [[c.swAt, THEME.ink1], [c.cvAt, THEME.gold]]) {
    if (!ev) continue;
    g.fillStyle = col; g.beginPath();
    g.arc(px(ev.t), py(ev.v), 2.2, 0, Math.PI * 2); g.fill();
  }

  /* 析锂标记。这里有个真实的呈现难题,解法要说清楚:
   * 1C 那批的预充段只有 0.04h —— 按 18h 图幅**如实**画,底纹宽不到一个像素,
   * 于是"最该被看见的一批"在图上几乎看不见。把底纹加宽是不行的:那等于谎报
   * 这段有多长,而这张图卖的就是"快慢看得见"。
   * 所以处理方式是:底纹如实(窄就是窄),**标记**做重 —— 一条竖引线 + 顶部
   * 一枚"析锂"角标,位置精确落在那一小截上。看得见靠标记,不靠把事实画胖。 */
  if (c.plating) {
    const tm = (c.preSpan.from + c.preSpan.to) / 2;
    const X = Math.max(px(tm), padL + 1);
    g.strokeStyle = 'rgba(226,86,95,.9)'; g.lineWidth = 1.4;
    g.beginPath(); g.moveTo(X, padT); g.lineTo(X, padT + h); g.stroke();

    const vm = c.pts.length ? c.pts[0].v : 3.1;
    g.fillStyle = THEME.li;
    g.beginPath(); g.arc(X, py(vm), 3.2, 0, Math.PI * 2); g.fill();

    // 角标压在图内顶部,不挤出画布 —— 窄预充段时引线在最左边,标签左对齐。
    const tag = '析锂 · 负极余量 ' + c.preMargin.toFixed(3) + 'V';
    g.font = '8.5px "SF Mono", ui-monospace, monospace';
    const tw = g.measureText(tag).width + 8;
    const tx = Math.min(X + 4, padL + w - tw);
    g.fillStyle = 'rgba(146,47,55,.92)';
    g.fillRect(tx, padT + 1, tw, 12);
    g.fillStyle = '#FFE8EA'; g.textAlign = 'left';
    g.fillText(tag, tx + 4, padT + 10);
  }

  // 图外收工:横轴固定 18h,画不完的批次要在右边缘明说,不能悄悄截断。
  if (c.overrun) {
    g.fillStyle = 'rgba(226,86,95,.95)';
    g.font = '8.5px "SF Mono", ui-monospace, monospace'; g.textAlign = 'right';
    g.fillText('未完 →', padL + w, padT + 8);
  }

  // 角注:一行读数。这一批的工艺事实,不是形容词。
  g.font = '9px "SF Mono", ui-monospace, monospace'; g.textAlign = 'left';
  g.fillStyle = 'rgba(186,198,213,.78)';
  const pre = x[0], T = x[1];
  const bits = [
    `${pre.toFixed(2)}C/${T.toFixed(0)}℃`,
    `预充 ${(c.swAt ? c.swAt.t : 0).toFixed(2)}h`,
    c.hours ? `共 ${c.hours.toFixed(1)}h` : '共 >18h',
  ];
  if (c.cutShort) bits.push('SEI 未长够');
  if (c.plating) bits.push('析锂');
  g.fillText(bits.join(' · '), padL, cssH - 4);

  // 右下角永远写"定性":这条线是机理规则的可视化,不是实测数据。
  g.textAlign = 'right'; g.fillStyle = 'rgba(124,140,160,.85)';
  g.fillText(label || '定性机理 · 非实测', padL + w, cssH - 4);
}

