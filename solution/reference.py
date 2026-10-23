#!/usr/bin/env python3
# Batch-two variant of the sterilization scheduling solver.
import argparse
import fcntl
import itertools
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
from pathlib import Path


ID = re.compile(r"[A-Z][A-Z0-9_-]{0,39}")
FAMILIES = {"GLASS", "INSTRUMENT", "LINEN", "UTILITY"}
SCHEMA = {
    "consumables": [
        ("consumable_id", "TEXT", 0, 1),
        ("capacity_units", "INTEGER", 1, 0),
        ("activation_fee_microcents", "INTEGER", 1, 0),
    ],
    "jobs": [
        ("job_id", "TEXT", 0, 1),
        ("family", "TEXT", 1, 0),
        ("duration_min", "INTEGER", 1, 0),
        ("ready_min", "INTEGER", 1, 0),
        ("due_min", "INTEGER", 1, 0),
        ("weight", "INTEGER", 1, 0),
        ("predecessor", "TEXT", 0, 0),
    ],
    "job_shifts": [
        ("job_id", "TEXT", 0, 1),
        ("shift_id", "TEXT", 1, 0),
    ],
    "job_supply_options": [
        ("job_id", "TEXT", 1, 1),
        ("option_id", "TEXT", 1, 2),
        ("consumable_id", "TEXT", 1, 0),
        ("units", "INTEGER", 1, 0),
        ("extra_min", "INTEGER", 1, 0),
    ],
    "machines": [
        ("machine_id", "TEXT", 0, 1),
        ("initial_family", "TEXT", 1, 0),
        ("available_min", "INTEGER", 1, 0),
        ("meter_before_kj", "INTEGER", 1, 0),
        ("meter_after_kj", "INTEGER", 1, 0),
    ],
    "maintenance_events": [
        ("event_id", "TEXT", 0, 1),
        ("begin_min", "INTEGER", 1, 0),
        ("end_min", "INTEGER", 1, 0),
        ("energy_kj", "INTEGER", 1, 0),
    ],
    "setup_matrix": [
        ("from_family", "TEXT", 1, 1),
        ("to_family", "TEXT", 1, 2),
        ("minutes", "INTEGER", 1, 0),
    ],
    "shift_windows": [
        ("shift_id", "TEXT", 0, 1),
        ("begin_min", "INTEGER", 1, 0),
        ("end_min", "INTEGER", 1, 0),
    ],
}


class InputError(Exception):
    pass


def integer(value):
    return type(value) is int


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InputError("duplicate config key")
        result[key] = value
    return result


def regular_file(path):
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def load(root):
    config_path = root / "config.json"
    if not regular_file(config_path):
        raise InputError("config file")
    try:
        config = json.loads(config_path.read_bytes().decode("utf-8"), object_pairs_hook=no_duplicate_keys)
    except Exception as error:
        raise InputError(f"config {error}") from error
    if set(config) != {
        "planning_start_min",
        "max_jobs",
        "meter_tolerance_kj",
    }:
        raise InputError("config keys")
    if not all(integer(config[key]) for key in config):
        raise InputError("config types")
    if (
        config["planning_start_min"] < 0
        or not 1 <= config["max_jobs"] <= 9
        or not 0 <= config["meter_tolerance_kj"] <= 100000
    ):
        raise InputError("config range")

    database_path = root / "sterilization.sqlite3"
    if not regular_file(database_path):
        raise InputError("database file")
    database = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    objects = database.execute(
        "select name,type from sqlite_master where name not like 'sqlite_%' order by name"
    ).fetchall()
    if objects != [
        ("consumables", "table"),
        ("job_shifts", "table"),
        ("job_supply_options", "table"),
        ("jobs", "table"),
        ("machines", "table"),
        ("maintenance_events", "table"),
        ("setup_matrix", "table"),
        ("shift_windows", "table"),
    ]:
        raise InputError("database objects")
    for table, expected in SCHEMA.items():
        actual = [(row[1], row[2].upper(), row[3], row[5]) for row in database.execute(f"pragma table_info({table})")]
        if actual != expected:
            raise InputError(f"{table} schema")

    job_rows = database.execute(
        "select job_id,family,duration_min,ready_min,due_min,weight,predecessor from jobs"
    ).fetchall()
    machine_rows = database.execute(
        "select machine_id,initial_family,available_min,meter_before_kj,meter_after_kj "
        "from machines order by machine_id"
    ).fetchall()
    event_rows = database.execute(
        "select event_id,begin_min,end_min,energy_kj from maintenance_events order by event_id"
    ).fetchall()
    setup_rows = database.execute("select from_family,to_family,minutes from setup_matrix").fetchall()
    shift_rows = database.execute(
        "select shift_id,begin_min,end_min from shift_windows order by begin_min,shift_id"
    ).fetchall()
    job_shift_rows = database.execute(
        "select job_id,shift_id from job_shifts order by job_id"
    ).fetchall()
    consumable_rows = database.execute(
        "select consumable_id,capacity_units,activation_fee_microcents "
        "from consumables order by consumable_id"
    ).fetchall()
    option_rows = database.execute(
        "select job_id,option_id,consumable_id,units,extra_min "
        "from job_supply_options order by job_id,option_id"
    ).fetchall()
    database.close()

    jobs = {row[0]: row for row in job_rows}
    machine_rows_by_id = {row[0]: row for row in machine_rows}
    events = {row[0]: row for row in event_rows}
    setup = {(row[0], row[1]): row[2] for row in setup_rows}
    shifts = {row[0]: row for row in shift_rows}
    job_shifts = dict(job_shift_rows)
    consumables = {row[0]: row for row in consumable_rows}
    options = {}
    for row in option_rows:
        options.setdefault(row[0], []).append(row)
    if not 1 <= len(jobs) <= config["max_jobs"] or not machine_rows_by_id:
        raise InputError("row count")
    for job_id, family, duration, ready, due, weight, predecessor in jobs.values():
        if (
            not isinstance(job_id, str)
            or not ID.fullmatch(job_id)
            or family not in FAMILIES
            or not all(integer(value) for value in (duration, ready, due, weight))
            or not 5 <= duration <= 180
            or ready < 0
            or due < ready
            or not 1 <= weight <= 100
            or (predecessor is not None and predecessor not in jobs)
            or predecessor == job_id
        ):
            raise InputError("job")
    for machine_id, family, available, meter_before, meter_after in machine_rows_by_id.values():
        if (
            not isinstance(machine_id, str)
            or not ID.fullmatch(machine_id)
            or family not in FAMILIES
            or not all(integer(value) for value in (available, meter_before, meter_after))
            or available < 0
            or meter_before < 0
            or meter_after <= meter_before
        ):
            raise InputError("machine")
    if not 1 <= len(events) <= 64:
        raise InputError("maintenance event count")
    for event_id, begin, end, energy in events.values():
        if (
            not isinstance(event_id, str)
            or not ID.fullmatch(event_id)
            or not all(integer(value) for value in (begin, end, energy))
            or begin < 0
            or begin > end
            or not 0 <= energy <= 10_000_000
        ):
            raise InputError("maintenance event")
    machines = {}
    for machine_id, family, available, meter_before, meter_after in machine_rows_by_id.values():
        machines[machine_id] = (
            machine_id,
            family,
            available,
            meter_before,
            meter_after,
        )
    expected_pairs = {(source, target) for source in FAMILIES for target in FAMILIES}
    if set(setup) != expected_pairs:
        raise InputError("setup coverage")
    for (source, target), minutes in setup.items():
        if not all(integer(value) for value in (minutes,)):
            raise InputError("setup type")
        if (source == target and minutes != 0) or (source != target and not 1 <= minutes <= 45):
            raise InputError("setup range")

    if len(shifts) != 2:
        raise InputError("shift count")
    ordered_shifts = sorted(shifts.values(), key=lambda row: (row[1], row[0]))
    for index, (shift_id, begin, end) in enumerate(ordered_shifts):
        if (
            not isinstance(shift_id, str)
            or not ID.fullmatch(shift_id)
            or not all(integer(value) for value in (begin, end))
            or begin < 0
            or end <= begin
        ):
            raise InputError("shift window")
        if index and begin < ordered_shifts[index - 1][2]:
            raise InputError("overlapping shifts")
    if set(job_shifts) != set(jobs) or any(value not in shifts for value in job_shifts.values()):
        raise InputError("job shift coverage")
    if not 2 <= len(consumables) <= 8:
        raise InputError("consumable count")
    for consumable_id, capacity, fee in consumables.values():
        if (
            not isinstance(consumable_id, str)
            or not ID.fullmatch(consumable_id)
            or not all(integer(value) for value in (capacity, fee))
            or not 1 <= capacity <= 50
            or not 0 <= fee <= 100_000_000
        ):
            raise InputError("consumable")
    if set(options) != set(jobs):
        raise InputError("job option coverage")
    for job_id, rows in options.items():
        if not 1 <= len(rows) <= 3:
            raise InputError("job option count")
        seen_options = set()
        for _, option_id, consumable_id, units, extra_min in rows:
            if (
                not isinstance(option_id, str)
                or not ID.fullmatch(option_id)
                or option_id in seen_options
                or consumable_id not in consumables
                or not all(integer(value) for value in (units, extra_min))
                or not 1 <= units <= consumables[consumable_id][1]
                or not 0 <= extra_min <= 30
            ):
                raise InputError("job option")
            seen_options.add(option_id)

    visiting = set()
    complete = set()

    def visit(job_id):
        if job_id in visiting:
            raise InputError("dependency cycle")
        if job_id in complete:
            return
        visiting.add(job_id)
        predecessor = jobs[job_id][6]
        if predecessor is not None:
            visit(predecessor)
        visiting.remove(job_id)
        complete.add(job_id)

    for job_id in jobs:
        visit(job_id)
    return config, jobs, machines, setup, events, shifts, job_shifts, consumables, options


def compositions(length, parts):
    for cuts in itertools.combinations(range(length + parts - 1), parts - 1):
        boundaries = (-1,) + cuts + (length + parts - 1,)
        yield tuple(boundaries[index + 1] - boundaries[index] - 1 for index in range(parts))


def simulate(
    planning_start,
    jobs,
    setup,
    queues,
    shift_bounds,
    machine_ids,
    initial_families,
    available_times,
    maintenance_due,
):
    times = [
        max(planning_start, available)
        for available in available_times
    ]
    last_family = list(initial_families)
    pointers = [0] * len(machine_ids)
    finish_by_job = [None] * len(jobs)
    finished_count = 0
    setup_total = [0] * len(machine_ids)
    assignments_by_machine = [[] for _ in machine_ids]

    while finished_count < len(jobs):
        selected = None
        for machine_index, machine_id in enumerate(machine_ids):
            if pointers[machine_index] >= len(queues[machine_index]):
                continue
            job_index = queues[machine_index][pointers[machine_index]]
            job_id, family, duration, ready, _, _, predecessor = jobs[job_index]
            if predecessor is not None and finish_by_job[predecessor] is None:
                continue
            setup_minutes = setup[last_family[machine_index]][family]
            base = max(times[machine_index], planning_start)
            predecessor_finish = (
                planning_start
                if predecessor is None
                else finish_by_job[predecessor]
            )
            shift_begin, shift_end = shift_bounds[job_index]
            start = max(base + setup_minutes, ready, predecessor_finish, shift_begin)
            finish = start + duration
            if finish > min(maintenance_due[machine_index], shift_end):
                return None
            candidate = (
                start,
                machine_id,
                machine_index,
                job_id,
                setup_minutes,
                finish,
                job_index,
                family,
            )
            if selected is None or candidate < selected:
                selected = candidate
        if selected is None:
            return None
        (
            start,
            machine_id,
            machine_index,
            job_id,
            setup_minutes,
            finish,
            job_index,
            family,
        ) = selected
        assignments_by_machine[machine_index].append(
            (machine_id, start, finish, job_id, setup_minutes)
        )
        finish_by_job[job_index] = finish
        finished_count += 1
        times[machine_index] = finish
        last_family[machine_index] = family
        setup_total[machine_index] += setup_minutes
        pointers[machine_index] += 1

    weighted_tardiness = sum(
        job[5] * max(0, finish - job[4])
        for job, finish in zip(jobs, finish_by_job)
    )
    makespan = max(finish_by_job)
    setup_minutes = sum(setup_total)
    canonical = tuple(
        (machine_id, start, job_id)
        for machine_assignments in assignments_by_machine
        for machine_id, start, _, job_id, _ in machine_assignments
    )
    return (
        (weighted_tardiness, makespan, setup_minutes, canonical),
        assignments_by_machine,
        finish_by_job,
        last_family,
        machine_ids,
        times,
        setup_total,
    )


def optimize_single_machine(config, jobs, machines, setup, job_shifts, shifts):
    machine_id = next(iter(machines))
    _, initial_family, available, _, _, _, maintenance_due, _ = machines[machine_id]
    job_ids = sorted(jobs)
    full_mask = (1 << len(job_ids)) - 1
    index_of = {job_id: index for index, job_id in enumerate(job_ids)}
    states = [dict() for _ in range(full_mask + 1)]

    def add_state(mask, last_index, label):
        bucket = states[mask].setdefault(last_index, [])
        time_value, tardiness, setup_total = label[:3]
        for existing in list(bucket):
            if (
                existing[0] <= time_value
                and existing[1] <= tardiness
                and existing[2] <= setup_total
            ):
                return
        bucket[:] = [
            existing
            for existing in bucket
            if not (
                time_value <= existing[0]
                and tardiness <= existing[1]
                and setup_total <= existing[2]
            )
        ]
        bucket.append(label)

    for job_index, job_id in enumerate(job_ids):
        job = jobs[job_id]
        if job[6] is not None:
            raise InputError("single-machine dynamic program requires roots first")
        setup_minutes = setup[(initial_family, job[1])]
        shift = shifts[job_shifts[job_id]]
        start = max(available, config["planning_start_min"], job[3], shift[1]) + setup_minutes
        finish = start + job[2]
        if finish > min(maintenance_due, shift[2]):
            continue
        tardiness = job[5] * max(0, finish - job[4])
        add_state(
            1 << job_index,
            job_index,
            (finish, tardiness, setup_minutes, (job_id,), ((job_id, start, finish, setup_minutes),)),
        )

    for mask in range(1, full_mask + 1):
        if not states[mask]:
            continue
        for last_index, labels in list(states[mask].items()):
            last_job = jobs[job_ids[last_index]]
            for label in list(labels):
                time_value, tardiness, setup_total, sequence, assignments = label
                for job_index, job_id in enumerate(job_ids):
                    bit = 1 << job_index
                    if mask & bit:
                        continue
                    job = jobs[job_id]
                    if job[6] is not None:
                        continue
                    setup_minutes = setup[(last_job[1], job[1])]
                    shift = shifts[job_shifts[job_id]]
                    start = max(time_value + setup_minutes, job[3], shift[1])
                    finish = start + job[2]
                    if finish > min(maintenance_due, shift[2]):
                        continue
                    add_state(
                        mask | bit,
                        job_index,
                        (
                            finish,
                            tardiness + job[5] * max(0, finish - job[4]),
                            setup_total + setup_minutes,
                            sequence + (job_id,),
                            assignments + ((job_id, start, finish, setup_minutes),),
                        ),
                    )

    candidates = []
    for labels in states[full_mask].values():
        for label in labels:
            time_value, tardiness, setup_total, sequence, assignments = label
            candidates.append(
                (
                    (tardiness, time_value, setup_total, sequence),
                    label,
                )
            )
    if not candidates:
        raise InputError("no feasible schedule")
    ordered_candidates = sorted(candidates, key=lambda item: item[0])
    _, (finish_min, tardiness, setup_total, sequence, raw_assignments) = ordered_candidates[0]
    ordered = sorted(raw_assignments, key=lambda item: (item[1], item[0]))
    assignments = [
        {
            "finish_min": finish,
            "job_id": job_id,
            "machine_id": machine_id,
            "setup_min": setup_minutes,
            "start_min": start,
        }
        for job_id, start, finish, setup_minutes in ordered
    ]
    result = {
        "assignments": assignments,
        "finish_by_job": {job_id: finish for job_id, _, finish, _ in raw_assignments},
        "key": (tardiness, finish_min, setup_total, tuple(sequence)),
        "last_family": [jobs[sequence[-1]][1]],
        "machine_ids": [machine_id],
        "objective": {
            "makespan": finish_min,
            "setup_minutes": setup_total,
            "weighted_tardiness": tardiness,
        },
        "setup_total": [setup_total],
        "times": [finish_min],
    }
    result["alternative_signature"] = None
    result["alternative_key"] = None
    if len(ordered_candidates) > 1:
        result["alternative_key"] = ordered_candidates[1][0]
        result["alternative_signature"] = [
            {
                "job_id": job_id,
                "machine_id": machine_id,
                "start_min": start,
            }
            for job_id, start, _, _ in sorted(
                ordered_candidates[1][1][4],
                key=lambda item: (item[1], item[0]),
            )
        ]
    return result


def optimize_mapping(config, jobs, machines, setup, job_shifts, shifts):
    if len(machines) == 1 and all(job[6] is None for job in jobs.values()):
        return optimize_single_machine(
            config, jobs, machines, setup, job_shifts, shifts
        )
    best = None
    second = None
    machine_ids = sorted(machines)
    job_ids = sorted(jobs)
    job_index = {job_id: index for index, job_id in enumerate(job_ids)}
    family_names = sorted(FAMILIES)
    family_index = {
        family: index
        for index, family in enumerate(family_names)
    }
    job_data = [
        (
            job_id,
            family_index[jobs[job_id][1]],
            jobs[job_id][2],
            jobs[job_id][3],
            jobs[job_id][4],
            jobs[job_id][5],
            (
                None
                if jobs[job_id][6] is None
                else job_index[jobs[job_id][6]]
            ),
        )
        for job_id in job_ids
    ]
    shift_bounds = [
        (
            shifts[job_shifts[job_id]][1],
            shifts[job_shifts[job_id]][2],
        )
        for job_id in job_ids
    ]
    setup_grid = [
        [
            setup[(source, target)]
            for target in family_names
        ]
        for source in family_names
    ]
    initial_families = [
        family_index[machines[machine_id][1]]
        for machine_id in machine_ids
    ]
    available_times = [
        machines[machine_id][2]
        for machine_id in machine_ids
    ]
    maintenance_due = [
        machines[machine_id][6]
        for machine_id in machine_ids
    ]

    def materialize(compact):
        key, assignments_by_machine, finish_by_job, last_family, machine_ids, times, setup_total = compact
        weighted_tardiness, makespan, setup_minutes, _ = key
        return {
            "assignments": [
                {
                    "finish_min": finish,
                    "job_id": job_id,
                    "machine_id": machine_id,
                    "setup_min": setup_minutes,
                    "start_min": start,
                }
                for machine_assignments in assignments_by_machine
                for machine_id, start, finish, job_id, setup_minutes in machine_assignments
            ],
            "finish_by_job": {
                job_id: finish_by_job[index]
                for index, job_id in enumerate(job_ids)
            },
            "key": key,
            "last_family": [
                family_names[family]
                for family in last_family
            ],
            "machine_ids": machine_ids,
            "objective": {
                "makespan": makespan,
                "setup_minutes": setup_minutes,
                "weighted_tardiness": weighted_tardiness,
            },
            "setup_total": setup_total,
            "times": times,
        }

    machine_count = len(machine_ids)
    for order in itertools.permutations(range(len(job_ids))):
        for lengths in compositions(len(job_ids), machine_count):
            queues = []
            cursor = 0
            for length in lengths:
                queues.append(order[cursor:cursor + length])
                cursor += length
            result = simulate(
                config["planning_start_min"],
                job_data,
                setup_grid,
                queues,
                shift_bounds,
                machine_ids,
                initial_families,
                available_times,
                maintenance_due,
            )
            if result is None:
                continue
            if best is None or result[0] < best[0]:
                second = best
                best = result
            elif second is None or result[0] < second[0]:
                second = result
    if best is None:
        raise InputError("no feasible schedule")
    best = materialize(best)
    best["alternative_signature"] = None
    best["alternative_key"] = None
    if second is not None:
        second = materialize(second)
        best["alternative_key"] = second["key"]
        best["alternative_signature"] = [
            {
                "job_id": item["job_id"],
                "machine_id": item["machine_id"],
                "start_min": item["start_min"],
            }
            for item in second["assignments"]
        ]
    return best


def resource_plans(jobs, options, consumables):
    job_ids = sorted(jobs)
    for selection in itertools.product(*(options[job_id] for job_id in job_ids)):
        usage = {consumable_id: 0 for consumable_id in consumables}
        for selection_row in selection:
            usage[selection_row[2]] += selection_row[3]
        if any(
            usage[consumable_id] > consumables[consumable_id][1]
            for consumable_id in usage
        ):
            continue
        adjusted_jobs = {}
        choices = {}
        for job_id, selection_row in zip(job_ids, selection):
            _, option_id, consumable_id, units, extra_min = selection_row
            original = jobs[job_id]
            adjusted_jobs[job_id] = (
                original[0],
                original[1],
                original[2] + extra_min,
                original[3],
                original[4],
                original[5],
                original[6],
            )
            choices[job_id] = (option_id, consumable_id, units, extra_min)
        activation_fee = sum(
            row[2]
            for consumable_id, row in consumables.items()
            if usage[consumable_id] > 0
        )
        yield adjusted_jobs, choices, activation_fee, usage


def canonical_decision(choice_rows, schedule_rows):
    return json.dumps(
        [choice_rows, schedule_rows],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def optimize(
    config,
    jobs,
    machine_rows,
    setup,
    events,
    shifts,
    job_shifts,
    consumables,
    options,
):
    machine_ids = sorted(machine_rows)
    candidate_events = []
    tolerance = config["meter_tolerance_kj"]
    for machine_id in machine_ids:
        _, _, available, meter_before, meter_after = machine_rows[machine_id]
        delta = meter_after - meter_before
        candidates = [
            event_id
            for event_id, event_row in sorted(events.items())
            if abs(event_row[3] - delta) <= tolerance and event_row[1] >= available
        ]
        if not 1 <= len(candidates) <= 4:
            raise InputError("maintenance event candidates")
        candidate_events.append(candidates)

    combined = []
    for adjusted_jobs, choices, activation_fee, usage in resource_plans(
        jobs, options, consumables
    ):
        for mapping in itertools.product(*candidate_events):
            if len(set(mapping)) != len(mapping):
                continue
            machines = {}
            for machine_id, event_id in zip(machine_ids, mapping):
                machine_id_value, family, available, before, after = machine_rows[machine_id]
                _, begin, end, _ = events[event_id]
                machines[machine_id] = (
                    machine_id_value,
                    family,
                    available,
                    before,
                    after,
                    event_id,
                    begin,
                    end,
                )
            try:
                result = optimize_mapping(
                    config, adjusted_jobs, machines, setup, job_shifts, shifts
                )
            except InputError as error:
                if str(error) == "no feasible schedule":
                    continue
                raise
            resource_signature = tuple(
                (
                    job_id,
                    choices[job_id][0],
                    choices[job_id][1],
                )
                for job_id in sorted(jobs)
            )
            result["machine_records"] = machines
            result["mapping"] = tuple(mapping)
            result["choices"] = choices
            result["consumable_usage"] = usage
            result["activation_fee"] = activation_fee
            schedule_rows = sorted(
                [
                    [machine_id, start_min, job_id]
                    for machine_id, start_min, _, job_id, _ in result["assignments"]
                ]
            )
            result["mapping_key"] = (
                result["key"][0],
                result["key"][1],
                result["key"][2],
                activation_fee,
                canonical_decision(
                    [list(row) for row in resource_signature],
                    schedule_rows,
                ),
                tuple(sorted(mapping)),
            )
            combined.append(result)
    if not combined:
        raise InputError("no feasible maintenance mapping")
    combined.sort(key=lambda item: item["mapping_key"])
    best = combined[0]

    def decorated(assignments, choices):
        return sorted(
            (
                {
                    "consumable_id": choices[item["job_id"]][1],
                    "job_id": item["job_id"],
                    "machine_id": item["machine_id"],
                    "option_id": choices[item["job_id"]][0],
                    "start_min": item["start_min"],
                }
                for item in assignments
            ),
            key=lambda item: (
                item["machine_id"],
                item["start_min"],
                item["job_id"],
            ),
        )

    def signature(rows):
        return tuple(
            (
                item["machine_id"],
                item["start_min"],
                item["job_id"],
                item["option_id"],
                item["consumable_id"],
            )
            for item in rows
        )

    best_rows = decorated(best["assignments"], best["choices"])
    best_signature = signature(best_rows)
    alternatives = []
    for result in combined:
        main_rows = decorated(result["assignments"], result["choices"])
        alternatives.append((result["mapping_key"], signature(main_rows), main_rows))
        if result.get("alternative_signature") is not None:
            alternative_rows = decorated(
                result["alternative_signature"], result["choices"]
            )
            alternative_key = result["alternative_key"]
            alternative_choice_rows = [
                [
                    item["job_id"],
                    item["option_id"],
                    item["consumable_id"],
                ]
                for item in sorted(alternative_rows, key=lambda item: item["job_id"])
            ]
            alternative_schedule_rows = [
                [item["machine_id"], item["start_min"], item["job_id"]]
                for item in alternative_rows
            ]
            alternatives.append(
                (
                    (
                        alternative_key[0],
                        alternative_key[1],
                        alternative_key[2],
                        result["activation_fee"],
                        canonical_decision(
                            alternative_choice_rows,
                            alternative_schedule_rows,
                        ),
                        tuple(sorted(result["mapping"])),
                    ),
                    signature(alternative_rows),
                    alternative_rows,
                )
            )
    alternatives.sort(key=lambda item: item[0])
    best["alternative_signature"] = None
    seen = {best_signature}
    for _, signature, assignments in alternatives:
        if signature in seen:
            continue
        best["alternative_signature"] = assignments
        break
    return best


def build_documents(jobs, machines, result, shifts, job_shifts, consumables):
    tardy = sorted(job_id for job_id, finish in result["finish_by_job"].items() if finish > jobs[job_id][4])
    shift_rows = []
    for shift_id in sorted(shifts):
        shift_jobs = [
            job_id
            for job_id in jobs
            if job_shifts[job_id] == shift_id
        ]
        shift_rows.append(
            {
                "finish_min": max(
                    (result["finish_by_job"][job_id] for job_id in shift_jobs),
                    default=shifts[shift_id][1],
                ),
                "shift_id": shift_id,
                "weighted_tardiness": sum(
                    jobs[job_id][5]
                    * max(0, result["finish_by_job"][job_id] - jobs[job_id][4])
                    for job_id in shift_jobs
                ),
            }
        )
    consumable_rows = []
    for consumable_id in sorted(consumables):
        used_units = result["consumable_usage"][consumable_id]
        consumable_rows.append(
            {
                "activation_fee_microcents": (
                    consumables[consumable_id][2] if used_units > 0 else 0
                ),
                "activated": used_units > 0,
                "consumable_id": consumable_id,
                "used_units": used_units,
            }
        )
    schedule = {
        "alternative_signature": result["alternative_signature"],
        "assignments": result["assignments"],
        "objective": {
            **result["objective"],
            "consumable_cost_microcents": result["activation_fee"],
        },
        "tardy_jobs": tardy,
    }
    machine_state = {
        "machines": [
            {
                "finish_min": result["times"][index],
                "last_family": result["last_family"][index],
                "machine_id": machine_id,
                "maintenance_event_id": result["machine_records"][machine_id][5],
                "total_setup_min": result["setup_total"][index],
            }
            for index, machine_id in enumerate(result["machine_ids"])
        ],
        "shared_resources": {
            "consumables": consumable_rows,
            "job_choices": [
                {
                    "consumable_id": result["choices"][job_id][1],
                    "job_id": job_id,
                    "option_id": result["choices"][job_id][0],
                    "shift_id": job_shifts[job_id],
                }
                for job_id in sorted(jobs)
            ],
            "shifts": shift_rows,
        },
    }
    return schedule, machine_state


def execute(root):
    with open(root / ".sterilization.lock", "a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config, jobs, machines, setup, events, shifts, job_shifts, consumables, options = load(root)
        result = optimize(
            config,
            jobs,
            machines,
            setup,
            events,
            shifts,
            job_shifts,
            consumables,
            options,
        )
        schedule, machine_state = build_documents(
            jobs, machines, result, shifts, job_shifts, consumables
        )
        stage = Path(tempfile.mkdtemp(prefix=".sterilization-", dir=root))
        staged = [stage / "schedule", stage / "state"]
        staged[0].write_text(
            json.dumps(schedule, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        staged[1].write_text(
            json.dumps(machine_state, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        for path in staged:
            with open(path, "rb") as handle:
                os.fsync(handle.fileno())
        targets = [root / "output/sterilizer-schedule.json", root / "output/machine-state.json"]
        backups = [root / "output" / f".sterilization-back-{os.getpid()}-{index}" for index in range(2)]
        had_old = [os.path.lexists(path) for path in targets]
        published = [False, False]
        try:
            for index, target in enumerate(targets):
                if had_old[index]:
                    os.replace(target, backups[index])
                os.replace(staged[index], target)
                published[index] = True
            descriptor = os.open(root / "output", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            os.fsync(descriptor)
            os.close(descriptor)
        except Exception:
            for index in (1, 0):
                if published[index] and os.path.lexists(targets[index]):
                    targets[index].unlink()
                if had_old[index] and os.path.lexists(backups[index]):
                    os.replace(backups[index], targets[index])
            raise
        finally:
            shutil.rmtree(stage, ignore_errors=True)
        for backup in backups:
            if os.path.lexists(backup):
                backup.unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    arguments = parser.parse_args()
    try:
        execute(Path(arguments.root))
        return 0
    except Exception as error:
        print(f"sterilization plan failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
