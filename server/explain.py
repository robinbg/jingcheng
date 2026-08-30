"""explain.py — 答辩现场的"解释与追问"入口。

跟 translate.py 是同一套纪律,但职责完全不同:
    translate.py  说话 → 先验(写)
    explain.py    已经发生的数据 → 人话(读,只读)

三层降级链(LLM 绝不进关键路径,这条纪律在这个项目里已经吃过一次亏,
见 MEMORY.md 的 BCO 教训 —— Zeabur 网关 ~60s 就切,HTTP handler 里
绝不能同步等 LLM):
    1) LLM(温度=0,超时给得很短 —— 这是"追问"场景,评委等不起翻译 Agent
       那种 45s 的等级,15s 内不回来就必须有兜底)
    2) 内置 FAQ 缓存(关键词匹配,毫秒级,覆盖答辩最常被问到的几类问题)
    3) 固定兜底句"这个问题留给答辩环节"(绝不空屏、绝不报错、绝不编数)

最重要的一条硬规矩,写死在 prompt 里,不允许模型绕过:
只解释已经发生的数据,不编造任何数值;凡是上下文里没有的数,回答"这个数我没有"。
这是整个 demo 的诚信底线 —— 我们的立场是"必须真跑",一个会编数的解释器
比翻译器编先验更致命,因为它面对的是评委的追问,编一次就把之前所有的
"真实性"宣称全部推翻。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

LLM_TIMEOUT_S = float(os.environ.get("EXPLAIN_LLM_TIMEOUT_S", "15"))
LLM_MODEL = os.environ.get("LLM_MODEL", "kimi-k3")
LLM_BASE = os.environ.get("LLM_BASE_URL", "https://api.aiping.cn/v1")
LLM_KEY = os.environ.get("LLM_API_KEY", "")

FALLBACK_SENTENCE = "这个问题留给答辩环节。"

# ---------------------------------------------------------------- prompt
# 铁律必须逐字出现在 system prompt 里 —— 这不是"建议",是验收标准里明写的
# 那句话,改一个字都可能在评审时被当场读出来对质。
NO_FABRICATION_RULE = "只解释已经发生的数据,不编造任何数值;凡是上下文里没有的数,回答\"这个数我没有\"。"

SYSTEM_PROMPT = f"""你是"精成"工艺寻优演示的答辩解释助手,不是翻译 Agent —— 你不写先验、
不提议参数、不知道也不允许猜真实响应面。你唯一的职责是把已经发生的运行数据讲成人话。

【铁律】{NO_FABRICATION_RULE}
上下文之外的任何数字(哪怕看起来很合理),一律回答"这个数我没有",不许估算、不许"大概"。

【你能看到的】用户上下文里给你的:每批次的 x/y/mu/sigma/ei/feasible/risks 记录、
先验编译后的 notes(审计文本,记录了先验做了哪些调整、拒收了什么、停靠了什么)、
一句该场景的公开机理简介。除此之外你什么都看不到,也不能问沙盘要更多数据。

【表述红线,answer 里绝对不能出现的东西】
1. 不得提及任何具体企业名称或暗示数据来自某家企业的真实产线。
   所有数值只能称"上下文里的运行数据"或"公开文献典型范围 + 演示标定"。
2. 不得暗示方法来自某篇具体论文或某个具体出处,只能说"公开学术方法族,
   团队自行实现"。
3. 不得使用夸大、绝对化的营销语言("颠覆性""史无前例"之类),这是工程演示
   不是发布会。

【回答风格】3~5 句话,中文,面向答辩评委。先直接回答问题本身;如果问题问的
是上下文里没有的数字,第一句话就说"这个数我没有",然后可以补充上下文里
实际有的、最相关的数据作为替代说明。不写代码、不贴 JSON、不用 markdown。"""


def build_user_prompt(scene_id: str, question: str, context: Dict[str, Any]) -> str:
    """把结构化事实拼成 prompt —— 绝不把自由文本原样灌进去,喂的是字段。

    history 只取最近若干批,防止长跑之后一次追问把 prompt 撑爆;这里要的是
    "最近发生了什么"的问答,不是把整条历史都甩给模型自己去总结。
    """
    history = context.get("history") or []
    notes = context.get("notes") or []
    mechanism = context.get("mechanism") or SCENE_MECHANISM.get(scene_id, "")

    recent = history[-12:]
    lines = []
    for h in recent:
        risks = h.get("risks") or {}
        risk_txt = ",".join(f"{k}={v}" for k, v in risks.items()) if risks else "无"
        lines.append(
            f"  批次{h.get('i')}: x={h.get('x')} y={h.get('y')} mu={h.get('mu')} "
            f"sigma={h.get('sigma')} ei={h.get('ei')} feasible={h.get('feasible')} "
            f"risks={{{risk_txt}}} best_so_far={h.get('best_so_far')}"
        )
    hist_txt = "\n".join(lines) if lines else "  (上下文没有给批次历史)"
    notes_txt = "\n".join(f"  - {n}" for n in notes) if notes else "  (上下文没有给先验 notes)"

    return f"""场景:{scene_id}

公开机理简介(供你理解术语,不是待解释的数据):
{mechanism}

先验编译审计(notes,记录了这句经验被翻译成了什么调整、拒收/停靠了什么):
{notes_txt}

最近的批次运行数据(只有这些是"已经发生的数据",其余一律视为不存在):
{hist_txt}

评委的追问:
{question}"""


# 每个场景一句公开机理简介,供 prompt 里给模型"看得懂术语"用,
# 不含任何企业专有工艺细节 —— 跟 sandbox.py 里写给沙盘自己用的机理注释
# 是两份东西,这里只挑“公开可查”的那一小段,别把沙盘实现细节带出去。
SCENE_MECHANISM: Dict[str, str] = {
    "formation": "锂电化成阶段的公开电化学常识:预充电流密度影响 SEI 成核速率与致密度,"
                  "温度影响副反应速率,老化时长影响 SEI 稳定所需时间。析锂、产气是公开文献"
                  "里常见的失效模式,不是本演示独有的判据。",
    "casting": "压铸工艺的公开常识:熔料/模具温度影响流动性与凝固速度,压射速度与切换点"
               "影响充型与卷气,保压时间影响补缩。冷隔、气孔是压铸公开文献里常见的缺陷类型。",
}


# ---------------------------------------------------------------- 第二层:FAQ 缓存
# 答辩现场被问到的问题高度集中在"这是不是真数据""方法哪来的""AI 会不会编"
# 这几类上 —— 这些问题恰恰是最不该让 LLM 即时生成的,因为答案必须逐字符合
# 合规红线,关键词命中比等 LLM 更快也更稳。
FAQ: List[Dict[str, Any]] = [
    {
        "keywords": ["真实", "真的假的", "造假", "企业数据", "产线数据", "是不是真数据"],
        "answer": "这里跑的是上下文里给出的运行数据,不是某家企业的真实产线数据。"
                   "数值标定参考的是公开文献里的典型范围,叠加了演示用的批次噪声,"
                   "不代表任何企业的实际产能或良率。",
    },
    {
        "keywords": ["论文", "出处", "参考文献", "抄", "引用"],
        "answer": "这个数我没有——具体论文出处不在上下文里。方法上属于公开的贝叶斯优化"
                   "与高斯过程方法族,是团队自行实现的,不对应某一篇具体论文或某个机构。",
    },
    {
        "keywords": ["先验", "prior", "是什么", "干什么用"],
        "answer": "先验是把一句工艺经验翻译成的搜索倾向,它只影响贝叶斯优化"
                   "下一批往哪个方向多试,不改变真实响应面,也不直接决定最终评分。"
                   "上下文里的 notes 记录了这句话具体被翻译成了哪些调整。",
    },
    {
        "keywords": ["采集函数", "ei", "怎么选下一个点", "怎么推荐"],
        "answer": "下一批试验点是按采集函数(公开方法族里的期望提升准则)在可行域内选的,"
                   "兼顾预测均值、不确定性和是否触碰红线。具体每一批选了哪个点、"
                   "对应的 mu/sigma/ei 数值,都在上下文的批次记录里,不在里面的我就说没有。",
    },
    {
        "keywords": ["红线", "安全", "exclude", "硬剪"],
        "answer": "红线是提前声明的安全边界,只有先验里的硬剪接口能剪掉对应区域,"
                   "而且这是唯一一个不会被后续数据推翻的调整——其余所有先验调整"
                   "都要在跑的过程中接受数据检验。",
    },
    {
        "keywords": ["置信度", "confidence"],
        "answer": "置信度反映的是这句经验在语气上的强弱(比如“一定”和“好像”对应不同"
                   "置信度),它影响先验调整的力度,不代表这句话本身有多“对”——"
                   "对不对是要靠后面的数据去验证的。",
    },
    {
        "keywords": ["消融", "打乱", "对照", "维度打乱"],
        "answer": "消融对照是把同一句话翻译出来的先验,维度故意打乱后再跑一遍,"
                   "用来证明效果来自“翻译”这件事本身,而不是随便扰动一下先验"
                   "都能加速。这两条曲线的具体数值要看上下文有没有给,没给的话"
                   "这个数我没有。",
    },
    {
        "keywords": ["两个引擎", "js", "python", "一致", "对齐"],
        "answer": "沙盘和先验解释各有 Python 和 JS 两份实现,靠专门的一致性测试"
                   "逐位比对均值/噪声/成本/可行性,保证同一句话在前端和后端"
                   "给出同一个结论,不会出现“后台一个结果、界面另一个结果”。",
    },
    {
        "keywords": ["幻觉", "编造", "会不会瞎说", "编数"],
        "answer": "回答只会基于上下文里已经发生的数据,上下文没有的数字我会直接说"
                   "“这个数我没有”,不会去估算或编一个听起来合理的数字凑数。",
    },
    {
        "keywords": ["卡片", "经验卡", "怎么来的", "预制"],
        "answer": "预制经验卡是团队手写的 PriorSpec,作为翻译 Agent 失效时的第三层兜底,"
                   "也是提示词里的示例来源;开演前会用真实 LLM 调用一遍留存原始响应,"
                   "作为“模型确实翻译过”的证据。",
    },
    # 下面这几条补的是**这个演示自己的主张**。上面十条答的都是"方法哪来的、
    # 数据真不真",而台上被问得最多的那一句恰恰是"你凭什么说它省了批次" ——
    # 实测过:无 key 时问"为什么这句话能省批次?"落到了"留给答辩环节",
    # 也就是全场最该答上的一问偏偏答不上。兜底层的覆盖面要照着**会被问什么**
    # 排,不是照着我们想讲什么排。
    {
        "keywords": ["省批次", "能省", "少试", "为什么快", "加速", "省了几批", "怎么省"],
        "answer": "省批次不是先验直接给的,是搜索顺序变了带来的:那句话把下一批往"
                   "更可能好的那片区域引,于是同样的收敛判定提前满足。判定本身没变——"
                   "连续几批最优值不再提升就停,停在哪一批由采集函数收益和批次预算决定,"
                   "不是人拍板。左右两田同一个沙盘、同一颗种子、同一个优化器,"
                   "唯一的差别就是有没有那句话,所以差出来的批次只能记在它头上。",
    },
    {
        "keywords": ["值多少钱", "怎么算钱", "金额", "结算", "批次等价", "折钱", "省了多少钱"],
        "answer": "结算把一句话的作用拆成几条可分别度量的轴:少试的批次、少报废的量、"
                   "推荐点真值的提高、交付读数虚高的下降,再减去声明了却没被执行到的那部分。"
                   "每条轴都换算成“批次等价”,乘一次单批成本就是金额。轴的读数在结算页上写着,"
                   "各轴的权重属于答辩细节,画面上只报读数不报系数。",
    },
    {
        "keywords": ["种子", "为什么不是台上那一轮", "口径", "16", "一轮", "重复"],
        "answer": "台上放的是一颗固定种子的回放,好让同一句话每次演都是同一条曲线;"
                   "但账按多颗种子的均值算。原因是单轮抖动比判定阈值还大,拿一轮下判决"
                   "会把好经验误判成没用。演的是一轮,判的是分布——所以结算页上的数"
                   "和你数屏幕上那几批可能不一样,脚注里写了种子数。",
    },
    {
        "keywords": ["孪生", "沙盘", "仿真", "算得准", "标定", "凭什么准", "模型准不准"],
        "answer": "这个沙盘是公开定性机理规则加公开量级估算搭出来的合成数据生成器,"
                   "明确不做电化学仿真,当前版本没有实测数据参与。它不靠“信我准”:"
                   "每批的预测区间都是在看到读数之前记下的,跑完当场对一遍,"
                   "对得准不准写在可信度那一栏里;标定页上每个数都能追到骨架表里的某个函数。",
    },
    {
        # 关键词是**子串**匹配,所以词序不同就是两个词:"听不懂"匹配不上
        # "没听懂",而后者才是人真会说的那句(实测这一条就是这么漏的)。
        # 同一个意思的几种自然说法都要列上,别指望评委照我们的词序问。
        "keywords": ["停靠", "接不住", "听不懂", "没听懂", "看不懂", "不懂",
                     "没翻译", "翻不出", "漏掉", "没理解"],
        "answer": "接不住的部分会显式停靠,原样留在界面上,不硬凑一个解读。"
                   "常见的三种情况是:说的东西超出了当前参数空间、需要中间结果才能判定、"
                   "或者纯感官描述。停靠不是失败,是不替一句话补它没说过的内容——"
                   "翻译层宁可少接一句,也不给出一个看起来完整其实是编的先验。",
    },
]


def faq_lookup(question: str) -> Optional[str]:
    t = question.strip().lower()
    if not t:
        return None
    best: Optional[str] = None
    best_hits = 0
    for item in FAQ:
        hits = sum(1 for kw in item["keywords"] if kw.lower() in t)
        if hits > best_hits:
            best_hits = hits
            best = item["answer"]
    return best if best_hits > 0 else None


# ---------------------------------------------------------------- 第一层:LLM
def _extract_answer(text: str) -> Optional[str]:
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:\w+)?|```$", "", t, flags=re.MULTILINE).strip()
    return t or None


def llm_explain(scene_id: str, question: str, context: Dict[str, Any]) -> Tuple[Optional[str], str]:
    """调 Kimi-K3。温度=0,超时很短——这是追问场景,等不起翻译 Agent 那种时长。

    失败(网络/超时/配额/无 key/格式)一律返回 (None, 原因),绝不抛出去,
    调用方按"LLM 没接住"处理,落到 FAQ 层。
    """
    if not LLM_KEY:
        return None, "未配置 LLM_API_KEY"
    try:
        import urllib.error
        import urllib.request

        body = json.dumps({
            "model": LLM_MODEL,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": build_user_prompt(scene_id, question, context)},
            ],
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{LLM_BASE.rstrip('/')}/chat/completions",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {LLM_KEY}"},
        )
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        raw = payload["choices"][0]["message"]["content"]
        answer = _extract_answer(raw)
        if not answer:
            return None, "LLM 输出为空"
        return answer, "ok"
    except Exception as e:                      # 网络/超时/配额/格式,一律降级
        return None, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- 异步 job
# 独立于 translate.py 的 _JOBS——两个模块职责不同(写先验 vs 读数据解释),
# 没必要共用一份状态,TTL 从写这个文件的第一天就带上,不用像 translate.py
# 那样后补。
_JOBS: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()
JOB_TTL_S = 600.0


def _sweep_jobs() -> None:
    now = time.time()
    with _jobs_lock:
        dead = [jid for jid, j in _JOBS.items()
                if j.get("status") == "done" and now - j.get("finished", now) > JOB_TTL_S]
        for jid in dead:
            del _JOBS[jid]


def _set_job(job_id: str, **kw: Any) -> None:
    with _jobs_lock:
        _JOBS.setdefault(job_id, {}).update(kw)


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _jobs_lock:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def explain_sync(scene_id: str, question: str, context: Dict[str, Any],
                 use_llm: bool = True) -> Dict[str, Any]:
    """三层降级链的同步实现。给 job worker 和测试直接复用。

    永远返回 {"answer": str, "source": "llm"|"faq"|"fallback", ...},
    永远是可以直接展示给评委的中文句子——这个函数内部绝不抛异常出去。
    """
    if use_llm:
        answer, reason = llm_explain(scene_id, question, context)
        if answer:
            return {"answer": answer, "source": "llm"}
        llm_reason = reason
    else:
        llm_reason = "已按请求跳过 LLM"

    faq_answer = faq_lookup(question)
    if faq_answer:
        return {"answer": faq_answer, "source": "faq", "llm_reason": llm_reason}

    return {"answer": FALLBACK_SENTENCE, "source": "fallback", "llm_reason": llm_reason}


def start_job(scene_id: str, question: str, context: Dict[str, Any]) -> str:
    """立即返回 job_id;LLM 在后台线程跑。HTTP handler 里绝不等慢活。"""
    _sweep_jobs()
    job_id = uuid.uuid4().hex[:12]
    _set_job(job_id, status="pending", started=time.time(), question=question, scene=scene_id)

    def worker() -> None:
        try:
            result = explain_sync(scene_id, question, context, use_llm=True)
            _set_job(job_id, status="done", result=result,
                     elapsed=time.time() - _JOBS[job_id]["started"], finished=time.time())
        except Exception as e:                  # 兜底的兜底——worker 本身不该炸,但万一炸了不能裸传上去
            _set_job(job_id, status="done",
                     result={"answer": FALLBACK_SENTENCE, "source": "fallback",
                             "llm_reason": f"worker 异常:{type(e).__name__}"},
                     finished=time.time())

    threading.Thread(target=worker, daemon=True).start()
    return job_id
