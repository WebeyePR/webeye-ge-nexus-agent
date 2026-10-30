"""删除类脚本的确认策略 + 环境加载回归测试（2026-10-30）

### 一、确认策略

`delete_agent.sh` / `delete_authorization.sh` 原先的确认提示写在「参数为空」分支里
（等于对空值问一次 y/N，传参则完全不问），既没有护栏也没有自动化友好性。现改为：

    显式传参 → 视为明确意图，不二次确认（清理链路依赖此行为）
    不传参数 → 落到 .env/.deploy_env 的默认值，必须二次确认
              非交互环境无法确认时直接中止（一次 API 都不发），可用 --yes 跳过

### 二、环境加载（`load_env.sh` 的 $1 泄漏）

`source` 时 `$1` 是**调用方脚本**的位置参数。旧实现 `ENV_SUFFIX="${1:-$ENV_SUFFIX}"`
把 `delete_authorization.sh ge-nexus-feishu-1` 的 AUTH_ID 当成了环境后缀 → 去找
`.env-ge-nexus-feishu-1` → `PROJECT_ID` 为空 → 删除 URL 变成
`projects//locations/global/authorizations/...` → API 400 `RESOURCE_PROJECT_INVALID`。
本文件的 URL 断言即锁定该回归。

### 隔离性

脚本副本 + 假 `curl`/`gcloud` 全在临时目录，**不触碰任何线上资源**。
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"

CURL_SHIM = """#!/bin/bash
# 假 curl：记录参数到 CURL_LOG，按 FAKE_CURL_MODE 回放固定响应
printf '%s\\n' "$*" >> "${CURL_LOG:-/dev/null}"
case "${FAKE_CURL_MODE:-ok}" in
  ok)
    printf '{}\\nHTTP_STATUS:200\\n'
    ;;
  notfound)
    printf '{"error":{"code":404}}\\nHTTP_STATUS:404\\n'
    ;;
  linked)
    printf '{"error":{"code":400,"message":"Authorization is linked to a resource and cannot be deleted: projects/839062387451/locations/global/collections/default_collection/engines/e/assistants/default_assistant/agents/999"}}\\nHTTP_STATUS:400\\n'
    ;;
  *)
    printf '{}\\nHTTP_STATUS:500\\n'
    ;;
esac
"""

GCLOUD_SHIM = """#!/bin/bash
echo fake-access-token
"""


class Sandbox:
    def __init__(self, root: Path):
        self.root = root
        self.scripts = root / "scripts"
        self.log = root / "curl.log"

    def write_env(self, base: str = "", deploy: str = "", suffix=None, base_suffix: str = ""):
        (self.root / ".env").write_text(base, encoding="utf-8")
        (self.root / ".deploy_env").write_text(deploy, encoding="utf-8")
        if suffix:
            (self.root / f".env-{suffix}").write_text(base_suffix, encoding="utf-8")

    def _env(self, mode: str = "ok"):
        env = dict(os.environ)
        env["PATH"] = f"{self.root / 'bin'}:{env['PATH']}"
        env["CURL_LOG"] = str(self.log)
        env["FAKE_CURL_MODE"] = mode
        # 防止宿主 shell 的环境后缀/变量串进来
        for key in ("ENV_SUFFIX", "GE_AGENT_RESOURCE_NAME", "LARK_AUTH_ID", "LARK_AUTH_IDS",
                    "PROJECT_ID", "GE_APP_LOCATION", "GOOGLE_WORKSPACE_AUTH_ID"):
            if key not in ("PROJECT_ID",):
                env.pop(key, None)
        return env

    def run(self, name, *args, mode="ok"):
        return subprocess.run(
            ["bash", str(self.scripts / name), *args],
            cwd=str(self.root),
            env=self._env(mode),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
            # 脱离控制终端：让「有 /dev/tty 就读」的分支不会把测试卡死
            start_new_session=True,
        )

    def run_bash(self, code: str):
        return subprocess.run(
            ["bash", "-c", code],
            cwd=str(self.root),
            env=self._env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
            start_new_session=True,
        )

    def curls(self):
        return [ln for ln in self.log.read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.fixture
def sandbox(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in (
        "delete_agent.sh",
        "delete_authorization.sh",
        "load_env.sh",
        "update_agent_GE.sh",
        "register_to_GE.sh",
        "check_ge_auth_binding.sh",
    ):
        shutil.copy2(SCRIPTS_DIR / name, scripts / name)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(CURL_SHIM, encoding="utf-8")
    (bin_dir / "gcloud").write_text(GCLOUD_SHIM, encoding="utf-8")
    for f in ("curl", "gcloud"):
        (bin_dir / f).chmod(0o755)

    sb = Sandbox(tmp_path)
    sb.log.write_text("", encoding="utf-8")
    return sb


# ---------------------------------------------------------------- delete_agent.sh

def test_agent_help_exits_zero_without_calling_api(sandbox):
    sandbox.write_env("PROJECT_ID=p\n", "GE_AGENT_RESOURCE_NAME=agents/1\n")
    r = sandbox.run("delete_agent.sh", "--help")
    assert r.returncode == 0
    assert "使用方法" in r.stdout
    assert sandbox.curls() == []


def test_agent_explicit_arg_skips_second_confirmation(sandbox):
    """核心诉求：传参即明确意图，不弹确认。"""
    sandbox.write_env("PROJECT_ID=p\n", "GE_AGENT_RESOURCE_NAME=agents/1\n")
    r = sandbox.run("delete_agent.sh", "projects/p/locations/global/agents/777")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "跳过二次确认" in r.stdout
    assert "确认删除?" not in r.stdout + r.stderr
    called = sandbox.curls()
    assert len(called) == 1
    assert "projects/p/locations/global/agents/777" in called[0]
    # 传参不能再被 load_env.sh 当成环境后缀：PROJECT_ID 必须来自 .env
    assert "X-Goog-User-Project: p" in called[0]


def test_agent_no_arg_noninteractive_aborts_before_api_call(sandbox):
    """无参 = 隐含路径（删线上当前部署），非交互必须拦下且一次 API 都不发。"""
    sandbox.write_env("PROJECT_ID=p\n", "GE_AGENT_RESOURCE_NAME=agents/1\n")
    r = sandbox.run("delete_agent.sh")
    assert r.returncode == 1
    assert "非交互环境无法二次确认" in r.stdout
    assert "确认删除?" not in r.stdout  # 不该先问再报错
    assert sandbox.curls() == []


def test_agent_no_arg_with_yes_deletes(sandbox):
    sandbox.write_env("PROJECT_ID=p\n", "GE_AGENT_RESOURCE_NAME=agents/1\n")
    r = sandbox.run("delete_agent.sh", "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "已指定 --yes" in r.stdout
    assert len(sandbox.curls()) == 1


def test_agent_no_default_name_errors(sandbox):
    sandbox.write_env("PROJECT_ID=p\n", "")
    r = sandbox.run("delete_agent.sh")
    assert r.returncode == 1
    assert "未指定 Agent" in r.stdout
    assert "使用方法" in r.stdout
    assert sandbox.curls() == []


def test_agent_rejects_unknown_flag(sandbox):
    sandbox.write_env("PROJECT_ID=p\n", "")
    r = sandbox.run("delete_agent.sh", "--nope")
    assert r.returncode == 1
    assert "未知参数" in r.stdout
    assert sandbox.curls() == []


def test_agent_rejects_two_positional_args(sandbox):
    sandbox.write_env("PROJECT_ID=p\n", "")
    r = sandbox.run("delete_agent.sh", "agents/1", "agents/2")
    assert r.returncode == 1
    assert "只能指定一个 Agent" in r.stdout
    assert sandbox.curls() == []


def test_agent_failure_returns_nonzero(sandbox):
    """HTTP 失败必须非 0：否则 cleanup/CI 会把「没删掉」当成功。"""
    sandbox.write_env("PROJECT_ID=p\n", "")
    r = sandbox.run("delete_agent.sh", "projects/p/locations/global/agents/1", mode="boom")
    assert r.returncode == 1
    assert "删除失败" in r.stdout


# -------------------------------------------------------- delete_authorization.sh

def test_auth_explicit_arg_skips_second_confirmation(sandbox):
    sandbox.write_env("PROJECT_ID=p\nLARK_AUTH_ID=legacy-id\n", "")
    r = sandbox.run("delete_authorization.sh", "ge-nexus-feishu-2")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "跳过二次确认" in r.stdout
    assert "确认删除?" not in r.stdout + r.stderr
    called = sandbox.curls()
    assert len(called) == 1
    # 关键回归：URL 里必须有 PROJECT_ID（旧实现被 $1 污染后这里是 projects//locations）
    assert "projects/p/locations/global/authorizations/ge-nexus-feishu-2" in called[0]


def test_auth_no_arg_noninteractive_aborts_before_api_call(sandbox):
    sandbox.write_env("PROJECT_ID=p\nLARK_AUTH_ID=legacy-id\n", "")
    r = sandbox.run("delete_authorization.sh")
    assert r.returncode == 1
    assert "非交互环境无法二次确认" in r.stdout
    assert sandbox.curls() == []


def test_auth_no_arg_with_yes_deletes_legacy(sandbox):
    sandbox.write_env("PROJECT_ID=p\nLARK_AUTH_ID=legacy-id\n", "")
    r = sandbox.run("delete_authorization.sh", "--yes")
    assert r.returncode == 0, r.stdout + r.stderr
    called = sandbox.curls()
    assert len(called) == 1
    assert "authorizations/legacy-id" in called[0]


def test_auth_no_arg_warns_when_batch_mode_configured(sandbox):
    """分批模式下无参只删 legacy 单批，必须提示其余批次去哪删，避免以为全清干净了。"""
    sandbox.write_env("PROJECT_ID=p\nLARK_AUTH_ID=legacy-id\nLARK_AUTH_IDS=a,b,c,d\n", "")
    r = sandbox.run("delete_authorization.sh")
    assert r.returncode == 1
    assert "分批模式" in r.stdout
    assert "cleanup_deployed.sh" in r.stdout
    assert sandbox.curls() == []


def test_auth_404_is_idempotent_success(sandbox):
    sandbox.write_env("PROJECT_ID=p\n", "")
    r = sandbox.run("delete_authorization.sh", "gone-id", mode="notfound")
    assert r.returncode == 0
    assert "已不存在" in r.stdout


def test_auth_linked_to_agent_skips_and_points_to_delete_agent(sandbox):
    """被 Agent 占用时 API 拒绝删除：安全跳过（不是失败），并给出下一步命令。"""
    sandbox.write_env("PROJECT_ID=p\n", "")
    r = sandbox.run("delete_authorization.sh", "in-use-id", mode="linked")
    assert r.returncode == 0
    assert "无法被强制物理删除" in r.stdout
    assert "delete_agent.sh" in r.stdout


def test_auth_failure_returns_nonzero(sandbox):
    sandbox.write_env("PROJECT_ID=p\n", "")
    r = sandbox.run("delete_authorization.sh", "some-id", mode="boom")
    assert r.returncode == 1
    assert "删除失败" in r.stdout


# ------------------------------------------------------- 批处理链路（cleanup 依赖）

def test_cleanup_loop_deletes_every_auth_id(sandbox):
    """复刻 cleanup_deployed.sh 的 here-string 循环。

    若子脚本在传参路径上读 stdin，会吃掉循环输入导致「只删第一条」——本测试锁死该回归。
    """
    sandbox.write_env("PROJECT_ID=p\n", "")
    ids = ["batch-1", "batch-2", "batch-3"]
    code = (
        'while IFS= read -r auth_id; do\n'
        '  [ -z "$auth_id" ] && continue\n'
        f'  bash "{sandbox.scripts}/delete_authorization.sh" "$auth_id"\n'
        'done <<< "$IDS"\n'
    )
    r = sandbox.run_bash(code.replace('"$IDS"', '"%s"' % "\n".join(ids)))
    assert r.returncode == 0, r.stdout + r.stderr
    called = sandbox.curls()
    assert len(called) == 3, r.stdout
    for i, auth_id in enumerate(ids):
        assert f"authorizations/{auth_id}" in called[i]


# --------------------------------------------- load_env.sh：后缀只认 ENV_SUFFIX

def test_load_env_ignores_positional_args(sandbox):
    """传参不得改变加载目标：$1 是调用方的参数，不是环境后缀。"""
    sandbox.write_env("PROJECT_ID=base-proj\n", "")
    r = sandbox.run_bash(
        f'source "{sandbox.scripts}/load_env.sh"; echo "P=$PROJECT_ID F=$BASE_ENV_FILE"'
    )
    assert r.returncode == 0, r.stderr
    assert "P=base-proj" in r.stdout
    # 加载的必须是 .env 本体，而不是被 AUTH_ID/模式名污染出来的 .env-<参数>
    assert f"F={sandbox.root}/.env" in r.stdout


def test_load_env_honors_env_suffix_variable(sandbox):
    """切环境能力保留：ENV_SUFFIX 变量优先于 .env。"""
    sandbox.write_env("PROJECT_ID=base-proj\n", "", suffix="dev", base_suffix="PROJECT_ID=dev-proj\n")
    r = sandbox.run_bash(
        f'ENV_SUFFIX=dev bash -c \'source "{sandbox.scripts}/load_env.sh"; echo "P=$PROJECT_ID"\''
    )
    assert r.returncode == 0, r.stderr
    assert "P=dev-proj" in r.stdout


def test_load_env_warns_when_suffix_file_missing(sandbox):
    """后缀拼错不再静默空配置，加载阶段就告警。"""
    sandbox.write_env("PROJECT_ID=base-proj\n", "")
    r = sandbox.run_bash(
        f'ENV_SUFFIX=typo bash -c \'source "{sandbox.scripts}/load_env.sh"; echo "P=$PROJECT_ID"\''
    )
    assert "配置文件不存在" in r.stderr