"""cards.py — 预制经验卡的 PriorSpec。

这些 spec 就是 LLM 的输出目标(开演前跑一次真 LLM 存入 cache/priors.json,
带原始响应,答辩时可出示"这是模型真翻的")。这里的手写版本是第三层兜底,
也是 prompt 的 few-shot 范例来源。
"""
from __future__ import annotations

from typing import Any, Dict, List

FORMATION_CARDS: List[Dict[str, Any]] = [
    {
        "id": "sei",
        "kind": "good",
        "text": "首充慢一点，SEI长得匀",
        "why": "低倍率成膜致密 → 内阻首效双优",
        "spec": {
            "utterance": "首充慢一点，SEI长得匀",
            "ops": [
                {"op": "narrow", "param": "预充倍率", "hi": 0.12},
                {"op": "ramp", "param": "预充倍率", "direction": "decrease", "strength": "moderate"},
                {"op": "bump", "param": "预充倍率", "at": 0.05, "width": 0.04, "strength": "moderate"},
            ],
            "confidence": 0.8,
            "rationale": "低电流密度下 SEI 成核速率低、膜层致密均匀，不可逆锂损失少 → FCE 与 DCR 同时改善",
        },
    },
    {
        "id": "cold",
        "kind": "good",
        "text": "低温别上倍率，析锂没商量",
        "why": "低温锂析出动力学差 → 死锂",
        # 这句话提到的红线。验收时废品按成因分账,只算它提过的那条 ——
        # 它没提产气,产气废品的涨落不该记在它头上。
        "speaks_to": ["li"],
        "spec": {
            "utterance": "低温别上倍率，析锂没商量",
            "ops": [
                {
                    "op": "exclude",
                    "modality": "prohibition",
                    "red_line": "析锂",
                    "when": {"化成温度": {"below": 30}, "预充倍率": {"above": 0.20}},
                },
                {
                    "op": "couple",
                    "params": ["化成温度", "预充倍率"],
                },
            ],
            "confidence": 0.9,
            "rationale": "低温下锂离子嵌入石墨的固相扩散受阻，负极极化超过锂沉积电位 → 金属锂析出、死锂不可逆",
        },
    },
    {
        "id": "temp",
        "kind": "good",
        "text": "温度托到四十，膜快又不烧",
        "why": "中高温加速 SEI 形成，过高则副反应",
        "spec": {
            "utterance": "温度托到四十，膜快又不烧",
            "ops": [
                {"op": "bump", "param": "化成温度", "at": 40, "width": 6, "strength": "moderate"},
                {"op": "lengthscale", "param": "化成温度", "terrain": "smooth"},
            ],
            "confidence": 0.75,
            "rationale": "升温提高 EC 还原动力学、缩短成膜时间；过高则电解液分解与产气副反应主导 → 倒U响应，峰在 40℃ 附近",
        },
    },
    {
        "id": "age",
        "kind": "good",
        "text": "静置不到位，内阻是假的",
        "why": "浸润/稳定不足 → 测量失真",
        "spec": {
            "utterance": "静置不到位，内阻是假的",
            "ops": [
                {"op": "noise", "when": {"高温老化时长": {"below": 24}}, "strength": "strong"},
            ],
            "confidence": 0.85,
            "rationale": "老化不足时 SEI 未稳定、电解液浸润未平衡，DCR 读数偏离稳态 → 这不是最优点问题，是观测可信度问题。所以它只动观测噪声这一个接口：既不收窄搜索域，也不说老化越长越好（那两句话它都没说）。老化时长还要和产气红线一起权衡，替它补一句「越长越好」反而会把批次推向高温长时的产气墙",
        },
    },
    {
        "id": "wrong",
        "kind": "wrong",
        "text": "化成就是走过场，1C 拉满省电费",
        "why": "（歪经）红线区照跑，多试若干批",
        "spec": {
            "utterance": "化成就是走过场，1C 拉满省电费",
            "ops": [
                {"op": "narrow", "param": "预充倍率", "lo": 0.40},
                {"op": "ramp", "param": "预充倍率", "direction": "increase", "strength": "strong"},
                {"op": "cost", "param": "高温老化时长", "direction": "increase", "max_multiplier": 2.0},
            ],
            "confidence": 0.7,
            "rationale": "（这句是歪经。系统照样把它当先验称量，不当真理执行；数据会在几批之内把它推翻。）",
        },
    },
    {
        "id": "cast",
        "kind": "good",
        "external": True,
        "text": "快压过 2.5 必卷气",
        "why": "（压铸底仓的老话，跨场景迁移）",
        "spec": {
            "utterance": "快压过 2.5 必卷气",
            "ops": [
                {"op": "park", "fragment": "快压过 2.5 必卷气",
                 "reason_code": "out_of_space",
                 "reason": "「快速压射速度」不在化成参数空间内 —— 跨工序口诀无法直接落地"},
                {"op": "narrow", "param": "化成温度", "hi": 46},
            ],
            "confidence": 0.4,
            "rationale": "压铸口诀迁移到化成：速度维度接不住（显式停靠），仅保留“上限要留余量”的弱类比",
        },
    },
]

CASTING_CARDS: List[Dict[str, Any]] = [
    {
        "id": "p1",
        "kind": "good",
        "text": "快压过 2.5 必卷气",
        "why": "充型速度上限 → 卷气",
        # 这句话提到的红线。验收时废品按成因分账,只算它提过的那条 ——
        # 它没提冷隔,冷隔废品的涨落不该记在它头上。
        "speaks_to": ["gas"],
        "spec": {
            "utterance": "快压过 2.5 必卷气",
            "ops": [
                {"op": "exclude", "modality": "prohibition", "red_line": "卷气",
                 "when": {"快速压射速度": {"above": 2.5}}},
                {"op": "bump", "param": "快速压射速度", "at": 2.1, "width": 0.3, "strength": "moderate"},
            ],
            "confidence": 0.9,
            "rationale": "充型速度超过临界值后金属液由层流转湍流，卷入型腔气体 → 气孔缺陷",
        },
    },
    {
        "id": "p2",
        "kind": "good",
        "text": "模温托住，冷隔不来",
        "why": "模温下限 → 熔体流动性",
        # 冷隔在后端 _risks 里占 li 槽(与前端 redLines 的 id 一致)
        "speaks_to": ["li"],
        "spec": {
            "utterance": "模温托住，冷隔不来",
            "ops": [
                {"op": "narrow", "param": "模具温度", "lo": 200},
                {"op": "bump", "param": "模具温度", "at": 215, "width": 20, "strength": "moderate"},
            ],
            "confidence": 0.8,
            "rationale": "模温过低使熔体前沿提前凝固，两股流汇合处未熔合 → 冷隔",
        },
    },
    {
        "id": "p3",
        "kind": "good",
        # 校准卡是"乘数"不是"加数":单独打出去数学上恒等于没打(平稳核下
        # 给所有输入加同一个常数,预测不变)。它的价值只在和"说绝对数值"的
        # 那类话一起打时才出现 —— 把别人的坐标搬正。所以按组合验收。
        "pair_with": "p2",
        "text": "我们那台机模温表偏高三度",
        "why": "（设备个体差异 → 坐标校准）",
        "spec": {
            "utterance": "我们那台机模温表偏高三度",
            "ops": [
                {"op": "recalibrate", "param": "模具温度", "offset": -3},
            ],
            "confidence": 0.85,
            "rationale": "仪表系统偏差属于坐标校准，不改响应面形状，只改读数与真值的映射。它一个字也没说模温该往哪走，所以不收窄搜索域 —— 那是「模温托住」那句话的活",
        },
    },
]

CARDS: Dict[str, List[Dict[str, Any]]] = {
    "formation": FORMATION_CARDS,
    "casting": CASTING_CARDS,
}


def card_list(scene_id: str) -> List[Dict[str, Any]]:
    """给前端的卡面(不含 spec 细节,spec 走 /api/translate)。"""
    return [
        {k: v for k, v in c.items() if k != "spec"}
        for c in CARDS.get(scene_id, [])
    ]


def find_card(scene_id: str, card_id: str) -> Dict[str, Any] | None:
    for c in CARDS.get(scene_id, []):
        if c["id"] == card_id:
            return c
    return None
