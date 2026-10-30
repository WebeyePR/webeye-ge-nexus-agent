import json
import os
from urllib.parse import parse_qs, quote, urlparse

URL_LENGTH_WARNING_THRESHOLD = 4000

# 官方登记的 GE OAuth 回调（飞书侧 authorize 缺它会直接报「redirect_uri 请求不合法」）
DEFAULT_REDIRECT_URI = "https://vertexaisearch.cloud.google.com/oauth-redirect"


def build_auth_url(base_url: str, params: dict) -> str:
    query_parts = []
    for key, value in params.items():
        encoded_key = quote(str(key), safe="")
        if key == "scope":
            encoded_value = quote(str(value), safe=":")
        else:
            encoded_value = quote(str(value), safe="")
        query_parts.append(f"{encoded_key}={encoded_value}")
    return f"{base_url}?{'&'.join(query_parts)}"


def build_length_warning(auth_url: str, scope_count: int) -> str:
    if len(auth_url) <= URL_LENGTH_WARNING_THRESHOLD:
        return ""

    return (
        f"警告: 授权链接长度为 {len(auth_url)} 字符，包含 {scope_count} 个 scope，"
        f"已超过 {URL_LENGTH_WARNING_THRESHOLD} 字符的保守阈值。\n"
        "这可能在浏览器、代理、网关或飞书授权服务前置层触发 HTTP 431。\n"
        "建议按场景精简 scope，或在 Gemini Enterprise Authorization 资源中使用 serverSideOauth2.scopes。"
    )


def generate_batch_auth_urls() -> bool:
    """分批授权模式：按 docs/lark_scope_batches.json 每批生成一条链接。

    触发条件：LARK_AUTH_IDS 非空且批次定义文件存在。返回 True 表示已处理，
    调用方应直接结束（不再走旧的单批链接逻辑）。

    为什么必须分批：飞书单次授权能带的 scope 有上限（本项目按 45/批 设计），
    173 项用户态权限无法塞进一条链接。
    """
    ids = [x.strip() for x in (os.getenv("LARK_AUTH_IDS") or "").split(",") if x.strip()]
    root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    plan_path = os.path.join(root_dir, "docs", "lark_scope_batches.json")
    if not ids or not os.path.exists(plan_path):
        return False

    client_id = os.getenv("LARK_CLIENT_ID")
    if not client_id:
        print("Error: LARK_CLIENT_ID not found in .env file.")
        return True

    try:
        with open(plan_path, encoding="utf-8") as f:
            plan = json.load(f)
    except json.JSONDecodeError:
        print(f"Error: Failed to parse {plan_path}.")
        return True

    by_name = {b.get("name"): b for b in plan.get("batches", [])}
    redirect_uri = os.getenv("LARK_REDIRECT_URI") or DEFAULT_REDIRECT_URI
    base_url = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"

    print("=" * 50)
    print(f"分批授权模式：LARK_AUTH_IDS 共 {len(ids)} 批")
    print(f"redirect_uri: {redirect_uri}")
    print("=" * 50)

    for index, auth_id in enumerate(ids, 1):
        batch = by_name.get(auth_id)
        if not batch:
            print(f"\n[{index}] {auth_id}: ★ 批次定义缺失，跳过（请检查 docs/lark_scope_batches.json）")
            continue
        scopes = list(dict.fromkeys(batch.get("scopes") or []))
        params = {
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "prompt": "consent",
        }
        url = build_auth_url(base_url, params)
        print(f"\n[{index}] {auth_id}（{len(scopes)} 项 · {batch.get('label', '')}）")
        print(url)
        warning = build_length_warning(url, len(scopes))
        if warning:
            print(warning)

    print("\n" + "=" * 50)
    print("说明：分批模式下这些链接仅用于人工核验；真实授权由 GE 按 authorization 资源触发。")
    print("=" * 50)
    return True


def generate_lark_auth_url():
    # 1. 尝试加载 .env 文件获取配置
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    # 分批授权优先：LARK_AUTH_IDS 非空且批次定义存在时，按批输出并结束
    if generate_batch_auth_urls():
        return

    # 2. 读取权限配置文件
    scopes_file = os.path.join(os.path.dirname(__file__), 'lark_scopes.json')
    if not os.path.exists(scopes_file):
        print(f"Error: {scopes_file} not found.")
        return

    with open(scopes_file) as f:
        try:
            scopes_data = json.load(f)
        except json.JSONDecodeError:
            print(f"Error: Failed to parse {scopes_file}. Please check the JSON format.")
            return

    # 合并 user 和 tenant 权限（OAuth2 授权通常需要 user 权限）
    user_scopes = scopes_data.get('scopes', {}).get('user', [])
    tenant_scopes = scopes_data.get('scopes', {}).get('tenant', [])
    all_scopes = sorted(list(set(user_scopes) | set(tenant_scopes)))

    if not all_scopes:
        print("Error: No scopes found in lark_scopes.json.")
        return

    # 3. 获取关键配置参数
    client_id = os.getenv("LARK_CLIENT_ID")
    redirect_uri = os.getenv("LARK_REDIRECT_URI")

    # 如果没有显式的 LARK_CLIENT_ID，尝试从现有的 LARK_AUTHORIZATION_URI 中解析
    existing_uri = os.getenv("LARK_AUTHORIZATION_URI")
    if not client_id and existing_uri:
        parsed_uri = urlparse(existing_uri)
        query_params = parse_qs(parsed_uri.query)
        client_id = query_params.get('client_id', [None])[0]
        if not redirect_uri:
            redirect_uri = query_params.get('redirect_uri', [None])[0]

    if not client_id:
        print("Error: LARK_CLIENT_ID not found in .env file.")
        print("Please add 'LARK_CLIENT_ID=your_cli_id' to your .env file.")
        return

    # 4. 构造新的 URL
    base_url = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"
    params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": " ".join(all_scopes) # 注意：urlencode 会处理空格转义
    }

    # 永远带 redirect_uri：缺它飞书 authorize 会直接报「redirect_uri 请求不合法」
    params["redirect_uri"] = redirect_uri or DEFAULT_REDIRECT_URI
    params["prompt"] = "consent"

    final_url = build_auth_url(base_url, params)

    print("-" * 50)
    print("成功生成飞书授权链接！")
    print("-" * 50)
    print(f"\n{final_url}\n")
    print("-" * 50)
    length_warning = build_length_warning(final_url, len(all_scopes))
    if length_warning:
        print(length_warning)
        print("-" * 50)
    print("操作建议：")
    print("1. 确保已在飞书开放平台后台勾选了 lark_scopes.json 中的所有权限并发布版本。")
    print("2. 将上述链接更新到 .env 文件的 LARK_AUTHORIZATION_URI 变量中。")
    print("3. 点击该链接重新进行授权以获取包含新权限的 Token。")
    print("-" * 50)

if __name__ == "__main__":
    generate_lark_auth_url()
