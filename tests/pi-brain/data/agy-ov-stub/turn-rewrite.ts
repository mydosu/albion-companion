/**
 * 自检用的假 albion-turn（2026-11-28）：在 `context` 钩子里把本轮的 user 消息改写成
 * 「原话 + 一段回忆块 + 逐轮约定」——生产里 albion-turn 就是这么干的。
 *
 * 有了它，才能证明宿主送进 OV 分支的是**他自己的原话**，不是脑自己拼进去的那一堆。
 */
export default function (pi: any) {
  pi.on("context", async (event: any) => {
    const messages = Array.isArray(event?.messages) ? event.messages : [];
    const out = messages.map((m: any) => {
      if (m?.role !== "user" || typeof m.content !== "string") return m;
      return {
        ...m,
        content: `${m.content}\n\n<memory-context>\n（自检用的假回忆块，不该进 OV）\n</memory-context>\n\n【阿尔比恩本轮说话约定】\n（自检用的假约定，不该进 OV）`,
      };
    });
    return { messages: out };
  });
}
