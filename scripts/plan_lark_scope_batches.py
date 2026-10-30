#!/usr/bin/env python3
"""一键生成/校验飞书分批授权清单（docs/lark_scope_batches.json）。

背景：分批清单原先是一次性产物 —— 只有结果文件，没有生成规则，应用新增权限时
无法重排。本脚本把「分组规则」固化下来，使清单可复现、可校验、可增量维护。

分组规则（按使用场景聚类，不是按命令量贪心）：
  B1 文档 / 知识库 / 云空间      docs, docx, drive, wiki, space (+ offline_access)
  B2 多维表格 / 电子表格 / 画板   base, sheets, board
  B3 消息 / 日程 / 会议 / 通讯录  im, calendar, minutes, vc, slides, contact
  B4 邮件 / 任务 / OKR / 审批 / 其他  mail, task, okr, approval, search, aily,
                                   attendance, mindnotes, event, apps
  —— 未在上表出现的域会自动并入最后一批，并明确打印（绝不静默丢弃）。

用法：
  python3 scripts/plan_lark_scope_batches.py --check        # 默认：拉飞书 app.scopes 比对现有清单
  python3 scripts/plan_lark_scope_batches.py --write        # 重算并写回（先备份）
  python3 scripts/plan_lark_scope_batches.py --from-cache f --check   # 离线：用已保存的 app.scopes JSON

退出码：0 = 一致 / 已写入；2 = 有差异、超限或出错。

安全设计：
- 默认只读（--check）；--write 才落盘，且先写 .bak
- 单批超限（> limit）直接报错不落盘：静默拆批会改变 batch 名，进而与线上
  authorization 资源（ge-nexus-feishu-N）错位
- 只取 token_types 含 "user" 的权限：tenant-only 权限用户态永远拿不到
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

DEFAULT_LIMIT = 45
OFFLINE_SCOPE = "offline_access"
AUTHORIZE_ENDPOINT = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
DEFAULT_REDIRECT = "https://vertexaisearch.cloud.google.com/oauth-redirect"

# (批次名后缀, 场景标签, 域列表)。顺序即批次顺序，改顺序会改变资源名，慎改。
DOMAIN_GROUPS = [
    ("feishu-1", "文档 / 知识库 / 云空间", ["docs", "docx", "drive", "wiki", "space"]),
    ("feishu-2", "多维表格 / 电子表格 / 画板", ["base", "sheets", "board"]),
    ("feishu-3", "消息 / 日程 / 会议 / 通讯录", ["im", "calendar", "minutes", "vc", "slides", "contact"]),
    ("feishu-4", "邮件 / 任务 / OKR / 审批 / 其他",
     ["mail", "task", "okr", "approval", "search", "aily", "attendance", "mindnotes", "event", "apps"]),
]


def parse_env_file(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def user_scopes_from(app_scopes: list[dict]) -> list[str]:
    """取出可用户授权的 scope（token_types 含 user），保持 API 顺序并去重。"""
    picked: list[str] = []
    for item in app_scopes:
        name = item.get("scope")
        if not name:
            continue
        if "user" not in (item.get("token_types") or []):
            continue
        if name not in picked:
            picked.append(name)
    return picked


def build_batches(
    user_scopes: list[str],
    limit: int = DEFAULT_LIMIT,
    groups: list[tuple[str, str, list[str]]] | None = None,
) -> tuple[list[dict], list[str]]:
    """按域聚合成批。返回 (batches, leftovers)。

    超限直接抛 ValueError —— 静默拆批会产生新的 batch 名，与线上资源错位。
    """
    groups = groups or DOMAIN_GROUPS
    by_domain: dict[str, list[str]] = {}
    for scope in user_scopes:
        by_domain.setdefault(scope.split(":")[0], []).append(scope)

    used: set[str] = set()
    batches: list[dict] = []

    for index, (suffix, label, domains) in enumerate(groups):
        picked: list[str] = []
        if index == 0 and OFFLINE_SCOPE in user_scopes:
            picked.append(OFFLINE_SCOPE)
            used.add(OFFLINE_SCOPE)
        for domain in domains:
            for scope in by_domain.get(domain, []):
                if scope not in picked:
                    picked.append(scope)
                    used.add(scope)
        if len(picked) > limit:
            raise ValueError(
                f"批次 {suffix}（{label}）超出上限：{len(picked)} > {limit}。"
                f"请调整 DOMAIN_GROUPS 的分组，脚本不会自行拆批（拆批会改名并与线上资源错位）。"
            )
        batches.append({"name": f"ge-nexus-{suffix}", "label": label,
                        "count": len(picked), "scopes": picked})

    leftovers = [s for s in user_scopes if s not in used]
    if leftovers and batches:
        last = batches[-1]
        for scope in leftovers:
            if scope not in last["scopes"]:
                last["scopes"].append(scope)
        last["count"] = len(last["scopes"])
        if last["count"] > limit:
            raise ValueError(f"兜底后的最后一批超出上限：{last['count']} > {limit}（leftovers={leftovers}）")

    return batches, leftovers


def signature(batches: list[dict]) -> dict[str, frozenset]:
    """批名 → scope 集合（只比集合，不比顺序：API 返回顺序可能变化）。"""
    return {b["name"]: frozenset(b["scopes"]) for b in batches}


def diff_plans(existing: list[dict], generated: list[dict]) -> list[str]:
    problems: list[str] = []
    old, new = signature(existing), signature(generated)
    for name in new:
        if name not in old:
            problems.append(f"{name}: 新增批次")
        elif old[name] != new[name]:
            added = sorted(new[name] - old[name])
            removed = sorted(old[name] - new[name])
            problems.append(f"{name}: 差异 +{added} -{removed}")
    for name in old:
        if name not in new:
            problems.append(f"{name}: 生成结果中缺失")
    return problems


def fetch_app_scopes(env: dict[str, str]) -> list[dict]:
    app_id, secret = env.get("LARK_CLIENT_ID"), env.get("LARK_CLIENT_SECRET")
    if not app_id or not secret:
        raise SystemExit("缺少 LARK_CLIENT_ID / LARK_CLIENT_SECRET，无法拉取 app.scopes")

    payload = json.dumps({"app_id": app_id, "app_secret": secret}).encode()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=payload, headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        token_data = json.loads(resp.read())
    if token_data.get("code") != 0:
        raise SystemExit(f"获取 tenant_access_token 失败: {token_data}")
    token = token_data["tenant_access_token"]

    url = f"https://open.feishu.cn/open-apis/application/v6/applications/{app_id}?lang=zh_cn"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    if data.get("code") != 0:
        raise SystemExit(f"查询应用信息失败: {data}")
    return ((data.get("data") or {}).get("app") or {}).get("scopes") or []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--plan", default="docs/lark_scope_batches.json")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--from-cache", help="离线模式：读取已保存的 app.scopes JSON")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只比对（默认）")
    mode.add_argument("--write", action="store_true", help="重算并写回清单")
    args = ap.parse_args()

    if args.from_cache:
        with open(args.from_cache, encoding="utf-8") as f:
            raw = json.load(f)
        app_scopes = raw.get("scopes") if isinstance(raw, dict) else raw
    else:
        app_scopes = fetch_app_scopes(parse_env_file(args.env_file))

    user_scopes = user_scopes_from(app_scopes)
    try:
        generated, leftovers = build_batches(user_scopes, args.limit)
    except ValueError as exc:
        print(f"★ {exc}", file=sys.stderr)
        return 2

    print(f"飞书 app.scopes 可用户授权: {len(user_scopes)} 项 → {len(generated)} 批 "
          f"{[b['count'] for b in generated]}")
    for batch in generated:
        print(f"  {batch['name']:20s} {batch['count']:3d} 项  {batch['label']}")
    if leftovers:
        print(f"  ⚠ 未归类的域已并入最后一批: {leftovers}")

    if not os.path.exists(args.plan):
        if not args.write:
            print(f"⚠ 目标清单不存在: {args.plan}（用 --write 生成）", file=sys.stderr)
            return 2
        existing = None
    else:
        with open(args.plan, encoding="utf-8") as f:
            existing = json.load(f)

    if existing is not None:
        problems = diff_plans(existing.get("batches") or [], generated)
        if problems:
            print("\n与现有清单存在差异:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            if not args.write:
                print("\n结论: ★ 有差异（--check 只读）；确认后用 --write 更新", file=sys.stderr)
                return 2
        else:
            print("\n结论: ✓ 生成结果与现有清单完全一致（按批名比较 scope 集合）")

    if not args.write:
        return 0

    payload = {"limit": args.limit, "total": len(user_scopes), "batches": generated}
    backup = args.plan + ".bak"
    if existing is not None:
        with open(backup, "w", encoding="utf-8") as f:
            json.dump(existing, f, ensure_ascii=False, indent=2)
        print(f"已备份原清单 → {backup}")
    with open(args.plan, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"已写入 {args.plan}（{len(user_scopes)} 项 / {len(generated)} 批）")
    print("提醒：若批次数或批名发生变化，需同步更新 .env 的 LARK_AUTH_IDS 并重新绑定 GE 授权。")
    return 0


if __name__ == "__main__":
    sys.exit(main())