"""
Adjusts the water heater in the XML files that OCHRE will read, for the ERWH tests.
Copies each home's home.xml and in.schedules.csv from the ERWH_A2 download folders (read only) and converts its
water heater into an electric resistance storage tank, matching Thomas Metzler's convert_to_ERWH
(ERWH_CONVERSION_CONFIG), with one tank size (50, 66 or 80 gal) for every home.
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

# ERWH_A2 download folder (read only) : ERWH input folder it becomes.
# If the download folder is missing, the same homes are copied from the HPWH folder instead.
FOLDERS = {
    "ERWH 50 Input Files": "50 Input Files",
    "ERWH All Input Files": "All Input Files",
}
HPWH_FALLBACK = {"ERWH 50 Input Files": "HPWH 50 Input Files", "ERWH All Input Files": "HPWH All Input Files"}

# ---------------------------------------------------------
# CONFIGURATIONS
# ---------------------------------------------------------
ERWH_CONVERSION_CONFIG = {
    "ERWH_Conversion": {
        "FuelType": "electricity",
        "NewType": "storage water heater",
        "HeatingCapacity": "18767.0",  # 5.5 kW element
        "EnergyFactor": "0.93",
        "RecoveryEfficiency": "0.98",
        # HPWH-only or replaced; EnergyFactor and RecoveryEfficiency are re-added after HeatingCapacity.
        "ElementsToRemove": [
            "BackupHeatingCapacity",
            "UniformEnergyFactor",
            "HPWHOperatingMode",
            "UsageBin",
            "FirstHourRating",
            "EnergyFactor",
            "RecoveryEfficiency",
        ]
    }
}

# One tank size for every ERWH (set_ERWH_size): 50, 66 or 80 gallons. The element, EF and RE stay the same.
TANK_SIZE_GAL = 80
ERWH_TANK_SIZES = {50: "50.0", 66: "66.0", 80: "80.0"}


# ---------------------------------------------------------
# MODIFIER FUNCTIONS
# ---------------------------------------------------------
def water_heaters(root):
    """Every <WaterHeatingSystem> block in the file."""
    return [e for e in root.iter() if e.tag.split('}')[-1] == 'WaterHeatingSystem']


def child(elem, tag):
    return next((c for c in elem if c.tag.split('}')[-1] == tag), None)


def convert_to_ERWH(root, config):
    """Converts the water heater into Thomas's standard ERWH (storage tank, 5.5 kW element, EF 0.93, RE 0.98)."""
    ns_match = re.match(r'\{.*\}', root.tag)
    ns_bracket = ns_match.group(0) if ns_match else ''
    conv_data = config["ERWH_Conversion"]

    for elem in water_heaters(root):
        # 1. Remove HPWH-only and replaced elements
        for c in [c for c in elem if c.tag.split('}')[-1] in conv_data["ElementsToRemove"]]:
            elem.remove(c)

        # 2. Update Water Heater Type, Fuel Type and Heating Capacity
        child(elem, 'WaterHeaterType').text = conv_data["NewType"]
        child(elem, 'FuelType').text = conv_data["FuelType"]
        cap_elem = child(elem, 'HeatingCapacity')
        cap_elem.text = conv_data["HeatingCapacity"]

        # 3. Add EnergyFactor and RecoveryEfficiency right after HeatingCapacity (schema order)
        idx = list(elem).index(cap_elem)
        for k, tag in enumerate(["EnergyFactor", "RecoveryEfficiency"], start=1):
            new_elem = ET.Element(f'{ns_bracket}{tag}')
            new_elem.text = conv_data[tag]
            elem.insert(idx + k, new_elem)


def set_ERWH_size(root, size_gal):
    """Gives every ERWH the same tank size (50, 66 or 80 gal)."""
    for elem in water_heaters(root):
        child(elem, 'TankVolume').text = ERWH_TANK_SIZES[size_gal]


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
    print("Starting OCHRE HPXML batch update (ERWH)...")
    assert TANK_SIZE_GAL in ERWH_TANK_SIZES, "TANK_SIZE_GAL must be 50, 66 or 80"

    for src_name, dst_name in FOLDERS.items():
        # 1. Copy each home's files from the ERWH_A2 download folder (or the HPWH folder if not downloaded)
        src_dir = WORKING_DIR / src_name / "bldg"
        if not src_dir.is_dir():
            src_dir = HPWH_DIR / HPWH_FALLBACK[src_name] / "bldg"
        print(f"Source: {src_dir}")
        xml_files = copy_homes(src_dir, WORKING_DIR / dst_name / "bldg")

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

                convert_to_ERWH(root, ERWH_CONVERSION_CONFIG)
                set_ERWH_size(root, TANK_SIZE_GAL)  # every ERWH gets the TANK_SIZE_GAL tank

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

        print(f"{dst_name}: {len(xml_files)} homes converted to ERWH ({TANK_SIZE_GAL} gal)")
