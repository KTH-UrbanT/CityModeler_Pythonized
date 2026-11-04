import json
import math
import os
import re
import shutil

import fiona
import geopandas as gpd
import pandas as pd
import numpy as np
import yaml

def read_geojson(self, Path):
    geodata = gpd.read_file(Path)
    return geodata

def readCSV(Path):
    return pd.read_csv(Path, delimiter=";", low_memory=False)

def Read_json(Path):
    try:
        with open(Path, 'r', encoding='utf-8') as file:
            data = json.load(file)  # Load JSON data into a Python dictionary
        return data
    except FileNotFoundError:
        print(f"Error: The file '{Path}' was not found.")

def Read_yml(Path):
    with open(Path, "r") as file:
        data = yaml.load(file, Loader=yaml.FullLoader)
        return data

def readGjsonFiona(Path):
    selected_data = []
    with fiona.open(Path) as src:
        for feature in src:
            selected_entry = {
                "50A_UUID": feature["properties"].get("50A_UUID"),
                "FootPrints": feature['geometry'].get("coordinates"),
                "FormularID": feature["properties"].get("FormularId"),
                "FNR" : feature["properties"].get("FNR"),
            }
            selected_data.append(selected_entry)
    return selected_data


def clean_nans(obj):
    """Recursively clean NaNs, Series, and NumPy types for JSON serialization."""
    if isinstance(obj, dict):
        return {k: clean_nans(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [clean_nans(v) for v in obj]
    elif isinstance(obj, (pd.Series, np.ndarray)):
        # Convert Series to list and clean each item
        return [clean_nans(x) for x in obj.tolist()]
    elif isinstance(obj, (np.integer, np.int64)):
        return int(obj)
    elif isinstance(obj, (np.floating, np.float64)):
        return None if math.isnan(obj) else float(obj)
    elif pd.isna(obj):  # catches pd.NA and np.nan
        return None
    elif obj == "":
        return ""
    else:
        return obj


def SaveitGeoJson(Mainpath, BuildingData, Path2Data, CaseName):
    # Replace this with your actual GeoJSON input dictionary
    geojson_cleaned = clean_nans(BuildingData)
    if 'bin' in Mainpath:
        Mainpath = Mainpath[:Mainpath.find('bin')]
    DataFolder = os.path.join(Mainpath,'examples', 'Data_for_'+CaseName)
        # os.path.join(f"{Mainpath}{Path2Data[3:]}"[:(f"{Mainpath}{Path2Data[3:]}").find('examples')+9], 'Data_for_'+CaseName)
    os.makedirs(os.path.dirname(DataFolder), exist_ok=True)
    if os.path.exists(DataFolder):
        shutil.rmtree(DataFolder)
        os.mkdir(DataFolder)
    else:
        os.mkdir(DataFolder)
    # Dump to string with Unicode and indentation
    geojson_str = json.dumps(geojson_cleaned, indent=2, ensure_ascii=False)
    geojson_str = re.sub(
        r'\[\s*([-0-9.eE]+),\s*([-0-9.eE]+),\s*([-0-9.eE]+)\s*\]',
        r'[\1, \2, \3]', geojson_str)
    geojson_str = re.sub(
        r'\[\s*([-0-9.eE]+),\s*([-0-9.eE]+)\s*\]',
        r'[\1, \2]', geojson_str)
    # Save to GeoJSON file
    with open(os.path.join(DataFolder, CaseName + '.geojson'), "w", encoding="utf-8") as f:
        f.write(geojson_str)
    return DataFolder
