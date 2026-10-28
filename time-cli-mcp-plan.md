# time CLI 与 MCP 的实证研究与实施计划

日期：2026-10-28（Asia/Shanghai）

## 简短结论

平台名称保留为 **time**。建议首选正式 CLI 命令为 `timectl`，避免与系统自带 `time` 和 shell 关键字冲突。下列 `command time` 用法是仅供评估的可选兼容入口，只有管理员明确决定提供该入口且已确认 PATH 和计时工具影响时才成立：

    command time run --gpus 1 -- python -u train.py --epochs 50
    env time queue --json
    /usr/local/bin/time show <平台任务 UUID>

本机实测 Bash 中 time 是 shell 关键字；直接写 time run 会把 run 当作待计时命令。command time、env time 和绝对路径才会进入外部 CLI。推荐使用 `timectl` 作为主命令，不覆盖系统计时工具，也不改变产品名。`command time` 能否找到平台程序取决于安装位置和 PATH。

当前真实设计链是“登录用户 -> Cockpit bridge -> root-owned Unix socket API -> root 后端 -> 受控 sbatch -> Slurm -> 用户 UID 作业”。普通用户不允许直接 sbatch。当前安装 API 没有 monthly_quota.py；源码暂存的月额实现也明确 enforcement_ready=false，因此月额必须显示“未启用/未知”，不能宣称严格 pre-allocation 已上线。

> 总控审核：原 u20 研究使用 `command time` 作为主入口；考虑它仍可能解析到系统计时工具，本计划推荐 `timectl`，平台仍称 time。示例中的 `command time` 仅用于说明冲突，不是现有可用命令。生产 pro6000d 的运行版本需上线前再次核验。

## 1. 证据边界和版本

| 层次 | 证据和结论 |
|---|---|
| 任务输入 API | /home/u20/.local/state/u20-dispatch/jobs/j-6db215cd313549c9a523/inputs/0/pro6000d-training-api.py，SHA-256 1b13f...，1373 行；含外部任务字段和月额路由，属于输入源码。 |
| CLI 设计稿 | inputs/2/training-cli-design.md:1-70；明确写着命令未安装，月额前门控未上线。其 p6 仅为旧方案名，本计划改用 time。 |
| 暂存后端 | /home/u20/u20-training-platform-monthly-staged/backend/pro6000d-training-api.py，SHA-256 5186cf...，1196 行。 |
| 本机安装 API | /usr/local/lib/pro6000d-training/pro6000d-training-api.py，SHA-256 65d213...，1156 行；不是当前暂存文件。 |
| 本机安装月额 | /usr/local/lib/pro6000d-training/monthly_quota.py 不存在；安装目录只有 API 文件。 |
| root service 文件 | /etc/systemd/system/pro6000d-training.service:5-23；User=root，ExecStart 指向安装 API。 |
| Slurm 提交门 | /etc/slurm-llnl/job_submit.lua:1-27；与暂存 backend/job_submit.lua 同 SHA，只允许 submit_uid=0、合法平台 comment 和非 root 作业 UID。 |
| 本机 Slurm | /etc/slurm-llnl/slurm.conf:1-35；u20-training、端口 16817/16818、Gres=gpu:1。 |
| 另一份 pro 快照 | source-input/server-source/etc/slurm/slurm.conf:1-43；是不同的 pro6000d/4 GPU 快照，不能当作当前 u20 或线上状态。 |

本次没有读取凭据、没有部署、没有提交训练、没有运行测试。当前沙箱中 systemctl show 被 D-Bus 权限拒绝，/etc/pro6000d-training/config.json 被权限拒绝，scontrol 也受权限/网络限制。因此只能确认安装文件和配置文件内容，不能宣称服务当前 active、API 当前可用或生产线上加载了哪一版。用户给出的 root broker + Slurm 事实与已安装 root 单元和 Lua 提交门一致；仍需有权限管理员在目标主机做只读健康核验。

## 2. API、身份和训练调用链

### 2.1 传输与身份

客户端读取最多 1 MiB 的一行 JSON，连接 PRO6000D_TRAINING_SOCKET 或默认 /run/pro6000d-training/backend.sock，读取最多 8 MiB 响应，socket timeout 为 45 秒（inputs/1/pro6000d-training-client.py:13-33）。

服务端以 SO_PEERCRED 取得每个连接的真实 UID，忽略浏览器传来的用户名和角色；UID 必须对应本机账户，并属于 training_group 或 admin_group（任务输入 API:233-263）。身份和权限由系统账户/组决定，CLI、MCP 不得接受请求中的 uid 作为可信身份，也不得通过 sudo 代用户提交。

当前请求和响应：

    {"method":"GET","path":"/api/v1/me","body":{}}
    {"ok":true,"data":{...}}
    {"ok":false,"error":{"status":409,"code":"...","message":"...","details":null}}

服务端接收请求约 3 秒、请求上限 1 MiB；未知异常只向调用者返回 internal_error，详情写后端 stderr（任务输入 API:1316-1356）。

### 2.2 路由和行为

| 方法/路径 | 主要输入 | 返回/权限 | 证据与限制 |
|---|---|---|---|
| GET /api/v1/me | 无 | identity、version；已授权用户 | 版本只有 0.1.0；API:1281-1288。 |
| GET /api/v1/state | 无 | 身份、主机、jobs、GPU、metrics、quota、accounts、rules、notifications、scheduler health | 普通用户只看自己可见数据，管理员看账户和全部 jobs；输入 API另有 monthly_quota、monthly_accounts、display_name；API:796-819、1288-1290。 |
| GET /api/v1/jobs | 无 | jobs；普通用户本人，管理员最多 1000 条 | 调用前 reconcile；API:973-979。 |
| POST /api/v1/jobs/validate | job 对象 | 归一化 job、quota、usage；不创建任务 | 默认 gpu=1、cpu=1、memory=1024 MiB、time=60 分钟、qos=normal、environment=system；API:325-378。 |
| POST /api/v1/jobs | job、可选 client_request_id、temporary_application_id | job；创建平台任务并尝试提交 | 先检查 scheduler/plugin，再 SQLite 入队；API:900-951。 |
| POST /api/v1/jobs/{id}/cancel | 无 | 最新 job；本人或管理员 | 调用 scancel，再 reconcile；提交结果未确认时拒绝假报取消成功；API:981-1003。 |
| GET /api/v1/jobs/{id}/log | 无 | 日志尾部或不可用原因 | 以作业 UID 读真实文件，最多最后 1 MiB；API:1005-1039。 |
| GET/POST /api/v1/applications | kind、requested、reason | 申请列表/新申请 | 安装版只有 temporary_job、quota_change；输入 API增加 monthly_gpu_hours；API:1044-1050、输入 API:1081-1119。 |
| POST /api/v1/applications/{id}/decision | approve、approve_adjusted、reject、理由、approved、valid_minutes | 管理员申请决定 | 只处理 PENDING，条件更新防重；API:1135-1182。 |
| POST /api/v1/notifications/{id}/read | 无 | ok | 只更新当前收件人；API:1192-1193、1299。 |
| POST /api/v1/rules/preview、/apply | rules 或 preview_id | 管理员预览/应用 | 预览十分钟有效、单次使用且快照必须不变；API:1195-1236。 |
| POST /api/v1/quotas/preview、/apply | scope、uid、quota/inherit 或 preview_id | 管理员预览/应用 | 配额 1-1024，事务写入，预览冲突拒绝；API:1238-1279。 |
| GET /api/v1/audit | 无 | 管理员最近 500 条 | 普通用户不降级；API:1304-1307。 |

### 2.3 校验、错误、幂等和日志

校验规则：job 名 1-64 个安全字符；command 非空、最多 4096 字符、不能含 NUL；GPU、CPU、内存、分钟数必须是整数且不超过服务端 capacity；qos 必须存在；workdir 和 output 必须是绝对路径、位于用户根目录、属于用户或 root；output 只允许一个文件名中的 %j；environment 只能是 system 或安全格式的环境名；当前 validate_job 的 SAFE_ENV 不接受以 / 开头的绝对环境路径（任务输入 API:325-378）。

主要错误码包括 not_authorized、admin_required、invalid_resource、machine_unsatisfiable、scheduler_unavailable、permanent_quota_exceeded、active_job_limit、submission_uncertain、job_not_found、job_forbidden、cancel_failed、log_read_failed，具体定义见任务输入 API:236-375、900-1038。当前错误没有稳定 schema_version、retryable 或 outcome。

作业幂等目前不充分：

- client_request_id 为 8-80 个字母/数字/连字符，数据库有 owner_uid + client_request_id 唯一索引（API:219-222、900-908）。
- 同一 UID 再次提交相同 ID 直接返回旧 job；服务端没有比较规范化正文哈希，同一 ID 对应不同正文也可能返回旧任务。CLI 必须本地拒绝，后端必须增加 idempotency_conflict。
- 提交前写入 SUBMITTING，随后调用 sbatch；超时或返回异常而没有可确认 Slurm ID 时保留提交意图，状态为 submission_uncertain，后台通过唯一平台 job name reconcile，不能自动创建第二个任务（API:847-869）。
- temporary application 通过授权一次性绑定和数据库条件更新防止并发消费（API:912-946）。
- rules/quota 需要 preview，十分钟内使用一次；重复或快照变化返回 preview_required/preview_stale。

日志是有界尾部，不是完整文件：服务端使用 openat/O_NOFOLLOW、以作业 UID 读取普通文件最后 1 MiB（API:1017-1039）。CLI/MCP 在现有接口下只能说“最多 1 MiB 尾部”，不能提供 follow、offset 恢复或无漏行保证。

### 2.4 真实训练链

1. Cockpit 前端用 cockpit.spawn 启动 /usr/local/bin/pro6000d-training-client，并把 method/path/body 加换行；45 秒本地 timer 后提示结果可能仍在处理中（暂存 UI app.js:27-42）。
2. bridge 以登录用户连接 Unix socket；后端再次检查 peer UID。
3. root API 每约 3 秒 reconcile：执行 scontrol show jobs -o，读取最近 8 MiB jobcomp，并处理等待任务（输入 API:476-552）。
4. 后端在 root-owned job script 目录写 0600 脚本，然后以固定 argv 调用 sbatch，包含 --uid 用户 UID、--gid GPU 作业组、--job-name p6-UUID、--gres gpu:N、CPU、内存、时间、workdir、output/error 和 --comment pro6000d-job:UUID（输入 API:828-869）。
5. Lua job_submit 检查真实 submit_uid、平台 comment 和非 root 作业 UID（已安装 /etc/slurm-llnl/job_submit.lua:4-18）。普通用户直接 sbatch 会被拒绝。
6. Slurm 用 GRES/cgroup 施加资源约束；训练以用户 UID 运行。API 不调用 PyTorch/torchrun，也不解析 #SBATCH。
7. 后端从 Slurm 状态和 jobcomp 更新平台状态；日志由用户 UID读取；取消走 scancel；调度器记录消失时保留 completion_unknown，不能把队列消失当成功（输入 API:492-524；docs/DEPLOYMENT.md:41-43）。

单 GPU命令可以是：

    command time run --gpus 1 -- python -u train.py --epochs 50

多 GPU只有 capacity 和 GRES确实允许时才可用：

    command time run --gpus 2 -- torchrun --nproc-per-node=2 train.py --epochs 50

--gpus 只申请 Slurm GPU数，不会自动注入 torchrun、CUDA_VISIBLE_DEVICES 或 nproc-per-node。当前可见 u20 配置为 Gres=gpu:1（/etc/slurm-llnl/slurm.conf:34-35），所以该主机应在 validate 阶段拒绝 2 GPU。接口没有 nodes、rank、master address、allocation generation；第一版明确拒绝多机。

环境执行也不是原生 Python argv：system 使用 /bin/bash -lc command；代码含绝对环境路径分支，但当前校验不接受以 / 开头的路径，不能承诺可用；名称环境尝试用户 Conda 的 conda run -n name（输入 API:953-968）。CLI应安全 shell-quote argv，但必须公开服务端最终仍以 Bash 执行；后端长期应新增原生 argv 字段。

### 2.5 版本差异

- 任务输入 API 1373 行新增 origin、source_scheduler_name、source_submit_time、resource_unknown，可选导入外部 Slurm 任务只读；当前暂存后端1196行没有这部分（输入 API:223-227、421-474）。
- 当前暂存后端相比本机安装 API主要增加 monthly_quota 导入、月账刷新、月额申请和 monthly_quota 返回字段；本机安装 API没有这些路由和字段。安装目录没有 monthly_quota.py。
- 本机 job_submit.lua 与暂存版同 SHA，但本机 slurm.conf 与 server-source 快照不同；快照不能代表当前 u20。
- systemd 单元只证明安装配置的 root 入口，不证明 active。后续需要管理员只读核验 systemctl is-active、socket、/api/v1/me 和 /api/v1/state。

## 3. time CLI 方案

### 3.1 shell 冲突实测和命令语义

本机实测：

- bash type -a time 报告 shell 关键字、/usr/bin/time、/bin/time；
- help time 显示 time [-p] pipeline；
- bash 中 time -- printf 会把 -- 当作待执行命令；
- command time --help、env time --help、/usr/bin/time --help 均进入外部命令；
- dash 中 time printf 进入外部 /usr/bin/time；
- PATH 中同时有 /usr/bin/time 和 /bin/time。

固定语义：

| 写法 | 解释 | 建议 |
|---|---|---|
| time run ... | Bash 关键字，计时 run | 禁止文档使用 |
| command time run ... | command builtin 查找 PATH 中的外部 time | 可选，必须核对实际解析结果，不能保证是平台程序 |
| env time run ... | env 查找外部 time | 远程自动化可用 |
| /usr/local/bin/time run ... | 绝对路径 | 最无歧义 |
| timectl run ... | 独立命令，不与常见计时工具同名 | 推荐主入口；安装前检查重名 |
| alias time='command time' | 会改变用户原有计时习惯 | 不作为默认安装 |

time CLI把第一个非全局参数解析为子命令；--只表示训练 command 的 argv边界：

    command time run --gpus 1 --cpus 8 --mem 32GiB --time 12h --       python -u train.py --epochs 50 --lr '1e-4'

CLI只解析自己的选项，对 -- 后每一项做 POSIX shell quoting，不让本地 shell二次展开字面 $、空格和引号。需要管道时用户明确写 bash -lc。当前后端仍是 shell command契约，不能假装是 execve argv。

### 3.2 命令和默认值

只读第一阶段：

    command time whoami
    command time doctor
    command time capabilities --json
    command time queue
    command time show <平台 UUID>
    command time logs <平台 UUID>
    command time gpu
    command time quota

写入和管理员：

    command time run --gpus 1 --mem 8GiB --time 60m -- python -u train.py
    command time cancel <平台 UUID>
    command time cancel --yes <平台 UUID>
    command time request temporary-job --gpus 2 --time 12h --reason '完成实验'
    command time requests
    command time request show <申请 ID>
    command time admin requests
    command time admin approve <申请 ID> --yes
    command time admin reject <申请 ID> --reason '...' --yes
    command time admin quota preview ...
    command time admin quota apply --preview-id <ID> --yes
    command time admin rules preview ...
    command time admin rules apply --preview-id <ID> --yes
    command time audit --json

以上为目标 CLI，不是现有命令。现有 API可映射 jobs、cancel、logs、applications、rules/quota preview/apply、audit；capabilities、request-status、统一 envelope、原生 argv、外部任务导入和月额 enforcing需要后端支持。

默认建议：GPU 1、CPU 1、内存 1 GiB、时间60分钟。32G/32GiB按32 GiB=32768 MiB；12h转整数分钟；qos由服务端规则决定；workdir取远端当前目录后再校验；environment不安装依赖。dry-run只调用 /jobs/validate，不预留GPU、不进Slurm、不保证依赖存在。run返回已创建、排队、等待或未知，不返回训练成功。queue --watch只轮询，Ctrl-C不取消。短 ID歧义时列候选，不猜。日志明确最后1 MiB尾部。

### 3.3 幂等和不确定结果

CLI生成稳定 client_request_id，保存在用户 0700目录/0600文件，记录规范化请求哈希、目标、时间和结果。提交超时：

1. 保留原 ID和原请求；
2. 用 request-status或jobs查询；
3. 只允许相同正文哈希的同 ID重试；
4. 不自动创建第二个训练；
5. 无法判断时退出码6、outcome=unknown。

建议退出码：0为请求处理成功但不等于训练成功；2参数错误；3权限拒绝；4资源/策略冲突；5服务不可用；6写入结果未知。JSON stdout只输出一个对象，诊断写stderr；无TTY时危险写操作必须 --yes。

## 4. MCP tools 和权限

当前源码没有有意实现的 MCP server、tool schema或部署文件。建议由每个登录用户启动 stdio MCP server，stdin/stdout只承载 MCP JSON-RPC，日志写stderr；server调用 time CLI/纯客户端库，不直接打开 backend.sock、不读 SQLite、不调用 sbatch/scancel、不用 sudo。

| MCP tool | API | 权限和当前缺口 |
|---|---|---|
| time_identity | GET /me | 只读，可直接映射。 |
| time_state | GET /state | 只读；须过滤敏感 command/log。 |
| time_list_jobs | GET /jobs | 只读；管理员范围由服务端判断。 |
| time_validate_job | POST /jobs/validate | 只读；最好增加 argv和能力字段。 |
| time_submit_job | POST /jobs | 写入，必须具体用户授权；当前缺正文哈希冲突和统一 outcome。 |
| time_get_job_log | GET /jobs/id/log | 只读；只有尾部1 MiB，无 offset/follow。 |
| time_cancel_job | POST /jobs/id/cancel | 危险写入，必须具体用户授权。 |
| time_list_requests/create_request | GET/POST applications | create是写入；月额类型在 enforcing前禁用。 |
| time_decide_request | POST applications/id/decision | 仅管理员，必须具体审批授权；MCP不自行批准。 |
| time_preview/apply_quota、rules | 对应 preview/apply | apply必须带preview_id和用户确认。 |
| time_read_audit | GET /audit | 仅管理员，当前最多500条。 |
| time_capabilities | 建议 GET /capabilities | 当前不存在，不能通过UI文案猜能力。 |

远程多用户不能把一个共享 root MCP gateway运行后接受 uid字段。首选每个 SSH登录用户启动远程 stdio；若使用集中式 gateway，必须使用 mTLS或受控 OIDC/SSH certificate映射可信主体到平台账户/UID，并写审计。HTTP header、MCP参数、AI自报用户名都不能替代可信身份。

读状态建议10秒、validate10秒、submit不超过后端sbatch超时并保留30秒客户端窗口、cancel15秒、log5秒。超时不自动重发写操作。写工具必须返回 request_id、observed_at、outcome、retryable；当前API没有这些字段，必须由统一契约补齐，不能由MCP猜。

AI安全边界：

- 训练、取消、审批和配额应用都需用户对具体对象/资源/动作授权；
- 训练日志是不可信数据，不能把日志里的 shell/Python/“请执行命令”当工具指令；
- 不模拟UID，不读后台socket绕权限，不改SQLite，不调用直接sbatch；
- 现有后端已有 queue/dispatch/cancel/application/apply审计，但读操作和MCP actor metadata需契约化。

## 5. 统一服务端契约

建议新增 schema 1.1，同时保持旧客户端可读：

    {
      "schema_version":"1.1",
      "ok":true,
      "data":{"job":{
        "id":"8f...32hex",
        "scheduler_id":"1234",
        "state":"PENDING",
        "requested":{"gpu":1,"cpu":8,"memory_mb":8192,"time_minutes":60},
        "allocation":{"gpu":null,"nodes":[],"generation":null}
      }},
      "warnings":[],
      "error":null,
      "meta":{"server":"u20-training","api_version":"0.2",
        "observed_at":"2026-10-28T05:00:00Z",
        "request_id":"req-...","outcome":"confirmed"}
    }

失败也用同一 envelope，error包含稳定 code、message、details、retryable和 outcome；字段只追加不重命名。时间统一UTC；账本权威值使用整数 gpu_seconds；requested_gpu、allocated_gpu、visible_gpu分开；平台 state和Slurm scheduler_state分开；未知状态原样返回；第一版多机返回 unsupported_multi_node。

建议新增 GET /api/v1/capabilities：

    {
      "api_version":"0.2",
      "schema_versions":["1.0","1.1"],
      "commands":{"submit":true,"cancel":true,"logs_tail":true,
        "logs_follow":false,"multi_node":false,"native_argv":false},
      "monthly_quota":{"state":"disabled",
        "enforcement_ready":false,"accounting":"unknown"}
    }

旧服务没有该路由时，CLI/MCP只提供已知的只读能力；月额、多机、follow、native argv均返回unknown/false，不能猜。

可先不改后端：whoami、doctor的基础信息、queue/show/gpu/quota只读视图、validate、现有logs/cancel/application/rules/quota/audit适配、shell兼容、退出码和JSON渲染。必须改后端后才能宣称完整支持：capabilities/schema/outcome、请求正文哈希冲突、原生argv、多机字段、allocation generation、日志offset/follow、外部任务导入、月额controller和可信远程actor审计。

兼容顺序：先适配旧 envelope并只读发布；后端追加字段/路由而不改旧字段；看到schema 1.1后启用新能力；月额只有controller交付后才从disabled/staged/unknown改为enforcing。

## 6. Slurm和月额边界

已安装 job_submit.lua 在 submit_uid非0时拒绝提交，要求 pro6000d-job:<32 hex> comment，拒绝 root作业 UID，并只允许 root 修改资源（/etc/slurm-llnl/job_submit.lua:4-25）。因此：

- CLI不是用户直接sbatch；
- MCP不能直接sbatch或srun绕过broker；
- time run是平台batch提交，不是交互式srun；
- 直接sbatch失败应解释为策略拒绝，而不是提示用户绕过。

暂存 monthly_quota.py:1-5、86-112、204-210 明确是 staged ledger且 enforcement_ready=false；MONTHLY_CONTROLLER_CONTRACT.md:1-3、27-39明确当前没有权威ALLOW socket；MONTHLY-GATE.md:133-143要求月额作为独立activation。故：

- time quota显示并发配额；月账显示未启用/未知，不显示0或可用余额；
- time request monthly和对应MCP tool在 enforcing前禁用，返回 feature_not_enabled或兼容旧码 monthly_not_enabled；
- 不把 debug 5、important 100、normal 500 GPU小时当当前生效政策；
- 不用Prolog、SPANK、UI badge或轮询余额宣称严格PENDING gate；
- 网页、CLI、MCP在未来都只读同一个controller权威结果。

## 7. 分阶段实施、验收和回退

| 阶段 | 工作和依赖 | 验收/回退 |
|---|---|---|
| P0 证据冻结（P0） | 管理员只读确认两台目标机的API SHA、systemd active、socket owner/mode、Slurm版本/config/plugin、capacity和账户组。 | 形成安装文件/运行状态/暂存源码三列；未确认时CLI/MCP只读。无训练、无改配置。 |
| P1 只读 time CLI/MCP（P0） | 实现 timectl 入口、冲突帮助、whoami/doctor/capabilities/queue/show/gpu/quota/logs；复用client。 | 普通用户仅本人、管理员按权限；单JSON、稳定退出码；写命令关闭即可回退。 |
| P2 validate和受控提交（P0） | dry-run、argv保真、request ID持久化；后端正文哈希冲突和恢复查询；保留root broker。 | 覆盖空格/引号/$、资源边界、断线/重复；绝不fallback到sbatch。未知写结果按ID查询。 |
| P3 取消、日志和申请（P1/P2） | 统一outcome；日志尾部元数据；applications、审批、quota/rules preview/apply、审计的CLI/MCP。 | cancel单对象且非交互需--yes；preview过期安全；日志不执行内容。可关闭写入回到P1。 |
| P4 统一契约/网页迁移（P1-P3） | capabilities、schema 1.1、requested/allocated分离、状态单位、generation、actor audit。 | 旧Cockpit继续可用；新客户端按能力开启；多机未实现时明确拒绝。 |
| P5 月额权威controller（最高风险、独立审批） | allocation commit前判断、整数GPU秒、跨月、并发、恢复、requeue/restart、grant幂等、pending reason、审计；按已安装Slurm版本分别适配。 | 证明耗尽时PENDING不获得GPU，运行中继续计费，未知恢复fail-closed。未完成始终disabled/staged；只回退新增代码。 |
| P6 多机和日志流 | 设计nodes/ntasks/rank/rendezvous、跨节点日志offset和轮转恢复。 | 没有多机权威状态不发布nodes>1；follow必须证明重复/丢失语义。 |

后续测试只提出、不在本次运行：shell冲突；argv特殊字符；资源/path/environment边界；单/多GPU和torchrun；普通用户/管理员/直接sbatch；提交断线、超时、同ID不同正文；队列状态、取消、UNKNOWN；日志截断/符号链接/轮转；审批并发与审计；月额跨月、超额、追加、恢复和真实allocation gate。测试必须在单独授权窗口进行。

