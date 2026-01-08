# 企业微信应用部署说明

## 文件说明

### 核心程序文件
- `quark_app.py` - 主程序文件，企业微信应用服务器
- `wechat_app.py` - 企业微信API封装
- `quark_manager.py` - 夸克网盘文件管理
- `utils.py` - 工具函数
- `WXBizMsgCrypt3.py` - 微信消息加解密库

### 配置文件
- `config/bot_config.json` - 主配置文件（需要根据实际情况修改）
- `config/bot_config.json.example` - 配置示例文件

### 依赖文件
- `requirements.txt` - Python依赖包列表

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

### 2. 配置设置

编辑 `config/bot_config.json`，配置以下信息：

```json
{
  "mode": "app",
  "corp_id": "你的企业ID",
  "agent_id": "你的应用ID",
  "secret": "你的应用密钥",
  "token": "自定义令牌（用于URL验证）",
  "encoding_aes_key": "消息加密密钥（43位，可选）",
  "host": "0.0.0.0",
  "port": 8888,
  "default_folder_id": "默认保存文件夹ID",
  "search_folder_id": "默认搜索文件夹ID",
  "proxy": "https://qyapi.weixin.qq.com"
}
```

### 3. 设置Cookie

将夸克网盘的Cookie保存到 `config/cookies.txt` 文件中（如果使用文件方式）。

或者通过企业微信应用发送 `cookie: <你的cookie>` 来设置。

### 4. 运行程序

```bash
python3 quark_app.py
```

### 5. 配置企业微信应用

在企业微信管理后台配置回调URL：
- URL: `http://你的服务器IP:8888/wechat/callback`
- Token: 与配置文件中的 `token` 一致
- EncodingAESKey: 与配置文件中的 `encoding_aes_key` 一致（可选）

## 功能说明

### 菜单功能
- **搜索** - 点击进入搜索模式，直接输入关键词搜索文件
- **帮助** - 查看使用说明
- **管理** - 验证Cookie

### 消息命令
- 直接发送夸克网盘链接 - 自动转存并生成分享链接
- `/search <关键词>` - 搜索文件
- `cookie: <cookie内容>` - 设置Cookie
- `verify` - 验证Cookie
- `/help` - 查看帮助

## 注意事项

1. **端口占用**：确保8888端口未被占用，或修改配置文件中的端口
2. **防火墙**：确保服务器的8888端口对外开放
3. **IP白名单**：如果企业微信应用配置了IP白名单，需要将服务器IP添加到白名单
4. **代理配置**：2022年6月20日后创建的自建应用需要配置代理地址

## 故障排查

1. **菜单未显示**：检查程序启动日志，确认菜单创建是否成功
2. **消息无法接收**：检查回调URL配置、Token和EncodingAESKey是否正确
3. **转存失败**：检查Cookie是否有效，运行 `verify` 命令验证

## 文件结构

```
wechat_app_deploy/
├── quark_app.py              # 主程序
├── wechat_app.py             # 企业微信API
├── quark_manager.py          # 夸克网盘管理
├── utils.py                  # 工具函数
├── WXBizMsgCrypt3.py         # 消息加解密
├── requirements.txt          # 依赖列表
├── config/
│   ├── bot_config.json       # 配置文件
│   └── bot_config.json.example
└── README_部署说明.md        # 本文件
```

