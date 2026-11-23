# -*- coding: utf-8 -*-
"""
Downloads the ResStock HPXML files (home.xml, in.schedules.csv) for every home in the Battery_A1 csv file.
Saves them in "Battery All Input Files/bldg", one folder per building and upgrade.
Battery_A3 then copies these files into the Battery input folder the tests read.

Based on HPWH_A2 by jdeline, modified by Thomas Metzler.
"""

import os
from ochre import Analysis
import pandas as pd

input_file_All = "Battery_OR_All.csv"
Output_folder_All = "Battery All Input Files"


def downloadTestSet(input_file, Output_folder):

    # -----------------------------
    # PATH SETUP
    # -----------------------------
    working_dir = os.path.dirname(os.path.abspath(__file__))
    csvFilePath = os.path.join(working_dir, "Battery Filtered", input_file)
    print(f"[INFO] CSV Path: {csvFilePath}")

    # -----------------------------
    # LOAD CSV
    # -----------------------------
    df = pd.read_csv(csvFilePath, low_memory=False)
    bldg_list = df['bldg_id'].tolist()

    # -----------------------------
    # CONSTANTS
    # -----------------------------
    upgrade = 9
    release = "resstock_amy2018_release_1"
    yr = "2025"

    base_output = os.path.join(working_dir, Output_folder, "bldg")
    print(f"[INFO] Base output directory: {base_output}")

    # -----------------------------
    # LOOP
    # -----------------------------
    for bldg in bldg_list:
        building_id_str = f"bldg{int(bldg):07}"
        upgrade_str = f"up{upgrade:02}"

        outdir = os.path.join(base_output, f"{building_id_str}-{upgrade_str}")
        os.makedirs(outdir, exist_ok=True)

        # skip if already exists
        if os.path.exists(os.path.join(outdir, "home.xml")):
            print(f"📁 Exists: {building_id_str}-{upgrade_str} — skipping")
            continue

        try:
            Analysis.download_resstock_model(
                building_id=int(bldg),
                upgrade_id=upgrade,
                local_folder=outdir,
                release=release,
                year=yr
            )
            print(f"✅ Downloaded: {building_id_str}-{upgrade_str}")

        except Exception as e:
            print(f"❌ Failed: {building_id_str}-{upgrade_str} — {e}")


if __name__ == "__main__":
    downloadTestSet(input_file_All, Output_folder_All)
