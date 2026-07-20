# 配置包入口。
# env 负责 .env 和 provider 环境变量，paths 负责本地目录布局，
# settings 负责 settings.json 与聚合权限规则。
# 这里集中 re-export，降低调用方迁移成本。

from .env import find_project_env, load_project_env, provider_env
from .paths import CodematePaths, codemate_home, codemate_paths, ensure_codemate_layout, project_id_for_root
from .settings import CodemateSettings, PermissionRules, default_settings, load_codemate_settings

__all__ = [
    "CodematePaths",
    "CodemateSettings",
    "PermissionRules",
    "codemate_home",
    "codemate_paths",
    "default_settings",
    "ensure_codemate_layout",
    "find_project_env",
    "load_codemate_settings",
    "load_project_env",
    "project_id_for_root",
    "provider_env",
]
