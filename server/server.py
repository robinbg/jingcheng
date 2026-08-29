"""server.py — FastAPI 后端 + 静态托管。

约束(来自 BCO 教训):HTTP handler 里绝不等慢活。
  /api/run_bo    毫秒级,同步返回
  /api/translate 立即返回 job_id,LLM 在后台线程;前端轮询,超时自己降级
"""
from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import cards as card_lib
import translate as tr
from bo import run_bo, run_pair
from prior_dsl import compile_prior, shuffle_dims
from sandbox import get_scene, scene_meta, SCENES

WEB_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

app = FastAPI(title="精成 · 工序寻优", version="1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


class RunReq(BaseModel):
    scene: str = "formation"
    spec: Optional[Dict[str, Any]] = None
    card_id: Optional[str] = None
    seed: int = 20260829
    base_iters: int = 24
    inj_iters: int = 16
    ablation: bool = False


class TranslateReq(BaseModel):
    scene: str = "formation"
    utterance: str
    card_id: Optional[str] = None


@app.get("/api/health")
def health() -> Dict[str, Any]:
    return {
        "ok": True,
        "engine": "server",
        "scenes": list(SCENES.keys()),
        "llm_configured": bool(tr.LLM_KEY),
        "llm_model": tr.LLM_MODEL,
        "cached_specs": len(tr.load_cache()),
    }


@app.get("/api/scene/{scene_id}")
def scene_info(scene_id: str) -> Dict[str, Any]:
    try:
        s = get_scene(scene_id)
    except KeyError:
        raise HTTPException(404, f"未知场景 {scene_id}")
    return {"scene": scene_meta(s), "cards": card_lib.card_list(scene_id)}


@app.get("/api/surface/{scene_id}")
def surface(scene_id: str, n: int = 56) -> Dict[str, Any]:
    """热力图数据:在 plane 两维上采样,其余维取中点。可行/红线区标 null。"""
    try:
        s = get_scene(scene_id)
    except KeyError:
        raise HTTPException(404, f"未知场景 {scene_id}")
    n = max(16, min(n, 96))
    px, py = s.plane
    ps = s.params
    grid: List[List[Optional[float]]] = []
    for j in range(n):
        row: List[Optional[float]] = []
        vy = ps[py]["lo"] + (ps[py]["hi"] - ps[py]["lo"]) * j / (n - 1)
        for i in range(n):
            vx = ps[px]["lo"] + (ps[px]["hi"] - ps[px]["lo"]) * i / (n - 1)
            x = [(p["lo"] + p["hi"]) / 2 for p in ps]
            x[px], x[py] = vx, vy
            row.append(None if not s.feasible(x) else round(s.reward(x), 4))
        grid.append(row)
    vals = [v for row in grid for v in row if v is not None]
    return {
        "grid": grid, "n": n,
        "x_param": ps[px], "y_param": ps[py],
        "vmin": min(vals) if vals else 0, "vmax": max(vals) if vals else 1,
    }


def _resolve_spec(scene_id: str, req: RunReq) -> Optional[Dict[str, Any]]:
    if req.spec:
        return req.spec
    if req.card_id:
        card = card_lib.find_card(scene_id, req.card_id)
        if not card:
            raise HTTPException(404, f"未知卡片 {req.card_id}")
        return card["spec"]
    return None


@app.post("/api/run_bo")
def api_run_bo(req: RunReq) -> Dict[str, Any]:
    try:
        s = get_scene(req.scene)
    except KeyError:
        raise HTTPException(404, f"未知场景 {req.scene}")
    space = s.space()
    spec = _resolve_spec(req.scene, req)
    prior = compile_prior(spec, space, req.scene, s.theta["sf"]) if spec else None
    out = run_pair(s, prior, req.seed, req.base_iters, req.inj_iters)
    out["prior"] = prior.summary() if prior else None
    out["scene"] = scene_meta(s)

    # 消融对照:维度打乱的先验。证明"翻译"本身携带信息
    if req.ablation and spec:
        shuf = shuffle_dims(spec, space)
        sp = compile_prior(shuf, space, req.scene, s.theta["sf"])
        r = run_bo(s, sp, req.seed, req.inj_iters, s.inj_start)
        out["ablation"] = {
            "history": r.history, "n_batches": r.n_batches,
            "best_y": r.best_y, "label": "维度打乱",
        }
    return out


@app.post("/api/translate")
def api_translate(req: TranslateReq) -> Dict[str, Any]:
    """立即返回 job_id。预制卡直接命中,不等 LLM。"""
    try:
        s = get_scene(req.scene)
    except KeyError:
        raise HTTPException(404, f"未知场景 {req.scene}")

    if req.card_id:
        card = card_lib.find_card(req.scene, req.card_id)
        if card:
            spec = dict(card["spec"])
            spec.setdefault("engine", "card")
            return {"status": "done", "spec": spec,
                    "translation": _card_view(s, req.scene, spec)}

    job_id = tr.start_job(req.utterance, s.space(), req.scene, s.red_lines)
    return {"status": "pending", "job_id": job_id}


@app.get("/api/translate/{job_id}")
def api_translate_poll(job_id: str) -> Dict[str, Any]:
    job = tr.get_job(job_id)
    if not job:
        raise HTTPException(404, "job 不存在或已过期")
    if job["status"] != "done":
        return {"status": job["status"], "elapsed": round(job.get("elapsed", 0), 1)}
    s = get_scene(job["scene"])
    spec = job["spec"]
    return {"status": "done", "spec": spec,
            "translation": _card_view(s, job["scene"], spec)}


def _card_view(s: Any, scene_id: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """翻译卡:原话 → 受影响维度 → 先验调整的人话 → confidence。

    可审计准入的落地 —— 翻不回中文的 op 一律拒收,这里就是那面镜子。
    """
    prior = compile_prior(spec, s.space(), scene_id, s.theta["sf"])
    v = prior.summary()
    v["utterance"] = spec.get("utterance", "")
    v["rationale"] = spec.get("rationale", "")
    v["engine"] = spec.get("engine", "unknown")
    v["fallback_reason"] = spec.get("fallback_reason")
    v["llm_model"] = spec.get("llm_model")
    v["has_llm_raw"] = bool(spec.get("llm_raw"))
    return v


# 静态托管放最后,避免吃掉 /api 路由
if os.path.isdir(WEB_DIR):
    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(os.path.join(WEB_DIR, "index.html"))

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
