# -*- coding: utf-8 -*-
"""
Builds the Battery input folder that OCHRE will read, for the Battery tests.
Copies each home's home.xml and in.schedules.csv from the Battery_A2 download folder (read only) into "All Input Files".
The XML is not changed: the battery is added by the Battery B scripts through OCHRE's equipment arguments
(BATTERY_PARAMS: same 12 kWh / 5 kW battery in every home, random starting SOC per home).
"""

import shutil
from pathlib import Path

# ---------------------------------------------------------
# DIRECTORY SETUP
# ---------------------------------------------------------
WORKING_DIR = Path(__file__).resolve().parent
HPWH_DIR = WORKING_DIR.parent / "HPWH"

# Battery_A2 download folder (read only) : Battery input folder it becomes.
# If the download folder is missing, the same homes are copied from the HPWH folder instead.
FOLDERS = {
    "Battery All Input Files": "All Input Files",
}
HPWH_FALLBACK = {"Battery All Input Files": "HPWH All Input Files"}


# ---------------------------------------------------------
# DUPLICATION LOGIC
# ---------------------------------------------------------
def copy_homes(input_dir, output_dir):
    """Copies home.xml and in.schedules.csv of every home; returns the copied home.xml files."""
    xml_files = []
    for home in sorted(Path(input_dir).iterdir()):
        xml_file, sched_file = home / "home.xml", home / "in.schedules.csv"
        if not (xml_file.is_file() and sched_file.is_file()):
            continue
        (output_dir / home.name).mkdir(parents=True, exist_ok=True)
        shutil.copy(xml_file, output_dir / home.name / "home.xml")
        shutil.copy(sched_file, output_dir / home.name / "in.schedules.csv")
        xml_files.append(output_dir / home.name / "home.xml")
    return xml_files


# ---------------------------------------------------------
# MAIN EXECUTION BLOCK
# ---------------------------------------------------------
if __name__ == "__main__":
    print("Starting OCHRE input copy (Battery)...")

    for src_name, dst_name in FOLDERS.items():
        # Never write through a link into the HPWH folders (they are a source and must stay unchanged).
        dst_dir = WORKING_DIR / dst_name / "bldg"
        if str(dst_dir.resolve()).startswith(str(HPWH_DIR.resolve())):
            raise RuntimeError(f"'{dst_name}' points into the HPWH folder; remove the link first")

        # Copy each home's files from the Battery_A2 download folder (or the HPWH folder if not downloaded)
        src_dir = WORKING_DIR / src_name / "bldg"
        if not src_dir.is_dir():
            src_dir = HPWH_DIR / HPWH_FALLBACK[src_name] / "bldg"
        print(f"Source: {src_dir}")
        xml_files = copy_homes(src_dir, dst_dir)

        print(f"{dst_name}: {len(xml_files)} homes copied")
