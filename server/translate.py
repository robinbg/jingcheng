"""translate.py — 自然语言经验 → PriorSpec 的翻译 Agent。

三层降级链(LLM 绝不进关键路径):
    1) LLM(Kimi-K3, 异步 job + 轮询, temperature=0)
    2) 关键词规则翻译器(纯本地,毫秒级)
    3) 预制卡缓存 / cache/priors.json

Agent 只写先验,不看沙盘响应面、不碰目标函数、不提议候选点。
否则整个 demo 循环论证 —— 这条纪律写进 prompt,也写进代码结构。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import cards as card_lib
from prior_dsl import ParamSpace, spec_hash

CACHE_PATH = os.path.join(os.path.dirname(__file__), "cache", "priors.json")
LLM_TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "45"))
LLM_MODEL = os.environ.get("LLM_MODEL", "kimi-k3")
LLM_BASE = os.environ.get("LLM_BASE_URL", "https://api.aiping.cn/v1")
LLM_KEY = os.environ.get("LLM_API_KEY", "")

# ---------------------------------------------------------------- prompt
SYSTEM_PROMPT = """你是工艺经验翻译器。把一句工程师口语翻译成贝叶斯优化的先验规格(PriorSpec JSON)。

【你的唯一职责】只写先验。你不知道真实响应面,不许猜最优参数值,不许直接提议试验点。
你的输出只影响"下一批往哪试"的倾向,不影响任何评分标准。

【可用接口 op】只能用下面这些,不许发明新 op,不许写数学表达式:
- bump          {param, at, width, strength}                某维在某值附近加分
- ramp          {param, direction:increase|decrease, strength}  单调倾向
- plateau       {param, direction, knee, strength}          到 knee 收益饱和
- region_score  {when, polarity:good|bad, strength}         任意区域加/扣分(兜底接口)
- joint_penalty {when, strength}                            联合区域扣分
- shift         {direction, strength}                       整体常数偏移
- narrow        {param, lo, hi}                             收缩搜索范围
- exclude       {modality:prohibition, red_line, when}      硬剪可行域(仅安全红线)
- lengthscale   {param, terrain:flat|smooth|normal|sensitive|sharp}  响应地形
- couple        {params:[a,b]}                              两维耦合,联动搜索
- noise         {when, strength}                            该区域"读数不可信"(不是最优点问题)
- pseudo_obs    {at:{param:value}, y, noise_inflate}         口述的一次历史观测
- cost          {param, direction, max_multiplier}          该方向更占资源(进 EI/cost)
- risk          {level:conservative|neutral|aggressive}      探索姿态
- recalibrate   {param, offset}                             仪表读数偏差校准
- park          {fragment, reason_code, reason}              接不住:显式停靠并说明原因

【when 的写法】{"维度名": {"below": 数} | {"above": 数} | {"between":[a,b]} | {"near": 数, "width": 数}}

【三条硬规矩】
1. strength 只能是 weak/moderate/strong/prohibitive 四个词。绝对不许写数字幅值——
   先验活在目标量纲里,你不知道那个量纲。
2. exclude 只在句子表达"禁止/必出废品/没商量"且命中已声明的安全红线时使用。
   拿不准就用 joint_penalty。硬剪除是唯一不可被数据推翻的接口。
3. 句子里有你接不住的部分(提到参数空间外的变量、需要看中间结果再定、纯感官描述),
   必须输出一条 park 说明,不许硬凑。宁可接一半,也不要假装全懂。

【confidence】0~1,由语气强度决定:"没商量/必/一定"→0.9;"一般/多半"→0.6;"好像/可能"→0.35。

只输出 JSON,不要解释、不要 markdown 代码块:
{"utterance": "原话", "ops": [...], "confidence": 0.x, "rationale": "机理一句话"}"""


def build_user_prompt(utterance: str, space: ParamSpace, scene_id: str,
                      red_lines: List[Dict[str, str]]) -> str:
    dims = "\n".join(
        f"  - {space.names[i]}({space.units[i] if i < len(space.units) else ''}): "
        f"{space.lo[i]:g} ~ {space.hi[i]:g}"
        for i in range(len(space))
    )
    rl = "\n".join(f"  - {r['label']}: {r['desc']}" for r in red_lines)
    few = json.dumps(
        card_lib.CARDS.get(scene_id, [{}])[1].get("spec", {}) if card_lib.CARDS.get(scene_id) else {},
        ensure_ascii=False,
    )
    return f"""场景:{scene_id}

参数空间(维度名白名单,只能用这些名字):
{dims}

已声明的安全红线(只有命中这些才允许 exclude):
{rl}

范例(同场景的一句话翻译结果):
{few}

现在翻译这句话:
{utterance}"""


# ---------------------------------------------------------------- 第二层:规则翻译器
DIM_WORDS: Dict[str, List[str]] = {
    "预充倍率": ["倍率", "电流", "首充", "预充", "充电快", "大电流", "小电流", "1c", "c率"],
    "化成温度": ["温度", "温", "热", "冷", "度"],
    "预充切换电压": ["电压", "截止电压", "切换", "平台"],
    "恒压截止电流": ["截止电流", "恒压", "尾电流"],
    "高温老化时长": ["静置", "老化", "时长", "小时", "存放", "陈化"],
    "熔料温度": ["熔料", "熔温", "汤温", "铝液"],
    "模具温度": ["模温", "模具"],
    "快速压射速度": ["快压", "压射速度", "充型", "速度"],
    "压射切换点": ["切换点", "低速转高速"],
    "保压时间": ["保压", "增压时间"],
}
FORBID_WORDS = ["别", "不要", "不能", "禁止", "必出", "没商量", "报废", "废掉", "不许"]
UP_WORDS = ["托", "顶", "抬", "拉高", "高一点", "大一点", "多一点", "长一点", "加大", "提到", "上"]
DOWN_WORDS = ["慢", "低", "小", "少", "降", "收", "压低", "别高", "慢一点", "小一点", "下"]
FLAT_WORDS = ["差不多", "都行", "一样", "没区别", "无所谓", "都可以"]
FAKE_WORDS = ["假的", "不准", "测不准", "虚的", "没意义", "看不出"]
COST_WORDS = ["占", "费", "贵", "慢", "耗", "等不起"]
COUPLE_WORDS = ["一起", "配着", "联动", "同时", "搭配", "组合"]
RISK_WORDS = ["宁可", "保险", "稳当", "别出废品", "求稳"]
NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _dims_in(text: str, space: ParamSpace) -> List[str]:
    t = text.lower()
    hits: List[str] = []
    for name in space.names:
        words = DIM_WORDS.get(name, []) + [name]
        if any(w.lower() in t for w in words):
            hits.append(name)
    return hits


def rule_translate(utterance: str, space: ParamSpace, scene_id: str) -> Dict[str, Any]:
    """关键词规则翻译器。LLM 挂了照常演,而且是确定性的。"""
    t = utterance.strip()
    dims = _dims_in(t, space)
    nums = [float(m) for m in NUM_RE.findall(t)]
    ops: List[Dict[str, Any]] = []

    def plausible(name: str, v: float) -> bool:
        i = space.index(name)
        return i is not None and space.lo[i] <= v <= space.hi[i]

    forbid = any(w in t for w in FORBID_WORDS)
    up = any(w in t for w in UP_WORDS)
    down = any(w in t for w in DOWN_WORDS)

    # 禁止 + 两个维度 → 联合扣分(规则器永不申请硬剪,授权只给 LLM/预制卡)
    if forbid and len(dims) >= 2:
        when: Dict[str, Any] = {}
        for name in dims[:2]:
            i = space.index(name)
            mid = (space.lo[i] + space.hi[i]) / 2
            cand = [v for v in nums if plausible(name, v)]
            thr = cand[0] if cand else mid
            when[name] = {"below": thr} if ("低" in t or "冷" in t) and name.endswith("温度") else {"above": thr}
        ops.append({"op": "joint_penalty", "when": when, "strength": "strong"})
        ops.append({"op": "couple", "params": dims[:2]})
    elif forbid and len(dims) == 1:
        name = dims[0]
        cand = [v for v in nums if plausible(name, v)]
        if cand:
            ops.append({"op": "narrow", "param": name, "hi": cand[0]} if not up
                       else {"op": "narrow", "param": name, "lo": cand[0]})
        else:
            ops.append({"op": "ramp", "param": name, "direction": "decrease", "strength": "strong"})

    for name in dims:
        if any(w in t for w in FAKE_WORDS):
            cand = [v for v in nums if plausible(name, v)]
            i = space.index(name)
            thr = cand[0] if cand else (space.lo[i] + space.hi[i]) / 2
            ops.append({"op": "noise", "when": {name: {"below": thr}}, "strength": "strong"})
            continue
        if any(w in t for w in FLAT_WORDS):
            ops.append({"op": "lengthscale", "param": name, "terrain": "flat"})
            continue
        cand = [v for v in nums if plausible(name, v)]
        if cand and not forbid:
            ops.append({"op": "bump", "param": name, "at": cand[0], "strength": "moderate"})
        elif down and not forbid:
            ops.append({"op": "ramp", "param": name, "direction": "decrease", "strength": "moderate"})
        elif up and not forbid:
            ops.append({"op": "ramp", "param": name, "direction": "increase", "strength": "moderate"})

    if any(w in t for w in COUPLE_WORDS) and len(dims) >= 2:
        ops.append({"op": "couple", "params": dims[:2]})
    if any(w in t for w in RISK_WORDS):
        ops.append({"op": "risk", "level": "conservative"})
    if not dims:
        ops.append({
            "op": "park",
            "fragment": t,
            "reason_code": "no_dim_match",
            "reason": "这句话没有对应到当前参数空间里的任何维度 —— 规则翻译器接不住",
        })

    return {
        "utterance": t,
        "ops": ops,
        "confidence": None,      # 交给 confidence_from_text 从语气推
        "rationale": "关键词规则翻译(LLM 未参与):按维度词 × 方向词 × 数值单位匹配",
        "engine": "rule",
    }


# ---------------------------------------------------------------- 第一层:LLM
def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.MULTILINE).strip()
    start = t.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(t)):
        if t[i] == "{":
            depth += 1
        elif t[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(t[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def llm_translate(utterance: str, space: ParamSpace, scene_id: str,
                  red_lines: List[Dict[str, str]]) -> Tuple[Optional[Dict[str, Any]], str]:
    """调 Kimi-K3。temperature=0 保证同句同结果。失败返回 (None, 原因)。"""
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
                {"role": "user", "content": build_user_prompt(utterance, space, scene_id, red_lines)},
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
        spec = _extract_json(raw)
        if not spec:
            return None, "LLM 输出无法解析为 JSON"
        spec["engine"] = "llm"
        spec["llm_raw"] = raw
        spec["llm_model"] = LLM_MODEL
        return spec, "ok"
    except Exception as e:                      # 网络/超时/配额/格式,一律降级
        return None, f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------- 缓存
_cache_lock = threading.Lock()


def load_cache() -> Dict[str, Any]:
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_cache(data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with _cache_lock:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 异步 job
_JOBS: Dict[str, Dict[str, Any]] = {}
_jobs_lock = threading.Lock()


def _set_job(job_id: str, **kw: Any) -> None:
    with _jobs_lock:
        _JOBS.setdefault(job_id, {}).update(kw)


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    with _jobs_lock:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def translate_sync(utterance: str, space: ParamSpace, scene_id: str,
                   red_lines: List[Dict[str, str]], use_llm: bool = True) -> Dict[str, Any]:
    """三层降级链的同步实现。给缓存生成脚本和 job worker 复用。"""
    key = spec_hash(utterance, scene_id)
    cache = load_cache()
    if key in cache:
        spec = dict(cache[key])
        spec["engine"] = spec.get("engine", "cache") + "+cache"
        return spec

    if use_llm:
        spec, reason = llm_translate(utterance, space, scene_id, red_lines)
        if spec:
            spec.setdefault("utterance", utterance)
            cache[key] = spec
            save_cache(cache)
            return spec
        fallback_reason = reason
    else:
        fallback_reason = "已按请求跳过 LLM"

    spec = rule_translate(utterance, space, scene_id)
    spec["fallback_reason"] = fallback_reason
    return spec


def start_job(utterance: str, space: ParamSpace, scene_id: str,
              red_lines: List[Dict[str, str]]) -> str:
    """立即返回 job_id;LLM 在后台线程跑。HTTP handler 里绝不等慢活。"""
    job_id = uuid.uuid4().hex[:12]
    _set_job(job_id, status="pending", started=time.time(), utterance=utterance, scene=scene_id)

    def worker() -> None:
        try:
            spec = translate_sync(utterance, space, scene_id, red_lines, use_llm=True)
            _set_job(job_id, status="done", spec=spec, elapsed=time.time() - _JOBS[job_id]["started"])
        except Exception as e:
            spec = rule_translate(utterance, space, scene_id)
            spec["fallback_reason"] = f"worker 异常:{type(e).__name__}"
            _set_job(job_id, status="done", spec=spec)

    threading.Thread(target=worker, daemon=True).start()
    return job_id
