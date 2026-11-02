// 官方 OpenViking 记忆扩展：每轮自动捕获 + 服务端工具面（openviking_*）。
// 主体在 ../openviking/（不直接放在 extensions/ 下，免得被 pi 的自动发现重复加载）。
// 召回仍由 albion-turn.ts 走 bin/memory.py —— 官方的自动召回已关（agent/.env 的 OPENVIKING_AUTO_RECALL=false），
// 免得同一轮出现两份回忆、还把"约定收尾"的顺序打乱。
export { default } from "../openviking/index.ts";
