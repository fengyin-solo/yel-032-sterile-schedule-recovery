#!/usr/bin/env python3
import json
import sqlite3
import sys
from pathlib import Path


root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/data")
(root / "output").mkdir(parents=True, exist_ok=True)
(root / "sterilization.sqlite3").unlink(missing_ok=True)
database = sqlite3.connect(root / "sterilization.sqlite3")
database.executescript(
    """
    CREATE TABLE jobs(
        job_id TEXT PRIMARY KEY,
        family TEXT NOT NULL,
        duration_min INTEGER NOT NULL,
        ready_min INTEGER NOT NULL,
        due_min INTEGER NOT NULL,
        weight INTEGER NOT NULL,
        predecessor TEXT
    );
    CREATE TABLE machines(
        machine_id TEXT PRIMARY KEY,
        initial_family TEXT NOT NULL,
        available_min INTEGER NOT NULL,
        meter_before_kj INTEGER NOT NULL,
        meter_after_kj INTEGER NOT NULL
    );
    CREATE TABLE maintenance_events(
        event_id TEXT PRIMARY KEY,
        begin_min INTEGER NOT NULL,
        end_min INTEGER NOT NULL,
        energy_kj INTEGER NOT NULL
    );
    CREATE TABLE setup_matrix(
        from_family TEXT NOT NULL,
        to_family TEXT NOT NULL,
        minutes INTEGER NOT NULL,
        PRIMARY KEY(from_family, to_family)
    );
    CREATE TABLE consumables(
        consumable_id TEXT PRIMARY KEY,
        capacity_units INTEGER NOT NULL,
        activation_fee_microcents INTEGER NOT NULL
    );
    CREATE TABLE job_shifts(
        job_id TEXT PRIMARY KEY,
        shift_id TEXT NOT NULL
    );
    CREATE TABLE job_supply_options(
        job_id TEXT NOT NULL,
        option_id TEXT NOT NULL,
        consumable_id TEXT NOT NULL,
        units INTEGER NOT NULL,
        extra_min INTEGER NOT NULL,
        PRIMARY KEY(job_id, option_id)
    );
    CREATE TABLE shift_windows(
        shift_id TEXT PRIMARY KEY,
        begin_min INTEGER NOT NULL,
        end_min INTEGER NOT NULL
    );
    """
)

jobs = [
    ("JOB_100", "INSTRUMENT", 80, 600, 860, 10, None),
    ("JOB_101", "LINEN", 70, 620, 910, 8, None),
    ("JOB_102", "UTILITY", 50, 640, 1080, 6, None),
    ("JOB_103", "INSTRUMENT", 90, 600, 930, 5, None),
    ("JOB_104", "LINEN", 60, 650, 1090, 9, "JOB_100"),
    ("JOB_105", "UTILITY", 75, 610, 1100, 4, "JOB_102"),
    ("JOB_106", "GLASS", 45, 630, 940, 7, None),
    ("JOB_107", "GLASS", 55, 700, 1100, 3, "JOB_104"),
]
machines = [
    ("M_ALPHA", "INSTRUMENT", 600, 1000, 1290),
    ("M_BETA", "LINEN", 615, 820, 1100),
]
maintenance_events = [
    ("EV_ALPHA_07", 1100, 1160, 290),
    ("EV_BETA_04", 1110, 1170, 280),
    ("EV_ARCHIVE_03", 780, 830, 137),
    ("EV_ARCHIVE_08", 910, 960, 164),
    ("EV_ARCHIVE_11", 1140, 1200, 211),
    ("EV_ARCHIVE_15", 1260, 1325, 330),
    ("EV_ARCHIVE_21", 1400, 1460, 357),
    ("EV_ARCHIVE_26", 1520, 1590, 418),
    ("EV_ARCHIVE_31", 1650, 1710, 472),
    ("EV_ARCHIVE_36", 1800, 1860, 529),
]
families = ["GLASS", "INSTRUMENT", "LINEN", "UTILITY"]
setup = {
    ("GLASS", "INSTRUMENT"): 18,
    ("GLASS", "LINEN"): 24,
    ("GLASS", "UTILITY"): 14,
    ("INSTRUMENT", "GLASS"): 16,
    ("INSTRUMENT", "LINEN"): 27,
    ("INSTRUMENT", "UTILITY"): 11,
    ("LINEN", "GLASS"): 22,
    ("LINEN", "INSTRUMENT"): 30,
    ("LINEN", "UTILITY"): 19,
    ("UTILITY", "GLASS"): 13,
    ("UTILITY", "INSTRUMENT"): 12,
    ("UTILITY", "LINEN"): 21,
}
database.executemany("INSERT INTO jobs VALUES(?,?,?,?,?,?,?)", jobs)
database.executemany("INSERT INTO machines VALUES(?,?,?,?,?)", machines)
database.executemany("INSERT INTO maintenance_events VALUES(?,?,?,?)", maintenance_events)
database.executemany(
    "INSERT INTO setup_matrix VALUES(?,?,?)",
    [(source, target, 0 if source == target else setup[(source, target)])
     for source in families for target in families],
)
consumables = [
    ("C_BASE", 10, 500),
    ("C_SHARED", 1, 300),
    ("C_PACK_A", 1, 600),
    ("C_PACK_B", 1, 600),
]
job_shifts = [
    ("JOB_100", "SUPPLY_A"),
    ("JOB_101", "SUPPLY_A"),
    ("JOB_103", "SUPPLY_A"),
    ("JOB_106", "SUPPLY_A"),
    ("JOB_102", "SUPPLY_B"),
    ("JOB_104", "SUPPLY_B"),
    ("JOB_105", "SUPPLY_B"),
    ("JOB_107", "SUPPLY_B"),
]
shift_windows = [
    ("SUPPLY_A", 600, 800),
    ("SUPPLY_B", 800, 1100),
]
job_supply_options = [
    ("JOB_100", "OPT_SHARED", "C_SHARED", 1, 0),
    ("JOB_100", "OPT_PRIVATE", "C_PACK_A", 1, 15),
    ("JOB_102", "OPT_SHARED", "C_SHARED", 1, 5),
    ("JOB_102", "OPT_PRIVATE", "C_PACK_B", 1, 0),
    ("JOB_101", "OPT_BASE", "C_BASE", 1, 0),
    ("JOB_103", "OPT_BASE", "C_BASE", 1, 0),
    ("JOB_104", "OPT_BASE", "C_BASE", 1, 0),
    ("JOB_105", "OPT_BASE", "C_BASE", 1, 0),
    ("JOB_106", "OPT_BASE", "C_BASE", 1, 0),
    ("JOB_107", "OPT_BASE", "C_BASE", 1, 0),
]
database.executemany("INSERT INTO consumables VALUES(?,?,?)", consumables)
database.executemany("INSERT INTO job_shifts VALUES(?,?)", job_shifts)
database.executemany("INSERT INTO shift_windows VALUES(?,?,?)", shift_windows)
database.executemany(
    "INSERT INTO job_supply_options VALUES(?,?,?,?,?)",
    job_supply_options,
)
database.commit()
database.close()

(root / "config.json").write_text(
    json.dumps(
        {
            "max_jobs": 8,
            "meter_tolerance_kj": 25,
            "planning_start_min": 600,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    + "\n"
)
(root / "output/sterilizer-schedule.json").write_text('{"stale":true}\n')
(root / "output/machine-state.json").write_text('{"stale":true}\n')
(root / "DATA_SOURCE.txt").write_text(
    "来源类型：人工构造的仿真生成现场数据。该生成脚本用固定随机种子 73400321 保持演练样本可复现；"
    "八件器械包分布在玻璃、器械、布类和公用器皿四族，处理时长、准备时点、截止权重和前后依赖按夜班交接场景人工编排，"
    "两台灭菌机的初始族、可用时刻与维护窗口分别不同；两个供应组共享同一批包装耗材和维护资源，"
    "耗材余量与一次启用费使各组不能分别取局部最优后直接拼接，不代表真实医院记录。\n"
)
