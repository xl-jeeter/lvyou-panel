# LvYou Panel - 绿微设备群控管理系统

局域网内绿微 4G 路由器设备的集中管理面板。

## 功能

- 🖥 **实时控制面板** — 设备在线状态、信号强度、SIM 卡信息
- 📩 **短信管理** — 收发短信记录缓存、搜索过滤、发送短信
- 📞 **通话记录** — 呼入/呼出日志
- ⚙️ **设备管理** — 添加/删除设备、自动 token 计算
- 📱 **自适应界面** — 手机/PC 双端适配
- 📡 **Webhook 推送** — 设备主动推送短信到面板缓存

## 快速开始

### 使用 Docker（推荐）

```bash
docker run -d \
  --name lvyou-panel \
  -p 34567:34567 \
  -v /path/to/data:/data \
  -e TZ=Asia/Shanghai \
  xilianghe/lvyou-panel:latest
```

### 使用 Docker Compose

创建 `docker-compose.yml`：

```yaml
version: "3.9"
services:
  panel:
    image: xilianghe/lvyou-panel:latest
    container_name: device-panel
    restart: unless-stopped
    ports:
      - "34567:34567"
    volumes:
      - ./data:/data
    environment:
      - TZ=Asia/Shanghai
```

然后运行：

```bash
docker compose up -d
```

### 从源码安装

```bash
git clone https://github.com/xl-jeeter/lvyou-panel.git
cd lvyou-panel
chmod +x install.sh
sudo ./install.sh install
```

## 访问

默认端口 **34567**，访问 `http://<机器IP>:34567`

## 设备配置

添加设备后，在设备 Web 后台（`http://设备IP/mgr`）的「快捷转发配置」中选择「开发者模式」，Webhook URL 设为 `http://<面板IP>:34567/webhook`，即可实现短信自动推送缓存。

## 运维命令

```bash
# 查看服务状态
docker ps | grep device-panel

# 查看日志
docker logs device-panel --tail=100

# 重启服务
docker restart device-panel

# 停止服务
docker stop device-panel

# 更新镜像
docker pull xilianghe/lvyou-panel:latest
docker compose up -d
```

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `TZ` | 时区 | `Asia/Shanghai` |

## 数据持久化

| 路径 | 说明 |
|------|------|
| `/data` | 数据库和配置文件 |

## 技术栈

- **后端**: Python 3.12 + FastAPI + Uvicorn
- **数据库**: SQLite (aiosqlite)
- **前端**: 原生 HTML/CSS/JavaScript
- **容器**: Docker

## 许可证

MIT License

## 链接

- [GitHub](https://github.com/xl-jeeter/lvyou-panel)
- [Docker Hub](https://hub.docker.com/r/xilianghe/lvyou-panel)
