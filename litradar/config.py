"""Configuration: .env loading, YAML config files, and a Settings object.

No third-party dotenv dependency. The .env format we support:

    KEY=VALUE                  # unquoted: a ' #' (space + hash) starts a trailing comment
    KEY="value with # inside"  # quoted (single or double): taken literally up to the closing quote
    export KEY=value           # the shell 'export' prefix is accepted and ignored
    # full-line comments and blank lines are skipped

Existing environment variables win over the file. An empty value (``KEY=``) is loaded as an empty
string; settings that have a default (such as the LLM model) fall back to it when the value is empty.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_LLM_MODEL = "claude-opus-5-5"


def parse_env_line(raw: str) -> Optional[tuple]:
    """Parse one .env line into (key, value) or None for blanks, comments and malformed lines."""
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    if line.startswith("export ") or line.startswith("export\t"):
        line = line[len("export") :].strip()
        if "=" not in line:
            return None
    key, _, value = line.partition("=")
    key = key.strip()
    value = value.strip()
    if not key:
        return None
    if value[:1] in ("'", '"'):
        quote = value[0]
        close = value.find(quote, 1)
        if close != -1:
            # quoted: everything up to the closing quote, verbatim (a '#' inside is data, not a comment)
            value = value[1:close]
        else:
            value = value[1:]  # unterminated quote: take the rest literally
    else:
        value = value.split(" #", 1)[0].split("\t#", 1)[0].strip()  # trailing comment only outside quotes
    return key, value


def load_env(path: Optional[Path] = None) -> Dict[str, str]:
    """Load KEY=VALUE pairs from a .env file into os.environ (without overriding)."""
    path = Path(path) if path else REPO_ROOT / ".env"
    loaded: Dict[str, str] = {}
    if not path.exists():
        return loaded
    for raw in path.read_text(encoding="utf-8").splitlines():
        parsed = parse_env_line(raw)
        if not parsed:
            continue
        key, value = parsed
        if key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded


def load_yaml(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _env(name: str, default: str = "") -> str:
    """Environment value, or ``default`` when the variable is unset *or empty*."""
    v = os.environ.get(name)
    return v if v else default


@dataclass
class Settings:
    """Everything the pipeline needs that is not a source list.

    Keys come from the environment (after .env is loaded); file paths default to
    the repository layout so a fresh clone works with zero configuration.
    """

    config_dir: Path = DEFAULT_CONFIG_DIR
    db_path: Path = REPO_ROOT / "data" / "radar.sqlite"
    cache_dir: Path = REPO_ROOT / ".cache"
    out_dir: Path = REPO_ROOT / "digests"
    contact_email: str = ""
    ncbi_api_key: str = ""
    semantic_scholar_api_key: str = ""
    openalex_api_key: str = ""
    altmetric_api_key: str = ""
    anthropic_api_key: str = ""
    llm_model: str = DEFAULT_LLM_MODEL
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    digest_from: str = ""
    digest_to: str = ""
    slack_webhook_url: str = ""
    radar: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_env(cls, config_dir: Optional[Path] = None, env_file: Optional[Path] = None) -> Settings:
        load_env(env_file)
        cfg_dir = Path(config_dir or os.environ.get("LITRADAR_CONFIG_DIR") or DEFAULT_CONFIG_DIR)
        radar = load_yaml(cfg_dir / "radar.yaml") if (cfg_dir / "radar.yaml").exists() else {}
        s = cls(
            config_dir=cfg_dir,
            db_path=Path(os.environ.get("LITRADAR_DB") or radar.get("db_path") or REPO_ROOT / "data" / "radar.sqlite"),
            cache_dir=Path(os.environ.get("LITRADAR_CACHE_DIR") or REPO_ROOT / ".cache"),
            out_dir=Path(os.environ.get("LITRADAR_OUT_DIR") or radar.get("out_dir") or REPO_ROOT / "digests"),
            contact_email=_env("LITRADAR_CONTACT_EMAIL"),
            ncbi_api_key=_env("NCBI_API_KEY"),
            semantic_scholar_api_key=_env("SEMANTIC_SCHOLAR_API_KEY"),
            openalex_api_key=_env("OPENALEX_API_KEY"),
            altmetric_api_key=_env("ALTMETRIC_API_KEY"),
            anthropic_api_key=_env("ANTHROPIC_API_KEY"),
            llm_model=_env("LITRADAR_LLM_MODEL", DEFAULT_LLM_MODEL),
            smtp_host=_env("SMTP_HOST"),
            smtp_port=int(_env("SMTP_PORT", "587")),
            smtp_user=_env("SMTP_USER"),
            smtp_password=_env("SMTP_PASSWORD"),
            digest_from=_env("DIGEST_FROM"),
            digest_to=_env("DIGEST_TO"),
            slack_webhook_url=_env("SLACK_WEBHOOK_URL"),
            radar=radar,
        )
        return s

    def yaml(self, name: str) -> Any:
        """Load a named YAML file from the config directory (e.g. 'instruments')."""
        path = self.config_dir / f"{name}.yaml"
        return load_yaml(path) if path.exists() else {}
