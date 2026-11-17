"""Checks common to the controller transport and priority policy store.

The caller must authenticate the controller and establish its expected epoch
on the same connection. Checking fields does not authenticate a peer.
"""
import re
import json


def validate_epoch(epoch):
    if not isinstance(epoch,str) or not re.fullmatch(r'[0-9a-f]{32}',epoch):
        raise ValueError('controller epoch is not verified')


def validate_priority_payload(payload):
    fields = {'action','actor_uid','actor_name','uid','username','enabled','expected_enabled','expected_version','identity_generation'}
    if not isinstance(payload,dict) or set(payload) != fields or payload.get('action') != 'set_top_priority':
        raise ValueError('unknown or malformed priority action')
    if any(type(payload[k]) is not int or not 0 <= payload[k] <= 0xffffffff for k in ('actor_uid','uid')):
        raise ValueError('invalid Unix uid')
    name = payload['username']
    # Match the controller's currently supported canonical JSON subset before
    # the request enters the ordered outbox. Unsupported names fail closed.
    if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', name):
        raise ValueError('invalid Unix username')
    name = payload['actor_name']
    if not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9._-]{1,128}', name):
        raise ValueError('invalid administrator username')
    if type(payload['enabled']) is not bool or type(payload['expected_enabled']) is not bool or payload['enabled'] == payload['expected_enabled']:
        raise ValueError('invalid priority transition')
    if type(payload['expected_version']) is not int or not 0 <= payload['expected_version'] < 2**53-1:
        raise ValueError('invalid priority version')
    generation = payload['identity_generation']
    if not isinstance(generation,str) or not re.fullmatch(r'[0-9a-f]{32}',generation):
        raise ValueError('account generation is not verified')


def request_audit_detail(event):
    payload = event['payload']
    return json.dumps({'uid':payload['uid'],'identity_generation':payload['identity_generation'],
                       'digest':event['digest']},sort_keys=True,separators=(',', ':'),ensure_ascii=False)


def verify_controller_result(event, result, expected_epoch):
    validate_epoch(expected_epoch)
    p = event.get('payload') if isinstance(event,dict) else None
    validate_priority_payload(p)
    if (not isinstance(result,dict) or type(result.get('uid')) is not int or
            type(result.get('enabled')) is not bool or type(result.get('version')) is not int or
            result.get('uid') != p['uid'] or result.get('enabled') is not p['enabled'] or
            result.get('version') != p['expected_version']+1 or result.get('epoch') != expected_epoch or
            result.get('identity_generation') != p['identity_generation']):
        raise ValueError('controller applied state, version or epoch did not match')
    return True
