#!/usr/bin/python3 -I
"""Provision ordinary training accounts through the privileged Cockpit bridge."""

import fcntl
import grp
import json
import os
from pathlib import Path
import pwd
import re
import sqlite3
import subprocess
import sys
import syslog
import time


STATE = Path('/var/lib/pro6000d-training')
CONFIG = Path('/etc/pro6000d-training/config.json')
USERNAME = re.compile(r'[a-z_][a-z0-9_-]{0,31}')
FORBIDDEN = {'sudo', 'admin', 'root', 'wheel', 'docker', 'lxd', 'incus-admin', 'disk', 'video', 'render'}


class ProvisionError(Exception):
    pass


def run(command, error, *, input_text=None, env=None):
    try:
        result = subprocess.run(command, input=input_text, stdin=None if input_text is not None else subprocess.DEVNULL,
                                text=True, capture_output=True, timeout=6, env=env)
    except (OSError, subprocess.TimeoutExpired):
        raise ProvisionError(error) from None
    if result.returncode:
        raise ProvisionError(error)
    return result.stdout


def ordinary_accounts(config):
    minimum, maximum = 1000, 60000
    for line in Path('/etc/login.defs').read_text().splitlines():
        fields = line.partition('#')[0].split()
        if len(fields) == 2 and fields[0] == 'UID_MIN':
            minimum = max(minimum, int(fields[1]))
        if len(fields) == 2 and fields[0] == 'UID_MAX':
            maximum = min(maximum, int(fields[1]))
    forbidden = FORBIDDEN | {config['admin_group'], config['job_group']}
    accounts = {}
    for line in Path('/etc/passwd').read_text().splitlines():
        username, _, uid, _, _, _, _ = line.split(':')
        if not USERNAME.fullmatch(username) or not minimum <= int(uid) <= maximum:
            continue
        user = pwd.getpwnam(username)
        if user.pw_uid != int(uid) or pwd.getpwuid(user.pw_uid).pw_name != username:
            continue
        if Path(user.pw_shell).name in ('nologin', 'false'):
            continue
        groups = {grp.getgrgid(gid).gr_name for gid in os.getgrouplist(username, user.pw_gid)}
        if not groups & forbidden:
            accounts[username] = (user, groups)
    return accounts, minimum, maximum


def check_sudo(username, env):
    result = subprocess.run(['/usr/bin/sudo', '-n', '-l', '-U', username],
                            stdin=subprocess.DEVNULL, capture_output=True, text=True, env=env, timeout=5)
    response = '\n'.join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    denial = r'User ' + re.escape(username) + r' is not allowed to run sudo on [^\s]+\.?'
    if result.returncode not in (0, 1) or not re.fullmatch(denial, response):
        raise ProvisionError('无法确认这是没有 sudo 权限的普通账户，请检查系统账户。')


def check_training_host(config, env):
    if (config.get('slurm_account'), config.get('partition'), config.get('unlimited_qos', 'long')) != ('training', 'gpu', 'long'):
        raise ProvisionError('当前服务器的训练调度配置与开通规则不匹配。')
    active_config = run(['/usr/local/bin/scontrol', 'show', 'config'], '无法读取 Slurm 配置。', env=env)
    if not re.search(r'^ClusterName\s*=\s*pro6000d\s*$', active_config, re.MULTILINE):
        raise ProvisionError('无法确认当前 Slurm 集群是 pro6000d。')
    partition = run(['/usr/local/bin/scontrol', '-o', 'show', 'partition', 'gpu'],
                    '无法读取 Slurm GPU 分区。', env=env)
    policy = dict(field.split('=', 1) for field in partition.split() if '=' in field)
    if policy.get('PartitionName') != 'gpu':
        raise ProvisionError('无法确认当前 Slurm 分区是 gpu。')
    if (policy.get('AllowAccounts') not in ('ALL', None) and
            'training' not in policy['AllowAccounts'].split(',')) or 'training' in policy.get('DenyAccounts', '').split(','):
        raise ProvisionError('Slurm GPU 分区当前不允许 training 账户。')
    if (policy.get('AllowQos') not in ('ALL', None) and
            'long' not in policy['AllowQos'].split(',')) or 'long' in policy.get('DenyQos', '').split(','):
        raise ProvisionError('Slurm GPU 分区当前不允许训练所需的 long QoS。')
    device_policy = run(['/usr/bin/systemctl', 'show', 'user.slice', '--property=DevicePolicy', '--value'],
                        '无法核对系统账户的 GPU 隔离。', env=env).strip()
    if device_policy != 'closed':
        raise ProvisionError('系统账户的 GPU 隔离尚未启用。')


def validate_history(config, username, uid=None):
    """Reserve a fresh UID past broker history and reject conflicting identities."""
    db_path = Path(config.get('state_db', STATE / 'state.sqlite3'))
    with sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True, timeout=3) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        identities = db.execute('''
            SELECT owner_uid, owner_name FROM jobs WHERE owner_uid=? OR owner_name=?
            UNION SELECT owner_uid, owner_name FROM applications WHERE owner_uid=? OR owner_name=?
            UNION SELECT uid, username FROM quota_overrides WHERE uid=? OR username=?
        ''', (uid, username) * 3).fetchall()
        if 'unix_account_generations' in tables:
            identities += db.execute('SELECT uid, username FROM unix_account_generations WHERE uid=? OR username=?',
                                     (uid, username)).fetchall()
        if any(identity != (uid, username) for identity in identities):
            raise ProvisionError('这个用户名或 UID 与历史训练账户冲突，请使用新的用户名。')
        if uid is None:
            columns = {'unix_account_generations': 'uid', 'jobs': 'owner_uid', 'applications': 'owner_uid',
                       'quota_overrides': 'uid', 'monthly_dispatch_holds': 'uid', 'monthly_usage': 'uid',
                       'monthly_additions': 'uid', 'monthly_grant_outbox': 'target_uid'}
            return max(db.execute(f'SELECT coalesce(max({col}), 0) FROM {table} WHERE {col} BETWEEN 1000 AND 60000')
                       .fetchone()[0] for table, col in columns.items() if table in tables)


def slurm_users(username, env, *, history=False):
    command = ['/usr/local/bin/sacctmgr', '-nP', 'show', 'user']
    if history:
        command.append('withdeleted')
    output = run(command + ['where', 'name=' + username, 'format=User%128,AdminLevel'],
                 '无法读取 Slurm 账户。', env=env)
    return [level for name, level in (line.split('|') for line in output.splitlines()) if name == username]


def associations(username, env):
    output = run(['/usr/local/bin/sacctmgr', '-nP', 'show', 'assoc', 'where', 'user=' + username,
                  'cluster=pro6000d', 'account=training', 'format=User%128,Partition%128,QOS%256,ID'],
                 '无法读取 Slurm training 关联。', env=env)
    return [(part, set(qos.split(',')), int(assoc_id)) for name, part, qos, assoc_id in
            (line.split('|') for line in output.splitlines()) if name == username]


def ensure_association(user, env):
    username = user.pw_name
    if any(level != 'None' for level in slurm_users(username, env)):
        raise ProvisionError('该账户已有 Slurm 管理权限，请在系统管理中单独处理。')
    existing = associations(username, env)
    if not any(part in ('', 'gpu') for part, _, _ in existing):
        run(['/usr/local/bin/sacctmgr', '-i', 'add', 'user', 'name=' + username, 'cluster=pro6000d',
             'account=training', 'partition=gpu', 'qos=debug,long,normal', 'defaultqos=normal'],
            'Slurm training 关联未确认，可以重试开通。', env=env)
        existing = associations(username, env)
    # The partition-specific association takes precedence over the account-wide one.
    applicable = [row for row in existing if row[0] == 'gpu'] or [row for row in existing if not row[0]]
    if len(applicable) != 1 or 'long' not in applicable[0][1]:
        raise ProvisionError('已有 Slurm 关联与 GPU 调度规则不匹配，请由管理员核对该关联。')
    partition, _, assoc_id = applicable[0]
    record = (f'ClusterName=pro6000d Account=training UserName={username}({user.pw_uid}) '
              f'Partition={partition} ')
    for _ in range(5):
        output = run(['/usr/local/bin/scontrol', 'show', 'assoc_mgr', 'flags=assoc,qos',
                      'users=' + username, 'accounts=training', 'qos=long'],
                     '无法核对 Slurm 控制器中的权限。', env=env)
        rows = output.splitlines()
        if (any(line.startswith(record) and f'ID={assoc_id}' in line.split() for line in rows) and
                any(re.fullmatch(r'QOS=long\([0-9]+\)', line) for line in rows)):
            return
        time.sleep(0.3)
    raise ProvisionError('Slurm 控制器尚未确认权限同步，请稍后重试。')


def apply(config, request):
    mode, username = request.get('mode'), request.get('username')
    if mode not in ('create', 'existing') or not isinstance(username, str) or not USERNAME.fullmatch(username):
        raise ProvisionError('请输入有效的 Linux 用户名。')
    password = request.get('password') if mode == 'create' else None
    if mode == 'create' and (not isinstance(password, str) or not 12 <= len(password) <= 128 or
                             any(char in password for char in '\x00\n\r')):
        raise ProvisionError('登录密码需为 12 至 128 个字符，且不能包含换行。')
    env = {'PATH': '/usr/local/bin:/usr/sbin:/usr/bin', 'LC_ALL': 'C', 'SLURM_CONF': config['slurm_conf']}
    check_training_host(config, env)
    accounts, minimum, maximum = ordinary_accounts(config)
    if mode == 'create':
        try:
            pwd.getpwnam(username)
        except KeyError:
            pass
        else:
            raise ProvisionError('同名账户已存在。请切换到「开通已有账户」。')
        previous_max = validate_history(config, username)
        if slurm_users(username, env, history=True):
            raise ProvisionError('同名账户已有 Slurm 历史，请使用新的用户名。')
        if os.path.lexists(Path('/home') / username):
            raise ProvisionError('同名主目录已存在，请使用新的用户名。')
        used = (user.pw_uid for user in pwd.getpwall() if minimum <= user.pw_uid <= maximum)
        uid = max(previous_max, max(used, default=0), minimum - 1) + 1
        if uid > maximum:
            raise ProvisionError('没有可安全分配的新 UID，请联系服务器管理员。')
        run(['/usr/sbin/useradd', '--uid', str(uid), '--create-home', '--home-dir', '/home/' + username,
             '--user-group', '--shell', '/bin/bash', '--', username],
            '创建 Linux 账户未确认，请到「开通已有账户」检查是否已创建。', env=env)
        accounts, _, _ = ordinary_accounts(config)
        if username not in accounts or accounts[username][0].pw_uid != uid:
            raise ProvisionError('新账户的 Unix 身份未核验，请在系统账户管理中检查。')
    if username not in accounts:
        raise ProvisionError('未找到可开通的普通本机账户。')
    user, groups = accounts[username]
    validate_history(config, username, user.pw_uid)
    try:
        check_sudo(username, env)
        ensure_association(user, env)
        if pwd.getpwnam(username) != user:
            raise ProvisionError('账户身份已变化，请重新核对。')
        training_group = config['training_group']
        if training_group not in groups:
            run(['/usr/sbin/usermod', '--append', '--groups', training_group, '--', username],
                'Slurm 权限已核验，加入训练组未确认，请重试开通。', env=env)
        if grp.getgrnam(training_group).gr_gid not in os.getgrouplist(username, user.pw_gid):
            raise ProvisionError('训练组成员身份未核验，请重试开通。')
    except ProvisionError as exc:
        if mode == 'create':
            raise ProvisionError(str(exc) + ' Linux 账户已创建，登录密码尚未设置。可从「开通已有账户」重试，再到「系统账户管理」设置密码。') from None
        raise
    if mode == 'create':
        run(['/usr/sbin/chpasswd'], '训练权限已开通，登录密码设置失败。请在「系统账户管理」设置密码。',
            input_text=f'{username}:{password}\n', env=env)
    syslog.syslog(syslog.LOG_INFO, f'training account {mode} complete username={username} uid={user.pw_uid}')
    return {'username': username, 'uid': user.pw_uid}


def main():
    if os.geteuid() != 0:
        raise SystemExit('root required')
    syslog.openlog('pro6000d-provision-account', facility=syslog.LOG_AUTHPRIV)
    try:
        config = json.loads(CONFIG.read_text())
        raw = sys.stdin.buffer.readline(4097)
        if not raw.endswith(b'\n') or len(raw) > 4096:
            raise ProvisionError('账户开通请求格式无效。')
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ProvisionError('账户开通请求格式无效。')
        if request.get('mode') == 'list':
            accounts, _, _ = ordinary_accounts(config)
            data = {'accounts': sorted(accounts)}
        else:
            fd = os.open(STATE / 'account-provision.lock', os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                data = apply(config, request)
            except BlockingIOError:
                raise ProvisionError('另一笔账户开通正在进行，请稍后重试。') from None
            finally:
                os.close(fd)
        response = {'ok': True, 'data': data}
    except ProvisionError as exc:
        response = {'ok': False, 'message': str(exc)}
    except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        syslog.syslog(syslog.LOG_ERR, 'account provisioning failed: ' + type(exc).__name__)
        response = {'ok': False, 'message': '无法确认账户开通结果，请核对后重试。'}
    print(json.dumps(response, ensure_ascii=False))


if __name__ == '__main__':
    main()
