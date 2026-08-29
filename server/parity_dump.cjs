/* parity_dump.cjs — BO 轨迹跨语言对账的 JS 一半。
 *
 * 只做一件事:把 JS 引擎在「两个场景 × 全部卡(含无先验) × 多种子」上跑出的
 * 全量轨迹以 JSON 打到 stdout。**判定全部留在 Python 一侧**(见
 * test_bo_parity.py)—— 容差、字段、分叉定位只写一份。两边各写一套"算不算
 * 分叉"的规则,就是又一处会自己分叉的地方,和 export_ir.py 拒绝把
 * merge_specs 交给前端是同一条纪律。
 *
 * 它抓的是 test_ir_parity.py 结构上看不见的东西:那个测试只比先验项在网格点
 * 上的取值,而分叉恰恰发生在取值全都一致**之后** —— 采集函数的 argmax 被
 * 浮点尾数决定;tie-break 饱和后 np.lexsort 取末位而 JS 的 > 取首位。两个真
 * bug 都是轨迹对账抓出来的,先验取值那一层一次也没红过。
 *
 *     node parity_dump.cjs > js.json      (cwd 无关,不要 chdir)
 */
const fs = require('fs');
const path = require('path');

// 路径一律相对脚本自身解析。前身那个临时脚本靠 process.chdir('D:/...') 硬编码
// 仓库位置,谁从别的目录(runner / IDE / CI)调它就会读不到文件;而 test_bo_parity
// 是从**临时目录**把它调起来的,正是为了让这条路径永远被验一次。
const DEMO = path.dirname(__dirname);
const at = f => path.join(DEMO, f);

// scenarios.js 用 const 声明场景对象,const 绑定不会逃出 eval 的作用域 ——
// 直接 eval 完再取 FORMATION 只会拿到 undefined。所以在同一次 eval 里追加一个
// 返回表达式,让它在绑定还活着的作用域内把要的东西交出来(同 test_align.py)。
// 去掉开头的 'use strict' 是这套惯用法的一部分:严格模式下 eval 里的声明不再
// 落到外层,连追加返回表达式这条路都堵死。
const src = f => fs.readFileSync(at(f), 'utf8').replace(/^'use strict';/, '');

// 读 priors_ir.js 而不是 cache/priors_ir.json:断网双击 index.html 时前端加载的
// 就是这个 .js 构建物。对账要对的是**真正会上台的那份数据**,不是它的兄弟文件。
const IR = eval(src('priors_ir.js').replace('window.PRIORS_IR', 'var PRIORS_IR')
  + '\n;PRIORS_IR');
const S = eval(src('scenarios.js') + src('engine.js')
  + '\n;({FORMATION:FORMATION,CASTING:CASTING,priorFromIR:priorFromIR,runBO:runBO})');

// 种子日程与轮数在 Python 那边**又写了一份**,这是故意的:不从 argv 灌进来。
// 灌进来就等于把两侧的日程绑成一根,而"两个引擎跑的其实不是同一批实验"这类
// 手滑正是对账要拦的东西 —— 现在改这里任一个数,测试当场红并报出第几批分叉。
// 与 engine.js 的 evalPrior / bo.py 的 eval_prior 同口径:步长 7919、基线 24 批、
// 带先验 20 批。
const SEED0 = 20260829, NSEED = 8, SEED_STEP = 7919;
const ITERS_BASE = 24, ITERS_INJ = 20;

// 落到 9 位小数纯粹是为了让 diff 打印出来能看,不是为了抹平差异:判据是 1e-6,
// 比这粗三个数量级 —— 真分叉圆完了还在。
const rd = v => Math.round(v * 1e9) / 1e9;

const out = {};
for (const scene of [S.FORMATION, S.CASTING]) {
  const bank = IR.scenes[scene.id];
  // 卡表由 JS 侧**独立**枚举一遍(scenarios.js 的 cards × IR 里真有的那张)。
  // Python 枚举的是 cards.py,两边键集合不一致时 test_bo_parity 会当场报
  // 缺失/多出 —— 那说明 priors_ir.js 是旧的构建物,没跟上 cards.py。
  const jobs = [['__base__', null]];
  for (const face of scene.cards) {
    const ir = bank && bank.cards[face.id];
    if (ir) jobs.push([face.id, S.priorFromIR(ir)]);
  }
  for (const [label, prior] of jobs) {
    const iters = prior ? ITERS_INJ : ITERS_BASE;
    const start = prior ? scene.injStart() : scene.baseStart();
    for (let k = 0; k < NSEED; k++) {
      const r = S.runBO(scene, prior, SEED0 + k * SEED_STEP, iters, start);
      out[`${scene.id}/${label}/${k}`] = {
        n: r.nBatches,
        stopped: r.stoppedBy,
        true_best: rd(r.trueBest),
        scrap: r.scrapped,
        xs: r.history.map(h => h.x.map(rd)),
        ys: r.history.map(h => rd(h.y)),
      };
    }
  }
}
process.stdout.write(JSON.stringify(out));
