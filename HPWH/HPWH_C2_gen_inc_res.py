# -*- coding: utf-8 -*-
"""
Results for HPWH_B2_gen_inc.py: fleet plots, per-unit statistics (delta_P, energy, response
time, time in SHED), and one summary text file.
"""

import os
import json
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from run_context import load_filename

#########################################
# SETTINGS (the only block that differs from HPWH_C1_gen_dec_res.py)
#########################################

PIPELINE = "FLC_INC"
DEFAULT_TEST = "FLC_INC_Test"
MODE = "INC - Shed"
STATE = "SHED"                  # the reserve state a dispatched unit is commanded into
SIGN = -1                       # change = SIGN * (controlled - baseline), so it is positive when working
CHANGE_NAME = "Load reduction"
CHANGE_YLABEL = "Reduction vs. baseline (kW)"
CHANGE_LEGEND = "reduction(t) = baseline - controlled"
PHASE_NAMES = ("Ramp-down", "Hold", "Ramp-up")

ON_KW = 0.1                     # a unit counts as running when its WH power is above this
P_RATED_KW = 0.5                # rated heat pump power per unit (OCHRE default HPWH Power = 500 W)
LOAD_SETPOINT_C = 60.0          # DEC load-up setpoint (same as MAX_TANK_TEMP_C in HPWH_B1_gen_dec.py)
LOAD_DEADBAND_C = 2.0           # DEC deadband (C); a unit can load up if its tank is colder than setpoint - deadband
PLOT_MARGIN_HR = 2.0            # hours shown before and after the event in the zoomed plots
PARTICIPATION_RATE = 1.0        # Flex Load Capacity inputs
REACHABLE_RATE = 1.0

#########################################
# LOAD DATA
#########################################

WH = "Water Heating Electric Power (kW)"
WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
test_name = os.environ.get('OCHRE_FILENAME') or load_filename(DEFAULT_TEST, pipeline=PIPELINE)
results_dir = os.path.join(WORKING_DIR, "Ready_data", test_name)
plots_dir = os.path.join(results_dir, "plots")
os.makedirs(plots_dir, exist_ok=True)


def run_file(suffix):
    return os.path.join(results_dir, test_name + suffix)


with open(run_file("_config.json")) as f:
    config = json.load(f)
dispatch_t = pd.to_datetime(config["dispatch_start"])
event_end = pd.to_datetime(config["event_end"])
TARGET_KW = config["target_kw"]
BAND_KW = config["band_kw"]

base = pd.read_csv(run_file("_baseline.csv"), parse_dates=["Time"])
ctrl = pd.read_csv(run_file("_controlled.csv"), parse_dates=["Time"])
log = pd.read_csv(run_file("_log.csv"), parse_dates=["Time"])
episodes = pd.read_csv(run_file("_episodes.csv"), parse_dates=["Command_Time", "End_Time"])

N = ctrl["Home"].nunique()
dt_hr = ctrl.loc[ctrl["Home"] == ctrl["Home"].iloc[0], "Time"].diff().median().total_seconds() / 3600

base_fleet = base.groupby("Time")[WH].sum()
ctrl_fleet = ctrl.groupby("Time")[WH].sum()
change = SIGN * (ctrl_fleet - base_fleet)  # fleet delta_p (DEC) or reduction (INC)
in_event = (change.index >= dispatch_t) & (change.index < event_end)

#########################################
# PHASES: ramp (until the target is first reached), hold (until event end), ramp back (until all released)
#########################################

reached = change[in_event] >= TARGET_KW - BAND_KW
hold_start = reached.idxmax() if reached.any() else event_end
out_after_event = log[(log["Time"] >= event_end) & (log["Active_Count"] + log["Parked_Count"] == 0)]
all_back_t = out_after_event["Time"].iloc[0] if len(out_after_event) else change.index.max()
phase_bounds = [(PHASE_NAMES[0], dispatch_t, hold_start), (PHASE_NAMES[1], hold_start, event_end),
                (PHASE_NAMES[2], event_end, all_back_t)]


def phase_of(t):
    for name, start, end in phase_bounds:
        if start <= t < end:
            return name
    return "Other"

#########################################
# PER-UNIT METRICS (one row per dispatch command)
#########################################

def unit_metrics():
    """For each command: change at response, energy while in the state, response time, and time in the state."""
    ctrl_p = ctrl.pivot(index="Time", columns="Home", values=WH)
    base_p = base.pivot(index="Time", columns="Home", values=WH)
    rows = []
    for ep in episodes.itertuples():
        steps = (ctrl_p.index >= ep.Command_Time) & (ctrl_p.index < ep.End_Time)
        p_ctrl = ctrl_p.loc[steps, ep.Home]
        d = SIGN * (p_ctrl - base_p.loc[steps, ep.Home])
        # Responded = the unit turned on (DEC) or turned off (INC) after the command.
        responded = p_ctrl > ON_KW if SIGN > 0 else p_ctrl <= ON_KW
        t_resp = responded.idxmax() if responded.any() else None
        response = (t_resp - ep.Command_Time).total_seconds() / 60 if t_resp is not None else np.nan
        # Delta_P at response = the unit's power change in the step it responded.
        dp_resp = d.loc[t_resp] if t_resp is not None else np.nan
        rows.append({"delta_p_kw": dp_resp, "peak_delta_p_kw": d.max(), "energy_kwh": d.sum() * dt_hr,
                     "response_min": response, "time_in_state_min": steps.sum() * dt_hr * 60,
                     "phase": phase_of(ep.Command_Time)})
    out = pd.concat([episodes.reset_index(drop=True), pd.DataFrame(rows)], axis=1)
    out.insert(1, "Command_No", out.groupby("Home").cumcount() + 1)
    return out


def first_responder(df):
    """The command whose unit responded earliest (command time + response time)."""
    resp = df.dropna(subset=["response_min"])
    if resp.empty:
        return None
    t_resp = resp["Command_Time"] + pd.to_timedelta(resp["response_min"], unit="min")
    return resp.loc[t_resp.idxmin()]


#########################################
# FLEET METRICS
#########################################

def timing_stats():
    """Minutes to first reach the target, minutes held continuously after that, and % of time in band."""
    s = change[in_event]
    ok = s >= TARGET_KW - BAND_KW
    if not ok.any():
        return None, None, np.nan
    first = s[ok].index[0]
    after = s[s.index >= first]
    dropped = after < TARGET_KW - BAND_KW
    hold_end = after[dropped].index[0] if dropped.any() else event_end
    pct_in_band = after.between(TARGET_KW - BAND_KW, TARGET_KW + BAND_KW).mean() * 100
    return (first - dispatch_t).total_seconds() / 60, (hold_end - first).total_seconds() / 60, pct_in_band


def avg_running(df):
    """Average number of units drawing power during the event."""
    w = df[(df["Time"] >= dispatch_t) & (df["Time"] <= event_end)]
    counts = w[w[WH] > ON_KW].groupby("Time").size().reindex(sorted(w["Time"].unique()), fill_value=0)
    return counts.mean()


def flex_load_capacity():
    """Per-unit active energy during the event, scaled by participation, reachability and sample size."""
    sample = float(os.environ.get('OCHRE_SAMPLE') or N)
    out = {}
    for name, df in (("baseline", base), ("controlled", ctrl)):
        w = df[(df["Time"] >= dispatch_t) & (df["Time"] < event_end) & (df[WH] > ON_KW)]
        per_unit = w[WH].sum() * dt_hr / N
        out[name] = (per_unit, per_unit * PARTICIPATION_RATE * REACHABLE_RATE * sample)
    return out, sample

#########################################
# PLOTS
#########################################

def mark_event(ax):
    ax.axvline(dispatch_t, color='red', linestyle=':', linewidth=1.5, label='Dispatch start')
    ax.axvline(event_end, color='gray', linestyle=':', linewidth=1.5, label='Event end')


def save(fig, ax, name, every_hours):
    if every_hours == 1:
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=1))
    else:
        ax.xaxis.set_major_locator(mdates.HourLocator(byhour=range(0, 24, every_hours)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    fig.savefig(os.path.join(plots_dir, f"{test_name}_{name}.png"), dpi=300, bbox_inches='tight')
    plt.close(fig)


def scale_to_data(ax, s):
    lo, hi = s.min(), s.max()
    pad = max(1.0, 0.1 * (hi - lo))
    ax.set_ylim(min(0, lo) - pad, hi + pad)


def plot_fleet_power_zoom():
    lo, hi = dispatch_t - pd.Timedelta(hours=PLOT_MARGIN_HR), event_end + pd.Timedelta(hours=PLOT_MARGIN_HR)
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(base_fleet[lo:hi].index, base_fleet[lo:hi].values, color="#133CAD", linewidth=1.3, label='Baseline')
    ax.plot(ctrl_fleet[lo:hi].index, ctrl_fleet[lo:hi].values, color="#ee5e0a", linestyle='--', linewidth=1.3, label='Controlled')
    ax.set(title=f'Fleet Water Heater Activation ({MODE}) (N={N})', xlabel='Time', ylabel='Fleet Power (kW)')
    ax.legend(loc='upper left')
    save(fig, ax, "fleet_activation_combined_plot", 1)


def plot_fleet_power_day():
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(base_fleet.index, base_fleet.values, color="#133CAD", linewidth=1.3, label='Baseline')
    ax.plot(ctrl_fleet.index, ctrl_fleet.values, color="#ee5e0a", linestyle='--', linewidth=1.3, label='Controlled')
    mark_event(ax)
    ax.axvspan(dispatch_t, event_end, color='red', alpha=0.1)
    ax.set(title=f'Aggregated Fleet Water Heater Power During Reserve Service Event ({MODE}) (N={N})',
           xlabel='Time', ylabel='Fleet Power (kW)')
    ax.legend(loc='upper left')
    save(fig, ax, "fleet_power_event_plot", 3)


def plot_change(zoom):
    lo, hi = change.index.min(), change.index.max()
    if zoom:
        lo, hi = dispatch_t - pd.Timedelta(hours=PLOT_MARGIN_HR), event_end + pd.Timedelta(hours=PLOT_MARGIN_HR)
    s = change[lo:hi]
    fig, ax = plt.subplots(figsize=(10, 5))
    ax.axvspan(dispatch_t, event_end, color='green', alpha=0.08, label='Event window')
    ax.axvspan(event_end, hi, color='orange', alpha=0.08, label='Post-event')
    ax.plot(s.index, s.values, color="#133CAD", linewidth=1.3, label=CHANGE_LEGEND)
    ax.axhline(0, color='black', linewidth=0.8)
    ax.axhline(TARGET_KW, color='black', linestyle='--', linewidth=1.3, label=f'Target = {TARGET_KW} kW')
    mark_event(ax)
    title = f'{CHANGE_NAME} vs. Target' if zoom else f'{CHANGE_NAME} Over the Full Day'
    ax.set(title=f'{title} ({MODE}) (N={N})', xlabel='Time', ylabel=CHANGE_YLABEL, xlim=(lo, hi))
    ax.legend(loc='upper left', fontsize=8)
    scale_to_data(ax, s)
    save(fig, ax, "deltaP" if zoom else "fleet_power_24hr_plot", 1 if zoom else 3)


def plot_active_units():
    w = log[(log["Time"] >= dispatch_t) & (log["Time"] <= event_end)]
    fig, ax = plt.subplots(figsize=(10, 5))
    ax2 = ax.twinx()
    ax.plot(w["Time"], w["Active_Count"], color="#133CAD", linewidth=1.3,
            label=f'Active units ({STATE.lower()}, not released)')
    mark_event(ax)
    ax.set(title=f'Active Dispatched Units Over Time ({MODE}) (N={N})', xlabel='Time',
           ylabel='Active units (count)')
    ax2.set_ylabel('Active units (% of fleet)')
    y_lo, y_hi = ax.get_ylim()
    ax2.set_ylim(y_lo / N * 100, y_hi / N * 100)
    ax.legend(loc='upper left', fontsize=8)
    save(fig, ax, "active_units_plot", 3)


def plot_unit_status(df, case):
    w = df[(df["Time"] >= dispatch_t) & (df["Time"] <= event_end)]
    running = w[w[WH] > ON_KW].groupby("Time").size().reindex(sorted(w["Time"].unique()), fill_value=0)
    fig, ax = plt.subplots(figsize=(10, 5))
    ax2 = ax.twinx()
    ax.plot(running.index, running.values, color="#133CAD", linewidth=1.3, label=f'Consuming power ({case.lower()})')
    ax.plot(running.index, N - running.values, color="#ee5e0a", linestyle='--', linewidth=1.3, label=f'Idle ({case.lower()})')
    mark_event(ax)
    ax.set(title=f'{case} Unit Status Over Time ({MODE}) (N={N})', xlabel='Time', ylabel='Units (count)',
           ylim=(0, N))
    ax2.set(ylabel='Units (% of fleet)', ylim=(0, 100))
    ax.legend(loc='upper left', fontsize=8)
    save(fig, ax, "baseline_status_plot" if case == "Baseline" else "control_status_plot", 3)


#########################################
# MAIN
#########################################

units = unit_metrics()
units.to_csv(run_file("_unit_metrics.csv"), index=False)

plot_fleet_power_zoom()
plot_fleet_power_day()
plot_change(zoom=False)
plot_change(zoom=True)
plot_active_units()
plot_unit_status(base, "Baseline")
plot_unit_status(ctrl, "Controlled")

n_dispatched = units["Home"].nunique()
max_active = log.loc[(log["Time"] >= dispatch_t) & (log["Time"] <= event_end), "Active_Count"].max()
t_reach, t_hold, pct_band = timing_stats()
event_kwh = change[in_event].sum() * dt_hr
base_event_kwh = base_fleet[in_event].sum() * dt_hr
after_event_min = change[change.index >= event_end].min()
# CF(t) = sum of P_i(t) over the units able to respond / (P_rated * N), from the baseline.
if SIGN < 0:
    # INC: only units consuming power can shed, so P_i is their baseline power.
    able = base[WH] > ON_KW
    cf = base[WH].where(able, 0).groupby(base["Time"]).sum() / (P_RATED_KW * N)
    CF_RULE = f"units consuming power (> {ON_KW} kW)"
else:
    # DEC: idle units with tank below setpoint - deadband can load up, so P_i is their headroom (P_rated - P).
    able = (base[WH] <= ON_KW) & (base["Hot Water Outlet Temperature (C)"] < LOAD_SETPOINT_C - LOAD_DEADBAND_C)
    cf = (P_RATED_KW - base[WH]).where(able, 0).groupby(base["Time"]).sum() / (P_RATED_KW * N)
    CF_RULE = f"idle units with tank < {LOAD_SETPOINT_C - LOAD_DEADBAND_C:.1f} C"
# Controlled CF(t) = fleet delta_P(t) / (P_rated * N), from the controlled test.
cf_ctrl = change / (P_RATED_KW * N)
run_base, run_ctrl = avg_running(base), avg_running(ctrl)
flc, sample = flex_load_capacity()
first = first_responder(units)
if first is not None:
    first_lines = [f"Home: {first['Home']} (command at {first['Command_Time']})",
                   f"Delta_P at response: {first['delta_p_kw']:.3f} kW",
                   f"Time in {STATE}: {first['time_in_state_min']:.1f} min"]
else:
    first_lines = ["No unit responded"]

lines = [
    f"FLC {MODE} -- Results Summary",
    "=" * 70,
    f"Test: {test_name}",
    f"Fleet size: {N} homes",
    f"Event: {dispatch_t} to {event_end}",
    f"Target: {TARGET_KW} kW (band +/- {BAND_KW} kW)",
    "",
    "--- Dispatch ---",
    f"Units dispatched: {n_dispatched} / {N} ({n_dispatched / N * 100:.1f}%)",
    f"Units never dispatched: {N - n_dispatched}",
    f"Commands sent: {len(units)} ({len(units) / max(n_dispatched, 1):.2f} per dispatched unit)",
    f"Max simultaneous units in {STATE}: {max_active} ({max_active / N * 100:.1f}% of fleet)",
    "",
    "--- Timing ---",
    f"Time to reach target: {t_reach:.1f} min after dispatch" if t_reach is not None
    else "Time to reach target: target - band was never reached",
    f"Time fleet held target (continuous): {t_hold:.1f} min" if t_hold is not None
    else "Time fleet held target (continuous): n/a",
    f"% of time in band (from first reaching target): {pct_band:.1f}%",
    f"All units back to idle at: {all_back_t}",
    "",
    f"--- Fleet {CHANGE_NAME.lower()} (event window) ---",
    f"Average: {change[in_event].mean():.3f} kW",
    f"Minimum: {change[in_event].min():.3f} kW",
    f"Maximum: {change[in_event].max():.3f} kW",
    f"Energy: {event_kwh:.3f} kWh ({event_kwh / base_event_kwh * 100:.1f}% of baseline energy)",
    f"Lowest value after the event (rebound): {after_event_min:.3f} kW",
    "",
    "--- 1st unit to respond ---",
    *first_lines,
    "",
    f"--- Capacity factor CF (P_rated = {P_RATED_KW} kW, N = {N}) ---",
    f"CF_baseline counts: {CF_RULE}",
    f"CF_baseline (full day average): {cf.mean():.3f}",
    f"CF_controlled (event window average): {cf_ctrl[in_event].mean():.3f}",
    "",
    "--- Unit status (event average, whole fleet) ---",
    f"Baseline -- consuming power: {run_base:.1f} ({run_base / N * 100:.1f}%), idle: {N - run_base:.1f}",
    f"Controlled -- consuming power: {run_ctrl:.1f} ({run_ctrl / N * 100:.1f}%), idle: {N - run_ctrl:.1f}",
    "",
    f"--- Flex Load Capacity (participation {PARTICIPATION_RATE}, reachable {REACHABLE_RATE}, sample {sample:g}) ---",
    f"Per-unit active energy: baseline {flc['baseline'][0]:.4f} kWh, controlled {flc['controlled'][0]:.4f} kWh",
    f"FLC: baseline {flc['baseline'][1]:.3f} kWh, controlled {flc['controlled'][1]:.3f} kWh",
    "=" * 70,
]
with open(run_file("_summary.txt"), "w") as f:
    f.write("\n".join(lines) + "\n")
print("\n".join(lines))
print(f"\nSummary, unit metrics and plots saved to {results_dir}")
