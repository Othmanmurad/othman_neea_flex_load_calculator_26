# -*- coding: utf-8 -*-
"""
Results for Battery_B2_gen_inc.py (Battery): fleet plots, per-unit metrics (delta_P, energy, response
time, time in SHED), capacity factor, and one summary text file.
"""

import os
import json
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from run_context import load_filename

#########################################
# SETTINGS (the only block that differs from Battery_C1_gen_dec_res.py)
#########################################

LOAD_NAME = "Battery"
PIPELINE = "FLC_Battery_INC"
TEST_NAME = ""  # test folder in Ready_data to analyse; "" = the last test simulated
MODE = "INC - Shed"
STATE = "SHED"                 # the reserve state a dispatched unit is commanded into
SIGN = -1                       # change = SIGN * (controlled - baseline), so it is positive when working
CHANGE_NAME = "Load reduction"
CHANGE_YLABEL = "Reduction vs. baseline (kW)"
PHASE_NAMES = ("Ramp-down", "Hold", "Ramp-up")

POWER = "Battery Electric Power (kW)"
DEVICE_LABEL = "Battery"        # used in plot titles
ON_KW = 0.1                     # a unit counts as running when |power| is above this
P_RATED_KW = 5.0               # battery power per unit; the test's own value from its config is used when saved
UNIT_STATE = "Battery SOC (-)"  # state shown in the single-unit plot
UNIT_STATE_LABEL = "SOC (%)"
UNIT_STATE_CONVERT = lambda s: s * 100  # fraction -> %
PLOT_MARGIN_HR = 2.0            # hours shown before and after the event in the zoomed plots
PLOT_AVG_MIN = 15              # averaging window (min) for the _avg plots
AVG_MIN = 10                    # minutes in the rolling average used for the peak and hold time
HOLD_FRACTION = 0.9             # "held" = the averaged fleet change stays at or above this share of its peak
PARTICIPATION_RATE = 1.0        # Flex Load Capacity inputs
REACHABLE_RATE = 1.0
SOC_MIN = 0.1                   # same as BATTERY_PARAMS; the test's own value from its config is used when saved
SOC_MARGIN = 0.05


def cf_baseline(base):
    """INC: batteries above SOC_MIN can discharge, so P_i is their room to drop (P + P_rated)."""
    p = base[POWER]
    able = base["Battery SOC (-)"] > SOC_MIN + SOC_MARGIN
    cf = (P_RATED_KW + p).where(able, 0).groupby(base["Time"]).sum() / (P_RATED_KW * N)
    return cf, f"batteries with SOC > {SOC_MIN + SOC_MARGIN:.2f}"


#########################################
# LOAD DATA
#########################################

WORKING_DIR = os.path.dirname(os.path.abspath(__file__))
test_name = os.environ.get('OCHRE_FILENAME') or TEST_NAME or load_filename("", pipeline=PIPELINE)
results_dir = os.path.join(WORKING_DIR, "Ready_data", test_name)
plots_dir = os.path.join(results_dir, "plots")
os.makedirs(plots_dir, exist_ok=True)


def run_file(suffix):
    return os.path.join(results_dir, test_name + suffix)


with open(run_file("_config.json")) as f:
    config = json.load(f)
STATE = config.get('level', STATE)  # the level this test actually used
P_RATED_KW = config.get('capacity', P_RATED_KW)  # battery settings this test actually used
SOC_MIN = config.get('soc_min', SOC_MIN)
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

base_fleet = base.groupby("Time")[POWER].sum()
ctrl_fleet = ctrl.groupby("Time")[POWER].sum()
change = SIGN * (ctrl_fleet - base_fleet)  # fleet delta_p (DEC) or reduction (INC)
in_event = (change.index >= dispatch_t) & (change.index < event_end)
RAMP_MIN = float(config.get('ramp_down_min', 10))  # ramp period after dispatch; the target is held after it
in_hold = (change.index >= dispatch_t + pd.Timedelta(minutes=RAMP_MIN)) & (change.index < event_end)
T_RES_MIN = (change.index[1] - change.index[0]).total_seconds() / 60  # minutes per row

def hold_time(frac=0.9):
    """Minutes the 15-min average stays >= frac x the hold-period average: longest unbroken stretch, and in total."""
    ok = change.rolling(PLOT_AVG_MIN, center=True, min_periods=1).mean()[in_hold] >= frac * change[in_hold].mean()
    continuous = int(ok.groupby((~ok).cumsum()).sum().max())  # longest run of True values
    return continuous * T_RES_MIN, int(ok.sum()) * T_RES_MIN, int(in_hold.sum()) * T_RES_MIN
change_avg = change.rolling(f"{AVG_MIN}min").mean()  # trailing average

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
    ctrl_p = ctrl.pivot(index="Time", columns="Home", values=POWER)
    base_p = base.pivot(index="Time", columns="Home", values=POWER)
    rows = []
    for ep in episodes.itertuples():
        steps = (ctrl_p.index >= ep.Command_Time) & (ctrl_p.index < ep.End_Time)
        p_ctrl = ctrl_p.loc[steps, ep.Home]
        d = SIGN * (p_ctrl - base_p.loc[steps, ep.Home])
        # Responded = the unit's power moved more than ON_KW from its baseline, in the commanded direction.
        responded = d > ON_KW
        t_resp = responded.idxmax() if responded.any() else None
        response = (t_resp - ep.Command_Time).total_seconds() / 60 if t_resp is not None else np.nan
        # Delta_P at response = the unit's power change in the step it responded.
        dp_resp = d.loc[t_resp] if t_resp is not None else np.nan
        rows.append({"delta_p_kw": dp_resp, "peak_delta_p_kw": d.max(), "energy_kwh": d.sum() * dt_hr,
                     "response_min": response, "time_in_state_min": steps.sum() * dt_hr * 60,
                     "phase": phase_of(ep.Command_Time)})
    cols = ["delta_p_kw", "peak_delta_p_kw", "energy_kwh", "response_min", "time_in_state_min", "phase"]
    out = pd.concat([episodes.reset_index(drop=True), pd.DataFrame(rows, columns=cols)], axis=1)
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

def peak_hold_stats():
    """Peak of the averaged fleet change in the event, and the longest continuous time it stayed above
    HOLD_FRACTION of that peak."""
    s = change_avg[(change_avg.index >= dispatch_t) & (change_avg.index < event_end)].dropna()
    peak, peak_t = s.max(), s.idxmax()
    above = s >= HOLD_FRACTION * peak
    longest = run = 0
    for ok in above:
        run = run + 1 if ok else 0
        longest = max(longest, run)
    return peak, peak_t, longest * dt_hr * 60


def avg_running(df):
    """Average number of units drawing power during the event."""
    w = df[(df["Time"] >= dispatch_t) & (df["Time"] <= event_end)]
    counts = w[w[POWER].abs() > ON_KW].groupby("Time").size().reindex(sorted(w["Time"].unique()), fill_value=0)
    return counts.mean()


def flex_load_capacity():
    """Per-unit active energy during the event, scaled by participation, reachability and sample size."""
    sample = float(os.environ.get('OCHRE_SAMPLE') or N)
    out = {}
    for name, df in (("baseline", base), ("controlled", ctrl)):
        w = df[(df["Time"] >= dispatch_t) & (df["Time"] < event_end) & (df[POWER].abs() > ON_KW)]
        per_unit = w[POWER].abs().sum() * dt_hr / N
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


def smooth(s):
    return s.rolling(PLOT_AVG_MIN, center=True, min_periods=1).mean()


def plot_fleet_power_zoom(avg=False):
    # avg=True: same plot with the PLOT_AVG_MIN centered average of each curve.
    b, c = (smooth(base_fleet), smooth(ctrl_fleet)) if avg else (base_fleet, ctrl_fleet)
    lo, hi = dispatch_t - pd.Timedelta(hours=PLOT_MARGIN_HR), event_end + pd.Timedelta(hours=PLOT_MARGIN_HR)
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(b[lo:hi].index, b[lo:hi].values, color="#133CAD", linewidth=1.3, label='Baseline')
    ax.plot(c[lo:hi].index, c[lo:hi].values, color="#ee5e0a", linestyle='--', linewidth=1.3, label='Controlled')
    ax.set(title=f'Aggregated Batteries Activation ({MODE}) (N={N}) (±2 hr of Event Window)', xlabel='Time', ylabel='Fleet Power (kW)')
    ax.legend(loc='upper left')
    save(fig, ax, "fleet_activation_combined_plot" + ("_avg" if avg else ""), 1)


def plot_fleet_power_day(avg=False):
    b, c = (smooth(base_fleet), smooth(ctrl_fleet)) if avg else (base_fleet, ctrl_fleet)
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(b.index, b.values, color="#133CAD", linewidth=1.3, label='Baseline')
    ax.plot(c.index, c.values, color="#ee5e0a", linestyle='--', linewidth=1.3, label='Controlled')
    mark_event(ax)
    ax.axvspan(dispatch_t, event_end, color='red', alpha=0.1)
    ax.set(title=f'Aggregated Batteries Power During Reserve Service Event ({MODE}) (N={N})',
           xlabel='Time', ylabel='Fleet Power (kW)')
    ax.legend(loc='upper left')
    save(fig, ax, "fleet_power_event_plot" + ("_avg" if avg else ""), 3)


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
    running = w[w[POWER].abs() > ON_KW].groupby("Time").size().reindex(sorted(w["Time"].unique()), fill_value=0)
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


def plot_single_unit(home):
    """The first responding unit's power and state, baseline vs controlled, with its commanded time shaded (±2 hr of the event)."""
    lo, hi = dispatch_t - pd.Timedelta(hours=PLOT_MARGIN_HR), event_end + pd.Timedelta(hours=PLOT_MARGIN_HR)
    b = base[(base["Home"] == home) & base["Time"].between(lo, hi)].set_index("Time")
    c = ctrl[(ctrl["Home"] == home) & ctrl["Time"].between(lo, hi)].set_index("Time")
    fig, (ax, ax_s) = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
    for df, label, color, style in ((b, 'Baseline', "#133CAD", '-'), (c, 'Controlled', "#ee5e0a", '--')):
        ax.plot(df.index, df[POWER], color=color, linestyle=style, linewidth=1.3, label=label)
        if UNIT_STATE in df.columns:  # some states are only logged in the controlled run
            ax_s.plot(df.index, UNIT_STATE_CONVERT(df[UNIT_STATE]), color=color, linestyle=style, linewidth=1.3, label=label)
    for k, ep in enumerate(episodes[episodes["Home"] == home].itertuples()):
        for a in (ax, ax_s):
            a.axvspan(ep.Command_Time, ep.End_Time, color='green', alpha=0.12, label=f'In {STATE}' if k == 0 and a is ax else None)
    for a in (ax, ax_s):
        mark_event(a)
    ax.set(title=f'Single {DEVICE_LABEL} Operating Status ({MODE})', ylabel='Power (kW)')
    ax_s.set(xlabel='Time', ylabel=UNIT_STATE_LABEL)
    ax.legend(loc='upper left', fontsize=8)
    save(fig, ax_s, "single_unit_status_plot", 1)


#########################################
# MAIN
#########################################

units = unit_metrics()
units.to_csv(run_file("_unit_metrics.csv"), index=False)

plot_fleet_power_zoom()
plot_fleet_power_day()
plot_fleet_power_zoom(avg=True)
plot_fleet_power_day(avg=True)
plot_active_units()
plot_unit_status(base, "Baseline")
plot_unit_status(ctrl, "Controlled")

n_dispatched = units["Home"].nunique()
max_active = log.loc[(log["Time"] >= dispatch_t) & (log["Time"] <= event_end), "Active_Count"].max()
peak_kw, peak_t, hold_min = peak_hold_stats()
event_kwh = change[in_event].sum() * dt_hr
base_event_kwh = base_fleet[in_event].sum() * dt_hr
after_event_min = change[change.index >= event_end].min()
# CF(t) = sum of P_i(t) over the units able to respond / (P_rated * N), from the baseline.
cf, CF_RULE = cf_baseline(base)
# Controlled CF(t) = fleet delta_P(t) / (P_rated * N), from the controlled test.
cf_ctrl = change / (P_RATED_KW * N)
run_base, run_ctrl = avg_running(base), avg_running(ctrl)
flc, sample = flex_load_capacity()
first = first_responder(units)
if first is not None:
    plot_single_unit(first["Home"])
if first is not None:
    first_lines = [f"Home: {first['Home']} (command at {first['Command_Time']})",
                   f"Delta_P at response: {first['delta_p_kw']:.3f} kW",
                   f"Time in {STATE}: {first['time_in_state_min']:.1f} min"]
else:
    first_lines = ["No unit responded"]

lines = [
    f"FLC {LOAD_NAME} {MODE} (level {config.get('level', '?')}) -- Results Summary",
    "=" * 70,
    f"Test: {test_name}",
    f"Fleet size: {N} homes",
    f"Event: {dispatch_t} to {event_end}",
    f"Target: {TARGET_KW} kW (band +/- {BAND_KW} kW)",
    f"Service duration: {(event_end - dispatch_t).total_seconds() / 3600:g} h",
    f"Control level: {config.get('level', '?')}"
    + (f" (setpoint {config['level_setpoint_f']} F, deadband {config['level_deadband_f']} F)"
       if "level_setpoint_f" in config else ""),
    "",
    "--- Dispatch ---",
    f"Units dispatched: {n_dispatched} / {N} ({n_dispatched / N * 100:.1f}%)",
    f"Units never dispatched: {N - n_dispatched}",
    f"Max simultaneous units in {STATE}: {max_active} ({max_active / N * 100:.1f}% of fleet)",
    "",
    "--- Timing ---",
    f"Peak {AVG_MIN}-min average {CHANGE_NAME.lower()}: {peak_kw:.3f} kW "
    f"at {peak_t:%H:%M} ({(peak_t - dispatch_t).total_seconds() / 60:.0f} min after dispatch)",
    f"Time held at >= {HOLD_FRACTION * 100:.0f}% of that peak (continuous): {hold_min:.1f} min",
    f"All units back to idle at: {all_back_t}",
    "",
    f"--- Fleet {CHANGE_NAME.lower()} (event window) ---",
    f"Average: {change[in_event].mean():.3f} kW",
    f"Average after the {RAMP_MIN:g}-min ramp (hold period): {change[in_hold].mean():.3f} kW",
    "Time held at >= 90%% of that average (15-min average): %g min longest unbroken, %g of %g min in total" % hold_time(),
    f"Minimum: {change[in_event].min():.3f} kW",
    f"Maximum: {change[in_event].max():.3f} kW",
    f"Energy: {event_kwh:.3f} kWh (" + (f"{event_kwh / base_event_kwh * 100:.1f}% of baseline energy" if base_event_kwh else "baseline battery is idle") + ")",
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
