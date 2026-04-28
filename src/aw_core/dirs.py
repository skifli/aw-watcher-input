import os
from pathlib import Path


def _xdg_dir(env_key: str, default_suffix: str) -> str:
    base = os.environ.get(env_key)
    if base:
        return str(Path(base))
    return str(Path.home() / default_suffix)


def get_config_dir(appname: str) -> str:
    return str(Path(_xdg_dir("XDG_CONFIG_HOME", ".config")) / appname)


def get_cache_dir(appname: str) -> str:
    return str(Path(_xdg_dir("XDG_CACHE_HOME", ".cache")) / appname)


def get_data_dir(appname: str) -> str:
    return str(Path(_xdg_dir("XDG_DATA_HOME", ".local/share")) / appname)
