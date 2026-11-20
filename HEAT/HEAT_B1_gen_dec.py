# -*- coding: utf-8 -*-
"""
FLC DEC (load-up) test: electric space heating pre-heats (LOAD or ALU) to hold the fleet's added load,
delta_p = controlled - baseline, at a target during a winter event.
"""

import os
import json
import math
import copy
import random
import shutil
import threading
import datetime as dt
import concurrent.futures
from pathlib import Path

import pandas as pd
import ochre
from ochre import Dwelling
from ochre.utils.schedule import ALL_SCHEDULE_NAMES
from run_context import save_filename, save_input_folder

#########################################
# PATHS (same in every flc_gen file)
#########################################

WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
WEATHER_FILE = os.path.join(WORKING_DIR, "Weather", "USA_OR_Portland.Intl.AP.726980_TMY3.epw")
METADATA_FILE = os.path.join(WORKING_DIR, "..", "HPWH", "Metadata", "OR_upgrade9.csv")
XML_NAME = "home.xml"
SCHEDULE_NAME = "in.schedules.csv"

#########################################
# USER SETTINGS
#########################################

# Test name; each test gets its own folder in Ready_data.
filename = os.environ.get('OCHRE_FILENAME', 'FLC_HEAT_DEC_Max_LU')
PIPELINE = "FLC_HEAT_DEC"
LOAD_NAME = "HEAT"

# Input folder with every home of the fleet.
INPUT_FOLDER = "All Input Files"
MAX_HOMES = int(os.environ.get('FLC_MAX_HOMES', 0))  # 0 = every home; a small number for a quick test

# Day 1 is warm-up, the event happens on day 2.
START = dt.datetime(2018, 1, 24, 0, 0)
DURATION_DAYS = 2
T_RES = int(os.environ.get('OCHRE_TIMESTEP_MIN', 1))  # minutes

# Event start time, length, and the added-load target (kW, positive) with its tolerance band.
if 'reserve_event' not in globals():
    reserve_event = {
        'dispatch_time': os.environ.get('OCHRE_DISPATCH_TIME', '14:00'),
        'duration': float(os.environ.get('OCHRE_DURATION_HR', 2.0)),
    }
TARGET_KW = float(os.environ.get('OCHRE_TARGET_DELTA_KW', 10000.0))
BAND_KW = float(os.environ.get('OCHRE_BAND_KW', 0.5))
SIGN = 1  # fleet change = SIGN * (controlled - baseline), positive when the fleet is doing its job

# Control level sent to dispatched units: "LOAD" or "ALU".
LEVEL = os.environ.get('OCHRE_LEVEL', 'LOAD')

# Dispatch settings (same logic as the HPWH tests).
EXPECTED_KW_PER_UNIT = 4.0  # assumed change per dispatched unit at the default level, used to size each batch
RAMP_STEPS = max(1, round(3 / T_RES))  # a new unit counts as "pending" for this many steps
RAMP_BATCH_SIZE = 30               # max units dispatched per step during the ramp
RAMP_BATCH_SPACING_MIN = 1
MAX_ADMISSIONS_SAFETY_CAP = 20   # max units dispatched per step after the ramp
ALLOW_OVERSHOOT_RELEASE = True   # above the band, park some active units
RELEASE_GROUP_SIZE = 20          # after the event, units go back to idle in groups of this size
RELEASE_GROUP_SPACING_MIN = 15   # minutes between release groups

#########################################
# DERIVED VALUES (same in every flc_gen file)
#########################################

INPUT_DIR = os.path.join(WORKING_DIR, INPUT_FOLDER, "bldg")
RESULTS_DIR = os.path.join(WORKING_DIR, "Ready_data", filename)
DT = dt.timedelta(minutes=T_RES)

EVENT_DATE = (START + dt.timedelta(days=DURATION_DAYS - 1)).date()
dispatch_t = pd.to_datetime(f"{EVENT_DATE} {reserve_event['dispatch_time']}")
event_end = dispatch_t + pd.Timedelta(hours=reserve_event['duration'])


def f_to_c(temp_f):
    return (temp_f - 32) * 5 / 9


def f_to_c_delta(delta_f):
    return delta_f * 5 / 9


def metadata_homes(column, values):
    """Home IDs (e.g. 'bldg0000062') whose ResStock metadata column has one of these values."""
    df = pd.read_csv(METADATA_FILE, usecols=["bldg_id", column], low_memory=False)
    return {f"bldg{int(b):07d}" for b in df.loc[df[column].isin(values), "bldg_id"]}

#########################################
# DEVICE SETTINGS (the part that differs between loads)
#########################################

POWER = "HVAC Heating Electric Power (kW)"
STATE_LOG_COL = "Indoor Air Temperature (C)"
SAVE_COLS = ["Time", "Total Electric Power (kW)", POWER, "Temperature - Indoor (C)", "Temperature - Outdoor (C)"]
# All homes: their HVAC was replaced by the standard ASHP.
HOME_IDS = None  # every home has the same ASHP (see HEAT_A3_adjustXML.py)
ON_KW = 0.1  # a unit counts as running when its power is above this

# Heating control parameters (F)
# GE - Grid Emergency, CP - Critical Peak, Shed
HEAT_Tcontrol_GEF = 45
HEAT_Tcontrol_GEdeadbandF = 4
HEAT_Tcontrol_CPF = 60
HEAT_Tcontrol_CPdeadbandF = 4
HEAT_Tcontrol_SHEDF = 64
HEAT_Tcontrol_deadbandF = 4
# ALU - Advanced load up, LOAD - Load up
HEAT_Tcontrol_ALUF = 76
HEAT_Tcontrol_ALUdeadbandF = 2
HEAT_Tcontrol_LOADF = 72
HEAT_Tcontrol_LOADdeadbandF = 2

HEAT_TbaselineF = 68
HEAT_TdeadbandF = 2
HEAT_TinitF = 68

# Response time in seconds
HVAC_RESPONSE_MIN = 30
HVAC_RESPONSE_MAX = 90

# (setpoint F, deadband F) for each level.
LEVELS = {"GE": (HEAT_Tcontrol_GEF, HEAT_Tcontrol_GEdeadbandF),
          "CP": (HEAT_Tcontrol_CPF, HEAT_Tcontrol_CPdeadbandF),
          "SHED": (HEAT_Tcontrol_SHEDF, HEAT_Tcontrol_deadbandF),
          "ALU": (HEAT_Tcontrol_ALUF, HEAT_Tcontrol_ALUdeadbandF),
          "LOAD": (HEAT_Tcontrol_LOADF, HEAT_Tcontrol_LOADdeadbandF)}
LEVEL_SETPOINT_F, LEVEL_DEADBAND_F = LEVELS[LEVEL]
RESPONSE_MIN_S = HVAC_RESPONSE_MIN
RESPONSE_MAX_S = HVAC_RESPONSE_MAX

EQUIPMENT_ARGS = {}
DWELLING_ARGS = {"Envelope": {"initial_temp_setpoint": f_to_c(HEAT_TinitF)}}  # starting indoor temperature


def setpoint_ctrl(setpoint_f, deadband_f):
    """Thermostat command sent every step; HVAC Cooling is kept off so it never fights this test."""
    return {"HVAC Heating": {"Setpoint": f_to_c(setpoint_f), "Deadband": f_to_c_delta(deadband_f)},
            "HVAC Cooling": {"Load Fraction": 0}}


BASELINE_CTRL = setpoint_ctrl(HEAT_TbaselineF, HEAT_TdeadbandF)
LEVEL_CTRL = setpoint_ctrl(LEVEL_SETPOINT_F, LEVEL_DEADBAND_F)
CONFIG_EXTRA = {"level_setpoint_f": LEVEL_SETPOINT_F, "level_deadband_f": LEVEL_DEADBAND_F,
                "baseline_setpoint_f": HEAT_TbaselineF, "baseline_deadband_f": HEAT_TdeadbandF}


#########################################
# DEVICE RULES (how a unit is controlled, dispatched and released)
#########################################

def get_device(dwelling):
    return dwelling.get_equipment_by_end_use("HVAC Heating")


def indoor_temp(u):
    return u["dwelling"].envelope.indoor_zone.temperature


def running(u):
    return u["dev"].electric_kw > ON_KW


def state_value(u, t):
    return indoor_temp(u)


def on_start(u, t):
    pass


def control(u, state, t):
    return LEVEL_CTRL if state == "active" else BASELINE_CTRL  # parked = back on the baseline setpoint

# DEC: LOAD/ALU = raised (pre-heat) setpoint; the unit runs until its thermostat is satisfied, then it is
# parked back on the baseline setpoint.
assert LEVEL in ("LOAD", "ALU"), "DEC level must be LOAD or ALU"


def will_turn_on(u):
    return indoor_temp(u) < f_to_c(LEVEL_SETPOINT_F - LEVEL_DEADBAND_F)  # cold enough to start heating


def limit_reached(u, t):
    return "satisfied" if not running(u) else None


def can_start_fresh(u, t, base_p):
    return base_p <= ON_KW and will_turn_on(u)


def can_recycle(u, t):
    return not running(u) and will_turn_on(u)


def priority(u, t):
    return indoor_temp(u)  # coldest house first


def overshoot_priority(u, t):
    return -priority(u, t)

# OCHRE seeds the global random generator while building a Dwelling, so only one is built at a time.
_dwelling_lock = threading.Lock()

#########################################
# HELPERS (same in every flc_gen file)
#########################################

def find_homes(base_dir):
    """Every folder that has a home.xml and an in.schedules.csv (and is in HOME_IDS, if set)."""
    homes = []
    for item in sorted(os.listdir(base_dir)):
        path = os.path.join(base_dir, item)
        if HOME_IDS is not None and item.split("-")[0] not in HOME_IDS:
            continue
        if os.path.isfile(os.path.join(path, XML_NAME)) and os.path.isfile(os.path.join(path, SCHEDULE_NAME)):
            homes.append(path)
    return homes


def filter_schedules(home_path):
    """Writes a copy of the schedule file with only the columns OCHRE understands."""
    df = pd.read_csv(os.path.join(home_path, SCHEDULE_NAME))
    df = df[[c for c in df.columns if c in ALL_SCHEDULE_NAMES]]
    out_file = os.path.join(home_path, f'filtered_schedules_{PIPELINE}.csv')
    tmp_file = f"{out_file}.{threading.get_ident()}.tmp"
    df.to_csv(tmp_file, index=False)
    os.replace(tmp_file, out_file)  # never leaves a half-written file for another run to read
    return out_file


def make_dwelling(home_path, name):
    args = {
        "start_time": START,
        "time_res": DT,
        "duration": dt.timedelta(days=DURATION_DAYS),
        "hpxml_file": os.path.join(home_path, XML_NAME),
        "hpxml_schedule_file": filter_schedules(home_path),
        "weather_file": WEATHER_FILE,
        "verbosity": 7,
        "seed": home_path,
        "Equipment": copy.deepcopy(EQUIPMENT_ARGS),
        **copy.deepcopy(DWELLING_ARGS),
    }
    with _dwelling_lock:
        return Dwelling(name=name, **args)


def make_unit(home_path, name):
    """Builds the dwelling and finds the device; homes without the device are skipped."""
    dwelling = make_dwelling(home_path, name)
    dev = get_device(dwelling)
    if dev is None:
        raise ValueError(f"no {LOAD_NAME} in this home")
    return {"name": os.path.basename(home_path), "path": home_path, "dwelling": dwelling, "dev": dev,
            "sim_times": list(dwelling.sim_times), "state_log": [], "info": {}}


def drop_first_day(df):
    if 'Time' not in df.columns:
        df = df.reset_index().rename(columns={'index': 'Time'})
    df['Time'] = pd.to_datetime(df['Time'])
    return df[df['Time'] >= START + pd.Timedelta(days=1)].copy()


def results_file(home_path, case):
    return os.path.join(home_path, "Results", f"{PIPELINE.lower()}_{case}.csv")

#########################################
# BASELINE RUN (every home, full simulation)
#########################################

def run_baseline(home_path):
    u = make_unit(home_path, f"{filename} Baseline")
    for t in u["sim_times"]:
        u["dwelling"].update(control_signal=control(u, "idle", t))
    df, _, _ = u["dwelling"].finalize()
    if 'Time' in df.columns:
        df = df.set_index('Time')
    df.index = pd.to_datetime(df.index)
    return df


def load_cached_baseline(home_path):
    """Reuses the baseline saved by an earlier run (set FLC_SKIP_BASELINE_SIM=1)."""
    cache = results_file(home_path, "baseline")
    if not os.path.exists(cache):
        return None
    return pd.read_csv(cache, parse_dates=['Time']).set_index('Time')

#########################################
# CONTROLLED RUN, PART 1: baseline control up to the event start
#########################################

def run_until_dispatch(home_path):
    u = make_unit(home_path, f"{filename} Controlled")
    for i, t in enumerate(u["sim_times"]):
        u["dwelling"].update(control_signal=control(u, "idle", t))
        u["state_log"].append((t, state_value(u, t)))
        if t >= dispatch_t:
            break
    u["resume_idx"] = i + 1
    return u

#########################################
# CONTROLLED RUN, PART 2: dispatch loop (all homes step together)
#########################################

def run_dispatch(units, base_kw):
    """Each step: check unit limits, pick units to dispatch or release, then step every home once."""
    units = {u["name"]: u for u in units}
    names = sorted(units)
    sim_times = units[names[0]]["sim_times"]
    resume_idx = units[names[0]]["resume_idx"]
    event_end_idx = next((i for i, t in enumerate(sim_times) if t >= event_end), len(sim_times))

    state = {n: "idle" for n in names}  # "idle" (baseline), "active" (reserve state), or "parked"
    ever_dispatched = {n: False for n in names}
    admission_order = {}
    last_dispatch_idx = {}
    release_idx = {}
    release_scheduled = False
    active_start = {}  # start time of each unit's current active episode
    respond_idx = {}   # step from which a dispatched unit follows its new command (response time)
    wait_state = {}    # state whose command the unit keeps until then
    episodes = []
    log_rows = []
    ramp_complete = False
    next_ramp_idx = resume_idx
    change_prev = 0.0

    def response_steps(n, i):
        """Random response time between RESPONSE_MIN_S and RESPONSE_MAX_S seconds, rounded up to whole steps."""
        if RESPONSE_MAX_S <= 0:
            return 0
        seconds = random.Random(f"{n}-{i}").uniform(RESPONSE_MIN_S, RESPONSE_MAX_S)
        return math.ceil(seconds / (60 * T_RES))

    def start(n, i):
        if not ever_dispatched[n]:
            ever_dispatched[n] = True
            admission_order[n] = len(admission_order)
        wait_state[n] = state[n]
        respond_idx[n] = i + response_steps(n, i)
        state[n] = "active"
        last_dispatch_idx[n] = i
        active_start[n] = sim_times[i]
        on_start(units[n], sim_times[i])

    def end(n, new_state, t, reason):
        state[n] = new_state
        episodes.append({"Home": n, "Command_Time": active_start.pop(n), "End_Time": t, "End_Reason": reason})

    for i in range(resume_idx, len(sim_times)):
        t = sim_times[i]
        in_event = t < event_end

        # 1) Active units that reached their limit are parked (during the event) or go idle (after).
        #    A unit is checked only after it has run at least one step on its new command.
        for n in names:
            if state[n] == "active" and i > respond_idx[n]:
                reason = limit_reached(units[n], t)
                if reason:
                    end(n, "parked" if in_event else "idle", t, reason)

        # 2) At the event end, schedule every unit still out (active or parked) to go back to idle in groups.
        if not release_scheduled and i >= event_end_idx:
            still_out = sorted([n for n in names if state[n] in ("active", "parked")], key=lambda n: admission_order[n])
            for k, n in enumerate(still_out):
                delay_min = (k // RELEASE_GROUP_SIZE) * RELEASE_GROUP_SPACING_MIN
                release_idx[n] = event_end_idx + round(delay_min / T_RES)
            release_scheduled = True

        # 3) Candidates: never-dispatched units that qualify right now, plus parked units that can be reused.
        fresh = [n for n in names if not ever_dispatched[n] and can_start_fresh(units[n], t, base_kw[n].get(t, 0.0))]
        recycled = [n for n in names if state[n] == "parked" and can_recycle(units[n], t)]
        candidates = sorted(fresh + recycled, key=lambda n: priority(units[n], t))

        active = [n for n in names if state[n] == "active"]
        pending = sum(1 for n in active if i - last_dispatch_idx[n] < RAMP_STEPS)
        pending_kw = pending * EXPECTED_KW_PER_UNIT

        if in_event and not ramp_complete and change_prev >= TARGET_KW - BAND_KW:
            ramp_complete = True

        # 4) Ramp: dispatch a batch of the best candidates every RAMP_BATCH_SPACING_MIN.
        if in_event and not ramp_complete:
            if i >= next_ramp_idx and candidates:
                shortfall = TARGET_KW - change_prev - pending_kw
                n_admit = min(RAMP_BATCH_SIZE, max(1, math.ceil(shortfall / EXPECTED_KW_PER_UNIT)), len(candidates))
                for n in candidates[:n_admit]:
                    start(n, i)
                next_ramp_idx = i + max(1, round(RAMP_BATCH_SPACING_MIN / T_RES))

        # 5) Hold, below the band: dispatch just enough of the best candidates to close the gap.
        elif in_event and change_prev < TARGET_KW - BAND_KW:
            shortfall = TARGET_KW - change_prev - pending_kw
            n_admit = min(max(0, math.ceil(shortfall / EXPECTED_KW_PER_UNIT)), len(candidates),
                          MAX_ADMISSIONS_SAFETY_CAP)
            for n in candidates[:n_admit]:
                start(n, i)

        # 6) Hold, above the band: park some active units.
        elif in_event and ALLOW_OVERSHOOT_RELEASE and change_prev > TARGET_KW + BAND_KW:
            n_release = max(1, math.ceil((change_prev - TARGET_KW - BAND_KW) / EXPECTED_KW_PER_UNIT))
            for n in sorted(active, key=lambda n: overshoot_priority(units[n], t))[:n_release]:
                end(n, "parked", t, "overshoot")

        # 7) Step every home forward one timestep with the command for its current state.
        for n in names:
            if state[n] in ("active", "parked") and n in release_idx and i >= release_idx[n]:
                if state[n] == "active":
                    end(n, "idle", t, "released")
                else:
                    state[n] = "idle"
            s = state[n]
            if s == "active" and i < respond_idx[n]:
                s = wait_state[n]  # still within its response time: keeps the previous command
            units[n]["dwelling"].update(control_signal=control(units[n], s, t))
            units[n]["state_log"].append((t, state_value(units[n], t)))

        # 8) Fleet change after this step, over every unit dispatched so far (positive when working).
        dispatched = [n for n in names if ever_dispatched[n]]
        delta_p = sum(units[n]["dev"].electric_kw - base_kw[n].get(t, 0.0) for n in dispatched)
        log_rows.append({
            "Time": t,
            "DeltaP_kW": delta_p,
            "Change_kW": SIGN * delta_p,
            "Dispatched_Count": len(dispatched),
            "Active_Count": sum(1 for n in names if state[n] == "active"),
            "Parked_Count": sum(1 for n in names if state[n] == "parked"),
            "Candidates_Available": len(candidates),
            "Total_Fleet_Power_kW": sum(units[n]["dev"].electric_kw for n in names),
        })
        change_prev = SIGN * delta_p

    # Close any active episode still open when the simulation ends.
    for n in list(active_start):
        end(n, "active", sim_times[-1] + pd.Timedelta(minutes=T_RES), "sim_end")

    return episodes, log_rows, sum(ever_dispatched.values())

#########################################
# FINALIZE EACH HOME
#########################################

def finalize_home(unit, df_base_full):
    """Collects the controlled results, checks they match baseline before the event, and saves both per home."""
    df_ctrl, _, _ = unit["dwelling"].finalize()
    df_ctrl = drop_first_day(df_ctrl)
    df_base = drop_first_day(df_base_full.reset_index())

    log = pd.DataFrame(unit["state_log"], columns=["Time", STATE_LOG_COL])
    log["Time"] = pd.to_datetime(log["Time"])
    df_ctrl = df_ctrl.merge(log, on="Time", how="left")

    # Same seed and same control before the event, so the power must be identical.
    pre_ctrl = df_ctrl[df_ctrl['Time'] < dispatch_t].set_index('Time')[POWER]
    pre_base = df_base.set_index('Time')[POWER].reindex(pre_ctrl.index)
    max_diff = (pre_ctrl - pre_base).abs().max() if len(pre_ctrl) else 0.0
    problem = f"{unit['name']}: max |diff| = {max_diff:.6f} kW" if max_diff > 1e-6 else None

    df_ctrl = df_ctrl[[c for c in SAVE_COLS + [STATE_LOG_COL] if c in df_ctrl.columns]]
    df_base = df_base[[c for c in SAVE_COLS if c in df_base.columns]]

    os.makedirs(os.path.join(unit["path"], "Results"), exist_ok=True)
    df_ctrl.to_csv(results_file(unit["path"], "controlled"), index=False)
    df_base.to_csv(results_file(unit["path"], "baseline"), index=False)

    df_ctrl["Home"] = unit["name"]
    df_base["Home"] = unit["name"]
    return df_ctrl, df_base, problem

#########################################
# MAIN
#########################################

#########################################
# CONSOLE: one progress bar and nothing else; Ctrl+C quits at once
#########################################

import sys
import signal
import warnings
from tqdm import tqdm
from ochre.Simulator import Simulator

warnings.filterwarnings("ignore")
Simulator.print = lambda self, *msg: None               # OCHRE status lines and warnings
signal.signal(signal.SIGINT, lambda *a: os._exit(130))  # quit without waiting for worker threads
STEPS = int(dt.timedelta(days=DURATION_DAYS) / dt.timedelta(minutes=T_RES))  # time steps in one home run
BAR = tqdm(total=0, desc="Simulation", bar_format="{l_bar}{bar}| {elapsed}<{remaining}", file=sys.__stderr__,
           ncols=min(80, shutil.get_terminal_size().columns - 1), mininterval=1)  # narrower than the window, so it never wraps


class _Console:
    """Only this script's prints are shown (above the bar); OCHRE's prints and 'Baseline skipped' lines are dropped."""
    def write(self, text):
        from_script = sys._getframe(1).f_globals.get("__name__") == "__main__"
        if from_script and text.strip() and not text.startswith("Baseline skipped"):
            tqdm.write(text.rstrip("\n"), file=sys.__stderr__)  # same stream as the bar, so it redraws below

    def flush(self):
        sys.__stdout__.flush()


def _as_completed(futures):
    """concurrent.futures.as_completed that wakes every 0.5 s, so Windows sees Ctrl+C."""
    pending = set(futures)
    while pending:
        done, pending = concurrent.futures.wait(pending, timeout=0.5, return_when=concurrent.futures.FIRST_COMPLETED)
        yield from done


# Progress counts home time steps: each home has a baseline run and a controlled run of STEPS each.
_update, _find_homes, _load_cached = Dwelling.update, find_homes, load_cached_baseline
_run_baseline, _finalize_home = run_baseline, finalize_home


def _update_counted(self, *args, **kwargs):
    if "control_signal" in kwargs:  # our steps only, not OCHRE's initialization
        BAR.update(1)
    return _update(self, *args, **kwargs)


def find_homes(base_dir):
    homes = _find_homes(base_dir)
    BAR.total = 2 * STEPS * len(homes)
    return homes


def load_cached_baseline(home_path):
    df = _load_cached(home_path)
    if df is not None:
        BAR.update(STEPS)  # a reused baseline counts as done
    return df


def run_baseline(home_path):
    try:
        return _run_baseline(home_path)
    except Exception:
        BAR.update(2 * STEPS)  # a skipped home has no baseline or controlled run
        raise


def finalize_home(*args):
    BAR.n = BAR.total  # all simulation steps are done once finalizing starts
    BAR.close()
    return _finalize_home(*args)


Dwelling.update = _update_counted
concurrent.futures.as_completed = _as_completed
sys.stdout = _Console()


if __name__ == "__main__":
    save_filename(filename, pipeline=PIPELINE)
    save_input_folder(INPUT_FOLDER, pipeline=PIPELINE)
    os.makedirs(RESULTS_DIR, exist_ok=True)
    if not os.path.exists(WEATHER_FILE):
        os.makedirs(os.path.dirname(WEATHER_FILE), exist_ok=True)
        shutil.copy(Path(ochre.__file__).parent / "defaults" / "Weather" / os.path.basename(WEATHER_FILE), WEATHER_FILE)

    homes = find_homes(INPUT_DIR)
    if MAX_HOMES:
        homes = homes[:MAX_HOMES]
    print(f"Found {len(homes)} homes in {INPUT_DIR}")
    print(f"{LOAD_NAME} {PIPELINE} level {LEVEL}: target {TARGET_KW} +/- {BAND_KW} kW, event {dispatch_t} to {event_end}")

    # Baseline: reuse saved results if FLC_SKIP_BASELINE_SIM=1, otherwise simulate.
    baselines = {}
    if os.environ.get('FLC_SKIP_BASELINE_SIM', '0') == '1':
        for h in homes:
            cached = load_cached_baseline(h)
            if cached is not None:
                baselines[h] = cached
        print(f"Reused cached baseline for {len(baselines)}/{len(homes)} homes.")
    to_simulate = [h for h in homes if h not in baselines]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(run_baseline, h): h for h in to_simulate}
        for f in concurrent.futures.as_completed(futures):
            try:
                baselines[futures[f]] = f.result()
            except Exception as e:
                print(f"Baseline skipped for {os.path.basename(futures[f])}: {e}")
    base_kw = {os.path.basename(h): df[POWER].to_dict() for h, df in baselines.items()}
    print(f"Baseline ready for {len(baselines)} homes.")

    # Controlled: every home runs baseline control up to the event start.
    units = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(run_until_dispatch, h): h for h in homes if h in baselines}
        for f in concurrent.futures.as_completed(futures):
            try:
                units.append(f.result())
            except Exception as e:
                print(f"Controlled run failed for {os.path.basename(futures[f])}: {e}")

    print(f"Starting dispatch for {len(units)} homes...")
    episodes, log_rows, n_dispatched = run_dispatch(units, base_kw)
    print(f"Dispatch complete: {n_dispatched}/{len(units)} homes dispatched.")

    # Finalize every home and stop if any home fails the before-event check.
    all_ctrl, all_base, problems = [], [], []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(finalize_home, u, baselines[u["path"]]): u["name"] for u in units}
        for f in concurrent.futures.as_completed(futures):
            try:
                df_ctrl, df_base, problem = f.result()
                all_ctrl.append(df_ctrl)
                all_base.append(df_base)
                if problem:
                    problems.append(problem)
            except Exception as e:
                print(f"Finalize failed for {futures[f]}: {e}")
    if problems:
        print("Controlled and baseline power differ before the event:\n  " + "\n  ".join(problems))
        raise RuntimeError(f"{len(problems)} home(s) failed the before-event check; no results written.")

    # Save the run's files.
    config = {**reserve_event, "load": LOAD_NAME, "level": LEVEL, "dispatch_start": str(dispatch_t),
              "event_end": str(event_end), "target_kw": TARGET_KW, "band_kw": BAND_KW,
              "response_s": [RESPONSE_MIN_S, RESPONSE_MAX_S], **CONFIG_EXTRA,
              "input_folder": INPUT_FOLDER, "n_homes": len(units)}
    with open(os.path.join(RESULTS_DIR, filename + "_config.json"), "w") as f:
        json.dump(config, f, indent=2)
    pd.concat(all_ctrl).sort_values(["Home", "Time"]).to_csv(
        os.path.join(RESULTS_DIR, filename + "_controlled.csv"), index=False)
    pd.concat(all_base).sort_values(["Home", "Time"]).to_csv(
        os.path.join(RESULTS_DIR, filename + "_baseline.csv"), index=False)
    pd.DataFrame(log_rows).to_csv(os.path.join(RESULTS_DIR, filename + "_log.csv"), index=False)
    pd.DataFrame(episodes, columns=["Home", "Command_Time", "End_Time", "End_Reason"]).sort_values(
        ["Home", "Command_Time"]).to_csv(os.path.join(RESULTS_DIR, filename + "_episodes.csv"), index=False)

    # Short console summary (the results script makes the full one).
    log_df = pd.DataFrame(log_rows)
    ev = log_df[(log_df['Time'] >= dispatch_t) & (log_df['Time'] < event_end)]['Change_kW']
    print(f"Fleet change during event: mean {ev.mean():.2f}, min {ev.min():.2f}, max {ev.max():.2f} kW")
    print(f"Results saved to {RESULTS_DIR}")
