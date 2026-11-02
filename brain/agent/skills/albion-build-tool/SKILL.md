---
name: albion-build-tool
description: 想给自己加新本事时读：写 Pi 扩展（agent/extensions/*.ts）、注册新工具、在工具里再调模型、装 npm 库。改我自己的身体前必读，写坏了整批扩展起不来、那一轮我就说不出话。
---

# 给自己加工具

扩展放 `/root/albion-pi/agent/extensions/*.ts`，照 `grab-page.ts`（调网络/外部命令）或 `delegate.ts`（跑子进程）的样子写。
Pi 自己的文档与例子：`/usr/local/lib/node_modules/@earendil-works/pi-coding-agent/docs/`（扩展看 `extensions.md`）与同级 `examples/extensions/`。先读再写，别凭印象编 API。

宿主下一次收到消息时发现文件变了，就换一具新身子整批加载——**一个扩展写坏，整批都起不来，那一轮我就说不出话**。所以：

1. `parameters` 必须是 typebox 的 `Type.Object({...})`，并且 `import { Type } from "typebox"`。
   写成普通对象本地不报错，上游却回 `400 invalid_client_tool_schema`。
2. 一次只加一件。存完立刻自检（不叫模型、不起会话，几秒出结果）：
   ```
   node /root/albion-pi/bin/check_extensions.ts
   ```
   没写"扩展自检通过"就是没过：照它列出的文件和原因改，改完再跑，直到通过。
3. 想真跑一下里面的函数：扩展是 ESM 的 TypeScript，用绝对路径 `import()`，不要 `require()`，也不要 `node 文件.ts`：
   ```
   node -e "import('/root/albion-pi/agent/extensions/page-outline.ts').then(m => m.getPageOutline('https://example.com')).then(console.log)"
   ```
4. 不要用 `bash cli.sh` 或再起一个 `pi` 来自检：那会把话塞进我正在干活的会话，还可能挂住不退、一层层递归。
5. 先想清楚这件本事靠什么实现：能在本机算出来的（农历节气、日期换算、单位换算……）优先找现成的库，别去找网上接口。
   要装库就在扩展目录里装，走国内镜像（npmjs 直连不通）：
   ```
   cd /root/albion-pi/agent/extensions && npm install <包名> --registry=https://registry.npmmirror.com
   ```
   装完 `package.json` 会多一行依赖；扩展里照常 `import`。
6. 外网接口直连不通，就加代理 `-x http://127.0.0.1:7897` 试一次；还不通就是用不了，换做法（换个库、换个来源、本机算），
   同一个地址别反复试。真做不成，就照实告诉指挥官卡在哪、试过什么，不要一句话不说就收工。

## 工具里要再调模型

连本机号池 `http://127.0.0.1:8000/v1`（地址与 key 在 `agent/models.json` 的 `grokpool`，照 `look-image.ts` 的 `pool()` 读）；
commandcode 那条走本机转发 `http://127.0.0.1:8798/v1`，别直连。

## 技能

做成一件以后还会再做的事，用 `skill` 工具记进我的技能本（`/root/家/技能/`）。
