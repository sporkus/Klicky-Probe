#!/usr/bin/env python3

# Automating probe_accuracy testing
# The following three tests will be done:
# 0) 20 tests, 5 samples at bed center - check consistency within normal measurements
# 1) 1 test, 100 samples at bed center - check for drift
# 2) 1 test, 30 samples at each bed mesh corners - check if there are issues with individual z drives
# Notes:
# * First probe measurements are dropped

import re
import os
from datetime import datetime
from statistics import mean

import pandas as pd
import matplotlib.pyplot as plt
from requests import get, post
from typing import Tuple, List, Dict

LEVEL_GCODE = "QUAD_GANTRY_LEVEL"
LOCK_PROBE = True  # if False, probe will dock/undock between tests
MOONRAKER_URL = "http://localhost:7125"
KLIPPY_LOG = "/home/pi/klipper_logs/klippy.log"
DATA_DIR = "/home/pi/probe_accuracy_results"


def main():
    if not os.path.exists(DATA_DIR):
        os.mkdir(DATA_DIR)
    try:
        homing()
        level_bed()
        test_routine()
    except KeyboardInterrupt:
        send_gcode("DOCK_PROBE_UNLOCK")


def test_routine():
    if LOCK_PROBE:
        send_gcode("ATTACH_PROBE_LOCK")
    dfs = [
        test_corners(n=30),
        test_drift(n=100),
        test_repeatability(test_count=20, probe_count=5),
    ]
    send_gcode("DOCK_PROBE_UNLOCK")

    df = pd.concat(dfs, axis=0, ignore_index=True).sort_index()

    file_nm = f"probe_accuracy_test_{now()}"
    df.to_csv(DATA_DIR + "/" + file_nm + ".csv", index=False)
    summary = df.groupby("test")["z"].agg(
        ["min", "max", "first", "last", "mean", "std", "count"]
    )
    summary["drift"] = summary["last"] - summary["first"]
    summary.to_csv(f"{DATA_DIR}/{file_nm}_summary.csv")


def test_drift(n=100):
    print(f"\nTake {n} samples in a row to check for drift")
    df = test_probe(probe_count=n, testname=f"center_{n}samples")
    summary = df.groupby("test")["z"].agg(
        ["min", "max", "first", "last", "mean", "std", "count"]
    )
    summary["drift"] = summary["last"] - summary["first"]
    print(summary)
    ax = df.plot.scatter(x="index", y="z", title=f"Drift test (n={n})")
    ax.figure.savefig(f"{DATA_DIR}/probe_accuracy_{now()}_drift.png")
    return df


def test_repeatability(test_count=10, probe_count=10):
    print(f"\nTake {test_count} probe_accuracy tests to check for repeatability")
    dfs = []
    for i in range(test_count):
        send_gcode("DOCK_PROBE_UNLOCK")
        df = test_probe(probe_count, testname=f"center_{probe_count}samples_#{i:02d}")
        df["measurement"] = f"Test #{i:02d}"
        dfs.append(df)
    df = pd.concat(dfs, axis=0).sort_index()
    summary = df.groupby("test")["z"].agg(
        ["min", "max", "first", "last", "mean", "std", "count"]
    )
    summary["drift"] = summary["last"] - summary["first"]
    print(summary)
    ax = df.boxplot(column="z", by="measurement", rot=45, fontsize=8)
    plot_nm = f"probe_accuracy_{now()}_repeatability"
    plt.title(plot_nm)
    plt.suptitle("")
    ax.figure.savefig(DATA_DIR + "/" + plot_nm + ".png")
    return df


def test_corners(n=30):
    print(
        "\nTest probe around the bed to see if there are issues with individual drives"
    )
    dfs = []
    for i, xy in enumerate(get_bed_corners()):
        xy_txt = f"({xy[0]:.0f}, {xy[1]:.0f})"
        df = test_probe(
            probe_count=n,
            loc=xy,
            testname=f"corner_{n}samples{xy_txt}",
        )
        df["measurement"] = xy_txt
        dfs.append(df)
    df = pd.concat(dfs, axis=0).sort_index()
    summary = df.groupby("test")["z"].agg(
        ["min", "max", "first", "last", "mean", "std", "count"]
    )
    summary["drift"] = summary["last"] - summary["first"]
    print(summary)
    ax = df.boxplot(column="z", by="measurement", rot=45, fontsize=8)
    plot_nm = f"probe_accuracy_{now()}_corners"
    plt.title(plot_nm)
    plt.suptitle("")
    ax.figure.savefig(DATA_DIR + "/" + plot_nm + ".png")
    return df


def send_gcode(gcode):
    gcode = re.sub(" ", "%20", gcode)
    url = f"{MOONRAKER_URL}/printer/gcode/script?script={gcode}"
    post(url)


def homing() -> None:
    """Home if not done already"""
    axes = query_printer_objects("toolhead", "homed_axes")
    if axes != "xyz":
        print("Homing")
        send_gcode("G28")


def level_bed() -> None:
    """Level bed if not done already"""
    status = query_printer_objects(LEVEL_GCODE.lower(), "applied")
    if not status:
        print("Leveling")
        send_gcode(LEVEL_GCODE)


def query_printer_objects(object, key=None):
    url = f"{MOONRAKER_URL}/printer/objects/query?{object}"
    resp = get(url).json()
    obj = resp["result"]["status"][object]
    if key:
        obj = obj[key]
    return obj


def get_bed_center() -> Tuple:
    xmin, ymin, _, _ = query_printer_objects("toolhead", "axis_minimum")
    xmax, ymax, _, _ = query_printer_objects("toolhead", "axis_maximum")

    x = mean([xmin, xmax])
    y = mean([ymin, ymax])
    return (x, y)


def get_bed_corners() -> List:
    cfg = query_printer_objects("configfile", "config")
    x_offset = cfg["probe"]["x_offset"]
    y_offset = cfg["probe"]["y_offset"]
    xmin, ymin = cfg["bed_mesh"]["mesh_min"].split(", ")
    xmax, ymax = cfg["bed_mesh"]["mesh_max"].split(", ")

    xmin = float(xmin) - float(x_offset)
    ymin = float(ymin) - float(y_offset)
    xmax = float(xmax) - float(x_offset)
    ymax = float(ymax) - float(y_offset)

    return [(xmin, ymin), (xmin, ymax), (xmax, ymax), (xmax, ymin)]


def move_to_loc(x, y):
    send_gcode(f"G0 X{x} Y{y} F99999")


def get_gcode_response(count=1000):
    url = f"{MOONRAKER_URL}/server/gcode_store?count={count}"
    gcode_resp = get(url).json()["result"]["gcode_store"]
    return gcode_resp


def collect_data(probe_count, discard_first_sample=True, test=None):
    "Send probe_accuracy command, and retrieve data from gcod respond cache"
    start_time = get_gcode_response(count=1)[0]["time"]
    send_gcode(f"PROBE_ACCURACY SAMPLES={probe_count}")
    raw = get_gcode_response(count=1000)
    gcode_resp = [x for x in raw if x["time"] > start_time]
    msgs = [x["message"] for x in gcode_resp if x["message"].startswith("// probe at")]

    data = []
    for i, msg in enumerate(msgs):
        coor = re.findall(r"[\d]+.[\d]+", msg)
        x, y, z = [float(k) for k in coor]
        data.append({"test": test, "index": i, "x": x, "y": y, "z": z})

    if discard_first_sample:
        data.pop(0)
    return data


def test_probe(probe_count, loc=None, testname=""):
    if loc:
        move_to_loc(*loc)
    else:
        move_to_loc(*get_bed_center())

    df = pd.DataFrame(collect_data(probe_count, test=testname))
    return df


def now() -> str:
    "current time in string"
    return datetime.now().strftime("%Y%m%d_%H%M")


if __name__ == "__main__":
    main()
