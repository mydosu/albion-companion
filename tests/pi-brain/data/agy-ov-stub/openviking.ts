/**
 * 自检用的假 OpenViking 扩展（2026-11-28）。
 *
 * 官方 openviking-pi-extension 的源码不进仓库（见 brain/agent/openviking/来源与配置.md），
 * 所以这里只**照它的契约**写一支最小的：session_start 时读一遍 OV 环境变量，turn_end 时把
 * `ctx.sessionManager.getBranch()` 里新增的条目发出去（官方是对着分支做增量捕获），
 * session_shutdown 时发一次 commit。宿主要证明的就是「这三个钩子会来、带来的是能落库的东西」。
 *
 * 官方扩展在 bypass 会话里整段跳过（isBypassed），这里照做：探针会话不该有任何请求。
 */
export default function (pi: any) {
  const endpoint = (): string => String(process.env.OPENVIKING_ENDPOINT || "").replace(/\/+$/, "");
  const bypassed = (): boolean => String(process.env.OPENVIKING_BYPASS_SESSION || "") === "1";

  const post = async (event: string, body: any): Promise<void> => {
    if (!endpoint()) return;
    try {
      await fetch(`${endpoint()}/__check/${event}`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      });
    } catch {
      /* 假端点挂了不影响宿主，自检自己会报数 */
    }
  };

  // 官方 sync.syncBranch 的水位线：分支条数，只发新增的那截
  let seen = 0;

  pi.on("session_start", async (_event: any, ctx: any) => {
    if (bypassed()) return;
    await post("session_start", {
      session: ctx.sessionManager.getSessionId(),
      user: String(process.env.OPENVIKING_USER || ""),
      account: String(process.env.OPENVIKING_ACCOUNT || ""),
      agent: String(process.env.OPENVIKING_AGENT || ""),
      url: String(process.env.OPENVIKING_URL || ""),
      has_key: Boolean(process.env.OPENVIKING_API_KEY),
      auto_recall: String(process.env.OPENVIKING_AUTO_RECALL || ""),
    });
  });

  pi.on("turn_end", async (_event: any, ctx: any) => {
    if (bypassed()) return;
    const branch = ctx.sessionManager.getBranch();
    const fresh = Array.isArray(branch) ? branch.slice(seen) : [];
    seen = Array.isArray(branch) ? branch.length : seen;
    await post("turn_end", { session: ctx.sessionManager.getSessionId(), entries: fresh });
  });

  pi.on("session_shutdown", async (_event: any, ctx: any) => {
    if (bypassed()) return;
    await post("commit", {
      session: ctx.sessionManager.getSessionId(),
      branch_entries: (ctx.sessionManager.getBranch() ?? []).length,
    });
  });
}
