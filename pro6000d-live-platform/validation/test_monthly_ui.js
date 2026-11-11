'use strict';
const assert = require('assert');
const ui = require('../cockpit-plugin/pro6000d-training/monthly-ui.js');
const tiers = [
  {name:'debug',limit_gpu_seconds:18000,used_gpu_seconds:3600,remaining_gpu_seconds:14400},
  {name:'important',limit_gpu_seconds:360000,used_gpu_seconds:0,remaining_gpu_seconds:360000},
  {name:'normal',limit_gpu_seconds:1800000,used_gpu_seconds:0,remaining_gpu_seconds:1800000}
];
const staged = {month:'2026-11',usage_verified:true,enforcement_ready:false,selectable_tiers_ready:false,overage_gpu_seconds:0,tiers};
const view = ui.panel(staged);
assert(view.includes('状态未确认') && view.includes('debug') && view.includes('1.00') && view.includes('5.00'));
assert(!ui.selector(staged, 'debug').includes('<select'));
assert(!ui.panel(staged).includes('monthly-apply'));
assert(ui.panel({usage_verified:false, enforcement_ready:false, tiers}).includes('状态未确认'));
assert(!ui.panel({usage_verified:false, enforcement_ready:false, tiers}).includes('5.00'));
const active = {...staged, enforcement_ready:true, selectable_tiers_ready:true, request_available:true};
assert(ui.panel(active).includes('data-action="monthly-apply"'));
assert(ui.panel(active).includes('账面剩余') && ui.panel(active).includes('未消耗预留'));
assert(ui.selector(active,'important').includes('name="selected_tier"'));
assert(ui.selector(active,'important').includes('value="important" selected'));
assert(ui.panel({state:'unknown',usage_verified:false,enforcement_ready:false,enforcement_limitation:'月度强制调度尚未启用。'}).includes('状态未确认'));
assert(!ui.panel({...staged,month:'<script>'}).includes('<script>'));
console.log('PASS: staged versus active quota display, tier selection, escaping');
