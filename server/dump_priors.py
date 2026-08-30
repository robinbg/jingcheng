"""dump_priors.py — 用真实 LLM 把预制经验卡跑一遍，存原始响应进
server/cache/priors.json，答辩时可以出示"这是模型真翻的，不是手写的"。

背景:cards.py 里的 spec 是团队手写的第三层兜底（也是 prompt 的 few-shot
范例来源），但答辩现场如果被问"你们的翻译 Agent 到底跑起来是什么样"，
手写 spec 没法当证据。这个脚本就是补那份证据的——只在有真实 LLM_API_KEY
时才跑，不许拿手写结果伪造成"LLM 输出"塞进缓存，那样比空着更糟。

用法（拿到 key 之后，在 server/ 目录下执行一次即可）:
    set LLM_API_KEY=你的key   (Windows cmd)
    $env:LLM_API_KEY="你的key"   (PowerShell)
    export LLM_API_KEY=你的key   (bash)
    python dump_priors.py

可选环境变量（跟 translate.py 共用同一套）:
    LLM_MODEL      默认 kimi-k3
    LLM_BASE_URL   默认 https://api.aiping.cn/v1
    LLM_TIMEOUT_S  默认 45

跑完会把每张卡的真实 LLM 输出（含 llm_raw 原始文本）写进
server/cache/priors.json，键是 spec_hash(utterance, scene_id)，跟
translate.py 的 load_cache/save_cache 是同一份文件、同一套键结构——
这样 /api/translate 命中缓存时用的就是这份真实响应，不需要额外接线。

没有 key 时直接退出并打印原因，不生成任何内容——参见文件头的那条纪律。
"""
from __future__ import annotations

import sys

import cards as card_lib
import translate as tr
from prior_dsl import ParamSpace, spec_hash
from sandbox import SCENES


def main() -> int:
    if not tr.LLM_KEY:
        print("[skip] 未设置 LLM_API_KEY，不生成任何缓存内容。")
        print("       拿到 key 后按本文件头部的用法说明设置环境变量再跑一次。")
        return 0

    cache = tr.load_cache()
    ok, fail = 0, 0
    for scene_id, cards in card_lib.CARDS.items():
        scene = SCENES[scene_id]
        space = ParamSpace.from_params(scene.params)
        for card in cards:
            utterance = card["spec"]["utterance"]
            key = spec_hash(utterance, scene_id)
            if key in cache:
                print(f"[skip] {scene_id}/{card['id']} 已在缓存里")
                continue
            spec, reason = tr.llm_translate(utterance, space, scene_id, scene.red_lines)
            if not spec:
                print(f"[fail] {scene_id}/{card['id']}: {reason}")
                fail += 1
                continue
            spec.setdefault("utterance", utterance)
            cache[key] = spec
            tr.save_cache(cache)
            print(f"[ok]   {scene_id}/{card['id']} → 已写入真实 LLM 响应")
            ok += 1

    print(f"\n共成功 {ok} 张，失败 {fail} 张。缓存文件:{tr.CACHE_PATH}")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
