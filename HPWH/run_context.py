# -*- coding: utf-8 -*-
"""
Last-run hand-off between the B (simulation) and C (results) scripts. HPWH_B1_gen_dec.py and
HPWH_B2_gen_inc.py save the test name and input folder they used; HPWH_C1_gen_dec_res.py and
HPWH_C2_gen_inc_res.py read the test name back, so they open the latest run automatically.

`pipeline` ("FLC_DEC" or "FLC_INC") keeps the DEC and INC values in separate small files
(.last_run_filename_FLC_DEC, ...), so the two never overwrite each other.
"""

import os
import json

_STATE_DIR = os.path.dirname(os.path.abspath(__file__))


def _state_file(pipeline=None):
    suffix = f"_{pipeline}" if pipeline else ""
    return os.path.join(_STATE_DIR, f".last_run_filename{suffix}")


def save_filename(name, pipeline=None):
    with open(_state_file(pipeline), "w") as f:
        f.write(name)


def load_filename(default, pipeline=None):
    try:
        with open(_state_file(pipeline)) as f:
            saved = f.read().strip()
        return saved or default
    except FileNotFoundError:
        return default


def _reserve_event_file(pipeline=None):
    suffix = f"_{pipeline}" if pipeline else ""
    return os.path.join(_STATE_DIR, f".last_reserve_event{suffix}.json")


def save_reserve_event(reserve_event, pipeline=None):
    with open(_reserve_event_file(pipeline), "w") as f:
        json.dump(reserve_event, f)


def load_reserve_event(default, pipeline=None):
    try:
        with open(_reserve_event_file(pipeline)) as f:
            saved = json.load(f)
        return saved or default
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _input_folder_file(pipeline=None):
    suffix = f"_{pipeline}" if pipeline else ""
    return os.path.join(_STATE_DIR, f".last_input_folder{suffix}")


def save_input_folder(name, pipeline=None):
    with open(_input_folder_file(pipeline), "w") as f:
        f.write(name)


def load_input_folder(default, pipeline=None):
    try:
        with open(_input_folder_file(pipeline)) as f:
            saved = f.read().strip()
        return saved or default
    except FileNotFoundError:
        return default
