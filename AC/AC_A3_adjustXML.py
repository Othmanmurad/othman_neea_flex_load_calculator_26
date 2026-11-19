# -*- coding: utf-8 -*-
"""
Adjusts the heating and cooling in the XML files that OCHRE will read, for the AC tests.
Copies each home's home.xml and in.schedules.csv from the AC_A2 download folders (read only) and replaces
the home's heating and cooling with one all-electric heat pump, matching Thomas Metzler's convert_to_HeatPump
(HVAC_CONVERSION_CONFIG): every home gets the same ASHP, 36000 BTU/h, SEER 18, HSPF 10, with 36000 BTU/h
electric backup.
"""

import shutil
import xml.etree.ElementTree as ET
from pathlib import Path
import re

# ---------------------------------------------------------
# DIRECTORY SETUP
# ---------------------------------------------------------
WORKING_DIR = Path(__file__).resolve().parent
HPWH_DIR = WORKING_DIR.parent / "HPWH"

# AC_A2 download folder (read only) : AC input folder it becomes.
# If the download folder is missing, the same homes are copied from the HPWH folder instead.
FOLDERS = {
    "AC 50 Input Files": "50 Input Files",
    "AC All Input Files": "All Input Files",
}
HPWH_FALLBACK = {"AC 50 Input Files": "HPWH 50 Input Files", "AC All Input Files": "HPWH All Input Files"}

# ---------------------------------------------------------
# CONFIGURATIONS
# ---------------------------------------------------------
# Values from Thomas's HVAC_CONVERSION_CONFIG.
HVAC_CONVERSION_CONFIG = {
    "Type": "ASHP",                  # "ASHP" for air-to-air, "MSHP" for mini-split
    "HeatPumpType": "air-to-air",    # "air-to-air" for ASHP, "mini-split" for MSHP
    "CompressorType": "single stage",
    "UnitLocation": "conditioned space",
    "Capacity": 36000.0,             # BTU/h, heating and cooling
    "SEER": 18.0,
    "HSPF": 10.0,
    "BackupHeatingCapacity": 36000.0,
    "HeatingAirflowCFM": 1200.0,
    "CoolingAirflowCFM": 1200.0,
    # MSHP only: capacity ratio and COP at each outdoor temperature (F)
    "MSHP_Cooling_Perf": [
        {"Temp": 95.0, "CapacityRatio": 1.0, "COP": 3.0, "Desc": "maximum"},
        {"Temp": 95.0, "CapacityRatio": 0.25, "COP": 4.0, "Desc": "minimum"},
        {"Temp": 82.0, "CapacityRatio": 1.03, "COP": 3.8, "Desc": "maximum"},
        {"Temp": 82.0, "CapacityRatio": 0.28, "COP": 5.2, "Desc": "minimum"},
    ],
    "MSHP_Heating_Perf": [
        {"Temp": 47.0, "CapacityRatio": 1.2, "COP": 3.5, "Desc": "maximum"},
        {"Temp": 47.0, "CapacityRatio": 0.3, "COP": 4.5, "Desc": "minimum"},
        {"Temp": 5.0, "CapacityRatio": 0.48, "COP": 2.2, "Desc": "maximum"},
        {"Temp": 5.0, "CapacityRatio": 0.18, "COP": 2.5, "Desc": "minimum"},
    ],
}


# ---------------------------------------------------------
# MODIFIER FUNCTIONS
# ---------------------------------------------------------
def sub(parent, ns, tag, text=None, **attrib):
    elem = ET.SubElement(parent, f"{ns}{tag}", **attrib)
    if text is not None:
        elem.text = str(text)
    return elem


def add_performance(hp, ns, tag, points, capacity):
    """MSHP detailed performance table (capacity = ratio x rated capacity)."""
    table = sub(hp, ns, tag)
    for pt in points:
        pdp = sub(table, ns, "PerformanceDataPoint")
        sub(pdp, ns, "OutdoorTemperature", pt["Temp"])
        sub(pdp, ns, "Capacity", round(capacity * pt["CapacityRatio"], 1))
        sub(pdp, ns, "CapacityDescription", pt["Desc"])
        eff = sub(pdp, ns, "Efficiency")
        sub(eff, ns, "Units", "COP")
        sub(eff, ns, "Value", pt["COP"])


def convert_to_heat_pump(root, conv):
    """Thomas's convert_to_HeatPump: removes every heating/cooling system and adds one heat pump."""
    ns_match = re.match(r"\{.*\}", root.tag)
    ns = ns_match.group(0) if ns_match else ""

    for plant in root.findall(f".//{ns}HVACPlant"):
        # 1. Primary systems point to the new heat pump.
        primary = plant.find(f"{ns}PrimarySystems")
        if primary is not None:
            for child in list(primary):
                primary.remove(child)
            sub(primary, ns, "PrimaryHeatingSystem", idref="HeatPump1")
            sub(primary, ns, "PrimaryCoolingSystem", idref="HeatPump1")

        # 2. Remove the old systems, keeping the first distribution system (ducts) they used.
        dist_idref = None
        for tag in ["HeatingSystem", "CoolingSystem", "HeatPump"]:
            for system in plant.findall(f"{ns}{tag}"):
                dist = system.find(f"{ns}DistributionSystem")
                if dist is not None and not dist_idref:
                    dist_idref = dist.get("idref")
                plant.remove(system)

        # 3. The new heat pump, in schema order.
        hp = sub(plant, ns, "HeatPump")
        sub(hp, ns, "SystemIdentifier", id="HeatPump1")
        sub(hp, ns, "UnitLocation", conv["UnitLocation"])
        if dist_idref and conv["HeatPumpType"] == "air-to-air":
            sub(hp, ns, "DistributionSystem", idref=dist_idref)
        sub(hp, ns, "HeatPumpType", conv["HeatPumpType"])
        sub(hp, ns, "HeatPumpFuel", "electricity")
        sub(hp, ns, "HeatingCapacity", conv["Capacity"])
        sub(hp, ns, "CoolingCapacity", conv["Capacity"])
        sub(hp, ns, "CompressorType", conv["CompressorType"])
        sub(hp, ns, "CompressorLockoutTemperature", "-20.0" if conv["Type"] == "MSHP" else "0.0")
        sub(hp, ns, "CoolingSensibleHeatFraction", "0.73")
        sub(hp, ns, "BackupType", "integrated")
        sub(hp, ns, "BackupSystemFuel", "electricity")
        backup_eff = sub(hp, ns, "BackupAnnualHeatingEfficiency")
        sub(backup_eff, ns, "Units", "Percent")
        sub(backup_eff, ns, "Value", "1.0")
        sub(hp, ns, "BackupHeatingCapacity", conv["BackupHeatingCapacity"])
        sub(hp, ns, "BackupHeatingLockoutTemperature", "40.0")
        sub(hp, ns, "FractionHeatLoadServed", "1.0")
        sub(hp, ns, "FractionCoolLoadServed", "1.0")
        cool_eff = sub(hp, ns, "AnnualCoolingEfficiency")
        sub(cool_eff, ns, "Units", "SEER")
        sub(cool_eff, ns, "Value", conv["SEER"])
        heat_eff = sub(hp, ns, "AnnualHeatingEfficiency")
        sub(heat_eff, ns, "Units", "HSPF")
        sub(heat_eff, ns, "Value", conv["HSPF"])

        # 4. MSHP only: detailed performance tables.
        if conv["Type"] == "MSHP":
            add_performance(hp, ns, "CoolingDetailedPerformanceData", conv["MSHP_Cooling_Perf"], conv["Capacity"])
            add_performance(hp, ns, "HeatingDetailedPerformanceData", conv["MSHP_Heating_Perf"], conv["Capacity"])

        # 5. Airflow.
        ext = sub(hp, ns, "extension")
        sub(ext, ns, "HeatingAirflowCFM", conv["HeatingAirflowCFM"])
        sub(ext, ns, "CoolingAirflowCFM", conv["CoolingAirflowCFM"])


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
    print("Starting OCHRE HPXML batch update (AC)...")

    for src_name, dst_name in FOLDERS.items():
        # Never write through a link into the HPWH folders (they are a source and must stay unchanged).
        dst_dir = WORKING_DIR / dst_name / "bldg"
        if str(dst_dir.resolve()).startswith(str(HPWH_DIR.resolve())):
            raise RuntimeError(f"'{dst_name}' points into the HPWH folder; remove the link first")

        # 1. Copy each home's files from the AC_A2 download folder (or the HPWH folder if not downloaded)
        src_dir = WORKING_DIR / src_name / "bldg"
        if not src_dir.is_dir():
            src_dir = HPWH_DIR / HPWH_FALLBACK[src_name] / "bldg"
        print(f"Source: {src_dir}")
        xml_files = copy_homes(src_dir, dst_dir)

        # 2. Update every copied home.xml
        for xml_file in xml_files:
            try:
                # Dynamically register namespaces
                for event, (prefix, uri) in ET.iterparse(xml_file, events=['start-ns']):
                    ET.register_namespace(prefix, uri)

                tree = ET.parse(xml_file)
                root = tree.getroot()

                # ==========================================
                # TURN YOUR UPDATES ON OR OFF HERE
                # ==========================================

                convert_to_heat_pump(root, HVAC_CONVERSION_CONFIG)

                # ==========================================

                # Apply pretty-print formatting to the tree
                if hasattr(ET, 'indent'):
                    ET.indent(tree, space="  ", level=0)

                # Write changes back to the copied file
                tree.write(xml_file, encoding='UTF-8', xml_declaration=True)

            except ET.ParseError as e:
                print(f"Failed to parse XML for {xml_file}: {e}")
            except Exception as e:
                print(f"An error occurred while processing {xml_file}: {e}")

        print(f"{dst_name}: {len(xml_files)} homes converted to the standard {HVAC_CONVERSION_CONFIG['Type']}")
