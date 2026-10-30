#!/usr/bin/env python3
"""飞书分批授权资源工具（幂等 / 默认只读）。

用途：把 docs/lark_scope_batches.json 里定义的 4 批飞书 scope，落地成 GE 的
authorization 资源，并产出可绑定到 agent 的资源名。

子命令：
  --check        只检查（默认）：存在性、scope 数、redirect_uri、prompt
  --apply        创建缺失的资源；已存在的**绝不修改**（如有偏差只报告）
  --print-names  输出完整资源名（每行一个），供 shell 组装 toolAuthorizations

退出码：0 = 全部就绪；2 = 有缺失或偏差（供调用脚本 fail-fast）。

设计约束（改前必读）：
1. GET 返回的 clientSecret 是 <redacted>，因此**不提供自动修改**能力 ——
   改写既有资源一律走人工流程，避免把密钥写坏。
2. 只创建 LARK_AUTH_IDS 里列出的批次；未配置时回退单批 LARK_AUTH_ID 的历史
   行为由调用方（register_to_GE.sh）保留，本工具不越权。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_REDIRECT = "https://vertexaisearch.cloud.google.com/oauth-redirect"
AUTHORIZE_ENDPOINT = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
DEFAULT_TOKEN_URI = "https://open.feishu.cn/open-apis/authen/v2/oauth/token"


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


def split_ids(raw: str | None) -> list[str]:
    ids: list[str] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if item and item not in ids:
            ids.append(item)
    return ids


def build_uri(client_id: str, scopes: list[str]) -> str:
    """构造飞书授权 URL。

    必须手工拼接而非 urlencode：urlencode 会把空格编码成 '+'，飞书按字面量
    解析，导致 scope 全部失效。既有可用资源用的是 %20。
    """
    deduped = list(dict.fromkeys(scopes))
    query = "&".join(
        [
            f"client_id={urllib.parse.quote(client_id, safe='')}",
            "response_type=code",
            f"redirect_uri={urllib.parse.quote(DEFAULT_REDIRECT, safe='')}",
            f"scope={urllib.parse.quote(' '.join(deduped), safe='')}",
            "prompt=consent",
        ]
    )
    return f"{AUTHORIZE_ENDPOINT}?{query}"


def get_token() -> str:
    token = subprocess.check_output(
        ["gcloud", "auth", "print-access-token"], text=True
    ).strip()
    if not token:
        raise SystemExit("无法获取 gcloud access token，请先 gcloud auth login")
    return token


def project_number(project_id: str) -> str:
    return subprocess.check_output(
        ["gcloud", "projects", "describe", project_id, "--format=value(projectNumber)"],
        text=True,
    ).strip()


def api(method: str, url: str, payload: dict | None, token: str) -> tuple[int, dict]:
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode() or "{}"
            return resp.status, json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode() or "{}"
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"raw": body[:400]}


def inspect_uri(uri: str) -> dict:
    query = urllib.parse.parse_qs(urllib.parse.urlparse(uri).query)
    scopes: list[str] = []
    for value in query.get("scope", []):
        scopes.extend(value.split())
    return {
        "scope_count": len(scopes),
        "has_redirect": "redirect_uri" in query,
        "redirect": (query.get("redirect_uri") or [""])[0],
        "prompt": (query.get("prompt") or [""])[0],
        "duplicates": len(scopes) - len(set(scopes)),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--env-file", default=".env")
    ap.add_argument("--plan", default="docs/lark_scope_batches.json")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="只检查（默认）")
    mode.add_argument("--apply", action="store_true", help="创建缺失资源")
    mode.add_argument("--print-names", action="store_true", help="输出资源全名")
    args = ap.parse_args()

    env = parse_env_file(args.env_file)
    client_id = env.get("LARK_CLIENT_ID")
    client_secret = env.get("LARK_CLIENT_SECRET")
    project_id = env.get("PROJECT_ID", "webeye-internal-test")
    token_uri = env.get("LARK_TOKEN_URI") or DEFAULT_TOKEN_URI
    auth_ids = split_ids(env.get("LARK_AUTH_IDS"))

    if not auth_ids:
        print("LARK_AUTH_IDS 未配置：当前为单批模式，本工具不适用。", file=sys.stderr)
        return 0
    if not client_id or not client_secret:
        print("缺少 LARK_CLIENT_ID / LARK_CLIENT_SECRET，无法创建授权资源。", file=sys.stderr)
        return 2
    if not os.path.exists(args.plan):
        print(f"找不到批次定义 {args.plan}", file=sys.stderr)
        return 2

    plan = json.load(open(args.plan, encoding="utf-8"))
    by_name = {b["name"]: b for b in plan["batches"]}
    unknown = [i for i in auth_ids if i not in by_name]
    if unknown:
        print(f"LARK_AUTH_IDS 含未在批次定义中的 id: {unknown}", file=sys.stderr)
        return 2

    number = project_number(project_id)
    base = (
        f"https://global-discoveryengine.googleapis.com/v1alpha/projects/{number}"
        f"/locations/global/authorizations"
    )
    full_names = [f"projects/{number}/locations/global/authorizations/{i}" for i in auth_ids]

    if args.print_names:
        print("\n".join(full_names))
        return 0

    token = get_token()
    problems: list[str] = []

    for auth_id in auth_ids:
        batch = by_name[auth_id]
        expected = len(list(dict.fromkeys(batch["scopes"])))
        code, body = api("GET", f"{base}/{auth_id}", None, token)

        if code == 404:
            if args.apply:
                payload = {
                    "serverSideOauth2": {
                        "clientId": client_id,
                        "clientSecret": client_secret,
                        "tokenUri": token_uri,
                        "authorizationUri": build_uri(client_id, batch["scopes"]),
                    }
                }
                code2, body2 = api(
                    "POST", f"{base}?authorizationId={auth_id}", payload, token
                )
                if code2 in (200, 201):
                    print(f"  {auth_id:22s} ✓ 已创建（scope={expected}）")
                else:
                    print(f"  {auth_id:22s} ★ 创建失败 HTTP {code2}: {str(body2)[:160]}")
                    problems.append(auth_id)
            else:
                print(f"  {auth_id:22s} ★ 不存在（用 --apply 创建）")
                problems.append(auth_id)
            continue

        if code != 200:
            print(f"  {auth_id:22s} ★ 查询失败 HTTP {code}")
            problems.append(auth_id)
            continue

        info = inspect_uri(((body.get("serverSideOauth2") or {}).get("authorizationUri")) or "")
        issues = []
        if info["scope_count"] != expected:
            issues.append(f"scope {info['scope_count']}≠{expected}")
        if not info["has_redirect"]:
            issues.append("缺 redirect_uri")
        if info["duplicates"]:
            issues.append(f"重复 {info['duplicates']} 项")
        if issues:
            print(f"  {auth_id:22s} ★ 存在但不符合预期: {'; '.join(issues)}（不会自动修改）")
            problems.append(auth_id)
        else:
            print(f"  {auth_id:22s} ✓ 就绪（scope={expected}, redirect, prompt={info['prompt'] or '-'}）")

    print()
    if problems:
        print(f"结论: ★ {len(problems)} 个批次未就绪: {problems}")
        return 2
    print(f"结论: ✓ {len(auth_ids)} 个批次全部就绪")
    return 0


if __name__ == "__main__":
    sys.exit(main())