/* api.js — 后端探测 + 双模适配器。
 *
 * 这个文件回答一个问题:**演示现场网络挂了怎么办。**
 *
 * 三条硬纪律,每一条都是踩过的坑:
 *
 * 1) `file://` 双击必须照旧能演。所以非 http(s) 协议下**一次请求都不发** ——
 *    连探测都不发。断网兜底是这个作品的最后一层降级,不能为了"后端联通"
 *    把它牺牲掉:双击打开时浏览器对 file:// 的 fetch 会直接抛,那一下就是
 *    一个红色的 console 和一个不动的徽章。
 *
 * 2) 客户端轮询预算必须**大于**服务端最坏耗时。后端 /api/translate 的 LLM
 *    超时是 45s(超了自己落规则引擎并返回 done),/api/explain 是 15s。前端
 *    预算给小了会出现最坏情况:后端明明算完了、前端已经放弃 —— 评委看到的
 *    是"它坏了",而它其实好着。所以 translate 给 58s、explain 给 26s。
 *
 * 3) **永不把裸 fetch 错误渲染到屏幕上。** 任何失败都翻成中文一句话,并且
 *    当场把徽章切回"本地兜底" —— 台上 kill 掉后端,画面上应该是徽章变了、
 *    演示继续,不是一个 TypeError。
 *
 * 还有一条结构纪律:**浏览器不编译先验。** 断网时自由输入走的不是"JS 版
 * 编译器"(那就是第二套语义,这个项目已经因此分叉过一次:同一张歪经卡后端
 * 否决 −3.76,前端却白省 2.00 批),而是"按维度重合度对上一张预制卡的先验,
 * 并且把这件事明写在翻译卡上"。降级要看得见,不能悄悄换一份东西。
 */
'use strict';

const API = (() => {
  // http(s) 之外一律当没有后端:file:// 下 fetch 会抛,而那一下毫无价值。
  const PROBE_OK = location.protocol === 'http:' || location.protocol === 'https:';
  const HEALTH_MS = 1800;      // 探测:短。探不到就是没有,不值得等
  const TR_BUDGET_MS = 58000;  // > 后端 LLM_TIMEOUT_S(45s)+ 编译与网络余量
  const EX_BUDGET_MS = 26000;  // > 后端 EXPLAIN_LLM_TIMEOUT_S(15s)+ 余量
  const POLL_MS = 700;

  let mode = 'local';          // 'server' | 'local'
  let health = null;

  /* ---------- 徽章 ----------
   * 徽章是**探测结果**,不是我们写死的一句话。它写死了这个演示就没法回答
   * "把后端关掉会怎样"—— 而那正是评委最爱当场试的一下。 */
  function paintBadge() {
    const el = document.getElementById('engineBadge');
    const tx = document.getElementById('ebText');
    if (!el || !tx) return;
    const server = mode === 'server';
    el.classList.toggle('eb-server', server);
    el.classList.toggle('eb-local', !server);
    el.querySelector('.eb-dot').textContent = server ? '●' : '○';
    tx.textContent = server
      ? (health && health.llm_configured ? '服务端 GP-BO · LLM 在线' : '服务端 GP-BO')
      : (PROBE_OK ? '本地兜底' : '本地兜底 · 离线');
    el.title = server
      ? '寻优在服务端跑(Python GP-BO);翻译走 LLM,超时自动落规则引擎'
      : '没有后端:寻优、结算、追问全部在浏览器里跑,同一份编译好的先验';
  }

  function setMode(m) {
    if (mode === m) return;
    mode = m;
    paintBadge();
  }

  /* 带超时的 fetch。失败一律 throw 一个**已经翻成中文**的 Error ——
   * 上层只管把 e.message 显示出去,不需要认识 TypeError/AbortError。 */
  async function jfetch(path, opts, ms) {
    const ac = new AbortController();
    const t = setTimeout(() => ac.abort(), ms);
    try {
      const r = await fetch(path, Object.assign({ signal: ac.signal }, opts || {}));
      if (!r.ok) throw new Error('后端返回 ' + r.status);
      return await r.json();
    } catch (e) {
      // 网络断了 / 后端被 kill / 超时 —— 对演示来说是同一件事:没有后端了。
      setMode('local');
      throw new Error(e && e.name === 'AbortError' ? '后端没有在时限内回应' : '连不上后端');
    } finally {
      clearTimeout(t);
    }
  }

  const sleep = (ms) => new Promise(r => setTimeout(r, ms));

  /* ---------- 探测 ---------- */
  async function probe() {
    paintBadge();
    if (!PROBE_OK) return mode;              // file://:一次请求都不发
    try {
      health = await jfetch('api/health', null, HEALTH_MS);
      setMode(health && health.ok ? 'server' : 'local');
    } catch (e) {
      health = null;
      setMode('local');
    }
    paintBadge();
    return mode;
  }

  /* ================================================================
   * 翻译:自由输入 → 可审计的先验
   * ================================================================ */

  /* 后端路径:POST 拿 job_id,再轮询。HTTP handler 那侧不等慢活,所以等在这儿。
   * onTick 每拍报一次已等秒数 —— 让"在算"和"卡死"在画面上分得开。 */
  async function serverTranslate(scene, utterance, cardId, onTick) {
    const body = JSON.stringify({ scene, utterance, card_id: cardId || null });
    const first = await jfetch('api/translate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body,
    }, 8000);
    if (first.status === 'done') return first.translation;

    const t0 = Date.now();
    while (Date.now() - t0 < TR_BUDGET_MS) {
      await sleep(POLL_MS);
      if (onTick) onTick(Math.round((Date.now() - t0) / 1000));
      const r = await jfetch('api/translate/' + first.job_id, null, 8000);
      if (r.status === 'done') return r.translation;
    }
    // 预算烧完:后端那侧最坏 45s 就该落规则引擎并返回 done,走到这儿说明
    // 连轮询都在丢包。当没有后端处理,让上层去走本地兜底。
    setMode('local');
    throw new Error('翻译等太久了,已切到本地兜底');
  }

  /* ---------- 本地兜底翻译 ----------
   * 不编译。只做两件浏览器有资格做的事:
   *   a) 用后端导出的**同一份**词表(faq.js 里的 LOCAL_DIM_WORDS)听出这句话
   *      说的是哪几维、什么方向;
   *   b) 在已编译好的预制卡里挑维度重合度最高的一张,借它的先验,并且把
   *      "这是借来的"写在卡上。
   * 挑不到就整句停靠 —— 宁可说"接不住",不要现编一份先验。 */
  function heardDims(scene, text) {
    const tbl = (window.LOCAL_DIM_WORDS && window.LOCAL_DIM_WORDS.dim_words) || {};
    const t = String(text || '').toLowerCase();
    return scene.params
      .map(p => p.name)
      .filter(name => (tbl[name] || []).concat([name]).some(w => t.includes(String(w).toLowerCase())));
  }

  function heardTone(text) {
    const w = window.LOCAL_DIM_WORDS || {};
    const t = String(text || '');
    const any = (list) => (list || []).some(k => t.includes(k));
    const out = [];
    if (any(w.forbid)) out.push('这是一句"别这么干"');
    if (any(w.down)) out.push('方向是往小/往慢');
    if (any(w.up)) out.push('方向是往大/往高');
    if (any(w.flat)) out.push('说的是"这一段差不多"');
    if (any(w.fake)) out.push('说的是"这儿的读数不可信"');
    if (any(w.couple)) out.push('两个旋钮要联动');
    if (any(w.risk)) out.push('宁可保守');
    return out;
  }

  /* 借卡:维度重合度优先,同分时取声明区间更保守(volume_cut 更大)的那张。 */
  function nearestCard(scene, dims) {
    const deck = (typeof cardsOf === 'function') ? cardsOf(scene) : [];
    let best = null, bestHit = 0;
    for (const c of deck) {
      if (c.broken || !c.prior || c.kind === 'wrong') continue;   // 歪经卡不借
      const cd = (c.audit && c.audit.affected_dims) || [];
      const hit = cd.filter(n => dims.indexOf(n) >= 0).length;
      const cut = (c.audit && c.audit.volume_cut) || 0;
      if (hit > bestHit || (hit === bestHit && hit > 0 && best && cut > ((best.audit && best.audit.volume_cut) || 0))) {
        best = c; bestHit = hit;
      }
    }
    return bestHit > 0 ? best : null;
  }

  function localTranslate(scene, utterance) {
    const dims = heardDims(scene, utterance);
    const tone = heardTone(utterance);
    const lent = dims.length ? nearestCard(scene, dims) : null;
    const ir = lent ? lent.ir : null;
    const bounds = scene.params.map((p, i) => {
      const b = ir && ir.bounds && ir.bounds[i];
      const lo = b ? b[0] : p.lo, hi = b ? b[1] : p.hi;
      return { param: p.name, lo, hi, narrowed: lo > p.lo + 1e-9 || hi < p.hi - 1e-9 };
    });
    const parked = [];
    if (!dims.length) {
      parked.push({
        fragment: utterance,
        reason: '这句话没有对应到当前参数空间里的任何维度 —— 本地词表接不住',
      });
    } else if (!lent) {
      parked.push({
        fragment: utterance,
        reason: '听出了维度,但没有哪张已编译的卡覆盖它 —— 浏览器不编译先验,所以这句话只能停靠',
      });
    }
    return {
      // 借来的先验带借来的置信度;没借到就没有置信度可报(不是 0,是"没有")
      confidence: lent ? (ir.confidence == null ? null : ir.confidence) : null,
      affected_dims: dims,
      notes: (lent ? (ir.notes || []).slice() : []).concat(tone.length ? ['听出的措辞:' + tone.join('、')] : []),
      bounds,
      hard_cuts: (ir && ir.exclusions ? ir.exclusions.length : 0),
      volume_cut: (lent && lent.audit && lent.audit.volume_cut) || 0,
      parked,
      rejected: [],
      downgraded: [],
      ir,
      utterance,
      rationale: lent
        ? ('断网兜底:浏览器不编译先验,这句话按维度重合借用了已编译卡《' + lent.text + '》的先验')
        : '断网兜底:浏览器不编译先验,这句话没有可借的已编译先验',
      engine: lent ? 'local-card' : 'local-park',
      fallback_reason: PROBE_OK ? '连不上后端,已切本地兜底' : '离线打开(file://),本来就没有后端',
      llm_model: null,
      has_llm_raw: false,
      lent_from: lent ? { id: lent.id, text: lent.text } : null,
    };
  }

  /* 对外只有一个 translate:上层不需要知道这次是谁翻的,view.engine 里写着。 */
  async function translate(scene, utterance, opts) {
    const o = opts || {};
    if (mode === 'server') {
      try {
        return await serverTranslate(scene.id, utterance, o.cardId, o.onTick);
      } catch (e) {
        // 后端半路没了 —— 不把错误抛给界面,直接兜底,并让 view 自己说清楚
        const v = localTranslate(scene, utterance);
        v.fallback_reason = e.message + ',已切本地兜底';
        return v;
      }
    }
    return localTranslate(scene, utterance);
  }

  /* ================================================================
   * 追问:已经发生的数据 → 人话(只读)
   * ================================================================ */
  async function serverExplain(sceneId, question, context, onTick) {
    const first = await jfetch('api/explain', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ scene: sceneId, question, context: context || {} }),
    }, 8000);
    const t0 = Date.now();
    while (Date.now() - t0 < EX_BUDGET_MS) {
      await sleep(POLL_MS);
      if (onTick) onTick(Math.round((Date.now() - t0) / 1000));
      const r = await jfetch('api/explain/' + first.job_id, null, 8000);
      if (r.status === 'done') {
        return { answer: r.answer, source: r.source, llm_reason: r.llm_reason };
      }
    }
    setMode('local');
    throw new Error('追问等太久了');
  }

  /* 本地 FAQ:与后端 explain.py 的第二层是**同一份文案**(faq.js 是从它导的)。
   * 命中不了就念固定兜底句 —— 绝不空屏、绝不编数。 */
  function localExplain(question) {
    const bank = window.EXPLAIN_FAQ || { items: [], fallback: '这个问题留给答辩环节。' };
    const t = String(question || '').trim().toLowerCase();
    let best = null, bestHits = 0;
    for (const it of (bank.items || [])) {
      const hits = (it.keywords || []).filter(k => t.includes(String(k).toLowerCase())).length;
      if (hits > bestHits) { bestHits = hits; best = it.answer; }
    }
    return bestHits > 0
      ? { answer: best, source: 'faq', llm_reason: '本地兜底:没有后端' }
      : { answer: bank.fallback, source: 'fallback', llm_reason: '本地兜底:没有后端' };
  }

  async function explain(sceneId, question, context, opts) {
    const o = opts || {};
    if (mode === 'server') {
      try {
        return await serverExplain(sceneId, question, context, o.onTick);
      } catch (e) {
        const r = localExplain(question);
        r.llm_reason = e.message + ',已落本地 FAQ';
        return r;
      }
    }
    return localExplain(question);
  }

  return {
    probe, translate, explain, localExplain, localTranslate,
    get mode() { return mode; },
    get health() { return health; },
    get probeAllowed() { return PROBE_OK; },
    paintBadge,
  };
})();
