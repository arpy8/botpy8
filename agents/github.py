"""Publish projects to the bot GitHub account without touching the owner's git setup.

The machine running botpy8_soul usually has the owner's own GitHub login stored
(global user.name/email, Git Credential Manager). Every git call here is
scoped so none of that is read or written:

- identity is set in the project repo's *local* config only;
- the bot token goes in an Authorization header passed through GIT_CONFIG_*
  environment variables for that single process — never in argv, a remote
  URL, or any config file;
- credential.helper is reset to empty for the same process, so git neither
  looks up the owner's stored GitHub password nor saves the bot's.
"""

import base64
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import NamedTuple

API = "https://api.github.com"
GITHUB_HOST = "https://github.com"
TOKEN_ENV = "BOTPY8_GITHUB_TOKEN"
DEFAULT_GITIGNORE = "node_modules/\n.next/\n.venv/\n__pycache__/\n.pytest_cache/\n.qa-server.log\n.env*\n"


class BotAccount(NamedTuple):
    login: str
    token: str
    name: str
    email: str


def _api(method: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "botpy8-soul-publisher",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except json.JSONDecodeError:
            payload = {}
        return exc.code, payload


def load_bot_account() -> BotAccount:
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        raise RuntimeError(f"{TOKEN_ENV} is not set; add the bot account's token to .env")
    status, user = _api("GET", "/user", token)
    if status != 200:
        raise RuntimeError(f"bot token rejected by GitHub (HTTP {status}): {user.get('message', '')}")
    login = user["login"]
    # The noreply address attributes commits to the bot without exposing an inbox.
    return BotAccount(login, token, login, f"{user['id']}+{login}@users.noreply.github.com")


def ensure_repo(account: BotAccount, name: str, description: str) -> str:
    """Create a public repo under the bot account; reuse it if it already exists (resume)."""
    status, repo = _api("POST", "/user/repos", account.token, {
        "name": name,
        "description": description[:350],
        "private": False,
        "has_wiki": False,
        "auto_init": False,
    })
    if status == 201:
        return repo["html_url"]
    if status == 422:
        existing_status, existing = _api("GET", f"/repos/{account.login}/{name}", account.token)
        if existing_status == 200:
            return existing["html_url"]
    raise RuntimeError(f"could not create repo {account.login}/{name} (HTTP {status}): {repo.get('message', '')}")


def remote_url(account: BotAccount, name: str) -> str:
    return f"{GITHUB_HOST}/{account.login}/{name}.git"


def bot_git_env(token: str) -> dict:
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = dict(os.environ)
    env.pop(TOKEN_ENV, None)
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GCM_INTERACTIVE="never",
        GIT_CONFIG_COUNT="2",
        GIT_CONFIG_KEY_0="credential.helper",
        GIT_CONFIG_VALUE_0="",
        GIT_CONFIG_KEY_1=f"http.{GITHUB_HOST}/.extraheader",
        GIT_CONFIG_VALUE_1=f"AUTHORIZATION: basic {basic}",
    )
    return env


def _git(project_dir: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=project_dir, capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL
    )


def push_project(project_dir: Path, account: BotAccount, url: str, message: str) -> tuple[bool, str]:
    """Commit the project as the bot in its own repo and push it to `url`."""
    if not (project_dir / ".git").exists():
        # Checked on disk: `git rev-parse` here would find the enclosing botpy8_soul repo.
        result = _git(project_dir, "init", "-b", "main")
        if result.returncode != 0:
            return False, result.stderr
    if not (project_dir / ".gitignore").exists():
        (project_dir / ".gitignore").write_text(DEFAULT_GITIGNORE, encoding="utf-8")
    for key, value in (("user.name", account.name), ("user.email", account.email)):
        _git(project_dir, "config", "--local", key, value)
    _git(project_dir, "add", "-A")
    if _git(project_dir, "diff", "--cached", "--quiet").returncode != 0:
        result = _git(project_dir, "commit", "-m", message)
        if result.returncode != 0:
            return False, result.stdout + result.stderr
    if _git(project_dir, "remote", "get-url", "origin").returncode == 0:
        _git(project_dir, "remote", "set-url", "origin", url)
    else:
        _git(project_dir, "remote", "add", "origin", url)
    result = _git(project_dir, "push", "-u", "origin", "main", env=bot_git_env(account.token))
    output = (result.stdout + result.stderr).replace(account.token, "***")
    return result.returncode == 0, output
