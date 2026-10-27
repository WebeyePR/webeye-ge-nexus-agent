# -*- coding: utf-8 -*-
"""生产引擎长时间采样测试。

用途：正式部署后跑一轮长时采样，量化真实延迟分布与稳定性，并区分
「冷启动（首次请求）」与「稳态」两段，避免把冷启动算成常态延迟。

用法：
    .venv/bin/python tests/remote/long_run_sample.py                    # 默认 40 轮
    .venv/bin/python tests/remote/long_run_sample.py --rounds 100       # 自定义轮次
    .venv/bin/python tests/remote/long_run_sample.py --idle 120         # 先空闲 120s 再测（触发冷启动）
    ENGINE_NAME=projects/.../reasoningEngines/<id> .venv/bin/python tests/remote/long_run_sample.py

输出：逐轮明细 + 分段统计（冷启动段/稳态段）+ 分位数（P50/P90/P95/P99）。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
import warnings

warnings.filterwarnings("ignore")

import google.auth.transport.requests  # noqa: E402
import requests  # noqa: E402

DEFAULT_ENGINE = (
    "projects/839062387451/locations/us-central1/"
    "reasoningEngines/8675870496681623552"
)
ENGINE = os.getenv("ENGINE_NAME", DEFAULT_ENGINE)
BASE = f"https://us-central1-aiplatform.googleapis.com/v1/{ENGINE}"

PROMPT = "用一个词回答：好"          # 最短路径，隔离工具耗时
TOOL_PROMPT = "查我的飞书授权状态"    # 工具链路径


def auth_headers() -> dict[str, str]:
    creds, _ = google.auth.default()
    creds.refresh(google.auth.transport.requests.Request())
    return {"Authorization": f"Bearer {creds.token}", "Content-Type": "application/json"}


HDRS = auth_headers()


def new_session(user: str) -> str:
    r = requests.post(f"{BASE}:query", headers=HDRS, timeout=180,
                      json={"class_method": "create_session", "input": {"user_id": user}})
    r.raise_for_status()
    return r.json()["output"]["id"]


def turn(sid: str, user: str, msg: str) -> tuple[float | None, float, int]:
    """返回 (首事件秒数, 总秒数, partial 数)。"""
    payload = json.dumps({"userId": user, "sessionId": sid,
                          "message": {"role": "user", "parts": [{"text": msg}]},
                          "streaming": True})
    t0 = time.time()
    first: float | None = None
    partial = 0
    resp = requests.post(f"{BASE}:streamQuery", headers=HDRS, timeout=900, stream=True,
                         json={"class_method": "streaming_agent_run_with_events",
                               "input": {"request_json": payload}})
    resp.raise_for_status()
    for line in resp.iter_lines(decode_unicode=True):
        if not line:
            continue
        if first is None:
            first = time.time() - t0
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("code") or obj.get("errorCode"):
            print(f"    ★错误事件: {str(obj.get('message'))[:100]}")
            break
        for ev in (obj.get("events") or []):
            if ev.get("partial"):
                partial += 1
    return first, time.time() - t0, partial


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))
    return ordered[idx]


def summarize(label: str, values: list[float]) -> None:
    if not values:
        print(f"  {label}: 无数据")
        return
    print(f"  {label}: n={len(values)}  "
          f"中位={statistics.median(values):.2f}s  "
          f"最小={min(values):.2f}s  最大={max(values):.2f}s  "
          f"P90={pct(values, 90):.2f}s  P95={pct(values, 95):.2f}s  P99={pct(values, 99):.2f}s")


def main() -> int:
    ap = argparse.ArgumentParser(description="生产引擎长时采样")
    ap.add_argument("--rounds", type=int, default=40)
    ap.add_argument("--idle", type=int, default=0, help="开始前先空闲 N 秒以触发冷启动")
    ap.add_argument("--tool-every", type=int, default=10,
                    help="每 N 轮插一次工具链用例（0=不插）")
    args = ap.parse_args()

    print(f"引擎: {ENGINE.split('/')[-1]}")
    print(f"轮次: {args.rounds}  空闲预热: {args.idle}s  工具链间隔: {args.tool_every}\n")

    if args.idle:
        print(f"空闲 {args.idle}s 中（模拟真实闲置）...", flush=True)
        time.sleep(args.idle)
        print("空闲结束，开始采样\n", flush=True)

    user = "longrun-u"
    sid = new_session(user)

    firsts: list[float] = []
    totals: list[float] = []
    tool_firsts: list[float] = []
    cold_start: float | None = None

    t_begin = time.time()
    for i in range(1, args.rounds + 1):
        try:
            f, total, parts = turn(sid, user, PROMPT)
        except Exception as exc:  # noqa: BLE001 — 单轮异常不中断采样
            print(f"  {i:3d}: ★异常 {type(exc).__name__}: {str(exc)[:80]}")
            continue
        if f is None:
            print(f"  {i:3d}: ★无事件")
            continue
        if i == 1:
            cold_start = f
        firsts.append(f)
        totals.append(total)
        flag = " ★慢" if f > 15 else ""
        print(f"  {i:3d}: 首事件={f:6.2f}s 总={total:6.2f}s partial={parts:2d}{flag}", flush=True)

        if args.tool_every and i % args.tool_every == 0:
            try:
                tf, tt, tp = turn(sid, user, TOOL_PROMPT)
                if tf is not None:
                    tool_firsts.append(tf)
                    print(f"       └ 工具链: 首事件={tf:6.2f}s 总={tt:6.2f}s partial={tp}")
            except Exception as exc:  # noqa: BLE001
                print(f"       └ 工具链异常: {str(exc)[:60]}")

    elapsed = time.time() - t_begin
    print(f"\n{'=' * 62}")
    print(f"采样完成：{len(firsts)} 轮成功 / {args.rounds} 轮请求，用时 {elapsed / 60:.1f} 分钟")
    print("=" * 62)
    summarize("全部轮次", firsts)
    if cold_start is not None and len(firsts) > 1:
        summarize("冷启动(首轮)", [cold_start])
        summarize("稳态(第2轮起)", firsts[1:])
    if tool_firsts:
        summarize("工具链用例", tool_firsts)

    slow = [x for x in firsts if x > 15]
    print(f"\n慢轮次(>15s): {len(slow)} 次" + (f" → {[f'{x:.0f}s' for x in slow]}" if slow else "（无）"))
    if firsts:
        ratio = len(slow) / len(firsts) * 100
        print(f"慢轮占比: {ratio:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
