# -*- coding: utf-8 -*-
"""
Recreate HEAT_OR_All.csv from OR_upgrade9.csv (same homes as the HPWH tests).

Filter logic (same as HPWH_A1):
  - upgrade == 9
  - in.state == "OR"
  - in.city == "OR, Portland"
  - in.geometry_building_type_recs == "Single-Family Detached"
  - every tank size                           -> HEAT_OR_All.csv

Every home gets the same heat pump later in HEAT_A3.
Based on HPWH_A1 by Jeff Dinsmore & Thomas Metzler.
"""

from pathlib import Path
import pandas as pd

working_dir = Path(__file__).resolve().parent

#-------------------------------------------------------
# metadata file name (shared ResStock metadata in the HPWH folder)
input_file = "OR_upgrade9.csv"

# filtered file save folder
save_folder = "HEAT Filtered"
#-------------------------------------------------------

SOURCE_FILE = working_dir.parent / "HPWH" / "Metadata" / input_file
OUTPUT_DIR = working_dir / save_folder

EXCLUDE_BLDG_IDS = {}


def recreate_files(source_file: Path = SOURCE_FILE, output_dir: Path = OUTPUT_DIR) -> None:
    if not source_file.exists():
        raise FileNotFoundError(f"Source file not found: {source_file}")

    output_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(source_file, low_memory=False)

    required_columns = [
        "bldg_id",
        "upgrade",
        "in.state",
        "in.city",
        "in.geometry_building_type_recs",
        "out.params.size_water_heater..gal"
    ]
    missing = [col for col in required_columns if col not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    base_filter_all = (
        (df["upgrade"] == 9)
        & (df["in.state"] == "OR")
        & (df["in.city"] == "OR, Portland")
        & (df["in.geometry_building_type_recs"] == "Single-Family Detached")
        & (~df["bldg_id"].isin(EXCLUDE_BLDG_IDS))
    )
    df[base_filter_all].to_csv(output_dir / 'HEAT_OR_All.csv', index=False)

    print(f"HEAT_OR_All.csv: {base_filter_all.sum()} homes")


if __name__ == "__main__":
    recreate_files()
