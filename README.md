# LvYou Panel - 绿微设备群控管理系统

局域网内绿微 4G 路由器设备的集中管理面板。

## 功能

- 🖥 **实时控制面板** — 设备在线状态、信号强度、SIM 卡信息
- 📩 **短信管理** — 收发短信记录缓存、搜索过滤、发送短信
- 📞 **通话记录** — 呼入/呼出日志
- ⚙️ **设备管理** — 添加/删除设备、自动 token 计算
- 📱 **自适应界面** — 手机/PC 双端适配
- 📡 **Webhook 推送** — 设备主动推送短信到面板缓存

## 一键安装

```bash
git clone https://github.com/xl-jeeter/lvyou-panel.git
cd lvyou-panel
chmod +x install.sh
sudo ./install.sh install
```

或直接在线安装：

```bash
curl -fsSL https://raw.githubusercontent.com/xl-jeeter/lvyou-panel/main/install.sh -o /tmp/lvyou-install.sh && \
chmod +x /tmp/lvyou-install.sh && \
sudo /tmp/lvyou-install.sh install
```

默认端口 **34567**，访问 `http://<机器IP>:34567`

## 手动运行

```bash
pip install -r requirements.txt
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 34567
```

## 运维命令

```bash
sudo ./install.sh status      # 服务状态
sudo ./install.sh restart     # 重启
sudo ./install.sh logs 100    # 查看日志
sudo ./install.sh backup      # 备份
sudo ./install.sh uninstall   # 卸载
```

## 设备配置

添加设备后，在设备 Web 后台（`http://设备IP/mgr`）的「快捷转发配置」中选择「开发者模式」，Webhook URL 设为 `http://<面板IP>:34567/webhook`，即可实现短信自动推送缓存。
