#!/usr/bin/env python3

# Automating probe_accuracy testing
# The following three tests will be done:
# 0) 20 tests, 5 samples at bed center - check consistency within normal measurements
# 1) 1 test, 100 samples at bed center - check for drift
# 2) 1 test, 30 samples at each bed mesh corners - check if there are issues with individual z drives
# Notes:
# * First probe measurements are dropped

import argparse
import re
import os
import math
import sys
from datetime import datetime

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from requests import get, post
from typing import Tuple, List, Dict

from scipy import rand

MOONRAKER_URL = "http://localhost:7125"
KLIPPY_LOG = "/home/pi/klipper_logs/klippy.log"
DATA_DIR = "/home/pi/probe_accuracy_results"
RUNID = datetime.now().strftime("%Y%m%d_%H%M")


def main(corner, repeatability, drift, export_csv, force_dock):
    if not os.path.exists(DATA_DIR):
        os.mkdir(DATA_DIR)
    try:
        homing()
        level_bed()
        move_to_safe_z()
        if not any([corner, repeatability, drift]):
            corner = 30
            repeatability = 20
            drift = 100
            print("Running all tests")
        test_routine(corner, repeatability, drift, export_csv, force_dock)
    except KeyboardInterrupt:
        pass
    send_gcode("DOCK_PROBE_UNLOCK")


def test_routine(corner, repeatability, drift, export_csv, force_dock):
    dfs = []
    if corner:
        dfs.append(test_corners(n=corner, force_dock=force_dock))
    if repeatability:
        dfs.append(
            test_repeatability(
                test_count=repeatability, probe_count=10, force_dock=force_dock
            )
        )
    if drift:
        dfs.append(test_drift(n=drift))
    df = pd.concat(dfs, axis=0, ignore_index=True).sort_index()

    file_nm = f"probe_accuracy_test_{RUNID}"
    summary = df.groupby("test")["z"].agg(
        ["min", "max", "first", "last", "mean", "std", "count"]
    )
    summary["range"] = summary["max"] - summary["min"]
    summary["drift"] = summary["last"] - summary["first"]

    if export_csv:
        df.to_csv(DATA_DIR + "/" + file_nm + ".csv", index=False)
        summary.to_csv(f"{DATA_DIR}/{file_nm}_summary.csv")


def test_drift(n=100):
    print(f"\nTake {n} samples in a row to check for drift")
    df = test_probe(probe_count=n, testname=f"center {n}samples")
    summary = df.groupby("test")["z"].agg(
        ["min", "max", "first", "last", "mean", "std", "count"]
    )
    summary["range"] = summary["max"] - summary["min"]
    summary["drift"] = summary["last"] - summary["first"]
    print(summary)
    ax = df.plot.scatter(x="index", y="z", title=f"Drift test (n={n})")
    ax.figure.savefig(f"{DATA_DIR}/probe_accuracy_{RUNID}_drift.png")
    return df


def random_loc(margin=50):
    xmin, ymin, _, _ = query_printer_objects("toolhead", "axis_minimum")
    xmax, ymax, _, _ = query_printer_objects("toolhead", "axis_maximum")
    cfg = query_printer_objects("configfile", "config")

    x = np.random.random() * (xmax - xmin - margin) + margin
    y = np.random.random() * (ymax - ymin - margin) + margin
    return x, y


def test_repeatability(test_count=10, probe_count=10, force_dock=False):
    if not force_dock:
        send_gcode("ATTACH_PROBE_LOCK")

    print(f"\nTake {test_count} probe_accuracy tests to check for repeatability")
    dfs = []
    print("Test number: ", end="", flush=True)
    for i in range(test_count):
        move_to_loc(*random_loc())
        move_to_loc(*get_bed_center())
        send_gcode(f"M117 repeatability test {i+1}/{test_count}")
        print(f"{test_count - i}...", end="", flush=True)
        df = test_probe(probe_count, testname=f"{i+1:02d}: center {probe_count}samples")
        df["measurement"] = f"Test #{i+1:02d}"
        dfs.append(df)
    print("Done")
    if not force_dock:
        send_gcode("DOCK_PROBE_UNLOCK")

    df = pd.concat(dfs, axis=0).sort_index()
    summary = df.groupby("test")["z"].agg(
        ["min", "max", "first", "last", "mean", "std", "count"]
    )
    summary["range"] = summary["max"] - summary["min"]
    summary["drift"] = summary["last"] - summary["first"]
    print(summary)
    ax = df.boxplot(column="z", by="measurement", rot=45, fontsize=8)
    plot_nm = f"probe_accuracy_{RUNID}_repeatability"
    plt.title(plot_nm)
    plt.suptitle("")
    ax.figure.savefig(DATA_DIR + "/" + plot_nm + ".png")
    plot_repeatability(df, plot_nm=f"{plot_nm}\n{probe_count} samples")
    return df


def test_corners(n=30, force_dock=False):
    print(
        "\nTest probe around the bed to see if there are issues with individual drives"
    )
    level_bed(force=True)
    if not force_dock:
        send_gcode("ATTACH_PROBE_LOCK")
    dfs = []
    for i, xy in enumerate(get_bed_corners()):
        xy_txt = f"({xy[0]:.0f}, {xy[1]:.0f})"
        send_gcode(f"M117 corner test {i+1}/4")
        print(f"{4-i}...", end="", flush=True)
        df = test_probe(
            probe_count=n,
            loc=xy,
            testname=f"{i+1}:corner {n}samples {xy_txt}",
        )
        df["measurement"] = f"{i+1}: {xy_txt}"
        dfs.append(df)
    print("Done")
    if not force_dock:
        send_gcode("DOCK_PROBE_UNLOCK")
    df = pd.concat(dfs, axis=0)
    summary = df.groupby("test")["z"].agg(
        ["min", "max", "first", "last", "mean", "std", "count"]
    )
    summary["range"] = summary["max"] - summary["min"]
    summary["drift"] = summary["last"] - summary["first"]
    print(summary)
    ax = df.boxplot(column="z", by="measurement", rot=45, fontsize=8)
    plot_nm = f"probe_accuracy_{RUNID}_corners"
    plt.title(plot_nm)
    plt.suptitle("")
    ax.figure.savefig(DATA_DIR + "/" + plot_nm + ".png")
    plot_repeatability(df, plot_nm=f"{plot_nm}\n{n} samples", cols=2, sharey=False)
    return df


def plot_repeatability(df, cols=5, plot_nm=None, sharey=True):
    dfg = df.groupby("measurement")
    rows = math.ceil(dfg.ngroups / cols)
    fig, axs = plt.subplots(rows, cols, sharex=True, sharey=sharey, figsize=(15, 10))
    ylim = (df["z"].min() - 0.001, df["z"].max() + 0.001)

    i, j = 0, 0
    for test, df in dfg:
        ax = axs[i][j] if rows > 1 else axs[j]
        x, y = df["index"], df["z"]
        ax.scatter(x, y)
        ax.hlines(y.median(), x.min(), x.max())
        if sharey:
            ax.set_ylim(*ylim)
        ax.set_title(test)
        j += 1
        if j == cols:
            i += 1
            j = 0

    for ax in axs.flat:
        ax.set(xlabel="probe sample", ylabel="z")
        ax.label_outer()

    fig.suptitle(plot_nm)
    fig.tight_layout()
    plot_nm = plot_nm.split("\n")[0]
    fig.savefig(f"{DATA_DIR}/{plot_nm}.png")


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


def level_bed(force=False) -> None:
    """Level bed if not done already"""
    cfg = query_printer_objects("configfile", "config")

    ztilt = cfg.get("z_tilt")
    qgl = cfg.get("quad_gantry_level")

    if ztilt:
        gcode = "z_tilt_adjust"
        leveled = query_printer_objects("z_tilt", "applied")
    elif qgl:
        gcode = "quad_gantry_level"
        leveled = query_printer_objects("quad_gantry_level", "applied")
    else:
        print(
            "User has no leveling gcode. Please check printer.cfg [z_tilt] or [quad_gantry_level]"
        )
        print("Skip leveling...")

    if (not leveled) or force:
        print("Leveling")
        send_gcode(gcode)


def move_to_safe_z():
    safe_z = query_printer_objects("gcode_macro _User_Variables", "safe_z")
    if not safe_z:
        print("Safe z has not been set in klicky-variables")
        safe_z = input("Enter safe z height to avoid crash:")

    send_gcode(f"G1 Z{safe_z}")


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

    x = np.mean([xmin, xmax])
    y = np.mean([ymin, ymax])
    return (x, y)


def get_bed_corners() -> List:
    cfg = query_printer_objects("configfile", "config")
    x_offset = cfg["probe"]["x_offset"]
    y_offset = cfg["probe"]["y_offset"]

    xmin, ymin = re.findall(r"[\d.]+", cfg["bed_mesh"]["mesh_min"])
    xmax, ymax = re.findall(r"[\d.]+", cfg["bed_mesh"]["mesh_max"])

    xmin = float(xmin) - float(x_offset)
    ymin = float(ymin) - float(y_offset)
    xmax = float(xmax) - float(x_offset)
    ymax = float(ymax) - float(y_offset)

    return [(xmin, ymax), (xmax, ymax), (xmin, ymin), (xmax, ymin)]


def move_to_loc(x, y, echo=False):
    gcode = f"G0 X{x} Y{y} F99999"
    if echo:
        print(gcode)
        send_gcode(f"M118 {gcode}")
    send_gcode("G90")
    send_gcode(gcode)


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

    err_msgs = [x["message"] for x in gcode_resp if x["message"].startswith("!!")]
    msgs = [x["message"] for x in gcode_resp if x["message"].startswith("// probe at")]

    if len(err_msgs):
        print("\nSomething's wrong with probe_accuracy! Klipper response:")
        for msg in set(err_msgs):
            print(msg)

    data = []
    for i, msg in enumerate(msgs):
        coor = re.findall(r"[\d.]+", msg)
        x, y, z = [float(k) for k in coor]
        data.append({"test": test, "index": i, "x": x, "y": y, "z": z})

    if len(data) == 0:
        print("No measurements collected")
        print("Exiting!")
        sys.exit(1)

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


class GcodeError(Exception):
    pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="""Automated probe testing. 
    All three tests will run at default values unless individual tests are specified"""
    )
    ap.add_argument(
        "--corner",
        nargs="?",
        type=int,
        help="Enable corner test. Number of probe samples at each corner can be optionally provided. Default 30.",
    )
    ap.add_argument(
        "--repeatability",
        nargs="?",
        type=int,
        help="Enable corner test. Number of probe_accuracy tests can be optionally provided. Default 20.",
    )
    ap.add_argument(
        "--drift",
        nargs="?",
        type=int,
        help="Enable drift test. Number of probe_accuracy samples can be optionally provided. Default 100.",
    )
    ap.add_argument(
        "--export_csv",
        action="store_true",
        help="export data as csv",
    )
    ap.add_argument(
        "--force_dock",
        action="store_true",
        help="Force docking between tests. Default False",
    )

    args = vars(ap.parse_args())
    main(**args)
