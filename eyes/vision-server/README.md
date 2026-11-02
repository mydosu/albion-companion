# 视觉机部署包：llama.cpp + Qwen3-VL 4B

她（阿尔比恩）的 `look` 工具里，摄像头那一路（`cam`）**只**交给局域网里的本地视觉模型，
照片不出局域网。这台「视觉机」就是跑那个模型的地方：Linux + NVIDIA 卡 + Docker 里的
llama.cpp server，开一个 OpenAI 兼容接口，端口 8080。

参考部署：i5-9400F / GTX 1660 Ti 6 GB / 16 GB 内存，跑 4B 的 Q4_K_M 刚好够。
没有视觉机也能用：摄像头那一路会「这次看不到」，屏幕两路（`his`/`mine`）不受影响。

```
eyes/vision-server/
├── README.md            这份
├── .env.example         参数模板（复制成 .env 再改，.env 不进 Git）
├── docker-compose.yml   容器：镜像、端口、挂载、GPU、llama-server 参数
├── download_models.sh   从 Hugging Face 下主模型 + mmproj，校验文件头是 GGUF
├── firewall.sh          只让局域网和 tailnet 访问 8080；装成 systemd 服务，docker 重启后自动重写
└── .gitattributes       这几个文件强制 LF（CRLF 的 .sh 在 Linux 上跑不起来）
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
   文件里用的是 `deploy.resources.reservations.devices` 写法，老版本 compose 也认。

验证容器能看见卡：

```bash
docker run --rm --gpus all nvcr.io/nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

## 步骤

```bash
git clone <本仓库> && cd <本仓库>/eyes/vision-server   # 或者只把这几个文件拷到视觉机上
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

## 实机核对结果

以下是从一台在跑的生产部署上逐项读出来的（`docker inspect`、`/props`、模型目录 `ls -la`、`/etc`
下的配置），不是通用猜测值；`.env.example` 和 `docker-compose.yml` 里的默认值就是这些。

- **系统 / 运行时**：Ubuntu 26.04 LTS（内核 7.0），Docker Engine 29.x + compose 插件 v5.x，
  NVIDIA 驱动 595.x，nvidia-container-toolkit 1.19.x，GTX 1660 Ti 6 GB。
- **容器**：名字 `llama-cpp`，镜像 `ghcr.io/ggml-org/llama.cpp:server-cuda`
  （本地实际解析到的构建号是 `b9859`），`restart: unless-stopped`，`8080:8080` 发布在所有网卡
  （IPv4 + IPv6），只挂模型目录到 `/models`，GPU 走 `deploy.resources.reservations.devices`
  （driver nvidia、count 1、capabilities [gpu]）并设 `CUDA_VISIBLE_DEVICES=0`。
- **实际命令**（`/props` 也对得上：`model_path` 在 `/models/` 下、`n_ctx` 5120、`total_slots` 1、
  `modalities.vision=true`）：

  ```bash
  llama-server -m /models/Qwen3VL-4B-Instruct-Q4_K_M.gguf     --mmproj /models/mmproj-Qwen3VL-4B-Instruct-F16.gguf     --host 0.0.0.0 --port 8080 -ngl 99 --ctx-size 5120 --threads 4 --parallel 1
  ```

- **模型文件**：目录里确认了 `Qwen3VL-4B-Instruct-Q4_K_M.gguf`（约 2.5 GB）和
  `mmproj-Qwen3VL-4B-Instruct-F16.gguf`（约 0.8 GB），`download_models.sh` 默认下的就是这两个。
- **它怎么起来的**：不是单跑这一份 compose，而是跟别的容器一起躺在同一个 compose 项目里
  （同一份 `docker-compose.yaml`，模型用 `./models/llama.cpp:/models` 这种相对挂载）。
  那台机器上别再把这份文件单独 `up` 一次，容器名会撞。
- **防火墙怎么触发**：生产用 `/etc/systemd/system/docker.service.d/override.conf` 里的
  `ExecStartPost=/usr/local/bin/docker-user-rules.sh`，docker 一起来就把 `DOCKER-USER` 重写一遍。
  那份手写脚本比本包管得多：顺手封了 ollama 和几个内部端口、放行 docker 网桥网段、还补了 IPv6 NAT。
  本包只管视觉模型这一个端口，复刻时用 `firewall.sh install` 装独立 unit（`PartOf=docker.service`，
  效果一样），或者照它写个 drop-in。

`DOCKER-USER` 在 DNAT 之后才走，所以 `--dport` 匹配的是**容器端口**（8080），不是宿主发布端口；
`firewall.sh` 按这个写，改 `VLM_PORT` 不用动规则。

**还剩的环境差异**：生产那台的 IPv6 侧把这些端口全 DROP、不单独放行来源；本包默认对 IPv6 也放行
`ALLOWED_SOURCES`（tailnet IPv6 也能用）。不需要就 `VLM_IPV6=0`。
