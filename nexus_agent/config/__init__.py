import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
    force=True,  # 强制重新配置，防止被其他库覆盖
)

logger = logging.getLogger(__name__)

LARK_AUTH_ID = os.getenv("LARK_AUTH_ID", "lark-agent-oauth_v1")


def _split_csv_ids(raw: str | None) -> list[str]:
    """把逗号分隔的授权资源 id 解析为**有序、去重**的列表。"""
    out: list[str] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if item and item not in out:
            out.append(item)
    return out


# 飞书单次授权能带的 scope 有上限，故按场景拆成多批（每批一个 authorization）。
# 顺序 = GE 里 toolAuthorizations 的 consent 顺序（1→N）；为空表示仍用单批
# LARK_AUTH_ID（未改造 / 回滚场景）。
LARK_AUTH_IDS: list[str] = _split_csv_ids(os.getenv("LARK_AUTH_IDS"))
LARK_DOMAIN = os.getenv("LARK_DOMAIN", "https://open.feishu.cn")
LARK_CLIENT_ID = os.getenv("LARK_CLIENT_ID")

GOOGLE_WORKSPACE_AUTH_ID = os.getenv("GOOGLE_WORKSPACE_AUTH_ID", "google-workspace-oauth_v1")
GOOGLE_WORKSPACE_PROJECT_ID = os.getenv("GOOGLE_WORKSPACE_PROJECT_ID", os.getenv("PROJECT_ID", ""))
