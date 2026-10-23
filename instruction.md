# 重排灭菌供应批次

中心供应室在凌晨切换排程服务后，现场目录中留下一份待处理批次库。旧的打印清单已经过期，两台灭菌机也带着上一班结束时的设备族设置。请修复 `/app/plan_sterilization.py`，根据器械包的截止时间、依赖和机器维护窗口，生成唯一一份全局最优排程。程序通过 `python3 /app/plan_sterilization.py --root <现场目录>` 启动。

一次排程必须在 20 秒内完成。

按本页交付当前结果。产物发布完成后可以结束运行，不需要再扩展额外测试或检查其他目录。

## 现场输入

`config.json` 必须是不跟随软链接的普通 UTF-8 JSON 对象，不允许重复键，字段恰好为：

```text
planning_start_min
max_jobs
meter_tolerance_kj
```

`planning_start_min` 是非负整数。`max_jobs` 接受 1 到 9 的整数。批次库中的器械包数量必须不超它。`meter_tolerance_kj` 是 0 到 100000 的整数。布尔值不算整数。

`sterilization.sqlite3` 必须是不跟随软链接的普通 SQLite 文件，用户对象恰好为：

```text
jobs(job_id TEXT PRIMARY KEY, family TEXT NOT NULL, duration_min INTEGER NOT NULL,
     ready_min INTEGER NOT NULL, due_min INTEGER NOT NULL, weight INTEGER NOT NULL,
     predecessor TEXT)
machines(machine_id TEXT PRIMARY KEY, initial_family TEXT NOT NULL,
         available_min INTEGER NOT NULL,
         meter_before_kj INTEGER NOT NULL, meter_after_kj INTEGER NOT NULL)
maintenance_events(event_id TEXT PRIMARY KEY, begin_min INTEGER NOT NULL,
                   end_min INTEGER NOT NULL, energy_kj INTEGER NOT NULL)
setup_matrix(from_family TEXT NOT NULL, to_family TEXT NOT NULL,
             minutes INTEGER NOT NULL, PRIMARY KEY(from_family,to_family))
shift_windows(shift_id TEXT PRIMARY KEY, begin_min INTEGER NOT NULL,
              end_min INTEGER NOT NULL)
job_shifts(job_id TEXT PRIMARY KEY, shift_id TEXT NOT NULL)
consumables(consumable_id TEXT PRIMARY KEY, capacity_units INTEGER NOT NULL,
            activation_fee_microcents INTEGER NOT NULL)
job_supply_options(job_id TEXT NOT NULL, option_id TEXT NOT NULL,
                   consumable_id TEXT NOT NULL, units INTEGER NOT NULL,
                   extra_min INTEGER NOT NULL,
                   PRIMARY KEY(job_id,option_id))
```

全部 ID 完整匹配 `[A-Z][A-Z0-9_-]{0,39}`。`family` 和 `initial_family` 只能取：

```text
GLASS
INSTRUMENT
LINEN
UTILITY
```

`duration_min` 为 5 到 180，`ready_min`、`available_min` 和两个表计读数均为非负整数，`due_min` 不早于对应器械包的 `ready_min`，`weight` 为 1 到 100。`predecessor` 为空时可省略。非空值必须引用另一器械包。依赖关系无环。

维护记录不携带机器编号，而且表计差与事件能量不再要求全局唯一。机器差值与该事件能量之差不超过 `meter_tolerance_kj` 时，该事件才是这台机器的候选。候选还必须满足 `begin_min` 不早于机器可用时刻。候选还必须满足 `begin_min <= end_min`。每台机器必须有 1 到 4 个候选。最终为每台机器选一条候选事件，事件编号不得重复。若没有完整候选，整批输入拒收。若没有任何候选映射能形成合法排程，整批输入拒收。

`maintenance_events` 含 1 到 64 条对象，记录维护窗口及其表计差。事件编号同样匹配
`[A-Z][A-Z0-9_-]{0,39}` 且不得重复。能源差为 0 到 10000000 的整数。

`setup_matrix` 含有四个族两两组合的 16 个对象。同族切换分钟为 0，异族切换为 1 到 45。每台机器至少有一个对象，器械包数量至少为 1。

`shift_windows` 恰好有两个班次组。两组编号按时间顺序排列，窗口互不重叠。每个窗口的 `begin_min` 不小于零，`end_min` 严格大于开始时刻。`job_shifts` 必须给每个器械包恰好指定一个窗口。

`consumables` 有 2 到 8 条耗材记录。容量是 1 到 50 的整数，启用费是 0 到 100000000 的整数微元。`job_supply_options` 为每个器械包提供 1 到 3 个候选方案。每个方案由 `option_id` 唯一标识，并指定一种耗材、消耗单位数以及额外处理分钟。额外处理分钟为 0 到 30。每个器械包必须恰好选择一个方案。

耗材由全部班次组共享。任一种耗材只要本批有器械包使用，就只收一次启用费。同一耗材的消耗单位数按全部班次组累加。耗材总用量受容量约束：

```text
sum(job_supply_options.units <= consumables.capacity_units)
```

## 批次约束

排程把每件器械包交给一台机器，且不会重复。机器按给出的序列连续处理器械包，后一项在前一项完成后开始。

机器从 `initial_family` 开始。若下一件器械包的族不同，先完成 `setup_matrix` 给出的换族准备，再运行器械包。准备安排在当前可用时刻或前一项完成之后。

维护事件选择和器械包排程必须一起求解。选定映射后，该事件的 `begin_min` 就是机器必须完成当前批次的时间上限。所选窗口的 `end_min` 也是器械包的完成上限，两个上限取较早者。
开始时刻由以下四个时刻的最大值决定：

```text
不早于 config.json 的 planning_start_min
不早于器械包 ready_min
不早于其 predecessor 的完成时刻
不晚于所匹配维护事件的 begin_min 扣掉准备和运行时长的极限
```

若找不到让全部器械包都在维护窗口内完成的排法，整次运行失败。

## 最优口径

在所有合法排程中，按下面的字典序取唯一最优：

```text
1. 加权迟交最小：sum(weight * max(0, finish_min - due_min))
2. 完工时间最小：所有 finish_min 的最大值
3. 换族准备总分钟最小：所有 setup_min 之和
4. 耗材启用费合计最小：所有实际使用耗材的 activation_fee_microcents 之和
5. 规范决策串最小。先取 `choice_rows=[[job_id,option_id,consumable_id],...]`（按 job_id 升序），再取 `schedule_rows=[[machine_id,start_min,job_id],...]`（按这三个字段依次升序），使用 `json.dumps([choice_rows,schedule_rows], ensure_ascii=false, separators=(",", ":"))` 生成 UTF-8 规范决策串，按 Unicode 码点逐字符比较，较小者优先
6. 若前五项仍完全相同，按 machine_id 升序比较选中的 maintenance_event_id 序列，较小者优先
```

按截止时间逐件贪心通常不是全局最优。同一个器械包早做可能让族设置阻塞另一台机器，而更小的单件迟交也可能抬高全局加权值。
规范决策串只用于并列裁决，不写入结果文件。方案即使完成时刻相同，只要器械包选择的 option_id 或 consumable_id 不同，也必须按上面的 choice_rows 顺序比较；不得依赖数据库行序、Python 字典插入序或优化器遍历顺序。

## 输出

成功后刷新两个普通文件：

```text
root/output/sterilizer-schedule.json
root/output/machine-state.json
```

`sterilizer-schedule.json` 的顶层键集合为 `alternative_signature,assignments,objective,tardy_jobs`。`alternative_signature` 给出按字典序紧接最优方案的下一套不同排程签名。方案也会因所选耗材不同而不同。没有第二套排程时它取 `null`。非空时数组按 `machine_id,start_min,job_id` 升序。数组行结构见下。

```text
consumable_id job_id machine_id option_id start_min
```

`assignments` 按 `machine_id,start_min,job_id` 升序，每项恰好为：

```text
finish_min
job_id
machine_id
setup_min
start_min
```

`start_min` 是器械包实际开始运行的时刻，`setup_min` 是本次换族准备分钟。`objective` 的键集合为：

```text
consumable_cost_microcents
makespan
setup_minutes
weighted_tardiness
```

`tardy_jobs` 是所有 `finish_min > due_min` 的 job_id 升序数组。

`machine-state.json` 的顶层键集合为 `machines,shared_resources`。`machines` 数组按 `machine_id` 升序，每项恰好为：

```text
finish_min
last_family
machine_id
maintenance_event_id
total_setup_min
```

`maintenance_event_id` 写联合求解后为该机器选中的维护事件。没有接到器械包的机器，其
`finish_min` 等于 `available_min`，`last_family` 保持初值。两份文件均为键排序紧凑 JSON 并换行。

`shared_resources` 的键集合见下方。

`job_choices` 按 `job_id` 升序。该数组的整行结构见下。

```text
consumable_id
job_id
option_id
shift_id
```

`consumables` 按编号升序。该数组的整行结构见下。

```text
activation_fee_microcents
activated
consumable_id
used_units
```

`shifts` 按编号升序。该数组的整行结构见下。

```text
finish_min
shift_id
weighted_tardiness
```

## 发布要求

使用 `root/.sterilization.lock` 排他锁。两个文件全部写入临时位置并持久化后，才能替换目标。替换途中失败要恢复旧文件的字节、权限和目录项类型，并清理备份与临时目录。输入数据库和配置不得改写。连续运行或两个进程同时运行，结果必须逐字节一致。

## 夜班交接的真实取舍

手术室在清晨会同时催要器械包，供应室可用的机器为两台。护理组长先看器械包能否消毒、是否还等上一件器械，再看机器保养结束后还能跑多久。玻璃、器械、布类和公用器皿之间的换族准备不算长，却足以让一台机器错过另一个更重的器械包。

因此，交接时不能简单把最急的器械包塞给当前最早空闲的机器。正确的排程需要重新分配整组器械包，比较加权迟交，再在相同迟交下压缩完工时间和换族准备。一次重排失败时，上一班打印出的两份文件仍要留在原处，值班人员才能继续照旧计划送包。
## 跨日供应补充说明

消毒供应中心这天接收了骨科、口腔科和急诊科临时追加的器械包。夜班组长发现上一班留下的机器占用表写到凌晨两点，后面的维护窗口被新排班覆盖，因此计划员需要从原始器械清单重新组织批次。手术室拿到各包预计送达时段和允许等待的范围，并不知道最终会被编入哪一炉。

夜班与次日供应组共享同一批包装耗材和两台灭菌机。某个供应组单独看时最省钱的耗材方案，可能让另一组没有余量或被迫延长处理时间；交接排程因此把两个供应组、维护事件与耗材启用放在同一个搜索里。

灭菌员会根据包装材质调整装载层数，纸塑袋与金属盒不能随意放在同一层。若前一炉刚完成高温循环，腔体还需要一段自然降温时间，排程时把这段冷却写进机器空闲区。急诊包可能插队，但它仍要占用完整的装载、升温、保温和泄压周期。供应中心更关心所有手术台能否在开台前收到器械，而不是单纯缩短最后一炉的完成时刻。

当天下午还安排了过滤器更换和压力表巡检，维护人员进入无装载炉腔。计划员需要把这些窗口与机器当前状态一起读入，并在换班前输出一张可执行炉次表。若某包因为错过时段改走外送，交接记录要说明它落在哪个等待区间，而不是直接消失。

手术室联络员会在早交班时再次确认每台手术的开台时间。骨科器械通常先拆包消毒再配制，口腔器械则可能分批送达，急诊包还会因为临时加台带来新的收件时刻。供应中心把这些变动写到同一份当天清单里，计划员据此重算批次，而不是沿用凌晨那版打印稿。若某台机器因压力波动提前退出维护，调度员可以把随后的批次前移，但前置器械尚未送达时仍需等待。

包装组的装载量也影响排程。小包可以拼炉，大包占用整层，器械夹与托盘之间还要保留蒸汽通道。计划完成并不等于工人立即下班，灭菌后的冷却与卸载需要占用有限台面，夜班组长因此更关心产出的交付时段是否与手术开台相衔接。报废包与召回包由质量管理单独处理，不进入这份生产输入。

院感科在下午巡检时发现一号灭菌器的门封温度偏高，设备工程师把原定四十分钟的维护延长到一小时。这个变化来自机器工作日志中的累计运行时间，计划员需要从表计差值识别真实维护事件，再据此调整当天剩下的炉次。另一台机器刚完成上一班任务，腔体仍处于保温状态，机器状态文件要把这项恢复后的起点保留下来，方便下一班核对为什么首炉没有从零时刻开始。
