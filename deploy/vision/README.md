# 视觉机部署包：llama.cpp + Qwen3-VL 4B

她（阿尔比恩）的 `look` 工具里，摄像头那一路（`cam`）**只**交给局域网里的本地视觉模型，
照片不出局域网。这台「视觉机」就是跑那个模型的地方：Linux + NVIDIA 卡 + Docker 里的
llama.cpp server，开一个 OpenAI 兼容接口，端口 8080。

参考部署：i5-9400F / GTX 1660 Ti 6 GB / 16 GB 内存，跑 4B 的 Q4_K_M 刚好够。
没有视觉机也能用：摄像头那一路会「这次看不到」，屏幕两路（`his`/`mine`）不受影响。

```
deploy/vision/
├── README.md            这份
├── .env.example         参数模板（复制成 .env 再改，.env 不进 Git）
├── docker-compose.yml   容器：镜像、端口、挂载、GPU、llama-server 参数
├── download_models.sh   从 Hugging Face 下主模型 + mmproj，校验文件头是 GGUF
└── firewall.sh          只让局域网和 tailnet 访问 8080；装成 systemd 服务，docker 重启后自动重写
```

## 前置

1. **Linux**（Ubuntu 22.04/24.04 之类都行），有 `sudo`。
2. **NVIDIA 驱动**：`nvidia-smi` 能出表。GTX 1660 Ti（Turing）用 550 以上都行。
3. **nvidia-container-toolkit**：让容器能用 GPU。

```bash
# Ubuntu / Debian，官方源
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
  | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
```

4. **Docker Engine + compose 插件**（`docker compose version` 有输出）。
   `gpus: all` 需要 compose v2.30 以上；更老的把 `docker-compose.yml` 里那行换成文件里注释掉的
   `deploy.resources.reservations.devices` 写法。

验证容器能看见卡：

```bash
docker run --rm --gpus all nvcr.io/nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

## 步骤

```bash
git clone <本仓库> && cd <本仓库>/deploy/vision   # 或者只把这几个文件拷到视觉机上
cp .env.example .env
$EDITOR .env                                     # 至少确认 MODEL_FILE / MMPROJ_FILE / MODEL_DIR
bash download_models.sh                          # 下到 .env 里的 MODEL_DIR（默认 ./models）
sudo bash firewall.sh install                    # 只放行局域网 + tailnet
docker compose up -d
docker compose logs -f --tail 50                 # 看到 "server is listening" 就好了
```

模型文件是 `Qwen/Qwen3-VL-4B-Instruct-GGUF` 里的
`Qwen3VL-4B-Instruct-Q4_K_M.gguf` + `mmproj-Qwen3VL-4B-Instruct-F16.gguf`。
国内直连 HF 慢的话，在 `.env` 里把 `HF_ENDPOINT` 改成 `https://hf-mirror.com` 再跑下载脚本。

## 验收

在视觉机上：

```bash
curl -s http://127.0.0.1:8080/health          # {"status":"ok"}
curl -s http://127.0.0.1:8080/props | head -c 400
```

`/props` 里应该有 `"model_path":"/models/Qwen3VL-4B-Instruct-Q4_K_M.gguf"`、
`"modalities":{"vision":true,...}`、`"n_ctx":5120`。带图请求（把 `cam.jpg` 换成你的图）：

```bash
curl -s http://127.0.0.1:8080/v1/chat/completions -H 'Content-Type: application/json' -d "$(python3 - <<'PY'
import base64, json
img = base64.b64encode(open('cam.jpg', 'rb').read()).decode()
print(json.dumps({
    "max_tokens": 64,
    "messages": [{"role": "user", "content": [
        {"type": "text", "text": "用一句话说这张图里有什么"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + img}},
    ]}],
}))
PY
)"
```

能返回一段中文描述就对了。再从**别的机器**验一次防火墙（这是她真正走的那条路）：

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://<视觉机 Tailscale IP>:8080/health   # 200
```

从一台不在局域网、也不在 tailnet 的机器上访问应该是连不上（不是 401，是根本不通）。

## 接进她的脑

两处地址填同一个：

- 脑（WSL）的 `agent/.env`：`ALBION_EYES_VLM=http://<视觉机 Tailscale IP>:8080/v1/chat/completions`
  （每次调用现读，改完不用重启脑）。
- 壳（`shell/apps/stage-tamagotchi/.env.local`）：`VITE_ALBION_VISION_BASE`，改完要重新打包。

配好之后让她「看一下摄像头」，她回的应该是一段文字描述，不是照片。

## 防火墙说明

Docker 发布端口时会绕过 `INPUT` 链，直接走 `DOCKER-USER`；而 Docker 每次启动都会重建这条链，
里面的规则会掉。所以 `firewall.sh install` 做两件事：

1. 把脚本装到 `/usr/local/bin/docker-user-rules.sh`；
2. 装一个 `docker-user-rules.service`（`PartOf=docker.service`），docker 起来就重跑，
   `apply` 会清空 `DOCKER-USER` 再按 `.env` 的 `ALLOWED_SOURCES` 重建。

`ALLOWED_SOURCES` 默认 `192.168.0.0/16 10.0.0.0/8 100.64.0.0/10`（局域网 + tailnet）。
换网段就改 `.env` 再 `sudo systemctl restart docker-user-rules.service`；
`sudo bash firewall.sh status` 看现在的规则，`remove` 收摊。

这台机器上如果还有别的容器要放行，别直接跑 `apply`（会清空整条链），把规则并进 `firewall.sh`。

## 已确认和未确认

**已确认**（直接问生产上那个 llama-server 的 `/props` 和 `/health`，不是猜的）：
模型文件 `Qwen3VL-4B-Instruct-Q4_K_M.gguf`（`model_path` 就在 `/models/` 下）、
`n_ctx` 5120、`total_slots` 1、端口 8080、`modalities.vision=true`（图片能进）。

**未确认**（没进过视觉机实机核对，文件里按通用默认写，并在注释里标了「未确认」）：

- mmproj 文件名：按 Qwen 官方 GGUF 仓库 `mmproj-Qwen3VL-4B-Instruct-F16.gguf` 写，生产上叫什么没核对；
- docker 镜像 tag：`server-cuda` 是 CUDA 通用 tag，生产钉的构建号不知道；
- `--n-gpu-layers`：99（全放显存）是通用值；
- restart 策略：`unless-stopped` 是通用值；
- 宿主机绑定地址：`0.0.0.0` + 防火墙，生产是不是这样没核对；
- 防火墙脚本：生产上是一份手写的 `/usr/local/bin/docker-user-rules.sh`，具体规则行和它由谁触发
  （systemd？docker 的钩子？）都没读到；本包的 `firewall.sh` 只是照「只放行局域网 + tailnet」
  这个行为用 `PartOf=docker.service` 复刻，`ALLOWED_SOURCES` 的网段也是通用值；
- 视觉机的发行版和内核、NVIDIA 驱动版本、nvidia-container-toolkit 配置：都没读到。

只有 `docker inspect`（镜像、完整命令行、挂载、端口、GPU 参数、restart 策略）和
`ls -la` 模型目录能把这些坐实；拿到之后照实际值改 `.env` 和 `docker-compose.yml` 就行。
