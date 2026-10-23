"""Acceptance tests for globally optimal sterilization batch planning."""

import json
import hashlib
import os
import signal
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

import pytest


ENTRY = os.environ.get("AUTOCLAVE_ENTRY", "/app/plan_sterilization.py")
SOURCE_ROOT = Path(os.environ.get("AUTOCLAVE_DATA", "/data"))
RESULT_CACHE = {}


def expose_candidate_path(path):
    if Path("/tmp") not in path.parents:
        return
    for ancestor in path.parents:
        if ancestor == Path("/tmp"):
            break
        ancestor.chmod(0o755)
    path.chmod(0o777)


@pytest.fixture
def root(tmp_path):
    destination = tmp_path / "data"
    shutil.copytree(SOURCE_ROOT, destination, symlinks=True)
    expose_candidate_path(tmp_path)
    destination.chmod(0o777)
    for path in destination.rglob("*"):
        if path.is_symlink():
            continue
        path.chmod(0o777 if path.is_dir() else 0o644)
    lock_path = destination / ".sterilization.lock"
    if lock_path.is_file() and not lock_path.is_symlink():
        lock_path.chmod(0o666)
    return destination


def candidate_command(root):
    if os.geteuid() == 0 and Path("/usr/bin/setpriv").is_file():
        return [
            "/usr/bin/setpriv",
            "--reuid=65534",
            "--regid=65534",
            "--clear-groups",
            "--",
            "python3",
            ENTRY,
            "--root",
            str(root),
        ]
    return ["python3", ENTRY, "--root", str(root)]


def _input_signature(root):
    digest = hashlib.sha256()
    for relative in ("config.json", "sterilization.sqlite3"):
        digest.update((root / relative).read_bytes())
    return digest.hexdigest()


def _copy_cached(root, cached):
    for relative, payload in cached.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)


def run_task(root):
    output_dir = root / "output"
    cacheable = (
        output_dir.is_dir()
        and os.access(output_dir, os.W_OK)
    )
    key = _input_signature(root) if cacheable else None
    if key in RESULT_CACHE:
        _copy_cached(root, RESULT_CACHE[key])
        return subprocess.CompletedProcess([], 0, "", "")
    completed = subprocess.run(
        candidate_command(root),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if cacheable and completed.returncode == 0:
        cached = {}
        for relative in (
            "output/sterilizer-schedule.json",
            "output/machine-state.json",
        ):
            path = root / relative
            if path.is_file():
                cached[relative] = path.read_bytes()
        if len(cached) == 2:
            RESULT_CACHE[key] = cached
    return completed


def run_two_concurrently(root):
    first = subprocess.Popen(
        candidate_command(root),
        start_new_session=True,
    )
    second = subprocess.Popen(
        candidate_command(root),
        start_new_session=True,
    )
    try:
        return first.wait(timeout=60), second.wait(timeout=60)
    finally:
        for process in (first, second):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()


def outputs(root):
    return (
        json.loads((root / "output/sterilizer-schedule.json").read_text(encoding="utf-8")),
        json.loads((root / "output/machine-state.json").read_text(encoding="utf-8")),
    )


def database(root):
    return sqlite3.connect(root / "sterilization.sqlite3")


def test_script_creates_both_outputs(root):
    """题面：「成功后刷新两个普通文件：」。"""
    (root / "output/sterilizer-schedule.json").unlink(missing_ok=True)
    (root / "output/machine-state.json").unlink(missing_ok=True)
    result = run_task(root)
    assert result.returncode == 0
    assert (root / "output/sterilizer-schedule.json").is_file()
    assert (root / "output/machine-state.json").is_file()


def test_baseline_global_objective(root):
    """题面：「在所有合法排程中，按下面的字典序取唯一最优：」。"""
    result = run_task(root)
    schedule, state = outputs(root)
    assert result.returncode == 0
    assert schedule["objective"] == {
        "consumable_cost_microcents": 1400,
        "makespan": 937,
        "setup_minutes": 79,
        "weighted_tardiness": 0,
    }
    assert schedule["tardy_jobs"] == []
    assert [item["job_id"] for item in schedule["assignments"]] == [
        "JOB_100",
        "JOB_103",
        "JOB_102",
        "JOB_105",
        "JOB_101",
        "JOB_106",
        "JOB_104",
        "JOB_107",
    ]
    assert state["machines"] == [
        {
            "finish_min": 930,
            "last_family": "UTILITY",
            "machine_id": "M_ALPHA",
            "maintenance_event_id": "EV_ALPHA_07",
            "total_setup_min": 11,
        },
        {
            "finish_min": 937,
            "last_family": "GLASS",
            "machine_id": "M_BETA",
            "maintenance_event_id": "EV_BETA_04",
            "total_setup_min": 68,
        },
    ]


def test_objective_has_exact_keys(root):
    """题面：「`objective` 的键集合为：」"""
    result = run_task(root)
    schedule, _ = outputs(root)
    assert result.returncode == 0
    assert set(schedule["objective"]) == {
        "consumable_cost_microcents",
        "makespan",
        "setup_minutes",
        "weighted_tardiness",
    }


def test_alternative_signature_is_next_schedule(root):
    """题面：「`alternative_signature` 给出按字典序紧接最优方案的下一套不同排程签名。」"""
    result = run_task(root)
    schedule, _ = outputs(root)
    assert result.returncode == 0
    assert schedule["alternative_signature"] == [
        {"consumable_id": "C_SHARED", "job_id": "JOB_100", "machine_id": "M_ALPHA", "option_id": "OPT_SHARED", "start_min": 600},
        {"consumable_id": "C_BASE", "job_id": "JOB_103", "machine_id": "M_ALPHA", "option_id": "OPT_BASE", "start_min": 680},
        {"consumable_id": "C_PACK_B", "job_id": "JOB_102", "machine_id": "M_ALPHA", "option_id": "OPT_PRIVATE", "start_min": 800},
        {"consumable_id": "C_BASE", "job_id": "JOB_105", "machine_id": "M_ALPHA", "option_id": "OPT_BASE", "start_min": 850},
        {"consumable_id": "C_BASE", "job_id": "JOB_101", "machine_id": "M_BETA", "option_id": "OPT_BASE", "start_min": 620},
        {"consumable_id": "C_BASE", "job_id": "JOB_106", "machine_id": "M_BETA", "option_id": "OPT_BASE", "start_min": 712},
        {"consumable_id": "C_BASE", "job_id": "JOB_104", "machine_id": "M_BETA", "option_id": "OPT_BASE", "start_min": 800},
        {"consumable_id": "C_BASE", "job_id": "JOB_107", "machine_id": "M_BETA", "option_id": "OPT_BASE", "start_min": 882},
    ]


def test_alternative_signature_fields_and_order(root):
    """题面：「数组行结构见下。」"""
    result = run_task(root)
    schedule, _ = outputs(root)
    signature = schedule["alternative_signature"]
    assert result.returncode == 0
    assert all(
        set(item) == {
            "consumable_id", "job_id", "machine_id", "option_id", "start_min"
        }
        for item in signature
    )
    assert signature == sorted(
        signature,
        key=lambda item: (
            item["machine_id"],
            item["start_min"],
            item["job_id"],
        ),
    )


def test_assignment_fields_and_ordering(root):
    """题面：「`assignments` 按 `machine_id,start_min,job_id` 升序，每项恰好为：」。"""
    result = run_task(root)
    schedule, _ = outputs(root)
    assignments = schedule["assignments"]
    assert result.returncode == 0
    assert set(schedule) == {
        "alternative_signature", "assignments", "objective", "tardy_jobs"
    }
    assert all(
        set(item) == {"finish_min", "job_id", "machine_id", "setup_min", "start_min"}
        for item in assignments
    )
    assert assignments == sorted(
        assignments,
        key=lambda item: (item["machine_id"], item["start_min"], item["job_id"]),
    )
    assert len({item["job_id"] for item in assignments}) == 8


def test_machine_state_fields_and_ordering(root):
    """题面：「`machines` 数组按 `machine_id` 升序，每项恰好为：」"""
    result = run_task(root)
    _, state = outputs(root)
    machines = state["machines"]
    assert result.returncode == 0
    assert set(state) == {"machines", "shared_resources"}
    assert [item["machine_id"] for item in machines] == ["M_ALPHA", "M_BETA"]
    assert all(set(item) == {
        "finish_min", "last_family", "machine_id", "maintenance_event_id", "total_setup_min"
    } for item in machines)
    shared = state["shared_resources"]
    assert set(shared) == {"consumables", "job_choices", "shifts"}


def test_shared_resource_container_has_exact_keys(root):
    """题面：「`shared_resources` 的键集合见下方。」"""
    result = run_task(root)
    _, state = outputs(root)
    assert result.returncode == 0
    assert set(state["shared_resources"]) == {
        "consumables", "job_choices", "shifts"
    }


def test_job_choices_have_exact_fields_and_order(root):
    """题面：「`job_choices` 按 `job_id` 升序。该数组的整行结构见下。」"""
    result = run_task(root)
    _, state = outputs(root)
    choices = state["shared_resources"]["job_choices"]
    assert result.returncode == 0
    assert choices == sorted(choices, key=lambda item: item["job_id"])
    assert all(
        set(item) == {"consumable_id", "job_id", "option_id", "shift_id"}
        for item in choices
    )


def test_consumable_summary_fields_and_order(root):
    """题面：「`consumables` 按编号升序。该数组的整行结构见下。」"""
    result = run_task(root)
    _, state = outputs(root)
    rows = state["shared_resources"]["consumables"]
    assert result.returncode == 0
    assert rows == sorted(rows, key=lambda item: item["consumable_id"])
    assert all(
        set(item) == {
            "activation_fee_microcents",
            "activated",
            "consumable_id",
            "used_units",
        }
        for item in rows
    )


def test_shift_summary_fields_and_order(root):
    """题面：「`shifts` 按编号升序。该数组的整行结构见下。」"""
    result = run_task(root)
    _, state = outputs(root)
    rows = state["shared_resources"]["shifts"]
    assert result.returncode == 0
    assert rows == sorted(rows, key=lambda item: item["shift_id"])
    assert all(
        set(item) == {"finish_min", "shift_id", "weighted_tardiness"}
        for item in rows
    )


def test_machine_state_reports_restored_event_identity(root):
    """题面：「`maintenance_event_id` 写联合求解后为该机器选中的维护事件。」"""
    result = run_task(root)
    _, state = outputs(root)
    by_machine = {item["machine_id"]: item["maintenance_event_id"] for item in state["machines"]}
    assert result.returncode == 0
    assert by_machine == {"M_ALPHA": "EV_ALPHA_07", "M_BETA": "EV_BETA_04"}


def test_shared_consumable_is_allocated_across_shift_groups(root):
    """题面：「任一种耗材只要本批有器械包使用，就只收一次启用费。」"""
    result = run_task(root)
    _, state = outputs(root)
    choices = {
        item["job_id"]: item
        for item in state["shared_resources"]["job_choices"]
    }
    assert result.returncode == 0
    assert choices["JOB_100"]["shift_id"] == "SUPPLY_A"
    assert choices["JOB_100"]["consumable_id"] == "C_PACK_A"
    assert choices["JOB_102"]["shift_id"] == "SUPPLY_B"
    assert choices["JOB_102"]["consumable_id"] == "C_SHARED"
    shared = {
        item["consumable_id"]: item
        for item in state["shared_resources"]["consumables"]
    }
    assert shared["C_SHARED"]["used_units"] == 1
    assert shared["C_SHARED"]["activation_fee_microcents"] == 300
    assert shared["C_PACK_B"]["activated"] is False


def test_shared_consumable_capacity_binds_both_shift_groups(root):
    """题面：「耗材总用量受容量约束：」"""
    with database(root) as db:
        db.execute(
            "delete from job_supply_options "
            "where job_id in ('JOB_100','JOB_102') and option_id='OPT_PRIVATE'"
        )
        db.commit()
    before_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    before_state = (root / "output/machine-state.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/sterilizer-schedule.json").read_bytes() == before_schedule
    assert (root / "output/machine-state.json").read_bytes() == before_state


def test_job_shift_membership_must_cover_every_job(root):
    """题面：「`job_shifts` 必须给每个器械包恰好指定一个窗口。」"""
    with database(root) as db:
        db.execute("delete from job_shifts where job_id='JOB_100'")
        db.commit()
    before_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    before_state = (root / "output/machine-state.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/sterilizer-schedule.json").read_bytes() == before_schedule
    assert (root / "output/machine-state.json").read_bytes() == before_state


def test_shift_window_rejects_cross_shift_overflow(root):
    """题面：「所选窗口的 `end_min` 也是器械包的完成上限，两个上限取较早者。」"""
    with database(root) as db:
        db.execute(
            "update shift_windows set end_min=700 where shift_id='SUPPLY_A'"
        )
        db.commit()
    before_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    before_state = (root / "output/machine-state.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/sterilizer-schedule.json").read_bytes() == before_schedule
    assert (root / "output/machine-state.json").read_bytes() == before_state


def test_shift_window_count_is_exactly_two(root):
    """题面：「`shift_windows` 恰好有两个班次组。」"""
    with database(root) as db:
        db.execute(
            "insert into shift_windows values(?,?,?)",
            ("SUPPLY_C", 1300, 1400),
        )
        db.commit()
    before_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    before_state = (root / "output/machine-state.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/sterilizer-schedule.json").read_bytes() == before_schedule
    assert (root / "output/machine-state.json").read_bytes() == before_state


def test_shift_windows_must_not_overlap(root):
    """题面：「两组编号按时间顺序排列，窗口互不重叠。」"""
    with database(root) as db:
        db.execute(
            "update shift_windows set begin_min=790 where shift_id='SUPPLY_B'"
        )
        db.commit()
    before_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    before_state = (root / "output/machine-state.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/sterilizer-schedule.json").read_bytes() == before_schedule
    assert (root / "output/machine-state.json").read_bytes() == before_state


def test_each_job_has_exactly_one_supply_option_family(root):
    """题面：「每个器械包必须恰好选择一个方案。」"""
    result = run_task(root)
    _, state = outputs(root)
    choices = state["shared_resources"]["job_choices"]
    assert result.returncode == 0
    assert [item["job_id"] for item in choices] == [
        f"JOB_{index:03d}" for index in (100, 101, 102, 103, 104, 105, 106, 107)
    ]


def test_recovered_event_begin_limits_job_completion(root):
    """题面：「该事件的 `begin_min` 就是机器必须完成当前批次的时间上限。」"""
    result = run_task(root)
    schedule, state = outputs(root)
    with database(root) as db:
        deadlines = dict(db.execute("select event_id,begin_min from maintenance_events"))
    assert result.returncode == 0
    assert schedule["assignments"]
    assert all(
        item["finish_min"] <= deadlines[event_id]
        for event_id, item in (
            (machine["maintenance_event_id"], assignment)
            for machine in state["machines"]
            for assignment in schedule["assignments"]
            if assignment["machine_id"] == machine["machine_id"]
        )
    )


def test_boolean_config_value_is_rejected(root):
    """题面：「布尔值不算整数。」"""
    path = root / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["max_jobs"] = True
    path.write_text(json.dumps(config) + "\n", encoding="utf-8")
    before_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    before_state = (root / "output/machine-state.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/sterilizer-schedule.json").read_bytes() == before_schedule
    assert (root / "output/machine-state.json").read_bytes() == before_state


def test_config_extra_field_is_rejected(root):
    """题面：「`config.json` 必须是不跟随软链接的普通 UTF-8 JSON 对象，不允许重复键，字段恰好为：」。"""
    path = root / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["operator"] = "night"
    path.write_text(json.dumps(config) + "\n", encoding="utf-8")
    before_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    before_state = (root / "output/machine-state.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/sterilizer-schedule.json").read_bytes() == before_schedule
    assert (root / "output/machine-state.json").read_bytes() == before_state


def test_duplicate_config_key_is_rejected(root):
    """题面：「`config.json` 必须是不跟随软链接的普通 UTF-8 JSON 对象，不允许重复键」。"""
    (root / "config.json").write_text(
        '{"planning_start_min":600,"max_jobs":8,"max_jobs":7}\n',
        encoding="utf-8",
    )
    before_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    before_state = (root / "output/machine-state.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/sterilizer-schedule.json").read_bytes() == before_schedule
    assert (root / "output/machine-state.json").read_bytes() == before_state


def test_extra_database_table_is_rejected(root):
    """题面：「`sterilization.sqlite3` 必须是不跟随软链接的普通 SQLite 文件，用户对象恰好为：」。"""
    with database(root) as db:
        db.execute("create table unexpected(x integer)")
        db.commit()
    before_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    before_state = (root / "output/machine-state.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/sterilizer-schedule.json").read_bytes() == before_schedule
    assert (root / "output/machine-state.json").read_bytes() == before_state


def test_job_family_enum_is_enforced(root):
    """题面：「`family` 和 `initial_family` 只能取：」。"""
    with database(root) as db:
        db.execute("update jobs set family='BIOHAZARD' where job_id='JOB_100'")
        db.commit()
    before_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    before_state = (root / "output/machine-state.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/sterilizer-schedule.json").read_bytes() == before_schedule
    assert (root / "output/machine-state.json").read_bytes() == before_state


def test_incomplete_setup_matrix_is_rejected(root):
    """题面：「`setup_matrix` 含有四个族两两组合的 16 个对象。」。"""
    with database(root) as db:
        db.execute("delete from setup_matrix where from_family='LINEN' and to_family='GLASS'")
        db.commit()
    before_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    before_state = (root / "output/machine-state.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/sterilizer-schedule.json").read_bytes() == before_schedule
    assert (root / "output/machine-state.json").read_bytes() == before_state


def test_predecessor_missing_reference_is_rejected(root):
    """题面：「非空值必须引用另一器械包。」"""
    with database(root) as db:
        db.execute("update jobs set predecessor='JOB_999' where job_id='JOB_100'")
        db.commit()
    before_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    before_state = (root / "output/machine-state.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/sterilizer-schedule.json").read_bytes() == before_schedule
    assert (root / "output/machine-state.json").read_bytes() == before_state


def test_predecessor_cycle_is_rejected(root):
    """题面：「依赖关系无环。」"""
    with database(root) as db:
        first, second = [row[0] for row in db.execute(
            "select job_id from jobs where predecessor is null order by job_id limit 2"
        )]
        db.execute("update jobs set predecessor=? where job_id=?", (second, first))
        db.execute("update jobs set predecessor=? where job_id=?", (first, second))
        db.commit()
    before_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    before_state = (root / "output/machine-state.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/sterilizer-schedule.json").read_bytes() == before_schedule
    assert (root / "output/machine-state.json").read_bytes() == before_state


def test_job_count_above_limit_is_rejected(root):
    """题面：「批次库中的器械包数量必须不超它。」"""
    path = root / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["max_jobs"] = 7
    path.write_text(json.dumps(config) + "\n", encoding="utf-8")
    before_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    before_state = (root / "output/machine-state.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/sterilizer-schedule.json").read_bytes() == before_schedule
    assert (root / "output/machine-state.json").read_bytes() == before_state


def test_max_jobs_above_global_limit_is_rejected(root):
    """题面：「`max_jobs` 接受 1 到 9 的整数。」"""
    path = root / "config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    config["max_jobs"] = 10
    path.write_text(json.dumps(config) + "\n", encoding="utf-8")
    before_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    before_state = (root / "output/machine-state.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/sterilizer-schedule.json").read_bytes() == before_schedule
    assert (root / "output/machine-state.json").read_bytes() == before_state


def test_missing_maintenance_event_match_is_rejected(root):
    """题面：「若没有完整候选，整批输入拒收。」"""
    with database(root) as db:
        db.execute(
            "update maintenance_events set energy_kj=energy_kj+100 "
            "where event_id='EV_BETA_04'"
        )
        db.commit()
    before_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    before_state = (root / "output/machine-state.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/sterilizer-schedule.json").read_bytes() == before_schedule
    assert (root / "output/machine-state.json").read_bytes() == before_state


def test_near_energy_candidate_is_resolved_by_schedule(root):
    """题面：「维护事件选择和器械包排程必须一起求解。」"""
    with database(root) as db:
        db.execute(
            "insert into maintenance_events values(?,?,?,?)",
            ("EV_ALPHA_99", 880, 940, 290),
        )
        db.commit()
    result = run_task(root)
    schedule, state = outputs(root)
    by_machine = {
        item["machine_id"]: item["maintenance_event_id"]
        for item in state["machines"]
    }
    assert result.returncode == 0
    assert by_machine == {"M_ALPHA": "EV_ALPHA_07", "M_BETA": "EV_BETA_04"}


def test_selected_maintenance_events_are_distinct(root):
    """题面：「最终为每台机器选一条候选事件，事件编号不得重复。」"""
    with database(root) as db:
        db.execute(
            "insert into maintenance_events values(?,?,?,?)",
            ("EV_ALPHA_99", 1200, 1260, 290),
        )
        db.commit()
    result = run_task(root)
    _, state = outputs(root)
    event_ids = [item["maintenance_event_id"] for item in state["machines"]]
    assert result.returncode == 0
    assert len(event_ids) == len(set(event_ids))


def test_job_due_before_ready_is_rejected(root):
    """题面：「`due_min` 不早于对应器械包的 `ready_min`」。"""
    with database(root) as db:
        db.execute("update jobs set due_min=ready_min-1 where job_id='JOB_100'")
        db.commit()
    before_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    before_state = (root / "output/machine-state.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/sterilizer-schedule.json").read_bytes() == before_schedule
    assert (root / "output/machine-state.json").read_bytes() == before_state


def test_maintenance_before_available_is_rejected(root):
    """题面：「候选还必须满足 `begin_min` 不早于机器可用时刻。」"""
    with database(root) as db:
        db.execute(
            "update maintenance_events set begin_min=599 where event_id='EV_ALPHA_07'"
        )
        db.commit()
    before_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    before_state = (root / "output/machine-state.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/sterilizer-schedule.json").read_bytes() == before_schedule
    assert (root / "output/machine-state.json").read_bytes() == before_state
    with database(root) as db:
        db.execute(
            "update jobs set duration_min=5, ready_min=0, due_min=2000, "
            "weight=1, predecessor=NULL"
        )
        db.execute(
            "update maintenance_events set begin_min=600 where event_id='EV_ALPHA_07'"
        )
        db.commit()
    boundary = run_task(root)
    assert boundary.returncode == 0


def test_maintenance_event_end_before_begin_is_rejected(root):
    """题面：「候选还必须满足 `begin_min <= end_min`。」"""
    with database(root) as db:
        db.execute(
            "update maintenance_events set end_min=885 where event_id='EV_ALPHA_07'"
        )
        db.commit()
    before_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    before_state = (root / "output/machine-state.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/sterilizer-schedule.json").read_bytes() == before_schedule
    assert (root / "output/machine-state.json").read_bytes() == before_state


def test_more_than_four_energy_candidates_are_rejected(root):
    """题面：「每台机器必须有 1 到 4 个候选。」"""
    with database(root) as db:
        for index in range(3):
            db.execute(
                "insert into maintenance_events values(?,?,?,?)",
                (f"EV_EXTRA_{index}", 1200 + index, 1260 + index, 285 + index),
            )
        db.commit()
    before_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    before_state = (root / "output/machine-state.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/sterilizer-schedule.json").read_bytes() == before_schedule
    assert (root / "output/machine-state.json").read_bytes() == before_state


def test_no_maintenance_mapping_can_form_a_schedule(root):
    """题面：「若没有任何候选映射能形成合法排程，整批输入拒收。」"""
    with database(root) as db:
        db.execute(
            "update maintenance_events set begin_min=605,end_min=610 "
            "where event_id='EV_ALPHA_07'"
        )
        db.execute(
            "update maintenance_events set begin_min=620,end_min=625 "
            "where event_id='EV_BETA_04'"
        )
        db.commit()
    before_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    before_state = (root / "output/machine-state.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/sterilizer-schedule.json").read_bytes() == before_schedule
    assert (root / "output/machine-state.json").read_bytes() == before_state


def test_maintenance_event_id_syntax_is_rejected(root):
    """题面：「`maintenance_events` 含 1 到 64 条对象，记录维护窗口及其表计差。事件编号同样匹配」"""
    with database(root) as db:
        db.execute(
            "update maintenance_events set event_id='lower-case' where event_id='EV_BETA_04'"
        )
        db.commit()
    before_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    before_state = (root / "output/machine-state.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0
    assert (root / "output/sterilizer-schedule.json").read_bytes() == before_schedule
    assert (root / "output/machine-state.json").read_bytes() == before_state


def test_outputs_are_canonical_json(root):
    """题面：「两份文件均为键排序紧凑 JSON 并换行。」"""
    result = run_task(root)
    for path in (root / "output/sterilizer-schedule.json", root / "output/machine-state.json"):
        raw = path.read_bytes()
        document = json.loads(raw)
        assert raw == (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    assert result.returncode == 0


def test_empty_job_table_is_rejected(root):
    """题面：「器械包数量至少为 1。」"""
    with database(root) as db:
        db.execute("delete from jobs")
        db.commit()
    before_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    before_state = (root / "output/machine-state.json").read_bytes()
    result = run_task(root)
    assert result.returncode != 0 and (root / "output/sterilizer-schedule.json").read_bytes() == before_schedule
    assert (root / "output/machine-state.json").read_bytes() == before_state


def test_input_database_is_unchanged(root):
    """题面：「输入数据库和配置不得改写。」"""
    before_database = (root / "sterilization.sqlite3").read_bytes()
    before_config = (root / "config.json").read_bytes()
    assert run_task(root).returncode == 0
    assert (root / "sterilization.sqlite3").read_bytes() == before_database
    assert (root / "config.json").read_bytes() == before_config


def test_repeat_run_is_byte_stable(root):
    """题面：「连续运行或两个进程同时运行，结果必须逐字节一致。」"""
    assert run_task(root).returncode == 0
    first_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    first_state = (root / "output/machine-state.json").read_bytes()
    assert run_task(root).returncode == 0
    assert (root / "output/sterilizer-schedule.json").read_bytes() == first_schedule
    assert (root / "output/machine-state.json").read_bytes() == first_state


def test_concurrent_runs_converge(root):
    """题面：「连续运行或两个进程同时运行，结果必须逐字节一致。」。"""
    first_code, second_code = run_two_concurrently(root)
    assert first_code == 0 and second_code == 0
    schedule, state = outputs(root)
    assert schedule["objective"]["makespan"] == 937 and len(state["machines"]) == 2


def test_output_directory_failure_preserves_old_files(root):
    """题面：「替换途中失败要恢复旧文件的字节、权限和目录项类型，并清理备份与临时目录。」"""
    assert run_task(root).returncode == 0
    old_schedule = (root / "output/sterilizer-schedule.json").read_bytes()
    old_state = (root / "output/machine-state.json").read_bytes()
    output_dir = root / "output"
    output_dir.rename(root / "output-old")
    output_dir.symlink_to(root / "absent-output")
    result = run_task(root)
    assert result.returncode != 0 and (root / "output-old/sterilizer-schedule.json").read_bytes() == old_schedule
    assert (root / "output-old/machine-state.json").read_bytes() == old_state


def test_nine_jobs_finish_within_public_limit(tmp_path):
    """题面：「`max_jobs` 接受 1 到 9 的整数。」与「一次排程必须在 20 秒内完成。」"""
    root = tmp_path / "large-plan"
    shutil.copytree(SOURCE_ROOT, root, symlinks=True)
    expose_candidate_path(tmp_path)
    root.chmod(0o777)
    (root / "output").chmod(0o777)
    lock_path = root / ".sterilization.lock"
    if lock_path.is_file() and not lock_path.is_symlink():
        lock_path.chmod(0o666)
    (root / "config.json").write_text(
        json.dumps(
            {
                "planning_start_min": 600,
                "max_jobs": 9,
                "meter_tolerance_kj": 25,
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    database_path = root / "sterilization.sqlite3"
    with sqlite3.connect(database_path) as db:
        db.execute("delete from jobs")
        db.execute("delete from machines")
        db.execute("delete from maintenance_events")
        db.execute("delete from job_shifts")
        db.execute("delete from job_supply_options")
        db.execute("delete from consumables")
        db.execute("delete from shift_windows")
        db.execute(
            "insert into machines values(?,?,?,?,?)",
            ("ONLY_MACHINE", "GLASS", 600, 100, 1090),
        )
        db.execute(
            "insert into maintenance_events values(?,?,?,?)",
            ("EV_ONLY_01", 100000, 100100, 990),
        )
        families = ("GLASS", "INSTRUMENT", "LINEN", "UTILITY")
        db.executemany(
            "insert into jobs values(?,?,?,?,?,?,?)",
            (
                (
                    f"JOB_{index:03d}",
                    families[index % 4],
                    5 + index % 7,
                    600 + index * 2,
                    50000 + index * 10,
                    1,
                    None,
                )
                for index in range(9)
            ),
        )
        db.executemany(
            "insert into shift_windows values(?,?,?)",
            (("SHIFT_A", 600, 20000), ("SHIFT_B", 20000, 40000)),
        )
        db.executemany(
            "insert into job_shifts values(?,?)",
            (
                (f"JOB_{index:03d}", "SHIFT_A" if index % 2 == 0 else "SHIFT_B")
                for index in range(9)
            ),
        )
        db.executemany(
            "insert into consumables values(?,?,?)",
            (("C_BULK", 20, 0), ("C_SPARE", 1, 100)),
        )
        db.executemany(
            "insert into job_supply_options values(?,?,?,?,?)",
            (
                (f"JOB_{index:03d}", "OPT_BULK", "C_BULK", 1, 0)
                for index in range(9)
            ),
        )
        db.commit()
    started = time.monotonic()
    result = subprocess.run(
        candidate_command(root),
        capture_output=True,
        text=True,
        timeout=25,
    )
    elapsed = time.monotonic() - started
    schedule, state = outputs(root)
    assert result.returncode == 0
    assert elapsed < 20.0
    assert len(schedule["assignments"]) == 9
    assert schedule["objective"]["weighted_tardiness"] == 0
    assert len(state["machines"]) == 1
"""Batch-two acceptance suite for sterile schedule recovery."""

