from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    env: str
    database_path: str
    artifacts_dir: str
    workdir_root: str

    # GitHub Apps: Code Agent
    code_app_id: str
    code_app_private_key_path: str
    code_webhook_secret: str

    # GitHub Apps: Reviewer Agent
    reviewer_app_id: str
    reviewer_app_private_key_path: str
    reviewer_webhook_secret: str

    # LLM (OpenRouter)
    openrouter_api_key: str
    openrouter_model: str
    openrouter_base_url: str
    openrouter_timeout_sec: int
    openrouter_max_retries: int
    openrouter_max_tokens: int

    github_api_base: str
    github_api_version: str
    git_user_name: str
    git_user_email: str

    @staticmethod
    def load() -> "Config":
        def _get_int(name: str, default: int) -> int:
            value = os.getenv(name, str(default))
            try:
                return int(value)
            except ValueError:
                return default

        return Config(
            env=os.getenv("APP_ENV", "dev"),
            database_path=os.getenv("DATABASE_PATH", "./data/agent.db"),
            artifacts_dir=os.getenv("ARTIFACTS_DIR", "./artifacts"),
            workdir_root=os.getenv("WORKDIR_ROOT", "./workdir"),
            code_app_id=os.getenv("CODE_APP_ID", ""),
            code_app_private_key_path=os.getenv("CODE_APP_PRIVATE_KEY_PATH", ""),
            code_webhook_secret=os.getenv("CODE_WEBHOOK_SECRET", ""),
            reviewer_app_id=os.getenv("REVIEWER_APP_ID", ""),
            reviewer_app_private_key_path=os.getenv("REVIEWER_APP_PRIVATE_KEY_PATH", ""),
            reviewer_webhook_secret=os.getenv("REVIEWER_WEBHOOK_SECRET", ""),
            openrouter_api_key=os.getenv("OPENROUTER_API_KEY", ""),
            openrouter_model=os.getenv("OPENROUTER_MODEL", "google/gemini-3-flash-preview"),
            openrouter_base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            openrouter_timeout_sec=_get_int("OPENROUTER_TIMEOUT_SEC", 60),
            openrouter_max_retries=_get_int("OPENROUTER_MAX_RETRIES", 2),
            openrouter_max_tokens=_get_int("OPENROUTER_MAX_TOKENS", 2048),
            github_api_base=os.getenv("GITHUB_API_BASE", "https://api.github.com"),
            github_api_version=os.getenv("GITHUB_API_VERSION", "2022-11-28"),
            git_user_name=os.getenv("GIT_USER_NAME", "code-agent[bot]"),
            git_user_email=os.getenv("GIT_USER_EMAIL", "code-agent@example.com"),
        )
