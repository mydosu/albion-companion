// albion-todo.ts / albion-browser.ts / albion-draw.ts 的离线检查（WSL 里跑）：
//   node tests/pi-brain/check_todo_browser_draw.ts
// 前置：装了 agent-browser（0.43.0），~/.cache/ms-playwright 里有对应 Chromium。
// 全用本机假服务与临时目录：不上外网、不调号池、不碰她真实的 state/ 与画廊。
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { execFileSync } from "node:child_process";
import { existsSync, mkdtempSync, readdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const root = mkdtempSync(join(tmpdir(), "todo-browser-draw-"));
process.env.ALBION_STATE_DIR = join(root, "state");
process.env.ALBION_DRAW_DIR = join(root, "画的");
process.env.PI_CODING_AGENT_DIR = root;
const BROWSER_SESSION = `browser-check-${process.pid}`;
process.env.ALBION_BROWSER_SESSION = BROWSER_SESSION;
process.env.ALBION_BROWSER_PROFILE = join(root, "browser-profile");

// 假网站 + 假号池（生图返回一个本机 url，再从那里取 JPEG 字节）
const JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0, 16, 74, 70, 73, 70, 0, 1, 0xff, 0xd9]);
const srv = createServer((req, res) => {
  if (req.url === "/") {
    res.setHeader("content-type", "text/html; charset=utf-8");
    return res.end(`<title>测试页</title><h1>阿尔比恩的测试页</h1><a href="/next">下一页</a>
      <input id="q"><button onclick="document.querySelector('h1').innerText='搜到：'+document.querySelector('#q').value">搜索</button>`);
  }
  if (req.url === "/next") { res.setHeader("content-type", "text/html; charset=utf-8"); return res.end("<title>第二页</title><p>这里是第二页</p>"); }
  if (req.url === "/v1/images/generations" && req.method === "POST") {
    res.setHeader("content-type", "application/json");
    return res.end(JSON.stringify({ data: [{ mime_type: "image/jpeg", url: `http://127.0.0.1:${port}/img/1` }] }));
  }
  if (req.url === "/img/1") { res.setHeader("content-type", "image/jpeg"); return res.end(JPEG); }
  res.statusCode = 404; res.end();
});
await new Promise<void>((r) => srv.listen(0, "127.0.0.1", r));
const port = (srv.address() as any).port;
writeFileSync(join(root, "models.json"), JSON.stringify({ providers: { grokpool: { baseUrl: `http://127.0.0.1:${port}/v1`, apiKey: "test" } } }));

const ext = (n: string) => new URL(`../../brain/agent/extensions/${n}`, import.meta.url).href;
const tools: Record<string, any> = {};
for (const n of ["albion-todo.ts", "albion-browser.ts", "albion-draw.ts"]) {
  const { default: f } = await import(ext(n));
  f({ registerTool: (t: any) => (tools[t.name] = t) } as any);
}
const call = (name: string, p: any) => tools[name].execute("t", p);
const txt = async (name: string, p: any) => String((await call(name, p)).content[0].text);

// todo
assert.ok((await txt("todo", { action: "list" })).includes("清单是空的"));
const plan = await txt("todo", { action: "plan", title: "整理画廊", steps: "1. 列出画廊\n- 挑三张\n写说明" });
assert.ok(plan.includes("还剩 3 / 3 步") && plan.includes("☐ #2 挑三张"), plan);
assert.ok((await txt("todo", { action: "done", id: 2 })).includes("还剩 2 / 3 步"));
assert.ok((await txt("todo", { action: "add", steps: "给他看" })).includes("☐ #4 给他看"));
assert.ok((await txt("todo", { action: "done", id: 99 })).includes("没有 #99"));

// browser
const opened = await txt("browser", { action: "open", url: `http://127.0.0.1:${port}/` });
assert.ok(opened.includes("测试页") && opened.includes("下一页"), opened);
assert.ok((await txt("browser", { action: "read" })).includes("阿尔比恩的测试页"));
const typed = await txt("browser", { action: "type", target: "#q", value: "灯" });
assert.ok(typed.includes("测试页"));
const clicked = await txt("browser", { action: "click", target: "搜索" });
assert.ok(!clicked.includes("这一步没做成"), clicked);
assert.ok((await txt("browser", { action: "read" })).includes("搜到：灯"));
const next = await txt("browser", { action: "click", target: "下一页" });
assert.ok(next.includes("第二页"), next);
const back = await txt("browser", { action: "back" });
assert.ok(back.includes("测试页"), back);
const shot = await call("browser", { action: "shot" });
assert.equal(shot.content[1].type, "image");
assert.ok((await txt("browser", { action: "open", url: "file:///etc/passwd" })).includes("只开 http/https"));
assert.ok((await txt("browser", { action: "click", target: "#不存在" })).includes("没做成"));

// draw
const drawn = await call("draw", { prompt: "一盏油灯" });
assert.ok(String(drawn.content[0].text).includes("画好了"), drawn.content[0].text);
assert.equal(drawn.content[1].type, "image");
assert.equal(readdirSync(process.env.ALBION_DRAW_DIR!).length, 1);
assert.ok((await txt("draw", { prompt: "" })).includes("要写想画什么"));

srv.close();
try {
  execFileSync("agent-browser", ["--session", BROWSER_SESSION, "close"], { timeout: 30_000 });
} catch {}
console.log("TODO/BROWSER/DRAW OK：清单列/勾/加；浏览器开/填/点/翻页/后退/截图/点不到；画画存进画廊并交图");
process.exit(0);
