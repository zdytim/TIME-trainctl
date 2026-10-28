/* Compatibility view helper. The controller remains the allocation authority. */
export async function monthlyQuota(api, uid) {
  const state = await api({method: "GET", path: "/api/v1/state"});
  const quota = state.monthly_quota;
  if (!quota || quota.uid && Number(quota.uid) !== Number(uid)) {
    throw new Error("monthly quota identity mismatch");
  }
  return quota;
}

export function quotaStatus(quota) {
  if (!quota || quota.accounting_status !== "ok" || !quota.usage_verified) {
    return {state: "unknown", message: "控制器状态未知；新 GPU 分配由 Slurmctld 安全拒绝"};
  }
  return {state: "ok", message: `${quota.month} 剩余 ${quota.remaining_gpu_seconds} GPU 秒`};
}
