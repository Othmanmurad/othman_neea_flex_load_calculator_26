# -*- coding: utf-8 -*-
"""
FLC INC (shed) test: HPWH units lower their setpoint (SHED) to hold the fleet's load
reduction, reduction = baseline - controlled, at a target during the event.
"""

import os
import json
import math
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
# PATHS
#########################################

WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
WEATHER_FILE = os.path.join(WORKING_DIR, "Weather", "USA_OR_Portland.Intl.AP.726980_TMY3.epw")
XML_NAME = "home.xml"
SCHEDULE_NAME = "in.schedules.csv"

#########################################
# USER SETTINGS
#########################################

# Test name; each test gets its own folder in Ready_data.
filename = os.environ.get('OCHRE_FILENAME', 'FLC_INC_Test')
PIPELINE = "FLC_INC"

#"HPWH 50 Input Files", "HPWH 66 Input Files/bldg", "HPWH 80 Input Files", "HPWH All Input Files/bldg"
INPUT_FOLDER = "HPWH All Input Files"

# Day 1 is warm-up, the event happens on day 2.
START = dt.datetime(2018, 1, 11, 0, 0)
DURATION_DAYS = 2
T_RES = int(os.environ.get('OCHRE_TIMESTEP_MIN', 1))  # minutes

# Event start time, length, and the reduction target (kW, positive) with its tolerance band.
if 'reserve_event' not in globals():
    reserve_event = {
        'dispatch_time': os.environ.get('OCHRE_DISPATCH_TIME', '14:00'),
        'duration': float(os.environ.get('OCHRE_DURATION_HR', 4.0)),
    }
TARGET_KW = float(os.environ.get('OCHRE_TARGET_REDUCTION_KW', 500.0))
BAND_KW = float(os.environ.get('OCHRE_BAND_KW', 0.5))

# Control level sent to dispatched units: "SHED", "CP" (Critical Peak) or "GE" (Grid Emergency).
LEVEL = os.environ.get('OCHRE_LEVEL', 'SHED')

# Dispatch settings (same values as the IEEE study).
ACTIVE_THRESHOLD_KW = 0.1        # a new unit is only dispatched if it is heating in its baseline right now
EXPECTED_KW_PER_UNIT = 0.45      # assumed reduced kW per unit, used to size each batch
RAMP_STEPS = max(1, round(3 / T_RES))  # a new unit counts as "pending" for this many steps
RAMP_BATCH_SPACING_MIN = 1       # ramp-down batches have no size cap, only this spacing
MAX_ADMISSIONS_SAFETY_CAP = 20   # max units dispatched per step after the ramp-down
ALLOW_OVERSHOOT_RELEASE = True   # above the band, move the coldest SHED units to RECOVERY
RELEASE_GROUP_SIZE = 20          # after the event, units go back to idle in groups of this size
RELEASE_GROUP_SPACING_MIN = 15   # minutes between release groups

#########################################
# DERIVED VALUES
#########################################

INPUT_DIR = os.path.join(WORKING_DIR, INPUT_FOLDER, "bldg")
RESULTS_DIR = os.path.join(WORKING_DIR, "Ready_data", filename)

EVENT_DATE = (START + dt.timedelta(days=DURATION_DAYS - 1)).date()
dispatch_t = pd.to_datetime(f"{EVENT_DATE} {reserve_event['dispatch_time']}")
event_end = dispatch_t + pd.Timedelta(hours=reserve_event['duration'])


def f_to_c(temp_f):
    return (temp_f - 32) * 5 / 9


def f_to_c_delta(delta_f):
    return delta_f * 5 / 9

# OCHRE seeds the global random generator while building a Dwelling, so only one is built at a time.
_dwelling_lock = threading.Lock()

#########################################
# DEVICE SETTINGS
#########################################

WH_POWER = "Water Heating Electric Power (kW)"

# Tank temperatures (F): baseline setpoint/deadband, initial.
BASELINE_SETPOINT_F = 130
BASELINE_DEADBAND_F = 7
INIT_TEMP_F = 128

# (setpoint F, deadband F) for each INC level.
LEVELS = {"SHED": (120, 10), "CP": (110, 10), "GE": (90, 10)}
SHED_SETPOINT_F, SHED_DEADBAND_F = LEVELS[LEVEL]

# A dispatched unit leaves its level once its upper tank temperature drops to the bottom of the deadband (C);
# it then reheats (RECOVERY).
MIN_TANK_TEMP_C = f_to_c(SHED_SETPOINT_F - SHED_DEADBAND_F)
MAX_TANK_TEMP_C = 60             # equipment limit, same as the DEC test
HP_DUTY_CYCLE_RECOVERY = 1.0

BASELINE_CTRL = {"Water Heating": {"Setpoint": f_to_c(BASELINE_SETPOINT_F),
                                   "Deadband": f_to_c_delta(BASELINE_DEADBAND_F), "Load Fraction": 1}}
SHED_CTRL = {"Water Heating": {"Setpoint": f_to_c(SHED_SETPOINT_F),
                               "Deadband": f_to_c_delta(SHED_DEADBAND_F), "Load Fraction": 1}}
RECOVERY_CTRL = {"Water Heating": {"HP Duty Cycle": HP_DUTY_CYCLE_RECOVERY, "Load Fraction": 1}}

#########################################
# HELPERS
#########################################

def find_homes(base_dir):
    """Every folder that has both a home.xml and an in.schedules.csv."""
    homes = []
    for item in sorted(os.listdir(base_dir)):
        path = os.path.join(base_dir, item)
        if os.path.isfile(os.path.join(path, XML_NAME)) and os.path.isfile(os.path.join(path, SCHEDULE_NAME)):
            homes.append(path)
    return homes


def filter_schedules(home_path):
    """Writes a copy of the schedule file with only the columns OCHRE understands."""
    df = pd.read_csv(os.path.join(home_path, SCHEDULE_NAME))
    df = df[[c for c in df.columns if c in ALL_SCHEDULE_NAMES]]
    out_file = os.path.join(home_path, 'filtered_schedules.csv')
    df.to_csv(out_file, index=False)
    return out_file


def make_dwelling(home_path, name):
    args = {
        "start_time": START,
        "time_res": dt.timedelta(minutes=T_RES),
        "duration": dt.timedelta(days=DURATION_DAYS),
        "hpxml_file": os.path.join(home_path, XML_NAME),
        "hpxml_schedule_file": filter_schedules(home_path),
        "weather_file": WEATHER_FILE,
        "verbosity": 7,
        "seed": home_path,
        "Equipment": {
            "Water Heating": {
                "Initial Temperature (C)": f_to_c(INIT_TEMP_F),
                "hp_only_mode": True,
                "Max Tank Temperature (C)": MAX_TANK_TEMP_C,
                "Upper Node": 3,
                "Lower Node": 10,
                "Upper Node Weight": 0.75,
                "ext_time_res": dt.timedelta(minutes=T_RES),  # required for 'HP Duty Cycle' control
            },
        },
    }
    with _dwelling_lock:
        return Dwelling(name=name, **args)


def avg_tank_temp_c(wh):
    return wh.model.states.dot(wh.model.vol_fractions)


def upper_tank_temp_c(wh):
    return wh.model.states[wh.t_upper_idx]


def drop_first_day(df):
    if 'Time' not in df.columns:
        df = df.reset_index().rename(columns={'index': 'Time'})
    df['Time'] = pd.to_datetime(df['Time'])
    return df[df['Time'] >= START + pd.Timedelta(days=1)].copy()

#########################################
# BASELINE RUN (every home, full simulation)
#########################################

def run_baseline(home_path):
    dwelling = make_dwelling(home_path, f"{filename} Baseline")
    for _ in dwelling.sim_times:
        dwelling.update(control_signal=BASELINE_CTRL)
    df, _, _ = dwelling.finalize()
    if 'Time' in df.columns:
        df = df.set_index('Time')
    df.index = pd.to_datetime(df.index)
    return df


def load_cached_baseline(home_path):
    """Reuses the baseline saved by an earlier run (set FLC_SKIP_BASELINE_SIM=1)."""
    cache = os.path.join(home_path, "Results", "hpwh_baseline.csv")
    if not os.path.exists(cache):
        return None
    df = pd.read_csv(cache, parse_dates=['Time']).set_index('Time')
    return df

#########################################
# CONTROLLED RUN, PART 1: baseline control up to the event start
#########################################

def run_until_dispatch(home_path):
    dwelling = make_dwelling(home_path, f"{filename} Controlled")
    wh = dwelling.get_equipment_by_end_use('Water Heating')
    sim_times = list(dwelling.sim_times)
    temp_log = []
    for i, t in enumerate(sim_times):
        dwelling.update(control_signal=BASELINE_CTRL)
        temp_log.append((t, avg_tank_temp_c(wh)))
        if t >= dispatch_t:
            break
    return {"name": os.path.basename(home_path), "path": home_path, "dwelling": dwelling, "wh": wh,
            "sim_times": sim_times, "resume_idx": i + 1, "temp_log": temp_log}

#########################################
# CONTROLLED RUN, PART 2: dispatch loop (all homes step together)
#########################################

def run_dispatch(units, base_kw):
    """Each step: check tank limits, pick units to dispatch or release, then step every home once."""
    units = {u["name"]: u for u in units}
    names = sorted(units)
    sim_times = units[names[0]]["sim_times"]
    resume_idx = units[names[0]]["resume_idx"]
    event_end_idx = next((i for i, t in enumerate(sim_times) if t >= event_end), len(sim_times))

    upper_temp = {n: upper_tank_temp_c(units[n]["wh"]) for n in names}
    state = {n: "idle" for n in names}  # "idle" (baseline), "shed" (lowered setpoint), or "recovery" (forced reheat)
    ever_dispatched = {n: False for n in names}
    admission_order = {}
    last_dispatch_idx = {}
    release_idx = {}
    release_scheduled = False
    shed_start = {}  # start time of each unit's current SHED episode
    episodes = []
    log_rows = []
    ramp_complete = False
    next_ramp_idx = resume_idx
    reduction_prev = 0.0

    def start_shed(n, i):
        if not ever_dispatched[n]:
            ever_dispatched[n] = True
            admission_order[n] = len(admission_order)
        state[n] = "shed"
        last_dispatch_idx[n] = i
        shed_start[n] = sim_times[i]

    def end_shed(n, new_state, t, reason):
        state[n] = new_state
        episodes.append({"Home": n, "Command_Time": shed_start.pop(n), "End_Time": t, "End_Reason": reason})

    def warmest_first(candidates):
        return sorted(candidates, key=lambda n: upper_temp[n], reverse=True)

    for i in range(resume_idx, len(sim_times)):
        t = sim_times[i]
        in_event = t < event_end

        # 1) SHED units that cooled to the min tank temperature go to RECOVERY (during the event) or idle (after).
        for n in names:
            if state[n] == "shed" and upper_temp[n] <= MIN_TANK_TEMP_C:
                end_shed(n, "recovery" if in_event else "idle", t, "min_temp")

        # 2) At the event end, schedule every unit still out (SHED or RECOVERY) to go back to idle in groups.
        if not release_scheduled and i >= event_end_idx:
            still_out = sorted([n for n in names if state[n] in ("shed", "recovery")],
                               key=lambda n: admission_order[n])
            for k, n in enumerate(still_out):
                delay_min = (k // RELEASE_GROUP_SIZE) * RELEASE_GROUP_SPACING_MIN
                release_idx[n] = event_end_idx + round(delay_min / T_RES)
            release_scheduled = True

        # 3) Candidates: never-dispatched units heating in their baseline right now, plus RECOVERY units.
        fresh = [n for n in names if not ever_dispatched[n] and base_kw[n].get(t, 0.0) > ACTIVE_THRESHOLD_KW]
        recycled = [n for n in names if state[n] == "recovery"]
        candidates = fresh + recycled

        shedding = [n for n in names if state[n] == "shed"]
        pending = sum(1 for n in shedding if i - last_dispatch_idx[n] < RAMP_STEPS)
        pending_kw = pending * EXPECTED_KW_PER_UNIT

        if in_event and not ramp_complete and reduction_prev >= TARGET_KW - BAND_KW:
            ramp_complete = True

        # 4) Ramp-down: dispatch as many of the warmest candidates as the gap needs, every RAMP_BATCH_SPACING_MIN.
        if in_event and not ramp_complete:
            if i >= next_ramp_idx and candidates:
                shortfall = TARGET_KW - reduction_prev - pending_kw
                n_admit = min(max(1, math.ceil(shortfall / EXPECTED_KW_PER_UNIT)), len(candidates))
                for n in warmest_first(candidates)[:n_admit]:
                    start_shed(n, i)
                next_ramp_idx = i + max(1, round(RAMP_BATCH_SPACING_MIN / T_RES))

        # 5) Hold, below the band: dispatch just enough of the warmest candidates to close the gap.
        elif in_event and reduction_prev < TARGET_KW - BAND_KW:
            shortfall = TARGET_KW - reduction_prev - pending_kw
            n_admit = min(max(0, math.ceil(shortfall / EXPECTED_KW_PER_UNIT)), len(candidates),
                          MAX_ADMISSIONS_SAFETY_CAP)
            for n in warmest_first(candidates)[:n_admit]:
                start_shed(n, i)

        # 6) Hold, above the band: move the coldest SHED units to RECOVERY.
        elif in_event and ALLOW_OVERSHOOT_RELEASE and reduction_prev > TARGET_KW + BAND_KW:
            n_release = max(1, math.ceil((reduction_prev - TARGET_KW - BAND_KW) / EXPECTED_KW_PER_UNIT))
            for n in sorted(shedding, key=lambda n: upper_temp[n])[:n_release]:
                end_shed(n, "recovery", t, "overshoot")

        # 7) Step every home forward one timestep with the command for its current state.
        for n in names:
            if state[n] in ("shed", "recovery") and n in release_idx and i >= release_idx[n]:
                if state[n] == "shed":
                    end_shed(n, "idle", t, "released")
                else:
                    state[n] = "idle"
            ctrl = SHED_CTRL if state[n] == "shed" else RECOVERY_CTRL if state[n] == "recovery" else BASELINE_CTRL
            units[n]["dwelling"].update(control_signal=ctrl)
            units[n]["temp_log"].append((t, avg_tank_temp_c(units[n]["wh"])))
            upper_temp[n] = upper_tank_temp_c(units[n]["wh"])

        # 8) Reduction after this step, over every unit dispatched so far.
        dispatched = [n for n in names if ever_dispatched[n]]
        delta_p = sum(units[n]["wh"].electric_kw - base_kw[n].get(t, 0.0) for n in dispatched)
        log_rows.append({
            "Time": t,
            "DeltaP_kW": delta_p,
            "Reduction_kW": -delta_p,
            "Dispatched_Count": len(dispatched),
            "Active_Count": sum(1 for n in names if state[n] == "shed"),
            "Parked_Count": sum(1 for n in names if state[n] == "recovery"),
            "Candidates_Available": len(candidates),
            "Total_Fleet_WH_Power_kW": sum(units[n]["wh"].electric_kw for n in names),
        })
        reduction_prev = -delta_p

    # Close any SHED episode still open when the simulation ends.
    for n in list(shed_start):
        end_shed(n, "shed", sim_times[-1] + pd.Timedelta(minutes=T_RES), "sim_end")

    return episodes, log_rows, sum(ever_dispatched.values())

#########################################
# FINALIZE EACH HOME
#########################################

def finalize_home(unit, df_base_full):
    """Collects the controlled results, checks they match baseline before the event, and saves both per home."""
    df_ctrl, _, _ = unit["dwelling"].finalize()
    df_ctrl = drop_first_day(df_ctrl)
    df_base = drop_first_day(df_base_full.reset_index())

    temps = pd.DataFrame(unit["temp_log"], columns=["Time", "Tank Average Temperature (C)"])
    temps["Time"] = pd.to_datetime(temps["Time"])
    df_ctrl = df_ctrl.merge(temps, on="Time", how="left")

    # Same seed and same control before the event, so the power must be identical.
    pre_ctrl = df_ctrl[df_ctrl['Time'] < dispatch_t].set_index('Time')[WH_POWER]
    pre_base = df_base.set_index('Time')[WH_POWER].reindex(pre_ctrl.index)
    max_diff = (pre_ctrl - pre_base).abs().max() if len(pre_ctrl) else 0.0
    problem = f"{unit['name']}: max |diff| = {max_diff:.6f} kW" if max_diff > 1e-6 else None

    cols = ["Time", "Total Electric Power (kW)", "Total Electric Energy (kWh)", WH_POWER,
            "Water Heating COP (-)", "Water Heating Deadband Upper Limit (C)",
            "Water Heating Deadband Lower Limit (C)", "Water Heating Heat Pump COP (-)",
            "Tank Average Temperature (C)", "Hot Water Minimum Temperature (C)",
            "Hot Water Average Temperature (C)", "Hot Water Maximum Temperature (C)",
            "Hot Water Outlet Temperature (C)", "Temperature - Indoor (C)"]
    df_ctrl = df_ctrl[[c for c in cols if c in df_ctrl.columns]]
    df_base = df_base[[c for c in cols if c in df_base.columns]]

    home_results = os.path.join(unit["path"], "Results")
    os.makedirs(home_results, exist_ok=True)
    df_ctrl.to_csv(os.path.join(home_results, 'hpwh_controlled.csv'), index=False)
    df_base.to_csv(os.path.join(home_results, 'hpwh_baseline.csv'), index=False)

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
    print(f"Found {len(homes)} homes in {INPUT_DIR}")
    print(f"Level {LEVEL}: target {TARGET_KW} +/- {BAND_KW} kW reduction, event {dispatch_t} to {event_end}")

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
                print(f"Baseline failed for {futures[f]}: {e}")
    base_kw = {os.path.basename(h): df[WH_POWER].to_dict() for h, df in baselines.items()}
    print(f"Baseline ready for {len(baselines)} homes.")

    # Controlled: every home runs baseline control up to the event start.
    units = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(run_until_dispatch, h): h for h in homes if h in baselines}
        for f in concurrent.futures.as_completed(futures):
            try:
                units.append(f.result())
            except Exception as e:
                print(f"Controlled run failed for {futures[f]}: {e}")

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
    config = {**reserve_event, "dispatch_start": str(dispatch_t), "event_end": str(event_end),
              "target_kw": TARGET_KW, "band_kw": BAND_KW, "level": LEVEL, "level_setpoint_f": SHED_SETPOINT_F,
              "level_deadband_f": SHED_DEADBAND_F, "min_tank_temp_c": MIN_TANK_TEMP_C,
              "input_folder": INPUT_FOLDER, "n_homes": len(units)}
    with open(os.path.join(RESULTS_DIR, filename + "_config.json"), "w") as f:
        json.dump(config, f, indent=2)
    pd.concat(all_ctrl).sort_values(["Home", "Time"]).to_csv(
        os.path.join(RESULTS_DIR, filename + "_controlled.csv"), index=False)
    pd.concat(all_base).sort_values(["Home", "Time"]).to_csv(
        os.path.join(RESULTS_DIR, filename + "_baseline.csv"), index=False)
    pd.DataFrame(log_rows).to_csv(os.path.join(RESULTS_DIR, filename + "_log.csv"), index=False)
    pd.DataFrame(episodes).sort_values(["Home", "Command_Time"]).to_csv(
        os.path.join(RESULTS_DIR, filename + "_episodes.csv"), index=False)

    # Short console summary (the results script makes the full one).
    log_df = pd.DataFrame(log_rows)
    ev = log_df[(log_df['Time'] >= dispatch_t) & (log_df['Time'] < event_end)]['Reduction_kW']
    print(f"Reduction during event: mean {ev.mean():.2f}, min {ev.min():.2f}, max {ev.max():.2f} kW")
    print(f"Results saved to {RESULTS_DIR}")
