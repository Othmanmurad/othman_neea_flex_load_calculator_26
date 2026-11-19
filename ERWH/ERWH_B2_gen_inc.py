# -*- coding: utf-8 -*-
"""
FLC INC (shed) test: electric resistance water heaters lower their setpoint (SHED, CP or GE) to hold the
fleet's load reduction, reduction = baseline - controlled, at a target during the event.
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
filename = os.environ.get('OCHRE_FILENAME', 'FLC_ERWH_INC_Test_100kW-Sh')
PIPELINE = "FLC_ERWH_INC"
LOAD_NAME = "ERWH"

# "50 Input Files" (109 homes) or "All Input Files" (587 homes).
INPUT_FOLDER = "All Input Files"
MAX_HOMES = int(os.environ.get('FLC_MAX_HOMES', 0))  # 0 = every home; a small number for a quick test

# Day 1 is warm-up, the event happens on day 2.
START = dt.datetime(2018, 1, 11, 0, 0)
DURATION_DAYS = 2
T_RES = int(os.environ.get('OCHRE_TIMESTEP_MIN', 1))  # minutes

# Event start time, length, and the reduction target (kW, positive) with its tolerance band.
if 'reserve_event' not in globals():
    reserve_event = {
        'dispatch_time': os.environ.get('OCHRE_DISPATCH_TIME', '14:00'),
        'duration': float(os.environ.get('OCHRE_DURATION_HR', 2.0)),
    }
# Pick a target the fleet can hold: run once with a very large target (e.g. 1000) to see the maximum
# reduction, then use ~70% of it. The band should be at least half an element (5.5 kW / 2).
TARGET_KW = float(os.environ.get('OCHRE_TARGET_REDUCTION_KW', 100.0))
BAND_KW = float(os.environ.get('OCHRE_BAND_KW', 2.75))
# The controller aims at target + this margin: new units are dispatched whenever the averaged reduction
# drops below it (e.g. 55 kW for a 50 kW target), so the reduction stays above the target.
DISPATCH_MARGIN_KW = float(os.environ.get('OCHRE_DISPATCH_MARGIN_KW', 5.0))

# Shape of the requested reduction inside the event: ramp down -> hold -> ramp up.
RAMP_DOWN_MIN = float(os.environ.get('OCHRE_RAMP_DOWN_MIN', 10))  # 0 -> target, right after dispatch
# Ramp up = release after the event end: no time limit, units go back gradually (see RELEASE_GROUP_*).
SIGN = -1  # fleet change = SIGN * (controlled - baseline), positive when the fleet is doing its job

# Control level sent to dispatched units: "SHED", "CP" or "GE".
LEVEL = os.environ.get('OCHRE_LEVEL', 'SHED')

# Dispatch settings (same logic as the HPWH tests).
EXPECTED_KW_PER_UNIT = 5.5  # change per unit used to size batches until enough units are active to measure it
MIN_KW_PER_UNIT = 1.0       # floor on the measured change per active unit, so batches do not overshoot
PRE_HEAT_MARGIN_F = 3       # idle units this close (F) above their baseline turn-on temperature can also be dispatched
INTEGRAL_CAP = 2.0          # the integral term is capped at this multiple of the target
RAMP_STEPS = max(1, round(3 / T_RES))  # a new unit counts as "pending" for this many steps
MAX_ADMISSIONS_SAFETY_CAP = 20   # max units dispatched per step
ALLOW_OVERSHOOT_RELEASE = False  # parked units reheat at baseline inside the event, so above the aim just stop adding
# After the event, units still held go back to idle in groups (as in HPWH), coldest tank first.
# HPWH uses 20 every 15 min; ERWH units are 5.5 kW each, so smaller groups keep each step small.
RELEASE_GROUP_SIZE = int(os.environ.get('OCHRE_RELEASE_GROUP_SIZE', 10))
RELEASE_GROUP_SPACING_MIN = float(os.environ.get('OCHRE_RELEASE_SPACING_MIN', 10))
# The dispatcher compares a rolling average of the fleet change over this many minutes to the target
# (ERWH bursts are short, so 1-minute values are noisy). 1 = no averaging. Short = less lag.
CONTROL_AVG_MIN = 5
# Integral term: a dispatched ERWH only helps while its own heating cycle would have run (a few min), so
# gap-only dispatch settles at about half the target. The accumulated shortfall adds extra units (kW per kW-min).
KI_PER_MIN = float(os.environ.get('OCHRE_KI_PER_MIN', 0.3))

#########################################
# DERIVED VALUES (same in every flc_gen file)
#########################################

INPUT_DIR = os.path.join(WORKING_DIR, INPUT_FOLDER, "bldg")
RESULTS_DIR = os.path.join(WORKING_DIR, "Ready_data", filename)
DT = dt.timedelta(minutes=T_RES)

EVENT_DATE = (START + dt.timedelta(days=DURATION_DAYS - 1)).date()
dispatch_t = pd.to_datetime(f"{EVENT_DATE} {reserve_event['dispatch_time']}")
event_end = dispatch_t + pd.Timedelta(hours=reserve_event['duration'])
ramp_up_start = event_end  # the ramp up (gradual release) starts when the event ends
assert RAMP_DOWN_MIN < reserve_event['duration'] * 60, "Ramp down must be shorter than the event."


def target_at(t):
    """Requested reduction (kW) at time t: ramp down after dispatch, then hold to the event end."""
    if t < dispatch_t or t >= event_end:
        return 0.0
    return TARGET_KW * (1.0 if RAMP_DOWN_MIN <= 0 else min(1.0, (t - dispatch_t) / pd.Timedelta(minutes=RAMP_DOWN_MIN)))


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

POWER = "Water Heating Electric Power (kW)"
STATE_LOG_COL = "Tank Upper Temperature (C)"
SAVE_COLS = ["Time", "Total Electric Power (kW)", POWER, "Hot Water Average Temperature (C)",
             "Hot Water Outlet Temperature (C)", "Temperature - Indoor (C)"]
HOME_IDS = None  # every home; ERWH_A3_adjustXML.py converted each home.xml water heater to an ERWH
ON_KW = 0.1      # a unit counts as heating when its power is above this

# WH control parameters (F)
# GE - Grid Emergency, CP - Critical Peak, Shed
WH_Tcontrol_GEF = 90
WH_Tcontrol_GEdeadbandF = 10
WH_Tcontrol_CPF = 110
WH_Tcontrol_CPdeadbandF = 10
WH_Tcontrol_SHEDF = 120
WH_Tcontrol_deadbandF = 10
# ALU - Advanced load up, LOAD - Load up
WH_Tcontrol_ALUF = 140
WH_Tcontrol_ALUdeadbandF = 2
WH_Tcontrol_LOADF = 130
WH_Tcontrol_LOADdeadbandF = 2

WH_TbaselineF = 130
WH_TdeadbandF = 7
WH_Tinit = 130

# Response time in seconds
WH_RESPONSE_MIN = 30
WH_RESPONSE_MAX = 90

# (setpoint F, deadband F) for each level.
LEVELS = {"GE": (WH_Tcontrol_GEF, WH_Tcontrol_GEdeadbandF),
          "CP": (WH_Tcontrol_CPF, WH_Tcontrol_CPdeadbandF),
          "SHED": (WH_Tcontrol_SHEDF, WH_Tcontrol_deadbandF),
          "ALU": (WH_Tcontrol_ALUF, WH_Tcontrol_ALUdeadbandF),
          "LOAD": (WH_Tcontrol_LOADF, WH_Tcontrol_LOADdeadbandF)}
LEVEL_SETPOINT_F, LEVEL_DEADBAND_F = LEVELS[LEVEL]
RESPONSE_MIN_S = WH_RESPONSE_MIN
RESPONSE_MAX_S = WH_RESPONSE_MAX
MAX_TANK_TEMP_C = 60  # equipment limit (140 F)

# 12-node electric resistance tank (home.xml gives the 5.5 kW elements, 80 gal tank, EF and RE).
EQUIPMENT_ARGS = {"Electric Resistance Water Heater": {
    "Initial Temperature (C)": f_to_c(WH_Tinit),
    "water_nodes": 12,
    "Max Tank Temperature (C)": MAX_TANK_TEMP_C,
}}
DWELLING_ARGS = {}


def setpoint_ctrl(setpoint_f, deadband_f):
    return {"Water Heating": {"Setpoint": f_to_c(setpoint_f), "Deadband": f_to_c_delta(deadband_f),
                              "Load Fraction": 1}}


BASELINE_CTRL = setpoint_ctrl(WH_TbaselineF, WH_TdeadbandF)
LEVEL_CTRL = setpoint_ctrl(LEVEL_SETPOINT_F, LEVEL_DEADBAND_F)
CONFIG_EXTRA = {"level_setpoint_f": LEVEL_SETPOINT_F, "level_deadband_f": LEVEL_DEADBAND_F,
                "baseline_setpoint_f": WH_TbaselineF, "baseline_deadband_f": WH_TdeadbandF,
                "control_avg_min": CONTROL_AVG_MIN, "min_kw_per_unit": MIN_KW_PER_UNIT,
                "pre_heat_margin_f": PRE_HEAT_MARGIN_F, "integral_cap": INTEGRAL_CAP,
                "overshoot_release": ALLOW_OVERSHOOT_RELEASE}


#########################################
# DEVICE RULES (how a unit is controlled, dispatched and released)
#########################################

def get_device(dwelling):
    return dwelling.get_equipment_by_end_use("Water Heating")


def upper_temp(u):
    return u["dev"].model.states[u["dev"].t_upper_idx]


def coldest_temp(u):
    """The colder of the two element thermostat temperatures (lower one averaged like OCHRE does)."""
    dev = u["dev"]
    lower = (dev.model.states[dev.t_lower_idx] + dev.model.states[dev.t_lower_idx - 1]) / 2
    return min(upper_temp(u), lower)


def running(u):
    return u["dev"].electric_kw > ON_KW


def state_value(u, t):
    return upper_temp(u)


def on_start(u, t):
    pass

# INC: SHED/CP/GE = lowered setpoint. With HOLD_UNTIL_EVENT_END, a unit stays on that setpoint until it is
# released (it reheats only up to the lowered setpoint); otherwise it goes back to baseline once it turns on.
assert LEVEL in ("SHED", "CP", "GE"), "INC level must be SHED, CP or GE"
HOLD_UNTIL_EVENT_END = True


def control(u, state, t):
    return LEVEL_CTRL if state == "active" else BASELINE_CTRL


def limit_reached(u, t):
    if HOLD_UNTIL_EVENT_END:
        return None  # held: leaves only on overshoot or a ramp-up release
    return "resumed" if running(u) else None


def can_start_fresh(u, t, base_p):
    # Heating in the baseline now, or idle but about to turn on (dispatching it saves its whole heating cycle).
    return base_p > ON_KW or coldest_temp(u) < f_to_c(WH_TbaselineF - WH_TdeadbandF + PRE_HEAT_MARGIN_F)


def can_recycle(u, t):
    return True  # same as HPWH: any parked unit can go back to SHED


def priority(u, t):
    return (not running(u), -upper_temp(u))  # units heating now first, then warmest upper tank temperature


def overshoot_priority(u, t):
    # Warmest first: a warm tank stays off under the baseline setpoint, so releasing it does not add
    # a 5.5 kW element right away (releasing the coldest made the fleet jump up by 5.5 kW per unit).
    return -coldest_temp(u)


def release_priority(u, t):
    return coldest_temp(u)  # coldest tank first (ramp up and after the event)

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
        "seed": os.path.basename(home_path),  # folder name, so the drive-letter case ('C:' vs 'c:') cannot change it
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

    state = {n: "idle" for n in names}  # "idle" (baseline), "active" (reserve state), or "parked"
    ever_dispatched = {n: False for n in names}
    last_dispatch_idx = {}
    active_start = {}  # start time of each unit's current active episode
    respond_idx = {}   # step from which a dispatched unit follows its new command (response time)
    wait_state = {}    # state whose command the unit keeps until then
    episodes = []
    log_rows = []
    change_prev = 0.0
    change_hist = []   # fleet change of recent steps, for the rolling average
    integ = 0.0        # integral of the shortfall (kW), see KI_PER_MIN
    release_idx = {}   # step at which each unit still held at the event end is released
    release_scheduled = False

    def response_steps(n, i):
        """Random response time between RESPONSE_MIN_S and RESPONSE_MAX_S seconds, rounded up to whole steps."""
        if RESPONSE_MAX_S <= 0:
            return 0
        seconds = random.Random(f"{n}-{i}").uniform(RESPONSE_MIN_S, RESPONSE_MAX_S)
        return math.ceil(seconds / (60 * T_RES))

    def start(n, i):
        ever_dispatched[n] = True
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
        in_event = t < event_end     # ramp down + hold: units can be dispatched or parked
        tgt = target_at(t)           # requested reduction now (ramp down -> hold -> ramp up)
        aim = tgt + DISPATCH_MARGIN_KW if in_event and t >= dispatch_t else 0.0  # what the dispatcher tracks

        # 1) Active units that reached their limit are parked (during the event) or go idle (after).
        #    A unit is checked only after it has run at least one step on its new command.
        for n in names:
            if state[n] == "active" and i > respond_idx[n]:
                reason = limit_reached(units[n], t)
                if reason:
                    end(n, "parked" if in_event else "idle", t, reason)

        # 2) Ramp up = after the event, no time limit. Parked units go idle; the units still held are
        #    released in groups (HPWH style), coldest first, so they do not all reheat at once.
        if not in_event:
            for n in names:
                if state[n] == "parked":
                    state[n] = "idle"
            still_active = [n for n in names if state[n] == "active"]
            if not release_scheduled:
                for k, n in enumerate(sorted(still_active, key=lambda n: release_priority(units[n], t))):
                    delay_min = (k // RELEASE_GROUP_SIZE) * RELEASE_GROUP_SPACING_MIN
                    release_idx[n] = i + round(delay_min / T_RES)
                release_scheduled = True
            for n in still_active:
                if i >= release_idx.get(n, i):
                    end(n, "idle", t, "released")

        # 3) Candidates: never-dispatched units that qualify right now, plus parked units that can be reused.
        fresh = [n for n in names if not ever_dispatched[n] and can_start_fresh(units[n], t, base_kw[n].get(t, 0.0))]
        recycled = [n for n in names if state[n] == "parked" and can_recycle(units[n], t)]
        candidates = sorted(fresh + recycled, key=lambda n: priority(units[n], t))

        active = [n for n in names if state[n] == "active"]
        pending = sum(1 for n in active if i - last_dispatch_idx[n] < RAMP_STEPS)
        # Measured change per active unit (a held unit only helps while its baseline would heat), floored.
        kw_per_unit = (min(EXPECTED_KW_PER_UNIT, max(MIN_KW_PER_UNIT, change_prev / len(active)))
                       if len(active) >= 10 else EXPECTED_KW_PER_UNIT)
        pending_kw = pending * kw_per_unit

        # 4) Ramp down and hold: when the reduction is below the aim (target + margin),
        #    dispatch just enough of the best candidates to close the gap, plus the integral term.
        if in_event and t >= dispatch_t and change_prev < aim:
            shortfall = aim - change_prev + integ - pending_kw
            n_admit = min(max(0, math.ceil(shortfall / kw_per_unit)), len(candidates),
                          MAX_ADMISSIONS_SAFETY_CAP)
            for n in candidates[:n_admit]:
                start(n, i)

        # 5) Hold, above the aim + band: park some active units (warmest first, see overshoot_priority).
        elif in_event and ALLOW_OVERSHOOT_RELEASE and change_prev > aim + BAND_KW:
            n_release = max(1, math.ceil((change_prev - aim - BAND_KW) / EXPECTED_KW_PER_UNIT))
            for n in sorted(active, key=lambda n: overshoot_priority(units[n], t))[:n_release]:
                end(n, "parked", t, "overshoot")

        # 6) Step every home forward one timestep with the command for its current state.
        for n in names:
            s = state[n]
            if s == "active" and i < respond_idx[n]:
                s = wait_state[n]  # still within its response time: keeps the previous command
            units[n]["dwelling"].update(control_signal=control(units[n], s, t))
            units[n]["state_log"].append((t, state_value(units[n], t)))

        # 7) Fleet change after this step, over every unit dispatched so far (positive when working).
        dispatched = [n for n in names if ever_dispatched[n]]
        delta_p = sum(units[n]["dev"].electric_kw - base_kw[n].get(t, 0.0) for n in dispatched)
        change_hist = (change_hist + [SIGN * delta_p])[-max(1, round(CONTROL_AVG_MIN / T_RES)):]
        change_prev = sum(change_hist) / len(change_hist)  # what the dispatcher compares to the target
        if in_event and t >= dispatch_t:  # integral of the shortfall to the aim, kept between 0 and INTEGRAL_CAP x target
            integ = min(max(integ + KI_PER_MIN * T_RES * (aim - change_prev), 0.0), INTEGRAL_CAP * TARGET_KW)
        log_rows.append({
            "Time": t,
            "Target_kW": tgt,
            "DeltaP_kW": delta_p,
            "Change_kW": SIGN * delta_p,
            "Change_Avg_kW": change_prev,
            "Integral_kW": integ,
            "Dispatched_Count": len(dispatched),
            "Active_Count": sum(1 for n in names if state[n] == "active"),
            "Parked_Count": sum(1 for n in names if state[n] == "parked"),
            "Candidates_Available": len(candidates),
            "Total_Fleet_Power_kW": sum(units[n]["dev"].electric_kw for n in names),
        })

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
    print(f"{LOAD_NAME} {PIPELINE} level {LEVEL}: target {TARGET_KW} +/- {BAND_KW} kW, event {dispatch_t} to {event_end}"
          f" (ramp down {RAMP_DOWN_MIN:.0f} min, release from {ramp_up_start:%H:%M})")

    # Baseline: reuse saved results if FLC_SKIP_BASELINE_SIM=1, otherwise simulate.
    baselines = {}
    if os.environ.get('FLC_SKIP_BASELINE_SIM', '1') == '1':
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
              "ramp_down_min": RAMP_DOWN_MIN, "ramp_up_start": str(ramp_up_start),
              "ki_per_min": KI_PER_MIN, "dispatch_margin_kw": DISPATCH_MARGIN_KW, "release_group_size": RELEASE_GROUP_SIZE, "release_group_spacing_min": RELEASE_GROUP_SPACING_MIN,
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
