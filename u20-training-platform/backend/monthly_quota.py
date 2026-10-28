"""Staged monthly ledger. Scheduler start enforcement is intentionally not enabled.

Only authoritative, continuous sacct allocation intervals are accepted. Requeue,
suspension, resizing and incomplete accounting require operator reconciliation;
we never extrapolate from polling time or treat absent records as zero usage.
"""
import datetime as dt
import hashlib
import json
import os
import re
import selectors
import subprocess
import time

UTC = dt.timezone.utc
BEIJING = dt.timezone(dt.timedelta(hours=8))
LIMITS = (("debug", 5 * 3600), ("important", 100 * 3600), ("normal", 500 * 3600))
LIMITATION = "月额功能暂未启用。目标是余额耗尽时任务继续排队、不得分配 GPU，已运行任务继续并记录超额。目前只有月账与界面暂存实现，尚不能控制 Slurm 在资源分配前暂停任务；不会用启动后拒绝或终止任务代替排队。"


def epoch(value):
    if not value or value in ("Unknown", "None", "N/A"):
        return None
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return int(parsed.replace(tzinfo=UTC).timestamp()) if parsed.tzinfo is None else int(parsed.timestamp())


def month_now():
    return dt.datetime.now(BEIJING).strftime("%Y-%m")


def split_months(start, end, gpu):
    while start < end:
        local = dt.datetime.fromtimestamp(start, BEIJING)
        boundary = local.replace(year=local.year+1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0) if local.month == 12 else local.replace(month=local.month+1, day=1, hour=0, minute=0, second=0, microsecond=0)
        stop = min(end, int(boundary.timestamp()))
        yield local.strftime("%Y-%m"), (stop-start)*gpu
        start = stop


class MonthlyLedger:
    def __init__(self, service):
        self.service = service
        self.db = service.store.db
        self.config = service.config.get("monthly_quota", {})
        self.enabled = bool(self.config.get("enabled", False))
        self.status = "unknown" if self.enabled else "disabled"
        self.error = None
        self.last_poll = 0
        self.observed_at = None
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS monthly_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS monthly_allocations(
            allocation_key TEXT PRIMARY KEY,db_index TEXT NOT NULL UNIQUE,scheduler_id TEXT NOT NULL,
            uid INTEGER NOT NULL,submit_epoch INTEGER NOT NULL,start_epoch INTEGER NOT NULL,
            end_epoch INTEGER NOT NULL,gpu INTEGER NOT NULL,terminal INTEGER NOT NULL,
            source_json TEXT NOT NULL,observed_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS monthly_usage(
            allocation_key TEXT NOT NULL,uid INTEGER NOT NULL,month TEXT NOT NULL,
            gpu_seconds INTEGER NOT NULL CHECK(gpu_seconds>=0),
            PRIMARY KEY(allocation_key,month));
          CREATE TABLE IF NOT EXISTS monthly_additions(
            application_id TEXT PRIMARY KEY,uid INTEGER NOT NULL,month TEXT NOT NULL,
            gpu_seconds INTEGER NOT NULL CHECK(gpu_seconds>0),actor_uid INTEGER NOT NULL,
            reason TEXT NOT NULL,created_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS monthly_requests(
            uid INTEGER NOT NULL,request_id TEXT NOT NULL,application_id TEXT NOT NULL,
            PRIMARY KEY(uid,request_id));
          CREATE TABLE IF NOT EXISTS monthly_notices(
            uid INTEGER NOT NULL,month TEXT NOT NULL,event TEXT NOT NULL,
            PRIMARY KEY(uid,month,event));
        ''')
        stored = self.db.execute("SELECT value FROM monthly_meta WHERE key='enabled_at'").fetchone()
        configured = self.config.get("enabled_at")
        self.cutover = epoch(stored[0]) if stored else None
        if self.enabled and self.cutover is None:
            if not configured or not re.search(r'(Z|[+-]\d\d:\d\d)$', configured):
                self.error = "必须明确设置含时区的 monthly_quota.enabled_at，不能自动回溯计费"
            else:
                self.cutover = epoch(configured)
                self.db.execute("INSERT INTO monthly_meta VALUES('enabled_at',?)", (configured,))
        if self.enabled and stored and configured and epoch(configured) != self.cutover:
            self.error = "计费启用时间与持久账本不同，拒绝改写"

    def summary(self, uid):
        month = month_now()
        used = self.db.execute("SELECT COALESCE(SUM(gpu_seconds),0) FROM monthly_usage WHERE uid=? AND month=?", (uid,month)).fetchone()[0]
        extra = self.db.execute("SELECT COALESCE(SUM(gpu_seconds),0) FROM monthly_additions WHERE uid=? AND month=?", (uid,month)).fetchone()[0]
        remaining_used = used
        tiers = []
        for name, base in LIMITS:
            limit = base + (extra if name == 'normal' else 0)
            consumed = min(remaining_used,limit)
            tiers.append(dict(name=name,limit_gpu_seconds=limit,used_gpu_seconds=consumed,remaining_gpu_seconds=limit-consumed))
            remaining_used -= consumed
        total = sum(limit for _,limit in LIMITS) + extra
        result = dict(enabled=self.enabled,mode='staged',enforcement_ready=False,enforcement_limitation=LIMITATION,
                    accounting_status=self.status,accounting_error=self.error,observed_at=self.observed_at,
                    enabled_at=dt.datetime.fromtimestamp(self.cutover,UTC).isoformat() if self.cutover is not None else None,
                    timezone='Asia/Shanghai',month=month,used_gpu_seconds=used,remaining_gpu_seconds=max(0,total-used),
                    overage_gpu_seconds=max(0,used-total),current_tier=next((t['name'] for t in tiers if t['remaining_gpu_seconds']>0),None),
                    additional_gpu_seconds=extra,added_normal_gpu_seconds=extra,tiers=tiers)
        result['usage_verified'] = self.enabled and self.status == 'ok'
        result['last_known_used_gpu_seconds'] = used if self.cutover is not None else None
        if not result['usage_verified']:
            for key in ('used_gpu_seconds','remaining_gpu_seconds','overage_gpu_seconds','current_tier'):
                result[key] = None
            for tier in tiers:
                tier['used_gpu_seconds'] = None
                tier['remaining_gpu_seconds'] = None
        return result

    @staticmethod
    def query(argv, slurm_conf):
        env = {"PATH":"/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/bin", "LANG":"C", "TZ":"UTC", "SLURM_TIME_FORMAT":"standard"}
        if slurm_conf: env['SLURM_CONF'] = slurm_conf
        proc = subprocess.Popen(argv,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,env=env)
        selector = selectors.DefaultSelector(); selector.register(proc.stdout,selectors.EVENT_READ)
        deadline = time.monotonic()+8; data = bytearray()
        try:
            while time.monotonic()<deadline:
                if not selector.select(max(0,deadline-time.monotonic())): break
                block = os.read(proc.stdout.fileno(),65536)
                if not block:
                    if proc.wait(timeout=max(.01,deadline-time.monotonic())) != 0: raise ValueError('sacct 查询失败或字段不受支持')
                    return data.decode('utf-8','strict')
                data.extend(block)
                if len(data)>4*1024*1024: raise ValueError('sacct 输出超限，需分段补账')
            raise ValueError('sacct 查询超时')
        finally:
            selector.close()
            if proc.poll() is None: proc.kill()
            proc.wait(); proc.stdout.close()

    def refresh(self, force=False):
        if not self.enabled or self.cutover is None or (self.error and self.last_poll == 0): return
        if not force and time.monotonic()-self.last_poll < 15: return
        self.last_poll = time.monotonic()
        try:
            if self.config.get('accounting_source') != 'sacct_continuous_v1':
                raise ValueError('未配置受信 sacct_continuous_v1 数据源；u20 需先配置可靠历史会计')
            since = dt.datetime.fromtimestamp(self.cutover,UTC).strftime('%Y-%m-%dT%H:%M:%S')
            output = self.query(['sacct','--duplicates','--allocations','--noheader','--parsable2','--starttime',since,
                                '--format=DBIndex,JobIDRaw,UID,Submit,Start,End,ElapsedRaw,AllocTRES,State,Suspended,JobName%128'], self.service.config.get('slurm_conf'))
            parsed = {}; seen_sids = set(); now = dt.datetime.now(UTC).isoformat()
            for line in output.splitlines():
                if not line.strip(): continue
                fields = line.split('|')
                if len(fields) != 11: raise ValueError('sacct 返回字段不完整')
                idx,sid,uid_s,submit_s,start_s,end_s,elapsed_s,tres,state,suspended,job_name = fields
                if not sid.isdigit() or not idx.isdigit() or not uid_s.isdigit(): raise ValueError('不支持的数组/异构任务或会计标识')
                uid = int(uid_s)
                try: self.service.identity(uid)
                except Exception: continue
                if not start_s or start_s in ('Unknown','None','N/A'): continue
                start,submit,end = epoch(start_s),epoch(submit_s),epoch(end_s)
                if submit is None or not elapsed_s.isdigit(): raise ValueError('缺少提交身份或实际占用秒数')
                elapsed = int(elapsed_s)
                if suspended not in ('0','00:00:00','00:00','0-00:00:00'): raise ValueError('挂起任务需要分段会计证据，暂停新派发')
                terminal = state.split()[0].split('+')[0] not in ('RUNNING','COMPLETING','SUSPENDED','PENDING','CONFIGURING','RESIZING')
                if state.startswith(('REQUEUED','RESIZING')): raise ValueError('重排队/调整资源需要分段会计证据')
                if end is not None and abs((end-start)-elapsed)>1: raise ValueError('实际时长与分配区间不一致，不能推算跨月账本')
                stop = start+elapsed
                if terminal and end is None: raise ValueError('结束任务缺少可靠结束时间')
                resources = dict(x.split('=',1) for x in tres.split(',') if '=' in x)
                value = resources.get('gres/gpu')
                if value is None:
                    typed = [v for k,v in resources.items() if k.startswith('gres/gpu:')]
                    value = str(sum(int(v) for v in typed)) if typed and all(v.isdigit() for v in typed) else None
                if value is None or not value.isdigit(): raise ValueError('实际分配 GPU 数量未知，不能按零计费')
                gpu = int(value)
                key = hashlib.sha256(('|'.join((idx,sid,uid_s,submit_s,start_s))).encode()).hexdigest()
                old = self.db.execute('SELECT * FROM monthly_allocations WHERE db_index=?',(idx,)).fetchone()
                if old and (old['allocation_key']!=key or old['gpu']!=gpu or old['end_epoch']>stop):
                    raise ValueError('分配发生重排队、资源变化或证据回退，需要分段补账')
                if idx in parsed: raise ValueError('同一会计记录出现多个分配区间')
                parsed[idx] = (key,idx,sid,uid,submit,start,stop,gpu,int(terminal),json.dumps(fields),now)
                seen_sids.add(sid)
            tracked = self.db.execute("SELECT scheduler_id FROM jobs WHERE state IN ('RUNNING','COMPLETING','SUSPENDED') AND scheduler_id IS NOT NULL").fetchall()
            if any(r[0] not in seen_sids for r in tracked): raise ValueError('活动分配尚未出现在会计源，冻结新派发')
            outstanding = self.db.execute('SELECT db_index FROM monthly_allocations WHERE terminal=0').fetchall()
            if any(r[0] not in parsed for r in outstanding): raise ValueError('历史活动分配证据缺失，需补账')
            self.db.execute('BEGIN IMMEDIATE')
            try:
                for values in parsed.values():
                    key,idx,sid,uid,submit,start,stop,gpu,terminal,source,observed = values
                    self.db.execute('INSERT INTO monthly_allocations VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(allocation_key) DO UPDATE SET end_epoch=excluded.end_epoch,terminal=excluded.terminal,source_json=excluded.source_json,observed_at=excluded.observed_at',values)
                    self.db.execute('DELETE FROM monthly_usage WHERE allocation_key=?',(key,))
                    for month,seconds in split_months(max(start,self.cutover),stop,gpu):
                        self.db.execute('INSERT INTO monthly_usage VALUES(?,?,?,?)',(key,uid,month,seconds))
                self.db.execute('COMMIT')
            except Exception:
                self.db.execute('ROLLBACK'); raise
            self.status='ok'; self.error=None; self.observed_at=now
            for uid in set(v[3] for v in parsed.values()):
                summary = self.summary(uid)
                event = summary['current_tier'] or 'exhausted'
                if self.db.execute('INSERT OR IGNORE INTO monthly_notices VALUES(?,?,?)',(uid,summary['month'],event)).rowcount:
                    self.service.add_notification(uid,'monthly_quota','月度额度状态变化','当前档位：'+event+'；月额功能仍处于暂存状态，未开启强制启动限制。')
        except Exception as exc:
            self.status='unknown'; self.error=str(exc)[:400]

    def block_reason(self, uid):
        if not self.enabled: return None
        if self.status != 'ok': return '月度计费证据不完整，暂停新的平台派发：'+(self.error or '等待会计同步')
        if self.summary(uid)['remaining_gpu_seconds'] <= 0: return '本月 GPU 小时已耗尽；运行任务继续记超额，等待追加额度'
        # Intentionally fail closed if someone enables accounting before a proven
        # scheduler gate. Never pretend post-allocation SPANK is pending control.
        return '月额启动控制尚未完成；仅保留尚未提交的任务在平台队列，不能保证 Slurm 中已有排队任务不获分配'

    def job_usage(self, row):
        if not self.enabled or self.status != 'ok': return None
        if not row['scheduler_id']: return 0
        records = self.db.execute('SELECT a.*,COALESCE(u.gpu_seconds,0) AS seconds FROM monthly_allocations a LEFT JOIN monthly_usage u ON a.allocation_key=u.allocation_key AND u.month=? WHERE a.scheduler_id=? AND a.uid=?',(month_now(),row['scheduler_id'],row['owner_uid'])).fetchall()
        matched = []
        for record in records:
            source = json.loads(record['source_json'])
            if len(source) != 11: continue
            if 'origin' in row.keys() and row['origin'] == 'external':
                if not row['source_submit_time']: continue
                # scontrol's local display is not assumed UTC: matching text
                # cannot prove identity across zones, so require configured TZ.
                if self.service.config.get('slurm_display_timezone') != 'UTC': continue
                if epoch(row['source_submit_time']) != record['submit_epoch']: continue
                if source[10] != row['source_scheduler_name']: continue
            elif source[10] != 'p6-'+row['id']:
                continue
            matched.append(record['seconds'])
        return sum(matched) if matched else None
