# -*- coding: utf-8 -*-
"""用户画像读写（user_profile.json）"""
from app.store import PROFILE_FILE, read_json, write_json

DEFAULT_PROFILE = {"manual": "", "auto": "", "auto_learn_enabled": True,
                   "learning_prompt_custom": ""}


def load_profile():
    data = read_json(PROFILE_FILE)
    if not isinstance(data, dict):
        return dict(DEFAULT_PROFILE)
    for key, val in DEFAULT_PROFILE.items():
        if key not in data:
            data[key] = val
    return data


def save_profile(profile):
    write_json(PROFILE_FILE, profile)
