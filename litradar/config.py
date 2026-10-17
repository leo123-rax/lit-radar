"""Configuration: .env loading, YAML config files, and a Settings object.

No third-party dotenv dependency: the .env format we support is KEY=VALUE per line,
'#' comments, optional surrounding quotes. Existing environment variables win.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"


def load_env(path: Optional[Path] = None) -> Dict[str, str]:
    """Load KEY=VALUE pairs from a .env file into os.environ (without overriding)."""
    path = Path(path) if path else REPO_ROOT / ".env"
    loaded: Dict[str, str] = {}
    if not path.exists():
        return loaded
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.split(" #", 1)[0].strip()  # allow trailing comments
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded[key] = value
    return loaded


def load_yaml(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


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
    llm_model: str = "claude-opus-5-5"
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
            contact_email=os.environ.get("LITRADAR_CONTACT_EMAIL", ""),
            ncbi_api_key=os.environ.get("NCBI_API_KEY", ""),
            semantic_scholar_api_key=os.environ.get("SEMANTIC_SCHOLAR_API_KEY", ""),
            openalex_api_key=os.environ.get("OPENALEX_API_KEY", ""),
            altmetric_api_key=os.environ.get("ALTMETRIC_API_KEY", ""),
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
            llm_model=os.environ.get("LITRADAR_LLM_MODEL", "claude-opus-5-5"),
            smtp_host=os.environ.get("SMTP_HOST", ""),
            smtp_port=int(os.environ.get("SMTP_PORT", "587") or 587),
            smtp_user=os.environ.get("SMTP_USER", ""),
            smtp_password=os.environ.get("SMTP_PASSWORD", ""),
            digest_from=os.environ.get("DIGEST_FROM", ""),
            digest_to=os.environ.get("DIGEST_TO", ""),
            slack_webhook_url=os.environ.get("SLACK_WEBHOOK_URL", ""),
            radar=radar,
        )
        return s

    def yaml(self, name: str) -> Any:
        """Load a named YAML file from the config directory (e.g. 'instruments')."""
        path = self.config_dir / f"{name}.yaml"
        return load_yaml(path) if path.exists() else {}
